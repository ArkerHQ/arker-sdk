"""Bounded filesystem differential with only the Sandbox import changed."""

import asyncio
import os
import uuid

import pytest
from compat_evidence import record_scenario

PROGRAM = r"""
from modal import Sandbox
from modal.exception import SandboxFilesystemDirectoryNotEmptyError, SandboxFilesystemNotFoundError
from modal.types import FileInfo, FileType
import asyncio
import tempfile
from pathlib import Path

async def program(identifier, asynchronous):
    sandbox = await asyncio.to_thread(Sandbox.from_id, identifier)
    filesystem = sandbox.filesystem
    async def call(name, *args, **options):
        method = getattr(filesystem, name)
        if asynchronous:
            return await method.aio(*args, **options)
        return await asyncio.to_thread(method, *args, **options)
    directory = "/tmp/arker-modal-filesystem-differential"
    await call("make_directory", directory)
    payload = bytes(range(256)) * 256
    await call("write_bytes", payload, directory + "/binary")
    assert await call("read_bytes", directory + "/binary") == payload
    await call("write_text", "hello λ\n", directory + "/nested/text")
    assert await call("read_text", directory + "/nested/text") == "hello λ\n"
    info = await call("stat", directory + "/binary")
    assert isinstance(info, FileInfo) and info.type == FileType.FILE
    assert info.size == len(payload) and info.owner == "root" and info.group == "root"
    assert info.permissions == "0644"
    entries = await call("list_files", directory)
    assert [entry.name for entry in entries] == ["binary", "nested"]
    with tempfile.TemporaryDirectory(prefix="modal-fs-copy-") as local:
        target = Path(local) / "nested/binary"
        await call("copy_to_local", directory + "/binary", target)
        assert target.read_bytes() == payload
        await call("copy_from_local", target, directory + "/uploaded")
        assert await call("read_bytes", directory + "/uploaded") == payload
        try:
            await call("copy_to_local", directory + "/missing", target)
        except SandboxFilesystemNotFoundError:
            pass
        else:
            raise AssertionError("Missing remote download must fail")
        assert target.read_bytes() == payload
    try:
        await call("remove", directory)
    except SandboxFilesystemDirectoryNotEmptyError:
        pass
    else:
        raise AssertionError("Nonrecursive removal must preserve a nonempty directory")
    await call("remove", directory, recursive=True)
    try:
        await call("stat", directory)
    except SandboxFilesystemNotFoundError:
        pass
    else:
        raise AssertionError("Removed directory must not remain")
"""


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("provider,shim", [("modal", False), ("modal", True), ("arker", True)])
def test_modal_filesystem_import_only(provider, shim, asynchronous, live_router, monkeypatch):
    modal = pytest.importorskip("modal")
    required = ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET") if provider == "modal" else ("ARKER_API_KEY",)
    if not all(os.environ.get(key) for key in required):
        pytest.skip("Live credentials required")
    if provider == "modal":
        fixture = modal.Sandbox.create(
            app=modal.App.lookup("arker-compat-image-test", create_if_missing=False), timeout=180
        )
        identifier, cleanup = fixture.object_id, fixture.terminate
    else:
        from arker import Arker

        replay_key = "modal-filesystem-" + uuid.uuid4().hex
        try:
            fixture = Arker().fork(
                source_vm_name=os.environ.get("ARKER_SOURCE", "ubuntu"),
                idempotency_key=replay_key,
                policies={
                    "policies": [{"type": "inbound", "action": "deny"}, {"type": "outbound", "action": "deny"}],
                },
            )
        except Exception as error:
            raise RuntimeError(
                f"Fork did not return an owned VM; reconcile replay key {replay_key} before another attempt"
            ) from error
        identifier, cleanup = fixture.id, fixture.delete
        monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
        monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    with record_scenario(
        "modal",
        "sync",
        provider,
        shim,
        "import-only-filesystem-binary-metadata-local-copy-" + ("aio" if asynchronous else "blocking"),
        [
            "Sandbox.from_id",
            "Sandbox.filesystem",
            *[
                "Sandbox.filesystem." + name
                for name in (
                    "make_directory",
                    "write_bytes",
                    "read_bytes",
                    "write_text",
                    "read_text",
                    "stat",
                    "list_files",
                    "copy_to_local",
                    "copy_from_local",
                    "remove",
                )
            ],
        ],
    ):
        try:
            source = (
                PROGRAM.replace("from modal import Sandbox", "from arker.modal import Sandbox") if shim else PROGRAM
            )
            namespace = {}
            exec(compile(source, "modal_filesystem_import_only.py", "exec"), namespace)
            asyncio.run(namespace["program"](identifier, asynchronous))
        finally:
            cleanup()
