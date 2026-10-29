import asyncio
import copy
from types import SimpleNamespace

import pytest

import arker.modal as shim
from arker._compat import UnsupportedCompatibilityOption


@pytest.fixture
def requests(monkeypatch):
    calls = []
    monkeypatch.setattr(shim, "routed_create", lambda name, config, creators: ("arker", creators["arker"]()))
    monkeypatch.setattr(
        "arker._compat.arker_client",
        lambda config: SimpleNamespace(fork=lambda **request: calls.append(request) or SimpleNamespace(id="owned")),
    )
    return calls


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("blocked", [False, True])
def test_default_lifetime_is_atomic_and_preserves_customer_policies(requests, asynchronous, blocked):
    policies = {"rules": {"custom": {"type": "inbound", "action": "deny"}}, "secrets": {"TOKEN": "synthetic"}}
    before = copy.deepcopy(policies)
    options = {"arker": {"source": "fixture", "policies": policies}, "block_network": blocked}
    sandbox = asyncio.run(shim.Sandbox.create.aio(**options)) if asynchronous else shim.Sandbox.create(**options)
    assert sandbox.object_id == "owned" and len(requests) == 1
    rules = requests[0]["policies"]["rules"]
    assert rules["computesdk-modal-lifetime"] == {"type": "schedule", "after_created": 300, "action": "delete"}
    assert "computesdk-e2b-lifetime" not in rules
    assert rules["custom"] == before["rules"]["custom"]
    assert requests[0]["policies"]["secrets"] == before["secrets"]
    assert len([rule for rule in rules.values() if rule["type"] == "outbound_guard"]) == int(blocked)
    assert policies == before


@pytest.mark.parametrize("policies", [{"rules": {"computesdk-modal-lifetime": None}}, {"policies": []}])
def test_conflicting_policy_shapes_fail_before_fork(requests, policies):
    before = copy.deepcopy(policies)
    with pytest.raises(UnsupportedCompatibilityOption):
        shim.Sandbox.create(arker={"source": "fixture", "policies": policies})
    assert requests == [] and policies == before


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("timeout,expected", [(0, 300), (None, 300), (10, 10), (600, 600), (86400, 86400)])
def test_explicit_lifetime(requests, asynchronous, timeout, expected):
    options = {"timeout": timeout, "arker": {"source": "fixture"}}
    if asynchronous:
        asyncio.run(shim.Sandbox.create.aio(**options))
    else:
        shim.Sandbox.create(**options)
    assert requests[0]["policies"]["rules"]["computesdk-modal-lifetime"]["after_created"] == expected


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("timeout", [-1, 1.5, True, 1, 9, 86401, 4294967296])
def test_invalid_lifetime_preserves_native_error_before_fork(requests, asynchronous, timeout):
    pytest.importorskip("modal")
    from modal.exception import InvalidError
    from modal_proto import api_pb2

    try:
        value = api_pb2.Sandbox(timeout_secs=timeout).timeout_secs
        if value and not 10 <= value <= 86400:
            raise InvalidError("Timeout must be between 10s and 86400s (inclusive).")
    except Exception as expected:
        options = {"timeout": timeout, "arker": {"source": "fixture"}}
        with pytest.raises(type(expected)) as actual:
            if asynchronous:
                asyncio.run(shim.Sandbox.create.aio(**options))
            else:
                shim.Sandbox.create(**options)
        assert str(actual.value) == str(expected)
    else:
        pytest.fail("test input must be invalid")
    assert requests == []


def test_cancelled_explicit_lifetime_creation_deletes_late_sandbox(monkeypatch):
    import threading

    started, release, deleted = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def fork(**request):
        calls.append(request)
        started.set()
        assert release.wait(5)
        return SimpleNamespace(id="owned", delete=lambda **kwargs: deleted.set())

    monkeypatch.setattr(shim, "routed_create", lambda name, config, creators: ("arker", creators["arker"]()))
    monkeypatch.setattr("arker._compat.arker_client", lambda config: SimpleNamespace(fork=fork))

    async def scenario():
        task = asyncio.create_task(shim.Sandbox.create.aio(timeout=10, arker={"source": "fixture"}))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        assert await asyncio.to_thread(deleted.wait, 5)

    asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0]["policies"]["rules"]["computesdk-modal-lifetime"]["after_created"] == 10


@pytest.mark.parametrize("timeout", [None, 0, 600])
def test_native_timeout_forwarding_is_unchanged(monkeypatch, timeout):
    calls = []
    native = SimpleNamespace(create=lambda **options: calls.append(options) or SimpleNamespace(object_id="sb-owned"))
    monkeypatch.setattr(shim.Sandbox, "_native_sandbox", lambda: native)
    monkeypatch.setattr(shim, "routed_create", lambda name, config, creators: ("modal", creators["modal"]()))
    assert shim.Sandbox.create(timeout=timeout).object_id == "sb-owned"
    assert calls == [{"timeout": timeout}]


@pytest.mark.parametrize(
    "timeout,lifetime,error",
    [
        (None, 300, None),
        (0, 300, None),
        (10, 10, None),
        (86400, 86400, None),
        (-1, None, ValueError),
        (1.5, None, TypeError),
        (True, None, ValueError),
        (86401, None, ValueError),
    ],
)
def test_lifetime_without_optional_vendor(requests, monkeypatch, timeout, lifetime, error):
    monkeypatch.setattr(shim, "import_optional", lambda _: None)
    if error:
        with pytest.raises(error):
            shim.Sandbox.create(timeout=timeout, arker={"source": "fixture"})
        assert requests == []
    else:
        shim.Sandbox.create(timeout=timeout, arker={"source": "fixture"})
        assert requests[0]["policies"]["rules"]["computesdk-modal-lifetime"]["after_created"] == lifetime
