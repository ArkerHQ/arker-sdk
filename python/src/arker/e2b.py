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


class Commands:
    def __init__(self, vm: VM | None = None, native: Any | None = None) -> None:
        self._vm = vm
        self._native = native

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> CommandResult | Any:
        if self._vm is not None:
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
            return run_command(self._vm, prefix + command, timeout=timeout)
        native_options = {
            **({"cwd": cwd} if cwd is not None else {}),
            **({"env": env} if env is not None else {}),
            **({"timeout": timeout} if timeout is not None else {}),
        }
        return self._native.run(command, **native_options)


class Files(UnsupportedMixin):
    _compat_name = "e2b"

    def __init__(self, vm: VM | None = None, native: Any | None = None) -> None:
        self._vm = vm
        self._native = native

    def read(self, path: str) -> str:
        if self._vm is None:
            return self._native.read(path)
        return self._vm.sync(path).decode("utf-8", "replace")  # type: ignore[union-attr]

    def write(self, path: str, content: str) -> None:
        if self._vm is None:
            result = self._native.write(path, content)
            return None if result is None else result
        self._vm.sync(path, content)
        return None

    def make_dir(self, path: str) -> None:
        if self._vm is None:
            if hasattr(self._native, "make_dir"):
                return self._native.make_dir(path)
            return self._native.makeDir(path)
        result = run_command(self._vm, f"mkdir -p {shlex.quote(path)}")
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or f"mkdir failed with exit {result.exit_code}")
        return None

    def makeDir(self, path: str) -> None:
        return self.make_dir(path)

    def list(self, path: str) -> list[Any]:
        if self._vm is None:
            return self._native.list(path)
        quoted = shlex.quote(path)
        script = (
            f"cd {quoted} 2>/dev/null || exit 3; "
            "for e in * .*; do "
            '[ "$e" = "." ] && continue; [ "$e" = ".." ] && continue; [ -e "$e" ] || continue; '
            'if [ -d "$e" ]; then t=dir; else t=file; fi; '
            'printf \'%s\t%s\n\' "$t" "$e"; done'
        )
        result = run_command(self._vm, script)
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or f"list failed with exit {result.exit_code}")
        return [
            {"type": line.split("\t", 1)[0], "name": line.split("\t", 1)[1]}
            for line in result.stdout.splitlines()
            if "\t" in line
        ]

    def exists(self, path: str) -> bool:
        if self._vm is None:
            return bool(self._native.exists(path))
        return run_command(self._vm, f"test -e {shlex.quote(path)}").exit_code == 0

    def remove(self, path: str) -> None:
        if self._vm is None:
            result = self._native.remove(path)
            return None if result is None else result
        result = run_command(self._vm, f"rm -rf {shlex.quote(path)}")
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or f"remove failed with exit {result.exit_code}")
        return None


class Sandbox(UnsupportedMixin):
    _compat_name = "e2b"

    def __init__(self, vm: VM | None = None, native: Any | None = None) -> None:
        self._vm = vm
        self._native = native
        self.sandbox_id = vm.id if vm is not None else getattr(native, "sandbox_id", getattr(native, "id", None))
        self.sandboxId = self.sandbox_id
        self.commands = Commands(vm, getattr(native, "commands", None) if native is not None else None)
        self.files = Files(vm, getattr(native, "files", None) if native is not None else None)

    @staticmethod
    def _native_sandbox() -> Any | None:
        module = import_optional("e2b")
        return getattr(module, "Sandbox", None) if module is not None else None

    @classmethod
    def create(cls, template_id: str | None = None, **kwargs: Any) -> Sandbox:
        timeout = kwargs.pop("timeout", kwargs.pop("timeout_ms", kwargs.pop("timeoutMs", None)))
        arker_config = kwargs.pop("arker", None) or {}
        router_path = kwargs.pop("router_config_path", kwargs.pop("routerConfigPath", None))
        if router_path:
            arker_config = {**arker_config, "router_config_path": router_path}
        if kwargs:
            unsupported = ", ".join(sorted(kwargs))
            raise TypeError(f"Sandbox.create received unsupported option(s): {unsupported}")
        native = cls._native_sandbox()

        def create_native() -> Any:
            if native is None:
                raise RuntimeError("Native E2B SDK is not installed")
            if template_id is not None:
                if timeout is not None:
                    return native.create(template_id, timeout=timeout)
                return native.create(template_id)
            return native.create(timeout=timeout) if timeout is not None else native.create()

        provider, result = routed_create(
            "e2b",
            arker_config,
            {"arker": lambda: arker_create(arker_config, template_id=template_id), "e2b": create_native},
        )
        return cls(vm=result if provider == "arker" else None, native=result if provider == "e2b" else None)

    @classmethod
    def connect(cls, sandbox_id: str, **kwargs: Any) -> Sandbox:
        arker_config = kwargs.pop("arker", None) or {}
        router_path = kwargs.pop("router_config_path", kwargs.pop("routerConfigPath", None))
        if router_path:
            arker_config = {**arker_config, "router_config_path": router_path}
        if kwargs:
            unsupported = ", ".join(sorted(kwargs))
            raise TypeError(f"Sandbox.connect received unsupported option(s): {unsupported}")
        native = cls._native_sandbox()
        found = routed_get(
            sandbox_id,
            "e2b",
            arker_config,
            {
                "arker": lambda: arker_get(sandbox_id, arker_config),
                "e2b": lambda: (
                    native.connect(sandbox_id) if native is not None and hasattr(native, "connect") else None
                ),
            },
        )
        if found is not None:
            provider, result = found
            return cls(vm=result if provider == "arker" else None, native=result if provider == "e2b" else None)
        raise format_not_found("Sandbox", sandbox_id)

    def kill(self) -> None:
        if self._vm is not None:
            self._vm.delete()
            return
        if hasattr(self._native, "kill"):
            self._native.kill()
            return
        if hasattr(self._native, "close"):
            self._native.close()
            return
        raise AttributeError("Native E2B sandbox does not expose kill().")
