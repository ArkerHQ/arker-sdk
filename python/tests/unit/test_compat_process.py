from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from arker._compat_process import GuestProcesses
from arker.computer import CompletedRunResult


class LocalVM:
    """Exercise the real wire script and supervisor without a cloud dependency."""

    def __init__(self, directory: Path):
        self.directory = directory

    def sync(self, path, content):
        (self.directory / Path(path).name).write_text(content)

    def run(self, command, **options):
        import os

        # The production path is content-addressed in /tmp; local tests keep
        # their process state and installed agent inside the pytest directory.
        command = command.replace("/tmp/arker-compat-process-", str(self.directory / "arker-compat-process-"))
        response = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            timeout=10,
            env={**os.environ, "ARKER_COMPAT_PROCESS_ROOT": str(self.directory / "processes")},
        )
        return CompletedRunResult(
            stdout=response.stdout.decode(),
            stderr=response.stderr.decode(),
            stdout_bytes=response.stdout,
            stderr_bytes=response.stderr,
            exit_code=response.returncode,
        )


@pytest.fixture
def processes(tmp_path):
    manager = GuestProcesses(LocalVM(tmp_path))
    yield manager, tmp_path
    for process in manager.list():
        if process.status == "running":
            manager.kill(process.name)
            manager.wait(process.name, max_wait=5000, interval=10)


def test_background_reconnect_stdin_eof_and_logs(processes):
    manager, directory = processes
    started = manager.exec(
        {
            "command": "cat; printf error >&2",
            "name": "input",
            "stdin": True,
            "workingDir": str(directory),
            "waitForCompletion": False,
        }
    )
    assert started.status == "running"
    reconnected = GuestProcesses(LocalVM(directory))
    assert reconnected.get(started.pid).name == "input"
    reconnected.write_stdin("input", "hello λ\n")
    reconnected.write_stdin("input", b"second\n")
    reconnected.close_stdin("input")
    finished = reconnected.wait("input", max_wait=5000, interval=10)
    assert finished.status == "completed"
    assert finished.exit_code == 0
    assert finished.stdout == "hello λ\nsecond\n"
    assert finished.stderr == "error"
    assert reconnected.logs("input", "stdout") == finished.stdout
    assert reconnected.logs("input", "stderr") == "error"
    assert any(item.name == "input" for item in reconnected.list())
    with pytest.raises(RuntimeError, match="stdin is closed"):
        reconnected.write_stdin("input", "late")


def test_foreground_callbacks_and_nonzero_exit(processes):
    manager, directory = processes
    chunks = []
    finished = manager.exec(
        {
            "command": "printf output; printf problem >&2; exit 7",
            "name": "finite",
            "workingDir": str(directory),
            "waitForCompletion": True,
            "on_stdout": chunks.append,
        }
    )
    assert finished.exit_code == 7
    assert finished.status == "failed"
    assert "".join(chunks) == "output"
    assert finished.to_dict()["workingDir"] == str(directory)


def test_wait_timeout_does_not_kill_and_name_does_not_reexecute(processes):
    manager, directory = processes
    manager.exec({"command": "sleep 30", "name": "slow", "workingDir": str(directory)})
    with pytest.raises(TimeoutError):
        manager.wait("slow", max_wait=1, interval=10)
    assert manager.get("slow").status == "running"
    with pytest.raises(RuntimeError):
        manager.exec({"command": "printf duplicate", "name": "slow", "workingDir": str(directory)})
    manager.kill("slow")
    assert manager.wait("slow", max_wait=5000, interval=10).status == "killed"


def test_execution_timeout_kills_process(processes):
    manager, directory = processes
    finished = manager.exec(
        {"command": "sleep 30", "timeout": 100, "workingDir": str(directory), "waitForCompletion": True}
    )
    assert finished.status == "killed"
    assert finished.exit_code != 0


def test_async_wait_cancellation_leaves_process_observable(processes):
    from arker.blaxel import _AsyncProcess

    manager, directory = processes

    async def program():
        process = _AsyncProcess(manager)
        started = await process.exec({"command": "sleep 30", "name": "cancel", "workingDir": str(directory)})
        assert started.status == "running"
        waiter = asyncio.create_task(process.wait("cancel", max_wait=-1, interval=10))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert (await process.get("cancel")).status == "running"
        await process.kill("cancel")
        assert (await process.wait("cancel", max_wait=5000, interval=10)).status == "killed"

    asyncio.run(program())


def test_replayed_transport_request_recovers_same_process(processes):
    manager, directory = processes
    request = {"command": "sleep 30", "name": "replay", "workingDir": str(directory), "_requestId": "nonce"}
    first = manager._call("exec", **request)
    second = manager._call("exec", **request)
    assert first["pid"] == second["pid"]
    with pytest.raises(RuntimeError, match="already exists"):
        manager._call("exec", **{**request, "_requestId": "different-intent"})
