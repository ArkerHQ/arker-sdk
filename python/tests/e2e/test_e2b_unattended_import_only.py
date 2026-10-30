"""Client-quiet daemon work, reconnect, pause/resume, fork and expiry proof."""

import asyncio
import importlib
import json
import os
import shlex
import uuid

import pytest
from compat_evidence import record_scenario


async def program(Sandbox, asynchronous):
    async def call(method, *args, **kwargs):
        return await method(*args, **kwargs) if asynchronous else await asyncio.to_thread(method, *args, **kwargs)

    async def unattended(box):
        path = "/home/user/quiet-" + uuid.uuid4().hex
        command = (
            "import json,time,pathlib; start=time.time(); time.sleep(2); "
            f"pathlib.Path({path!r}).write_text(json.dumps([start,time.time()]))"
        )
        handle = await call(box.commands.run, "python3 -c " + shlex.quote(command), background=True, timeout=30)
        await call(handle.disconnect)
        # No command/file/control requests during this interval. In particular,
        # do not poll a process handle: that can keep an otherwise idle VM awake.
        await asyncio.sleep(8)
        started, completed = json.loads(await call(box.files.read, path))
        assert 1.5 <= completed - started < 5, "Work resumed only when the client returned"

    owned, errors = [], []
    try:
        box = await call(Sandbox.create, timeout=180)
        owned.append(box)
        await unattended(box)
        box = await call(Sandbox.connect, box.sandbox_id)
        await unattended(box)
        for mode in ("full", "filesystem"):
            assert await call(box.pause, mode=mode)
            assert await call(box.is_running) is False
            box = await call(Sandbox.connect, box.sandbox_id)
            await unattended(box)
        children = await call(box.fork, count=1, timeout=60)
        owned.extend(child for child in children if not isinstance(child, Exception))
        assert len(children) == 1
        if isinstance(children[0], Exception):
            raise children[0]
        await unattended(children[0])
        await unattended(box)
        await call(box.set_timeout, 3)
        await asyncio.sleep(6)
        assert await call(box.is_running) is False, "Lifetime run prevented expiry"
    except BaseException as error:
        errors.append(error)
    finally:
        for box in reversed(owned):
            try:
                await call(box.kill)
                assert await call(box.is_running) is False
            except BaseException as error:
                errors.append(error)
    if errors:
        failure = RuntimeError("Unattended differential or cleanup failed")
        failure.errors = tuple(errors)
        raise failure from errors[0]


@pytest.mark.parametrize("style", ["sync", "async"])
@pytest.mark.parametrize(
    "provider,shim", [("e2b", False), ("e2b", True), ("arker", True)], ids=["reference", "native", "arker"]
)
def test_unattended(style, provider, shim, live_router, monkeypatch):
    apis = [
        "Sandbox.create",
        "Sandbox.connect",
        "Sandbox.pause",
        "Sandbox.fork",
        "Sandbox.set_timeout",
        "Sandbox.is_running",
        "Sandbox.kill",
        "Sandbox.commands.run",
        "Sandbox.files.read",
    ]
    with record_scenario("e2b", style, provider, shim, "import-only-unattended", apis):
        for name in ("E2B_API_KEY", "ARKER_API_KEY", "COMPAT_COVERAGE_EVIDENCE", "COMPAT_COVERAGE_RUN"):
            assert os.environ.get(name, "").strip(), f"Missing live prerequisite: {name}"
        if provider == "arker":
            monkeypatch.delenv("E2B_API_KEY")
        module = importlib.import_module("arker.e2b" if shim else "e2b")
        Sandbox = getattr(module, "AsyncSandbox" if style == "async" else "Sandbox")
        asyncio.run(program(Sandbox, style == "async"))
