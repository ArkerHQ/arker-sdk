from __future__ import annotations

import asyncio
import builtins

from ._compat import _InstanceOrClassMethod, import_optional
from ._compat_async import NativeBridge, route
from ._compat_async import call as _call
from ._compat_e2b_lifecycle import AsyncLifecycle


async def _route(method, *args, **kwargs):
    from .e2b import Sandbox

    loop = asyncio.get_running_loop()
    module = import_optional("e2b")
    native = getattr(module, "AsyncSandbox", None) if module else None
    bridge = NativeBridge(native, loop, creation_cleanup=lambda sandbox: sandbox.kill()) if native else None

    class Routed(Sandbox):
        def _prepare_data_plane(self, *, envs=None):
            from ._compat_e2b_bootstrap import bootstrap_envd
            from ._compat_e2b_urls import prepare_file_urls

            # Bootstrap precedes mounts; construct async clients on the caller's loop.
            self._prepared_descriptor, _ = bootstrap_envd(self._vm, **({"envs": envs} if envs is not None else {}))
            prepare_file_urls(self._vm)

        @staticmethod
        def _native_sandbox():
            return bridge

    return await route(
        lambda: getattr(Routed, method)(*args, **kwargs),
        bridges=[bridge] if bridge else [],
        cleanup=(lambda result: result.kill()) if method == "create" else None,
    )


async def _dispose_fork_clients(sandboxes):
    failures = []
    for sandbox in sandboxes:
        if sandbox._data_plane is not None:
            try:
                await sandbox._data_plane.aclose()
            except Exception as error:
                failures.append(error)
        try:
            await _call(sandbox._sandbox._vm.delete)
        except Exception as error:
            failures.append(error)
    if failures:
        raise builtins.ExceptionGroup("Fork client cleanup failed", failures)


