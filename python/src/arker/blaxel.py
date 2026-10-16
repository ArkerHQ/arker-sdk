from __future__ import annotations

import re
import shlex
from typing import Any

from ._compat import (
    CommandResult,
    UnsupportedCompatibilityOption,
    UnsupportedMixin,
    arker_create,
    arker_get,
    format_not_found,
    get_or_none,
    import_optional,
    resolve_compat_path,
    routed_create,
    routed_get,
    run_command,
)
from .computer import VM


class Process:
    def __init__(self, vm: VM | None = None, native: Any | None = None) -> None:
        self._vm = vm
        self._native = native

    def exec(self, request: str | dict[str, Any]) -> CommandResult | Any:
        if self._vm is None:
            module = import_optional("blaxel.core.sandbox")
            request_type = getattr(module, "ProcessRequestWithLog", None) if module is not None else None
            if request_type is None:
                return self._native.exec(request)
            values = {"command": request} if isinstance(request, str) else dict(request)
            if "workingDir" in values:
                values["working_dir"] = values.pop("workingDir")
            if "envs" in values:
                values["env"] = values.pop("envs")
            values.setdefault("wait_for_completion", True)
            return self._native.exec(request_type(**values))
        if isinstance(request, str):
            command = f"mkdir -p /blaxel && cd /blaxel && {request}"
            timeout = None
        else:
            command = request.get("command") or request.get("cmd")
            if isinstance(command, list):
                command = " ".join(shlex.quote(str(value)) for value in command)
            if not isinstance(command, str):
                raise TypeError("process.exec requires a command string or list")
            working_dir = request.get("workingDir") or request.get("working_dir")
            environment = request.get("env") or request.get("envs")
            prefix = ""
            if working_dir:
                prefix += f"cd {shlex.quote(str(working_dir))} && "
            else:
                prefix += "mkdir -p /blaxel && cd /blaxel && "
            if environment:
                invalid = next(
                    (key for key in environment if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key))),
                    None,
                )
                if invalid is not None:
                    raise ValueError(f"invalid environment variable name: {invalid}")
                prefix += (
                    "env " + " ".join(f"{key}={shlex.quote(str(value))}" for key, value in environment.items()) + " "
                )
            command = prefix + command
            timeout = request.get("timeout")
        return run_command(self._vm, command, timeout=timeout)


class FileSystem:
    def __init__(self, vm: VM | None = None, native: Any | None = None) -> None:
        self._vm = vm
        self._native = native

    def read(self, path: str) -> str:
        if self._vm is None:
            return self._native.read(path)
        return self._vm.sync(resolve_compat_path(path, "/blaxel")).decode("utf-8", "replace")

    def write(self, path: str, content: str | bytes) -> None:
        if self._vm is None:
            return self._native.write(path, content)
        self._vm.sync(resolve_compat_path(path, "/blaxel"), content)
        return None

    def mkdir(self, path: str) -> None:
        resolved = resolve_compat_path(path, "/blaxel")
        result = (
            self._native.mkdir(path) if self._vm is None else run_command(self._vm, f"mkdir -p {shlex.quote(resolved)}")
        )
        if isinstance(result, CommandResult) and result.exit_code != 0:
            raise RuntimeError(result.stderr or f"mkdir failed with exit {result.exit_code}")

    def rm(self, path: str) -> None:
        resolved = resolve_compat_path(path, "/blaxel")
        result = self._native.rm(path) if self._vm is None else run_command(self._vm, f"rm -rf {shlex.quote(resolved)}")
        if isinstance(result, CommandResult) and result.exit_code != 0:
            raise RuntimeError(result.stderr or f"remove failed with exit {result.exit_code}")


