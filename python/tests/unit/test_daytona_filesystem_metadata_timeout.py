import asyncio
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("daytona")

from daytona import DaytonaBadRequestError, DaytonaConnectionTimeoutError, DaytonaError

from arker._daytona_filesystem import AsyncFileSystem, FileSystem
from arker._request_timeout import request_executor, transport_timeout
from arker.computer import Arker

INFO = {
    "name": "file",
    "group": "1000",
    "owner": "1000",
    "size": 0,
    "isDir": False,
    "modTime": "now",
    "mode": "0644",
    "permissions": "0644",
}


@pytest.fixture
def endpoint():
    calls = []
    settings = {"delay": 0.4, "parts": 4, "stall": False}
    received = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def respond(self, arker):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0")))) if arker else None
            calls.append((self.command, self.path, body))
            received.set()
            payload = (
                {
                    "exit_code": 0,
                    "stdout": json.dumps(INFO),
                    "stderr": "",
                    "stdout_encoding": "utf-8",
                    "stderr_encoding": "utf-8",
                    "state": "completed",
                }
                if arker
                else INFO
            )
            if settings.get("poll"):
                payload = (
                    {"run_id": "run-test", "state": "running"}
                    if arker
                    else {
                        "run_id": "run-test",
                        "state": "completed",
                        "started_at": "now",
                        "exit_code": 0,
                        "stdout": json.dumps(INFO),
                        "stdout_encoding": "utf-8",
                        "stderr": "",
                        "stderr_encoding": "utf-8",
                    }
                )
            data = json.dumps(payload).encode()
            if settings["stall"]:
                release.wait(5)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                count = settings["parts"]
                for index in range(count):
                    self.wfile.write(data[len(data) * index // count : len(data) * (index + 1) // count])
                    self.wfile.flush()
                    if index + 1 < count:
                        time.sleep(settings["delay"])
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            self.respond(False)

        def do_POST(self):
            self.respond(True)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls, settings, received
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


@contextmanager
def sync_fs(url, native):
    if native:
        import httpx
        from daytona._sync.filesystem import FileSystem as NativeFileSystem
        from daytona_toolbox_api_client import ApiClient, Configuration, FileSystemApi

        with ApiClient(Configuration(host=url)) as client, httpx.Client() as http:
            yield NativeFileSystem(FileSystemApi(client), http)
    else:
        yield FileSystem(Arker(api_key="synthetic", base_url=url).vm("vm-test"))


async def async_info(url, native, seconds):
    if native:
        from daytona._async.filesystem import AsyncFileSystem as NativeFileSystem
        from daytona_toolbox_api_client_async import ApiClient, Configuration, FileSystemApi

        async with ApiClient(Configuration(host=url)) as client:
            return await NativeFileSystem(FileSystemApi(client)).get_file_info("file", request_timeout=seconds)
    return await AsyncFileSystem(Arker(api_key="synthetic", base_url=url).vm("vm-test")).get_file_info(
        "file", request_timeout=seconds
    )


@pytest.mark.parametrize("native", [True, False])
def test_sync_progress_can_outlast_response_timeout(endpoint, native):
    url, calls, _, _ = endpoint
    started = time.monotonic()
    with sync_fs(url, native) as fs:
        assert fs.get_file_info("file", request_timeout=1).name == "file"
    assert time.monotonic() - started >= 1
    assert len(calls) == 1
    if not native:
        assert "timeout" not in calls[0][2]
        assert "request_timeout" not in calls[0][2]
    assert request_executor() is None
    assert transport_timeout(300) == 300


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("stall", [False, True], ids=["body-progress", "before-headers"])
@pytest.mark.parametrize("operation", ["filesystem", "process"])
def test_async_total_response_timeout_matches_native(endpoint, native, stall, operation):
    url, calls, settings, _ = endpoint
    settings["stall"] = stall
    expected = DaytonaConnectionTimeoutError if asyncio.TimeoutError is TimeoutError else DaytonaError

    async def run():
        if operation == "filesystem":
            return await async_info(url, native, 1)
        if native:
            from daytona._async.process import AsyncProcess
            from daytona_toolbox_api_client_async import ApiClient, Configuration, ProcessApi

            async with ApiClient(Configuration(host=url)) as client:
                return await AsyncProcess("python", ProcessApi(client)).get_session("session", request_timeout=1)
        from types import SimpleNamespace

        from arker import computer
        from arker._compat_daytona_async import _AsyncProcess
        from arker._daytona_request import request_timeout

        @request_timeout
        def get_session(session_id, request_timeout=None):
            return computer._http("POST", url, {}, b"{}")

        return await _AsyncProcess(SimpleNamespace(get_session=get_session)).get_session("session", request_timeout=1)

    with pytest.raises(expected) as caught:
        asyncio.run(run())
    assert type(caught.value) is expected
    assert len(calls) == 1
    assert request_executor() is None


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("asynchronous", [True, False])
def test_zero_disables_response_timeout(endpoint, native, asynchronous):
    url, calls, _, _ = endpoint
    if asynchronous:
        assert asyncio.run(async_info(url, native, 0)).name == "file"
    else:
        with sync_fs(url, native) as fs:
            assert fs.get_file_info("file", request_timeout=0).name == "file"
    assert len(calls) == 1


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("asynchronous", [True, False])
def test_invalid_timeout_rejects_before_http(endpoint, native, asynchronous):
    url, calls, _, _ = endpoint
    with pytest.raises(DaytonaBadRequestError):
        if asynchronous:
            asyncio.run(async_info(url, native, -1))
        else:
            with sync_fs(url, native) as fs:
                fs.get_file_info("file", request_timeout=-1)
    assert calls == []


def test_async_cancellation_drains_http_worker(endpoint):
    url, calls, settings, received = endpoint
    settings["stall"] = True

    async def run():
        task = asyncio.create_task(async_info(url, False, 0))
        assert await asyncio.to_thread(received.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert request_executor() is None

    asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize("asynchronous", [True, False])
def test_each_http_response_has_its_own_budget(endpoint, asynchronous):
    url, calls, settings, _ = endpoint
    settings.update(poll=True, parts=2, delay=0.65)
    started = time.monotonic()
    if asynchronous:
        result = asyncio.run(async_info(url, False, 1))
    else:
        with sync_fs(url, False) as fs:
            result = fs.get_file_info("file", request_timeout=1)
    assert result.name == "file"
    assert time.monotonic() - started >= 1.3
    assert [method for method, _, _ in calls] == ["POST", "GET"]


@pytest.mark.parametrize("asynchronous", [True, False])
def test_mutation_timeout_does_not_replay_or_kill_guest(endpoint, asynchronous):
    url, calls, settings, _ = endpoint
    settings["stall"] = True
    vm = Arker(api_key="synthetic", base_url=url).vm("vm-test")
    expected = (
        DaytonaError if asynchronous and asyncio.TimeoutError is not TimeoutError else DaytonaConnectionTimeoutError
    )
    with pytest.raises(expected) as caught:
        if asynchronous:
            asyncio.run(AsyncFileSystem(vm).delete_file("file", request_timeout=0.1))
        else:
            FileSystem(vm).delete_file("file", request_timeout=0.1)
    assert type(caught.value) is expected
    assert len(calls) == 1
    method, path, body = calls[0]
    assert method == "POST" and path.endswith("/runs")
    assert "timeout" not in body and "signal" not in body


@pytest.mark.parametrize("lost_start_ack", [False, True])
def test_async_accepted_creation_total_timeout_keeps_identity_without_killing(endpoint, monkeypatch, lost_start_ack):
    from types import SimpleNamespace

    from arker import _daytona_sessions as sessions
    from arker import computer
    from arker._compat_daytona_async import _AsyncProcess
    from arker._daytona_request import is_response_timeout, request_timeout

    url, calls, settings, _ = endpoint
    settings["stall"] = True
    killed = []
    owner = sessions.SessionProcess()
    owner._vm = object()
    owner._session_source = lambda: "/helper.py"

    @request_timeout
    def get_session(identifier, request_timeout=None):
        return computer._http("POST", url, {}, b"{}")

    def start(options):
        if lost_start_ack:
            return computer._http("POST", url, {}, b"{}")
        return SimpleNamespace(name=options["name"])

    owner.get_session = get_session
    monkeypatch.setattr(sessions, "run_command", lambda *args: SimpleNamespace(stdout='["/home/daytona",1000]'))
    monkeypatch.setattr(
        sessions,
        "GuestProcesses",
        lambda vm: SimpleNamespace(
            exec=start,
            kill=killed.append,
            get=lambda *args: pytest.fail("deadline must not trigger reconciliation or replay"),
        ),
    )
    expected = DaytonaConnectionTimeoutError if asyncio.TimeoutError is TimeoutError else DaytonaError
    with pytest.raises(expected) as caught:
        asyncio.run(_AsyncProcess(owner).create_session("owned", request_timeout=1))
    assert type(caught.value) is expected
    assert is_response_timeout(caught.value)
    assert not is_response_timeout(DaytonaError("unrelated failure"))
    assert caught.value.session_id == "owned"
    assert caught.value.process_id.startswith("daytona-session:")
    assert len(calls) == 1
    assert killed == []
