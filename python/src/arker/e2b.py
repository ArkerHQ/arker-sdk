from __future__ import annotations

import os
import shlex
from typing import Any

from ._compat import (
    CompatibilityCreationNotStartedError,
    UnsupportedCompatibilityOption,
    UnsupportedMixin,
    _InstanceOrClassMethod,
    arker_create,
    arker_get,
    arker_source,
    compat_network_policies,
    format_not_found,
    get_or_none,
    import_optional,
    pop_arker_config,
    routed_create,
    routed_get,
    run_command,
)
from ._compat_e2b_async import AsyncSandbox as AsyncSandbox
from ._compat_e2b_lifecycle import Lifecycle, _control, _options, _timeout
from ._compat_e2b_mounts import (
    AsyncVolume as AsyncVolume,
)
from ._compat_e2b_mounts import (
    Volume as Volume,
)
from ._compat_e2b_mounts import (
    check_mount_backend,
    mount_ready_command,
    mount_store,
)
from .computer import VM, ArkerError

_E2B_DEFAULT_TIMEOUT_SECONDS = 300
_E2B_LIFETIME_POLICY_ID = "computesdk-e2b-lifetime"


def _mount_readiness_command(command: str) -> str:
    shell = (
        f"cd /home/user && export HOME=/home/user USER=user LOGNAME=user && exec /bin/bash -c {shlex.quote(command)}"
    )
    return f"setpriv --reuid=user --regid=user --init-groups -- sh -c {shlex.quote(shell)}"


def __getattr__(name: str) -> Any:
    module = import_optional("e2b")
    if module is not None:
        return getattr(module, name)
    raise AttributeError(name)


