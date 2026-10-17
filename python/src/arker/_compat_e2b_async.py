from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import shlex
import threading
import warnings
from typing import Any

from ._compat import UnsupportedCompatibilityOption, _InstanceOrClassMethod, import_optional


class _RoutingCancelled(UnsupportedCompatibilityOption):
    pass


async def _call(function, *args, **kwargs):
    result = await asyncio.to_thread(function, *args, **kwargs)
    return await result if inspect.isawaitable(result) else result


class _NativeBridge:
    def __init__(self, native, loop):
        self.native, self.loop = native, loop
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.pending = set()

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            for future in self.pending:
                future.cancel()

    def __getattr__(self, name):
        function = getattr(self.native, name)

        def invoke(*args, **kwargs):
            with self.lock:
                if self.cancelled.is_set():
                    raise _RoutingCancelled("Async routing was cancelled")
                coroutine = function(*args, **kwargs)
                future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
                self.pending.add(future)
            try:
                return future.result()
            except concurrent.futures.CancelledError as error:
                raise _RoutingCancelled("Async routing was cancelled") from error
            finally:
                with self.lock:
                    self.pending.discard(future)

        return invoke


_cleanups: set[asyncio.Task] = set()


def _cleanup_finished(task):
    _cleanups.discard(task)
    if not task.cancelled() and task.exception() is not None:
        warnings.warn(
            "Cleanup after cancelled E2B creation failed; reconcile the created sandbox", RuntimeWarning, stacklevel=2
        )


async def _route(method, *args, **kwargs):
    from .e2b import Sandbox

    loop = asyncio.get_running_loop()
    module = import_optional("e2b")
    native = getattr(module, "AsyncSandbox", None) if module else None
    bridge = _NativeBridge(native, loop) if native else None

    class Routed(Sandbox):
        @staticmethod
        def _native_sandbox():
            return bridge

    task = asyncio.create_task(asyncio.to_thread(getattr(Routed, method), *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except _RoutingCancelled as error:
        raise asyncio.CancelledError from error
    except asyncio.CancelledError:
        if bridge:
            bridge.cancel()

        def discard(done):
            if done.cancelled() or done.exception() is not None:
                return
            if method == "create":
                cleanup = loop.create_task(_call(done.result().kill))
                _cleanups.add(cleanup)
                cleanup.add_done_callback(_cleanup_finished)

        task.add_done_callback(discard)
        raise
    finally:
        if bridge:
            bridge.cancel()


class _AsyncFiles:
    def __init__(self, files):
        self._files = files

    def __getattr__(self, name):
        if self._files._native is not None:
            return getattr(self._files._native, name)
        function = getattr(self._files, name)

        async def invoke(*args, **kwargs):
            return await _call(function, *args, **kwargs)

        return invoke


class _AsyncCommands:
    def __init__(self, commands):
        self._commands = commands

    def __getattr__(self, name):
        if self._commands._native is not None:
            return getattr(self._commands._native, name)
        function = getattr(self._commands, name)

        async def invoke(*args, **kwargs):
            return await _call(function, *args, **kwargs)

        return invoke

    async def run(
        self,
        cmd: str,
        background: bool | None = None,
        envs: dict[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        on_stdout: Any = None,
        on_stderr: Any = None,
        stdin: bool | None = None,
        timeout: float | None = 60,
        request_timeout: float | None = None,
    ):
        if self._commands._native is not None:
            return await self._commands._native.run(
                cmd,
                background=background,
                envs=envs,
                user=user,
                cwd=cwd,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
                stdin=stdin,
                timeout=timeout,
                request_timeout=request_timeout,
            )
        if user is not None or request_timeout is not None:
            raise UnsupportedCompatibilityOption("E2B user and per-request timeouts are not yet supported on Arker")
        from ._compat_e2b import async_command_handle, require_native_handle

        require_native_handle()
        current = await _call(
            self._commands._processes.exec,
            {
                "command": f"exec /bin/bash -lc {shlex.quote(cmd)}",
                "workingDir": cwd or "/home/user",
                "env": envs or {},
                "stdin": bool(stdin),
            },
        )
        handle = async_command_handle(self._commands._processes, current, timeout, on_stdout, on_stderr)
        return handle if background else await handle.wait()

    async def connect(self, pid, timeout=60, request_timeout=None, on_stdout=None, on_stderr=None):
        if self._commands._native is not None:
            return await self._commands._native.connect(
                pid,
                timeout=timeout,
                request_timeout=request_timeout,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
            )
        if request_timeout is not None:
            raise UnsupportedCompatibilityOption("Per-request command timeouts are not yet supported on Arker")
        from ._compat_e2b import async_command_handle, process_control

        current = await _call(process_control, self._commands._processes.get, str(pid))
        return async_command_handle(self._commands._processes, current, timeout, on_stdout, on_stderr)


class AsyncSandbox:
    def __init__(self, sandbox):
        self._sandbox = sandbox
        self.sandbox_id, self.sandboxId = sandbox.sandbox_id, sandbox.sandboxId
        self.commands = _AsyncCommands(sandbox.commands)
        self.files = _AsyncFiles(sandbox.files)

    def __getattr__(self, name):
        return getattr(self._sandbox, name)

    @classmethod
    async def create(cls, template: str | None = None, **kwargs):
        return cls(await _route("create", template, **kwargs))

    @classmethod
    async def connect(cls, sandbox_id: str, **kwargs):
        return cls(await _route("connect", sandbox_id, **kwargs))

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
        if kwargs:
            raise UnsupportedCompatibilityOption("Arker sandbox deletion does not yet support per-request options")
        return await _call(self._sandbox.kill)

    @classmethod
    async def _class_kill(cls, sandbox_id: str | AsyncSandbox, **kwargs):
        if isinstance(sandbox_id, cls):
            return await sandbox_id.kill(**kwargs)
        return await _route("kill", sandbox_id, **kwargs)
