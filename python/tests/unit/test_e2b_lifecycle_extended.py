import asyncio
import copy
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from arker import _compat_e2b_lifecycle as lifecycle
from arker._compat import UnsupportedCompatibilityOption
from arker.computer import ArkerError

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def vm(monkeypatch):
    rules = {
        "custom": {"type": "outbound", "action": "deny", "priority": 1},
        lifecycle.LIFETIME_POLICY_ID: {"type": "schedule", "action": "delete", "after_created": 300},
    }
    policies = {"rules": rules, "secrets": {"EXAMPLE": "test-value"}}
    calls = []
    vm = SimpleNamespace(id="sandbox", state="running", created_at=NOW.isoformat(), policies=policies, calls=calls)
    vm.refresh = lambda **kwargs: vm

    def update(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        rules.update(kwargs["policies"]["rules"])

    vm.requests = []
    vm.base_url = "https://worker.example"
    vm.lifecycle_response = {"vm_id": vm.id, "state": "running", "mode": None, "customer_paused": False}

    def request(method, path, body=None, **kwargs):
        vm.requests.append((method, path, body, kwargs))
        if method == "GET":
            return vm.lifecycle_response
        if path.endswith("/deadline"):
            if vm.lifecycle_response["customer_paused"]:
                raise ArkerError(
                    "invalid_state",
                    "paused",
                    409,
                    body={"details": {"requirement": "explicit_resume", "resource": {"kind": "vm", "id": vm.id}}},
                )
            return {"vm_id": vm.id, "deadline_at": (NOW + timedelta(seconds=body["timeout_seconds"])).isoformat()}
        return {
            "vm_id": vm.id,
            "state": "paused" if path.endswith("/pause") else "running",
            "mode": body.get("mode", "full"),
            "already_paused": False,
        }

    vm._client = SimpleNamespace(_request=request)
    vm.update = update
    vm.delete = lambda: pytest.fail("lifecycle must not delete")
    vm.run = lambda *a, **k: pytest.fail("inspection must not wake a VM")
    return vm


@pytest.mark.parametrize("seconds", [1, 17, 300])
def test_timeout_uses_atomic_endpoint_without_policy_patch_or_resume(vm, seconds):
    original = copy.deepcopy(vm.policies)
    vm.refresh = lambda **kwargs: pytest.fail("must not preflight or activate")
    lifecycle.Lifecycle(vm=vm).set_timeout(seconds)
    assert vm.calls == [] and vm.policies == original
    assert [(method, path, body) for method, path, body, _ in vm.requests] == [
        ("POST", "/v1/vms/sandbox/deadline", {"policy_id": lifecycle.LIFETIME_POLICY_ID, "timeout_seconds": seconds})
    ]


@pytest.mark.parametrize(
    "seconds,exception",
    [(True, TypeError), (1.5, TypeError), ("1", TypeError), (0, ValueError), (-1, ValueError), (86401, ValueError)],
)
def test_timeout_validation_before_mutation(vm, seconds, exception):
    with pytest.raises(exception):
        lifecycle.Lifecycle(vm=vm).set_timeout(seconds)
    assert vm.calls == []


@pytest.mark.parametrize("timeout", [None, 1, 100, 300, 400])
def test_connect_sends_atomic_deadline_without_policy_patch(vm, timeout):
    assert lifecycle.Lifecycle(vm=vm).connect(timeout) is vm
    assert vm.requests[0][2] == {
        "deadline": {"policy_id": lifecycle.LIFETIME_POLICY_ID, "timeout_seconds": 300 if timeout is None else timeout}
    }
    assert vm.calls == []


def test_connect_reads_fresh_handle_and_absolute_deadline(vm):
    fresh = SimpleNamespace(
        state="running",
        policies={
            "rules": {
                lifecycle.LIFETIME_POLICY_ID: {
                    "type": "schedule",
                    "action": "delete",
                    "at": (NOW + timedelta(seconds=600)).isoformat(),
                }
            }
        },
    )
    vm.refresh = lambda **kwargs: fresh
    assert lifecycle.Lifecycle(vm=vm).connect(400) is fresh
    assert vm.calls == []


def test_connect_asks_backend_to_create_missing_deadline(vm):
    del vm.policies["rules"][lifecycle.LIFETIME_POLICY_ID]
    lifecycle.Lifecycle(vm=vm).connect(100)
    assert vm.calls == []


@pytest.mark.parametrize("operation", ["pause", "connect"])
def test_backend_deadline_rejection_does_not_patch_or_retry(vm, operation):
    failure = RuntimeError("selected policy is not a delete schedule")
    requests = []

    def reject(*args, **kwargs):
        requests.append(args)
        raise failure

    vm._client._request = reject
    with pytest.raises(RuntimeError) as error:
        getattr(lifecycle.Lifecycle(vm=vm), operation)()
    assert error.value is failure
    assert len(requests) == 1 and vm.calls == []


@pytest.mark.parametrize(
    "method,kwargs",
    [
        ("pause", {"unsupported": 1}),
        ("get_info", {}),
        ("set_timeout", {"timeout": 10, "unsupported": 1}),
    ],
)
def test_unsupported_operations_do_not_mutate(vm, method, kwargs):
    with pytest.raises(UnsupportedCompatibilityOption):
        getattr(lifecycle.Lifecycle(vm=vm), method)(**kwargs)
    assert vm.calls == []


@pytest.mark.parametrize("mode", [None, "full", "filesystem"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_automatic_idle_is_logically_running(vm, mode, asynchronous):
    vm.lifecycle_response["state"] = "idle"
    vm.lifecycle_response["mode"] = mode
    helper = (lifecycle.AsyncLifecycle if asynchronous else lifecycle.Lifecycle)(vm=vm)
    result = helper.is_running()
    assert (asyncio.run(result) if asynchronous else result) is True
    assert len(vm.requests) == 1 and vm.requests[0][:2] == ("GET", "/v1/vms/sandbox/lifecycle")
    assert vm.calls == []


@pytest.mark.parametrize("state,paused", [("idle", True), ("paused", False), ("running", True), ("idle", None)])
def test_inconsistent_pause_state_is_not_guessed(vm, state, paused):
    vm.lifecycle_response.update(state=state, customer_paused=paused)
    with pytest.raises(RuntimeError, match="Inconsistent"):
        lifecycle.Lifecycle(vm=vm).is_running()


@pytest.mark.parametrize("status", [400, 404, 402, 403, 409, 422, 503])
@pytest.mark.parametrize("method", ["pause", "connect", "is_running"])
def test_lifecycle_errors_propagate_without_fallback(vm, status, method):
    del vm.policies["rules"][lifecycle.LIFETIME_POLICY_ID]
    error = ArkerError("request_failed", "request failed", status=status)
    requests = []

    def request(*args, **kwargs):
        requests.append(args)
        raise error

    vm._client._request = request
    if method == "is_running" and status == 404:
        assert lifecycle.Lifecycle(vm=vm).is_running() is False
    else:
        with pytest.raises(ArkerError) as raised:
            getattr(lifecycle.Lifecycle(vm=vm), method)()
        assert raised.value is error
    assert len(requests) == 1
    assert vm.calls == []


@pytest.mark.parametrize(
    "kwargs,mode",
    [
        ({}, "full"),
        ({"mode": "filesystem"}, "filesystem"),
        ({"keep_memory": False}, "filesystem"),
        ({"keep_memory": True}, "full"),
    ],
)
@pytest.mark.parametrize("asynchronous", [False, True])
def test_pause_uses_explicit_route_and_preserves_policy(vm, kwargs, mode, asynchronous):
    del vm.policies["rules"][lifecycle.LIFETIME_POLICY_ID]
    before = copy.deepcopy(vm.policies)
    helper = (lifecycle.AsyncLifecycle if asynchronous else lifecycle.Lifecycle)(vm=vm)
    result = helper.pause(**kwargs)
    assert (asyncio.run(result) if asynchronous else result) is True
    assert vm.requests == [
        ("GET", "/v1/vms/sandbox/lifecycle", None, {"base_url": vm.base_url, "request_timeout": 60}),
        (
            "POST",
            "/v1/vms/sandbox/pause",
            {"mode": mode, "deadline": {"policy_id": lifecycle.LIFETIME_POLICY_ID}},
            {"base_url": vm.base_url, "request_timeout": 60},
        ),
    ]
    assert vm.calls == [] and vm.policies == before


@pytest.mark.parametrize("kwargs", [{"mode": "bad"}, {"mode": "full", "keep_memory": True}, {"keep_memory": 1}])
def test_invalid_pause_options_do_not_send_requests(vm, kwargs):
    from e2b import InvalidArgumentException

    with pytest.raises(InvalidArgumentException):
        lifecycle.Lifecycle(vm=vm).pause(**kwargs)
    assert vm.requests == []


def test_restore_leaves_mode_to_backend_and_reboot_explicitly_selects_filesystem(vm):
    vm.state = "idle"
    helper = lifecycle.Lifecycle(vm=vm)
    helper.connect()
    helper.connect(on_resume="reboot")
    assert [call[2] for call in vm.requests] == [
        {"deadline": {"policy_id": lifecycle.LIFETIME_POLICY_ID, "timeout_seconds": 300}},
        {"mode": "filesystem", "deadline": {"policy_id": lifecycle.LIFETIME_POLICY_ID, "timeout_seconds": 300}},
    ]
    assert all(call[:2] == ("POST", "/v1/vms/sandbox/resume") for call in vm.requests)
    assert vm.calls == []


def test_authoritative_pause_state_and_idempotent_pause(vm):
    del vm.policies["rules"][lifecycle.LIFETIME_POLICY_ID]
    vm.lifecycle_response.update(state="paused", mode="filesystem", customer_paused=True)
    assert lifecycle.Lifecycle(vm=vm).is_running() is False
    assert lifecycle.Lifecycle(vm=vm).pause() is False


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"vm_id": "other", "state": "paused", "mode": "full"},
        {"vm_id": "sandbox", "state": "paused", "mode": "full", "already_paused": "false"},
    ],
)
def test_invalid_pause_response_does_not_report_success(vm, response):
    del vm.policies["rules"][lifecycle.LIFETIME_POLICY_ID]
    vm._client._request = lambda method, *a, **k: vm.lifecycle_response if method == "GET" else response
    with pytest.raises(RuntimeError, match="Invalid Arker"):
        lifecycle.Lifecycle(vm=vm).pause()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "method,kwargs",
    [
        ("pause", {"keep_memory": None, "mode": "filesystem", "request_timeout": 2}),
        ("beta_pause", {"keep_memory": True, "mode": None}),
        ("connect", {"timeout": 20, "on_resume": "restore"}),
        ("set_timeout", {"timeout": 20}),
        ("is_running", {"request_timeout": 2}),
        ("get_info", {}),
    ],
)
@pytest.mark.parametrize("fails", [False, True])
def test_native_preserves_return_and_error_identity(asynchronous, method, kwargs, fails):
    marker = object()
    error = RuntimeError("native failure")

    def call(*args, **actual):
        if args:
            assert method == "set_timeout" and len(args) == 1
            actual["timeout"] = args[0]
        assert actual == kwargs
        if fails:
            raise error
        return marker

    async def async_call(*args, **actual):
        return call(*args, **actual)

    native = SimpleNamespace(**{method: async_call if asynchronous else call})
    adapter = (lifecycle.AsyncLifecycle if asynchronous else lifecycle.Lifecycle)(native=native)

    def invoke():
        result = getattr(adapter, method)(**kwargs)
        return asyncio.run(result) if asynchronous else result

    if fails:
        with pytest.raises(RuntimeError) as raised:
            invoke()
        assert raised.value is error
    else:
        assert invoke() is marker


