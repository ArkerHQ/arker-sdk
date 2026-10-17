from __future__ import annotations

import asyncio
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
        self.stream.close()

    def abort(self):
        getattr(self.stream, "abort", self.stream.close)()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        raise httpcore.UnsupportedProtocol("Private sandbox fetch expects an HTTP service")


class _Backend(httpcore.NetworkBackend):
    def __init__(self, vm, port):
        self.vm = vm
        self.port = port

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host != "localhost" or port != self.port:
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
    def __init__(self, vm, port):
        self.pool = httpcore.ConnectionPool(network_backend=_Backend(vm, port), max_connections=1)

    def handle_request(self, request):
        with _http_errors():
            response = self.pool.handle_request(_core_request(request))
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_ResponseStream(response.stream),
            extensions=response.extensions,
        )

    def close(self):
        self.pool.close()


def _core_request(request):
    return httpcore.Request(
        method=request.method,
        url=httpcore.URL(
            scheme=request.url.raw_scheme,
            host=request.url.raw_host,
            port=request.url.port,
            target=request.url.raw_path,
        ),
        headers=request.headers.raw,
        content=request.stream,
        extensions=request.extensions,
    )


def _url(port, path):
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be an integer from 1 through 65535")
    normalized = path if path.startswith("/") else f"/{path}"
    return f"http://localhost:{port}{normalized}"


def fetch(vm, port: int, path: str = "/", method: str = "GET", **kwargs):
    url = _url(port, path)
    with httpx.Client(transport=_Transport(vm, port), trust_env=False) as client:
        return client.request(method, url, **kwargs)


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
    def __init__(self, vm, port):
        self.sync = _Backend(vm, port)

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
    def __init__(self, vm, port):
        self.pool = httpcore.AsyncConnectionPool(network_backend=_AsyncBackend(vm, port), max_connections=1)

    async def handle_async_request(self, request):
        with _http_errors():
            response = await self.pool.handle_async_request(_core_request(request))
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_AsyncResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self):
        await self.pool.aclose()


async def afetch(vm, port: int, path: str = "/", method: str = "GET", **kwargs):
    url = _url(port, path)
    async with httpx.AsyncClient(transport=_AsyncTransport(vm, port), trust_env=False) as client:
        return await client.request(method, url, **kwargs)
