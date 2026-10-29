"""Translate authoritative Arker admission errors at E2B operation boundaries."""

from __future__ import annotations

from .computer import ArkerError


def _field(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def is_explicit_resume(status, code, details):
    return (
        status == 409
        and code == "invalid_state"
        and _field(details, "requirement") == "explicit_resume"
        and _field(_field(details, "resource"), "kind") == "vm"
    )


def translate_error(error):
    if isinstance(error, ArkerError) and is_explicit_resume(error.status, error.code, _field(error.body, "details")):
        from e2b import SandboxNotRunningException

        return SandboxNotRunningException("Sandbox is paused; call Sandbox.connect() to resume it")
    return error
