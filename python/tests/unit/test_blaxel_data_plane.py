import asyncio

import pytest

from arker.blaxel import SyncSandboxInstance

httpx = pytest.importorskip("httpx")
pytest.importorskip("blaxel.core.sandbox")

adapter = pytest.importorskip("arker._compat_blaxel_data_plane")


class VM:
    id = "vm-test"
    hostname = "custom.example"
    base_url = "https://custom.example/api"


def test_native_headers_are_scoped_and_no_credentials_required(monkeypatch):
    from blaxel.core.sandbox.sync import filesystem as native
    from blaxel.core.sandbox.sync.filesystem import SyncSandboxFileSystem

    original = native.settings
    calls = []

    def reply(request):
        calls.append(request)
        return httpx.Response(200, content=b"bytes")

    monkeypatch.setattr(adapter, "_Transport", lambda vm: httpx.MockTransport(reply))
    first, second = adapter.BlaxelDataPlane(VM()), adapter.BlaxelDataPlane(VM())
    try:
        assert isinstance(first.fs, SyncSandboxFileSystem)
        assert first.fs.read_binary("/file") == b"bytes"
        first.close()
        assert second.fs.read_binary("/file") == b"bytes"
        assert native.settings is original
        assert all("authorization" not in request.headers for request in calls)
        assert all(request.url.host == "custom.example" for request in calls)
        with pytest.raises(RuntimeError, match="closed"):
            first.fs.read_binary("/file")
    finally:
        first.close()
        second.close()


def test_async_native_headers_and_error_identity(monkeypatch):
    from blaxel.core.sandbox.default.filesystem import SandboxFileSystem
    from blaxel.core.sandbox.types import ResponseError

    monkeypatch.setattr(
        adapter,
        "_AsyncTransport",
        lambda vm: httpx.MockTransport(lambda request: httpx.Response(404, json={"error": "missing"})),
    )

    async def run():
        plane = adapter.BlaxelDataPlane(VM(), asynchronous=True)
        try:
            assert isinstance(plane.fs, SandboxFileSystem)
            with pytest.raises(ResponseError) as error:
                await plane.fs.read_binary("/missing")
            assert type(error.value) is ResponseError
        finally:
            await plane.aclose()

    asyncio.run(run())


def test_changed_native_source_fails_closed(monkeypatch):
    monkeypatch.setattr(adapter, "_SOURCES", {**adapter._SOURCES, "sync.filesystem": "0" * 64})
    with pytest.raises(RuntimeError, match="implementation changed"):
        adapter.BlaxelDataPlane(VM())


def test_async_generated_client_keeps_private_factory_across_calls(monkeypatch):
    calls = []

    def reply(request):
        calls.append(request)
        return httpx.Response(200, json={"message": "ok"})

    monkeypatch.setattr(adapter, "_AsyncTransport", lambda vm: httpx.MockTransport(reply))

    async def run():
        plane = adapter.BlaxelDataPlane(VM(), asynchronous=True)
        try:
            await plane.process.write_stdin("123", "first")
            await plane.process.write_stdin("123", "second")
            assert [request.content for request in calls] == [b"first", b"second"]
            assert all(request.url.host == "custom.example" for request in calls)
        finally:
            await plane.aclose()

    asyncio.run(run())


def test_sync_facade_preserves_native_dictionary_requests(monkeypatch):
    from types import SimpleNamespace

    requests = []

    def reply(request):
        requests.append(request.content)
        return httpx.Response(
            200,
            json={
                "command": "printf native",
                "completedAt": "",
                "exitCode": 0,
                "logs": "",
                "name": "test",
                "pid": "123",
                "startedAt": "",
                "status": "running",
                "stdout": "",
                "stderr": "",
                "workingDir": "/tmp",
            },
        )

    monkeypatch.setattr(adapter, "_Transport", lambda vm: httpx.MockTransport(reply))
    plane = adapter.BlaxelDataPlane(VM())
    request = {"command": "printf native", "waitForCompletion": False, "workingDir": "/tmp"}
    try:
        expected = plane.process.exec(request)
        actual = SyncSandboxInstance(native=SimpleNamespace(process=plane.process)).process.exec(request)
        assert type(actual) is type(expected)
        assert actual.to_dict() == expected.to_dict()
        assert requests[0] == requests[1]
        assert request["waitForCompletion"] is False
    finally:
        plane.close()


