from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ._compat import (
    CompatibilityCreationNotStartedError,
    UnsupportedCompatibilityOption,
    UnsupportedMixin,
    arker_create,
    arker_get,
    get_or_none,
    import_optional,
    pop_arker_config,
    routed_create,
    routed_get,
)
from ._compat_async import NativeBridge, RoutingCancelled, call, route
from ._compat_blaxel_create import creation_metadata, fork_sandbox, sandbox_metadata, sandbox_spec
from ._compat_blaxel_errors import control_call, not_found
from ._compat_blaxel_network import validate_network
from .computer import VM

if TYPE_CHECKING:
    from blaxel.core.client.models import Env, SandboxForkResponse, SandboxLifecycle, SandboxSpec
    from blaxel.core.sandbox import (
        SandboxDrive,
        SandboxFileSystem,
        SandboxProcess,
        SyncSandboxDrive,
        SyncSandboxFileSystem,
        SyncSandboxProcess,
    )


def _dispose_created(sandbox):
    plane = getattr(sandbox, "_data_plane", None)
    if plane is not None:
        plane.close()


def _record_cleanup_failure(error, cleanup_error, sandbox_id):
    previous = getattr(error, "cleanup_errors", ())
    if not previous and getattr(error, "cleanup_error", None) is not None:
        previous = (error.cleanup_error,)
    error.cleanup_errors = (*previous, cleanup_error)
    error.cleanup_error = cleanup_error
    error.sandbox_id = sandbox_id


def __getattr__(name: str) -> Any:
    for module_name in ("blaxel.core.sandbox", "blaxel.core"):
        module = import_optional(module_name)
        if module is not None:
            try:
                return getattr(module, name)
            except AttributeError:
                pass
    raise AttributeError(name)


@dataclass(frozen=True)
class SandboxMetadata:
    name: str


def _request_values(request: Any) -> dict[str, Any]:
    if isinstance(request, dict):
        return dict(request)
    if hasattr(request, "to_dict"):
        values = request.to_dict()
        for key in ("on_log", "on_stdout", "on_stderr"):
            callback = getattr(request, key, None)
            if callback is not None:
                values[key] = callback
        return values
    return {key: value for key, value in vars(request).items() if value is not None}


class _AsyncNetwork:
    def __init__(self, vm: VM):
        self._vm = vm
        self._client = None

    def get_client(self):
        from ._compat_network import async_client

        if self._client is None:
            self._client = async_client(self._vm)
        return self._client

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(self, port: int, path: str = "/", method: str = "GET", **kwargs):
        from ._compat_network import request_path

        return await self.get_client().request(method, request_path(port, path), **kwargs)


class _Network:
    def __init__(self, vm: VM):
        self._vm = vm

    def fetch(self, port: int, path: str = "/", method: str = "GET", **kwargs):
        from ._compat_network import fetch

        return fetch(self._vm, port, path, method=method, **kwargs)


