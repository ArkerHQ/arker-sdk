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
    compat_network_policies,
    format_not_found,
    get_or_none,
    import_optional,
    resolve_compat_path,
    routed_create,
    routed_get,
    run_command,
)
from .computer import VM


def __getattr__(name: str) -> Any:
    module = import_optional("e2b")
    if module is not None:
        return getattr(module, name)
    raise AttributeError(name)


class Commands(UnsupportedMixin):
    _compat_name = "e2b"

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
        background: bool = False,
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
            prefix += f"cd {shlex.quote(cwd or '/home/user')} && "
            full_command = prefix + command
            if background:
                full_command = f"nohup sh -lc {shlex.quote(full_command)} > /dev/null 2>&1 &"
            return run_command(self._vm, full_command, timeout=timeout)
        native_options = {
            **({"cwd": cwd} if cwd is not None else {}),
            **({"env": env} if env is not None else {}),
            **({"timeout": timeout} if timeout is not None else {}),
            **({"background": True} if background else {}),
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
        return self._vm.sync(resolve_compat_path(path, "/home/user")).decode("utf-8", "replace")  # type: ignore[union-attr]

    def write(self, path: str, content: str) -> None:
        if self._vm is None:
            result = self._native.write(path, content)
            return None if result is None else result
        self._vm.sync(resolve_compat_path(path, "/home/user"), content)
        return None

    def make_dir(self, path: str) -> None:
        if self._vm is None:
            if hasattr(self._native, "make_dir"):
                return self._native.make_dir(path)
            return self._native.makeDir(path)
        result = run_command(self._vm, f"mkdir -p {shlex.quote(resolve_compat_path(path, '/home/user'))}")
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or f"mkdir failed with exit {result.exit_code}")
        return None

    def makeDir(self, path: str) -> None:
        return self.make_dir(path)

    def list(self, path: str) -> list[Any]:
        if self._vm is None:
            return self._native.list(path)
        quoted = shlex.quote(resolve_compat_path(path, "/home/user"))
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
        return run_command(self._vm, f"test -e {shlex.quote(resolve_compat_path(path, '/home/user'))}").exit_code == 0

    def remove(self, path: str) -> None:
        if self._vm is None:
            result = self._native.remove(path)
            return None if result is None else result
        result = run_command(self._vm, f"rm -rf {shlex.quote(resolve_compat_path(path, '/home/user'))}")
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

    def get_host(self, port: int) -> str:
        if self._vm is None:
            if hasattr(self._native, "get_host"):
                return self._native.get_host(port)
            return self._native.getHost(port)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("port must be an integer from 1 through 65535")
        vm = self._vm if self._vm.hostname else self._vm.refresh()
        if not vm.hostname:
            raise UnsupportedCompatibilityOption("Arker did not return an inbound hostname for this VM")
        if port == 8080:
            return vm.hostname
        first, separator, remainder = vm.hostname.partition(".")
        return f"{first}-{port}{separator}{remainder}"

    def getHost(self, port: int) -> str:
        return self.get_host(port)

    @staticmethod
    def _native_sandbox() -> Any | None:
        module = import_optional("e2b")
        return getattr(module, "Sandbox", None) if module is not None else None

    @classmethod
    def create(cls, template_id: str | None = None, **kwargs: Any) -> Sandbox:
        timeout = kwargs.pop("timeout", kwargs.pop("timeout_ms", kwargs.pop("timeoutMs", None)))
        allow_internet_access = kwargs.pop("allow_internet_access", kwargs.pop("allowInternetAccess", True))
        arker_config = kwargs.pop("arker", None) or {}
        router_path = kwargs.pop("router_config_path", kwargs.pop("routerConfigPath", None))
        if router_path:
            arker_config = {**arker_config, "router_config_path": router_path}
        native = cls._native_sandbox()

        def create_native() -> Any:
            if native is None:
                raise RuntimeError("Native E2B SDK is not installed")
            native_options = {**kwargs, "allow_internet_access": allow_internet_access}
            if timeout is not None:
                native_options["timeout"] = timeout
            if template_id is not None:
                return native.create(template_id, **native_options)
            return native.create(**native_options)

        provider, result = routed_create(
            "e2b",
            arker_config,
            {
                "arker": lambda: cls._create_arker(
                    arker_config,
                    template_id,
                    timeout,
                    allow_internet_access,
                    kwargs,
                ),
                "e2b": create_native,
            },
        )
        return cls(vm=result if provider == "arker" else None, native=result if provider == "e2b" else None)

    @staticmethod
    def _create_arker(
        arker_config: dict[str, Any],
        template_id: str | None,
        timeout: int | None,
        allow_internet_access: bool,
        native_options: dict[str, Any],
    ) -> VM:
        unsupported = [*([] if timeout is None else ["timeout"]), *sorted(native_options)]
        if unsupported:
            raise UnsupportedCompatibilityOption(
                f"Sandbox.create option(s) are not supported by Arker: {', '.join(unsupported)}"
            )
        return arker_create(
            arker_config,
            template_id=template_id,
            policies=compat_network_policies(block_all=allow_internet_access is False),
        )

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
                    get_or_none(lambda: native.connect(sandbox_id))
                    if native is not None and hasattr(native, "connect")
                    else None
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
