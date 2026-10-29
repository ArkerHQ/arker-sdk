import asyncio
from types import SimpleNamespace

import pytest

from arker._compat import UnsupportedCompatibilityOption
from arker._compat_e2b_inventory import AsyncInventoryPaginator, InventoryPaginator, sandbox_info
from arker._compat_e2b_lifecycle import Lifecycle
from arker.computer import ArkerError

START = "2026-01-01T01:00:00Z"
END = "2026-01-01T01:05:00Z"


@pytest.fixture
def vm():
    value = SimpleNamespace(
        id="vm-a",
        base_url="https://example.invalid",
        created_at="2025-01-01T00:00:00Z",
        metadata={
            "arker_e2b": {
                "version": 1,
                "metadata": {"team": "x"},
                "source": {"kind": "vm_id", "value": "vm-source"},
                "envd_version": "0.10.2",
            }
        },
        resources={"vcpu": 2, "memory_mib": 512},
        policies={"rules": {"computesdk-e2b-lifetime": {"type": "schedule", "action": "delete", "at": END}}},
    )
    value.refresh = lambda **kwargs: value
    value._client = SimpleNamespace(
        _request=lambda *a, **k: {"vm_id": value.id, "state": "idle", "mode": None, "customer_paused": False}
    )
    return value


@pytest.mark.parametrize("state", ["running", "paused"])
def test_info_uses_observed_lifecycle_times_and_native_model(vm, state):
    from e2b import SandboxInfo, SandboxState

    result = sandbox_info(
        vm, {"customer_paused": state == "paused", "started_at": START, "paused_at": END if state == "paused" else None}
    )
    assert isinstance(result, SandboxInfo)
    assert result.state == SandboxState(state)
    assert result.started_at.isoformat() == "2026-01-01T01:00:00+00:00"
    assert result.end_at.isoformat() == "2026-01-01T01:05:00+00:00"
    assert result.template_id == "vm-source" and result.name is None
    assert result.metadata == {"team": "x"}
    assert (result.cpu_count, result.memory_mb) == (2, 512)


@pytest.mark.parametrize("resources", [{}, {"vcpu": 2, "memory_mib": None}, {"vcpu": True, "memory_mib": 512}])
def test_missing_resources_never_fabricated(vm, resources):
    vm.resources = resources
    with pytest.raises(UnsupportedCompatibilityOption, match="concrete CPU"):
        sandbox_info(vm, {"customer_paused": False, "started_at": START})


def test_creation_time_never_substitutes_for_lifecycle_time(vm):
    with pytest.raises(UnsupportedCompatibilityOption, match="lifecycle start"):
        sandbox_info(vm, {"customer_paused": False})


