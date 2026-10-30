import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_e2b_urls import DESCRIPTOR, delegation_response

from arker._compat_e2b_urls import E2BFileURLs, prepare_file_urls, prepare_file_urls_async
from arker.computer import ArkerError
from arker.generated.api_models import MatchedRequestContext, UnsupportedOperation, UnsupportedOperationDetails


def unsupported():
    body = UnsupportedOperation(
        code="unsupported_operation",
        message="File URLs are unavailable",
        timestamp="2026-01-01T00:00:00Z",
        request_id="test",
        request=MatchedRequestContext("matched", "createFileTransferDelegation"),
        details=UnsupportedOperationDetails("feature_not_supported"),
    )
    return ArkerError(body.code, body.message, 422, body=body)


def vm_fixture(error):
    calls = []

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        raise error

    vm = SimpleNamespace(id="vm", base_url="https://example.test/api", _client=SimpleNamespace(_request=request))
    return vm, calls


def test_repeated_unsupported_urls_keep_tracebacks_bounded():
    error = unsupported()
    body = error.body
    vm, calls = vm_fixture(error)
    prepare_file_urls(vm)
    urls = E2BFileURLs(vm, descriptor=DESCRIPTOR)
    depths = []
    for _ in range(50):
        try:
            urls.download_url("file")
        except ArkerError as caught:
            assert caught is error
            assert caught.body is body
            depth = 0
            traceback = caught.__traceback__
            while traceback is not None:
                depth += 1
                traceback = traceback.tb_next
            depths.append(depth)
        else:
            pytest.fail("unsupported URL unexpectedly succeeded")
    assert depths[0] > 0
    assert len(set(depths)) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
def test_unsupported_is_retained_exactly_and_reconnect_retries(asynchronous):
    error = unsupported()
    vm, calls = vm_fixture(error)
    prepare = lambda: asyncio.run(prepare_file_urls_async(vm)) if asynchronous else prepare_file_urls(vm)
    prepare()
    urls = E2BFileURLs(vm, descriptor=DESCRIPTOR)
    for method in (urls.download_url, urls.upload_url):
        with pytest.raises(ArkerError) as caught:
            method("file")
        assert caught.value is error
    assert len(calls) == 1
    assert not hasattr(vm._e2b_file_delegation, "_key")
    with pytest.raises(AttributeError):
        vm._e2b_file_delegation.error = RuntimeError()

    def supported(*args, **kwargs):
        calls.append((args, kwargs))
        return delegation_response()

    vm._client._request = supported
    prepare()
    assert len(calls) == 2
    assert urls.download_url("file").startswith("https://regional.example.test/")
    assert len(calls) == 2


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "mismatch",
    [
        401,
        403,
        404,
        409,
        429,
        500,
        503,
        "code",
        "body_code",
        "reason",
        "kind",
        "operation",
        "request",
        "details",
        "body",
        "untyped",
        "transport",
    ],
)
def test_every_nonmatching_failure_propagates_unchanged(mismatch, asynchronous):
    error = unsupported()
    if isinstance(mismatch, int):
        error.status = mismatch
    elif mismatch == "code":
        error.code = "operation_failed"
    elif mismatch == "body_code":
        error.body = replace(error.body, code="operation_failed")
    elif mismatch == "reason":
        error.body = replace(error.body, details=UnsupportedOperationDetails("platform_mismatch"))
    elif mismatch in ("kind", "operation"):
        error.body = replace(
            error.body,
            request=MatchedRequestContext(
                "unmatched" if mismatch == "kind" else "matched",
                "sync" if mismatch == "operation" else "createFileTransferDelegation",
            ),
        )
    elif mismatch in ("request", "details"):
        error.body = replace(error.body, **{mismatch: None})
    elif mismatch == "body":
        error.body = None
    elif mismatch == "untyped":
        error.body = {
            "code": "unsupported_operation",
            "details": {"reason": "feature_not_supported"},
            "request": {"kind": "matched", "operation_id": "createFileTransferDelegation"},
        }
    else:
        error = RuntimeError("transport failed")
    vm, calls = vm_fixture(error)
    with pytest.raises(type(error)) as caught:
        if asynchronous:
            asyncio.run(prepare_file_urls_async(vm))
        else:
            prepare_file_urls(vm)
    assert caught.value is error
    assert len(calls) == 1
    assert not hasattr(vm, "_e2b_file_delegation")


@pytest.mark.parametrize("asynchronous", [False, True])
def test_basic_create_and_commands_continue_when_only_file_urls_are_unsupported(monkeypatch, asynchronous):
    import arker._compat_e2b_bootstrap as bootstrap
    import arker._compat_e2b_data_plane as data_plane
    import arker.e2b as shim

    error = unsupported()
    vm, calls = vm_fixture(error)
    vm._client._api_key = "test-key"
    monkeypatch.setattr(shim, "routed_create", lambda *args, creation_key=None: ("arker", vm))
    monkeypatch.setattr(bootstrap, "bootstrap_envd", lambda *args, **kwargs: (DESCRIPTOR, {"ready": True}))
    plane = SimpleNamespace(commands=SimpleNamespace(run=lambda command: command), files=object(), git=object())
    monkeypatch.setattr(data_plane, "create_data_plane", lambda *args, **kwargs: plane)
    sandbox = asyncio.run(shim.AsyncSandbox.create()) if asynchronous else shim.Sandbox.create()
    assert sandbox.commands.run("echo ready") == "echo ready"
    with pytest.raises(ArkerError) as caught:
        sandbox.download_url("file")
    assert caught.value is error
    assert len(calls) == 1
