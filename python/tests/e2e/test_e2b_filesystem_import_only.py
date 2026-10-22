"""E2B filesystem semantics with only the Sandbox import changed."""

import json
import os

import pytest
from compat_evidence import record

SYNC_PROGRAM = r"""
from e2b import FileType, Sandbox

sandbox = Sandbox.create()
root = "/tmp/compat-e2b-files-sync"
relative = "compat-e2b-relative-sync"
identity_command = (
    "printf '%s|%s|%s|%s|%s|%s|%s' "
    '"$(id -un)" "$(id -u)" "$(id -g)" "$HOME" "$USER" "$LOGNAME" "$PWD"'
)
try:
    identity = sandbox.commands.run(identity_command)
    assert identity.stdout == "user|1000|1000|/home/user|user|user|/home/user"
    root_identity = sandbox.commands.run(identity_command, user="root")
    assert root_identity.stdout == "root|0|0|/root|root|root|/home/user"
    assert sandbox.commands.run("printf %s \"$COMPAT_ONCE\"", envs={"COMPAT_ONCE": "one"}).stdout == "one"
    assert sandbox.commands.run("printf %s \"${COMPAT_ONCE-unset}\"").stdout == "unset"
    assert sandbox.commands.run("pwd", cwd="/tmp").stdout.strip() == "/tmp"
    assert sandbox.commands.run("pwd").stdout.strip() == "/home/user"
    sandbox.commands.run("umask 077")
    sandbox.commands.run(
        "printf secret > /tmp/compat-e2b-root-only && chmod 600 /tmp/compat-e2b-root-only",
        user="root",
    )
    assert sandbox.files.read("/tmp/compat-e2b-root-only") == "secret"
    sandbox.commands.run("rm -f /tmp/compat-e2b-root-only", user="root")
    sandbox.commands.run("rm -rf " + root + " /home/user/" + relative)
    sandbox.files.make_dir(root)
    text = sandbox.files.write(root + "/text.txt", "hello\n")
    binary = sandbox.files.write(root + "/binary.bin", b"\x00\xff\x10")
    relative_info = sandbox.files.write(relative + "/cwd.txt", "cwd")
    batch = sandbox.files.write_files([
        {"path": root + "/batch-a.txt", "data": "a"},
        {"path": root + "/batch-b.bin", "data": b"\x01\x02"},
    ])
    assert text.path == root + "/text.txt"
    assert binary.path == root + "/binary.bin"
    assert relative_info.path == "/home/user/" + relative + "/cwd.txt"
    assert [entry.path for entry in batch] == [root + "/batch-a.txt", root + "/batch-b.bin"]
    assert sandbox.files.read(root + "/text.txt") == "hello\n"
    assert bytes(sandbox.files.read(root + "/binary.bin", format="bytes")) == b"\x00\xff\x10"
    assert sandbox.files.exists(relative + "/cwd.txt")

    info = sandbox.files.get_info(root + "/text.txt")
    assert info.name == "text.txt"
    assert info.path == root + "/text.txt"
    assert info.type == FileType.FILE
    assert info.size == 6
    assert info.mode & 0o777 == 0o644
    assert info.permissions == "-rw-r--r--"
    assert info.owner == "user"
    assert info.group == "user"
    assert info.modified_time is not None
    assert info.symlink_target is None

    entries = {entry.name: entry for entry in sandbox.files.list(root)}
    assert set(entries) == {"batch-a.txt", "batch-b.bin", "binary.bin", "text.txt"}
    assert entries["binary.bin"].size == 3
    assert entries["binary.bin"].type == FileType.FILE
    renamed = sandbox.files.rename(root + "/text.txt", root + "/renamed.txt")
    assert renamed.path == root + "/renamed.txt"
    assert not sandbox.files.exists(root + "/text.txt")
    assert sandbox.files.exists(root + "/renamed.txt")
    sandbox.files.remove(root + "/renamed.txt")
    assert not sandbox.files.exists(root + "/renamed.txt")
finally:
    sandbox.kill()
"""