class AsyncSandbox:
    def __init__(self, sandbox):
        self._sandbox = sandbox
        self._data_plane = None
        self.sandbox_id, self.sandboxId = sandbox.sandbox_id, sandbox.sandboxId
        self.commands = getattr(sandbox._native, "commands", None)
        self.files = getattr(sandbox._native, "files", None)
        self.git = getattr(sandbox._native, "git", None)

    async def _prepare_data_plane(self):
        from ._compat_e2b_bootstrap import bootstrap_envd_async
        from ._compat_e2b_data_plane import create_data_plane
        from ._compat_e2b_urls import prepare_file_urls_async

        vm = self._sandbox._vm
        if vm is None:
            return
        descriptor = getattr(self._sandbox, "_prepared_descriptor", None)
        if descriptor is None:
            descriptor, _ = await bootstrap_envd_async(vm)
            await prepare_file_urls_async(vm)
        else:
            del self._sandbox._prepared_descriptor
        replacement = create_data_plane(descriptor, vm._client._api_key, asynchronous=True)
        previous = self._data_plane
        self._data_plane = replacement
        self._sandbox._envd_descriptor = descriptor
        self.commands, self.files, self.git = replacement.commands, replacement.files, replacement.git
        if previous is not None:
            await previous.aclose()

    @property
    def pty(self):
        if self._data_plane is not None:
            return self._data_plane.pty
        if self._sandbox._native is not None:
            return self._sandbox._native.pty
        raise RuntimeError("Sandbox data plane is not initialized; use create or connect")

    def __getattr__(self, name):
        return getattr(self._sandbox, name)

    @classmethod
    async def create(cls, template: str | None = None, **kwargs):
        sandbox = cls(await _route("create", template, **kwargs))
        try:
            await sandbox._prepare_data_plane()
        except BaseException:
            try:
                await sandbox.kill()
            except BaseException as cleanup_error:
                raise RuntimeError(
                    f"Sandbox creation and cleanup failed for VM {sandbox.sandbox_id}"
                ) from cleanup_error
            raise
        return sandbox

    @classmethod
    async def _class_fork(cls, sandbox_id, timeout=None, count=None, **opts):
        from e2b import AsyncSandbox as NativeSandbox
        from e2b import SandboxNotFoundException

        from .e2b import Sandbox

        # Lookup is read-only: unlike connect, it never updates the parent's deadline.
        found, _ = await _call(Sandbox._lookup, sandbox_id, dict(opts))
        if found is None:
            raise SandboxNotFoundException("Sandbox was not found")
        provider, vm = found
        if provider == "e2b":
            from ._compat import pop_arker_config

            pop_arker_config(opts)
            values = await NativeSandbox.fork(sandbox_id, timeout=timeout, count=count, **opts)
            return [value if isinstance(value, Exception) else cls(Sandbox(native=value)) for value in values]
        from ._compat import pop_arker_config

        pop_arker_config(opts)
        return await cls(Sandbox(vm=vm)).fork(timeout=timeout, count=count, **opts)

    @_InstanceOrClassMethod
    async def fork(self, timeout=None, count=None, **opts):
        from ._compat_e2b_bootstrap import bootstrap_envd
        from ._compat_e2b_fork import _join_cleanup, fork_arker_async
        from ._compat_e2b_urls import prepare_file_urls
        from .e2b import Sandbox

        if self._sandbox._native is not None:
            values = await self._sandbox._native.fork(timeout=timeout, count=count, **opts)
            return [value if isinstance(value, Exception) else type(self)(Sandbox(native=value)) for value in values]

        def prepare(vm):
            sandbox = Sandbox(vm=vm)
            sandbox._prepared_descriptor, _ = bootstrap_envd(vm)
            prepare_file_urls(vm)
            return sandbox

        values = await fork_arker_async(self._sandbox._vm, prepare, timeout=timeout, count=count, **opts)
        results = []
        handles = []
        try:
            for value in values:
                if isinstance(value, Exception):
                    results.append(value)
                    continue
                sandbox = type(self)(value)
                handles.append(sandbox)
                try:
                    await sandbox._prepare_data_plane()
                except Exception as error:
                    try:
                        await _dispose_fork_clients([sandbox])
                    except Exception as cleanup:
                        error = builtins.ExceptionGroup(
                            "Fork client initialization and cleanup failed", [error, cleanup]
                        )
                    results.append(error)
                else:
                    results.append(sandbox)
            return results
        except BaseException as error:
            # Include values whose async client has not been constructed yet.
            by_vm = {sandbox._sandbox._vm.id: sandbox for sandbox in handles}
            all_handles = [
                by_vm.get(value._vm.id) or type(self)(value) for value in values if not isinstance(value, Exception)
            ]
            try:
                await _join_cleanup(asyncio.create_task(_dispose_fork_clients(all_handles)))
            except Exception as cleanup:
                raise builtins.BaseExceptionGroup(
                    "Fork client initialization cleanup failed", [error, cleanup]
                ) from None
            raise

    @classmethod
    async def _class_connect(cls, sandbox_id: str, **kwargs):
        sandbox = cls(await _route("connect", sandbox_id, **kwargs))
        await sandbox._prepare_data_plane()
        return sandbox

    def _lifecycle(self):
        return AsyncLifecycle(vm=self._sandbox._vm, native=self._sandbox._native)

    @_InstanceOrClassMethod
    async def connect(self, timeout=None, *, on_resume="restore", **opts):
        await self._lifecycle().connect(timeout=timeout, on_resume=on_resume, **opts)
        if self._sandbox._vm is not None:
            await self._prepare_data_plane()
        return self

    @classmethod
    async def _class_pause(cls, sandbox_id, keep_memory=None, *, mode=None, **opts):
        return await _route("pause", sandbox_id, keep_memory=keep_memory, mode=mode, **opts)

    @classmethod
    async def _class_beta_pause(cls, sandbox_id, keep_memory=None, *, mode=None, **opts):
        return await _route("beta_pause", sandbox_id, keep_memory=keep_memory, mode=mode, **opts)

    @classmethod
    async def _class_set_timeout(cls, sandbox_id, timeout, **opts):
        return await _route("set_timeout", sandbox_id, timeout, **opts)

    @_InstanceOrClassMethod
    async def pause(self, keep_memory=None, *, mode=None, **opts):
        return await self._lifecycle().pause(keep_memory, mode=mode, **opts)

    @_InstanceOrClassMethod
    async def beta_pause(self, keep_memory=None, *, mode=None, **opts):
        return await self._lifecycle().beta_pause(keep_memory, mode=mode, **opts)

    @_InstanceOrClassMethod
    async def set_timeout(self, timeout, **opts):
        return await self._lifecycle().set_timeout(timeout, **opts)

    async def is_running(self, request_timeout=None):
        return await self._lifecycle().is_running(request_timeout)

    @classmethod
    def list(cls, query=None, limit=None, next_token=None, order=None, **opts):
        from ._compat import pop_arker_config
        from ._compat_e2b_inventory import list_sandboxes

        config = pop_arker_config(opts)
        module = import_optional("e2b")
        native = getattr(module, "AsyncSandbox", None) if module else None
        return list_sandboxes(
            native, config, asynchronous=True, query=query, limit=limit, next_token=next_token, order=order, **opts
        )

    @classmethod
    async def _class_get_info(cls, sandbox_id, **opts):
        return await _route("get_info", sandbox_id, **opts)

    @_InstanceOrClassMethod
    async def get_info(self, **opts):
        return await self._lifecycle().get_info(**opts)

    def download_url(self, path: str, user: str | None = None, use_signature_expiration: int | None = None) -> str:
        return self._sandbox.download_url(path, user, use_signature_expiration)

    def upload_url(self, path: str, user: str | None = None, use_signature_expiration: int | None = None) -> str:
        return self._sandbox.upload_url(path, user, use_signature_expiration)

    def get_host(self, port: int):
        return self._sandbox.get_host(port)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.kill()

    @_InstanceOrClassMethod
    async def kill(self, **kwargs):
        if self._sandbox._native is not None:
            return await self._sandbox._native.kill(**kwargs)
        try:
            if self._data_plane is not None:
                await self._data_plane.aclose()
        finally:
            deleted = await _call(self._sandbox.kill, **kwargs)
        return deleted

    @classmethod
    async def _class_kill(cls, sandbox_id: str | AsyncSandbox, **kwargs):
        if isinstance(sandbox_id, cls):
            return await sandbox_id.kill(**kwargs)
        return await _route("kill", sandbox_id, **kwargs)
