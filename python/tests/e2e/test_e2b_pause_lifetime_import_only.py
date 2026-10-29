"""Paused sandboxes must survive their old running deadline.

Contract: https://docs.e2b.dev/sandbox/persistence#paused-sandbox-retention
"""

import asyncio
import os

import pytest
from compat_evidence import record_scenario
from compat_program import async_program

PROGRAM = r"""
from e2b import Sandbox
from time import monotonic, sleep

sandbox = Sandbox.create()
try:
    sandbox.files.write("compat-pause-lifetime", "survived-running-deadline")
    sandbox.set_timeout(30)
    old_deadline = monotonic() + 30
    assert sandbox.pause(mode=mode)
    assert monotonic() < old_deadline, "Pause did not complete within the running timeout"
    assert sandbox.is_running() is False
    # The clock starts after set_timeout returns, so this outlasts the server deadline.
    sleep(max(0, old_deadline - monotonic()) + 1)
    assert sandbox.is_running() is False
    connected = Sandbox.connect(sandbox.sandbox_id)
    assert connected.sandbox_id == sandbox.sandbox_id
    assert connected.files.read("compat-pause-lifetime") == "survived-running-deadline"
    assert connected.is_running() is True
finally:
    sandbox.kill()
"""

ASYNC_PROGRAM = async_program(
    PROGRAM.replace("from time import monotonic, sleep", "from time import monotonic\nfrom asyncio import sleep"),
    rename={"Sandbox": "AsyncSandbox"},
    targets={"AsyncSandbox", "sandbox", "connected", "sleep"},
)
APIS = [
    "Sandbox.create",
    "Sandbox.set_timeout",
    "Sandbox.pause",
    "Sandbox.is_running",
    "Sandbox.connect",
    "Sandbox.files.write",
    "Sandbox.files.read",
    "Sandbox.kill",
]


@pytest.mark.parametrize("mode", ["full", "filesystem"])
@pytest.mark.parametrize("style", ["sync", "async"])
@pytest.mark.parametrize(
    "provider,shim", [("e2b", False), ("e2b", True), ("arker", True)], ids=["reference", "native", "arker"]
)
def test_pause_survives_prior_running_deadline(mode, style, provider, shim, live_router, monkeypatch):
    with record_scenario("e2b", style, provider, shim, "import-only-pause-beyond-ttl-" + mode, APIS):
        for name in ("E2B_API_KEY", "ARKER_API_KEY", "COMPAT_COVERAGE_EVIDENCE", "COMPAT_COVERAGE_RUN"):
            assert os.environ.get(name, "").strip(), f"Missing live prerequisite: {name}"
        assert any(os.environ.get(name) for name in ("ARKER_SOURCE", "ARKER_SOURCE_VM", "ARKER_SOURCE_VM_ID")), (
            "An isolated source is required"
        )
        if provider == "arker":
            monkeypatch.delenv("E2B_API_KEY")
        source = ASYNC_PROGRAM if style == "async" else PROGRAM
        if shim:
            source = source.replace("from e2b import", "from arker.e2b import")
        namespace = {"mode": mode}
        exec(compile(source, __file__, "exec"), namespace)
        if style == "async":
            asyncio.run(namespace["program"]())
