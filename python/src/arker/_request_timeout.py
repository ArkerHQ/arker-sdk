"""Context-local HTTP phase timeouts, independent of operation deadlines."""

from contextlib import contextmanager
from contextvars import ContextVar

import httpx2 as httpx

_current: ContextVar[httpx.Timeout | None] = ContextVar("arker_http_phase_timeout", default=None)


def transport_timeout(default):
    value = _current.get()
    return default if value is None else httpx.Timeout(value)


def has_transport_timeout():
    return _current.get() is not None


@contextmanager
def request_timeout_scope(timeout):
    token = _current.set(httpx.Timeout(timeout))
    try:
        yield
    finally:
        _current.reset(token)


@contextmanager
def without_request_timeout():
    """Reconcile or clean up independently of the initiating request's timeout."""
    token = _current.set(None)
    try:
        yield
    finally:
        _current.reset(token)