class SandboxInstance(UnsupportedMixin):
    _compat_name = "blaxel"

    def __init__(self, vm: VM | None = None, native: Any | None = None) -> None:
        self._vm = vm
        self._native = native
        metadata = getattr(native, "metadata", None)
        metadata_name = (
            metadata.get("name")
            if isinstance(metadata, dict)
            else getattr(metadata, "name", None)
        )
        self.id = (
            vm.id
            if vm is not None
            else getattr(native, "id", None) or getattr(native, "sandbox_id", None) or metadata_name
        )
        self.process = Process(vm, getattr(native, "process", None))
        self.fs = FileSystem(vm, getattr(native, "fs", getattr(native, "filesystem", None)))

    @staticmethod
    def _native_class() -> Any | None:
        module = import_optional("blaxel.core.sandbox")
        return getattr(module, "SyncSandboxInstance", None) if module is not None else None

    @classmethod
    def create(cls, sandbox: Any = None, **kwargs: Any) -> SandboxInstance:
        arker_config = kwargs.pop("arker", None) or {}
        router_path = kwargs.pop("router_config_path", kwargs.pop("routerConfigPath", None))
        if router_path:
            arker_config = {**arker_config, "router_config_path": router_path}
        native = cls._native_class()

        def create_native() -> Any:
            if native is None:
                raise RuntimeError("Native Blaxel SDK is not installed")
            return native.create(sandbox, **kwargs)

        values = sandbox if isinstance(sandbox, dict) else vars(sandbox) if sandbox is not None else {}

        def create_arker() -> VM:
            supported = {"name", "image", "memory", "ttl", "envs", "runtime"}
            unknown = sorted(set(values) - supported)
            if kwargs or unknown:
                names = [*sorted(kwargs), *unknown]
                raise UnsupportedCompatibilityOption(
                    f"SandboxInstance.create received unsupported option(s) for Arker: {', '.join(names)}"
                )
            unsupported = [key for key in ("ttl", "envs", "runtime") if values.get(key) is not None]
            if unsupported:
                names = ", ".join(unsupported)
                raise UnsupportedCompatibilityOption(
                    f"SandboxInstance.create option(s) are not supported by Arker: {names}"
                )
            memory = values.get("memory")
            return arker_create(
                arker_config,
                name=values.get("name"),
                image=values.get("image"),
                resources={"memory_mib": memory} if memory is not None else None,
            )

        provider, result = routed_create(
            "blaxel",
            arker_config,
            {
                "arker": create_arker,
                "blaxel": create_native,
            },
        )
        return cls(vm=result if provider == "arker" else None, native=result if provider == "blaxel" else None)

    @classmethod
    def get(cls, sandbox_id: str, **kwargs: Any) -> SandboxInstance:
        arker_config = kwargs.pop("arker", None) or {}
        router_path = kwargs.pop("router_config_path", kwargs.pop("routerConfigPath", None))
        if router_path:
            arker_config = {**arker_config, "router_config_path": router_path}
        if kwargs:
            raise TypeError(f"SandboxInstance.get received unsupported option(s): {', '.join(sorted(kwargs))}")
        native = cls._native_class()
        found = routed_get(
            sandbox_id,
            "blaxel",
            arker_config,
            {
                "arker": lambda: arker_get(sandbox_id, arker_config),
                "blaxel": lambda: get_or_none(lambda: native.get(sandbox_id)) if native is not None else None,
            },
        )
        if found is None:
            raise format_not_found("Sandbox", sandbox_id)
        provider, result = found
        return cls(vm=result if provider == "arker" else None, native=result if provider == "blaxel" else None)

    @classmethod
    def create_if_not_exists(cls, sandbox: Any = None, **kwargs: Any) -> SandboxInstance:
        raise NotImplementedError(
            "create_if_not_exists cannot be implemented atomically across routed providers; "
            "use create(), or the native Blaxel SDK when atomic name creation is required"
        )

    def delete(self) -> None:
        if self._vm is not None:
            self._vm.delete()
            return
        native = self._native_class()
        if native is not None and self.id:
            native.delete(self.id)
            return
        raise AttributeError("Native Blaxel sandbox does not expose delete().")


SyncSandboxInstance = SandboxInstance
