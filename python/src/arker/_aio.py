"""The async face of the compatibility layers: the sync facade's own calls,
run in worker threads so they never block the event loop.

``AsyncWrapper`` subclasses stand for one sync facade class each. Their
methods are the sync object's methods as coroutines, their sub-objects (a
sandbox's ``commands``, ``files``...) are wrapped in turn, and only what an
async provider API does differently is written out by hand.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, ClassVar

from ._compat import UnsupportedMixin, _UnsupportedClass

# Each call holds its thread for a network round trip, or for a whole command,
# so the pool is sized for concurrent calls rather than for CPUs.
_EXECUTOR = ThreadPoolExecutor(max_workers=128, thread_name_prefix="arker-compat-async")
_ASYNC_TYPES: dict[type, type[AsyncWrapper]] = {}
_DONE = object()


async def deliver(callback: Callable[..., Any] | None, *args: Any) -> None:
    """Call a provider callback, which may be sync or async."""
    if callback is not None and inspect.isawaitable(result := callback(*args)):
        await result


def _on_loop(callback: Callable[..., Any], loop: asyncio.AbstractEventLoop) -> Callable[..., None]:
    """``callback``, called from a worker thread but run on the event loop, as async SDKs run them."""

    def call(*args: Any) -> None:
        asyncio.run_coroutine_threadsafe(deliver(callback, *args), loop).result()

    return call


def _unwrap(value: Any) -> Any:
    if isinstance(value, AsyncWrapper):
        return value._sync
    if isinstance(value, dict):
        return {key: _unwrap(item) for key, item in value.items()}
    if type(value) in (list, tuple):
        return type(value)(_unwrap(item) for item in value)
    return value


def _sync_argument(value: Any, name: str, loop: asyncio.AbstractEventLoop) -> Any:
    if name.startswith("on_") and callable(value):
        return _on_loop(value, loop)
    return _unwrap(value)


async def call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """``function(*args, **kwargs)`` in a worker thread. Wrapped objects are
    passed as the sync objects they wrap, and ``on_*`` callbacks run on the loop."""
    loop = asyncio.get_running_loop()
    try:
        bound = inspect.signature(function).bind(*args, **kwargs)
    except (TypeError, ValueError):
        pass  # Let the call itself report the bad arguments.
    else:
        for name, value in bound.arguments.items():
            bound.arguments[name] = _sync_argument(value, name, loop)
        args, kwargs = bound.args, bound.kwargs
    context = contextvars.copy_context()
    return await loop.run_in_executor(_EXECUTOR, functools.partial(context.run, function, *args, **kwargs))


async def iterate(iterator: Iterator[Any]) -> AsyncIterator[Any]:
    """A blocking iterator's items, each fetched in a worker thread."""
    while (item := await call(next, iterator, _DONE)) is not _DONE:
        yield wrap(item)


def wrap(value: Any) -> Any:
    """``value`` with sync facade objects replaced by their async wrappers."""
    for cls in type(value).__mro__:
        if cls in _ASYNC_TYPES:
            return _ASYNC_TYPES[cls](value)
    if isinstance(value, list):
        return [wrap(item) for item in value]
    if isinstance(value, Iterator):
        return iterate(value)
    return value


def asynchronous(function: Callable[..., Any]) -> Callable[..., Any]:
    """``function`` as a coroutine function, or as an async generator function
    when it is a generator function; its results are wrapped."""
    if inspect.iscoroutinefunction(function):
        return function
    if inspect.isgeneratorfunction(function):

        @functools.wraps(function)
        def generator(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            return iterate(function(*args, **kwargs))

        return generator

    @functools.wraps(function)
    async def coroutine(*args: Any, **kwargs: Any) -> Any:
        return wrap(await call(function, *args, **kwargs))

    return coroutine


def _forwards(target: Any, name: str) -> bool:
    if name.startswith("_"):
        return False
    try:
        inspect.getattr_static(target, name)
    except AttributeError:
        return False
    return True


def _async_attribute(value: Any) -> Any:
    return asynchronous(value) if callable(value) and not isinstance(value, type) else wrap(value)


class _AsyncClass(_UnsupportedClass):
    def __getattr__(cls, name: str) -> Any:
        if cls._sync_type is not None and _forwards(cls._sync_type, name):
            return _async_attribute(getattr(cls._sync_type, name))
        return super().__getattr__(name)


class AsyncWrapper(UnsupportedMixin, metaclass=_AsyncClass):
    """The async version of a sync facade object. Subclass it with
    ``wraps=SyncClass``; anything the subclass does not define is the sync
    object's, with methods awaited in a worker thread."""

    _sync_type: ClassVar[type | None] = None

    def __init_subclass__(cls, wraps: type | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if wraps is None:
            return
        cls._sync_type = wraps
        _ASYNC_TYPES[wraps] = cls
        cls._compat_path = vars(cls).get("_compat_path") or getattr(wraps, "_compat_path", wraps.__name__)
        cls._compat_hints = vars(cls).get("_compat_hints") or getattr(wraps, "_compat_hints", {})

    def __init__(self, sync: Any) -> None:
        self._sync = sync

    def __getattr__(self, name: str) -> Any:
        if name != "_sync" and _forwards(self._sync, name):
            return _async_attribute(getattr(self._sync, name))
        return super().__getattr__(name)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._sync!r})"


class AsyncMethods:
    """A sync object's methods with an extra ``.aio`` variant, as Modal offers them."""

    def __init__(self, function: Any) -> None:
        self._function = function

    def __get__(self, obj: Any, cls: type) -> Any:
        return _WithAio(self._function.__get__(obj, cls))

    @classmethod
    def install(cls, owner: type, *names: str) -> None:
        for name in names:
            setattr(owner, name, cls(vars(owner)[name]))


class _WithAio:
    def __init__(self, function: Callable[..., Any]) -> None:
        functools.update_wrapper(self, function)
        self._function = function

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._function(*args, **kwargs)

    def aio(self, *args: Any, **kwargs: Any) -> Any:
        """The same call, awaited without blocking the event loop."""
        if inspect.isgeneratorfunction(self._function):
            return iterate(self._function(*args, **kwargs))
        return call(self._function, *args, **kwargs)
