"""E2B filesystem semantics with only the Sandbox import changed."""

import os

import pytest
from compat_evidence import record_scenario
from compat_program import async_program, trace_program

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
    assert sandbox.files.make_dir(root) is True
    assert sandbox.files.make_dir(root) is False
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
    assert sandbox.files.make_dir(root + "/nested", user="root") is True
    sandbox.files.write(root + "/nested/child", "nested", user="root")
    unusual = "tab\tnewline\nseparator\x1f"
    sandbox.files.write(root + "/" + unusual, "unusual")
    assert unusual in {entry.name for entry in sandbox.files.list(root)}
    for depth in (2,):
        assert root + "/nested/child" in {
            entry.path for entry in sandbox.files.list(root, depth=depth, user="root")
        }
    assert root + "/nested/child" not in {entry.path for entry in sandbox.files.list(root, depth=None)}
    renamed = sandbox.files.rename(root + "/text.txt", root + "/renamed.txt")
    assert renamed.path == root + "/renamed.txt"
    assert not sandbox.files.exists(root + "/text.txt")
    assert sandbox.files.exists(root + "/renamed.txt")
    sandbox.files.remove(root + "/renamed.txt")
    assert not sandbox.files.exists(root + "/renamed.txt")
finally:
    sandbox.kill()
"""


ASYNC_PROGRAM = (
    "import asyncio\n"
    + async_program(
        SYNC_PROGRAM,
        rename={"Sandbox": "AsyncSandbox"},
        targets={"AsyncSandbox", "sandbox"},
    )
    + "\nasyncio.run(program())\n"
)


@pytest.mark.parametrize("style,program", [("sync", SYNC_PROGRAM), ("async", ASYNC_PROGRAM)], ids=["sync", "async"])
@pytest.mark.parametrize("provider,shim", [("e2b", False), ("e2b", True), ("arker", True)])
def test_filesystem_import_only(style, program, provider, shim, live_router, monkeypatch):
    pytest.importorskip("e2b")
    if (provider == "e2b" and not os.environ.get("E2B_API_KEY")) or (
        provider == "arker" and not os.environ.get("ARKER_API_KEY")
    ):
        pytest.skip("live credentials required")
    if provider == "arker":
        monkeypatch.delenv("E2B_API_KEY", raising=False)
    if shim:
        program = program.replace("from e2b import", "from arker.e2b import")
    called = set()
    with record_scenario(
        "e2b", style, provider, shim, "import-only-filesystem-crud-metadata", called, api_evidence="runtime"
    ):
        namespace = {}
        roots = {"Sandbox": "Sandbox", "AsyncSandbox": "Sandbox", "sandbox": "Sandbox"}
        exec(trace_program(program, __file__, namespace, roots, called), namespace)