@pytest.mark.parametrize("unsupported", ["0.0.0", "0.4.19", "0.4.20", "0.4.22"])
def test_native_version_guard(monkeypatch, unsupported):
    monkeypatch.setattr(adapter, "version", lambda name: unsupported)
    with pytest.raises(RuntimeError, match=r"0\.4\.21"):
        adapter.validate_native()


def test_owned_clients_survive_gc_until_closed_and_preserve_headers(monkeypatch):
    import gc
    import weakref

    calls = []
    monkeypatch.setattr(
        adapter,
        "_Transport",
        lambda vm: httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200)),
    )
    plane = adapter.BlaxelDataPlane(VM())
    client = plane.client(
        headers={"Range": "bytes=0-9", "Accept": "application/octet-stream", "x-blaxel-authorization": "test-only"}
    )
    client.get("/port/49983/files/test")
    assert calls[0].headers["range"] == "bytes=0-9"
    assert calls[0].headers["accept"] == "application/octet-stream"
    assert "x-blaxel-authorization" not in calls[0].headers
    reference = weakref.ref(client)
    del client
    gc.collect()
    assert reference() is not None
    assert len(plane.clients) == 1
    plane.close()
    assert not plane.clients


def test_async_clients_are_released_on_context_exit_and_disposal(monkeypatch):
    monkeypatch.setattr(adapter, "_AsyncTransport", lambda vm: httpx.MockTransport(lambda request: httpx.Response(200)))

    async def run():
        plane = adapter.BlaxelDataPlane(VM(), asynchronous=True)
        async with plane.async_client() as client:
            assert len(plane.async_clients) == 1
            await client.get("/port/49983/files/test")
        assert not plane.async_clients
        retained = plane.get_client()
        assert len(plane.async_clients) == 1
        await plane.aclose()
        assert retained.is_closed
        assert not plane.async_clients

    asyncio.run(run())