def test_ten_async_sandbox_timeouts_execute_off_loop_without_shared_state():
    async def run():
        loop_thread = threading.get_ident()
        calls = [[] for _ in range(10)]

        def adapter(index):
            def update(**kwargs):
                assert threading.get_ident() != loop_thread
                calls[index].append(kwargs)

            vm = SimpleNamespace(id=f"vm-{index}", base_url="https://worker.example", update=update, policies=None)

            def request(*args, **kwargs):
                assert threading.get_ident() != loop_thread
                assert args[0] == "POST" and args[1].endswith("/deadline")
                return {
                    "vm_id": vm.id,
                    "deadline_at": (NOW + timedelta(seconds=args[2]["timeout_seconds"])).isoformat(),
                }

            vm._client = SimpleNamespace(_request=request)
            vm.refresh = lambda **kwargs: vm
            return lifecycle.AsyncLifecycle(vm=vm)

        results = await asyncio.gather(*(adapter(i).set_timeout(i + 1) for i in range(10)), return_exceptions=True)
        assert results == [None] * 10
        assert calls == [[] for _ in range(10)]

    asyncio.run(run())


def test_set_timeout_preserves_backend_policy_rejection(vm):
    rejection = ArkerError("invalid_request", "selected rule is not a delete schedule", 400)

    def reject(*args, **kwargs):
        raise rejection

    vm._client._request = reject
    with pytest.raises(ArkerError) as raised:
        lifecycle.Lifecycle(vm=vm).set_timeout(20)
    assert raised.value is rejection and vm.calls == []


