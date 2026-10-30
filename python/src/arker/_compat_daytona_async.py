from __future__ import annotations

import asyncio

from ._compat import CompatibilityCreationNotStartedError, UnsupportedCompatibilityOption, import_optional
from ._compat_async import NativeBridge, call, route
from ._daytona_pty import AsyncPtyProcess


class _AsyncProcess(AsyncPtyProcess):
    def __init__(self, process):
        self._process = process

    async def create_session(self, session_id, request_timeout=None):
        await route(
            lambda: self._process._create_session_owned(session_id, request_timeout),
            cleanup=lambda identifier: self._process._discard_created_session(session_id, identifier),
            drain_on_cancel=True,
        )

    async def get_session_command_logs_async(self, session_id, command_id, on_stdout, on_stderr):
        return await self._process.get_session_command_logs_async(session_id, command_id, on_stdout, on_stderr)

    def __getattr__(self, name):
        function = getattr(self._process, name)

        async def invoke(*args, **kwargs):
            return await call(function, *args, **kwargs)

        return invoke


class AsyncSandbox:
    def __init__(self, sandbox):
        self._sandbox = sandbox
        self.id = sandbox.id
        if sandbox._native is not None:
            self.fs = getattr(sandbox._native, "fs", None)
        else:
            from ._daytona_filesystem import AsyncFileSystem

            self.fs = AsyncFileSystem(sandbox._vm)
        self.process = sandbox._native.process if sandbox._native is not None else _AsyncProcess(sandbox.process)

    def __getattr__(self, name):
        return getattr(self._sandbox, name)

    async def get_user_home_dir(self) -> str:
        if self._sandbox._native is not None:
            return await self._sandbox._native.get_user_home_dir()
        return await call(self._sandbox.get_user_home_dir)

    async def pause(self, timeout: float = 60) -> None:
        if self._sandbox._native is not None:
            return await self._sandbox._native.pause(timeout)
        return await call(self._sandbox.pause, timeout)

    async def start(self, timeout: float | None = 60):
        if self._sandbox._native is not None:
            return await self._sandbox._native.start(timeout)
        return await call(self._sandbox.start, timeout)

    async def stop(self, timeout: float | None = 60, force: bool = False):
        if self._sandbox._native is not None:
            return await self._sandbox._native.stop(timeout, force=force)
        return await call(self._sandbox.stop, timeout, force=force)

    async def archive(self, request_timeout: float | None = None) -> None:
        if self._sandbox._native is not None:
            return await self._sandbox._native.archive(request_timeout=request_timeout)
        return await call(self._sandbox.archive, request_timeout=request_timeout)

    async def fork(self, name: str | None = None, timeout: float | None = 60):
        if self._sandbox._native is not None:
            return await self._sandbox._native.fork(name=name, timeout=timeout)
        return type(self)(await call(self._sandbox.fork, name=name, timeout=timeout))

    async def delete(self, timeout=60, *, wait=False):
        if self._sandbox._native is not None:
            return await self._sandbox._native.delete(timeout, wait=wait)
        if timeout != 60 or wait:
            raise UnsupportedCompatibilityOption(
                "Daytona deletion waiting and custom timeouts are not supported on Arker"
            )
        return await call(self._sandbox.delete)


class AsyncDaytona:
    def __init__(self, *args, **kwargs):
        from .daytona import Daytona

        self._configuration = Daytona(*args, **kwargs)
        self._native = None
        self._loop = None
        self._closed = False

    def _bind_loop(self):
        loop = asyncio.get_running_loop()
        if self._closed:
            raise RuntimeError("AsyncDaytona client is closed")
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("AsyncDaytona must be used on its original event loop")
        self._loop = loop
        return loop

    def _native_client(self):
        self._bind_loop()
        if self._native is None:
            module = import_optional("daytona")
            if module is None:
                raise CompatibilityCreationNotStartedError("Native Daytona SDK is not installed")
            self._native = module.AsyncDaytona(*self._configuration._native_args, **self._configuration._native_kwargs)
        return self._native

    @property
    def volume(self):
        from ._daytona_volumes import AsyncVolumeService

        return AsyncVolumeService(self)

    def __getattr__(self, name):
        return getattr(self._native_client(), name)

    async def _route(self, method, *args, **kwargs):
        from .daytona import Daytona

        loop = self._bind_loop()
        owner = self

        class NativeOperations:
            async def create(self, *args, **kwargs):
                return await owner._native_client().create(*args, **kwargs)

            async def get(self, *args, **kwargs):
                return await owner._native_client().get(*args, **kwargs)

        bridge = NativeBridge(NativeOperations(), loop, creation_cleanup=lambda sandbox: sandbox.delete())

        from ._daytona_volumes import NativeVolumeOperations, VolumeService

        volumes = NativeBridge(NativeVolumeOperations(owner), loop)

        class Routed(Daytona):
            @property
            def volume(self):
                return VolumeService(
                    owner._configuration._arker_config,
                    lambda: volumes,
                    native_available=owner._native is not None or owner._configuration._native_volume_available(),
                )

            def _native_client(self):
                return bridge

        routed = Routed(*self._configuration._native_args, **self._configuration._native_kwargs)
        routed._arker_config = self._configuration._arker_config
        result = await route(
            lambda: getattr(routed, method)(*args, **kwargs),
            bridges=[bridge, volumes],
            cleanup=(lambda sandbox: AsyncSandbox(sandbox).delete()) if method == "create" else None,
        )
        return AsyncSandbox(result)

    async def start(self, sandbox: AsyncSandbox, timeout: float = 60) -> None:
        self._bind_loop()
        return await sandbox.start(timeout)

    async def stop(self, sandbox: AsyncSandbox, timeout: float = 60) -> None:
        self._bind_loop()
        return await sandbox.stop(timeout)

    async def create(self, *args, **kwargs):
        return await self._route("create", *args, **kwargs)

    async def get(self, sandbox_id_or_name):
        return await self._route("get", sandbox_id_or_name)

    async def delete(self, sandbox, timeout=60, wait=False):
        self._bind_loop()
        target = await self.get(sandbox) if isinstance(sandbox, str) else sandbox
        return await target.delete(timeout, wait=wait)

    async def close(self):
        if self._closed:
            return
        self._bind_loop()
        try:
            if self._native is not None:
                await self._native.close()
        finally:
            self._closed = True

    async def __aenter__(self):
        self._bind_loop()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()
