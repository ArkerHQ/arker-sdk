"""The same lifecycle program runs against native E2B and both routed facades."""

import asyncio
import os

import pytest
from compat_evidence import record_scenario
from compat_program import async_program

PROGRAM = r"""
from e2b import Sandbox
from e2b import SandboxNotFoundException

sandbox = Sandbox.create()
try:
    if case == "running-timeout":
        before = sandbox.get_info()
        assert sandbox.set_timeout(120) is None
        shorter = sandbox.get_info()
        assert shorter.state == "running"
        assert shorter.started_at == before.started_at
        assert shorter.end_at < before.end_at
        assert Sandbox.set_timeout(sandbox.sandbox_id, 240, request_timeout=60) is None
        longer = sandbox.get_info()
        assert longer.started_at == before.started_at
        assert longer.end_at > shorter.end_at
        assert_deadline(sandbox.sandbox_id, True)
    elif case == "deleted-state":
        assert sandbox.kill() is True
        assert sandbox.is_running() is False
        try:
            sandbox.get_info()
        except SandboxNotFoundException as error:
            assert type(error) is SandboxNotFoundException
        else:
            raise AssertionError("Deleted get_info must raise native SandboxNotFoundException")
        assert sandbox.kill() is False
    else:
        assert_deadline(sandbox.sandbox_id, True)
        assert sandbox.pause() is True
        assert sandbox.is_running() is False
        assert_deadline(sandbox.sandbox_id, False)
        if case == "paused-timeout":
            try:
                sandbox.set_timeout(120)
            except SandboxNotFoundException as error:
                assert type(error) is SandboxNotFoundException
            else:
                raise AssertionError("Paused set_timeout must raise native SandboxNotFoundException")
            finally:
                assert_deadline(sandbox.sandbox_id, False)
        else:
            assert sandbox.pause() is False
            assert_deadline(sandbox.sandbox_id, False)
            assert Sandbox.beta_pause(sandbox.sandbox_id, request_timeout=60) is False
            assert sandbox.get_info(request_timeout=0).state == "paused"
        assert sandbox.is_running() is False
        assert_deadline(sandbox.sandbox_id, False)
finally:
    sandbox.kill()
"""

ASYNC_PROGRAM = async_program(
    PROGRAM,
    rename={"Sandbox": "AsyncSandbox"},
    targets={"AsyncSandbox", "sandbox", "assert_deadline"},
)
CASES = {
    "running-timeout": ["Sandbox.set_timeout", "Sandbox.get_info"],
    "paused-timeout": ["Sandbox.pause", "Sandbox.set_timeout", "Sandbox.is_running"],
    "repeated-pause": ["Sandbox.pause", "Sandbox.beta_pause", "Sandbox.is_running", "Sandbox.get_info"],
    "deleted-state": ["Sandbox.is_running", "Sandbox.get_info"],
}


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("style", ["sync", "async"])
@pytest.mark.parametrize(
    "provider,shim", [("e2b", False), ("e2b", True), ("arker", True)], ids=["reference", "native", "arker"]
)
def test_lifecycle_differential(case, style, provider, shim, live_router, monkeypatch):
    apis = ["Sandbox.create", "Sandbox.kill", *CASES[case]]
    with record_scenario("e2b", style, provider, shim, "import-only-lifecycle-" + case, apis):
        for name in ("E2B_API_KEY", "ARKER_API_KEY", "COMPAT_COVERAGE_EVIDENCE", "COMPAT_COVERAGE_RUN"):
            assert os.environ.get(name, "").strip(), f"Missing live prerequisite: {name}"
        assert any(os.environ.get(name) for name in ("ARKER_SOURCE", "ARKER_SOURCE_VM", "ARKER_SOURCE_VM_ID")), (
            "An isolated source is required"
        )
        if provider == "arker":
            monkeypatch.delenv("E2B_API_KEY")

        def assert_deadline(sandbox_id, present):
            # This backend invariant supplements the unchanged vendor program.
            # Observe via the control API, never a guest command that could wake it.
            if provider == "arker":
                from arker import Arker

                current = Arker().vm(sandbox_id).refresh()
                assert current.policies is not None, "VM response omitted effective policies"
                actual = "computesdk-e2b-lifetime" in (current.policies.rules or {})
                assert actual is present, "Running deadline must exist before pause and remain absent afterward"

        async def assert_deadline_async(sandbox_id, present):
            await asyncio.to_thread(assert_deadline, sandbox_id, present)

        source = ASYNC_PROGRAM if style == "async" else PROGRAM
        if shim:
            # Exception imports deliberately stay native to verify class identity.
            source = source.replace("from e2b import Sandbox\n", "from arker.e2b import Sandbox\n")
            source = source.replace("from e2b import AsyncSandbox\n", "from arker.e2b import AsyncSandbox\n")
        namespace = {
            "case": case,
            "assert_deadline": assert_deadline_async if style == "async" else assert_deadline,
        }
        exec(compile(source, __file__, "exec"), namespace)
        if style == "async":
            asyncio.run(namespace["program"]())
