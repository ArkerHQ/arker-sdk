from __future__ import annotations

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
    shell_command,
)
from .computer import VM


class _Stream:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text

    def read_text(self) -> str:
        return self._text

    def readText(self) -> str:
        return self._text


class Process:
    def __init__(self, result: CommandResult | Any) -> None:
        if isinstance(result, CommandResult):
            self._exit_code = result.exit_code
            self.stdout = _Stream(result.stdout)
            self.stderr = _Stream(result.stderr)
        else:
            self._native = result
            self.stdout = result.stdout
            self.stderr = result.stderr
            self._exit_code = None

    def wait(self) -> int:
        native = getattr(self, "_native", None)
        if native is not None and hasattr(native, "wait"):
            return native.wait()
        return int(self._exit_code)


class Sandbox(UnsupportedMixin):
    _compat_name = "modal"

    def __init__(self, vm: VM | None = None, native: Any | None = None) -> None:
        self._vm = vm
        self._native = native
        self.sandbox_id = vm.id if vm is not None else getattr(native, "sandbox_id", getattr(native, "id", None))
        self.sandboxId = self.sandbox_id

    @staticmethod
    def _native_sandbox() -> Any | None:
        module = import_optional("modal")
        return getattr(module, "Sandbox", None) if module is not None else None

    @classmethod
    def create(cls, *args: Any, **kwargs: Any) -> Sandbox:
        arker_config = kwargs.pop("arker", None) or {}
        router_path = kwargs.pop("router_config_path", kwargs.pop("routerConfigPath", None))
        if router_path:
            arker_config = {**arker_config, "router_config_path": router_path}
        if args or kwargs:
            raise TypeError("Sandbox.create only supports create() in the Arker compatibility layer for modal.")
        native = cls._native_sandbox()

        def create_native() -> Any:
            if native is None or not hasattr(native, "create"):
                raise RuntimeError("Native Modal SDK is not installed")
            return native.create()

        provider, result = routed_create(
            "modal",
            arker_config,
            {
                "arker": lambda: arker_create(arker_config),
                "modal": create_native,
            },
        )
        return cls(vm=result if provider == "arker" else None, native=result if provider == "modal" else None)

    @classmethod
    def from_id(cls, sandbox_id: str, **kwargs: Any) -> Sandbox:
        arker_config = kwargs.pop("arker", None) or {}
        router_path = kwargs.pop("router_config_path", kwargs.pop("routerConfigPath", None))
        if router_path:
            arker_config = {**arker_config, "router_config_path": router_path}
        if kwargs:
            unsupported = ", ".join(sorted(kwargs))
            raise TypeError(f"Sandbox.from_id received unsupported option(s): {unsupported}")
        native = cls._native_sandbox()

        def get_native() -> Any | None:
            for method_name in ("from_id", "fromId", "connect"):
                if native is not None and hasattr(native, method_name):
                    return getattr(native, method_name)(sandbox_id)
            return None

        found = routed_get(
            sandbox_id,
            "modal",
            arker_config,
            {"arker": lambda: arker_get(sandbox_id, arker_config), "modal": get_native},
        )
        if found is not None:
            provider, result = found
            return cls(vm=result if provider == "arker" else None, native=result if provider == "modal" else None)
        raise format_not_found("Sandbox", sandbox_id)

    @classmethod
    def fromId(cls, sandbox_id: str, **kwargs: Any) -> Sandbox:
        return cls.from_id(sandbox_id, **kwargs)

    def exec(self, *command: Any, **kwargs: Any) -> Process:
        if kwargs:
            unsupported = ", ".join(sorted(kwargs))
            raise TypeError(f"Sandbox.exec received unsupported option(s): {unsupported}")
        if self._vm is not None:
            return Process(run_command(self._vm, shell_command(command)))
        return Process(self._native.exec(*command))

    def terminate(self) -> None:
        if self._vm is not None:
            self._vm.delete()
            return
        if hasattr(self._native, "terminate"):
            self._native.terminate()
            return
        if hasattr(self._native, "close"):
            self._native.close()
            return
        raise AttributeError("Native Modal sandbox does not expose terminate().")
