"""Scripted HTTP transport shared by SDK and compatibility tests."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import httpx2 as httpx

import arker.computer as sdk


class FakeTransport:
    """Scripts httpx responses by (method, url); drive it with ``use_transport``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.script: list[tuple[Any, int | None, bytes | httpx.RequestError]] = []

    def add_json(self, predicate, status: int, body: dict[str, Any]) -> None:
        self.script.append((predicate, status, json.dumps(body).encode()))

    def add_raw(self, predicate, status: int, body: bytes) -> None:
        self.script.append((predicate, status, body))

    def add_network_error(self, predicate, message: str = "response lost") -> None:
        self.script.append((predicate, None, httpx.ReadError(message)))

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        url = str(request.url)
        self.calls.append(
            {
                "method": method,
                "url": url,
                "body": request.content or None,
                "headers": request.headers,
            }
        )

        for index, (predicate, status, payload) in enumerate(self.script):
            if predicate(method, url):
                self.script.pop(index)
                if isinstance(payload, httpx.RequestError):
                    raise payload
                assert status is not None
                return httpx.Response(status, content=payload)

        raise AssertionError(f"no scripted response for {method} {url}")


@contextmanager
def use_transport(t: FakeTransport):
    """Route the SDK's shared client through ``t`` for the duration."""
    previous = sdk._http_client
    sdk._http_client = httpx.Client(transport=httpx.MockTransport(t.handler))
    try:
        yield
    finally:
        sdk._http_client.close()
        sdk._http_client = previous
