"""Native envd components with instance-scoped Arker ingress transports."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class EnvdDescriptor:
    endpoint: str
    envd_version: str
    envd_access_token: str = field(repr=False)


def _scope(endpoint: str, url: str) -> None:
    expected, actual = urlsplit(endpoint), urlsplit(url)
    if (
        expected.scheme not in {"https", "http"}
        or expected.username
        or expected.password
        or expected.query
        or expected.fragment
        or actual.scheme != expected.scheme
        or actual.username
        or actual.password
        or actual.hostname != expected.hostname
        or (actual.port or (443 if actual.scheme == "https" else 80))
        != (expected.port or (443 if expected.scheme == "https" else 80))
        or not actual.path.startswith(expected.path.rstrip("/") + "/")
    ):
        raise ValueError("Envd transport request is outside its configured endpoint")


def _paused(status: int, body: bytes) -> bool:
    if status != 409:
        return False
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    error = value.get("error", value) if isinstance(value, dict) else {}
    if not isinstance(error, dict):
        return False
    from ._compat_e2b_errors import is_explicit_resume

    return is_explicit_resume(status, error.get("code"), error.get("details"))


# Native envd recognizes this proxy response in REST, RPC, and health checks.
_ERROR_LIMIT = 64 * 1024


def _read_error(chunks):
    body = bytearray()
    for chunk in chunks:
        remaining = _ERROR_LIMIT - len(body)
        body.extend(chunk[:remaining])
        if len(chunk) > remaining:
            return bytes(body), False
    return bytes(body), True


async def _aread_error(chunks):
    body = bytearray()
    async for chunk in chunks:
        remaining = _ERROR_LIMIT - len(body)
        body.extend(chunk[:remaining])
        if len(chunk) > remaining:
            return bytes(body), False
    return bytes(body), True


_STOPPED = b'{"message":"Sandbox was not found: explicitly paused; call connect to resume"}'


@dataclass
class DataPlane:
    commands: Any
    files: Any
    pty: Any
    git: Any
    _clients: tuple[Any, ...] = field(repr=False)
    _pool: Any = field(repr=False)
    _state: dict[str, bool] = field(repr=False)
    _asynchronous: bool = field(repr=False)

    def close(self) -> None:
        if self._asynchronous:
            raise TypeError("Use aclose for an asynchronous data plane")
        if self._state["closed"]:
            return
        self._state["closed"] = True
        try:
            for client in self._clients:
                client.close()
        finally:
            self._pool.close()

    async def aclose(self) -> None:
        if not self._asynchronous:
            self.close()
            return
        if self._state["closed"]:
            return
        self._state["closed"] = True
        try:
            for client in self._clients:
                await client.aclose()
        finally:
            await self._pool.aclose()


class _AsyncPool:
    """Own pending dispatch and body reads, including responses not yet consumed."""

    def __init__(self, transport):
        self.transport = transport
        self.pending = set()
        self.responses = {}
        self.closed = False

    async def _wait(self, awaitable):
        task = asyncio.ensure_future(awaitable)
        self.pending.add(task)
        if self.closed:
            task.cancel()
        try:
            return await task
        finally:
            self.pending.discard(task)

    async def execute(self, request):
        from pyqwest import Response

        response = await self._wait(self.transport.execute(request))
        owner = self
        self.responses[id(response)] = response

        class Content:
            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return await owner._wait(anext(response.content))
                except BaseException:
                    await self.aclose()
                    raise

            async def aclose(self):
                owner.responses.pop(id(response), None)
                await response.aclose()

        return Response(
            status=response.status,
            http_version=response.http_version,
            headers=response.headers,
            content=Content(),
            trailers=response.trailers,
        )

    async def aclose(self):
        self.closed = True
        pending = tuple(self.pending)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        responses = tuple(self.responses.values())
        self.responses.clear()
        try:
            await asyncio.gather(*(response.aclose() for response in responses))
        finally:
            await self.transport.aclose()


def create_data_plane(descriptor: EnvdDescriptor, platform_key: str, *, asynchronous: bool = False) -> DataPlane:
    """Construct components only; never bootstrap, activate, or call vendor APIs.

    Async components must be constructed on their caller's event loop. Imports
    stay local so importing Arker does not require the optional E2B dependency.
    """
    import httpx
    from e2b import AsyncSandbox, Sandbox
    from e2b.connection_config import ConnectionConfig
    from e2b.envd.client_shared import ENVD_JSON_CODEC, ENVD_RPC_COMPRESSION
    from e2b.envd.interceptors import build_interceptors
    from packaging.version import Version
    from pyqwest import Headers

    _scope(descriptor.endpoint, descriptor.endpoint.rstrip("/") + "/health")
    config = ConnectionConfig(
        api_key="arker-data-plane-only",
        sandbox_url=descriptor.endpoint,
        extra_sandbox_headers={"X-Access-Token": descriptor.envd_access_token},
    )
    sandbox = (AsyncSandbox if asynchronous else Sandbox)(
        sandbox_id="arker-data-plane",
        sandbox_domain=None,
        envd_version=Version(descriptor.envd_version),
        envd_access_token=descriptor.envd_access_token,
        connection_config=config,
    )

    state = {"closed": False}

    def authorize(request):
        if state["closed"]:
            raise RuntimeError("Envd data plane is closed")
        _scope(descriptor.endpoint, str(request.url))
        request.headers["X-API-Key"] = platform_key

    class Rest(httpx.BaseTransport):
        def __init__(self, inner):
            self.inner = inner

        def handle_request(self, request):
            authorize(request)
            response = self.inner.handle_request(request)
            if response.status_code == 409:
                try:
                    body, complete = _read_error(response.iter_raw())
                finally:
                    response.close()
                paused = complete and _paused(409, body)
                headers = dict(response.headers)
                headers.pop("content-length", None)
                return httpx.Response(
                    502 if paused else 409,
                    content=_STOPPED if paused else body,
                    headers={"content-type": "application/json"} if paused else headers,
                )
            return response

    class AsyncRest(httpx.AsyncBaseTransport):
        def __init__(self, inner):
            self.inner = inner

        async def handle_async_request(self, request):
            authorize(request)
            response = await self.inner.handle_async_request(request)
            if response.status_code == 409:
                try:
                    body, complete = await _aread_error(response.aiter_raw())
                finally:
                    await response.aclose()
                paused = complete and _paused(409, body)
                headers = dict(response.headers)
                headers.pop("content-length", None)
                return httpx.Response(
                    502 if paused else 409,
                    content=_STOPPED if paused else body,
                    headers={"content-type": "application/json"} if paused else headers,
                )
            return response

    if asynchronous:
        from e2b.envd.client_async import PlainHTTPErrorTransport
        from pyqwest import Client, HTTPTransport, Response
        from pyqwest.httpx import AsyncPyqwestTransport as RestTransport

        class Rpc:
            async def execute(self, request):
                authorize(request)
                response = await pool.execute(request)
                if response.status != 409:
                    return response
                try:
                    body, complete = await _aread_error(response.content)
                finally:
                    await response.aclose()
                paused = complete and _paused(409, body)
                return Response(
                    status=502 if paused else 409,
                    headers=Headers({"content-type": "application/json"}) if paused else response.headers,
                    content=_STOPPED if paused else body,
                )
    else:
        from e2b.envd.client_sync import PlainHTTPErrorTransport
        from pyqwest import SyncClient as Client
        from pyqwest import SyncHTTPTransport as HTTPTransport
        from pyqwest import SyncResponse as Response
        from pyqwest.httpx import PyqwestTransport as RestTransport

        class Rpc:
            def execute_sync(self, request):
                authorize(request)
                response = pool.execute_sync(request)
                if response.status != 409:
                    return response
                try:
                    body, complete = _read_error(response.content)
                finally:
                    response.close()
                paused = complete and _paused(409, body)
                return Response(
                    status=502 if paused else 409,
                    headers=Headers({"content-type": "application/json"}) if paused else response.headers,
                    content=_STOPPED if paused else body,
                )

    pool = HTTPTransport(follow_redirects=False, tls_include_system_certs=True)
    if asynchronous:
        pool = _AsyncPool(pool)
    clients = {}
    for component in (sandbox.commands, sandbox.files, sandbox.pty):
        component._rpc = type(component._rpc)(
            descriptor.endpoint,
            codec=ENVD_JSON_CODEC,
            interceptors=build_interceptors(config, descriptor.endpoint),
            http_client=Client(PlainHTTPErrorTransport(Rpc())),
            **ENVD_RPC_COMPRESSION,
        )
        for name in ("_envd_api", "_envd_api_streaming", "_envd_health_api"):
            client = getattr(component, name, None)
            if client is not None and id(client) not in clients:
                clients[id(client)] = client
                client.follow_redirects = False
                client._transport = (AsyncRest if asynchronous else Rest)(RestTransport(pool))
    return DataPlane(
        sandbox.commands, sandbox.files, sandbox.pty, sandbox.git, tuple(clients.values()), pool, state, asynchronous
    )
