import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx2 as httpx
import pytest

from arker import computer
from arker._compat_daytona_async import _AsyncProcess
from arker._daytona_request import normalized_timeout, request_timeout
from arker._request_timeout import request_executor, request_wait, transport_timeout, without_request_timeout


@pytest.fixture
def drip_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "4")
            self.end_headers()
            try:
                for chunk in b"done":
                    self.wfile.write(bytes([chunk]))
                    self.wfile.flush()
                    time.sleep(0.4)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def process(url):
    @request_timeout
    def get_session(identifier, request_timeout=None):
        return computer._http("GET", url, {}, None)[1]

    return SimpleNamespace(get_session=get_session)


def test_sync_progressing_response_can_outlast_request_timeout(drip_server):
    started = time.monotonic()
    assert process(drip_server).get_session("session", request_timeout=1) == b"done"
    assert time.monotonic() - started >= 1


def test_async_response_has_native_total_timeout(drip_server):
    errors = pytest.importorskip("daytona.common.errors")
    expected = errors.DaytonaConnectionTimeoutError if asyncio.TimeoutError is TimeoutError else errors.DaytonaError

    async def run():
        started = time.monotonic()
        with pytest.raises(expected) as caught:
            await _AsyncProcess(process(drip_server)).get_session("session", request_timeout=1)
        assert type(caught.value) is expected
        assert time.monotonic() - started < 1.2
        assert request_executor() is None

    asyncio.run(run())


@pytest.mark.parametrize("value,expected", [(None, None), (0, None), (0.2, 1), (1.2, 1.2)])
def test_generated_api_timeout_normalization(value, expected):
    assert normalized_timeout(value) == expected


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_invalid_timeout_fails_before_io(value):
    DaytonaBadRequestError = pytest.importorskip("daytona.common.errors").DaytonaBadRequestError

    with pytest.raises(DaytonaBadRequestError):
        process("http://unreachable.invalid").get_session("s", request_timeout=value)


def test_timeout_does_not_become_guest_timeout_and_cleanup_is_independent():
    seen = []

    @request_timeout
    def operation(request_timeout=None):
        seen.append(transport_timeout(300).read)
        with without_request_timeout():
            assert transport_timeout(300) == 300
            assert request_executor() is None
        seen.append(transport_timeout(300).read)

    operation(request_timeout=0.2)
    assert seen == [1, 1]
    assert transport_timeout(300) == 300


@pytest.mark.parametrize("lost_start_ack", [False, True])
def test_accepted_session_is_not_killed_on_response_timeout(monkeypatch, lost_start_ack):
    DaytonaConnectionTimeoutError = pytest.importorskip("daytona.common.errors").DaytonaConnectionTimeoutError

    from arker import _daytona_sessions as sessions

    killed = []
    owner = sessions.SessionProcess()
    owner._vm = object()
    owner._session_source = lambda: "/helper.py"
    owner.get_session = lambda *args: (_ for _ in ()).throw(httpx.ReadTimeout("response stalled"))
    monkeypatch.setattr(sessions, "run_command", lambda *args: SimpleNamespace(stdout='["/home/daytona",1000]'))
    monkeypatch.setattr(
        sessions,
        "GuestProcesses",
        lambda vm: SimpleNamespace(
            exec=lambda options: (
                (_ for _ in ()).throw(httpx.ReadTimeout("lost start response"))
                if lost_start_ack
                else SimpleNamespace(name=options["name"])
            ),
            kill=killed.append,
            get=lambda *args: pytest.fail("must not retry a timed out request"),
        ),
    )
    with pytest.raises(DaytonaConnectionTimeoutError) as caught:
        owner.create_session("owned", request_timeout=1)
    assert caught.value.session_id == "owned"
    assert caught.value.process_id.startswith("daytona-session:")
    assert killed == []


def test_async_nested_process_calls_share_one_deadline(drip_server):
    errors = pytest.importorskip("daytona.common.errors")
    expected = errors.DaytonaConnectionTimeoutError if asyncio.TimeoutError is TimeoutError else errors.DaytonaError
    completed = []

    @request_timeout
    def get_session(identifier, request_timeout=None):
        for _ in range(2):
            completed.append(process(drip_server).get_session(identifier, request_timeout=request_timeout))

    async def run():
        started = time.monotonic()
        with pytest.raises(expected) as caught:
            await _AsyncProcess(SimpleNamespace(get_session=get_session)).get_session("s", request_timeout=2)
        assert type(caught.value) is expected
        assert 1.9 <= time.monotonic() - started < 2.3
        assert completed == [b"done"]
        assert request_executor() is None

    asyncio.run(run())


def test_async_cancellation_closes_inflight_http_before_draining_thread(drip_server):
    async def run():
        task = asyncio.create_task(_AsyncProcess(process(drip_server)).get_session("s", request_timeout=2))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
        assert request_executor() is None

    asyncio.run(run())


def test_cleanup_after_logical_deadline_uses_independent_transport(drip_server):
    errors = pytest.importorskip("daytona.common.errors")
    expected = errors.DaytonaConnectionTimeoutError if asyncio.TimeoutError is TimeoutError else errors.DaytonaError
    cleaned = []

    @request_timeout
    def get_session(identifier, request_timeout=None):
        try:
            request_wait(2)
        except httpx.TimeoutException:
            with without_request_timeout():
                assert request_executor() is None
                cleaned.append(computer._http("GET", drip_server, {}, None)[1])
            raise

    async def run():
        with pytest.raises(expected) as caught:
            await _AsyncProcess(SimpleNamespace(get_session=get_session)).get_session("s", request_timeout=1)
        assert type(caught.value) is expected
        assert cleaned == [b"done"]
        assert request_executor() is None

    asyncio.run(run())