def test_get_info_missing_maps_native_exception(vm):
    from e2b import SandboxNotFoundException

    def missing(**kwargs):
        raise ArkerError("not_found", "missing", 404)

    vm.refresh = missing
    with pytest.raises(SandboxNotFoundException):
        Lifecycle(vm=vm).get_info()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_inventory_cursor_filters_metadata_before_pagination(vm, monkeypatch, asynchronous):
    import arker._compat_e2b_inventory as inventory

    calls = []

    def listing(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(vms=[vm], next_cursor="page2" if len(calls) == 1 else None)

    client = SimpleNamespace(list_vms=listing)
    monkeypatch.setattr(
        inventory,
        "sandbox_info",
        lambda item, observed: (item.id, "paused" if observed["customer_paused"] else "running"),
    )
    paginator = InventoryPaginator(client, query=SimpleNamespace(metadata={"team": "x"}, state=["running"]), limit=2)
    if asynchronous:
        paginator = AsyncInventoryPaginator(paginator)

    def next_items():
        result = paginator.next_items()
        return asyncio.run(result) if asynchronous else result

    assert paginator.has_next and paginator.next_token is None
    assert next_items() == [(vm.id, "running")]
    assert paginator.next_token == "page2"
    assert next_items() == [(vm.id, "running")]
    assert not paginator.has_next
    assert calls[0] == {
        "cursor": None,
        "limit": 2,
        "request_timeout": 60,
        "started_after": None,
        "started_at_order": "desc",
        "metadata_filter": {"arker_e2b": {"version": 1, "metadata": {"team": "x"}}},
    }
    assert calls[1]["cursor"] == "page2"
    with pytest.raises(Exception, match="No more items"):
        next_items()


def test_inventory_conversion_failure_keeps_cursor(vm):
    client = SimpleNamespace(list_vms=lambda **kw: SimpleNamespace(vms=[vm], next_cursor="later"))
    paginator = InventoryPaginator(client)
    with pytest.raises(UnsupportedCompatibilityOption, match="lifecycle start"):
        paginator.next_items()
    assert paginator.has_next and paginator.next_token is None


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_static_inventory_keeps_exception_and_result_identity(monkeypatch, asynchronous):
    import e2b

    import arker._compat_e2b_inventory as inventory
    import arker.e2b as shim
    from arker._router import RouterConfig

    result, paginator = object(), object()
    calls = []

    def get_info(identifier, **opts):
        calls.append((identifier, opts))
        return result

    async def async_get_info(identifier, **opts):
        return get_info(identifier, **opts)

    native = SimpleNamespace(get_info=async_get_info if asynchronous else get_info, list=lambda **opts: paginator)
    monkeypatch.setattr(e2b, "AsyncSandbox" if asynchronous else "Sandbox", native)
    monkeypatch.setattr(shim, "routed_get", lambda identifier, provider, config, handlers: ("e2b", handlers["e2b"]()))
    router = RouterConfig.from_dict(
        {
            "version": 1,
            "existing": {"strategy": "priority", "order": ["e2b"]},
            "create": {"strategy": "priority", "order": ["e2b"]},
        }
    )
    monkeypatch.setattr(inventory, "router_config", lambda *args: router, raising=False)
    import arker._compat as compat

    monkeypatch.setattr(compat, "router_config", lambda *args: router)
    cls = shim.AsyncSandbox if asynchronous else shim.Sandbox
    info = cls.get_info("native-id", request_timeout=7)
    assert (asyncio.run(info) if asynchronous else info) is result
    assert calls == [("native-id", {"request_timeout": 7})]
    assert cls.list(limit=3) is paginator


def test_inventory_does_not_advance_cursor_on_transport_error(vm):
    def failing(**kwargs):
        raise ArkerError("unavailable", "offline", 503)

    paginator = InventoryPaginator(SimpleNamespace(list_vms=failing), next_token="retry")
    with pytest.raises(ArkerError):
        paginator.next_items()
    assert paginator.next_token == "retry" and paginator.has_next


def test_after_created_deadline_uses_vm_birth_but_start_uses_latest_resume(vm):
    vm.policies["rules"]["computesdk-e2b-lifetime"] = {"type": "schedule", "action": "delete", "after_created": 300}
    info = sandbox_info(vm, {"customer_paused": False, "started_at": START})
    assert info.started_at.isoformat() == "2026-01-01T01:00:00+00:00"
    assert info.end_at.isoformat() == "2025-01-01T00:05:00+00:00"


def test_paused_info_uses_pause_marker_without_lifetime_rule(vm):
    vm.policies = {"rules": {}}
    vm._client._request = lambda *a, **k: {
        "vm_id": vm.id,
        "state": "paused",
        "mode": "full",
        "customer_paused": True,
        "started_at": START,
        "paused_at": END,
    }
    result = Lifecycle(vm=vm).get_info()
    assert str(result.state) == "paused" and result.end_at.isoformat() == "2026-01-01T01:05:00+00:00"


def test_template_filter_uses_recorded_source_reference():
    paginator = InventoryPaginator(object(), query=SimpleNamespace(template="vm-source"))
    assert paginator.metadata_filter["arker_e2b"]["source"] == {"value": "vm-source"}


@pytest.mark.parametrize("order", [None, "asc", "desc"])
def test_inventory_forwards_inclusive_start_filter_and_global_order(order):
    from datetime import datetime

    calls = []
    client = SimpleNamespace(
        list_vms=lambda **kwargs: calls.append(kwargs) or SimpleNamespace(vms=[], next_cursor=None)
    )
    paginator = InventoryPaginator(
        client, query=SimpleNamespace(started_after=datetime.fromisoformat("2026-01-01T03:00:00+02:00")), order=order
    )
    assert paginator.next_items() == []
    assert calls[0]["started_after"] == "2026-01-01T01:00:00+00:00"
    assert calls[0]["started_at_order"] == (order or "desc")


@pytest.mark.parametrize("options", [{"order": "newest"}, {"query": {"started_after": "yesterday"}}])
def test_invalid_inventory_order_and_timestamp_fail_before_io(options):
    from e2b import InvalidArgumentException

    with pytest.raises(InvalidArgumentException):
        InventoryPaginator(object(), **options)


@pytest.mark.parametrize("expires_at", ["2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z"])
@pytest.mark.parametrize("expiring_rule", ["apps", "control"])
@pytest.mark.parametrize("app_auth", ["open", "arker"])
def test_expiring_inbound_rules_never_infer_public_traffic(vm, expires_at, expiring_rule, app_auth):
    rules = vm.policies["rules"]
    rules.update(
        {
            "apps": {
                "type": "inbound",
                "action": "allow",
                "auth": app_auth,
                "match": {"ports": [[1, 49982], [49984, 65535]]},
            },
            "control": {"type": "inbound", "action": "allow", "auth": "arker", "match": {"ports": [49983]}},
        }
    )
    rules[expiring_rule]["expires_at"] = expires_at
    result = sandbox_info(vm, {"customer_paused": False, "started_at": START})
    assert result.network is None
