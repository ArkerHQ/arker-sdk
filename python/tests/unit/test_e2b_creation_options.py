import asyncio
import copy
from types import SimpleNamespace

import pytest

import arker.e2b as shim
from arker._compat import UnsupportedCompatibilityOption


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "lifecycle",
    [{}, {"auto_resume": False}, {"on_timeout": "kill"}, {"on_timeout": {"action": "kill"}, "auto_resume": False}],
)
def test_arker_creation_keeps_native_credentials_out_of_requests(monkeypatch, asynchronous, lifecycle):
    from arker.computer import Arker

    monkeypatch.setenv("ARKER_API_KEY", "arker-only")
    monkeypatch.delenv("COMPUTESDK_ROUTER_CONFIG", raising=False)
    calls = []
    child = SimpleNamespace(id="child")

    def get_vm(client, source):
        assert client._api_key == "arker-only"
        assert client._e2b_request_timeout == 0
        calls.append(("get", source))
        return SimpleNamespace(policies=None)

    def fork(client, **request):
        assert client._api_key == "arker-only"
        assert client._e2b_request_timeout == 0
        calls.append(("fork", request))
        return child

    monkeypatch.setattr(Arker, "get_vm", get_vm)
    monkeypatch.setattr(Arker, "fork", fork)
    marker = RuntimeError("stop before bootstrap")

    def stop(*args, **kwargs):
        raise marker

    monkeypatch.setattr("arker._compat_e2b_bootstrap.bootstrap_envd", stop)
    child.delete = lambda: None
    options = {
        "api_key": "native-only",
        "secure": False,
        "validate_api_key": False,
        "debug": False,
        "lifecycle": lifecycle,
        "request_timeout": 0,
    }
    original = copy.deepcopy(options)
    with pytest.raises(RuntimeError) as caught:
        if asynchronous:
            asyncio.run(shim.AsyncSandbox.create(template="selected-template", **options))
        else:
            shim.Sandbox.create(template="selected-template", **options)
    assert caught.value is marker
    request = calls[-1][1]
    assert request["source_vm_name"] == "selected-template"
    assert request["policies"]["rules"]["computesdk-e2b-lifetime"] == {
        "type": "schedule",
        "after_created": 300,
        "action": "delete",
    }
    assert request["policies"]["rules"]["computesdk-e2b-envd"]["auth"] == "arker"
    assert "native-only" not in repr(calls)
    assert options == original


@pytest.mark.parametrize(
    "options",
    [
        {"debug": True},
        {"lifecycle": {"on_timeout": "pause"}},
        {"lifecycle": {"on_timeout": {"action": "pause", "mode": "filesystem"}}},
        {"lifecycle": {"auto_resume": True}},
        {"lifecycle": {"auto_resume": 0}},
        {"lifecycle": {"on_timeout": {"action": "kill", "mode": "full"}}},
        {"lifecycle": {"unknown": False}},
        {"proxy": "unsupported"},
        {"headers": {}},
    ],
)
def test_unsupported_options_fail_before_arker_client(monkeypatch, options):
    monkeypatch.setattr("arker._compat.arker_client", lambda *a: pytest.fail("must not create client"))
    with pytest.raises(UnsupportedCompatibilityOption):
        shim.Sandbox._create_arker({}, "template", None, None, options)


