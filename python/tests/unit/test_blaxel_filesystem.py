"""Native filesystem request and result contracts through the scoped transport."""

import json
from types import SimpleNamespace

import pytest

from arker.blaxel import SyncSandboxInstance

httpx = pytest.importorskip("httpx")
pytest.importorskip("blaxel.core.sandbox")

adapter = pytest.importorskip("arker._compat_blaxel_data_plane")


@pytest.fixture
def filesystem(monkeypatch):
    calls = []
    response = {"message": "success", "path": "/work"}

    def reply(request):
        calls.append(request)
        return httpx.Response(200, json=response)

    monkeypatch.setattr(adapter, "_Transport", lambda vm: httpx.MockTransport(reply))
    plane = adapter.BlaxelDataPlane(
        SimpleNamespace(id="test", hostname="custom.example", base_url="https://custom.example/api")
    )
    box = SyncSandboxInstance(vm=SimpleNamespace(id="test", name="test"))
    box._data_plane = plane
    try:
        yield box.fs, calls, response
    finally:
        plane.close()


@pytest.mark.parametrize(
    "path, encoded",
    [
        ("bare", "bare"),
        ("nested/file", "nested/file"),
        ("/work///file", "/work///file"),
        ("~/file", "~/file"),
        ("~root/file", "~root/file"),
        ("/work/../file", "/file"),
    ],
)
def test_native_paths_are_preserved_at_transport_boundary(filesystem, path, encoded):
    fs, calls, response = filesystem
    response.clear()
    response.update(content="value")
    assert fs.read(path) == "value"
    assert calls[0].url.path == "/port/49983/filesystem/" + encoded


def test_native_permissions_and_removal_options(filesystem):
    fs, calls, _ = filesystem
    fs.mkdir("private", "0700")
    assert json.loads(calls[-1].content) == {"isDirectory": True, "permissions": "0700"}
    fs.rm("private")
    assert "recursive" not in calls[-1].url.params
    fs.rm("private", recursive=True)
    assert calls[-1].url.params["recursive"] == "true"


def test_native_directory_preserves_metadata_and_control_characters(filesystem):
    fs, _, response = filesystem
    entry = {
        "name": "line\nbreak",
        "path": "/work/line\nbreak",
        "size": 42,
        "permissions": "0600",
        "owner": "root",
        "group": "root",
        "lastModified": "2026-01-01T00:00:00Z",
    }
    response.clear()
    response.update(name="work", path="/work", files=[entry], subdirectories=[])
    result = fs.ls("/work")
    assert result.files[0].to_dict() == entry
    assert result.path == "/work"


def test_native_async_grep_preserves_options_and_result_model(monkeypatch):
    import asyncio

    calls = []
    response = {
        "query": "needle",
        "total": 1,
        "matches": [
            {
                "path": "line\nbreak:λ.txt",
                "line": 2,
                "column": 3,
                "text": "A Needle here",
                "context": "before\nA Needle here\nafter",
            }
        ],
    }
    monkeypatch.setattr(
        adapter,
        "_AsyncTransport",
        lambda vm: httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200, json=response)),
    )

    async def run():
        plane = adapter.BlaxelDataPlane(
            SimpleNamespace(id="test", hostname="custom.example", base_url="https://custom.example/api"),
            asynchronous=True,
        )
        try:
            result = await plane.fs.grep("needle", "/work", context_lines=1)
            assert result.to_dict() == response
            assert calls[-1].url.params["contextLines"] == "1"
        finally:
            await plane.aclose()

    asyncio.run(run())


def test_native_read_rejects_unencoded_control_characters(filesystem):
    fs, calls, _ = filesystem
    with pytest.raises(httpx.InvalidURL):
        fs.read("line\nbreak")
    assert not calls


def test_native_binary_download_keeps_bytes_and_local_permissions(monkeypatch, tmp_path):
    payload = bytes(range(256))
    monkeypatch.setattr(
        adapter, "_Transport", lambda vm: httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
    )
    plane = adapter.BlaxelDataPlane(
        SimpleNamespace(id="test", hostname="custom.example", base_url="https://custom.example/api")
    )
    try:
        target = tmp_path / "output"
        plane.fs.download("/binary", str(target), mode=0o600)
        assert target.read_bytes() == payload
        assert target.stat().st_mode & 0o777 == 0o600
    finally:
        plane.close()


def test_native_copy_quotes_paths_and_preserves_failure(filesystem, monkeypatch):
    import shlex

    from blaxel.core.sandbox.types import CopyResponse

    fs, _, _ = filesystem
    commands = []
    monkeypatch.setattr(
        fs.process, "exec", lambda request: commands.append(request["command"]) or SimpleNamespace(pid="123")
    )
    result = SimpleNamespace(status="completed", logs="")
    monkeypatch.setattr(fs.process, "wait", lambda *args, **kwargs: result)
    source, destination = "~/$literal;echo nope", "line\nbreak"
    copied = fs.cp(source, destination)
    assert type(copied) is CopyResponse
    assert (copied.source, copied.destination) == (source, destination)
    assert shlex.split(commands[0]) == ["cp", "-r", source, destination]
    result.status, result.logs = "failed", "copy failed"
    with pytest.raises(Exception, match="copy failed"):
        fs.cp(source, destination)


def test_native_listing_rejects_non_directory_response(filesystem):
    fs, _, response = filesystem
    response.clear()
    response.update(content="file")
    with pytest.raises(Exception, match="Directory not found"):
        fs.ls("/file")


def test_native_unsupported_sync_extensions_are_not_fabricated(filesystem):
    from blaxel.core.sandbox.sync.filesystem import SyncSandboxFileSystem

    fs, _, _ = filesystem
    assert isinstance(fs, SyncSandboxFileSystem)
    for name in ["find", "grep", "search"]:
        assert hasattr(fs, name) == hasattr(SyncSandboxFileSystem, name)
    assert callable(fs.watch)
