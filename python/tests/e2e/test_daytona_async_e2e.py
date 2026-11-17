"""Live tests for the Daytona compatibility layer's async API: Daytona async
code, unchanged except for its imports, against a real deployment.

Run with:

    ARKER_API_KEY=... python -m pytest tests/e2e/test_daytona_async_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Awaitable, Callable

import pytest

from arker.daytona import (
    AsyncDaytona,
    AsyncSandbox,
    CreateSandboxFromSnapshotParams,
    DaytonaNotFoundError,
    DaytonaNotSupportedError,
    ListSandboxesQuery,
    PtySize,
    SessionExecuteRequest,
    VolumeMount,
)

if not os.environ.get("ARKER_API_KEY"):
    pytest.skip("live Arker credentials are not configured", allow_module_level=True)

if not os.environ.get("ARKER_BASE_URL"):
    os.environ.setdefault("ARKER_PROVIDER", "aws")
    os.environ.setdefault("ARKER_REGION", "us-east-1")

LABEL = {"suite": f"async-{uuid.uuid4().hex[:8]}"}


@pytest.fixture(scope="module")
def sandbox():
    async def create() -> AsyncSandbox:
        async with AsyncDaytona() as daytona:
            return await daytona.create(CreateSandboxFromSnapshotParams(env_vars={"SANDBOX_VAR": "x"}, labels=LABEL))

    sandbox = asyncio.run(create())
    yield sandbox
    asyncio.run(sandbox.delete())


async def ticking(work: Awaitable[object]) -> tuple[object, int]:
    """``work``'s result, and how often a 50ms ticker ran meanwhile: it only
    runs while the event loop is free."""
    ticks = 0
    done = False

    async def ticker() -> None:
        nonlocal ticks
        while not done:
            await asyncio.sleep(0.05)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        result = await work
    finally:
        done = True
        await task
    return result, ticks


async def until(condition: Callable[[], bool], seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out waiting"
        await asyncio.sleep(0.2)


def test_create_exec_files_and_delete() -> None:
    async def main() -> None:
        async with AsyncDaytona() as daytona:
            sandbox = await daytona.create(CreateSandboxFromSnapshotParams(env_vars={"V": "create"}))
            try:
                assert isinstance(sandbox, AsyncSandbox)
                response, ticks = await ticking(sandbox.process.exec("sleep 2; echo $V; exit 3"))
                assert (response.exit_code, response.result) == (3, "create\n")
                assert ticks > 20, "the event loop was blocked while the command ran"
                await sandbox.fs.upload_file(b"hello", "/tmp/a/b.txt")
                assert await sandbox.fs.download_file("/tmp/a/b.txt") == b"hello"
                assert [chunk async for chunk in await sandbox.fs.download_file_stream("/tmp/a/b.txt")] == [b"hello"]
                assert [entry.name for entry in await sandbox.fs.list_files("/tmp/a")] == ["b.txt"]
                assert (await sandbox.fs.get_file_info("/tmp/a/b.txt")).size == 5
                await sandbox.fs.delete_file("/tmp/a", recursive=True)
                with pytest.raises(DaytonaNotFoundError):
                    await sandbox.fs.get_file_info("/tmp/a")
                await sandbox.set_labels({"k": "v"})
                assert sandbox.labels == {"k": "v"}
                with pytest.raises(DaytonaNotSupportedError, match=r"AsyncSandbox\.create_ssh_access"):
                    await sandbox.create_ssh_access()
            finally:
                await daytona.delete(sandbox)
            with pytest.raises(DaytonaNotFoundError):
                await daytona.get(sandbox.id)

    asyncio.run(main())


def test_list_and_get(sandbox: AsyncSandbox) -> None:
    async def main() -> None:
        daytona = AsyncDaytona()
        listed = [found.id async for found in daytona.list(ListSandboxesQuery(labels=LABEL))]
        assert listed == [sandbox.id]
        again = await daytona.get(sandbox.id)
        assert isinstance(again, AsyncSandbox)
        assert (await again.process.exec("echo $SANDBOX_VAR")).result == "x\n"
        await daytona.stop(again)
        assert again.state == "stopped"
        await daytona.start(again)
        assert again.state == "started"
        assert [found.id async for found in daytona.list({"labels": LABEL, "is_public": True})] == []
        page = await daytona.snapshot.list(source_sandbox_id=sandbox.id)
        assert (page.items, page.total) == ([], 0)
        assert [info.id for info in await sandbox.process.list_pty_sessions()] == []

    asyncio.run(main())


def test_sessions_stream_logs(sandbox: AsyncSandbox) -> None:
    async def main() -> None:
        process = sandbox.process
        await process.create_session("async")
        started = await process.execute_session_command(
            "async",
            SessionExecuteRequest(command="read name; for i in 1 2; do echo $name$i; sleep 1; done", run_async=True),
        )
        await process.send_session_command_input("async", started.cmd_id, "arker\n")
        arrivals: list[tuple[float, str]] = []

        async def on_stdout(text: str) -> None:
            arrivals.append((time.monotonic(), text))

        await process.get_session_command_logs_async("async", started.cmd_id, on_stdout, lambda _: None)
        assert "".join(text for _, text in arrivals) == "arker1\narker2\n"
        assert arrivals[-1][0] - arrivals[0][0] > 0.5, "output arrived only at the end"
        logs = await process.get_session_command_logs("async", started.cmd_id)
        assert logs.stdout == "arker1\narker2\n"
        assert [session.session_id for session in await process.list_sessions()] == ["async"]
        await process.delete_session("async")

    asyncio.run(main())


def test_pty_session(sandbox: AsyncSandbox) -> None:
    async def main() -> None:
        output = bytearray()

        async def on_data(data: bytes) -> None:
            output.extend(data)

        pty = await sandbox.process.create_pty_session(
            "term", on_data, cwd="/tmp", envs={"GREETING": "hi"}, pty_size=PtySize(rows=24, cols=80)
        )
        await pty.wait_for_connection()
        assert pty.is_connected()
        await pty.resize(PtySize(rows=30, cols=111))
        await pty.send_input("echo $GREETING $(pwd) $(tput cols); exit 4\n")
        result = await pty.wait()
        assert result.exit_code == 4
        assert b"hi /tmp 111" in output

    asyncio.run(main())


def test_git_and_code_interpreter(sandbox: AsyncSandbox) -> None:
    async def main() -> None:
        await sandbox.git.init("/tmp/repo", initial_branch="main")
        await sandbox.fs.upload_file(b"one", "/tmp/repo/a.txt")
        status = await sandbox.git.status("/tmp/repo")
        assert (status.current_branch, [file.name for file in status.file_status]) == ("main", ["a.txt"])
        printed = []

        async def on_stdout(message) -> None:
            printed.append(message.output)

        result = await sandbox.code_interpreter.run_code("x = 2; print(x * 21)", on_stdout=on_stdout)
        assert (result.stdout, printed) == ("42\n", ["42\n"])
        context = await sandbox.code_interpreter.create_context()
        assert [found.id for found in await sandbox.code_interpreter.list_contexts()] == [context.id]
        await sandbox.code_interpreter.delete_context(context)

    asyncio.run(main())


def test_sandboxes_run_concurrently() -> None:
    async def main() -> None:
        daytona = AsyncDaytona()
        sandboxes = await asyncio.gather(*(daytona.create() for _ in range(3)))
        try:
            started = time.monotonic()
            responses = await asyncio.gather(*(sandbox.process.exec("sleep 3; hostname") for sandbox in sandboxes))
            assert time.monotonic() - started < 7, "the sandboxes ran one after another"
            assert all(response.exit_code == 0 for response in responses)
        finally:
            await asyncio.gather(*(daytona.delete(sandbox) for sandbox in sandboxes))

    asyncio.run(main())


def test_volumes() -> None:
    async def main() -> None:
        daytona = AsyncDaytona()
        name = f"daytona-async-{uuid.uuid4().hex[:8]}"
        volume = await daytona.volume.create(name)
        try:
            assert (await daytona.volume.get(name)).id == volume.id
            sandbox = await daytona.create(
                CreateSandboxFromSnapshotParams(volumes=[VolumeMount(volume_id=volume.id, mount_path="/mnt/v")])
            )
            try:
                await sandbox.fs.upload_file(b"kept", "/mnt/v/note.txt")
            finally:
                await sandbox.delete()
        finally:
            await daytona.volume.delete(volume)
        assert name not in [found.name for found in await daytona.volume.list()]

    asyncio.run(main())
