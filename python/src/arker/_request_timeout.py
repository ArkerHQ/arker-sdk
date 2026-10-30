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
    executor_token = _executor.set(None)
    try:
        yield
    finally:
        _executor.reset(executor_token)
        _current.reset(token)


_executor: ContextVar[object] = ContextVar("arker_http_executor", default=None)


def request_executor():
    return _executor.get()


@contextmanager
def request_executor_scope(executor):
    token = _executor.set(executor)
    try:
        yield
    finally:
        _executor.reset(token)
