import asyncio
from types import SimpleNamespace

import pytest

from arker import _modal_defaults
from arker.computer import _server_error
from arker.modal import Sandbox


def missing(resource="vm", status=404):
    return _server_error(
        {
            "code": "not_found",
            "message": "Unavailable",
            "details": {"resource": resource},
            "timestamp": "2026-01-01T00:00:00Z",
            "request_id": "test",
            "request": {"kind": "matched", "operation_id": "createRun"},
        },
        status,
    )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("deleted", [False, True])
def test_exec_maps_native_shutdown_phase_without_retry(monkeypatch, asynchronous, deleted):
    native = pytest.importorskip("modal.exception")
    error = missing()
    reads = []
    attempts = []

    def refresh():
        reads.append("refresh")
        if deleted:
            raise missing()
        return SimpleNamespace(state="idle")

    def prepare(*args):
        attempts.append("prepare")
        raise error

    monkeypatch.setattr(_modal_defaults, "prepare_exec", prepare)
    box = Sandbox(vm=SimpleNamespace(id="owned", refresh=refresh))
    expected = native.NotFoundError if deleted else native.ConflictError
    with pytest.raises(expected) as caught:
        if asynchronous:
            asyncio.run(box.exec.aio("true"))
        else:
            box.exec("true")
    assert type(caught.value) is expected
    assert caught.value.__cause__ is error
    assert attempts == ["prepare"]
    assert reads == ["refresh"]


@pytest.mark.parametrize("error", [missing("file"), missing(status=403), RuntimeError("transport")])
def test_exec_does_not_reclassify_other_errors(monkeypatch, error):
    def prepare(*args):
        raise error

    def refresh():
        pytest.fail("Unrelated failures must not trigger a VM lookup")

    monkeypatch.setattr(_modal_defaults, "prepare_exec", prepare)
    with pytest.raises(type(error)) as caught:
        Sandbox(vm=SimpleNamespace(id="owned", refresh=refresh)).exec("true")
    assert caught.value is error


def test_exec_failed_phase_lookup_keeps_primary_and_cleanup_cause(monkeypatch):
    primary = missing()
    cleanup = RuntimeError("cleanup failure")

    def prepare(*args):
        raise primary from cleanup

    def refresh():
        raise RuntimeError("lookup transport failure")

    monkeypatch.setattr(_modal_defaults, "prepare_exec", prepare)
    with pytest.raises(type(primary)) as caught:
        Sandbox(vm=SimpleNamespace(id="owned", refresh=refresh)).exec("true")
    assert caught.value is primary
    assert primary.__cause__ is cleanup


def test_minimal_install_preserves_original_error_without_phase_lookup(monkeypatch):
    from arker import _modal_errors

    error = missing()
    monkeypatch.setattr(_modal_errors, "import_optional", lambda name: None)
    monkeypatch.setattr(_modal_defaults, "prepare_exec", lambda *args: (_ for _ in ()).throw(error))
    with pytest.raises(type(error)) as caught:
        Sandbox(vm=SimpleNamespace(id="owned", refresh=lambda: pytest.fail("no native error class"))).exec("true")
    assert caught.value is error


def test_native_route_keeps_original_exception_identity():
    error = missing()

    def execute(*args, **kwargs):
        raise error

    with pytest.raises(type(error)) as caught:
        Sandbox(native=SimpleNamespace(exec=execute)).exec("true")
    assert caught.value is error