def test_async_native_wait_cancellation_leaves_process_observable(monkeypatch):
    response = {
        "command": "sleep",
        "completedAt": "",
        "exitCode": 0,
        "logs": "",
        "name": "test",
        "pid": "123",
        "startedAt": "",
        "status": "running",
        "stdout": "",
        "stderr": "",
        "workingDir": "/tmp",
    }
    requests = []

    def reply(request):
        requests.append(request.method)
        return httpx.Response(200, json=response)

    monkeypatch.setattr(adapter, "_AsyncTransport", lambda vm: httpx.MockTransport(reply))

    async def run():
        plane = adapter.BlaxelDataPlane(VM(), asynchronous=True)
        try:
            waiter = asyncio.create_task(plane.process.wait("123", max_wait=-1, interval=10))
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert (await plane.process.get("123")).status == "running"
            assert set(requests) == {"GET"}
        finally:
            await plane.aclose()
        assert not plane.async_clients

    asyncio.run(run())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_disposal_attempts_every_client_and_retains_failures(asynchronous):
    from types import SimpleNamespace

    plane = adapter.BlaxelDataPlane(VM(), asynchronous=asynchronous)
    attempts = []
    sync_error, async_error = RuntimeError("sync close"), RuntimeError("async close")

    def close(name, error=None):
        attempts.append(name)
        if error:
            raise error

    async def aclose(name, error=None):
        close(name, error)

    plane.clients = [
        SimpleNamespace(close=lambda: close("sync1", sync_error)),
        SimpleNamespace(close=lambda: close("sync2")),
    ]
    plane.async_clients = [
        SimpleNamespace(aclose=lambda: aclose("async1", async_error)),
        SimpleNamespace(aclose=lambda: aclose("async2")),
    ]
    with pytest.raises(adapter.BlaxelCleanupError) as error:
        asyncio.run(plane.aclose()) if asynchronous else plane.close()
    assert attempts == (["sync1", "sync2", "async1", "async2"] if asynchronous else ["sync1", "sync2"])
    assert error.value.errors == ((sync_error, async_error) if asynchronous else (sync_error,))
    assert plane.closed


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("method", ["read", "read_binary", "ls"])
def test_native_filesystem_paths_cannot_select_another_vm_port(monkeypatch, asynchronous, method):
    from arker._compat_network import _core_request, _origin

    requests = []

    def reply(request):
        core = _core_request(request, _origin(VM()))
        requests.append((core.url.port, core.url.target))
        if method == "ls":
            return httpx.Response(200, json={"name": "foo", "path": "/foo", "files": [], "subdirectories": []})
        return httpx.Response(200, json={"content": "daemon response"})

    monkeypatch.setattr(adapter, "_Transport", lambda vm: httpx.MockTransport(reply))
    monkeypatch.setattr(adapter, "_AsyncTransport", lambda vm: httpx.MockTransport(reply))
    plane = adapter.BlaxelDataPlane(VM(), asynchronous=asynchronous)
    path = "../../../port/8080/foo"

    async def run():
        try:
            await getattr(plane.fs, method)(path)
        finally:
            await plane.aclose()

    if asynchronous:
        asyncio.run(run())
    else:
        try:
            getattr(plane.fs, method)(path)
        finally:
            plane.close()
    assert requests
    assert all(port == 49983 for port, _ in requests)
    assert requests[0][1] != b"/foo"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "url",
    [
        "https://other.example/filesystem/foo",
        "http://custom.example/filesystem/foo",
        "https://custom.example:8080/filesystem/foo",
    ],
)
def test_daemon_transport_rejects_foreign_origin_before_opening_port(monkeypatch, asynchronous, url):
    def reply(request):
        pytest.fail("foreign origin reached private port transport")

    monkeypatch.setattr(adapter, "_Transport", lambda vm: httpx.MockTransport(reply))
    monkeypatch.setattr(adapter, "_AsyncTransport", lambda vm: httpx.MockTransport(reply))
    plane = adapter.BlaxelDataPlane(VM(), asynchronous=asynchronous)

    async def run():
        try:
            await plane.async_client().get(url)
        finally:
            await plane.aclose()

    with pytest.raises(httpx.UnsupportedProtocol):
        if asynchronous:
            asyncio.run(run())
        else:
            try:
                plane.client().get(url)
            finally:
                plane.close()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "path",
    [
        "/filesystem/../../../port/8080/foo?value=%2f",
        "/filesystem/%2e%2e/%2e%2e/port/8080/foo?value=%2f",
        "https://custom.example/port/8080/foo",
        "//other.example/filesystem/foo",
    ],
)
def test_daemon_raw_requests_keep_normalized_path_body_and_fixed_port(monkeypatch, asynchronous, path):
    from arker._compat_network import _core_request, _origin

    seen = []

    def reply(request):
        core = _core_request(request, _origin(VM()))
        seen.append((core.url.port, core.url.target, request.content, request.headers["Range"]))
        return httpx.Response(200)

    monkeypatch.setattr(adapter, "_Transport", lambda vm: httpx.MockTransport(reply))
    monkeypatch.setattr(adapter, "_AsyncTransport", lambda vm: httpx.MockTransport(reply))
    plane = adapter.BlaxelDataPlane(VM(), asynchronous=asynchronous)
    payload = b"\x00\xffpayload"
    with httpx.Client(base_url="https://custom.example") as reference:
        expected_path = reference.build_request("POST", path).url.raw_path

    async def run():
        try:
            await plane.async_client().post(path, content=payload, headers={"Range": "bytes=2-3"})
        finally:
            await plane.aclose()

    if asynchronous:
        asyncio.run(run())
    else:
        try:
            plane.client().post(path, content=payload, headers={"Range": "bytes=2-3"})
        finally:
            plane.close()
    assert seen == [(49983, expected_path, payload, "bytes=2-3")]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_daemon_redirects_cannot_escape_fixed_service(monkeypatch, asynchronous):
    from arker._compat_network import _core_request, _origin

    seen = []

    def reply(request):
        seen.append(_core_request(request, _origin(VM())).url.port)
        return httpx.Response(307, headers={"Location": "https://other.example/port/8080/foo"})

    monkeypatch.setattr(adapter, "_Transport", lambda vm: httpx.MockTransport(reply))
    monkeypatch.setattr(adapter, "_AsyncTransport", lambda vm: httpx.MockTransport(reply))
    plane = adapter.BlaxelDataPlane(VM(), asynchronous=asynchronous)

    async def run():
        try:
            client = plane.async_client()
            assert (await client.get("/redirect")).status_code == 307
            with pytest.raises(httpx.UnsupportedProtocol):
                await client.get("/redirect", follow_redirects=True)
        finally:
            await plane.aclose()

    if asynchronous:
        asyncio.run(run())
    else:
        try:
            client = plane.client()
            assert client.get("/redirect").status_code == 307
            with pytest.raises(httpx.UnsupportedProtocol):
                client.get("/redirect", follow_redirects=True)
        finally:
            plane.close()
    assert seen == [49983, 49983]


