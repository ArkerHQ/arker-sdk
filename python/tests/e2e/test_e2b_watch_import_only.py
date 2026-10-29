"""Watch the same filesystem mutations through native and routed clients."""

import asyncio
import os
import time

import pytest
from compat_evidence import record_scenario
from compat_program import async_program

PROGRAM = r"""
from e2b import Sandbox
from e2b import SandboxException

sandbox = Sandbox.create()
try:
    root = '/home/user/watch-test'
    sandbox.files.make_dir(root + '/nested')
    events, exits = [], []
    if async_style:
        watcher = sandbox.files.watch_dir(
            root, events.append, on_exit=exits.append, timeout=0, recursive=recursive, include_entry=True
        )
    else:
        watcher = sandbox.files.watch_dir(root, recursive=recursive, include_entry=True)
    try:
        name = 'nested/event.txt' if recursive else 'event.txt'
        sandbox.files.write(root + '/' + name, 'watch-data')
        deadline = monotonic() + 30
        while not any(event.name == name and event.type.value == 'create' for event in events):
            assert monotonic() < deadline, 'Create event was not delivered'
            if not async_style:
                events.extend(watcher.get_new_events(request_timeout=10))
            sleep(0.01)
        created = next(event for event in events if event.name == name and event.type.value == 'create')
        assert created.entry is not None
        assert created.entry.path == root + '/' + name
        assert created.entry.name == 'event.txt'
        sandbox.files.remove(root + '/' + name)
        while not any(event.name == name and event.type.value == 'remove' for event in events):
            assert monotonic() < deadline, 'Remove event was not delivered'
            if not async_style:
                events.extend(watcher.get_new_events(request_timeout=10))
            sleep(0.01)
    finally:
        if async_style:
            watcher.stop()
        else:
            watcher.stop(request_timeout=10)
    if async_style:
        assert exits == [None]
        watcher.stop()
        assert exits == [None]
    else:
        try:
            watcher.get_new_events()
        except SandboxException as error:
            assert type(error) is SandboxException
            assert str(error) == 'The watcher is already stopped'
        else:
            raise AssertionError('Stopped watcher accepted an event read')
finally:
    sandbox.kill()
"""
ASYNC_PROGRAM = async_program(
    PROGRAM, rename={"Sandbox": "AsyncSandbox"}, targets={"AsyncSandbox", "sandbox", "watcher", "sleep"}
)


@pytest.mark.parametrize("recursive", [False, True], ids=["directory", "recursive"])
@pytest.mark.parametrize("style", ["sync", "async"])
@pytest.mark.parametrize(
    "provider,shim", [("e2b", False), ("e2b", True), ("arker", True)], ids=["reference", "native", "arker"]
)
def test_watch_import_only(recursive, style, provider, shim, live_router, monkeypatch):
    for key in ("E2B_API_KEY", "ARKER_API_KEY", "COMPAT_COVERAGE_EVIDENCE", "COMPAT_COVERAGE_RUN"):
        assert os.environ.get(key), f"Missing live prerequisite: {key}"
    apis = [
        "Sandbox.create",
        "Sandbox.kill",
        "Sandbox.files.make_dir",
        "Sandbox.files.watch_dir",
        "Sandbox.files.write",
        "Sandbox.files.remove",
        "WatchHandle.stop",
    ]
    if style == "sync":
        apis.append("WatchHandle.get_new_events")
    with record_scenario("e2b", style, provider, shim, f"import-only-watch-{recursive}", apis):
        if provider == "arker":
            monkeypatch.delenv("E2B_API_KEY")
        program = ASYNC_PROGRAM if style == "async" else PROGRAM
        if shim:
            program = program.replace("from e2b import Sandbox\n", "from arker.e2b import Sandbox\n")
            program = program.replace("from e2b import AsyncSandbox\n", "from arker.e2b import AsyncSandbox\n")
        namespace = {
            "recursive": recursive,
            "async_style": style == "async",
            "monotonic": time.monotonic,
            "sleep": asyncio.sleep if style == "async" else time.sleep,
        }
        exec(compile(program, __file__, "exec"), namespace)
        if style == "async":
            asyncio.run(namespace["program"]())
