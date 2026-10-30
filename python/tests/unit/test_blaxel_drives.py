import asyncio
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("blaxel.core.sandbox")
from blaxel.core.sandbox import SandboxDrive, SyncSandboxDrive
from blaxel.core.sandbox.client.models import DriveMountInfo, DriveMountResponse, DriveUnmountResponse

from arker._compat_blaxel_drives import AsyncDrives, SyncDrives
from arker.computer import ArkerError


class Backend:
    def __init__(self):
        self.calls = []
        self.files = [{"filesystem_id": "dir-test", "name": "shared", "provider": "aws", "region": "region-one"}]
        self.rows = []
        self.base_url = "https://regional.custom.example/api"
        self._explicit_base_url = True
        self.vm = SimpleNamespace(
            id="vm-test",
            provider="aws",
            region="region-one",
            _client=self,
            base_url="https://control.custom.example/api",
        )
        self.fail_post = None
        self.after_post = None

    def _request(self, method, path, body=None, **options):
        self.calls.append((method, path, body, options))
        url = urlsplit(path)
        if url.path.startswith("/v1/filesystems"):
            assert options["base_url"] == self.base_url
            assert method == "GET"
            if url.path == "/v1/filesystems":
                return {"filesystems": self.files}
            return next(file for file in self.files if url.path.endswith(file["filesystem_id"]))
        assert options["base_url"] == self.vm.base_url
        if method == "GET":
            return {"mounts": self.rows.copy()}
        assert options["retry_network_failures"] is False
        if method == "POST":
            if self.fail_post:
                raise self.fail_post
            self.rows.append({"mount_id": "mount-test", **body, "state": "mounted"})
            if self.after_post:
                raise self.after_post
            return self.rows[-1]
        assert method == "DELETE"
        self.rows.clear()
        return {"deleted": True}


def perform(asynchronous, backend, method, *args):
    if asynchronous:
        return asyncio.run(getattr(AsyncDrives(backend.vm, None), method)(*args))
    return getattr(SyncDrives(backend.vm, None), method)(*args)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_mount_list_reconnect_and_unmount_return_native_models(asynchronous):
    backend = Backend()
    result = perform(asynchronous, backend, "mount", "shared", "data", "/nested")
    assert isinstance(result, DriveMountResponse)
    assert result.success is True
    assert result.mount_path == "/data"
    assert result.drive_path == "/nested"
    listing = perform(asynchronous, backend, "list")
    assert len(listing) == 1
    assert isinstance(listing[0], DriveMountInfo)
    assert listing[0].drive_name == "shared"
    removed = perform(asynchronous, backend, "unmount", "data")
    assert isinstance(removed, DriveUnmountResponse)
    assert removed.success is True
    assert backend.rows == []


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("read_only,path", [(True, "/data"), (False, "/"), (False, "/data/../other")])
def test_unsupported_options_fail_before_requests(asynchronous, read_only, path):
    backend = Backend()
    with pytest.raises(ValueError):
        perform(asynchronous, backend, "mount", "shared", path, "/", read_only)
    assert backend.calls == []


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("lost", [ConnectionError("response lost"), ArkerError("network_error", "response lost", 0)])
def test_lost_response_is_reconciled_without_replay(asynchronous, lost):
    backend = Backend()
    backend.after_post = lost
    assert perform(asynchronous, backend, "mount", "shared", "/data").success is True
    assert sum(call[0] == "POST" for call in backend.calls) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
def test_failed_mount_never_deletes_borrowed_sandbox_or_drive(asynchronous):
    backend = Backend()
    error = ArkerError("invalid_state", "mount refused", 409)
    backend.fail_post = error
    with pytest.raises(Exception, match="Mount drive failed") as caught:
        perform(asynchronous, backend, "mount", "shared", "/data")
    assert caught.value.__cause__ is error
    assert sum(call[0] == "POST" for call in backend.calls) == 1
    assert not any(call[0] == "DELETE" for call in backend.calls)


@pytest.mark.parametrize("native,adapter", [(SandboxDrive, AsyncDrives), (SyncSandboxDrive, SyncDrives)])
def test_native_class_and_parameter_signatures(native, adapter):
    assert issubclass(adapter, native)
    for name in ("mount", "list", "unmount"):
        actual = inspect.signature(getattr(adapter, name))
        expected = inspect.signature(getattr(native, name))
        assert [(p.name, p.default, p.kind) for p in actual.parameters.values()] == [
            (p.name, p.default, p.kind) for p in expected.parameters.values()
        ]
        assert inspect.iscoroutinefunction(getattr(adapter, name)) == inspect.iscoroutinefunction(getattr(native, name))