class Sandbox(UnsupportedMixin):
    _compat_name = "e2b"

    def __init__(self, vm: VM | None = None, native: Any | None = None) -> None:
        self._vm = vm
        self._native = native
        self.sandbox_id = vm.id if vm is not None else getattr(native, "sandbox_id", getattr(native, "id", None))
        self.sandboxId = self.sandbox_id
        self._data_plane = None
        self._envd_descriptor = None
        self.commands = getattr(native, "commands", None)
        self.files = getattr(native, "files", None)
        self.git = getattr(native, "git", None)

    def _prepare_data_plane(self, *, envs=None) -> None:
        from ._compat_e2b_bootstrap import bootstrap_envd
        from ._compat_e2b_data_plane import create_data_plane
        from ._compat_e2b_urls import prepare_file_urls

        descriptor, _ = bootstrap_envd(self._vm, **({"envs": envs} if envs is not None else {}))
        prepare_file_urls(self._vm)
        replacement = create_data_plane(descriptor, self._vm._client._api_key)
        previous = self._data_plane
        self._data_plane = replacement
        self._envd_descriptor = descriptor
        self.commands, self.files, self.git = replacement.commands, replacement.files, replacement.git
        if previous is not None:
            previous.close()

    @property
    def traffic_access_token(self) -> str | None:
        if self._native is not None:
            return self._native.traffic_access_token
        from ._compat_e2b_network import traffic_token

        return traffic_token(self._vm)

    @property
    def pty(self):
        if self._data_plane is not None:
            return self._data_plane.pty
        if self._native is not None:
            return self._native.pty
        raise RuntimeError("Sandbox data plane is not initialized; use create or connect")

    def download_url(self, path: str, user: str | None = None, use_signature_expiration: int | None = None) -> str:
        from ._compat_e2b_urls import E2BFileURLs

        return E2BFileURLs(self._vm, self._native, self._envd_descriptor).download_url(
            path, user, use_signature_expiration
        )

    def upload_url(self, path: str, user: str | None = None, use_signature_expiration: int | None = None) -> str:
        from ._compat_e2b_urls import E2BFileURLs

        return E2BFileURLs(self._vm, self._native, self._envd_descriptor).upload_url(
            path, user, use_signature_expiration
        )

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
        if "template" in kwargs:
            if template_id is not None:
                raise TypeError("Specify template only once")
            template_id = kwargs.pop("template")
        timeout = kwargs.pop("timeout", None)
        timeout_aliases = sorted({"timeout_ms", "timeoutMs"}.intersection(kwargs))
        if timeout_aliases:
            raise TypeError(
                f"Sandbox.create does not accept {', '.join(timeout_aliases)}; "
                "the E2B Python API uses timeout in seconds"
            )
        allow_internet_access = kwargs.pop("allow_internet_access", kwargs.pop("allowInternetAccess", None))
        arker_config = pop_arker_config(kwargs)
        native = cls._native_sandbox()

        def create_native() -> Any:
            check_mount_backend(kwargs.get("volume_mounts"), "e2b")
            if native is None:
                raise CompatibilityCreationNotStartedError("Native E2B SDK is not installed")
            native_options = dict(kwargs)
            if allow_internet_access is not None:
                native_options["allow_internet_access"] = allow_internet_access
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
        if provider == "arker":
            sandbox = None
            try:
                sandbox = cls(vm=result)
                sandbox._prepare_data_plane(**({"envs": kwargs["envs"]} if "envs" in kwargs else {}))
                mounts = kwargs.get("volume_mounts")
                if mounts:
                    mount_store(result._client, mounts).attach(result, mounts)
                    readiness = run_command(
                        result,
                        _mount_readiness_command(mount_ready_command(mounts, timeout or _E2B_DEFAULT_TIMEOUT_SECONDS)),
                        timeout=timeout or _E2B_DEFAULT_TIMEOUT_SECONDS,
                    )
                    if readiness.exit_code != 0:
                        raise RuntimeError("Volume mount readiness failed; sandbox was not returned")
                return sandbox
            except BaseException:
                try:
                    try:
                        if sandbox is not None and sandbox._data_plane is not None:
                            sandbox._data_plane.close()
                    finally:
                        result.delete()
                except BaseException as cleanup_error:
                    raise RuntimeError(f"Sandbox creation and cleanup failed for VM {result.id}") from cleanup_error
                raise
        return cls(vm=result if provider == "arker" else None, native=result if provider == "e2b" else None)

    @staticmethod
    def _create_arker(
        arker_config: dict[str, Any],
        template_id: str | None,
        timeout: int | None,
        allow_internet_access: bool | None,
        native_options: dict[str, Any],
    ) -> VM:
        check_mount_backend(native_options.get("volume_mounts"), "arker")
        unsupported = sorted(
            key
            for key in native_options
            if key
            not in {
                "volume_mounts",
                "network",
                "envs",
                "metadata",
                "api_key",
                "secure",
                "validate_api_key",
                "debug",
                "lifecycle",
            }
        )
        if unsupported:
            raise UnsupportedCompatibilityOption(
                f"Sandbox.create option(s) are not supported by Arker: {', '.join(unsupported)}"
            )
        if "debug" in native_options and native_options["debug"] is not False:
            raise UnsupportedCompatibilityOption("Arker Sandbox.create supports only debug=False")
        lifecycle = native_options.get("lifecycle")
        if lifecycle is not None:
            if not isinstance(lifecycle, dict) or set(lifecycle) - {"on_timeout", "auto_resume"}:
                raise UnsupportedCompatibilityOption("Arker Sandbox.create supports only kill lifecycle")
            action = lifecycle.get("on_timeout", "kill")
            if action != "kill" and action != {"action": "kill"}:
                raise UnsupportedCompatibilityOption("Arker Sandbox.create supports only kill lifecycle")
            if lifecycle.get("auto_resume", False) is not False:
                raise UnsupportedCompatibilityOption("Arker Sandbox.create supports only auto_resume=False")
        metadata = native_options.get("metadata")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in metadata.items()
        ):
            raise TypeError("metadata must be a mapping of strings to strings")
        envs = native_options.get("envs")
        if envs is not None and (
            not isinstance(envs, dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in envs.items())
        ):
            raise TypeError("envs must be a mapping of strings to strings")
        lifetime = _E2B_DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout
        _timeout(lifetime)
        configured_rules = (arker_config.get("policies") or {}).get("rules", {})
        if _E2B_LIFETIME_POLICY_ID in configured_rules:
            raise ValueError(f"policy id {_E2B_LIFETIME_POLICY_ID!r} is reserved for the E2B sandbox lifetime")
        network_options = native_options.get("network") or {}
        if not isinstance(network_options, dict) or set(network_options) - {"allow_public_traffic"}:
            raise UnsupportedCompatibilityOption("Arker E2B network supports only allow_public_traffic")
        public = network_options.get("allow_public_traffic", True)
        if not isinstance(public, bool):
            raise TypeError("network.allow_public_traffic must be a boolean")
        network = compat_network_policies(block_all=allow_internet_access is False)
        policies = {
            "rules": {
                **((network or {}).get("rules", {})),
                _E2B_LIFETIME_POLICY_ID: {
                    "type": "schedule",
                    "after_created": lifetime,
                    "action": "delete",
                },
            }
        }
        from ._compat_e2b_artifact import envd_version
        from ._compat_e2b_network import fork_with_traffic

        source_id = arker_config.get("source_vm_id") or os.environ.get("ARKER_SOURCE_VM_ID")
        source = (
            {"kind": "vm_id", "value": source_id}
            if source_id and not template_id
            else {"kind": "vm_name", "value": template_id or arker_source(arker_config)}
        )
        return arker_create(
            arker_config,
            template_id=template_id,
            policies=policies,
            fork=lambda client, request: fork_with_traffic(client, request, public),
            metadata={
                "arker_e2b": {
                    "version": 1,
                    "metadata": dict(metadata),
                    "source": source,
                    "envd_version": envd_version(),
                    **(
                        {"network": {"allow_public_traffic": public}}
                        if "allow_public_traffic" in network_options
                        else {}
                    ),
                }
            },
        )

    @classmethod
    def _lookup(cls, sandbox_id: str, kwargs: dict[str, Any]):
        arker_config = pop_arker_config(kwargs)
        native = cls._native_sandbox()
        found = routed_get(
            sandbox_id,
            "e2b",
            arker_config,
            {
                "arker": lambda: _control(
                    arker_get, sandbox_id, arker_config, **_options({"request_timeout": kwargs.get("request_timeout")})
                ),
                "e2b": lambda: (
                    get_or_none(lambda: native.get_info(sandbox_id, **kwargs)) if native is not None else None
                ),
            },
        )
        return found, native

    @classmethod
    def _class_fork(cls, sandbox_id: str, timeout: int | None = None, count: int | None = None, **opts):
        from e2b import SandboxNotFoundException

        config = pop_arker_config(opts)
        found, native = cls._lookup(sandbox_id, {"arker": config, **opts})
        if found is None:
            raise SandboxNotFoundException("Sandbox was not found")
        provider, vm = found
        if provider == "e2b":
            return [
                item if isinstance(item, Exception) else cls(native=item)
                for item in native.fork(sandbox_id, timeout=timeout, count=count, **opts)
            ]
        return cls(vm=vm).fork(timeout=timeout, count=count, **opts)

    @_InstanceOrClassMethod
    def fork(self, timeout: int | None = None, count: int | None = None, **opts):
        if self._native is not None:
            return [
                item if isinstance(item, Exception) else type(self)(native=item)
                for item in self._native.fork(timeout=timeout, count=count, **opts)
            ]
        from ._compat_e2b_fork import fork_arker

        def prepare(vm):
            sandbox = type(self)(vm=vm)
            sandbox._prepare_data_plane()
            return sandbox

        return fork_arker(self._vm, prepare, timeout=timeout, count=count, **opts)

    @classmethod
    def _class_connect(cls, sandbox_id: str, **kwargs: Any) -> Sandbox:
        config = pop_arker_config(kwargs)
        found, native = cls._lookup(
            sandbox_id,
            {
                "arker": config,
                **{key: value for key, value in kwargs.items() if key not in {"timeout", "on_resume"}},
            },
        )
        if found is None:
            raise format_not_found("Sandbox", sandbox_id)
        provider, result = found
        if provider == "e2b":
            return cls(native=native.connect(sandbox_id, **kwargs))
        current = Lifecycle(vm=result).connect(**kwargs)
        sandbox = cls(vm=current)
        sandbox._prepare_data_plane()
        return sandbox

    @_InstanceOrClassMethod
    def connect(self, timeout: int | None = None, *, on_resume: str = "restore", **opts: Any) -> Sandbox:
        Lifecycle(vm=self._vm, native=self._native).connect(timeout=timeout, on_resume=on_resume, **opts)
        if self._vm is not None:
            self._prepare_data_plane()
        return self

    @classmethod
    def _class_lifecycle(cls, method, sandbox_id, *args, **opts):
        from e2b import SandboxNotFoundException

        config = pop_arker_config(opts)
        lookup = {"arker": config, **{key: value for key, value in opts.items() if key not in {"mode", "keep_memory"}}}
        found, native = cls._lookup(sandbox_id, lookup)
        if found is None:
            raise SandboxNotFoundException("Sandbox was not found")
        provider, vm = found
        return (
            getattr(native, method)(sandbox_id, *args, **opts)
            if provider == "e2b"
            else getattr(Lifecycle(vm=vm), method)(*args, **opts)
        )

    @classmethod
    def _class_pause(cls, sandbox_id, keep_memory=None, *, mode=None, **opts):
        return cls._class_lifecycle("pause", sandbox_id, keep_memory=keep_memory, mode=mode, **opts)

    @classmethod
    def _class_beta_pause(cls, sandbox_id, keep_memory=None, *, mode=None, **opts):
        return cls._class_lifecycle("beta_pause", sandbox_id, keep_memory=keep_memory, mode=mode, **opts)

    @classmethod
    def _class_set_timeout(cls, sandbox_id, timeout, **opts):
        return cls._class_lifecycle("set_timeout", sandbox_id, timeout, **opts)

    @_InstanceOrClassMethod
    def pause(self, keep_memory: bool | None = None, *, mode: str | None = None, **opts: Any) -> bool:
        return Lifecycle(vm=self._vm, native=self._native).pause(keep_memory, mode=mode, **opts)

    @_InstanceOrClassMethod
    def beta_pause(self, keep_memory: bool | None = None, *, mode: str | None = None, **opts: Any) -> bool:
        return Lifecycle(vm=self._vm, native=self._native).beta_pause(keep_memory, mode=mode, **opts)

    @_InstanceOrClassMethod
    def set_timeout(self, timeout: int, **opts: Any) -> None:
        return Lifecycle(vm=self._vm, native=self._native).set_timeout(timeout, **opts)

    def is_running(self, request_timeout: float | None = None) -> bool:
        return Lifecycle(vm=self._vm, native=self._native).is_running(request_timeout)

    @classmethod
    def list(cls, query=None, limit=None, next_token=None, order=None, **opts):
        from ._compat_e2b_inventory import list_sandboxes

        config = pop_arker_config(opts)
        return list_sandboxes(
            cls._native_sandbox(), config, query=query, limit=limit, next_token=next_token, order=order, **opts
        )

    @classmethod
    def _class_get_info(cls, sandbox_id, **opts):
        from e2b import SandboxNotFoundException

        config = pop_arker_config(opts)
        native = cls._native_sandbox()
        found = routed_get(
            sandbox_id,
            "e2b",
            config,
            {
                "arker": lambda: _control(
                    arker_get, sandbox_id, config, **_options({"request_timeout": opts.get("request_timeout")})
                ),
                "e2b": lambda: get_or_none(lambda: native.get_info(sandbox_id, **opts)) if native else None,
            },
        )
        if found is None:
            raise SandboxNotFoundException("Sandbox was not found")
        provider, result = found
        return result if provider == "e2b" else Lifecycle(vm=result).get_info(**opts)

    @_InstanceOrClassMethod
    def get_info(self, **opts: Any) -> Any:
        return Lifecycle(vm=self._vm, native=self._native).get_info(**opts)

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.kill()

    @classmethod
    def _class_kill(cls, sandbox_id: str | Sandbox, **kwargs: Any) -> bool:
        if isinstance(sandbox_id, cls):
            return sandbox_id.kill(**kwargs)
        found, native = cls._lookup(sandbox_id, kwargs)
        if found is None:
            return False
        provider, result = found
        return native.kill(sandbox_id, **kwargs) if provider == "e2b" else cls(vm=result).kill(**kwargs)

    @_InstanceOrClassMethod
    def kill(self, **opts) -> bool:
        if self._vm is not None:
            request = _options(opts)
            try:
                if self._data_plane is not None:
                    self._data_plane.close()
            finally:
                try:
                    deleted = _control(self._vm.delete, **request).deleted
                except ArkerError as error:
                    if error.status != 404:
                        raise
                    deleted = False
            return deleted
        if hasattr(self._native, "kill"):
            return self._native.kill(**opts)
        raise AttributeError("Native E2B sandbox does not expose kill().")
