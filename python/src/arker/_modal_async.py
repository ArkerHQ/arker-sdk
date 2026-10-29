"""Modal's dual-mode entry points over the shared routing cancellation boundary."""

from __future__ import annotations

import asyncio
from functools import wraps

from ._compat_async import NativeBridge, route


class dualmethod:
    def __init__(self, method):
        self.method = method

    def __get__(self, instance, owner):
        method = self.method.__get__(instance, owner)
        target = owner if isinstance(self.method, classmethod) else instance

        @wraps(method)
        def sync(*args, **kwargs):
            return method(*args, **kwargs)

        @wraps(method)
        async def aio(*args, **kwargs):
            if target is None:
                return await invoke(args[0], method.__name__, *args[1:], **kwargs)
            return await invoke(target, method.__name__, *args, **kwargs)

        sync.aio = aio
        return sync


class NativeAsync:
    def __init__(self, native):
        self.native = native

    def __getattr__(self, name):
        return getattr(self.native, name).aio


async def invoke(target, name, *args, **kwargs):
    if name in ("create", "from_id"):
        native = target._native_sandbox()
        bridge = (
            NativeBridge(
                NativeAsync(native),
                asyncio.get_running_loop(),
                creation_cleanup=lambda sandbox: sandbox.terminate.aio(),
            )
            if native is not None
            else None
        )

        class Routed(target):
            @staticmethod
            def _native_sandbox():
                return bridge

        result = await route(
            lambda: getattr(Routed, name)(*args, **kwargs),
            bridges=[bridge] if bridge else [],
            cleanup=(lambda sandbox: invoke(sandbox, "terminate")) if name == "create" else None,
        )
        return target(vm=result._vm, native=result._native)
    if target._native is not None:
        return await getattr(target._native, name).aio(*args, **kwargs)
    if name == "exec":

        def discard(process):
            if target._processes is None:
                return
            from modal._utils.async_utils import synchronizer

            native = synchronizer._translate_in(process)
            target._processes.kill(native._process_id)

        return await route(lambda: target.exec(*args, **kwargs), cleanup=discard)
    return await route(lambda: target.terminate(*args, **kwargs))
