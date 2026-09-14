import base64
import json

import httpx2 as httpx
import pytest

import arker.computer as sdk


@pytest.mark.parametrize("size", [0, 5, 20 * 1024 * 1024 + 1])
def test_write_uses_supported_sync_chunks(monkeypatch, size):
    data = (b"\x00\xffhello" * (size // 7 + 1))[:size]
    received = bytearray()
    upload_ids = set()

    def handle(request):
        assert request.method == "POST"
        assert request.url.path == "/api/v1/vms/vm_1/sync"
        body = json.loads(request.content)
        assert body["op"] == "write"
        assert len(body["writes"]) == 1
        results = []
        for entry in body["writes"]:
            chunk = base64.b64decode(entry["content"])
            assert len(chunk) <= 5 * 1024 * 1024
            assert entry["path"] == "/tmp/probe"
            assert entry["size"] == size
            assert entry["start"] == len(received)
            received.extend(chunk)
            assert entry["end"] == len(received)
            upload_ids.add(entry["upload_id"])
            results.append(
                {
                    "path": "/tmp/probe",
                    "size": size,
                    "written": len(received) == size,
                    "complete": len(received) == size,
                    "received_bytes": len(received),
                    "ranges": [{"start": 0, "end": len(received)}],
                }
            )
        return httpx.Response(200, json={"ok": True, "op": "write", "results": results})

    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        monkeypatch.setattr(sdk, "_http_client", transport)
        sdk.Arker(api_key="test", base_url="https://test.invalid/api", retry=False).vm("vm_1").sync("/tmp/probe", data)
    assert received == data
    assert len(upload_ids) == 1


def test_directory_uploads_archive_then_extracts(monkeypatch, tmp_path):
    import io
    import shlex
    import tarfile

    local = tmp_path / "local"
    local.mkdir()
    script = local / "hello.sh"
    script.write_bytes(b"#!/bin/sh\necho hello\n")
    script.chmod(0o755)
    uploaded = bytearray()
    remote_tar = None
    commands = []

    def handle(request):
        nonlocal remote_tar
        body = json.loads(request.content)
        if request.url.path.endswith("/runs"):
            command = body["command"]
            assert remote_tar in command
            assert shlex.quote("/tmp/dst with 'quote") in command
            assert "tar -xf" in command
            commands.append(command)
            return httpx.Response(
                200,
                json={
                    "run_id": "r1",
                    "state": "completed",
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "stdout_encoding": "utf-8",
                    "stderr_encoding": "utf-8",
                },
            )
        assert request.url.path == "/api/v1/vms/vm_1/sync"
        if body["op"] == "manifest":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "op": "manifest",
                    "root": body["path"],
                    "hash_algo": "sha256",
                    "entries": [],
                    "truncated": False,
                },
            )
        assert body["op"] == "write"
        results = []
        for entry in body["writes"]:
            remote_tar = entry["path"]
            uploaded.extend(base64.b64decode(entry["content"]))
            results.append(
                {
                    "path": remote_tar,
                    "size": entry["size"],
                    "received_bytes": len(uploaded),
                    "ranges": [{"start": 0, "end": len(uploaded)}],
                    "complete": len(uploaded) == entry["size"],
                    "written": len(uploaded) == entry["size"],
                }
            )
        return httpx.Response(200, json={"ok": True, "op": "write", "results": results})

    monkeypatch.setenv("ARKER_CACHE_DIR", str(tmp_path / "cache"))
    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        monkeypatch.setattr(sdk, "_http_client", transport)
        result = (
            sdk.Arker(api_key="test", base_url="https://test.invalid/api", retry=False)
            .vm("vm_1")
            .sync_dir(str(local), "/tmp/dst with 'quote")
        )
    assert result.sent == 1
    assert len(commands) == 1
    with tarfile.open(fileobj=io.BytesIO(uploaded)) as archive:
        member = archive.getmember("hello.sh")
        assert member.mode == 0o755
        assert archive.extractfile(member).read() == script.read_bytes()


@pytest.mark.parametrize("complete,written", [(False, False), (True, False)])
def test_write_rejects_unfinished_file(monkeypatch, complete, written):
    def handle(request):
        entry = json.loads(request.content)["writes"][0]
        return httpx.Response(
            200,
            json={
                "ok": True,
                "op": "write",
                "results": [
                    {
                        "path": entry["path"],
                        "size": 1,
                        "received_bytes": 1,
                        "ranges": [{"start": 0, "end": 1}],
                        "complete": complete,
                        "written": written,
                    }
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        monkeypatch.setattr(sdk, "_http_client", transport)
        with pytest.raises(sdk.ArkerError, match="did not complete"):
            sdk.Arker(api_key="test", base_url="https://test.invalid/api", retry=False).vm("vm_1").sync("/tmp/x", b"x")
