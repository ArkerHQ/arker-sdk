from __future__ import annotations

import re
from dataclasses import asdict, is_dataclass
from typing import Any

from ._compat import (
    CommandResult,
    CompatibilityCreationNotStartedError,
    UnsupportedCompatibilityOption,
    UnsupportedMixin,
    arker_create,
    arker_get,
    compat_network_policies,
    format_not_found,
    get_or_none,
    import_optional,
    pop_arker_config,
    routed_create,
    routed_get,
    run_command,
)
from ._compat_daytona_async import AsyncDaytona as AsyncDaytona
from ._compat_daytona_async import AsyncSandbox as AsyncSandbox
from ._daytona_filesystem import FileSystem, guest_command
from .computer import VM


def __getattr__(name: str) -> Any:
    module = import_optional("daytona")
    if module is not None:
        return getattr(module, name)
    raise AttributeError(name)


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
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        if env:
            for key in env:
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
                    raise ValueError(f"Invalid environment variable name: {key}")
        result = run_command(
            self._vm,
            guest_command({"operation": "exec", "command": command, "cwd": cwd, "env": env}),
            timeout=timeout,
        )
        native = import_optional("daytona.common.process")
        if native is not None:
            return native.ExecuteResponse(
                exit_code=result.exit_code,
                result=result.stdout,
                artifacts=native.ExecutionArtifacts(stdout=result.stdout, charts=[]),
            )
        return ExecuteResponse(result)

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

    def __init__(self, vm: VM | None = None, native: Any | None = None, *, lifecycle_config=None) -> None:
        self._vm = vm
        self._native = native
        self._lifecycle_config = lifecycle_config or {}
        self.id = vm.id if vm is not None else getattr(native, "id", getattr(native, "sandbox_id", None))
        if vm is not None:
            self.process = Process(vm)
            self._fs = FileSystem(vm)
        else:
            self.process = native.process
            self._fs = getattr(native, "fs", None)

    @property
    def fs(self) -> FileSystem:
        return self._fs

    def get_user_home_dir(self) -> str:
        if self._native is not None:
            return self._native.get_user_home_dir()
        from daytona.common.errors import DaytonaError

        result = self.process.exec('printf "%s" "$HOME"')
        if result.exit_code:
            raise DaytonaError("Failed to get user home directory")
        return result.result

    def pause(self, timeout: float = 60) -> None:
        if self._native is not None:
            return self._native.pause(timeout)
        from ._daytona_lifecycle import pause

        return pause(self._vm, timeout)

    def start(self, timeout: float | None = 60):
        if self._native is not None:
            return self._native.start(timeout)
        from ._daytona_lifecycle import start

        return start(self._vm, timeout)

    def stop(self, timeout: float | None = 60, force: bool = False):
        if self._native is not None:
            return self._native.stop(timeout, force=force)
        from ._daytona_lifecycle import unsupported_stop

        return unsupported_stop()

    def archive(self, request_timeout: float | None = None) -> None:
        if self._native is not None:
            return self._native.archive(request_timeout=request_timeout)
        from ._daytona_lifecycle import unsupported_archive

        return unsupported_archive()

    def fork(self, name: str | None = None, timeout: float | None = 60):
        if self._native is not None:
            return self._native.fork(name=name, timeout=timeout)
        from ._daytona_lifecycle import fork

        return type(self)(
            vm=fork(self._vm, name, timeout, config=self._lifecycle_config), lifecycle_config=self._lifecycle_config
        )

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
        self._arker_config = dict(pop_arker_config(self._native_kwargs))
        self._native = None

    def _native_client(self) -> Any | None:
        if self._native is not None:
            return self._native
        module = import_optional("daytona")
        if module is None or not hasattr(module, "Daytona"):
            return None
        self._native = module.Daytona(*self._native_args, **self._native_kwargs)
        return self._native

    def _native_volume_available(self):
        if self._native is not None:
            return True
        environment = import_optional("daytona._utils.env")
        if environment is None:
            return False

        # Invalid native constructor arguments must still reach native validation.
        if len(self._native_args) > 1 or set(self._native_kwargs) - {"config"}:
            return True
        config = self._native_args[0] if self._native_args else self._native_kwargs.get("config")
        if config is not None and not hasattr(config, "api_key"):
            return True
        env = environment.DaytonaEnvReader()
        return bool(
            getattr(config, "api_key", None)
            or getattr(config, "jwt_token", None)
            or env.get("DAYTONA_API_KEY")
            or env.get("DAYTONA_JWT_TOKEN")
        )

    @property
    def volume(self):
        from ._daytona_volumes import VolumeService

        return VolumeService(
            self._arker_config,
            lambda: self._native_client().volume,
            native_available=self._native_volume_available(),
        )

    def create(self, *args: Any, **kwargs: Any) -> Sandbox:
        def create_native() -> Any:
            from ._daytona_volumes import check_native_mounts

            check_native_mounts(args, kwargs, self.volume)
            native = self._native_client()
            if native is None:
                raise CompatibilityCreationNotStartedError("Native Daytona SDK is not installed")
            return native.create(*args, **kwargs)

        def create_arker() -> VM:
            values = dict(kwargs)
            if len(args) > 1 or (args and "params" in values):
                raise TypeError("Daytona.create accepts one params argument")
            params = args[0] if args else values.pop("params", None)
            if params is not None:
                if hasattr(params, "model_dump"):
                    params = params.model_dump(exclude_none=True)
                if not isinstance(params, dict):
                    raise TypeError("Daytona.create params must be a mapping or a native parameter model")
                if set(params) & set(values):
                    raise TypeError("Daytona.create received duplicate parameter values")
                values = {**params, **values}
            supported = {
                "image",
                "volumes",
                "snapshot",
                "name",
                "resources",
                "envVars",
                "env_vars",
                "language",
                "networkBlockAll",
                "network_block_all",
                "networkAllowList",
                "network_allow_list",
                "domainAllowList",
                "domain_allow_list",
            }
            unknown = sorted(set(values) - supported)
            if unknown:
                raise UnsupportedCompatibilityOption(
                    f"Daytona.create received unsupported option(s) for Arker: {', '.join(unknown)}"
                )
            unsupported = [key for key in ("envVars", "env_vars", "language") if values.get(key) is not None]
            if unsupported:
                raise UnsupportedCompatibilityOption(
                    f"Daytona.create option(s) are not supported by Arker: {', '.join(unsupported)}"
                )
            volumes = values.get("volumes")
            from ._daytona_volumes import resolve_mounts

            mounts = resolve_mounts(volumes, self.volume)
            raw_resources = values.get("resources") or {}
            if is_dataclass(raw_resources) and not isinstance(raw_resources, type):
                raw_resources = {key: value for key, value in asdict(raw_resources).items() if value is not None}
            if not isinstance(raw_resources, dict):
                raise TypeError("Daytona resources must be a mapping or native Resources model")
            unknown_resources = sorted(set(raw_resources) - {"cpu", "vcpu", "memory", "memory_mib", "disk", "disk_mib"})
            if unknown_resources:
                raise UnsupportedCompatibilityOption("Unsupported Daytona resource(s): " + ", ".join(unknown_resources))
            resources = {
                **({"vcpu": raw_resources["cpu"]} if "cpu" in raw_resources else {}),
                **({"vcpu": raw_resources["vcpu"]} if "vcpu" in raw_resources else {}),
                **({"memory_mib": raw_resources["memory"] * 1024} if "memory" in raw_resources else {}),
                **({"memory_mib": raw_resources["memory_mib"]} if "memory_mib" in raw_resources else {}),
                **({"disk_mib": raw_resources["disk"] * 1024} if "disk" in raw_resources else {}),
                **({"disk_mib": raw_resources["disk_mib"]} if "disk_mib" in raw_resources else {}),
            }
            vm = arker_create(
                self._arker_config,
                name=values.get("name"),
                template_id=values.get("snapshot"),
                image=values.get("image"),
                resources=resources or None,
                policies=compat_network_policies(
                    block_all=bool(values.get("networkBlockAll", values.get("network_block_all", False))),
                    domains=values.get("domainAllowList", values.get("domain_allow_list")),
                    cidrs=values.get("networkAllowList", values.get("network_allow_list")),
                ),
            )

            if not mounts:
                return vm
            try:
                from ._compat_e2b_mounts import mount_ready_command
                from ._daytona_filesystem import guest_command

                for mount in mounts:
                    vm.create_mount(**mount)
                command = mount_ready_command([mount["path"] for mount in mounts], 60)
                result = run_command(vm, guest_command({"operation": "exec", "command": command}))
                if result.exit_code:
                    raise RuntimeError("Daytona volume mounts did not become accessible to the guest user")
                return vm
            except BaseException as error:
                try:
                    vm.delete()
                except BaseException as cleanup:
                    raise RuntimeError(f"Sandbox creation and cleanup failed for VM {vm.id}") from cleanup
                raise error

        provider, result = routed_create(
            "daytona",
            self._arker_config,
            {
                "arker": create_arker,
                "daytona": create_native,
            },
        )
        return Sandbox(
            vm=result if provider == "arker" else None,
            native=result if provider == "daytona" else None,
            lifecycle_config=self._arker_config,
        )

    def _lookup(self, sandbox_id: str):
        return routed_get(
            sandbox_id,
            "daytona",
            self._arker_config,
            {
                "arker": lambda: arker_get(sandbox_id, self._arker_config),
                "daytona": lambda: self._native_get(sandbox_id),
            },
        )

    def get(self, sandbox_id: str) -> Sandbox:
        found = self._lookup(sandbox_id)
        if found is None:
            raise format_not_found("Sandbox", sandbox_id)
        provider, result = found
        return Sandbox(
            vm=result if provider == "arker" else None,
            native=result if provider == "daytona" else None,
            lifecycle_config=self._arker_config,
        )

    def _native_get(self, sandbox_id: str) -> Any | None:
        native = self._native_client()
        return get_or_none(lambda: native.get(sandbox_id)) if native is not None and hasattr(native, "get") else None

    def start(self, sandbox: Sandbox, timeout: float = 60) -> None:
        return sandbox.start(timeout)

    def stop(self, sandbox: Sandbox, timeout: float = 60) -> None:
        return sandbox.stop(timeout)

    def delete(self, sandbox: str | Sandbox) -> None:
        if not isinstance(sandbox, str):
            sandbox.delete()
            return
        found = self._lookup(sandbox)
        if found is None:
            native = self._native_client()
            if native is not None and hasattr(native, "delete"):
                native.delete(sandbox)
                return
            raise format_not_found("Sandbox", sandbox)
        provider, result = found
        if provider == "arker":
            Sandbox(vm=result).delete()
        else:
            Sandbox(native=result).delete()
