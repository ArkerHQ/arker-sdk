import asyncio
from types import SimpleNamespace

import pytest

from arker import _modal_lifecycle, modal
from arker._compat import UnsupportedCompatibilityOption
from arker.computer import VM, _server_error


def response(reason=None, vm_id="01ARZ3NDEKTSV4RRFFQ69G5FAV"):
    return {
        "vm_id": vm_id,
        "state": "running" if reason is None else "terminated",
        "termination_reason": reason,
        "terminated_at": None if reason is None else "2026-01-01T00:00:00Z",
    }


def absent(status=404):
    return _server_error(
        {
            "code": "not_found",
            "message": "Missing",
            "details": {"resource": "vm"},
            "timestamp": "2026-01-01T00:00:00Z",
            "request_id": "test",
            "request": {"kind": "matched", "operation_id": "getVmLifecycle"},
        },
        status,
    )


def sandbox(replies):
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    vm = VM(SimpleNamespace(_request=request, base_url="https://fixture.invalid"), "01ARZ3NDEKTSV4RRFFQ69G5FAV")
    return modal.Sandbox(vm=vm), calls


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("reason,code", [("deadline", 124), ("requested", 137)])
def test_poll_uses_recorded_result_and_native_wait_errors(asynchronous, reason, code):
    native = pytest.importorskip("modal.exception")
    box, calls = sandbox([response(reason), response(reason)])
    assert (asyncio.run(box.poll.aio()) if asynchronous else box.poll()) == code
    assert box.returncode == code
    expected = native.SandboxTimeoutError if reason == "deadline" else native.SandboxTerminatedError
    with pytest.raises(expected) as caught:
        asyncio.run(box.wait.aio()) if asynchronous else box.wait()
    assert type(caught.value) is expected
    assert len(calls) == 2
    assert all(path.endswith("/lifecycle?include_terminated=true") for _, path, _ in calls)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_wait_can_accept_explicit_termination(asynchronous):
    box, _ = sandbox([response("requested")])
    assert (asyncio.run(box.wait.aio(raise_on_termination=False)) if asynchronous else box.wait(False)) is None
    assert box.returncode == 137


@pytest.mark.parametrize("asynchronous", [False, True])
def test_wait_duplicate_option_rejected_before_lookup(asynchronous):
    box, calls = sandbox([])
    with pytest.raises(TypeError):
        if asynchronous:
            asyncio.run(box.wait.aio(True, raise_on_termination=False))
        else:
            box.wait(True, raise_on_termination=False)
    assert not calls


@pytest.mark.parametrize("label", ["foreign", "nonexistent", "purged"])
def test_retained_lookup_never_constructs_handle_on_missing(monkeypatch, label):
    native = pytest.importorskip("modal.exception")
    box, calls = sandbox([absent()])
    monkeypatch.setattr(_modal_lifecycle, "arker_client", lambda _: box._vm._client)
    with pytest.raises(native.NotFoundError) as caught:
        _modal_lifecycle.retained("01ARZ3NDEKTSV4RRFFQ69G5FAV", {})
    assert type(caught.value) is native.NotFoundError
    assert len(calls) == 1


@pytest.mark.parametrize("reason", ["deadline", "requested", "unknown"])
def test_from_id_requires_retained_server_proof(monkeypatch, reason):
    box, calls = sandbox([response(reason)])
    monkeypatch.setattr(modal, "arker_get", lambda *_: None)
    monkeypatch.setattr(_modal_lifecycle, "arker_client", lambda _: box._vm._client)
    found = modal.Sandbox.from_id("01ARZ3NDEKTSV4RRFFQ69G5FAV", arker={"api_key": "test-only"})
    assert found.object_id == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert len(calls) == 1
    if reason == "unknown":
        with pytest.raises(UnsupportedCompatibilityOption):
            _ = found.returncode
    else:
        assert found.returncode == (124 if reason == "deadline" else 137)


@pytest.mark.parametrize("reply", [response("unknown"), {}, response("deadline", "foreign-id")])
def test_unknown_or_malformed_result_never_invents_exit_code(reply):
    box, _ = sandbox([reply])
    with pytest.raises((UnsupportedCompatibilityOption, ValueError)):
        box.poll()


def test_wait_observes_real_transition(monkeypatch):
    box, calls = sandbox([response(), response("requested")])
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    box.wait(raise_on_termination=False)
    assert len(calls) == 2
    assert sleeps == [1]


def test_async_wait_cancellation_stops_polling(monkeypatch):
    box, calls = sandbox([response()])
    entered = asyncio.Event()

    async def sleeping(_):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(_modal_lifecycle, "completed", lambda *_: pytest.fail("No terminal result"))
    monkeypatch.setattr(asyncio, "sleep", sleeping)

    async def run():
        task = asyncio.create_task(box.wait.aio())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert len(calls) == 1


def test_transport_failures_propagate_without_terminal_state():
    error = RuntimeError("transport")
    box, calls = sandbox([error])
    with pytest.raises(RuntimeError) as caught:
        box.poll()
    assert caught.value is error
    assert box.returncode is None
    assert len(calls) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
def test_terminate_wait_uses_recorded_outcome(asynchronous):
    box, calls = sandbox([{"deleted": True}, response("requested")])
    assert (asyncio.run(box.terminate.aio(wait=True)) if asynchronous else box.terminate(wait=True)) == 137
    assert calls[0][0] == "DELETE"
    assert calls[1][0] == "GET"
    assert box.returncode == 137
