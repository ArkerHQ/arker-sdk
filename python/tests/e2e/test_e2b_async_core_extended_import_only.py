"""Independent async core cases; all three routes must pass to claim equivalence."""

import asyncio
import os

import pytest
from compat_evidence import record_scenario
from compat_program import trace_program

# Only vendor import statements are substituted between routes.
PROGRAM = r"""
from e2b import AsyncSandbox, CommandExitException, PtySize

async def program():
    if case == "volume-mount":
        from e2b import AsyncVolume
        from uuid import uuid4
        volume = await AsyncVolume.create("compat-async-core-" + uuid4().hex)
        try:
            for writing in (True, False):
                sandbox = await AsyncSandbox.create(volume_mounts={"/mnt/compat": volume})
                try:
                    if writing:
                        await sandbox.files.write("/mnt/compat/persist", "volume-data", user="root")
                    assert await sandbox.files.read("/mnt/compat/persist", user="root") == "volume-data"
                finally:
                    await sandbox.kill()
        finally:
            assert await AsyncVolume.destroy(volume.volume_id)
    else:
        sandbox = await AsyncSandbox.create()
        try:
            if case == "home-reconnect-user":
                await sandbox.files.write("compat-home", "shared-home", user="user")
                assert (await sandbox.commands.run("cat ~/compat-home")).stdout == "shared-home"
                connected = await AsyncSandbox.connect(sandbox.sandbox_id)
                assert connected.sandbox_id == sandbox.sandbox_id
                assert await connected.files.read("/home/user/compat-home", user="user") == "shared-home"
                await connected.commands.run("printf changed > ~/compat-home")
                assert await sandbox.files.read("compat-home", user="user") == "changed"
                await sandbox.files.write("relative-root", "root-home", user="root")
                assert await sandbox.files.read("~/relative-root", user="root") == "root-home"
                assert await sandbox.files.read("/root/relative-root") == "root-home"
                assert await sandbox.files.read("~/compat-home") == "changed"
                await sandbox.commands.run(
                    "printf secret > /root/compat-secret; chmod 600 /root/compat-secret", user="root"
                )
                assert await connected.files.read("/root/compat-secret", user="root") == "secret"
                assert await connected.files.read("/root/compat-secret", user="user") == "secret"
                assert (await sandbox.commands.run("test ! -r /root/compat-secret", user="user")).exit_code == 0
                await sandbox.commands.run("chmod 000 /root/compat-secret", user="root")
                await sandbox.files.write("/root/compat-secret", "updated", user="user")
                assert await sandbox.files.read("/root/compat-secret", user="user") == "updated"
                info = await sandbox.files.get_info("/root/compat-secret", user="user")
                assert (info.owner, info.group, info.mode & 0o777) == ("user", "user", 0)
                assert await sandbox.files.exists("/root/compat-secret", user="user")
                entries = await sandbox.files.list("/root", user="user")
                assert "compat-secret" in {entry.name for entry in entries}
                assert await sandbox.files.make_dir("/root/compat-user-dir/nested", user="user")
                info = await sandbox.files.get_info("/root/compat-user-dir/nested", user="user")
                assert (info.owner, info.group, info.mode & 0o777) == ("user", "user", 0o755)
                await sandbox.files.remove("/root/compat-secret", user="user")
                assert not await sandbox.files.exists("/root/compat-secret", user="user")
                await sandbox.files.remove("/root/compat-user-dir", user="user")
            elif case in ("stream-read", "stream-cancel"):
                payload = bytes(range(256)) * 16384
                await sandbox.files.write("/tmp/compat-stream", payload)
                stream = await sandbox.files.read("/tmp/compat-stream", format="stream")
                try:
                    first = await anext(stream)
                    assert first and payload.startswith(first)
                    assert len(first) < len(payload), "stream returned the entire file in one chunk"
                    if case == "stream-read":
                        assert first + b"".join([chunk async for chunk in stream]) == payload
                finally:
                    await stream.aclose()
                assert (await sandbox.commands.run("printf alive")).stdout == "alive"
                assert bytes(await sandbox.files.read("/tmp/compat-stream", format="bytes")) == payload
            elif case in ("pty", "pty-reconnect", "pty-kill"):
                output = []
                async def on_data(chunk):
                    await asyncio.sleep(0)
                    output.append(chunk)
                terminal = await sandbox.pty.create(
                    size=PtySize(rows=24, cols=80), on_data=on_data, timeout=0
                )
                pid = terminal.pid
                assert isinstance(pid, int) and pid > 0
                if case == "pty":
                    await sandbox.pty.resize(pid, PtySize(rows=37, cols=91))
                    await sandbox.pty.send_stdin(
                        pid, b"stty size > /tmp/compat-pty-size; printf '\\120\\124\\131_OK'; exit\n"
                    )
                    assert (await terminal.wait()).exit_code == 0
                    assert b"PTY_OK" in b"".join(output)
                    assert (await sandbox.files.read("/tmp/compat-pty-size")).strip() == "37 91"
                else:
                    if case == "pty-reconnect":
                        await terminal.disconnect()
                        output.clear()
                        terminal = await sandbox.pty.connect(pid, on_data=on_data, timeout=0)
                        assert terminal.pid == pid
                        await sandbox.pty.send_stdin(pid, b"printf '\\120\\124\\131_RECONNECTED\\377\\000'; exit 7\n")
                    else:
                        assert await sandbox.pty.kill(pid) is True
                    try:
                        await terminal.wait()
                    except CommandExitException as error:
                        assert error.exit_code == (7 if case == "pty-reconnect" else -1)
                        assert error.stdout == error.stderr == ""
                    else:
                        raise AssertionError("PTY wait did not raise CommandExitException")
                    assert all(isinstance(chunk, bytes) for chunk in output)
                    if case == "pty-reconnect":
                        assert b"PTY_RECONNECTED\xff\x00" in b"".join(output)
                    else:
                        # Native returns false on NOT_FOUND; retention after exit is server-dependent.
                        repeated = await sandbox.pty.kill(pid)
                        assert isinstance(repeated, bool)
                        print(f"PTY kill after wait returned {repeated}")
            elif case.startswith("pause-"):
                mode = case.removeprefix("pause-")
                await sandbox.files.write("compat-persist", "disk-state")
                process = await sandbox.commands.run(
                    "python3 -c 'import signal; signal.pause()' compat-memory-marker", background=True, timeout=0
                )
                assert await sandbox.pause(mode=mode)
                connected = await AsyncSandbox.connect(sandbox.sandbox_id)
                assert connected.sandbox_id == sandbox.sandbox_id
                assert await connected.files.read("compat-persist") == "disk-state"
                state = (await connected.commands.run(
                    f"if test -r /proc/{process.pid}/cmdline; then cat /proc/{process.pid}/cmdline; fi"
                )).stdout
                assert ("compat-memory-marker" in state) == (mode == "full")
            else:
                raise AssertionError(case)
        finally:
            await sandbox.kill()
"""