def test_native_credential_cannot_authenticate_arker(monkeypatch):
    monkeypatch.delenv("ARKER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="api_key is required"):
        shim.Sandbox._create_arker({}, "template", None, None, {"api_key": "native-only"})


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_options_are_forwarded_without_mutation(monkeypatch, asynchronous):
    import e2b

    monkeypatch.delenv("ARKER_API_KEY", raising=False)
    monkeypatch.delenv("COMPUTESDK_ROUTER_CONFIG", raising=False)
    options = {
        "api_key": "native-only",
        "secure": False,
        "validate_api_key": True,
        "debug": False,
        "lifecycle": {"on_timeout": "pause", "auto_resume": True},
        "request_timeout": 0,
    }
    seen = []
    result = SimpleNamespace(sandbox_id="native", commands=object(), files=object(), git=object())

    def create(*args, **kwargs):
        seen.append((args, kwargs))
        return result

    async def create_async(*args, **kwargs):
        return create(*args, **kwargs)

    monkeypatch.setattr(
        e2b.AsyncSandbox if asynchronous else e2b.Sandbox, "create", create_async if asynchronous else create
    )
    sandbox = (
        asyncio.run(shim.AsyncSandbox.create(template="native-template", **options))
        if asynchronous
        else shim.Sandbox.create(template="native-template", **options)
    )
    assert sandbox.sandbox_id == "native"
    assert seen == [(("native-template",), options)]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_keyed_retry_binds_payload_and_never_deletes_unproven_ownership(monkeypatch, asynchronous):
    from arker.computer import Arker

    monkeypatch.setenv("ARKER_API_KEY", "test-only-creator")
    monkeypatch.delenv("COMPUTESDK_ROUTER_CONFIG", raising=False)
    requests = []
    child = SimpleNamespace(id="existing-child", delete=lambda: pytest.fail("replay is not owned"))
    monkeypatch.setattr(Arker, "get_vm", lambda *args: SimpleNamespace(policies=None))
    monkeypatch.setattr(Arker, "fork", lambda self, **request: requests.append(request) or child)
    failure = RuntimeError("bootstrap failed")

    def stop(*args, **kwargs):
        raise failure

    monkeypatch.setattr("arker._compat_e2b_bootstrap.bootstrap_envd", stop)
    for env in ({"VALUE": "one"}, {"VALUE": "one"}, {"VALUE": "two"}):
        with pytest.raises(RuntimeError) as caught:
            options = {"idempotency_key": "a" * 255, "envs": env, "network": {"allow_public_traffic": False}}
            if asynchronous:
                asyncio.run(shim.AsyncSandbox.create("template", **options))
            else:
                shim.Sandbox.create("template", **options)
        assert caught.value is failure
    assert requests[0] == requests[1]
    assert len(requests[0]["idempotency_key"]) == 64
    assert requests[0]["idempotency_key"] == requests[2]["idempotency_key"]
    assert requests[0]["metadata"] != requests[2]["metadata"]
    assert "test-only-creator" not in repr(requests)


def test_dns_servers_are_host_firewall_exceptions_without_resolver_changes():
    from arker._compat_e2b_create import network_options

    network = {"deny_out": ["0.0.0.0/0"], "allow_out": ["example.com"], "dns_servers": ["1.1.1.1"]}
    original = copy.deepcopy(network)
    normalized, policy = network_options(network)
    assert normalized == original == network
    assert list(policy["rules"].values()) == [
        {"type": "outbound_guard", "allow": [{"hosts": ["example.com"]}, {"ips": ["1.1.1.1"]}]},
        {"type": "outbound_guard", "allow": [{"ports": [[1, 52], [54, 65535]]}, {"ips": ["1.1.1.1"]}]},
    ]
    assert network_options(network)[1] == policy
    assert network_options({"dns_servers": []})[1] is None
    without_dns = network_options({**network, "dns_servers": []})[1]
    assert set(without_dns["rules"]).isdisjoint(policy["rules"])
    assert next(iter(without_dns["rules"].values()))["allow"] == [{"hosts": ["example.com"]}]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_public_facade_replays_and_preserves_native_conflict_type(monkeypatch, asynchronous):
    from e2b import SandboxException

    from arker.computer import Arker, ArkerError

    monkeypatch.setenv("ARKER_API_KEY", "test-only-creator")
    monkeypatch.delenv("COMPUTESDK_ROUTER_CONFIG", raising=False)
    claims = {}
    created = []
    monkeypatch.setattr(Arker, "get_vm", lambda *a: SimpleNamespace(policies=None))

    def fork(client, **request):
        key = request["idempotency_key"]
        if key in claims:
            previous, child = claims[key]
            if previous != request:
                raise ArkerError("idempotency_conflict", "different request", 409)
            return child
        child = SimpleNamespace(id="child", delete=lambda: pytest.fail("must not delete replay"))
        claims[key] = (copy.deepcopy(request), child)
        created.append(child)
        return child

    async def prepared(self):
        return None

    monkeypatch.setattr(Arker, "fork", fork)
    monkeypatch.setattr(shim.Sandbox, "_prepare_data_plane", lambda *a, **k: None)
    monkeypatch.setattr(shim.AsyncSandbox, "_prepare_data_plane", prepared)
    monkeypatch.setattr("arker._compat_e2b_bootstrap.bootstrap_envd", lambda *a, **k: ({}, False))
    monkeypatch.setattr("arker._compat_e2b_urls.prepare_file_urls", lambda *a, **k: None)

    def create(envs):
        options = {"idempotency_key": "repeat-key", "envs": envs, "network": {"allow_public_traffic": False}}
        return (
            asyncio.run(shim.AsyncSandbox.create("template", **options))
            if asynchronous
            else shim.Sandbox.create("template", **options)
        )

    first, second = create({"VALUE": "one"}), create({"VALUE": "one"})
    assert first.sandbox_id == second.sandbox_id == "child"
    assert len(created) == 1
    with pytest.raises(SandboxException) as caught:
        create({"VALUE": "two"})
    assert caught.value.status_code == 409


def test_creation_digest_excludes_client_options_and_normalizes_empty_payloads():
    from arker._compat_e2b_create import intent_hash, network_options

    base = intent_hash("template", 300, None, {})
    assert base == intent_hash(
        "template",
        300,
        None,
        {
            "secure": False,
            "validate_api_key": False,
            "debug": False,
            "request_timeout": 0,
            "envs": {},
            "metadata": {},
            "volume_mounts": {},
            "lifecycle": {"on_timeout": "kill"},
        },
    )
    network, _ = network_options({"allow_out": lambda ctx: list(ctx.rules.keys())})
    assert network == {"allow_out": []}
    assert intent_hash("template", 300, None, {"network": network}) == intent_hash(
        "template", 300, None, {"network": {"allow_out": []}}
    )


def test_native_network_defaults_require_deny_all_for_restrictions():
    from e2b import SandboxException

    from arker._compat_e2b_create import network_options

    assert network_options({"allow_out": []})[1] is None
    assert network_options({"allow_out": ["8.8.8.8"]})[1] is None
    with pytest.raises(SandboxException) as caught:
        network_options({"allow_out": ["example.com"]})
    assert caught.value.status_code == 400
    blocked = network_options({"deny_out": ["0.0.0.0/0"], "dns_servers": ["1.1.1.1"]})[1]
    assert next(iter(blocked["rules"].values()))["allow"] == []


def test_creation_intent_matches_shared_language_vectors():
    import json
    from pathlib import Path

    from arker._compat_e2b_create import intent_hash

    for case in json.loads((Path(__file__).resolve().parents[3] / "compatibility/e2b-create-vectors.json").read_text()):
        assert intent_hash(case["template"], case["timeout"], case["internet"], case["options"]) == case["hash"]


@pytest.mark.parametrize("dns", [[], ["1.1.1.1"], ["8.8.8.8", "1.1.1.1"]])
def test_dns_port_floor_has_no_implicit_hostname_exception(dns):
    from arker._compat_e2b_create import network_options

    _, policy = network_options(
        {"allow_out": ["dns.google", "9.9.9.0/24"], "deny_out": ["0.0.0.0/0"], "dns_servers": dns}
    )
    assert all(len(name) <= 64 for name in policy["rules"])
    floor = list(policy["rules"].values())[1]
    assert floor["allow"] == [{"ports": [[1, 52], [54, 65535]]}, {"ips": ["9.9.9.0/24", *dns]}]
    assert all("hosts" not in scope for scope in floor["allow"])
