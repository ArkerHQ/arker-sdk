from __future__ import annotations

import dataclasses
import importlib
import os
import queue
import shlex
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from ._router import JsonRouteCache, RouterConfig, load_router_config, select_weighted_provider
from .computer import VM, Arker, ArkerError, CompletedRunResult

ARKER_CLIENT_KEYS = {
    "api_key",
    "base_url",
    "control_base_url",
    "region",
    "provider",
    "retry",
}


@dataclasses.dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int

    @property
    def exitCode(self) -> int:
        return self.exit_code


class UnsupportedCompatibilityOption(TypeError):
    """An option cannot be represented by the selected compatibility provider."""


class UnsupportedMixin:
    _compat_name = "provider"

    def __getattr__(self, name: str) -> Any:
        native = self.__dict__.get("_native")
        if native is not None:
            return getattr(native, name)
        raise AttributeError(
            f"'{name}' is not supported by the Arker compatibility layer for {self._compat_name}. "
            f"The compatibility layer covers create/connect/get, command execution, and core filesystem operations. "
            f"Use the native {self._compat_name} SDK directly for the full API."
        )


def arker_client(config: dict[str, Any] | None = None) -> Arker:
    cfg = {key: value for key, value in (config or {}).items() if key in ARKER_CLIENT_KEYS}
    return Arker(**cfg)


def arker_source(config: dict[str, Any] | None = None) -> str:
    cfg = config or {}
    source = cfg.get("source") or os.environ.get("ARKER_SOURCE") or os.environ.get("ARKER_SOURCE_VM")
    if not source:
        raise ValueError("Arker source is required; set source, ARKER_SOURCE, or ARKER_SOURCE_VM")
    return str(source)


def arker_create(
    config: dict[str, Any] | None = None,
    *,
    name: str | None = None,
    template_id: str | None = None,
    image: str | None = None,
    resources: dict[str, Any] | None = None,
    policies: dict[str, Any] | None = None,
) -> VM:
    cfg = config or {}
    request: dict[str, Any] = {"name": name or cfg.get("name")}
    if image:
        request["image"] = image
    else:
        request["source_vm_name"] = template_id or arker_source(config)
    if resources:
        request["resources"] = resources
    if policies:
        request["policies"] = policies
    return arker_client(config).fork(**request)


def compat_network_policies(
    *,
    block_all: bool = False,
    domains: str | list[str] | None = None,
    cidrs: str | list[str] | None = None,
    domain_ports: list[int] | None = None,
) -> dict[str, Any] | None:
    def values(value: str | list[str] | None) -> list[str]:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return list(value or [])

    domain_values = values(domains)
    cidr_values = values(cidrs)
    if block_all and (domain_values or cidr_values):
        raise ValueError("block-all network policy cannot be combined with outbound allowlists")
    if not block_all and not domain_values and not cidr_values:
        return None
    policies: list[dict[str, Any]] = []
    if domain_values:
        match: dict[str, Any] = {"hosts": domain_values}
        if domain_ports:
            match["ports"] = domain_ports
        policies.append(
            {
                "type": "outbound",
                "match": match,
                "action": "allow",
            }
        )
    if cidr_values:
        policies.append(
            {
                "type": "outbound",
                "match": {"ips": cidr_values},
                "action": "allow",
            }
        )
    policies.append({"type": "outbound", "action": "deny"})
    # The ordered form replaces the source VM's inherited policy. A keyed
    # update would merge and could let an inherited rule override the shim.
    return {"policies": policies}


def arker_get(vm_id: str, config: dict[str, Any] | None = None) -> VM | None:
    try:
        return arker_client(config).get_vm(vm_id)
    except ArkerError as error:
        if error.status == 404:
            return None
        raise


def decode(data: bytes, encoding: str = "utf-8") -> str:
    return data.decode(encoding, "replace")


def resolve_compat_path(path: str, default_directory: str) -> str:
    if path.startswith("/"):
        return path
    parts: list[str] = []
    for part in f"{default_directory}/{path}".split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
        else:
            parts.append(part)
    return "/" + "/".join(parts)


def run_command(vm: VM, command: str, *, timeout: int | None = None) -> CommandResult:
    # Vendor exec calls start a fresh shell; Arker's default session persists.
    result = vm.run(f"(\n{command}\n)", timeout=timeout)
    if not isinstance(result, CompletedRunResult):
        raise RuntimeError(f"Arker run did not complete synchronously (type={result.type}).")
    return CommandResult(
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
    )


def shell_command(parts: tuple[Any, ...]) -> str:
    if len(parts) == 1:
        value = parts[0]
        if isinstance(value, (list, tuple)):
            return " ".join(shlex.quote(str(part)) for part in value)
        return str(value)
    return " ".join(shlex.quote(str(part)) for part in parts)


def import_optional(module_name: str) -> Any | None:
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name is None or (error.name != module_name and not module_name.startswith(f"{error.name}.")):
            raise
        return None


def format_not_found(kind: str, sandbox_id: str) -> RuntimeError:
    return RuntimeError(f"{kind} {sandbox_id} not found")