ASYNC_PROGRAM = r"""
import asyncio
from e2b import AsyncSandbox, FileType

async def main():
    sandbox = await AsyncSandbox.create()
    root = "/tmp/compat-e2b-files-async"
    relative = "compat-e2b-relative-async"
    identity_command = (
        "printf '%s|%s|%s|%s|%s|%s|%s' "
        '"$(id -un)" "$(id -u)" "$(id -g)" "$HOME" "$USER" "$LOGNAME" "$PWD"'
    )
    try:
        identity = await sandbox.commands.run(identity_command)
        assert identity.stdout == "user|1000|1000|/home/user|user|user|/home/user"
        root_identity = await sandbox.commands.run(identity_command, user="root")
        assert root_identity.stdout == "root|0|0|/root|root|root|/home/user"
        assert (await sandbox.commands.run("printf %s \"$COMPAT_ONCE\"", envs={"COMPAT_ONCE": "one"})).stdout == "one"
        assert (await sandbox.commands.run("printf %s \"${COMPAT_ONCE-unset}\"")).stdout == "unset"
        assert (await sandbox.commands.run("pwd", cwd="/tmp")).stdout.strip() == "/tmp"
        assert (await sandbox.commands.run("pwd")).stdout.strip() == "/home/user"
        await sandbox.commands.run("umask 077")
        await sandbox.commands.run(
            "printf secret > /tmp/compat-e2b-root-only-async && chmod 600 /tmp/compat-e2b-root-only-async",
            user="root",
        )
        assert await sandbox.files.read("/tmp/compat-e2b-root-only-async") == "secret"
        await sandbox.commands.run("rm -f /tmp/compat-e2b-root-only-async", user="root")
        await sandbox.commands.run("rm -rf " + root + " /home/user/" + relative)
        await sandbox.files.make_dir(root)
        await sandbox.files.write(root + "/text.txt", "hello\n")
        await sandbox.files.write(root + "/binary.bin", b"\x00\xff\x10")
        relative_info = await sandbox.files.write(relative + "/cwd.txt", "cwd")
        await sandbox.files.write_files([
            {"path": root + "/batch-a.txt", "data": "a"},
            {"path": root + "/batch-b.bin", "data": b"\x01\x02"},
        ])
        assert relative_info.path == "/home/user/" + relative + "/cwd.txt"
        assert await sandbox.files.read(root + "/text.txt") == "hello\n"
        assert bytes(await sandbox.files.read(root + "/binary.bin", format="bytes")) == b"\x00\xff\x10"
        info = await sandbox.files.get_info(root + "/text.txt")
        assert info.type == FileType.FILE
        assert info.size == 6
        assert info.mode & 0o777 == 0o644
        assert info.permissions == "-rw-r--r--"
        assert info.owner == "user"
        assert info.group == "user"
        entries = {entry.name: entry for entry in await sandbox.files.list(root)}
        assert entries["binary.bin"].size == 3
        renamed = await sandbox.files.rename(root + "/text.txt", root + "/renamed.txt")
        assert renamed.path == root + "/renamed.txt"
        assert not await sandbox.files.exists(root + "/text.txt")
        await sandbox.files.remove(root + "/renamed.txt")
        assert not await sandbox.files.exists(root + "/renamed.txt")
    finally:
        await sandbox.kill()

asyncio.run(main())
"""


APIS = [
    "Sandbox.create",
    "Sandbox.commands.run",
    "Sandbox.files.make_dir",
    "Sandbox.files.write",
    "Sandbox.files.write_files",
    "Sandbox.files.read",
    "Sandbox.files.exists",
    "Sandbox.files.get_info",
    "Sandbox.files.list",
    "Sandbox.files.rename",
    "Sandbox.files.remove",
    "Sandbox.kill",
]


@pytest.mark.parametrize("style,program", [("sync", SYNC_PROGRAM), ("async", ASYNC_PROGRAM)])
@pytest.mark.parametrize("provider,shim", [("e2b", False), ("e2b", True), ("arker", True)])
def test_filesystem_import_only(style, program, provider, shim, tmp_path, monkeypatch):
    pytest.importorskip("e2b")
    if (provider == "e2b" and not os.environ.get("E2B_API_KEY")) or (
        provider == "arker" and not os.environ.get("ARKER_API_KEY")
    ):
        pytest.skip("live credentials required")
    if provider == "arker":
        monkeypatch.delenv("E2B_API_KEY", raising=False)
    config = tmp_path / "router.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "priority", "order": [provider]},
                "create": {"strategy": "priority", "order": [provider]},
            }
        )
    )
    monkeypatch.setenv("COMPUTESDK_ROUTER_CONFIG", str(config))
    if shim:
        program = program.replace("from e2b import", "from arker.e2b import")
    outcome = "failed"
    try:
        exec(compile(program, "e2b_filesystem_import_only.py", "exec"), {})
        outcome = "passed"
    finally:
        record(
            "e2b",
            style,
            "reference" if not shim else "arker" if provider == "arker" else "native",
            "import-only-filesystem-crud-metadata",
            APIS,
            outcome,
        )
