from __future__ import annotations

import asyncio
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from arker.blaxel import SandboxInstance, SyncSandboxInstance


@pytest.fixture(autouse=True)
def native_components(monkeypatch):
    # These tests exercise application HTTP transport independently of the daemon client.
    monkeypatch.setattr(
        "arker._compat_blaxel_data_plane.BlaxelDataPlane",
        lambda *args, **kwargs: SimpleNamespace(fs=object(), process=object()),
    )


@pytest.fixture
def service():
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            assert self.headers["Authorization"] == "Bearer application-only"
            assert self.headers["x-api-key"] == "application-key"
            self.send_response(207)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Application", "kept")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/cookies/"):
                body = self.headers.get("Cookie", "").encode()
                self.send_response(200)
                if self.path == "/cookies/set":
                    self.send_header("Set-Cookie", "compat-session=kept; Secure; HttpOnly")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path in ("/redirect", "/outside"):
                self.send_response(302)
                self.send_header("Location", "result" if self.path == "/redirect" else "http://example.com/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/slow":
                time.sleep(0.2)
                return
            self.send_response(418)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"3\r\neof\r\n3\r\n\x00\xff\x01\r\n0\r\n\r\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opened = []

    class VM:
        id = "test-vm"
        name = "test-vm"

        def connect_port(self, port, **kwargs):
            opened.append(port)
            return socket.create_connection(server.server_address, timeout=5)

    try:
        yield SyncSandboxInstance(vm=VM()), opened
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_private_fetch_preserves_binary_body_and_both_application_credentials(service, asynchronous):
    box, opened = service
    if asynchronous:
        box = SandboxInstance(box)
    body = bytes(range(256)) * 1024
    response = box.fetch(
        8080,
        "echo?x=1",
        method="POST",
        content=body,
        headers={
            "Authorization": "Bearer application-only",
            "x-api-key": "application-key",
        },
    )
    if asynchronous:
        response = asyncio.run(response)
    assert response.status_code == 207
    assert response.headers["x-application"] == "kept"
    assert response.content == body
    assert opened == [8080]


def test_network_fetch_preserves_chunked_binary_non_success_and_redirect_options(service):
    box, opened = service
    response = box.network.fetch(8081, "/redirect", follow_redirects=True)
    assert response.status_code == 418
    assert response.content == b"eof\x00\xff\x01"
    assert len(response.history) == 1
    assert opened == [8081]


def test_native_fetch_delegates_unchanged():
    calls = []

    class Native:
        def __init__(self):
            self.metadata = {"name": "native"}

        def fetch(self, *args, **kwargs):
            calls.append((args, kwargs))
            return "native-result"

    box = SyncSandboxInstance(native=Native())
    assert box.fetch(3000, "/test", headers={"a": "b"}) == "native-result"
    assert calls == [((3000, "/test"), {"method": "GET", "headers": {"a": "b"}})]


def test_private_fetch_does_not_follow_redirects_on_the_sdk_hosts_network(service):
    box, opened = service
    with pytest.raises(httpx.UnsupportedProtocol, match="outside"):
        box.fetch(8080, "/outside", follow_redirects=True)
    assert opened == [8080]


def test_private_fetch_preserves_read_timeout_type(service):
    box, _ = service
    with pytest.raises(httpx.ReadTimeout):
        box.fetch(8080, "/slow", timeout=0.02)


@pytest.mark.parametrize("port", [True, 0, -1, 65536, "8080"])
def test_invalid_port_is_rejected_before_opening_transport(service, port):
    box, opened = service
    with pytest.raises(ValueError):
        box.fetch(port)
    assert opened == []


def test_async_fetch_cancellation_closes_the_private_stream():
    started = threading.Event()
    closed = threading.Event()

    class Stream:
        def settimeout(self, timeout):
            pass

        def sendall(self, data):
            pass

        def recv(self, size):
            started.set()
            closed.wait(2)
            return b""

        def close(self):
            closed.set()

    class VM:
        id = "test"
        name = "test"

        def connect_port(self, *args, **kwargs):
            return Stream()

    async def run():
        box = SandboxInstance(SyncSandboxInstance(vm=VM()))
        task = asyncio.create_task(box.fetch(8080))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set(), "cancelled fetch must release its private port immediately"

    try:
        asyncio.run(run())
    finally:
        closed.set()


def test_async_cancellation_before_upgrade_disposes_the_late_connection():
    started = threading.Event()
    proceed = threading.Event()
    closed = threading.Event()

    class Stream:
        def close(self):
            closed.set()

    class VM:
        id = "test"
        name = "test"

        def connect_port(self, *args, **kwargs):
            started.set()
            assert proceed.wait(2)
            return Stream()

    async def run():
        box = SandboxInstance(SyncSandboxInstance(vm=VM()))
        task = asyncio.create_task(box.fetch(8080))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        proceed.set()
        assert await asyncio.to_thread(closed.wait, 1)

    try:
        asyncio.run(run())
    finally:
        proceed.set()


def test_async_cookie_state_is_persistent_but_default_cookie_paths_are_port_scoped(service):
    sync, _ = service

    async def run():
        box = SandboxInstance(sync)
        await box.fetch(8080, "/cookies/set")
        assert (await box.fetch(8080, "/cookies/check")).text == "compat-session=kept"
        assert (await box.fetch(8081, "/cookies/check")).text == ""
        assert box.network.get_client().timeout.connect == 10
        assert box.network.get_client().timeout.read == 300
        await box.network.aclose()

    asyncio.run(run())