def get_or_none(getter: Callable[[], Any]) -> Any | None:
    try:
        return getter()
    except Exception as error:
        response = getattr(error, "response", None)
        status = (
            getattr(error, "status_code", None) or vars(error).get("status") or getattr(response, "status_code", None)
        )
        code = str(getattr(error, "code", "")).lower()
        message = str(error).lower()
        if status == 404 or code in {"not_found", "notfound"} or "not found" in message:
            return None
        raise


def router_config(config: dict[str, Any] | None, native_provider: str) -> RouterConfig:
    cfg = config or {}
    path = cfg.get("router_config_path") or cfg.get("routerConfigPath") or os.environ.get("COMPUTESDK_ROUTER_CONFIG")
    if path:
        return load_router_config(path)
    default_path = Path("computesdk_router.json")
    if default_path.exists():
        return load_router_config(default_path)
    has_arker = bool(cfg.get("api_key") or os.environ.get("ARKER_API_KEY"))
    order = ["arker", native_provider] if has_arker else [native_provider]
    return RouterConfig.from_dict(
        {
            "version": 1,
            "existing": {"strategy": "priority", "order": order},
            "create": {"strategy": "priority", "order": order},
        }
    )


def _cache(router: RouterConfig) -> JsonRouteCache | None:
    if router.cache.path is None:
        return None
    return JsonRouteCache(router.cache.path, max_entries=router.cache.max_entries, max_bytes=router.cache.max_bytes)


def _validate_router_providers(names: tuple[str, ...] | list[str], handlers: dict[str, Any]) -> None:
    for name in names:
        if name not in handlers:
            configured = ", ".join(handlers)
            raise ValueError(f'router config references unknown provider "{name}"; configured providers: {configured}')


def _cache_set(cache: JsonRouteCache | None, identifier: str, provider: str) -> None:
    if cache is not None:
        with suppress(Exception):
            cache.set(identifier, provider)


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, UnsupportedCompatibilityOption):
        return False
    current: BaseException | None = error
    for _ in range(5):
        if current is None:
            break
        response = getattr(current, "response", None)
        status = (
            getattr(current, "status_code", None)
            or vars(current).get("status")
            or getattr(response, "status_code", None)
        )
        if status in {400, 401, 403, 422}:
            return False
        code = str(getattr(current, "code", "")).lower()
        if code in {"authentication", "authorization", "unauthorized", "forbidden", "invalid_request", "validation"}:
            return False
        message = str(current).lower()
        if any(text in message for text in ("authentication failed", "unauthorized", "forbidden", "invalid api key")):
            return False
        if any(
            text in message
            for text in (
                "outcome is unknown",
                "may have succeeded",
                "may have been created",
                "network failure",
                "transport error",
                "timed out",
                "timeout",
            )
        ):
            return False
        current = current.__cause__ or current.__context__
    return True


def routed_create(
    native_provider: str,
    config: dict[str, Any] | None,
    creators: dict[str, Any],
) -> tuple[str, Any]:
    router = router_config(config, native_provider)
    if router.create.strategy == "weighted":
        _validate_router_providers([*router.create.weights, *router.create.fallback], creators)
        first = select_weighted_provider(router.create.weights)
        order = (first, *(name for name in router.create.fallback if name != first))
    else:
        _validate_router_providers(list(router.create.order), creators)
        order = router.create.order
    last_error: Exception | None = None
    for name in order:
        try:
            result = creators[name]()
            identifier = getattr(result, "id", getattr(result, "sandbox_id", None))
            cache = _cache(router)
            if identifier:
                _cache_set(cache, str(identifier), name)
            return name, result
        except Exception as error:
            last_error = error
            if not _is_retryable(error):
                raise
    if last_error:
        raise last_error
    raise ValueError(f"router config contains neither arker nor {native_provider}")


def routed_get(
    identifier: str,
    native_provider: str,
    config: dict[str, Any] | None,
    getters: dict[str, Any],
) -> tuple[str, Any] | None:
    router = router_config(config, native_provider)
    _validate_router_providers(list(router.existing.order), getters)
    cache = _cache(router)
    try:
        cached = cache.get(identifier) if cache is not None else None
    except Exception:
        cached = None
    if cached is not None and cached not in getters:
        if cache is not None:
            with suppress(Exception):
                cache.delete(identifier)
        cached = None
    if cached in getters:
        result = getters[cached]()
        if result is not None:
            return cached, result
        with suppress(Exception):
            cache.delete(identifier)
    order = [name for name in router.existing.order if name in getters and name != cached]
    if router.existing.strategy == "race" and len(order) > 1:
        results: queue.Queue[tuple[str, Any, Exception | None]] = queue.Queue()
        errors: list[Exception] = []

        def lookup(name: str) -> None:
            try:
                results.put((name, getters[name](), None))
            except Exception as error:
                results.put((name, None, error))

        for name in order:
            threading.Thread(target=lookup, args=(name,), daemon=True).start()
        for _ in order:
            name, result, error = results.get()
            if error is not None:
                errors.append(error)
            elif result is not None:
                _cache_set(cache, identifier, name)
                return name, result
        if errors:
            raise RuntimeError(f"lookup failed for {identifier}: {errors[-1]}") from errors[-1]
        return None
    for name in order:
        result = getters[name]()
        if result is not None:
            _cache_set(cache, identifier, name)
            return name, result
    return None