@pytest.mark.parametrize("asynchronous", [False, True])
def test_static_connect_resumes_before_bootstrap(vm, monkeypatch, asynchronous):
    from arker import e2b

    events = []
    request = vm._client._request

    def tracked_request(*args, **kwargs):
        events.append("resume")
        return request(*args, **kwargs)

    vm._client._request = tracked_request
    monkeypatch.setattr(e2b.Sandbox, "_lookup", classmethod(lambda cls, *a: (("arker", vm), None)))
    from arker import _compat_e2b_bootstrap as bootstrap
    from arker import _compat_e2b_data_plane as data_plane

    vm._client._api_key = "synthetic"
    monkeypatch.setattr(bootstrap, "bootstrap_envd", lambda current: (events.append("bootstrap") or object(), {}))
    monkeypatch.setattr(
        data_plane,
        "create_data_plane",
        lambda *a, **k: SimpleNamespace(commands=object(), files=object(), git=object()),
    )
    result = asyncio.run(e2b.AsyncSandbox.connect(vm.id)) if asynchronous else e2b.Sandbox.connect(vm.id)
    assert result.sandbox_id == vm.id
    assert events == ["resume", "bootstrap"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_static_connect_failed_restore_never_runs_bootstrap_or_cold_boot(vm, monkeypatch, asynchronous):
    from arker import e2b

    calls = []

    def request(*args, **kwargs):
        calls.append(args)
        raise ArkerError("conflict", "missing checkpoint", status=409)

    vm._client._request = request
    monkeypatch.setattr(e2b.Sandbox, "_lookup", classmethod(lambda cls, *a: (("arker", vm), None)))
    monkeypatch.setattr(e2b.Sandbox, "_prepare_data_plane", lambda self: pytest.fail("must not wake before resume"))
    with pytest.raises(ArkerError):
        if asynchronous:
            asyncio.run(e2b.AsyncSandbox.connect(vm.id))
        else:
            e2b.Sandbox.connect(vm.id)
    assert len(calls) == 1 and calls[0][2] == {
        "deadline": {"policy_id": lifecycle.LIFETIME_POLICY_ID, "timeout_seconds": 300}
    }


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("method", ["pause", "beta_pause"])
@pytest.mark.parametrize("mode", [None, "full", "filesystem"])
@pytest.mark.parametrize("clock", ["at", "after_created"])
def test_pause_with_reserved_lifetime_uses_atomic_backend_operation(vm, asynchronous, method, mode, clock):
    rule = {"type": "schedule", "action": "delete", clock: NOW.isoformat() if clock == "at" else 300}
    vm.policies["rules"][lifecycle.LIFETIME_POLICY_ID] = rule
    before = copy.deepcopy(vm.policies)
    adapter = (lifecycle.AsyncLifecycle if asynchronous else lifecycle.Lifecycle)(vm=vm)
    result = getattr(adapter, method)(mode=mode)
    assert (asyncio.run(result) if asynchronous else result) is True
    assert vm.requests[-1][2] == {"mode": mode or "full", "deadline": {"policy_id": lifecycle.LIFETIME_POLICY_ID}}
    assert vm.calls == []
    assert vm.policies == before


def test_pause_does_not_race_a_policy_read_with_separate_patch(vm):
    vm.refresh = lambda **kwargs: pytest.fail("atomic endpoint must own policy validation")
    assert lifecycle.Lifecycle(vm=vm).pause() is True
    assert [request[0] for request in vm.requests] == ["GET", "POST"] and vm.calls == []


def test_pause_preserves_unrelated_explicit_delete_rules(vm):
    del vm.policies["rules"][lifecycle.LIFETIME_POLICY_ID]
    vm.policies["rules"]["user-deadline"] = {"type": "schedule", "action": "delete", "after_created": 600}
    before = copy.deepcopy(vm.policies)
    assert lifecycle.Lifecycle(vm=vm).pause() is True
    assert vm.policies == before and vm.calls == []


@pytest.mark.parametrize("asynchronous", [False, True])
def test_paused_timeout_never_restores_removed_lifetime_policy(vm, asynchronous):
    from e2b import SandboxNotFoundException

    vm.lifecycle_response.update(state="paused", customer_paused=True, mode="full")
    del vm.policies["rules"][lifecycle.LIFETIME_POLICY_ID]
    before = copy.deepcopy(vm.policies)
    helper = (lifecycle.AsyncLifecycle if asynchronous else lifecycle.Lifecycle)(vm=vm)
    with pytest.raises(SandboxNotFoundException):
        result = helper.set_timeout(30)
        if asynchronous:
            asyncio.run(result)
    assert vm.calls == [] and vm.policies == before
    assert all(request[0] == "POST" and request[1].endswith("/deadline") for request in vm.requests)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mode", ["full", "filesystem"])
def test_repeated_pause_does_not_change_persisted_mode(vm, asynchronous, mode):
    vm.lifecycle_response.update(state="paused", customer_paused=True, mode="filesystem")
    helper = (lifecycle.AsyncLifecycle if asynchronous else lifecycle.Lifecycle)(vm=vm)
    result = helper.pause(mode=mode)
    assert (asyncio.run(result) if asynchronous else result) is False
    assert vm.lifecycle_response["mode"] == "filesystem"
    assert all(request[0] == "GET" for request in vm.requests)
    assert vm.calls == []


@pytest.mark.parametrize("asynchronous", [False, True])
def test_deleted_is_running_returns_false_without_guest_activation(vm, asynchronous):
    def request(*args, **kwargs):
        raise ArkerError("not_found", "missing", status=404)

    vm._client._request = request
    helper = (lifecycle.AsyncLifecycle if asynchronous else lifecycle.Lifecycle)(vm=vm)
    result = helper.is_running()
    assert (asyncio.run(result) if asynchronous else result) is False
    assert vm.calls == []


@pytest.mark.parametrize(
    "status,code,details",
    [
        (409, "invalid_state", {"requirement": "other"}),
        (409, "invalid_state", {"requirement": "explicit_resume", "resource": {"kind": "disk"}}),
        (503, "unavailable", {"requirement": "explicit_resume", "resource": {"kind": "vm"}}),
        (402, "billing_required", {}),
    ],
)
def test_timeout_does_not_mask_unrelated_backend_failures(vm, status, code, details):
    error = ArkerError(code, "failure", status, body={"details": details})

    def fail(*args, **kwargs):
        raise error

    vm._client._request = fail
    with pytest.raises(ArkerError) as raised:
        lifecycle.Lifecycle(vm=vm).set_timeout(5)
    assert raised.value is error and vm.calls == []
