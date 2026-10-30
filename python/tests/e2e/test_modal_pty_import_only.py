"""PTY process differential; only the public import changes."""

import asyncio
import os

import pytest
from compat_evidence import ModalCleanup, record_scenario

PROGRAM = r"""
import modal
from modal.container_process import ContainerProcess
from modal._output.pty import get_pty_info
import asyncio
import json
import time

async def program(asynchronous, cleanup):
    async def call(method, *args, **options):
        if asynchronous:
            return await method.aio(*args, **options)
        return await asyncio.to_thread(method, *args, **options)
    app = await asyncio.to_thread(modal.App.lookup, "arker-compat-lifecycle", create_if_missing=True)
    image = modal.Image.from_registry("python:3.12-slim-bookworm")
    box = await call(modal.Sandbox.create, app=app, image=image, timeout=60)
    cleanup.allocated(box.object_id)
    print(json.dumps({"event": "allocated", "id": box.object_id}), flush=True)
    try:
        info = get_pty_info(shell=True)
        command = (
            'import os,json,sys;line=sys.stdin.readline();print("RESULT:"+json.dumps({'
            '"tty":[os.isatty(n) for n in range(3)],"size":list(os.get_terminal_size()),'
            '"term":os.getenv("TERM"),"line":line}));'
            'os.write(2,b"stderr-marker\\n");raise SystemExit(7)'
        )
        process = await call(box.exec, "python3", "-c", command, pty=True, env={"TERM":"explicit-term"})
        assert isinstance(process, ContainerProcess)
        process.stdin.write(b"input\n")
        await call(process.stdin.drain)
        output = await call(process.stdout.read)
        assert await call(process.stderr.read) == ""
        assert await call(process.wait) == 7
        record = next(line[7:] for line in output.split("\r\n") if line.startswith("RESULT:"))
        assert json.loads(record) == {
            "tty":[True,True,True], "size":[info.winsz_cols,info.winsz_rows],
            "term":"explicit-term", "line":"input\n",
        }
        assert "stderr-marker\r\n" in output
    finally:
        await call(box.terminate)
        end = time.monotonic() + 75
        while await call(box.poll) is None:
            assert time.monotonic() < end, "Owned sandbox cleanup unconfirmed"
            await asyncio.sleep(0.5)
        await cleanup.observe(box.object_id,
            lambda identifier: call(modal.Sandbox.from_id, identifier),
            lambda retained: call(retained.poll))
        print(json.dumps({"event": "cleanup_confirmed", "id": box.object_id}), flush=True)
"""


@pytest.mark.parametrize("provider,shim", [("modal", False), ("modal", True), ("arker", True)])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_modal_pty_import_only(provider, shim, asynchronous, live_router, monkeypatch):
    pytest.importorskip("modal")
    required = ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET") if provider == "modal" else ("ARKER_API_KEY",)
    if not all(os.environ.get(key) for key in required):
        pytest.skip("Live credentials required")
    if provider == "arker":
        monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
        monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    namespace = {}
    source = PROGRAM.replace("import modal\n", "from arker import modal\n", 1) if shim else PROGRAM
    exec(compile(source, "modal_pty_import_only.py", "exec"), namespace)
    cleanup = ModalCleanup()
    with record_scenario(
        "modal",
        "async" if asynchronous else "sync",
        provider,
        shim,
        "import-only-pty-process",
        [
            "Sandbox.create",
            "Sandbox.exec",
            "ContainerProcess.stdout",
            "ContainerProcess.stderr",
            "ContainerProcess.stdin",
            "ContainerProcess.wait",
            "Sandbox.terminate",
        ],
        cleanup=cleanup,
    ):
        asyncio.run(namespace["program"](asynchronous, cleanup))