class SyncSandboxInstance(UnsupportedMixin):
    _compat_name = "blaxel"

    def __init__(self, vm: VM | None = None, native: Any | None = None, arker_config=None) -> None:
        self._vm = vm
        self._fork_config = dict(arker_config or {})
        self._native = native
        metadata = getattr(native, "metadata", None)
        metadata_name = metadata.get("name") if isinstance(metadata, dict) else getattr(metadata, "name", None)
        self.id = (
            vm.id
            if vm is not None
            else getattr(native, "id", None) or getattr(native, "sandbox_id", None) or metadata_name
        )
        self.metadata = sandbox_metadata(vm) if vm is not None else metadata
        self._data_plane = None
        self.network = getattr(native, "network", None) if native is not None else _Network(vm)

    @property
    def spec(self) -> SandboxSpec:
        return self._native.spec if self._native is not None else sandbox_spec(self._vm)

    def fork(
        self,
        target_name: str,
        *,
        target_type: str = "sandbox",
        port: int | None = None,
        traffic: int | None = None,
        custom_domain: str | None = None,
        prefix: str | None = None,
        snapshot_id: str | None = None,
        envs: list[Env] | None = None,
        lifecycle: SandboxLifecycle | dict[str, Any] | None = None,
    ) -> SandboxForkResponse:
        options = {
            "target_type": target_type,
            **{
                key: value
                for key, value in {
                    "port": port,
                    "traffic": traffic,
                    "custom_domain": custom_domain,
                    "prefix": prefix,
                    "snapshot_id": snapshot_id,
                    "envs": envs,
                    "lifecycle": lifecycle,
                }.items()
                if value is not None
            },
        }
        if self._native is not None:
            return self._native.fork(target_name, **options)
        return fork_sandbox(self._vm, target_name, options, self._fork_config)

    def _plane(self):
        if self._data_plane is None:
            from ._compat_blaxel_bootstrap import bootstrap_blaxel
            from ._compat_blaxel_data_plane import BlaxelDataPlane

            bootstrap_blaxel(self._vm)
            self._data_plane = BlaxelDataPlane(self._vm)
        return self._data_plane

    @property
    def process(self) -> SyncSandboxProcess:
        return self._native.process if self._native is not None else self._plane().process

    @property
    def fs(self) -> SyncSandboxFileSystem:
        return self._native.fs if self._native is not None else self._plane().fs

    @property
    def drives(self) -> SyncSandboxDrive:
        return self._native.drives if self._native is not None else self._plane().drives

    def fetch(self, port: int, path: str = "/", method: str = "GET", **kwargs):
        if self._native is not None:
            return self._native.fetch(port, path, method=method, **kwargs)
        return self.network.fetch(port, path, method=method, **kwargs)

    @staticmethod
    def _native_class() -> Any | None:
        module = import_optional("blaxel.core.sandbox")
        return getattr(module, "SyncSandboxInstance", None) if module is not None else None

    @staticmethod
    def _creation_cancelled() -> bool:
        return False

    @classmethod
    def create(cls, sandbox: Any = None, **kwargs: Any) -> SyncSandboxInstance:
        arker_config = pop_arker_config(kwargs)
        native = cls._native_class()

        def create_native() -> Any:
            if native is None:
                raise CompatibilityCreationNotStartedError("Native Blaxel SDK is not installed")
            return native.create(sandbox, **kwargs)

        values = (
            {key: value for key, value in _request_values(sandbox).items() if value is not None}
            if sandbox is not None
            else {}
        )

        def create_arker() -> VM:
            from ._compat_blaxel_create import (
                fork_named,
                image_configuration,
                lifetime_policies,
                registry_configuration,
                validate_name,
            )
            from ._compat_blaxel_data_plane import validate_native

            options = dict(kwargs)
            get_or_create = options.pop("create_if_not_exist", False)
            supported = {"name", "image", "memory", "ttl", "envs", "runtime", "network", "labels", "docker_config"}
            unknown = sorted(set(values) - supported)
            if options or unknown:
                names = [*sorted(options), *unknown]
                raise UnsupportedCompatibilityOption(
                    f"SandboxInstance.create received unsupported option(s) for Arker: {', '.join(names)}"
                )
            if values.get("runtime") is not None:
                raise UnsupportedCompatibilityOption(
                    "SandboxInstance.create option(s) are not supported by Arker: runtime"
                )
            creation_config = registry_configuration(
                values.get("docker_config"), values.get("image"), arker_config or {}
            )
            policies = lifetime_policies(values.get("ttl"), arker_config)
            validate_name(values.get("name"))
            validate_network(values.get("network"))
            from ._compat_blaxel_packages import validate_firewall_startup

            validate_firewall_startup(values.get("image"), (arker_config or {}).get("image_config"))
            validate_native()
            if cls._creation_cancelled():
                raise RoutingCancelled("Async routing was cancelled")
            memory = values.get("memory")
            return arker_create(
                creation_config,
                fork=lambda client, request: fork_named(
                    client,
                    request,
                    config=arker_config or {},
                    get_or_create=get_or_create,
                    cancelled=cls._creation_cancelled,
                ),
                metadata=creation_metadata(values.get("labels"), values.get("ttl")),
                policies=policies,
                name=values.get("name"),
                image=values.get("image"),
                image_config=image_configuration(values, arker_config or {}),
                resources={"memory_mib": memory} if memory is not None else None,
            )

        provider, result = routed_create(
            "blaxel",
            arker_config,
            {
                "arker": lambda: control_call("create", create_arker),
                "blaxel": create_native,
            },
        )
        instance = cls(
            vm=result if provider == "arker" else None,
            native=result if provider == "blaxel" else None,
            arker_config=arker_config,
        )
        if provider == "arker":
            try:
                instance._plane()
            except BaseException as error:
                if kwargs.get("create_if_not_exist", False) or cls._creation_cancelled():
                    raise
                try:
                    result.delete()
                except Exception as cleanup_error:
                    _record_cleanup_failure(error, cleanup_error, result.id)
                raise
        return instance

    @classmethod
    def get(cls, sandbox_id: str, **kwargs: Any) -> SyncSandboxInstance:
        arker_config = pop_arker_config(kwargs)
        if kwargs:
            raise TypeError(f"SandboxInstance.get received unsupported option(s): {', '.join(sorted(kwargs))}")
        instance = cls._lookup(sandbox_id, arker_config)
        if instance._vm is not None:
            instance._plane()
        return instance

    @classmethod
    def _lookup(cls, sandbox_id: str, arker_config: dict[str, Any]) -> SyncSandboxInstance:
        native = cls._native_class()
        found = routed_get(
            sandbox_id,
            "blaxel",
            arker_config,
            {
                "arker": lambda: control_call("get", lambda: arker_get(sandbox_id, arker_config)),
                "blaxel": lambda: get_or_none(lambda: native.get(sandbox_id)) if native is not None else None,
            },
        )
        if found is None:
            raise not_found()
        provider, result = found
        return cls(
            vm=result if provider == "arker" else None,
            native=result if provider == "blaxel" else None,
            arker_config=arker_config,
        )

    @classmethod
    def create_if_not_exists(cls, sandbox: Any = None, **kwargs: Any) -> SyncSandboxInstance:
        # Name reconciliation belongs to the provider's atomic create operation.
        return cls.create(sandbox, create_if_not_exist=True, **kwargs)

    def delete(self) -> None:
        if isinstance(self, str):
            SyncSandboxInstance._lookup(self, {}).delete()
            return
        if self._vm is not None:
            try:
                result = control_call("delete", self._vm.delete)
                if getattr(result, "deleted", True) is False:
                    raise not_found()
            finally:
                if self._data_plane is not None:
                    self._data_plane.close()
            return
        native = self._native_class()
        if native is not None and self.id:
            native.delete(self.id)
            return
        raise AttributeError("Native Blaxel sandbox does not expose delete().")


