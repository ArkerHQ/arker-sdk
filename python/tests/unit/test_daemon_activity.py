import json
from types import SimpleNamespace

import pytest

from arker import _compat_daemon_activity as module

IDENTITY = {"pid": 17, "start_ticks": 1234, "boot_id": "00000000-0000-0000-0000-000000000001"}


def fixture(monkeypatch, *, identity=None, failure=None, final="completed"):
    events = []
    ack = []
    ready = {"ready": True, "version": "test", "activity": identity if identity is not None else IDENTITY}
    polls = 0

    def request(method, path, body, **kwargs):
        events.append(("register", method, path, body, kwargs))
        assert not ack
        assert body == {**IDENTITY, "intent_version": 1}
        if failure:
            raise failure

    def sync(vm, path, content):
        events.append(("ack", json.loads(content)))
        ack.append(json.loads(content))

    def run(command, **kwargs):
        events.append(("run", kwargs))
        assert kwargs["time_to_background"] == 0
        return SimpleNamespace(type="background", run_id="test-run")

    def get_run(run_id):
        nonlocal polls
        polls += 1
        assert run_id == "test-run"
        events.append(("poll", bool(ack)))
        if polls == 1:
            return SimpleNamespace(state="running", stdout='{"ready":')
        return SimpleNamespace(state=final if ack else "running", stdout=json.dumps(ready) + "\n", exit_code=0)

    monkeypatch.setattr(module, "secret_sync", sync)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    vm = SimpleNamespace(
        id="test",
        base_url="https://custom.example/api",
        run=run,
        get_run=get_run,
        _client=SimpleNamespace(_request=request),
    )
    return vm, events, ack


def test_registers_before_releasing_guard_and_waits_for_completion(monkeypatch):
    vm, events, ack = fixture(monkeypatch)
    result = module.registered_bootstrap(vm, "bootstrap", "/private/request.json", "test")
    assert result["activity"] == IDENTITY
    registration = next(event for event in events if event[0] == "register")
    assert registration[1:4] == ("POST", "/v1/vms/test/daemon-activity", {**IDENTITY, "intent_version": 1})
    assert registration[4]["base_url"] == vm.base_url
    assert ack == [{"registered": True}]
    assert events[-1] == ("poll", True)


@pytest.mark.parametrize("failure", [RuntimeError("unsupported_operation"), RuntimeError("stale identity")])
def test_registration_rejection_releases_without_exposing_ready(monkeypatch, failure):
    vm, _, ack = fixture(monkeypatch, failure=failure)
    with pytest.raises(RuntimeError) as result:
        module.registered_bootstrap(vm, "bootstrap", "/private/request.json", "test")
    assert result.value is failure
    assert ack == [{"registered": False}]


@pytest.mark.parametrize(
    "patch", [{"pid": True}, {"pid": 1}, {"start_ticks": -1}, {"start_ticks": 2**63}, {"boot_id": "bad"}]
)
def test_invalid_identity_never_calls_registration(monkeypatch, patch):
    vm, events, ack = fixture(monkeypatch, identity={**IDENTITY, **patch})
    with pytest.raises((RuntimeError, ValueError)):
        module.registered_bootstrap(vm, "bootstrap", "/private/request.json", "test")
    assert not any(event[0] == "register" for event in events)
    assert ack == [{"registered": False}]


def test_successful_registration_requires_successful_guard_completion(monkeypatch):
    vm, _, _ = fixture(monkeypatch, final="failed")
    with pytest.raises(RuntimeError, match="after registration"):
        module.registered_bootstrap(vm, "bootstrap", "/private/request.json", "test")


def test_exited_bootstrap_never_registers_from_retained_output(monkeypatch):
    vm, events, ack = fixture(monkeypatch)
    vm.get_run = lambda _: SimpleNamespace(
        state="completed", stdout=json.dumps({"ready": True, "version": "test", "activity": IDENTITY}) + "\n"
    )
    with pytest.raises(RuntimeError, match="released its activity guard"):
        module.registered_bootstrap(vm, "bootstrap", "/private/request.json", "test")
    assert not any(event[0] == "register" for event in events)
    assert ack == [{"registered": False}]


@pytest.mark.parametrize("accepted,alive", [(False, True), (True, True), (True, False)])
def test_guest_acknowledgement_is_required_before_return(tmp_path, monkeypatch, capsys, accepted, alive):
    import importlib.util
    import sys
    from pathlib import Path

    security = SimpleNamespace(
        BootstrapError=RuntimeError, load=lambda path: json.loads(path.read_text()), same_process=lambda _: alive
    )
    monkeypatch.setitem(sys.modules, "_guest_e2b_envd", security)
    source = Path(__file__).parents[2] / "src/arker/_guest_blaxel.py"
    spec = importlib.util.spec_from_file_location("guest_activity_handshake", source)
    guest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guest)
    request = tmp_path / "request.json"
    ack = request.with_suffix(".registered")
    ack.write_text(json.dumps({"registered": accepted}))
    state = {"pid": 17, "start": "1234"}
    ready = {"ready": True, "version": "test"}
    if accepted and alive:
        assert guest.register_activity(request, state, ready) is ready
    else:
        with pytest.raises(RuntimeError, match="registration_failed" if not accepted else "identity_mismatch"):
            guest.register_activity(request, state, ready)
    assert not ack.exists()
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["activity"]["pid"] == 17
    assert emitted["activity"]["start_ticks"] == 1234


@pytest.mark.parametrize("where", ["poll", "ack", "cancel"])
def test_failure_keeps_primary_when_negative_ack_also_fails(monkeypatch, where):
    import asyncio

    primary = asyncio.CancelledError() if where == "cancel" else RuntimeError("primary failure")
    cleanup = RuntimeError("negative acknowledgement failed")
    vm, _, _ = fixture(monkeypatch)
    if where in ("poll", "cancel"):

        def fail_poll(_):
            raise primary

        vm.get_run = fail_poll

    def fail_sync(vm, path, body):
        raise primary if json.loads(body)["registered"] else cleanup

    monkeypatch.setattr(module, "secret_sync", fail_sync)
    with pytest.raises(BaseException) as result:
        module.registered_bootstrap(vm, "bootstrap", "/private/request.json", "test")
    assert result.value is primary
    assert result.value.cleanup_error is cleanup
