from __future__ import annotations

import asyncio
import copy
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("cleanup_coverage", ROOT / "scripts/compat_coverage.py")
coverage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coverage)
sys.path.insert(0, str(ROOT / "tests/e2e"))
from compat_evidence import ModalCleanup  # noqa: E402


def events(receipt):
    return [
        {
            "provider": "modal",
            "run": "run",
            "scenario": "cleanup",
            "route": route,
            "outcome": "passed",
            "vendor_version": "1",
            "implementation_fingerprint": "code",
            "apis": ["Sandbox.create"],
            "cleanup": receipt,
        }
        for route in ("reference", "native", "arker")
    ]


def verified(items):
    return coverage.verified_scenarios("Sandbox.create", items, "1", "code")


def test_acknowledgements_and_historical_passes_have_no_cleanup_credit():
    for receipt in (None, True, {"verified": True}, {"cleanupRequested": True}):
        assert verified(events(receipt)) == []


def test_modal_readback_is_required_for_every_enrolled_resource():
    cleanup = ModalCleanup()
    cleanup.allocated("one")
    cleanup.allocated("two")
    calls = []

    async def lookup(identifier):
        calls.append(("lookup", identifier))
        return SimpleNamespace(object_id=identifier)

    async def poll(box):
        calls.append(("poll", box.object_id))
        return 137

    asyncio.run(cleanup.observe("one", lookup, poll))
    assert verified(events(cleanup.receipt())) == []
    asyncio.run(cleanup.observe("two", lookup, poll))
    assert verified(events(cleanup.receipt())) == ["run:cleanup"]
    assert calls == [("lookup", "one"), ("poll", "one"), ("lookup", "two"), ("poll", "two")]
    historical = events(cleanup.receipt())
    historical.append({**historical[0], "outcome": "failed", "apis": [], "cleanup": None})
    original = copy.deepcopy(historical)
    assert verified(historical) == []
    assert historical == original
    assert coverage.verified_scenarios("Sandbox.create", events(cleanup.receipt()), "1", "other") == []
    assert coverage.verified_scenarios("Sandbox.create", events(cleanup.receipt()), "1", "code", "()") == []


@pytest.mark.parametrize("result", [None, True, "137", 137.5])
def test_modal_poll_running_or_invalid_never_qualifies(result):
    cleanup = ModalCleanup()
    cleanup.allocated("one")

    async def lookup(identifier):
        return SimpleNamespace(object_id=identifier)

    async def poll(box):
        return result

    with pytest.raises(RuntimeError, match="not been observed"):
        asyncio.run(cleanup.observe("one", lookup, poll))
    assert verified(events(cleanup.receipt())) == []


@pytest.mark.parametrize("failure", [RuntimeError("transport"), PermissionError("denied"), asyncio.CancelledError()])
def test_failed_readback_preserves_error_and_invalidates_previous_observation(failure):
    cleanup = ModalCleanup()
    cleanup.allocated("one")

    async def lookup(identifier):
        return SimpleNamespace(object_id=identifier)

    async def poll(box):
        return 0

    asyncio.run(cleanup.observe("one", lookup, poll))

    async def failed(identifier):
        raise failure

    async def verify_failure():
        with pytest.raises(type(failure)) as caught:
            await cleanup.observe("one", failed, poll)
        assert caught.value is failure

    asyncio.run(verify_failure())
    assert verified(events(cleanup.receipt())) == []


def test_wrong_identity_unenrolled_and_duplicate_resources_are_rejected():
    cleanup = ModalCleanup()
    cleanup.allocated("one")

    async def lookup(identifier):
        return SimpleNamespace(object_id="other")

    async def poll(box):
        pytest.fail("wrong identity must not be polled")

    with pytest.raises(RuntimeError, match="different"):
        asyncio.run(cleanup.observe("one", lookup, poll))
    with pytest.raises(ValueError, match="enrolled"):
        asyncio.run(cleanup.observe("two", lookup, poll))
    with pytest.raises(ValueError, match="new sandbox"):
        cleanup.allocated("one")
    assert verified(events(cleanup.receipt())) == []


def test_missing_resource_or_wrong_provider_receipt_cannot_qualify():
    receipt = {
        "protocol": "modal-terminal-v1",
        "resources": ["one", "two"],
        "observations": [{"id": "one", "operation": "Sandbox.from_id.poll", "returncode": 0}],
    }
    assert verified(events(receipt)) == []
    receipt["resources"] = ["one"]
    for field, value in (("provider", "e2b"), ("cleanup", {**receipt, "resources": []})):
        assert verified([{**item, field: value} for item in events(receipt)]) == []


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("cleanup_running", [False, True])
def test_actual_terminal_program_waits_for_readback_before_recording(monkeypatch, asynchronous, cleanup_running):
    import compat_evidence
    import test_modal_terminal_import_only as scenario

    class Terminated(Exception):
        pass

    class NotFound(Exception):
        pass

    calls = []

    def method(fn):
        async def aio(*args, **kwargs):
            return fn(*args, **kwargs)

        fn.aio = aio
        return fn

    polls = iter([None, 137, None if cleanup_running else 137])

    @method
    def poll():
        result = next(polls)
        calls.append(("poll", result))
        return result

    @method
    def terminate(**kwargs):
        calls.append(("terminate", kwargs))
        return 137

    @method
    def wait():
        raise Terminated()

    @method
    def execute(*args):
        raise NotFound()

    box = SimpleNamespace(object_id="box", poll=poll, terminate=terminate, wait=wait, exec=execute)

    @method
    def create(**kwargs):
        return box

    @method
    def lookup(identifier):
        assert identifier == "box"
        calls.append(("lookup", identifier))
        return box

    monkeypatch.setitem(
        sys.modules,
        "modal",
        SimpleNamespace(
            Sandbox=SimpleNamespace(create=create, from_id=lookup),
            App=SimpleNamespace(lookup=lambda *a, **kw: None),
            Image=SimpleNamespace(from_registry=lambda *a: None),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "modal.exception",
        SimpleNamespace(NotFoundError=NotFound, SandboxTerminatedError=Terminated, SandboxTimeoutError=TimeoutError),
    )
    namespace = {}
    exec(compile(scenario.PROGRAM, "actual-terminal-program", "exec"), namespace)
    records = []
    monkeypatch.setattr(compat_evidence, "record", lambda *args, **kwargs: records.append((args, kwargs)))
    cleanup = ModalCleanup()

    def run():
        with compat_evidence.record_scenario(
            "modal",
            "async" if asynchronous else "sync",
            "modal",
            False,
            "terminal",
            ["Sandbox.create"],
            cleanup=cleanup,
        ):
            asyncio.run(namespace["program"](asynchronous, "requested", cleanup))

    if cleanup_running:
        with pytest.raises(RuntimeError, match="not been observed"):
            run()
    else:
        run()
    args, kwargs = records[0]
    assert args[-1] == ("failed" if cleanup_running else "passed")
    assert calls[-3:] == [("terminate", {"wait": True}), ("lookup", "box"), ("poll", None if cleanup_running else 137)]
    assert coverage.cleanup_observed({"provider": "modal", "cleanup": kwargs["cleanup"]}) is not cleanup_running
