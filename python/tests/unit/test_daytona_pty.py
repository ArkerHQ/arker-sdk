from __future__ import annotations

import asyncio
import json
import sys

import pytest
import test_compat_process

from arker._daytona_pty import AsyncPtyHandle, PtyHandle, PtyProcess


@pytest.fixture
def processes(tmp_path):
    yield from test_compat_process.processes.__wrapped__(tmp_path)


def start(manager, directory, program):
    manager.exec(
        {
            "name": "daytona-pty:terminal",
            "command": json.dumps({"id": "terminal", "cwd": str(directory), "envs": {}}),
            "argv": [sys.executable, "-c", program],
            "workingDir": str(directory),
            "stdin": True,
            "tty": {"rows": 24, "cols": 80},
        }
    )
    owner = PtyProcess()
    owner._terminal_manager = manager
    return owner, PtyHandle(owner, "terminal")


def test_detach_reconnect_preserves_terminal_and_exit_status(processes):
    manager, directory = processes
    owner, first = start(manager, directory, "import sys; print(input(),flush=True); sys.exit(7)")
    before = manager.get("daytona-pty:terminal").pid
    first.disconnect()
    assert not first.is_connected()
    assert manager.get("daytona-pty:terminal").status == "running"
    second = owner.connect_pty_session("terminal")
    second.send_input("hello\n")
    output = []
    result = second.wait(output.append, timeout=5)
    assert result.exit_code == 7
    assert result.error == "non-zero exit"
    assert b"hello" in b"".join(output)
    assert manager.get("daytona-pty:terminal").pid == before
    assert not second.is_connected()
    assert owner.list_pty_sessions() == []
    with pytest.raises(Exception, match="PTY session not found"):
        owner.get_pty_session_info("terminal")


def test_async_wait_cancellation_preserves_reader_and_awaits_callbacks(processes):
    async def exercise():
        manager, directory = processes
        _owner, handle = start(manager, directory, "import sys; print(input(),flush=True)")
        output = []

        async def on_data(data):
            await asyncio.sleep(0)
            output.append(data)

        terminal = AsyncPtyHandle(handle, on_data)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(terminal.wait(), 0.01)
        assert terminal.is_connected()
        await terminal.send_input("async-data\n")
        result = await asyncio.wait_for(terminal.wait(), 5)
        assert result.exit_code == 0
        assert result.error is None
        assert b"async-data" in b"".join(output)
        await terminal.disconnect()

    asyncio.run(exercise())


def test_terminal_launcher_drops_identity_and_transfers_terminal_ownership(processes):
    import os
    import pwd
    import shlex

    if os.geteuid() != 0:
        pytest.skip("Changing terminal ownership requires root")
    account = pwd.getpwnam("nobody")
    manager, _directory = processes
    manager.exec(
        {
            "name": "unprivileged-terminal",
            "command": "terminal ownership",
            "argv": ["/bin/sh", "-i"],
            "user": "nobody",
            "workingDir": "/tmp",
            "stdin": True,
            "tty": {"rows": 24, "cols": 80},
        }
    )
    probe = (
        "import os,json; print(json.dumps({'uid':os.getuid(),'owner':os.fstat(0).st_uid,"
        "'tty':os.isatty(0),'controlling':os.tcgetpgrp(0)==os.getpgrp()}))"
    )
    manager.write_stdin("unprivileged-terminal", "python3 -c " + shlex.quote(probe) + "; exit\n")
    result = manager.wait("unprivileged-terminal", max_wait=5000, interval=10)
    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{"uid"')]
    assert records == [{"uid": account.pw_uid, "owner": account.pw_uid, "tty": True, "controlling": True}]
    assert result.exit_code == 0


def test_async_created_terminal_is_reconciled_after_cancellation():
    import threading
    from types import SimpleNamespace

    from arker._daytona_pty import AsyncPtyProcess

    async def exercise():
        entered, release, cleaned = threading.Event(), threading.Event(), threading.Event()
        events = []

        def create(*args):
            entered.set()
            assert release.wait(5)
            events.append("created")
            return SimpleNamespace(
                kill=lambda: events.append("killed"), disconnect=lambda: (events.append("disconnected"), cleaned.set())
            )

        owner = AsyncPtyProcess()
        owner._process = SimpleNamespace(create_pty_session=create)
        pending = asyncio.create_task(owner.create_pty_session("owned", lambda _: None))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            pending.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert await asyncio.to_thread(cleaned.wait, 5)
            assert events == ["created", "killed", "disconnected"]
        finally:
            release.set()

    asyncio.run(exercise())


def test_async_callback_can_disconnect_its_terminal(processes):
    async def exercise():
        manager, directory = processes
        _owner, handle = start(manager, directory, "import sys; print(input(),flush=True); input()")
        completed = asyncio.Event()
        terminal = None

        async def output(_):
            await terminal.disconnect()
            completed.set()

        terminal = AsyncPtyHandle(handle, output)
        await terminal.send_input("disconnect\n")
        await asyncio.wait_for(completed.wait(), 5)
        await asyncio.wait_for(terminal.wait(), 5)
        assert terminal.error is None
        assert manager.get("daytona-pty:terminal").status == "running"

    asyncio.run(exercise())
