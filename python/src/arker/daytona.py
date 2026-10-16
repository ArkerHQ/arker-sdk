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
        else:
            prefix += "mkdir -p /home/daytona && cd /home/daytona && "
        return ExecuteResponse(run_command(self._vm, prefix + command, timeout=timeout))

    def execute_command(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        return self.exec(command, cwd=cwd, env=env, timeout=timeout)

    def executeCommand(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        return self.exec(command, cwd=cwd, env=env, timeout=timeout)


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

        def create_arker() -> VM:
            if args:
                raise UnsupportedCompatibilityOption(
                    "Daytona.create positional arguments are not supported when routing to Arker"
                )
            supported = {
                "image",
                "snapshot",
                "name",
                "resources",
                "envVars",
                "language",
                "networkBlockAll",
                "network_block_all",
                "networkAllowList",
                "network_allow_list",
                "domainAllowList",
                "domain_allow_list",
            }
            unknown = sorted(set(kwargs) - supported)
            if unknown:
                raise UnsupportedCompatibilityOption(
                    f"Daytona.create received unsupported option(s) for Arker: {', '.join(unknown)}"
                )
            unsupported = [key for key in ("envVars", "language") if kwargs.get(key) is not None]
            if unsupported:
                raise UnsupportedCompatibilityOption(
                    f"Daytona.create option(s) are not supported by Arker: {', '.join(unsupported)}"
                )
            raw_resources = kwargs.get("resources") or {}
            resources = {
                **({"vcpu": raw_resources["cpu"]} if "cpu" in raw_resources else {}),
                **({"vcpu": raw_resources["vcpu"]} if "vcpu" in raw_resources else {}),
                **({"memory_mib": raw_resources["memory"]} if "memory" in raw_resources else {}),
                **({"memory_mib": raw_resources["memory_mib"]} if "memory_mib" in raw_resources else {}),
                **({"disk_mib": raw_resources["disk"]} if "disk" in raw_resources else {}),
                **({"disk_mib": raw_resources["disk_mib"]} if "disk_mib" in raw_resources else {}),
            }
            return arker_create(
                self._arker_config,
                name=kwargs.get("name"),
                template_id=kwargs.get("snapshot"),
                image=kwargs.get("image"),
                resources=resources or None,
                policies=compat_network_policies(
                    block_all=bool(kwargs.get("networkBlockAll", kwargs.get("network_block_all", False))),
                    domains=kwargs.get("domainAllowList", kwargs.get("domain_allow_list")),
                    cidrs=kwargs.get("networkAllowList", kwargs.get("network_allow_list")),
                ),
            )

        provider, result = routed_create(
            "daytona",
            self._arker_config,
            {
                "arker": create_arker,
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
                "daytona": lambda: (
                    get_or_none(lambda: native.get(sandbox_id))
                    if native is not None and hasattr(native, "get")
                    else None
                ),
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
                    get_or_none(lambda: native.get(sandbox)) if native is not None and hasattr(native, "get") else None
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
