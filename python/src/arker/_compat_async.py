from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import threading
import warnings

from ._compat import UnsupportedCompatibilityOption


class RoutingCancelled(UnsupportedCompatibilityOption):
    pass


async def call(function, *args, **kwargs):
    result = await asyncio.to_thread(function, *args, **kwargs)
    return await result if inspect.isawaitable(result) else result


class NativeBridge:
    """Let the shared synchronous router invoke native code on its original loop."""

    def __init__(self, native, loop, *, creation_cleanup=None):
        self.native, self.loop = native, loop
        self.creation_cleanup = creation_cleanup
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
            async def complete():
                result = await function(*args, **kwargs)
                if name == "create" and self.cancelled.is_set() and self.creation_cleanup is not None:
                    try:
                        await self.creation_cleanup(result)
                    except Exception:
                        warnings.warn(
                            "Cleanup after cancelled native creation failed; reconcile the created sandbox",
                            RuntimeWarning,
                            stacklevel=2,
                        )
                    raise asyncio.CancelledError
                return result

            with self.lock:
                if self.cancelled.is_set():
                    raise RoutingCancelled("Async routing was cancelled")
                coroutine = complete()
                future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
                self.pending.add(future)
            try:
                return future.result()
            except concurrent.futures.CancelledError as error:
                raise RoutingCancelled("Async routing was cancelled") from error
            finally:
                with self.lock:
                    self.pending.discard(future)

        return invoke


_cleanups: set[asyncio.Task] = set()


def _cleanup_finished(task):
    _cleanups.discard(task)
    if not task.cancelled() and task.exception() is not None:
        warnings.warn(
            "Cleanup after cancelled sandbox creation failed; reconcile the created sandbox",
            RuntimeWarning,
            stacklevel=2,
        )


async def route(function, *, bridges=(), cleanup=None):
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(asyncio.to_thread(function))
    try:
        return await asyncio.shield(task)
    except RoutingCancelled as error:
        raise asyncio.CancelledError from error
    except asyncio.CancelledError:
        for bridge in bridges:
            bridge.cancel()

        def discard(done):
            if done.cancelled() or done.exception() is not None or cleanup is None:
                return
            pending = loop.create_task(call(cleanup, done.result()))
            _cleanups.add(pending)
            pending.add_done_callback(_cleanup_finished)

        task.add_done_callback(discard)
        raise
    finally:
        for bridge in bridges:
            bridge.cancel()
