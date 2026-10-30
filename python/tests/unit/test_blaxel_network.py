import copy
from types import SimpleNamespace

import pytest

from arker import blaxel
from arker._compat import UnsupportedCompatibilityOption
from arker._compat_blaxel_network import validate_network


@pytest.mark.parametrize(
    "network", [None, {}, {"firewall": {}}, {"firewall": {"rulesets": []}}, {"firewall": {"rulesets": ["default"]}}]
)
def test_noop_network_preserves_customer_policy_floor_and_private_ingress(monkeypatch, network):
    from arker import _compat_blaxel_data_plane as plane

    policies = {
        "rules": {
            "floor": {"type": "outbound_guard", "allow": []},
            "daemon": {"type": "inbound", "action": "allow", "auth": "arker", "match": {"ports": [49983]}},
        },
        "secrets": {"customer": "test-only"},
    }
    original = copy.deepcopy(policies)
    requests = []
    client = SimpleNamespace(
        fork=lambda **request: requests.append(request) or SimpleNamespace(id="vm-test", name="test")
    )
    monkeypatch.setattr("arker._compat.arker_client", lambda config: client)
    monkeypatch.setattr(plane, "validate_native", lambda: None)
    monkeypatch.setattr(blaxel, "routed_create", lambda _name, _config, handlers: ("arker", handlers["arker"]()))
    # Stop at the returned VM; no native bootstrap is needed to inspect the atomic policy payload.
    monkeypatch.setattr("arker._compat_blaxel_bootstrap.bootstrap_blaxel", lambda *args: None)
    monkeypatch.setattr(plane, "BlaxelDataPlane", lambda *args: SimpleNamespace())
    blaxel.SyncSandboxInstance.create({"network": network}, arker={"source": "source", "policies": policies})
    assert len(requests) == 1
    assert requests[0]["policies"] == original
    assert policies == original


@pytest.mark.parametrize(
    "network",
    [
        {"allowedDomains": []},
        {"forbiddenDomains": ["example.com"]},
        {"proxy": {"allowedDomains": ["example.com"], "bypass": []}},
        {"proxy": {"routing": [{"headers": {"Authorization": "test-only"}}]}},
        {"egress": {}},
        {"subnet": "default"},
        {"firewall": {"rulesets": ["proxy"]}},
        {"firewall": {"rulesets": ["dedicated-ip"]}},
        {"firewall": {"rulesets": ["default", "unknown"]}},
        {"firewall": {"rulesets": "default"}},
        {"firewall": {"allowedDomains": ["example.com"]}},
        {"firewall": None},
    ],
)
def test_unsupported_network_fails_before_any_provider_io(monkeypatch, network):
    monkeypatch.setattr(blaxel, "routed_create", lambda _name, _config, handlers: handlers["arker"]())
    monkeypatch.setattr(blaxel, "arker_create", lambda *args, **kwargs: pytest.fail("provider I/O"))
    with pytest.raises(UnsupportedCompatibilityOption, match="network") as error:
        blaxel.SyncSandboxInstance.create({"network": network})
    assert "test-only" not in str(error.value)


def test_native_model_serialization_and_route_passthrough(monkeypatch):
    models = pytest.importorskip("blaxel.core.client.models")
    native_network = models.SandboxNetwork(firewall=models.FirewallConfig(rulesets=["default"]))
    validate_network(native_network)
    assert native_network.to_dict() == {"firewall": {"rulesets": ["default"]}}
    request = {"network": {"proxy": {"allowedDomains": ["example.com"]}}}
    result = object()

    def create(value, **kwargs):
        assert value is request
        return result

    monkeypatch.setattr(
        blaxel.SyncSandboxInstance, "_native_class", staticmethod(lambda: SimpleNamespace(create=create))
    )
    monkeypatch.setattr(blaxel, "routed_create", lambda _name, _config, handlers: ("blaxel", handlers["blaxel"]()))
    assert blaxel.SyncSandboxInstance.create(request)._native is result


@pytest.mark.parametrize("value", ["FROM alpine", {"dockerfile": "FROM alpine"}])
def test_docker_config_rejected_before_arker_mutation_and_preserved_for_native(monkeypatch, value):
    request = {"docker_config": value}
    monkeypatch.setattr(blaxel, "arker_create", lambda *a, **kw: pytest.fail("provider I/O"))
    monkeypatch.setattr(blaxel, "routed_create", lambda _name, _config, handlers: handlers["arker"]())
    with pytest.raises(UnsupportedCompatibilityOption, match="docker_config"):
        blaxel.SyncSandboxInstance.create(request)
    result = object()

    def create(received, **kwargs):
        assert received is request
        return result

    monkeypatch.setattr(
        blaxel.SyncSandboxInstance, "_native_class", staticmethod(lambda: SimpleNamespace(create=create))
    )
    monkeypatch.setattr(blaxel, "routed_create", lambda _name, _config, handlers: ("blaxel", handlers["blaxel"]()))
    assert blaxel.SyncSandboxInstance.create(request)._native is result
