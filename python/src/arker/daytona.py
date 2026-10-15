from __future__ import annotations

import re
import shlex
from typing import Any

from ._compat import (
    CommandResult,
    UnsupportedMixin,
    arker_create,
    arker_get,
    format_not_found,
    import_optional,
    routed_create,
    routed_get,
    run_command,
)
from .computer import VM


class ExecuteResponse:
    def __init__(self, result: CommandResult) -> None:
        self.exit_code = result.exit_code
        self.exitCode = result.exit_code
        self.result = result.stdout
        self.stdout = result.stdout
        self.stderr = result.stderr


class Process:
    def __init__(self, vm: VM) -> None:
        self._vm = vm

    def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        prefix = ""
        if env:
            exports = []
            for key, value in env.items():
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
                    raise ValueError(f"Invalid environment variable name: {key}")
                exports.append(f"export {key}={shlex.quote(value)};")
            prefix += " ".join(exports) + " "
        if cwd:
            prefix += f"cd {shlex.quote(cwd)} && "
        return ExecuteResponse(run_command(self._vm, prefix + command, timeout=timeout))

    def execute_command(self, command: str, **kwargs: Any) -> ExecuteResponse:
        return self.exec(command, **kwargs)

    def executeCommand(self, command: str, **kwargs: Any) -> ExecuteResponse:
        return self.exec(command, **kwargs)


class Sandbox(UnsupportedMixin):
    _compat_name = "daytona"

    def __init__(self, vm: VM | None = None, native: Any | None = None) -> None:
        self._vm = vm
        self._native = native
        self.id = vm.id if vm is not None else getattr(native, "id", getattr(native, "sandbox_id", None))
        if vm is not None:
            self.process = Process(vm)
        else:
            self.process = native.process

    def delete(self) -> None:
        if self._vm is not None:
            self._vm.delete()
            return
        if hasattr(self._native, "delete"):
            self._native.delete()
            return
        raise AttributeError("Native Daytona sandbox does not expose delete().")


class Daytona(UnsupportedMixin):
    _compat_name = "daytona"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._native_args = args
        self._native_kwargs = dict(kwargs)
        self._arker_config = self._native_kwargs.pop("arker", None)
        router_path = self._native_kwargs.pop("router_config_path", self._native_kwargs.pop("routerConfigPath", None))
        self._arker_config = dict(self._arker_config or {})
        if router_path:
            self._arker_config["router_config_path"] = router_path
        self._native = None

    def _native_client(self) -> Any | None:
        if self._native is not None:
            return self._native
        module = import_optional("daytona")
        if module is None or not hasattr(module, "Daytona"):
            return None
        self._native = module.Daytona(*self._native_args, **self._native_kwargs)
        return self._native

    def create(self, *args: Any, **kwargs: Any) -> Sandbox:
        native = self._native_client()

        def create_native() -> Any:
            if native is None:
                raise RuntimeError("Native Daytona SDK is not installed")
            return native.create(*args, **kwargs)

        provider, result = routed_create(
            "daytona",
            self._arker_config,
            {
                "arker": lambda: arker_create(
                    self._arker_config,
                    name=kwargs.get("name") if kwargs else None,
                    template_id=kwargs.get("snapshot") if kwargs else None,
                ),
                "daytona": create_native,
            },
        )
        return Sandbox(vm=result if provider == "arker" else None, native=result if provider == "daytona" else None)

    def get(self, sandbox_id: str) -> Sandbox:
        native = self._native_client()
        found = routed_get(
            sandbox_id,
            "daytona",
            self._arker_config,
            {
                "arker": lambda: arker_get(sandbox_id, self._arker_config),
                "daytona": lambda: native.get(sandbox_id) if native is not None and hasattr(native, "get") else None,
            },
        )
        if found is not None:
            provider, result = found
            return Sandbox(vm=result if provider == "arker" else None, native=result if provider == "daytona" else None)
        raise format_not_found("Sandbox", sandbox_id)

    def delete(self, sandbox: str | Sandbox) -> None:
        if not isinstance(sandbox, str):
            sandbox.delete()
            return
        native = self._native_client()
        found = routed_get(
            sandbox,
            "daytona",
            self._arker_config,
            {
                "arker": lambda: arker_get(sandbox, self._arker_config),
                "daytona": lambda: (
                    native.get(sandbox)
                    if native is not None and hasattr(native, "get")
                    else None
                ),
            },
        )
        if found is None:
            if native is not None and hasattr(native, "delete"):
                native.delete(sandbox)
                return
            raise format_not_found("Sandbox", sandbox)
        provider, result = found
        if provider == "arker":
            Sandbox(vm=result).delete()
        else:
            Sandbox(native=result).delete()
