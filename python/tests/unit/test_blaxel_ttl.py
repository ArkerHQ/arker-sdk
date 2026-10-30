import asyncio
import importlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

native = pytest.importorskip("blaxel.core.sandbox")


CASES = json.loads((Path(__file__).parents[3] / "tests/fixtures/blaxel-ttl.json").read_text())


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("case", CASES["durations"])
def test_ttl_create_uses_existing_policy_merge(monkeypatch, asynchronous, case):
    import arker._compat as compat
    import arker.blaxel as facade
    from arker import _compat_blaxel_create as creation
    from arker import _compat_blaxel_data_plane as plane

    requests = []
    vm = SimpleNamespace(id="vm-test", name="named", created_at=None, metadata={})
    config = {"source": "source", "policies": {"rules": {"customer-deny": {"type": "inbound", "action": "deny"}}}}
    original = deepcopy(config)
    monkeypatch.setattr(facade, "routed_create", lambda vendor, config, callbacks: ("arker", callbacks["arker"]()))
    monkeypatch.setattr(compat, "arker_client", lambda config: None)
    monkeypatch.setattr(creation, "fork_named", lambda client, request, **kwargs: requests.append(request) or vm)
    monkeypatch.setattr(plane, "validate_native", lambda: None)
    monkeypatch.setattr(facade.SyncSandboxInstance, "_plane", lambda self: None)

    async def wrap(cls, box):
        return box

    monkeypatch.setattr(facade.SandboxInstance, "_wrap", classmethod(wrap))
    request = {"name": "named", **case["options"]}

    def create():
        if asynchronous:
            return asyncio.run(facade.SandboxInstance.create(request, arker=config))
        return facade.SyncSandboxInstance.create(request, arker=config)

    if case.get("reject"):
        with pytest.raises(compat.UnsupportedCompatibilityOption):
            create()
        assert not requests
        return
    create()
    seconds = case["seconds"]
    rules = requests[0]["policies"]["rules"]
    assert rules["customer-deny"] == original["policies"]["rules"]["customer-deny"]
    if seconds is None:
        assert rules == original["policies"]["rules"]
    else:
        assert rules["computesdk-blaxel-lifetime"] == {"type": "schedule", "after_created": seconds, "action": "delete"}
    assert config == original


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("options", [case["options"] for case in CASES["durations"]])
def test_pinned_native_create_ttl_wire_semantics(monkeypatch, asynchronous, options):
    from arker import blaxel as facade

    cls = native.SandboxInstance if asynchronous else native.SyncSandboxInstance
    module = importlib.import_module(cls.__module__)
    captured = []

    class Observed(Exception):
        pass

    def capture(**kwargs):
        captured.append(kwargs["body"].to_dict())
        raise Observed()

    async def async_capture(**kwargs):
        capture(**kwargs)

    monkeypatch.setattr(module, "create_sandbox", async_capture if asynchronous else capture)
    monkeypatch.setattr(facade, "routed_create", lambda vendor, config, callbacks: (vendor, callbacks[vendor]()))
    for target in (cls, facade.SandboxInstance if asynchronous else facade.SyncSandboxInstance):
        with pytest.raises(Observed):
            request = {"name": "named", "region": "us-pdx-1", **options}
            if asynchronous:
                asyncio.run(target.create(request))
            else:
                target.create(request)
    expected = options.get("ttl")
    for body in captured:
        if expected:
            assert body["spec"]["runtime"]["ttl"] == expected
        else:
            assert "ttl" not in body["spec"]["runtime"]
    assert captured[0] == captured[1]


@pytest.mark.parametrize("case", CASES["policies"])
def test_ttl_policy_merge_handles_nullable_rules(monkeypatch, case):
    from arker import _compat as compat
    from arker._compat_blaxel_create import lifetime_policies

    config = {"source": "source", "policies": case["config"]}
    original = deepcopy(config)
    monkeypatch.setattr(compat, "arker_client", lambda config: None)
    if case.get("reject"):
        with pytest.raises(compat.UnsupportedCompatibilityOption):
            lifetime_policies("5m", config)
    else:
        request = compat.arker_create(config, policies=lifetime_policies("5m", config), fork=lambda _, request: request)
        assert request["policies"]["rules"] == {
            **(case["config"].get("rules") or {}),
            "computesdk-blaxel-lifetime": {"type": "schedule", "after_created": 300, "action": "delete"},
        }
    assert config == original
    assert lifetime_policies(None, config) is None
