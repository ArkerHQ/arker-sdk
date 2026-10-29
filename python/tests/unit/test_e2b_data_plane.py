"""Exercise native components against local HTTP responses, without guest activation."""

import asyncio
import inspect
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from arker._compat_e2b_data_plane import EnvdDescriptor, _paused, create_data_plane


@pytest.fixture
def servers():
    received = []
    redirected = []
    behavior = {"status": 409, "requirement": "explicit_resume"}

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            redirected.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *args):
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)

    class Handler(Target):
        def do_GET(self):
            received.append((self.path, dict(self.headers)))
            body = json.dumps(
                {
                    "error": {
                        "code": "invalid_state",
                        "details": {"resource": {"kind": "vm", "id": "vm"}, "requirement": behavior["requirement"]},
                    }
                }
            ).encode()
            self.send_response(behavior["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/stolen")
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (server, target)]
    for thread in threads:
        thread.start()
    yield f"http://127.0.0.1:{server.server_port}", behavior, received, redirected
    for instance in (server, target):
        instance.shutdown()
        instance.server_close()
    for thread in threads:
        thread.join()


def call(data, operation):
    if operation == "read":
        return data.files.read("/tmp/file")
    if operation == "stat":
        return data.files.get_info("/tmp/file")
    if operation == "command":
        return data.commands.run("true")
    return data.pty.create(
        size=SimpleNamespace(rows=24, cols=80),
        **({"on_data": lambda data: None} if inspect.iscoroutinefunction(data.pty.create) else {}),
    )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("operation", ["read", "stat", "command", "pty"])
def test_native_components_translate_only_verified_paused_response(servers, asynchronous, operation):
    from e2b import SandboxNotRunningException

    endpoint, _behavior, received, redirected = servers
    descriptor = EnvdDescriptor(endpoint, "0.10.2", "guest-test-token")

    async def run():
        data = create_data_plane(descriptor, "platform-test-key", asynchronous=True)
        try:
            with pytest.raises(SandboxNotRunningException):
                await call(data, operation)
        finally:
            await data.aclose()

    if asynchronous:
        asyncio.run(run())
    else:
        data = create_data_plane(descriptor, "platform-test-key")
        try:
            with pytest.raises(SandboxNotRunningException):
                call(data, operation)
        finally:
            data.close()
    assert len(received) == 1
    assert {k.lower(): v for k, v in received[0][1].items()}["x-api-key"] == "platform-test-key"
    assert not redirected


@pytest.mark.parametrize("status", [302, 307, 308, 404])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("operation", ["read", "stat", "command", "pty"])
def test_redirect_never_sends_credentials_to_target(servers, status, asynchronous, operation):
    from e2b import SandboxNotRunningException

    endpoint, behavior, received, redirected = servers
    behavior["status"] = status
    descriptor = EnvdDescriptor(endpoint, "0.10.2", "guest-test-token")

    async def run():
        data = create_data_plane(descriptor, "platform-test-key", asynchronous=True)
        try:
            await call(data, operation)
        except Exception as error:
            assert not isinstance(error, SandboxNotRunningException)
        finally:
            await data.aclose()

    if asynchronous:
        asyncio.run(run())
    else:
        data = create_data_plane(descriptor, "platform-test-key")
        try:
            call(data, operation)
        except Exception as error:
            assert not isinstance(error, SandboxNotRunningException)
        finally:
            data.close()
    assert received
    assert not redirected


def test_paused_match_is_strict_and_descriptor_hides_token():
    assert "secret" not in repr(EnvdDescriptor("https://example.test", "0.10.2", "secret"))
    for body in [b"null", b"[]", b'{"code":"invalid_state"}', b"invalid"]:
        assert not _paused(409, body)


def test_async_rpc_task_cancellation_is_preserved_without_health_probe():
    started = threading.Event()
    release = threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.path)
            started.set()
            release.wait(3)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def run():
        descriptor = EnvdDescriptor(f"http://127.0.0.1:{server.server_port}", "0.10.2", "guest")
        data = create_data_plane(descriptor, "platform", asynchronous=True)
        task = asyncio.create_task(data.commands.run("sleep 60"))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(requests) == 1
        await data.aclose()

    try:
        asyncio.run(run())
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_close_is_idempotent_and_does_not_close_other_sandbox_pool(servers, asynchronous):
    from e2b import SandboxNotRunningException

    endpoint, _, received, _ = servers
    descriptor = EnvdDescriptor(endpoint, "0.10.2", "guest")

    async def run():
        first = create_data_plane(descriptor, "platform-test-key", asynchronous=True)
        second = create_data_plane(descriptor, "platform-test-key", asynchronous=True)
        try:
            await first.aclose()
            await first.aclose()
            with pytest.raises(Exception, match="closed"):
                await first.files.read("/tmp/file")
            with pytest.raises(SandboxNotRunningException):
                await second.files.read("/tmp/file")
        finally:
            await first.aclose()
            await second.aclose()

    if asynchronous:
        asyncio.run(run())
    else:
        first = create_data_plane(descriptor, "platform-test-key")
        second = create_data_plane(descriptor, "platform-test-key")
        try:
            first.close()
            first.close()
            with pytest.raises(Exception, match="closed"):
                first.files.read("/tmp/file")
            with pytest.raises(SandboxNotRunningException):
                second.files.read("/tmp/file")
        finally:
            first.close()
            second.close()
    assert len(received) == 1


def test_error_reader_bounds_sync_and_async_bodies():
    from arker._compat_e2b_data_plane import _aread_error, _read_error

    pulls = []

    def chunks():
        for index in range(10):
            pulls.append(index)
            yield b"x" * 32768

    body, complete = _read_error(chunks())
    assert len(body) == 65536 and complete is False and len(pulls) == 3

    async def achunks():
        for chunk in chunks():
            yield chunk

    pulls.clear()
    body, complete = asyncio.run(_aread_error(achunks()))
    assert len(body) == 65536 and complete is False and len(pulls) == 3


def test_endpoint_scope_normalizes_dns_case_and_default_port():
    from arker._compat_e2b_data_plane import _scope

    _scope("https://VM.example.test", "https://vm.example.test:443/files")
    with pytest.raises(ValueError):
        _scope("https://VM.example.test", "https://vm.example.test:444/files")
    with pytest.raises(ValueError):
        _scope("https://VM.example.test", "https://attacker@vm.example.test/files")


def test_async_close_cancels_pending_dispatch_and_body_reads():
    from pyqwest import Response

    from arker._compat_e2b_data_plane import _AsyncPool

    async def check(body):
        entered = asyncio.Event()
        closed = []

        async def chunks():
            entered.set()
            await asyncio.Event().wait()
            yield b"never"

        async def execute(request):
            if not body:
                entered.set()
                await asyncio.Event().wait()
            return Response(status=200, content=chunks())

        async def close():
            closed.append(True)

        pool = _AsyncPool(SimpleNamespace(execute=execute, aclose=close))
        if body:
            response = await pool.execute(None)
            pending = asyncio.create_task(anext(response.content))
        else:
            pending = asyncio.create_task(pool.execute(None))
        await entered.wait()
        await pool.aclose()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not pool.pending and not pool.responses
        assert closed == [True]

    asyncio.run(check(False))
    asyncio.run(check(True))
