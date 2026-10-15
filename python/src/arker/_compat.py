from __future__ import annotations

import dataclasses
import importlib
import os
import shlex
from concurrent.futures import ThreadPoolExecutor, as_completed
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


class UnsupportedMixin:
    _compat_name = "provider"

    def __getattr__(self, name: str) -> Any:
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
) -> VM:
    cfg = config or {}
    source = template_id or arker_source(config)
    return arker_client(config).fork(
        source_vm_name=source,
        name=name or cfg.get("name"),
    )


def arker_get(vm_id: str, config: dict[str, Any] | None = None) -> VM | None:
    try:
        return arker_client(config).get_vm(vm_id)
    except ArkerError as error:
        if error.status == 404:
            return None
        raise


def decode(data: bytes, encoding: str = "utf-8") -> str:
    return data.decode(encoding, "replace")


def run_command(vm: VM, command: str, *, timeout: int | None = None) -> CommandResult:
    result = vm.run(command, timeout=timeout)
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


def router_config(config: dict[str, Any] | None, native_provider: str) -> RouterConfig:
    cfg = config or {}
    path = cfg.get("router_config_path") or cfg.get("routerConfigPath") or os.environ.get("COMPUTESDK_ROUTER_CONFIG")
    if path:
        return load_router_config(path)
    default_path = Path("computesdk_router.json")
    if default_path.exists():
        return load_router_config(default_path)
    return RouterConfig.from_dict(
        {
            "version": 1,
            "existing": {"strategy": "priority", "order": ["arker", native_provider]},
            "create": {"strategy": "priority", "order": ["arker", native_provider]},
        }
    )


def _cache(router: RouterConfig) -> JsonRouteCache | None:
    if router.cache.path is None:
        return None
    return JsonRouteCache(router.cache.path, max_entries=router.cache.max_entries, max_bytes=router.cache.max_bytes)


def routed_create(
    native_provider: str,
    config: dict[str, Any] | None,
    creators: dict[str, Any],
) -> tuple[str, Any]:
    router = router_config(config, native_provider)
    if router.create.strategy == "weighted":
        first = select_weighted_provider(router.create.weights)
        order = (first, *(name for name in router.create.fallback if name != first))
    else:
        order = router.create.order
    relevant = [name for name in order if name in creators]
    last_error: Exception | None = None
    for name in relevant:
        try:
            result = creators[name]()
            identifier = getattr(result, "id", getattr(result, "sandbox_id", None))
            cache = _cache(router)
            if cache is not None and identifier:
                cache.set(str(identifier), name)
            return name, result
        except Exception as error:
            last_error = error
            status = getattr(error, "status", None)
            if status in {400, 401, 403, 422}:
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
    cache = _cache(router)
    cached = cache.get(identifier) if cache is not None else None
    if cached is not None and cached not in getters:
        if cache is not None:
            cache.delete(identifier)
        cached = None
    if cached in getters:
        result = getters[cached]()
        if result is not None:
            return cached, result
        cache.delete(identifier)
    order = [name for name in router.existing.order if name in getters and name != cached]
    if router.existing.strategy == "race" and len(order) > 1:
        executor = ThreadPoolExecutor(max_workers=len(order))
        try:
            futures = {executor.submit(getters[name]): name for name in order}
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    name = futures[future]
                    if cache is not None:
                        cache.set(identifier, name)
                    return name, result
            return None
        finally:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
    for name in order:
        result = getters[name]()
        if result is not None:
            if cache is not None:
                cache.set(identifier, name)
            return name, result
    return None
