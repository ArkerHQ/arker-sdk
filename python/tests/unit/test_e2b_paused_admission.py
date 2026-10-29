import asyncio
from types import SimpleNamespace

import pytest
from e2b import SandboxNotRunningException

from arker import e2b
from arker._compat_e2b_errors import translate_error
from arker.computer import ArkerError


def refusal(*, status=409, code="invalid_state", requirement="explicit_resume", kind="vm"):
    return ArkerError(
        code,
        "explicit resume required",
        status,
        body={"code": code, "details": {"resource": {"kind": kind, "id": "vm-paused"}, "requirement": requirement}},
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"status": 404},
        {"status": 503},
        {"code": "conflict"},
        {"requirement": "running"},
        {"requirement": "mutable"},
        {"kind": "session"},
    ],
)
def test_other_backend_errors_keep_identity(changes):
    error = refusal(**changes)
    assert translate_error(error) is error


def test_typed_backend_details_are_supported():
    error = refusal()
    error.body = SimpleNamespace(
        details=SimpleNamespace(resource=SimpleNamespace(kind="vm"), requirement="explicit_resume")
    )
    assert isinstance(translate_error(error), SandboxNotRunningException)


def test_missing_details_and_native_errors_are_not_inferred():
    error = ArkerError("invalid_state", "explicit resume required", 409)
    assert translate_error(error) is error
    native = SandboxNotRunningException("native marker")
    assert translate_error(native) is native


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_route_errors_keep_identity_even_if_they_resemble_arker_refusals(asynchronous):
    error = refusal()

    def run(*args, **kwargs):
        raise error

    async def async_run(*args, **kwargs):
        raise error

    native = SimpleNamespace(sandbox_id="native", commands=SimpleNamespace(run=async_run if asynchronous else run))
    sandbox = e2b.Sandbox(native=native)
    if asynchronous:
        sandbox = e2b.AsyncSandbox(sandbox)
    with pytest.raises(ArkerError) as raised:
        result = sandbox.commands.run("true")
        if asynchronous:
            asyncio.run(result)
    assert raised.value is error
