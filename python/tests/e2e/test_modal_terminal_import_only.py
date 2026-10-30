"""Retained terminal lifecycle differential; only the public import changes."""

import asyncio
import os

import pytest
from compat_evidence import record_scenario

PROGRAM = r"""
import modal
from modal.exception import NotFoundError, SandboxTerminatedError, SandboxTimeoutError
import asyncio
import json
import time

async def program(asynchronous, reason):
    async def call(method, *args, **options):
        if asynchronous:
            return await method.aio(*args, **options)
        return await asyncio.to_thread(method, *args, **options)
    app = await asyncio.to_thread(modal.App.lookup, "arker-compat-lifecycle", create_if_missing=True)
    image = modal.Image.from_registry("python:3.12-slim-bookworm")
    box = await call(modal.Sandbox.create, app=app, image=image, timeout=30)
    print(json.dumps({"event": "allocated", "id": box.object_id}), flush=True)
    try:
        assert await call(box.poll) is None
        if reason == "requested":
            assert await call(box.terminate, wait=True) == 137
        else:
            end = time.monotonic() + 120
            while await call(box.poll) is None:
                assert time.monotonic() < end, "Sandbox deadline did not terminate execution"
                await asyncio.sleep(0.5)
        retained = await call(modal.Sandbox.from_id, box.object_id)
        assert retained.object_id == box.object_id
        assert await call(retained.poll) == (124 if reason == "deadline" else 137)
        expected = SandboxTimeoutError if reason == "deadline" else SandboxTerminatedError
        try:
            await call(retained.wait)
        except expected as error:
            assert type(error) is expected
        else:
            raise AssertionError("wait must preserve the native terminal exception")
        try:
            await call(retained.exec, "true")
        except NotFoundError as error:
            assert type(error) is NotFoundError
        else:
            raise AssertionError("A retained handle must never admit execution")
    finally:
        await call(box.terminate)
        print(json.dumps({"event": "cleanup_requested", "id": box.object_id}), flush=True)
"""


@pytest.mark.parametrize("provider,shim", [("modal", False), ("modal", True), ("arker", True)])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("reason", ["requested", "deadline"])
def test_modal_terminal_import_only(provider, shim, asynchronous, reason, live_router, monkeypatch):
    pytest.importorskip("modal")
    required = ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET") if provider == "modal" else ("ARKER_API_KEY",)
    if not all(os.environ.get(key) for key in required):
        pytest.skip("Live credentials required")
    if provider == "arker":
        monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
        monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    namespace = {}
    source = PROGRAM.replace("import modal\n", "from arker import modal\n", 1) if shim else PROGRAM
    exec(compile(source, "modal_terminal_import_only.py", "exec"), namespace)
    with record_scenario(
        "modal",
        "async" if asynchronous else "sync",
        provider,
        shim,
        f"import-only-retained-terminal-{reason}",
        ["Sandbox.create", "Sandbox.from_id", "Sandbox.poll", "Sandbox.wait", "Sandbox.terminate", "Sandbox.exec"],
    ):
        asyncio.run(namespace["program"](asynchronous, reason))
