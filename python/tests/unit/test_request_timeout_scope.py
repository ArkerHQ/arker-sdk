import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx2 as httpx
import pytest

from arker import computer
from arker._request_timeout import request_timeout_scope, transport_timeout, without_request_timeout


def test_phase_scope_restores_nested_cleanup_and_copies_configuration():
    config = httpx.Timeout(connect=30, pool=60, read=0.2, write=0.3)
    with request_timeout_scope(config):
        config.read = 99
        assert transport_timeout(300).read == 0.2
        with request_timeout_scope(None):
            assert transport_timeout(300).as_dict() == dict.fromkeys(("connect", "pool", "read", "write"))
        with without_request_timeout():
            assert transport_timeout(300) == 300
        assert transport_timeout(300).read == 0.2
    assert transport_timeout(300) == 300


def test_http_and_presigned_transfers_receive_phase_configuration(monkeypatch):
    seen = []

    def handle(request):
        seen.append(request.extensions["timeout"])
        if request.url.path.endswith("/sync"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "op": "read",
                    "path": "/file",
                    "size": 5,
                    "presigned_url": "https://storage.invalid/file",
                    "expires_in": 900,
                    "method": "GET",
                },
            )
        return httpx.Response(200, content=b"hello")

    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        monkeypatch.setattr(computer, "_http_client", transport)
        client = computer.Arker(api_key="synthetic", base_url="https://api.invalid", retry=False)
        config = httpx.Timeout(connect=30, pool=60, read=0.2, write=0.2)
        with request_timeout_scope(config):
            assert b"".join(client.vm("vm")._sync_read_chunks("/file")) == b"hello"
            assert client._download("https://storage.invalid/file").content == b"hello"
            assert client._download("https://storage.invalid/file", request_timeout=5).content == b"hello"
    assert seen[:3] == [config.as_dict()] * 3
    assert seen[3] == dict.fromkeys(("connect", "read", "write", "pool"), 5)


def test_scoped_timeout_propagates_without_retry(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("no response")

    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        monkeypatch.setattr(computer, "_http_client", transport)
        client = computer.Arker(api_key="synthetic", base_url="https://api.invalid")
        with request_timeout_scope(0.1), pytest.raises(httpx.ReadTimeout):
            client._request("GET", "/metadata")
    assert len(calls) == 1


def test_scope_follows_async_thread_dispatch_without_cross_task_leakage():
    async def exercise():
        with request_timeout_scope(2):
            value = await asyncio.to_thread(transport_timeout, 300)
            assert value.read == 2
        assert transport_timeout(300) == 300

    asyncio.run(exercise())


def test_progressing_response_is_not_subject_to_total_wall_clock_deadline():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "3")
            self.end_headers()
            for value in (b"a", b"b", b"c"):
                self.wfile.write(value)
                self.wfile.flush()
                time.sleep(0.15)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with request_timeout_scope(httpx.Timeout(connect=30, pool=60, read=0.25, write=0.25)):
            start = time.monotonic()
            status, body = computer._http("GET", f"http://127.0.0.1:{server.server_port}/file", {}, None)
        assert status == 200 and body == b"abc"
        assert time.monotonic() - start >= 0.3
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