CASES = (
    "home-reconnect-user",
    "stream-read",
    "stream-cancel",
    "pty",
    "pty-reconnect",
    "pty-kill",
    "pause-full",
    "pause-filesystem",
    "volume-mount",
)


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize(
    "provider,shim", [("e2b", False), ("e2b", True), ("arker", True)], ids=["reference", "native", "arker"]
)
def test_async_core_extended_import_only(case, provider, shim, live_router, monkeypatch):
    called = set()
    with record_scenario(
        "e2b", "async", provider, shim, "import-only-async-core-" + case, called, api_evidence="runtime"
    ):
        for name in ("E2B_API_KEY", "ARKER_API_KEY", "COMPAT_COVERAGE_EVIDENCE", "COMPAT_COVERAGE_RUN"):
            assert os.environ.get(name, "").strip(), f"Missing live prerequisite: {name}"
        assert any(os.environ.get(name) for name in ("ARKER_SOURCE", "ARKER_SOURCE_VM", "ARKER_SOURCE_VM_ID")), (
            "An isolated source is required"
        )
        if provider == "arker":
            monkeypatch.delenv("E2B_API_KEY")
        source = PROGRAM.replace("from e2b import", "from arker.e2b import") if shim else PROGRAM
        namespace = {"asyncio": asyncio, "case": case}
        exec(
            trace_program(
                source,
                __file__,
                namespace,
                {
                    "Sandbox": "Sandbox",
                    "AsyncSandbox": "Sandbox",
                    "sandbox": "Sandbox",
                    "connected": "Sandbox",
                    "terminal": "CommandHandle",
                    "process": "CommandHandle",
                    "Volume": "Volume",
                    "AsyncVolume": "Volume",
                    "volume": "Volume",
                },
                called,
            ),
            namespace,
        )
        asyncio.run(namespace["program"]())
