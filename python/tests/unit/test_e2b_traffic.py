from copy import deepcopy
from types import SimpleNamespace

import pytest

from arker._compat_e2b_network import APP, CONTROL, PORTS, SECRET, fork_with_traffic, traffic_policies, traffic_token


def test_private_fork_overrides_token_atomically_and_excludes_control():
    parent, old = traffic_policies(None, None, False)
    seen = []
    vm = SimpleNamespace(id="child")

    def fork(**request):
        seen.append(request)
        return vm

    client = SimpleNamespace(get_vm=lambda source: SimpleNamespace(policies=parent), fork=fork)
    assert fork_with_traffic(client, {"source_vm_id": "parent"}, False) is vm
    token = traffic_token(vm)
    assert token != old and len(token) == 43
    policy = seen[0]["policies"]
    assert policy["secrets"][SECRET] == token
    assert policy["rules"][APP]["match"] == {
        "ports": PORTS,
        "headers": {"e2b-traffic-access-token": ["${secret:" + SECRET + "}"]},
    }
    assert policy["rules"][CONTROL]["match"] == {"ports": [49983]}
    assert policy["rules"][CONTROL]["auth"] == "arker"


def test_public_default_has_no_token_and_preserves_other_policy_updates():
    configured = {"secrets": {"other": "synthetic"}, "rules": {"outbound": {"type": "outbound", "action": "deny"}}}
    policy, token = traffic_policies(None, configured, True)
    assert token is None and policy["secrets"][SECRET] is None
    assert policy["secrets"]["other"] == "synthetic"
    assert policy["rules"]["outbound"] == configured["rules"]["outbound"]
    assert "headers" not in policy["rules"][APP]["match"]


def test_custom_policy_is_not_broadened():
    custom = {"rules": {"custom": {"type": "inbound", "auth": "open", "action": "allow", "match": {"ports": [8080]}}}}
    assert traffic_policies(custom, None, True) == ({}, None)
    with pytest.raises(ValueError, match="conflicts"):
        traffic_policies(custom, None, False)
    modified, _ = traffic_policies(None, None, False)
    modified["rules"][APP]["match"]["paths"] = ["/restricted"]
    with pytest.raises(ValueError, match="Custom and managed"):
        traffic_policies(modified, None, True)


def test_legacy_public_rule_removed_in_fork_request():
    name = "e2b-app-ingress-00000000-0000-0000-0000-000000000000"
    source = {"rules": {name: {"type": "inbound", "action": "allow", "auth": "open", "match": {"ports": PORTS}}}}
    policy, token = traffic_policies(source, None, False)
    assert token and policy["rules"][name] is None


def test_facade_properties_delegate_native_and_keep_arker_token_out_of_metadata():
    from arker._compat_e2b_network import remember_traffic_token
    from arker.e2b import AsyncSandbox, Sandbox

    vm = SimpleNamespace(id="owned")
    box = Sandbox(vm=vm)
    assert box.traffic_access_token is None
    remember_traffic_token(vm, "a" * 43)
    assert box.traffic_access_token == "a" * 43
    assert AsyncSandbox(box).traffic_access_token == "a" * 43
    native = SimpleNamespace(sandbox_id="native", traffic_access_token="native-token")
    assert Sandbox(native=native).traffic_access_token == "native-token"


@pytest.mark.parametrize("auth,action", [("arker", "allow"), ("open", "allow"), (None, "allow"), ("arker", "deny")])
@pytest.mark.parametrize("configured", [False, True])
def test_custom_private_rejected_before_fork_without_mutating_policies(auth, action, configured):
    custom = {"rules": {"customer": {"type": "inbound", "action": action, "match": {"ports": [8080]}}}}
    if auth is not None:
        custom["rules"]["customer"]["auth"] = auth
    original = deepcopy(custom)
    calls = []
    client = SimpleNamespace(
        get_vm=lambda source: SimpleNamespace(policies=None if configured else custom),
        fork=lambda **request: calls.append(request),
    )
    request = {"source_vm_id": "parent"}
    if configured:
        request["policies"] = custom
    with pytest.raises(ValueError, match="custom inbound policies"):
        fork_with_traffic(client, request, False)
    assert calls == []
    assert custom == original


@pytest.mark.parametrize("private", [False, True])
def test_canonical_managed_get_without_open_auth_rotates_private_child(private):
    source = {
        "rules": {
            APP: {"type": "inbound", "action": "allow", "priority": 0, "match": {"ports": PORTS}},
            CONTROL: {
                "type": "inbound",
                "action": "allow",
                "priority": 1000000,
                "auth": "arker",
                "match": {"ports": [49983]},
            },
        },
        "secrets": {SECRET: "***"},
    }
    if private:
        source["rules"][APP]["match"]["headers"] = {"e2b-traffic-access-token": ["***"]}
    original = deepcopy(source)
    calls = []
    child = SimpleNamespace(id="child")
    client = SimpleNamespace(
        get_vm=lambda _: SimpleNamespace(policies=source), fork=lambda **request: calls.append(request) or child
    )
    assert fork_with_traffic(client, {"source_vm_id": "parent"}, False) is child
    token = traffic_token(child)
    assert len(token) == 43 and token != "***"
    policy = calls[0]["policies"]
    assert policy["secrets"][SECRET] == token
    assert policy["rules"][APP]["auth"] == "open"
    assert policy["rules"][APP]["match"]["headers"] == {"e2b-traffic-access-token": ["${secret:" + SECRET + "}"]}
    assert policy["rules"][CONTROL]["auth"] == "arker"
    assert source == original


@pytest.mark.parametrize("change", ["name", "ports", "paths", "header", "auth_null", "control_auth"])
def test_missing_auth_does_not_recognize_custom_or_control_rules(change):
    source = {
        "rules": {
            APP: {
                "type": "inbound",
                "action": "allow",
                "match": {"ports": PORTS, "headers": {"e2b-traffic-access-token": ["***"]}},
            }
        }
    }
    rule = source["rules"][APP]
    if change == "name":
        source["rules"]["customer"] = source["rules"].pop(APP)
    elif change == "ports":
        rule["match"]["ports"] = [8080]
    elif change == "paths":
        rule["match"]["paths"] = ["/restricted"]
    elif change == "header":
        rule["match"]["headers"] = {"customer-token": ["***"]}
    elif change == "auth_null":
        rule["auth"] = None
    else:
        source["rules"][CONTROL] = {"type": "inbound", "action": "allow", "match": {"ports": [49983]}}
    original = deepcopy(source)
    calls = []
    client = SimpleNamespace(get_vm=lambda _: SimpleNamespace(policies=source), fork=lambda **kw: calls.append(kw))
    with pytest.raises(ValueError, match="custom inbound"):
        fork_with_traffic(client, {"source_vm_id": "parent"}, False)
    assert calls == [] and source == original
