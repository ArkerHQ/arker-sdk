"""Live tests for the Modal compatibility layer's ``.aio`` methods: Modal
async code, unchanged except for its imports, against a real deployment.

Run with:

    ARKER_API_KEY=... python -m pytest tests/e2e/test_modal_async_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Awaitable

import pytest

from arker import modal

if not os.environ.get("ARKER_API_KEY"):
    pytest.skip("live Arker credentials are not configured", allow_module_level=True)

if not os.environ.get("ARKER_BASE_URL"):
    os.environ.setdefault("ARKER_PROVIDER", "aws")
    os.environ.setdefault("ARKER_REGION", "us-east-1")

TAGS = {"suite": f"async-{uuid.uuid4().hex[:8]}"}


@pytest.fixture(scope="module")
def sb():
    sandbox = asyncio.run(modal.Sandbox.create.aio(env={"SANDBOX_VAR": "from-create"}, workdir="/tmp"))
    yield sandbox
    asyncio.run(sandbox.terminate.aio())


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


def test_create_exec_and_terminate() -> None:
    async def main() -> None:
        app = await modal.App.lookup.aio("arker-e2e", create_if_missing=True)
        sandbox = await modal.Sandbox.create.aio(app=app, tags=TAGS)
        try:
            process = await sandbox.exec.aio("bash", "-c", "sleep 2; echo out; echo err >&2; exit 3")
            exit_code, ticks = await ticking(process.wait.aio())
            assert exit_code == 3 and await process.poll.aio() == 3
            assert ticks > 20, "the event loop was blocked while the command ran"
            assert await process.stdout.read.aio() == "out\n"
            assert await process.stderr.read.aio() == "err\n"
            assert [found.object_id async for found in modal.Sandbox.list.aio(tags=TAGS)] == [sandbox.object_id]
            assert (await modal.Sandbox.from_id.aio(sandbox.object_id)).object_id == sandbox.object_id
            await sandbox.set_tags.aio({**TAGS, "more": "1"})
            assert await sandbox.get_tags.aio() == {**TAGS, "more": "1"}
        finally:
            assert await sandbox.terminate.aio(wait=True) == 137
        assert [found async for found in modal.Sandbox.list.aio(tags=TAGS)] == []

    asyncio.run(main())


def test_output_streams_line_by_line(sb: modal.Sandbox) -> None:
    async def main() -> None:
        process = await sb.exec.aio("bash", "-c", "for i in 1 2 3; do echo line$i; sleep 1; done")
        arrivals = [(time.monotonic(), line) async for line in process.stdout]
        assert [line for _, line in arrivals] == ["line1\n", "line2\n", "line3\n"]
        assert arrivals[-1][0] - arrivals[0][0] > 1.5, "lines arrived only at the end"
        raw = await sb.exec.aio("printf", "a\\nb", text=False)
        assert [chunk async for chunk in raw.stdout] == [b"a\n", b"b"]

    asyncio.run(main())


def test_stdin(sb: modal.Sandbox) -> None:
    async def main() -> None:
        process = await sb.exec.aio("bash", "-c", "read a; echo got $a; cat")
        process.stdin.write(b"one\n")
        process.stdin.write("rest")
        process.stdin.write_eof()
        await process.stdin.drain.aio()
        assert await process.wait.aio() == 0
        assert await process.stdout.read.aio() == "got one\nrest"

    asyncio.run(main())


def test_files(sb: modal.Sandbox) -> None:
    async def main() -> None:
        fs = sb.filesystem
        await fs.write_text.aio("hello", "/tmp/aio/notes.txt")
        assert await fs.read_text.aio("/tmp/aio/notes.txt") == "hello"
        assert await fs.read_bytes.aio("/tmp/aio/notes.txt") == b"hello"
        assert [info.name for info in await fs.list_files.aio("/tmp/aio")] == ["notes.txt"]
        assert (await fs.stat.aio("/tmp/aio/notes.txt")).size == 5
        handle = await sb.open.aio("/tmp/aio/opened.txt", "w")
        await handle.write.aio("written")
        await handle.close.aio()
        assert "opened.txt" in await sb.ls.aio("/tmp/aio")
        await fs.remove.aio("/tmp/aio", recursive=True)
        with pytest.raises(modal.SandboxFilesystemNotFoundError):
            await fs.read_text.aio("/tmp/aio/notes.txt")

    asyncio.run(main())


def test_watch(sb: modal.Sandbox) -> None:
    async def main() -> None:
        await sb.mkdir.aio("/tmp/watched")

        async def write_soon() -> None:
            await asyncio.sleep(1.5)
            await sb.filesystem.write_text.aio("x", "/tmp/watched/new.txt")

        writer = asyncio.create_task(write_soon())
        events = [event async for event in sb.filesystem.watch.aio("/tmp/watched", timeout=4)]
        await writer
        assert modal.FileWatchEvent(["/tmp/watched/new.txt"], modal.FileWatchEventType.Create) in events

    asyncio.run(main())


def test_sandboxes_run_concurrently() -> None:
    async def main() -> None:
        sandboxes = await asyncio.gather(*(modal.Sandbox.create.aio(timeout=180) for _ in range(3)))

        async def sleep_in(sandbox: modal.Sandbox) -> int:
            process = await sandbox.exec.aio("sleep", "3")
            return await process.wait.aio()

        try:
            started = time.monotonic()
            assert await asyncio.gather(*(sleep_in(sandbox) for sandbox in sandboxes)) == [0, 0, 0]
            assert time.monotonic() - started < 7, "the sandboxes ran one after another"
        finally:
            await asyncio.gather(*(sandbox.terminate.aio() for sandbox in sandboxes))

    asyncio.run(main())


def test_volumes() -> None:
    async def main() -> None:
        name = f"modal-async-{uuid.uuid4().hex[:8]}"
        volume = modal.Volume.from_name(name, create_if_missing=True)
        try:
            sandbox = await modal.Sandbox.create.aio(volumes={"/mnt/v": volume}, timeout=180)
            try:
                await sandbox.filesystem.write_text.aio("kept", "/mnt/v/note.txt")
                await volume.commit.aio()
            finally:
                await sandbox.terminate.aio()
        finally:
            await modal.Volume.objects.delete.aio(name)
        with pytest.raises(modal.NotFoundError):
            await modal.Volume.objects.delete.aio(name)

    asyncio.run(main())