def test_simultaneous_mounts_reconcile_same_binding():
    backend = Backend()
    request = backend._request
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    reads = 0

    def concurrent(method, path, body=None, **options):
        nonlocal reads
        if method == "GET" and path.endswith("/mounts"):
            with lock:
                reads += 1
                initial = reads <= 2
            if initial:
                barrier.wait(timeout=5)
                return {"mounts": []}
        if method == "POST":
            with lock:
                if backend.rows:
                    raise ArkerError("conflict", "exists", 409)
                return request(method, path, body, **options)
        return request(method, path, body, **options)

    backend._request = concurrent
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(perform, False, backend, "mount", "shared", "/data") for _ in range(2)]
        assert all(future.result(timeout=5).success for future in futures)
    assert len(backend.rows) == 1


def test_paginated_lookup_uses_exact_name():
    backend = Backend()
    request = backend._request
    cursors = []

    def paged(method, path, body=None, **options):
        parsed = urlsplit(path)
        if parsed.path == "/v1/filesystems":
            cursor = parse_qs(parsed.query).get("cursor")
            cursors.append(cursor)
            if cursor:
                return {"filesystems": backend.files}
            return {"filesystems": [{**backend.files[0], "name": "shared-extra"}], "next_cursor": "next"}
        return request(method, path, body, **options)

    backend._request = paged
    assert perform(False, backend, "mount", "shared", "/data").success is True
    assert cursors == [None, ["next"]]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_facade_exposes_native_drive_class_and_delegates_native_unchanged(monkeypatch, asynchronous):
    from arker import _compat_blaxel_bootstrap
    from arker.blaxel import SandboxInstance, SyncSandboxInstance

    backend = Backend()
    backend.vm.hostname = "custom.example"
    backend.vm.name = "test"
    backend.vm.metadata = {}
    backend.vm.created_at = None
    monkeypatch.setattr(_compat_blaxel_bootstrap, "bootstrap_blaxel", lambda *_: None)
    sync = SyncSandboxInstance(vm=backend.vm)
    if asynchronous:

        async def exercise():
            box = SandboxInstance(sync)
            try:
                assert isinstance(box.drives, SandboxDrive)
                assert (await box.drives.mount("shared", "/data")).success is True
            finally:
                await box._data_plane.aclose()

        asyncio.run(exercise())
    else:
        try:
            assert isinstance(sync.drives, SyncSandboxDrive)
            assert sync.drives.mount("shared", "/data").success is True
        finally:
            sync._data_plane.close()
    sentinel = object()
    native = SimpleNamespace(
        id="native", metadata={"name": "native"}, process=None, fs=None, network=None, drives=sentinel
    )
    wrapped = SyncSandboxInstance(native=native)
    assert wrapped.drives is sentinel
    assert SandboxInstance(wrapped, native=native).drives is sentinel


def test_async_cancellation_prevents_post_after_lookup_returns():
    backend = Backend()
    request = backend._request
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocked(method, path, body=None, **options):
        if path.startswith("/v1/filesystems?"):
            entered.set()
            assert release.wait(timeout=5)
            try:
                return request(method, path, body, **options)
            finally:
                finished.set()
        return request(method, path, body, **options)

    backend._request = blocked

    async def exercise():
        task = asyncio.create_task(AsyncDrives(backend.vm, None).mount("shared", "/data"))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 5)

    asyncio.run(exercise())
    assert not any(call[0] != "GET" for call in backend.calls)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_conflicting_binding_is_not_overwritten(asynchronous):
    backend = Backend()
    backend.rows = [{"mount_id": "mount-other", "filesystem_id": "dir-other", "path": "/data", "state": "mounted"}]
    with pytest.raises(Exception, match="another binding"):
        perform(asynchronous, backend, "mount", "shared", "/data")
    assert not any(call[0] != "GET" for call in backend.calls)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_failed_readiness_preserves_mount_and_filesystem(asynchronous):
    backend = Backend()
    backend.rows = [
        {
            "mount_id": "mount-test",
            "filesystem_id": "dir-test",
            "path": "/data",
            "state": "failed",
            "state_detail": "attachment failed",
        }
    ]
    with pytest.raises(Exception, match="attachment failed"):
        perform(asynchronous, backend, "mount", "shared", "/data")
    assert not any(call[0] != "GET" for call in backend.calls)


