"""Live tests for the E2B compatibility layer's async API: E2B async code,
unchanged except for its imports, against a real deployment.

Run with:

    ARKER_API_KEY=... python -m pytest tests/e2e/test_e2b_async_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import time
import urllib.request
import uuid
from collections.abc import Awaitable, Callable

import pytest

from arker import e2b_code_interpreter
from arker.e2b import (
    AsyncCommandHandle,
    AsyncSandbox,
    AsyncVolume,
    CommandExitException,
    FilesystemEventType,
    NotSupportedException,
    PtySize,
    SandboxException,
    SandboxQuery,
)

if not os.environ.get("ARKER_API_KEY"):
    pytest.skip("live Arker credentials are not configured", allow_module_level=True)

if not os.environ.get("ARKER_BASE_URL"):
    os.environ.setdefault("ARKER_PROVIDER", "aws")
    os.environ.setdefault("ARKER_REGION", "us-east-1")

SUITE = f"async-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def sbx():
    sandbox = asyncio.run(AsyncSandbox.create(envs={"SANDBOX_VAR": "from-create"}, metadata={"suite": SUITE}))
    yield sandbox
    asyncio.run(sandbox.kill())


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


def test_create_run_files_and_kill() -> None:
    async def main() -> None:
        async with await AsyncSandbox.create(envs={"V": "create"}) as sandbox:
            assert isinstance(sandbox, AsyncSandbox)
            assert await sandbox.is_running()
            seen: list[str] = []

            async def collect(text: str) -> None:
                await asyncio.sleep(0)
                seen.append(text)

            result = await sandbox.commands.run("echo $V $X; echo warn >&2", envs={"X": "run"}, on_stdout=collect)
            assert (result.stdout, result.stderr, seen) == ("create run\n", "warn\n", ["create run\n"])
            with pytest.raises(CommandExitException) as failed:
                await sandbox.commands.run("exit 3")
            assert failed.value.exit_code == 3

            info = await sandbox.files.write("notes/a.txt", "hello")
            assert info.path == "/home/user/notes/a.txt"
            assert await sandbox.files.read("notes/a.txt") == "hello"
            assert [chunk async for chunk in await sandbox.files.read("notes/a.txt", format="stream")] == [b"hello"]
            assert [entry.name for entry in await sandbox.files.list("notes")] == ["a.txt"]
            assert await sandbox.files.exists("/home/user/notes/a.txt")
            await sandbox.files.remove("notes")
            assert not await sandbox.files.exists("notes")
            assert (await sandbox.get_info()).sandbox_id == sandbox.sandbox_id
            with pytest.raises(NotSupportedException, match=r"AsyncSandbox\.get_mcp_token"):
                await sandbox.get_mcp_token()
            sandbox_id = sandbox.sandbox_id
        assert await AsyncSandbox.kill(sandbox_id) is False

    asyncio.run(main())


def test_background_output_streams_without_blocking_the_loop(sbx: AsyncSandbox) -> None:
    async def main() -> None:
        arrivals: list[tuple[float, str]] = []

        async def on_stdout(text: str) -> None:
            arrivals.append((time.monotonic(), text))

        handle = await sbx.commands.run(
            "for i in 1 2 3; do echo line$i; sleep 1; done; echo oops >&2", background=True, on_stdout=on_stdout
        )
        assert isinstance(handle, AsyncCommandHandle)
        assert handle.pid > 0 and handle.exit_code is None
        result, ticks = await ticking(handle.wait())
        assert result.stdout == handle.stdout == "line1\nline2\nline3\n"
        assert handle.stderr == "oops\n" and handle.exit_code == 0
        assert "".join(text for _, text in arrivals) == result.stdout
        assert arrivals[-1][0] - arrivals[0][0] > 1.5, "output arrived only at the end"
        assert ticks > 20, "the event loop was blocked while waiting"

        sleeper = await sbx.commands.run("sleep 600", background=True, timeout=0)
        assert await sleeper.kill()
        with pytest.raises(SandboxException):
            await sleeper.wait()
        failing = await sbx.commands.run("exit 4", background=True)
        with pytest.raises(CommandExitException):
            await failing.wait()
        assert failing.exit_code == 4

    asyncio.run(main())


def test_reconnect_and_disconnect(sbx: AsyncSandbox) -> None:
    async def main() -> None:
        handle = await sbx.commands.run("sleep 2; echo late", background=True)
        await handle.disconnect()
        with pytest.raises(SandboxException, match="Disconnected"):
            await handle.wait()
        assert handle.pid in [process.pid for process in await sbx.commands.list()]
        seen: list[str] = []
        again = await sbx.commands.connect(handle.pid, on_stdout=seen.append)
        assert (await again.wait()).stdout == "late\n"
        assert seen == ["late\n"]

    asyncio.run(main())


def test_stdin(sbx: AsyncSandbox) -> None:
    async def main() -> None:
        handle = await sbx.commands.run("read a; read b; echo got $a $b", background=True, stdin=True)
        await handle.send_stdin("one\n")
        async with sbx.commands.stream_input(handle.pid) as stream:
            await stream.send("two\n")
        with pytest.raises(SandboxException, match="closed"):
            await stream.send("three\n")
        assert (await handle.wait()).stdout == "got one two\n"

    asyncio.run(main())


def test_pty(sbx: AsyncSandbox) -> None:
    async def main() -> None:
        output = bytearray()

        async def on_data(data: bytes) -> None:
            output.extend(data)

        terminal = await sbx.pty.create(PtySize(rows=24, cols=80), on_data=on_data)
        await sbx.pty.send_stdin(terminal.pid, b"echo pty-$((40 + 2))\n")
        await until(lambda: b"pty-42" in output)
        async with sbx.pty.stream_input(terminal.pid) as stream:
            await stream.send(b"exit 0\n")
        assert (await terminal.wait()).exit_code == 0

    asyncio.run(main())


def test_watch_dir(sbx: AsyncSandbox) -> None:
    async def main() -> None:
        await sbx.files.make_dir("/tmp/watched")
        events = []
        exits = []
        watcher = await sbx.files.watch_dir("/tmp/watched", on_event=events.append, on_exit=exits.append)
        await sbx.files.write("/tmp/watched/new.txt", "x")
        await until(lambda: any(event.name == "new.txt" for event in events))
        assert events[0].type == FilesystemEventType.CREATE
        await watcher.stop()
        assert exits == [None]

        timed_out = []
        await sbx.files.watch_dir("/tmp/watched", on_event=events.append, on_exit=timed_out.append, timeout=1)
        await until(lambda: bool(timed_out))
        assert "timeout" in str(timed_out[0])

    asyncio.run(main())


def test_git(sbx: AsyncSandbox) -> None:
    async def main() -> None:
        await sbx.git.init("/tmp/async-repo", initial_branch="main")
        await sbx.files.write("/tmp/async-repo/a.txt", "a")
        assert (await sbx.git.status("/tmp/async-repo")).has_untracked
        await sbx.git.add("/tmp/async-repo")
        await sbx.git.commit("/tmp/async-repo", "first", author_name="A", author_email="a@example.com")
        assert (await sbx.git.status("/tmp/async-repo")).is_clean
        assert (await sbx.git.branches("/tmp/async-repo")).current_branch == "main"

    asyncio.run(main())


def test_list_connect_and_metadata(sbx: AsyncSandbox) -> None:
    async def main() -> None:
        pages = AsyncSandbox.list(query=SandboxQuery(metadata={"suite": SUITE}))
        assert pages.has_next
        assert [info.sandbox_id for info in await pages.next_items()] == [sbx.sandbox_id]
        assert not pages.has_next
        again = await AsyncSandbox.connect(sbx.sandbox_id, timeout=600)
        assert isinstance(again, AsyncSandbox)
        assert (await again.commands.run("echo $SANDBOX_VAR")).stdout == "from-create\n"
        assert await sbx.connect() is sbx
        assert (pages.next_token, sbx.traffic_access_token) == (None, None)
        await sbx.files.write("/tmp/signed.txt", "signed")
        url = await sbx.download_url("/tmp/signed.txt")
        with urllib.request.urlopen(url, timeout=30) as response:
            assert response.read() == b"signed"

    asyncio.run(main())


def test_sandboxes_and_commands_run_concurrently() -> None:
    async def main() -> None:
        sandboxes = await asyncio.gather(*(AsyncSandbox.create(timeout=180) for _ in range(3)))
        try:
            started = time.monotonic()
            results = await asyncio.gather(
                *(sandbox.commands.run("sleep 3; echo $E2B_SANDBOX_ID") for sandbox in sandboxes)
            )
            assert time.monotonic() - started < 7, "the sandboxes ran one after another"
            assert [result.stdout.strip() for result in results] == [sandbox.sandbox_id for sandbox in sandboxes]

            handles = await asyncio.gather(
                *(sandboxes[0].commands.run(f"sleep 3; echo {n}", background=True) for n in range(4))
            )
            started = time.monotonic()
            done = await asyncio.gather(*(handle.wait() for handle in handles))
            assert time.monotonic() - started < 7, "the commands ran one after another"
            assert [result.stdout for result in done] == [f"{n}\n" for n in range(4)]
        finally:
            await asyncio.gather(*(sandbox.kill() for sandbox in sandboxes))

    asyncio.run(main())


def test_volume_mounts() -> None:
    async def main() -> None:
        name = f"e2b-async-{uuid.uuid4().hex[:8]}"
        volume = await AsyncVolume.create(name)
        try:
            async with await AsyncSandbox.create(volume_mounts={"/mnt/data": volume}, timeout=180) as sandbox:
                await sandbox.files.write("/mnt/data/note.txt", "kept")
            assert (await AsyncVolume.get_info(volume.volume_id)).name == name
        finally:
            assert await AsyncVolume.destroy(volume.volume_id)

    asyncio.run(main())


def test_code_interpreter() -> None:
    async def main() -> None:
        async with await e2b_code_interpreter.AsyncSandbox.create() as sandbox:
            assert isinstance(sandbox, e2b_code_interpreter.AsyncSandbox)
            printed = []

            async def on_stdout(message: e2b_code_interpreter.OutputMessage) -> None:
                printed.append(message.line)

            await sandbox.run_code("x = 41")
            execution = await sandbox.run_code("print('hi'); x + 1", on_stdout=on_stdout)
            assert execution.text == "42"
            assert printed == ["hi\n"]
            context = await sandbox.create_code_context()
            assert (await sandbox.run_code("'x' in globals()", context=context)).text == "False"
            assert (await sandbox.commands.run("echo shell")).stdout == "shell\n"
            assert (await sandbox.run_code("const n: number = 6; n * 7", language="ts")).text == "42"

    asyncio.run(main())
