from __future__ import annotations

import dataclasses
import importlib
import os
import queue
import shlex
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from ._router import (
    JsonRouteCache,
    RouterConfig,
    RouterLookupTimeoutError,
    load_router_config,
    provider_accepts_identifier,
    select_weighted_provider,
)
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


class CompatibilityCreationNotStartedError(ValueError):
    """Local preflight refusal, before any sandbox creation request is issued."""


class _InstanceOrClassMethod:
    def __init__(self, method):
        self.method = method

    def __get__(self, instance, owner):
        return (
            self.method.__get__(instance, owner)
            if instance is not None
            else getattr(owner, f"_class_{self.method.__name__}")
        )


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


def pop_arker_config(options: dict[str, Any]) -> dict[str, Any]:
    """Consume shim options, keeping snake-case router-path precedence."""
    config = options.pop("arker", None) or {}
    router_path = options.pop("router_config_path", options.pop("routerConfigPath", None))
    return {**config, "router_config_path": router_path} if router_path else config


def arker_client(config: dict[str, Any] | None = None) -> Arker:
    cfg = {key: value for key, value in (config or {}).items() if key in ARKER_CLIENT_KEYS}
    return Arker(**cfg)


def arker_source(config: dict[str, Any] | None = None) -> str:
    cfg = config or {}
    source = cfg.get("source") or os.environ.get("ARKER_SOURCE") or os.environ.get("ARKER_SOURCE_VM")
    if not source:
        raise CompatibilityCreationNotStartedError(
            "Arker source is required; set source, ARKER_SOURCE, or ARKER_SOURCE_VM_ID"
        )
    return str(source)


def arker_create(
    config: dict[str, Any] | None = None,
    *,
    name: str | None = None,
    template_id: str | None = None,
    image: str | None = None,
    image_config: dict[str, Any] | None = None,
    resources: dict[str, Any] | None = None,
    policies: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    fork: Callable[..., VM] | None = None,
) -> VM:
    cfg = config or {}
    request: dict[str, Any] = {"name": name or cfg.get("name")}
    if image:
        request["image"] = image
        if cfg.get("registry_auth") is not None:
            request["registry_auth"] = cfg["registry_auth"]
    elif not template_id and (source_id := cfg.get("source_vm_id") or os.environ.get("ARKER_SOURCE_VM_ID")):
        request["source_vm_id"] = source_id
    else:
        request["source_vm_name"] = template_id or arker_source(config)
    image_config = image_config if image_config is not None else cfg.get("image_config")
    if image_config is not None:
        if not image:
            raise UnsupportedCompatibilityOption("image_config requires an image source")
        request["image_config"] = image_config
    if metadata is not None:
        request["metadata"] = metadata
    if resources:
        request["resources"] = resources
    fork_policies = dict(cfg.get("policies") or {})
    if policies is not None:
        if "policies" in policies:
            fork_policies.pop("rules", None)
        rules = {**fork_policies.get("rules", {}), **policies.get("rules", {})}
        fork_policies.update(policies)
        if rules:
            fork_policies["rules"] = rules
    if fork_policies:
        request["policies"] = fork_policies
    client = arker_client(config)
    return fork(client, request) if fork is not None else client.fork(**request)