def test_repeated_mount_cursor_fails_without_mutation():
    backend = Backend()
    request = backend._request

    def repeated(method, path, body=None, **options):
        if "/mounts" in path:
            return {"mounts": [], "next_cursor": "same"}
        return request(method, path, body, **options)

    backend._request = repeated
    with pytest.raises(RuntimeError, match="cursor repeated"):
        perform(False, backend, "mount", "shared", "/data")
    assert not any(call[0] != "GET" for call in backend.calls)


def test_readiness_waits_for_actual_state(monkeypatch):
    backend = Backend()
    backend.rows = [{"mount_id": "mount-test", "filesystem_id": "dir-test", "path": "/data", "state": "attaching"}]
    waits = []

    def mounted(_event, timeout=None):
        waits.append(timeout)
        backend.rows[0]["state"] = "mounted"

    monkeypatch.setattr(threading.Event, "wait", mounted)
    assert perform(False, backend, "mount", "shared", "/data").success is True
    assert waits == [0.1]


def test_failed_unmount_does_not_report_success():
    backend = Backend()
    perform(False, backend, "mount", "shared", "/data")
    request = backend._request

    def not_deleted(method, path, body=None, **options):
        if method == "DELETE":
            return {"deleted": False}
        return request(method, path, body, **options)

    backend._request = not_deleted
    with pytest.raises(Exception, match="not confirmed"):
        perform(False, backend, "unmount", "/data")
    assert len(backend.rows) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
def test_pinned_native_models_and_mount_path_normalization(monkeypatch, asynchronous):
    import importlib

    import httpx
    from blaxel.core.sandbox.client.client import Client

    module = importlib.import_module("blaxel.core.sandbox." + ("default" if asynchronous else "sync") + ".drive")
    calls = []

    def reply(request):
        import json

        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "driveName": "shared",
                "mountPath": "/data",
                "drivePath": "/nested",
                "readOnly": False,
                "success": True,
                "message": "Drive mounted successfully",
            },
        )

    def client(**kwargs):
        return Client(**kwargs, httpx_args={"transport": httpx.MockTransport(reply), "trust_env": False})

    monkeypatch.setattr(module, "Client", client)
    monkeypatch.setattr(module, "settings", SimpleNamespace(headers={}))
    config = SimpleNamespace(force_url="https://custom.example", headers={})
    native = (SandboxDrive if asynchronous else SyncSandboxDrive)(config)
    value = native.mount("shared", "data", "/nested")
    expected = asyncio.run(value) if asynchronous else value
    actual = perform(asynchronous, Backend(), "mount", "shared", "data", "/nested")
    assert type(actual) is type(expected)
    assert actual.to_dict() == expected.to_dict()
    assert calls == [{"driveName": "shared", "mountPath": "/data", "drivePath": "/nested", "readOnly": False}]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("readback", ["matching", "different", "missing"])
def test_real_core_lost_ack_reconciles_only_exact_binding(monkeypatch, asynchronous, readback):
    import json

    from arker import computer as core

    backend = Backend()
    request = backend._request
    posts, failures = [], []

    def lost_ack(method, url, headers, data, **kwargs):
        assert method == "POST"
        body = json.loads(data)
        posts.append(body)
        if readback != "missing":
            row = {"mount_id": "mount-test", **body, "state": "mounted"}
            if readback == "different":
                row["filesystem_id"] = "dir-other"
            backend.rows.append(row)
        raise core.httpx.ReadError("synthetic lost acknowledgement")

    def through_core(method, path, body=None, **options):
        if method == "POST":
            try:
                return core._request_json(
                    method,
                    path,
                    body,
                    base_url=backend.base_url,
                    retry=core.RetryOptions(),
                    retry_network_failures=False,
                )
            except ArkerError as error:
                failures.append(error)
                raise
        return request(method, path, body, **options)

    monkeypatch.setattr(core, "_http", lost_ack)
    backend._request = through_core
    if readback == "matching":
        assert perform(asynchronous, backend, "mount", "shared", "/data").success is True
    else:
        with pytest.raises(Exception, match="Mount drive failed") as caught:
            perform(asynchronous, backend, "mount", "shared", "/data")
        assert caught.value.__cause__ is failures[0]
    assert len(posts) == 1
    assert len(failures) == 1
    assert failures[0].status == 0
    assert not any(call[0] == "DELETE" for call in backend.calls)
