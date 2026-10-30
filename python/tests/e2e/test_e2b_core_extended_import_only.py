"""Independent live core cases; all three routes must pass to claim equivalence."""

import os

import pytest
from compat_evidence import record_scenario
from compat_program import trace_program

# Only vendor import statements are substituted between routes.
PROGRAM = r"""
from e2b import CommandExitException, PtySize, Sandbox

if case == "volume-mount":
    from e2b import Volume
    from uuid import uuid4
    volume = Volume.create("compat-core-" + uuid4().hex)
    try:
        for writing in (True, False):
            sandbox = Sandbox.create(volume_mounts={"/mnt/compat": volume})
            try:
                if writing:
                    sandbox.files.write("/mnt/compat/persist", "volume-data", user="root")
                assert sandbox.files.read("/mnt/compat/persist", user="root") == "volume-data"
            finally:
                sandbox.kill()
    finally:
        assert Volume.destroy(volume.volume_id)
else:
    sandbox = Sandbox.create()
    try:
        if case == "home-reconnect-user":
            sandbox.files.write("compat-home", "shared-home", user="user")
            assert sandbox.commands.run("cat ~/compat-home").stdout == "shared-home"
            connected = Sandbox.connect(sandbox.sandbox_id)
            assert connected.sandbox_id == sandbox.sandbox_id
            assert connected.files.read("/home/user/compat-home", user="user") == "shared-home"
            connected.commands.run("printf changed > ~/compat-home")
            assert sandbox.files.read("compat-home", user="user") == "changed"
            sandbox.files.write("relative-root", "root-home", user="root")
            assert sandbox.files.read("~/relative-root", user="root") == "root-home"
            assert sandbox.files.read("/root/relative-root") == "root-home"
            assert sandbox.files.read("~/compat-home") == "changed"
            sandbox.commands.run("printf secret > /root/compat-secret; chmod 600 /root/compat-secret", user="root")
            assert connected.files.read("/root/compat-secret", user="root") == "secret"
            assert connected.files.read("/root/compat-secret", user="user") == "secret"
            assert sandbox.commands.run("test ! -r /root/compat-secret", user="user").exit_code == 0
            sandbox.commands.run("chmod 000 /root/compat-secret", user="root")
            sandbox.files.write("/root/compat-secret", "updated", user="user")
            assert sandbox.files.read("/root/compat-secret", user="user") == "updated"
            info = sandbox.files.get_info("/root/compat-secret", user="user")
            assert (info.owner, info.group, info.mode & 0o777) == ("user", "user", 0)
            assert sandbox.files.exists("/root/compat-secret", user="user")
            entries = sandbox.files.list("/root", user="user")
            assert "compat-secret" in {entry.name for entry in entries}
            assert sandbox.files.make_dir("/root/compat-user-dir/nested", user="user")
            info = sandbox.files.get_info("/root/compat-user-dir/nested", user="user")
            assert (info.owner, info.group, info.mode & 0o777) == ("user", "user", 0o755)
            sandbox.files.remove("/root/compat-secret", user="user")
            assert not sandbox.files.exists("/root/compat-secret", user="user")
            sandbox.files.remove("/root/compat-user-dir", user="user")
        elif case in ("stream-read", "stream-cancel"):
            payload = bytes(range(256)) * 16384
            sandbox.files.write("/tmp/compat-stream", payload)
            stream = sandbox.files.read("/tmp/compat-stream", format="stream")
            try:
                first = next(stream)
                assert first and payload.startswith(first)
                assert len(first) < len(payload), "stream returned the entire file in one chunk"
                if case == "stream-read":
                    assert first + b"".join(stream) == payload
            finally:
                stream.close()
            assert sandbox.commands.run("printf alive").stdout == "alive"
            assert bytes(sandbox.files.read("/tmp/compat-stream", format="bytes")) == payload
        elif case in ("pty", "pty-reconnect", "pty-kill"):
            terminal = sandbox.pty.create(size=PtySize(rows=24, cols=80), timeout=0)
            pid = terminal.pid
            assert isinstance(pid, int) and pid > 0
            output = []
            if case == "pty":
                sandbox.pty.resize(pid, PtySize(rows=37, cols=91))
                sandbox.pty.send_stdin(
                    pid, b"stty size > /tmp/compat-pty-size; printf '\\120\\124\\131_OK'; exit\n"
                )
                assert terminal.wait(on_pty=output.append).exit_code == 0
                assert b"PTY_OK" in b"".join(output)
                assert sandbox.files.read("/tmp/compat-pty-size").strip() == "37 91"
            else:
                if case == "pty-reconnect":
                    terminal.disconnect()
                    terminal = sandbox.pty.connect(pid, timeout=0)
                    assert terminal.pid == pid
                    sandbox.pty.send_stdin(pid, b"printf '\\120\\124\\131_RECONNECTED\\377\\000'; exit 7\n")
                else:
                    assert sandbox.pty.kill(pid) is True
                try:
                    terminal.wait(on_pty=output.append)
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
                    repeated = sandbox.pty.kill(pid)
                    assert isinstance(repeated, bool)
                    print(f"PTY kill after wait returned {repeated}")
        elif case.startswith("pause-"):
            mode = case.removeprefix("pause-")
            sandbox.files.write("compat-persist", "disk-state")
            process = sandbox.commands.run(
                "python3 -c 'import signal; signal.pause()' compat-memory-marker", background=True, timeout=0
            )
            assert sandbox.pause(mode=mode)
            connected = Sandbox.connect(sandbox.sandbox_id)
            assert connected.sandbox_id == sandbox.sandbox_id
            assert connected.files.read("compat-persist") == "disk-state"
            state = connected.commands.run(
                f"if test -r /proc/{process.pid}/cmdline; then cat /proc/{process.pid}/cmdline; fi"
            ).stdout
            assert ("compat-memory-marker" in state) == (mode == "full")
        else:
            raise AssertionError(case)
    finally:
        sandbox.kill()
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
@pytest.mark.parametrize("provider,shim", [("e2b", False), ("e2b", True), ("arker", True)])
def test_core_extended_import_only(case, provider, shim, live_router, monkeypatch):
    called = set()
    with record_scenario("e2b", "sync", provider, shim, "import-only-core-" + case, called, api_evidence="runtime"):
        for name in ("E2B_API_KEY", "ARKER_API_KEY", "COMPAT_COVERAGE_EVIDENCE", "COMPAT_COVERAGE_RUN"):
            assert os.environ.get(name, "").strip(), f"Missing live prerequisite: {name}"
        assert any(os.environ.get(name) for name in ("ARKER_SOURCE", "ARKER_SOURCE_VM", "ARKER_SOURCE_VM_ID")), (
            "An isolated source is required"
        )
        if provider == "arker":
            monkeypatch.delenv("E2B_API_KEY")
        program = PROGRAM.replace("from e2b import", "from arker.e2b import") if shim else PROGRAM
        namespace = {"case": case}
        exec(
            trace_program(
                program,
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