def compat_network_policies(
    *,
    block_all: bool = False,
    domains: str | list[str] | None = None,
    cidrs: str | list[str] | None = None,
    domain_ports: list[int] | None = None,
    empty_allowlist_blocks: bool = False,
) -> dict[str, Any] | None:
    def values(value: str | list[str] | None) -> list[str]:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if value is None:
            return []
        if not isinstance(value, Sequence) or not all(isinstance(entry, str) for entry in value):
            raise TypeError("network allowlist must be a string or sequence of strings")
        return list(value)

    domain_values = values(domains)
    cidr_values = values(cidrs)
    allowlist_present = domains is not None or cidrs is not None
    if block_all and (domain_values or cidr_values or (empty_allowlist_blocks and allowlist_present)):
        raise ValueError("block-all network policy cannot be combined with outbound allowlists")
    if not block_all and not domain_values and not cidr_values and not (empty_allowlist_blocks and allowlist_present):
        return None
    allow: list[dict[str, Any]] = []
    if domain_values:
        match: dict[str, Any] = {"hosts": domain_values}
        if domain_ports:
            match["ports"] = domain_ports
        allow.append(match)
    if cidr_values:
        allow.append({"ips": cidr_values})
    # Independent floors intersect inherited permissions without discarding
    # inbound, lifecycle, or credential-injection rules.
    return {"rules": {f"computesdk-egress-{uuid.uuid4().hex}": {"type": "outbound_guard", "allow": allow}}}


def arker_get(vm_id: str, config: dict[str, Any] | None = None, **request_options: Any) -> VM | None:
    try:
        return arker_client(config).get_vm(vm_id, **request_options)
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


class CompatibilityNotFoundError(RuntimeError):
    pass


def format_not_found(kind: str, sandbox_id: str) -> CompatibilityNotFoundError:
    return CompatibilityNotFoundError(f"{kind} {sandbox_id} not found")


def get_or_none(getter: Callable[[], Any]) -> Any | None:
    try:
        return getter()
    except Exception as error:
        response = getattr(error, "response", None)
        status = (
            getattr(error, "status_code", None) or vars(error).get("status") or getattr(response, "status_code", None)
        )
        code = str(getattr(error, "code", "")).lower()
        if status is not None and status != 404:
            raise
        if status == 404 or code in {"404", "not_found", "notfound"}:
            return None
        for module_name, class_name in (
            ("modal.exception", "NotFoundError"),
            ("e2b.exceptions", "SandboxNotFoundException"),
            ("e2b.exceptions", "VolumeNotFoundException"),
            ("daytona.common.errors", "DaytonaNotFoundError"),
        ):
            if any(base.__module__ == module_name for base in type(error).__mro__):
                module = import_optional(module_name)
                missing_type = getattr(module, class_name, None)
                if isinstance(missing_type, type) and isinstance(error, missing_type):
                    return None
        raise


def router_config(
    config: dict[str, Any] | None, native_provider: str, *, providers: dict[str, Any] | None = None
) -> RouterConfig:
    cfg = config or {}
    path = cfg.get("router_config_path") or cfg.get("routerConfigPath") or os.environ.get("COMPUTESDK_ROUTER_CONFIG")
    if path:
        return load_router_config(path)
    default_path = Path("computesdk_router.json")
    if default_path.exists():
        return load_router_config(default_path)
    has_arker = bool(cfg.get("api_key") or os.environ.get("ARKER_API_KEY"))
    order = ["arker", native_provider] if has_arker else [native_provider]
    if providers is not None:
        order = [name for name in order if name in providers]
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


def _instance_identifiers(instance: Any) -> list[str]:
    values = [getattr(instance, key, None) for key in ("id", "sandbox_id", "object_id", "name")]
    metadata = getattr(instance, "metadata", None)
    values.append(metadata.get("name") if isinstance(metadata, dict) else getattr(metadata, "name", None))
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def _is_retryable(error: Exception) -> bool:
    # Even a 429 can come from polling a sandbox that was already created.
    return isinstance(error, CompatibilityCreationNotStartedError)


def routed_create(
    native_provider: str,
    config: dict[str, Any] | None,
    creators: dict[str, Any],
    *,
    resource: str | None = None,
    resource_id: Callable[[Any], str] | None = None,
) -> tuple[str, Any]:
    router = router_config(config, native_provider, providers=creators)
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
        except Exception as error:
            last_error = error
            if not _is_retryable(error):
                raise
            continue
        # A cache/metadata failure after successful creation must never send
        # the same request to a second provider.
        with suppress(Exception):
            cache = _cache(router)
            identifiers = [resource_id(result)] if resource_id else _instance_identifiers(result)
            for identifier in identifiers:
                if isinstance(identifier, str):
                    _cache_set(cache, f"{resource}:{identifier}" if resource else identifier, name)
        return name, result
    if last_error:
        raise last_error
    raise ValueError(f"router config contains neither arker nor {native_provider}")


