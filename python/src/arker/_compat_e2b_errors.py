"""Translate authoritative Arker admission errors at E2B operation boundaries."""

from __future__ import annotations

import inspect
from functools import wraps

from .computer import ArkerError


def _field(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def translate_error(error):
    if isinstance(error, ArkerError) and error.status == 409 and error.code == "invalid_state":
        details = _field(error.body, "details")
        if _field(details, "requirement") == "explicit_resume" and _field(_field(details, "resource"), "kind") == "vm":
            from e2b import SandboxNotRunningException

            return SandboxNotRunningException("Sandbox is paused; call Sandbox.connect() to resume it")
    return error


def translate_errors(method):
    def native_receiver(instance):
        component = vars(instance).get("_component", instance)
        return vars(component).get("_native") is not None

    if inspect.iscoroutinefunction(method):

        @wraps(method)
        async def asynchronous(*args, **kwargs):
            if native_receiver(args[0]):
                return await method(*args, **kwargs)
            try:
                return await method(*args, **kwargs)
            except ArkerError as error:
                translated = translate_error(error)
                if translated is error:
                    raise
                raise translated from error

        return asynchronous

    @wraps(method)
    def synchronous(*args, **kwargs):
        if native_receiver(args[0]):
            return method(*args, **kwargs)
        try:
            return method(*args, **kwargs)
        except ArkerError as error:
            translated = translate_error(error)
            if translated is error:
                raise
            raise translated from error

    return synchronous