class _AsyncComponent:
    def __init__(self, component: Any) -> None:
        self._component = component

    def __getattr__(self, name: str) -> Any:
        member = getattr(self._component, name)
        if not callable(member):
            return member

        async def call(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.to_thread(member, *args, **kwargs)

        return call


class SandboxInstance:
    """Awaitable Blaxel surface using the same router and adapters as sync calls."""

    process: SandboxProcess
    fs: SandboxFileSystem
    drives: SandboxDrive

    def __init__(self, sandbox: SyncSandboxInstance, native: Any = None) -> None:
        self._sandbox = sandbox
        self._native = native
        self.id = sandbox.id
        self._data_plane = None
        if native is None:
            from ._compat_blaxel_data_plane import BlaxelDataPlane

            self._data_plane = BlaxelDataPlane(sandbox._vm, asynchronous=True)
        self.process = native.process if native is not None else self._data_plane.process
        self.fs = native.fs if native is not None else self._data_plane.fs
        self.drives = native.drives if native is not None else self._data_plane.drives
        self.network = native.network if native is not None else _AsyncNetwork(sandbox._vm)

    async def fetch(self, port: int, path: str = "/", method: str = "GET", **kwargs):
        if self._native is not None:
            return await self._native.fetch(port, path, method=method, **kwargs)
        return await self.network.fetch(port, path, method=method, **kwargs)

    @classmethod
    async def _wrap(cls, sandbox: SyncSandboxInstance) -> SandboxInstance:
        native = None
        if sandbox._vm is None:
            module = import_optional("blaxel.core.sandbox")
            native = sandbox._native
            if not isinstance(native, module.SandboxInstance):
                native = await module.SandboxInstance.get(sandbox.id)
        return cls(sandbox, native)

    @classmethod
    async def create(cls, sandbox: Any = None, **kwargs: Any) -> SandboxInstance:
        """Cancel the wait without deleting an accepted remote creation."""
        owns_creation = not kwargs.get("create_if_not_exist", False)
        module = import_optional("blaxel.core.sandbox")
        native = getattr(module, "SandboxInstance", None)
        bridge = NativeBridge(native, asyncio.get_running_loop())

        class RoutedSync(SyncSandboxInstance):
            @staticmethod
            def _native_class():
                return bridge if native is not None else None

            _creation_cancelled = staticmethod(bridge.cancelled.is_set)

        lock = threading.Lock()
        cancelled = False
        pending = []

        def create():
            result = RoutedSync.create(sandbox, **kwargs)
            with lock:
                if not cancelled:
                    pending.append(result)
                    return result
            # The originating event loop may already be closed.
            _dispose_created(result)
            return result

        try:
            created = await route(create, bridges=(bridge,))
        except asyncio.CancelledError:
            with lock:
                cancelled = True
                completed = pending.pop() if pending else None
            if completed is not None:
                _dispose_created(completed)
            raise
        try:
            return await cls._wrap(created)
        except asyncio.CancelledError:
            await route(lambda: _dispose_created(created), drain_on_cancel=True)
            raise
        except BaseException as error:
            if owns_creation:
                try:
                    native_result = getattr(created, "_native", None)
                    if native_result is not None:
                        await call(native_result.delete)
                    else:
                        await route(created.delete, drain_on_cancel=True)
                except Exception as cleanup_error:
                    _record_cleanup_failure(error, cleanup_error, created.id)
            raise

    @classmethod
    async def create_if_not_exists(cls, sandbox: Any = None, **kwargs: Any) -> SandboxInstance:
        return await cls.create(sandbox, create_if_not_exist=True, **kwargs)

    @classmethod
    async def get(cls, sandbox_id: str, **kwargs: Any) -> SandboxInstance:
        return await cls._wrap(await asyncio.to_thread(SyncSandboxInstance.get, sandbox_id, **kwargs))

    async def fork(
        self,
        target_name: str,
        *,
        target_type: str = "sandbox",
        port: int | None = None,
        traffic: int | None = None,
        custom_domain: str | None = None,
        prefix: str | None = None,
        snapshot_id: str | None = None,
        envs: list[Env] | None = None,
        lifecycle: SandboxLifecycle | dict[str, Any] | None = None,
    ) -> SandboxForkResponse:
        options = {
            "target_type": target_type,
            "port": port,
            "traffic": traffic,
            "custom_domain": custom_domain,
            "prefix": prefix,
            "snapshot_id": snapshot_id,
            "envs": envs,
            "lifecycle": lifecycle,
        }
        if self._native is not None:
            return await self._native.fork(target_name, **options)
        cancelled = threading.Event()
        try:
            return await asyncio.to_thread(
                fork_sandbox, self._sandbox._vm, target_name, options, self._sandbox._fork_config, cancelled.is_set
            )
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def delete(self) -> None:
        if isinstance(self, str):
            await asyncio.to_thread(SyncSandboxInstance.delete, self)
            return
        if self._native is not None:
            await self._native.delete()
            return
        try:
            if self._native is None:
                try:
                    await self._data_plane.aclose()
                finally:
                    await self.network.aclose()
        finally:
            await asyncio.to_thread(self._sandbox.delete)

    def __getattr__(self, name: str) -> Any:
        if self._native is not None:
            return getattr(self._native, name)
        return getattr(_AsyncComponent(self._sandbox), name)