def routed_get(
    identifier: str,
    native_provider: str,
    config: dict[str, Any] | None,
    getters: dict[str, Any],
    *,
    resource: str | None = None,
    require_unique: bool = False,
) -> tuple[str, Any] | None:
    cache_identifier = f"{resource}:{identifier}" if resource else identifier
    router = router_config(config, native_provider, providers=getters)
    started = time.monotonic()

    def remaining() -> float:
        seconds = router.existing.timeout_ms / 1000 - (time.monotonic() - started)
        if seconds <= 0:
            raise RouterLookupTimeoutError(f"ownership lookup timed out for {identifier}")
        return min(seconds, threading.TIMEOUT_MAX)

    def wait_result(results):
        try:
            result = results.get(timeout=remaining())
            remaining()
            return result
        except queue.Empty:
            raise RouterLookupTimeoutError(f"ownership lookup timed out for {identifier}") from None

    def probe(name):
        remaining()
        results = queue.Queue()

        def lookup():
            try:
                results.put((getters[name](), None))
            except BaseException as error:
                results.put((None, error))

        threading.Thread(target=lookup, daemon=True).start()
        result, error = wait_result(results)
        if error is not None:
            raise error
        return result

    _validate_router_providers(list(router.existing.order), getters)
    eligible = {
        name
        for name in router.existing.order
        if resource or provider_accepts_identifier(name, identifier, arker_enabled="arker" in router.existing.order)
    }
    cache = None if require_unique else _cache(router)
    try:
        cached = cache.get(cache_identifier) if cache is not None else None
    except Exception:
        cached = None
    if cached is not None and (cached not in getters or cached not in eligible):
        if cache is not None:
            with suppress(Exception):
                cache.delete(cache_identifier)
        cached = None
    if cached in getters:
        result = probe(cached)
        if result is not None:
            return cached, result
        with suppress(Exception):
            cache.delete(cache_identifier)
    order = [name for name in router.existing.order if name in getters and name != cached and name in eligible]
    if (require_unique or router.existing.strategy == "race") and len(order) > 1:
        results: queue.Queue[tuple[str, Any, BaseException | None]] = queue.Queue()
        completed: dict[str, tuple[Any, BaseException | None]] = {}

        def lookup(name: str) -> None:
            try:
                results.put((name, getters[name](), None))
            except BaseException as error:
                results.put((name, None, error))

        for name in order:
            remaining()
            threading.Thread(target=lookup, args=(name,), daemon=True).start()
        for _ in order:
            try:
                name, result, error = wait_result(results)
            except RouterLookupTimeoutError as error:
                for name in order:
                    completed.setdefault(name, (None, error))
                break
            completed[name] = (result, error)
        for name in order:
            error = completed[name][1]
            if error is not None:
                raise error
        if require_unique and sum(completed[name][0] is not None for name in order) > 1:
            raise ValueError(f"Resource name {identifier!r} is ambiguous across configured providers")
        for name in order:
            result = completed[name][0]
            if result is not None:
                _cache_set(cache, cache_identifier, name)
                return name, result
        return None
    for name in order:
        result = probe(name)
        if result is not None:
            _cache_set(cache, cache_identifier, name)
            return name, result
    return None


def routed_list(native_provider: str, config: dict[str, Any] | None, listers: dict[str, Any]) -> list[Any]:
    """List each configured resource backend without treating errors as absence."""
    router = router_config(config, native_provider, providers=listers)
    _validate_router_providers(list(router.existing.order), listers)
    return [item for name in router.existing.order for item in listers[name]()]
