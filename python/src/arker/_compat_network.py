from __future__ import annotations

import asyncio
import hashlib
import re
import socket
import threading
from contextlib import contextmanager, suppress

import httpcore
import httpx
from websocket import WebSocketException, WebSocketTimeoutException


@contextmanager
def _http_errors():
    try:
        yield
    except (
        httpcore.TimeoutException,
        httpcore.NetworkError,
        httpcore.ProtocolError,
        httpcore.ProxyError,
        httpcore.UnsupportedProtocol,
    ) as error:
        kind = getattr(httpx, type(error).__name__, httpx.TransportError)
        raise kind(str(error)) from error


class _PortNetworkStream(httpcore.NetworkStream):
    def __init__(self, stream):
        self.stream = stream

    def read(self, max_bytes, timeout=None):
        self.stream.settimeout(timeout)
        try:
            return self.stream.recv(max_bytes)
        except (TimeoutError, WebSocketTimeoutException) as error:
            raise httpcore.ReadTimeout(str(error)) from error
        except (OSError, WebSocketException) as error:
            raise httpcore.ReadError(str(error)) from error

    def write(self, buffer, timeout=None):
        self.stream.settimeout(timeout)
        try:
            self.stream.sendall(buffer)
        except (TimeoutError, WebSocketTimeoutException) as error:
            raise httpcore.WriteTimeout(str(error)) from error
        except (OSError, WebSocketException) as error:
            raise httpcore.WriteError(str(error)) from error

    def close(self):
        # HTTP connection teardown must interrupt an outstanding read.
        self.abort()

    def abort(self):
        getattr(self.stream, "abort", self.stream.close)()

    def get_extra_info(self, info):
        # Native watch cancellation uses the httpcore socket shutdown hook.
        return self if info == "socket" else None

    def shutdown(self, how):
        if how != socket.SHUT_RDWR:
            raise ValueError("Private transport shutdown requires both directions")
        self.abort()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        raise httpcore.UnsupportedProtocol("Private sandbox fetch expects an HTTP service")


class _Backend(httpcore.NetworkBackend):
    def __init__(self, vm):
        self.vm = vm

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host != "localhost" or not 1 <= port <= 65535:
            raise httpcore.UnsupportedProtocol("Redirect outside the requested private sandbox service is unsupported")
        return _PortNetworkStream(self.vm.connect_port(port, timeout=timeout if timeout is not None else 300))


class _ResponseStream(httpx.SyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    def __iter__(self):
        with _http_errors():
            yield from self.stream

    def close(self):
        self.stream.close()


class _Transport(httpx.BaseTransport):
    def __init__(self, vm):
        self.origin = _origin(vm)
        self.pool = httpcore.ConnectionPool(network_backend=_Backend(vm))

    def handle_request(self, request):
        with _http_errors():
            response = self.pool.handle_request(_core_request(request, self.origin))
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_ResponseStream(response.stream),
            extensions=response.extensions,
        )

    def close(self):
        self.pool.close()


def _core_request(request, origin):
    if (request.url.scheme, request.url.host, request.url.port) != (origin.scheme, origin.host, origin.port):
        raise httpcore.UnsupportedProtocol("Redirect outside the requested private sandbox service is unsupported")
    target = re.fullmatch(rb"/port/([1-9][0-9]{0,4})(/.*)", request.url.raw_path)
    if target is None or int(target[1]) > 65535:
        raise httpcore.UnsupportedProtocol("Redirect outside the requested private sandbox service is unsupported")
    return httpcore.Request(
        method=request.method,
        url=httpcore.URL(
            scheme=b"http",
            host=b"localhost",
            port=int(target[1]),
            target=target[2],
        ),
        headers=request.headers.raw,
        content=request.stream,
        extensions=request.extensions,
    )


def _origin(vm):
    hostname = getattr(vm, "hostname", None)
    if not isinstance(hostname, str) or not hostname:
        label = hashlib.sha256(vm.id.encode()).hexdigest()[:32]
        hostname = f"{label}.private.arker.invalid"
    scheme = "http" if str(getattr(vm, "base_url", "")).startswith("http://") else "https"
    return httpx.URL(f"{scheme}://{hostname}")


def request_path(port, path):
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be an integer from 1 through 65535")
    normalized = path if path.startswith("/") else f"/{path}"
    return f"/port/{port}{normalized}"


def fetch(vm, port: int, path: str = "/", method: str = "GET", **kwargs):
    path = request_path(port, path)
    with httpx.Client(base_url=_origin(vm), transport=_Transport(vm), trust_env=False) as client:
        return client.request(method, path, **kwargs)


class _AsyncPortNetworkStream(httpcore.AsyncNetworkStream):
    def __init__(self, stream):
        self.sync = stream

    async def _call(self, method, *args):
        try:
            return await asyncio.to_thread(method, *args)
        except asyncio.CancelledError:
            self.sync.abort()
            raise

    async def read(self, max_bytes, timeout=None):
        return await self._call(self.sync.read, max_bytes, timeout)

    async def write(self, buffer, timeout=None):
        await self._call(self.sync.write, buffer, timeout)

    async def aclose(self):
        self.sync.abort()

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        raise httpcore.UnsupportedProtocol("Private sandbox fetch expects an HTTP service")


class _AsyncBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, vm):
        self.sync = _Backend(vm)

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        cancelled = threading.Event()

        def connect():
            stream = self.sync.connect_tcp(host, port, timeout)
            if cancelled.is_set():
                stream.abort()
            return stream

        pending = asyncio.create_task(asyncio.to_thread(connect))
        try:
            return _AsyncPortNetworkStream(await asyncio.shield(pending))
        except asyncio.CancelledError:
            cancelled.set()

            # A websocket handshake already in a worker thread cannot be cancelled.
            # Dispose its result even when cancellation precedes the upgrade.
            def dispose(task):
                with suppress(Exception, asyncio.CancelledError):
                    task.result().abort()

            pending.add_done_callback(dispose)
            raise


class _AsyncResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        with _http_errors():
            async for chunk in self.stream:
                yield chunk

    async def aclose(self):
        await self.stream.aclose()


class _AsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, vm):
        self.origin = _origin(vm)
        # Keep cookies/auth on the client, but never retain idle VM activity leases.
        self.pool = httpcore.AsyncConnectionPool(
            network_backend=_AsyncBackend(vm),
            max_connections=100,
            max_keepalive_connections=0,
        )

    async def handle_async_request(self, request):
        with _http_errors():
            response = await self.pool.handle_async_request(_core_request(request, self.origin))
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_AsyncResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self):
        await self.pool.aclose()


def async_client(vm):
    return httpx.AsyncClient(
        base_url=_origin(vm),
        transport=_AsyncTransport(vm),
        trust_env=False,
        timeout=httpx.Timeout(300, connect=10),
    )
