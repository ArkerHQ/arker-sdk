from __future__ import annotations

import asyncio

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

            # Bootstrap precedes mounts; construct async clients on the caller's loop.
            self._prepared_descriptor, _ = bootstrap_envd(self._vm, **({"envs": envs} if envs is not None else {}))

        @staticmethod
        def _native_sandbox():
            return bridge

    return await route(
        lambda: getattr(Routed, method)(*args, **kwargs),
        bridges=[bridge] if bridge else [],
        cleanup=(lambda result: result.kill()) if method == "create" else None,
    )


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

        vm = self._sandbox._vm
        if vm is None:
            return
        descriptor = getattr(self._sandbox, "_prepared_descriptor", None)
        if descriptor is None:
            descriptor, _ = await bootstrap_envd_async(vm)
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
            await sandbox.kill()
            raise
        return sandbox

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
        if self._data_plane is not None:
            await self._data_plane.aclose()
        return await _call(self._sandbox.kill, **kwargs)

    @classmethod
    async def _class_kill(cls, sandbox_id: str | AsyncSandbox, **kwargs):
        if isinstance(sandbox_id, cls):
            return await sandbox_id.kill(**kwargs)
        return await _route("kill", sandbox_id, **kwargs)
