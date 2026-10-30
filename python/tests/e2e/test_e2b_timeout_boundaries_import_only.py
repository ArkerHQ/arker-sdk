"""Operation-specific timeout boundaries for the tested 24-hour account profile."""

import asyncio
import os

import pytest
from compat_evidence import record_scenario
from compat_program import async_program

PROGRAM = r"""
from e2b import Sandbox
from e2b import SandboxException, SandboxNotFoundException

sandbox = None
try:
    if operation == "create":
        try:
            sandbox = Sandbox.create(timeout=seconds)
        except SandboxException as error:
            assert type(error) is SandboxException and error.status_code == 400
        else:
            raise AssertionError("Invalid create timeout was accepted")
    else:
        sandbox = Sandbox.create()
        if operation == "connect-paused":
            assert sandbox.pause() is True
        before = sandbox.get_info()
        if operation.startswith("connect"):
            try:
                Sandbox.connect(sandbox.sandbox_id, timeout=seconds)
            except SandboxException as error:
                assert type(error) is SandboxException and error.status_code == 400
            else:
                raise AssertionError("Invalid connect timeout was accepted")
            after = sandbox.get_info()
            assert (after.state, after.started_at, after.end_at) == (before.state, before.started_at, before.end_at)
        elif seconds < 0:
            try:
                sandbox.set_timeout(seconds)
            except SandboxException as error:
                assert type(error) is SandboxException and error.status_code == 400
            else:
                raise AssertionError("Negative setter timeout was accepted")
            assert sandbox.get_info().end_at == before.end_at
        elif seconds == 0:
            assert sandbox.set_timeout(0) is None
            try:
                sandbox.get_info()
            except SandboxNotFoundException as error:
                assert type(error) is SandboxNotFoundException
            else:
                raise AssertionError("Zero timeout returned before expiration")
            assert sandbox.is_running() is False
            assert sandbox.kill() is False
        else:
            assert sandbox.set_timeout(seconds) is None
            after = sandbox.get_info()
            assert after.started_at == before.started_at
            assert abs((after.end_at - after.started_at).total_seconds() - 86400) <= 0.001
finally:
    if sandbox is not None:
        sandbox.kill()
"""
ASYNC_PROGRAM = async_program(PROGRAM, rename={"Sandbox": "AsyncSandbox"}, targets={"AsyncSandbox", "sandbox"})
CASES = [
    (operation, seconds) for operation in ("create", "connect-running", "connect-paused") for seconds in (0, 86401)
]
CASES += [("setter", seconds) for seconds in (-1, 0, 86401)]


@pytest.mark.parametrize("operation,seconds", CASES)
@pytest.mark.parametrize("style", ["sync", "async"])
@pytest.mark.parametrize(
    "provider,shim", [("e2b", False), ("e2b", True), ("arker", True)], ids=["reference", "native", "arker"]
)
def test_timeout_boundaries(operation, seconds, style, provider, shim, live_router, monkeypatch):
    for key in ("E2B_API_KEY", "ARKER_API_KEY", "COMPAT_COVERAGE_RUN", "COMPAT_COVERAGE_EVIDENCE"):
        assert os.environ.get(key), f"Missing live prerequisite: {key}"
    apis = ["Sandbox.create"]
    if operation != "create":
        apis += ["Sandbox.kill", "Sandbox.get_info"]
        apis += ["Sandbox.connect"] if operation.startswith("connect") else ["Sandbox.set_timeout"]
        if operation == "connect-paused":
            apis += ["Sandbox.pause"]
        if operation == "setter" and seconds == 0:
            apis += ["Sandbox.is_running"]
    if provider == "arker":
        monkeypatch.delenv("E2B_API_KEY")
    source = ASYNC_PROGRAM if style == "async" else PROGRAM
    if shim:
        source = source.replace("from e2b import Sandbox\n", "from arker.e2b import Sandbox\n")
        source = source.replace("from e2b import AsyncSandbox\n", "from arker.e2b import AsyncSandbox\n")
    with record_scenario("e2b", style, provider, shim, f"timeout-boundary-{operation}-{seconds}", apis):
        namespace = {"operation": operation, "seconds": seconds}
        exec(compile(source, __file__, "exec"), namespace)
        if style == "async":
            asyncio.run(namespace["program"]())
