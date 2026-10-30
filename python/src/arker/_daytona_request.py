"""Request timing for the native process clients."""

from __future__ import annotations

import asyncio
import inspect
import math
from contextlib import asynccontextmanager
from functools import wraps

import httpx2 as httpx

from ._compat import import_optional
from ._daytona_sync_request import sync_request_executor
from ._request_timeout import request_executor_scope, request_timeout_scope


class _ResponseDeadline(httpx.ReadTimeout):
    """An elapsed total response budget, distinct from transport phase timeouts."""


def normalized_timeout(value):
    if value is not None and (not math.isfinite(value) or value < 0):
        errors = import_optional("daytona.common.errors")
        error = errors.DaytonaBadRequestError if errors is not None else ValueError
        raise error("Timeout must be a finite non-negative number")
    return None if value is None or value == 0 else float(math.ceil(value)) if value < 1 else float(value)


def request_timeout(function):
    signature = inspect.signature(function)

    @wraps(function)
    def invoke(*args, **kwargs):
        seconds = normalized_timeout(signature.bind(*args, **kwargs).arguments.get("request_timeout"))
        try:
            with request_timeout_scope(seconds), sync_request_executor():
                return function(*args, **kwargs)
        except httpx.TimeoutException as cause:
            errors = import_optional("daytona.common.errors")
            if errors is None:
                raise
            error_type = errors.DaytonaConnectionTimeoutError
            # Native maps built-in TimeoutError, whereas asyncio has a separate
            # exception type on Python 3.10 and therefore returns DaytonaError.
            if isinstance(cause, _ResponseDeadline) and asyncio.TimeoutError is not TimeoutError:
                error_type = errors.DaytonaError
            error = error_type(str(cause))
            for field in ("session_id", "process_id", "run_id"):
                if hasattr(cause, field):
                    setattr(error, field, getattr(cause, field))
            raise error from cause

    return invoke


def is_response_timeout(error):
    errors = import_optional("daytona.common.errors")
    return (
        isinstance(error, httpx.TimeoutException)
        or isinstance(error.__cause__, _ResponseDeadline)
        or (errors is not None and isinstance(error, errors.DaytonaConnectionTimeoutError))
    )


@asynccontextmanager
async def async_request_timeout(function, args, kwargs):
    parameters = inspect.signature(function).bind(*args, **kwargs).arguments
    seconds = normalized_timeout(parameters.get("request_timeout"))
    loop = asyncio.get_running_loop()
    pending = set()
    cancelled = False

    class Requests:
        def cancel(self):
            nonlocal cancelled
            cancelled = True
            for task in tuple(pending):
                task.cancel()

    async with httpx.AsyncClient(http2=False, timeout=None) as client:

        async def request(method, url, headers, data, timeout):
            if cancelled:
                raise asyncio.CancelledError
            task = asyncio.current_task()
            pending.add(task)

            async def receive():
                async with client.stream(method, url, headers=headers, content=data, timeout=None) as response:
                    try:
                        content = await response.aread()
                    except httpx.RequestError:
                        if response.status_code >= 400:
                            return response.status_code, b""
                        raise
                    return response.status_code, content

            try:
                return await asyncio.wait_for(receive(), seconds)
            except asyncio.TimeoutError as cause:
                raise _ResponseDeadline("HTTP response timeout") from cause
            finally:
                pending.discard(task)

        def execute(method, url, headers, data, timeout):
            return asyncio.run_coroutine_threadsafe(request(method, url, headers, data, timeout), loop).result()

        with request_executor_scope(execute):
            yield Requests()