def test_async_filesystem_uses_native_persistent_client_timeouts(monkeypatch):
    from blaxel.core.sandbox.default.action import SandboxAction

    seen = []

    def reply(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, content=b"native")

    monkeypatch.setattr(adapter, "_AsyncTransport", lambda vm: httpx.MockTransport(reply))

    async def exercise():
        plane = adapter.BlaxelDataPlane(VM(), asynchronous=True)
        native = SandboxAction(plane.fs.sandbox_config).get_client()
        try:
            expected = native.build_request("GET", "/filesystem/file").extensions["timeout"]
            assert await plane.fs.read_binary("/file") == b"native"
            assert seen == [expected]
            assert plane.fs.get_client() is plane.process.get_client()
        finally:
            await native.aclose()
            await plane.aclose()

    asyncio.run(exercise())


def test_direct_async_and_sync_clients_keep_native_defaults():
    from blaxel.core.sandbox.sync.action import SyncSandboxAction

    async def exercise():
        plane = adapter.BlaxelDataPlane(VM(), asynchronous=True)
        native_direct = httpx.AsyncClient()
        native_sync = SyncSandboxAction(plane.fs.sandbox_config).get_client()
        try:
            direct = plane.async_client()
            assert direct.timeout == native_direct.timeout
            with plane.client() as sync:
                assert sync.timeout == native_sync.timeout
        finally:
            native_sync.close()
            await native_direct.aclose()
            await plane.aclose()

    asyncio.run(exercise())


def test_sync_watch_close_interrupts_private_stream():
    import threading

    class Wire:
        def __init__(self):
            self.release = threading.Event()
            self.reading = threading.Event()
            self.interrupted = threading.Event()
            self.parts = iter(
                [
                    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
                    b"c\r\n[keepalive]\n\r\n",
                ]
            )

        def settimeout(self, timeout):
            pass

        def sendall(self, data):
            pass

        def recv(self, size):
            part = next(self.parts, None)
            if part is not None:
                return part
            self.reading.set()
            assert self.release.wait(5)
            return b"0\r\n\r\n"

        def abort(self):
            self.interrupted.set()
            self.release.set()

        def close(self):
            self.release.set()

    wire = Wire()
    vm = VM()
    vm.connect_port = lambda port, timeout: wire
    plane = adapter.BlaxelDataPlane(vm)
    errors = []
    handle = plane.fs.watch("/tmp", lambda event: None, {"on_error": errors.append})
    try:
        assert wire.reading.wait(2)
        handle.close()
        assert wire.interrupted.wait(1), "watch close did not interrupt the private stream"
    finally:
        wire.release.set()
        handle.close()
        plane.close()
    assert not errors


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("recursive", [False, True])
def test_native_read_tree_uses_private_transport(monkeypatch, asynchronous, recursive):
    from blaxel.core.sandbox.client.models import Directory

    requests = []

    def reply(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "name": "tmp",
                "path": "/tmp",
                "files": [],
                "subdirectories": [],
                "recursive": recursive,
            },
        )

    monkeypatch.setattr(
        adapter, "_AsyncTransport" if asynchronous else "_Transport", lambda vm: httpx.MockTransport(reply)
    )
    plane = adapter.BlaxelDataPlane(VM(), asynchronous=asynchronous)

    async def read_async():
        try:
            return await plane.fs.read_tree("/tmp", max_files=0, exclude_hidden=False)
        finally:
            await plane.aclose()

    def read():
        if asynchronous:
            return asyncio.run(read_async())
        try:
            return plane.fs.read_tree("/tmp", max_files=0, exclude_hidden=False)
        finally:
            plane.close()

    if recursive:
        result = read()
        assert isinstance(result, Directory)
        assert result.recursive is True
    else:
        with pytest.raises(RuntimeError, match="recursive tree reads"):
            read()
    assert len(requests) == 1
    request = requests[0]
    assert request.url.host == "custom.example"
    assert request.url.path.startswith("/port/49983/filesystem/tree/")
    assert dict(request.url.params) == {
        "recursive": "true",
        "content": "true",
        "maxFiles": "0",
        "excludeHidden": "false",
    }
