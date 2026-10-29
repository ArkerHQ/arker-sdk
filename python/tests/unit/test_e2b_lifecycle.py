import asyncio
import types
from contextlib import nullcontext

import pytest

import arker.e2b as shim


@pytest.fixture
def fork_requests(monkeypatch):
    requests = []
    child = types.SimpleNamespace(id="child")
    monkeypatch.setattr(
        "arker._compat.arker_client",
        lambda config: types.SimpleNamespace(
            get_vm=lambda source: types.SimpleNamespace(policies=None),
            fork=lambda **request: requests.append(request) or child,
        ),
    )
    return requests, child


@pytest.mark.parametrize("timeout", [None, 17])
def test_arker_creation_maps_e2b_lifetime_to_server_delete_schedule(timeout, fork_requests):
    requests, child = fork_requests
    config = {
        "source": "template",
        "policies": {
            "secrets": {"TOKEN": "synthetic"},
            "rules": {"custom": {"type": "outbound", "priority": 7, "action": "allow"}},
        },
    }

    assert (
        shim.Sandbox._create_arker(
            config,
            "template",
            timeout,
            False,
            {},
        )
        is child
    )

    rules = requests[0]["policies"]["rules"]
    schedules = [rule for rule in rules.values() if rule["type"] == "schedule"]
    assert schedules == [
        {
            "type": "schedule",
            "after_created": 300 if timeout is None else timeout,
            "action": "delete",
        }
    ]
    assert len([rule for rule in rules.values() if rule["type"] == "outbound_guard"]) == 1
    assert rules["custom"] == {"type": "outbound", "priority": 7, "action": "allow"}
    assert requests[0]["policies"]["secrets"]["TOKEN"] == "synthetic"
    assert set(config["policies"]["rules"]) == {"custom"}


@pytest.mark.parametrize("timeout", [True, 1.5, "300"])
def test_arker_creation_rejects_invalid_e2b_lifetime_before_fork(timeout, monkeypatch):
    monkeypatch.setattr(shim, "arker_create", lambda *args, **kwargs: pytest.fail("must not fork"))
    with pytest.raises((TypeError, ValueError), match="timeout"):
        shim.Sandbox._create_arker({}, None, timeout, None, {})


def test_arker_creation_rejects_reserved_lifetime_policy_without_fork(monkeypatch):
    monkeypatch.setattr(shim, "arker_create", lambda *args, **kwargs: pytest.fail("must not fork"))
    with pytest.raises(ValueError, match="reserved"):
        shim.Sandbox._create_arker({"policies": {"rules": {"computesdk-e2b-lifetime": None}}}, None, None, None, {})


def test_native_filesystem_is_preserved_with_binary_options_and_metadata():
    marker = object()

    class NativeFiles:
        def read(self, path, **options):
            assert path == "/binary"
            assert options == {"format": "bytes", "user": "user", "request_timeout": 7}
            return bytearray(b"\x00\xff\x80")

        def write(self, path, content, **options):
            assert (path, content, options) == ("/binary", b"\xff", {"metadata": {"fixture": "owned"}})
            return marker

    files = NativeFiles()
    sandbox = shim.Sandbox(native=types.SimpleNamespace(sandbox_id="native", files=files))
    assert sandbox.files is files
    assert sandbox.files.read("/binary", format="bytes", user="user", request_timeout=7) == bytearray(b"\x00\xff\x80")
    assert sandbox.files.write("/binary", b"\xff", metadata={"fixture": "owned"}) is marker


def test_async_native_filesystem_is_preserved_without_sync_translation():
    from arker._compat_e2b_async import AsyncSandbox

    class NativeFiles:
        async def read(self, path, **options):
            assert (path, options) == ("/binary", {"format": "bytes", "request_timeout": 7})
            return bytearray(b"\xff")

    files = NativeFiles()
    sandbox = AsyncSandbox(shim.Sandbox(native=types.SimpleNamespace(sandbox_id="native", files=files)))
    assert sandbox.files is files
    assert asyncio.run(sandbox.files.read("/binary", format="bytes", request_timeout=7)) == bytearray(b"\xff")


@pytest.mark.parametrize("alias", ["timeout_ms", "timeoutMs"])
def test_python_create_does_not_misread_millisecond_alias_as_seconds(alias, monkeypatch):
    monkeypatch.setattr(shim, "routed_create", lambda *args, **kwargs: pytest.fail("must not route"))
    with pytest.raises(TypeError, match="E2B Python API uses timeout in seconds"):
        shim.Sandbox.create(**{alias: 300_000})


def test_arker_creation_refuses_uncomposable_legacy_policy_without_mutation(fork_requests):
    requests, _ = fork_requests
    ordered = [{"type": "outbound", "priority": 1, "action": "deny"}]
    config = {"source": "template", "policies": {"policies": ordered}}

    with pytest.raises(ValueError, match="named ingress"):
        shim.Sandbox._create_arker(config, None, None, None, {})

    assert requests == []
    assert config == {"source": "template", "policies": {"policies": ordered}}


@pytest.mark.parametrize("deleted", [True, False])
def test_kill_preserves_provider_boolean(deleted):
    assert (
        shim.Sandbox(
            vm=types.SimpleNamespace(
                id="vm", refresh=lambda **kwargs: None, delete=lambda **kwargs: types.SimpleNamespace(deleted=deleted)
            )
        ).kill()
        is deleted
    )
    assert shim.Sandbox(native=types.SimpleNamespace(sandbox_id="native", kill=lambda: deleted)).kill() is deleted


def test_context_manager_cleans_up_without_swallowing_user_error():
    calls = []
    sandbox = shim.Sandbox(native=types.SimpleNamespace(sandbox_id="native", kill=lambda: calls.append("kill")))
    failure = RuntimeError("application failed")
    with pytest.raises(RuntimeError) as raised, sandbox as entered:
        assert entered is sandbox
        raise failure
    assert raised.value is failure
    assert calls == ["kill"]


@pytest.mark.parametrize("status", [404, 403, 503])
def test_kill_only_treats_authoritative_absence_as_false(status):
    from arker.computer import ArkerError

    error = ArkerError("request_failed", "request failed", status=status)

    def delete(**kwargs):
        raise error

    sandbox = shim.Sandbox(vm=types.SimpleNamespace(id="vm", refresh=lambda **kwargs: None, delete=delete))
    if status == 404:
        assert sandbox.kill() is False
    else:
        with pytest.raises(ArkerError) as raised:
            sandbox.kill()
        assert raised.value is error


@pytest.mark.parametrize("owner", ["arker", "e2b"])
def test_connect_probes_metadata_before_connecting_only_the_winner(monkeypatch, owner):
    calls = []
    identifier = "shared-id" if owner == "arker" else "native-id"
    native = types.SimpleNamespace(
        get_info=lambda identifier: calls.append(("info", identifier)) or object(),
        connect=lambda identifier: (
            calls.append(("connect", identifier)) or types.SimpleNamespace(sandbox_id=identifier)
        ),
    )
    monkeypatch.setattr(shim.Sandbox, "_native_sandbox", staticmethod(lambda: native))
    vm = types.SimpleNamespace(id="arker-winner", state="idle", base_url="https://worker.example")

    def refresh(**kwargs):
        calls.append(("refresh", vm.state))
        return vm

    def request(method, path, body, **kwargs):
        assert (method, path, body) == (
            "POST",
            "/v1/vms/arker-winner/resume",
            {"deadline": {"policy_id": "computesdk-e2b-lifetime", "timeout_seconds": 300}},
        )
        assert kwargs == {"base_url": vm.base_url, "request_timeout": 60}
        calls.append(("resume", vm.id))
        vm.state = "running"
        return {"vm_id": vm.id, "state": "running", "mode": "full"}

    def prepare(self):
        assert self._vm is vm and vm.state == "running"
        calls.append(("bootstrap", vm.id))

    vm.refresh = refresh
    vm._client = types.SimpleNamespace(_request=request)
    monkeypatch.setattr(shim.Sandbox, "_prepare_data_plane", prepare)

    def route(identifier, provider, config, getters):
        metadata = getters["e2b"]()
        assert calls == [("info", identifier)]
        return owner, metadata if owner == "e2b" else vm

    monkeypatch.setattr(shim, "routed_get", route)
    assert shim.Sandbox.connect(identifier).sandbox_id == (identifier if owner == "e2b" else "arker-winner")
    assert calls == (
        [("info", identifier), ("connect", identifier)]
        if owner == "e2b"
        else [("info", identifier), ("resume", vm.id), ("refresh", "running"), ("bootstrap", vm.id)]
    )


@pytest.mark.parametrize("owner", ["e2b", "arker", None])
def test_static_kill_uses_shared_ownership_without_connecting(monkeypatch, owner):
    calls = []
    native = types.SimpleNamespace(
        get_info=lambda identifier: calls.append("info") or object(),
        connect=lambda identifier: pytest.fail("Deletion must not connect"),
        kill=lambda identifier: calls.append(("native-kill", identifier)) or True,
    )
    vm = types.SimpleNamespace(
        id="shared-id",
        refresh=lambda **kwargs: None,
        delete=lambda **kwargs: calls.append("arker-kill") or types.SimpleNamespace(deleted=True),
    )
    monkeypatch.setattr(shim.Sandbox, "_native_sandbox", staticmethod(lambda: native))

    def lookup(identifier, provider, config, getters):
        assert identifier == "shared-id"
        if owner == "e2b":
            return owner, getters[owner]()
        return (owner, vm) if owner else None

    monkeypatch.setattr(shim, "routed_get", lookup)
    assert shim.Sandbox.kill(sandbox_id="shared-id") is (owner is not None)
    assert calls == (["info", ("native-kill", "shared-id")] if owner == "e2b" else ["arker-kill"] if owner else [])


@pytest.mark.parametrize("public", [None, False, True])
def test_create_preserves_custom_ingress_without_adding_broader_rules(fork_requests, public):
    requests, _ = fork_requests
    custom = {"type": "inbound", "priority": 7, "action": "deny", "match": {"ports": [22]}}
    options = {} if public is None else {"network": {"allow_public_traffic": public}}
    with pytest.raises(ValueError, match="custom inbound policies") if public is False else nullcontext():
        shim.Sandbox._create_arker(
            {"source": "template", "policies": {"rules": {"custom": custom}, "secrets": {"TOKEN": "synthetic"}}},
            None,
            None,
            False,
            options,
        )
    if public is False:
        assert requests == []
        return
    policies = requests[0]["policies"]
    rules = policies["rules"]
    assert rules["custom"] == custom
    assert policies["secrets"] == {"TOKEN": "synthetic"}
    assert {name: rule for name, rule in rules.items() if rule["type"] == "inbound"} == {"custom": custom}
    assert any(rule["type"] == "outbound_guard" for rule in rules.values())


@pytest.mark.parametrize("envs", [["bad"], {"NAME": 1}, {1: "value"}])
def test_invalid_create_envs_are_rejected_before_fork(monkeypatch, envs):
    monkeypatch.setattr(shim, "arker_create", lambda *a, **k: pytest.fail("must not fork"))
    with pytest.raises(TypeError, match="envs"):
        shim.Sandbox._create_arker({}, None, None, None, {"envs": envs})


@pytest.mark.parametrize("priority", [0, 1000000])
def test_reserved_ingress_priority_collision_never_rewrites_user_rules(fork_requests, priority):
    custom = {"type": "outbound", "priority": priority, "action": "deny"}
    config = {"source": "template", "policies": {"rules": {"custom": custom}}}
    with pytest.raises(ValueError, match="reserved"):
        shim.Sandbox._create_arker(config, None, None, None, {})
    assert fork_requests[0] == []
    assert config["policies"]["rules"] == {"custom": custom}


@pytest.mark.parametrize(
    "options,expected", [({}, {}), ({"metadata": None}, {}), ({"metadata": {"team": "example"}}, {"team": "example"})]
)
@pytest.mark.parametrize("source_id", [None, "vm_source"])
def test_create_replaces_inherited_metadata_with_versioned_descriptor(
    fork_requests, monkeypatch, options, expected, source_id
):
    monkeypatch.delenv("ARKER_SOURCE_VM_ID", raising=False)
    config = {"source": "ubuntu", "metadata": {"parent": "must not inherit"}}
    if source_id:
        config["source_vm_id"] = source_id
    requests, _ = fork_requests
    shim.Sandbox._create_arker(config, None, None, None, options)
    assert requests[0]["metadata"] == {
        "arker_e2b": {
            "version": 1,
            "envd_version": "0.10.2",
            "metadata": expected,
            "source": {"kind": "vm_id" if source_id else "vm_name", "value": source_id or "ubuntu"},
        }
    }


@pytest.mark.parametrize("metadata", [[], {"value": 1}, {"value": None}])
def test_create_rejects_invalid_metadata_before_fork(metadata, fork_requests):
    with pytest.raises(TypeError, match="metadata"):
        shim.Sandbox._create_arker({"source": "ubuntu"}, None, None, None, {"metadata": metadata})
    assert fork_requests[0] == []


@pytest.mark.parametrize("timeout", [0, -1, 86401])
def test_create_timeout_range_error_is_native_generic_400(timeout, monkeypatch):
    from e2b import SandboxException

    monkeypatch.setattr(shim, "arker_create", lambda *args, **kwargs: pytest.fail("must not fork"))
    with pytest.raises(SandboxException) as raised:
        shim.Sandbox._create_arker({}, None, timeout, None, {})
    assert type(raised.value) is SandboxException
    assert raised.value.status_code == 400


@pytest.mark.parametrize("public", [False, True])
def test_facade_fork_installs_child_ingress_and_token_in_single_request(monkeypatch, public):
    from arker._compat_e2b_network import APP, CONTROL, SECRET, traffic_policies, traffic_token

    inherited, parent_token = traffic_policies(None, None, False)
    child, calls = types.SimpleNamespace(id="child"), []
    client = types.SimpleNamespace(
        get_vm=lambda source: types.SimpleNamespace(policies=inherited),
        fork=lambda **request: calls.append(request) or child,
    )
    monkeypatch.setattr("arker._compat.arker_client", lambda config: client)
    result = shim.Sandbox._create_arker(
        {"source": "parent"}, None, None, None, {"network": {"allow_public_traffic": public}}
    )
    assert result is child and len(calls) == 1
    policy = calls[0]["policies"]
    token = traffic_token(child)
    assert token != parent_token
    assert policy["secrets"][SECRET] == token
    assert policy["rules"][CONTROL]["auth"] == "arker"
    assert policy["rules"][CONTROL]["match"] == {"ports": [49983]}
    if public:
        assert token is None and "headers" not in policy["rules"][APP]["match"]
    else:
        assert len(token) == 43
        assert policy["rules"][APP]["match"]["headers"] == {"e2b-traffic-access-token": ["${secret:" + SECRET + "}"]}


@pytest.mark.parametrize("network", [None, {}, {"allow_public_traffic": True}, {"allow_public_traffic": False}])
def test_create_records_only_explicit_network_visibility(network, fork_requests):
    requests, _ = fork_requests
    options = {"network": network}
    shim.Sandbox._create_arker({"source": "ubuntu"}, None, None, None, options)
    descriptor = requests[0]["metadata"]["arker_e2b"]
    if network:
        assert descriptor["network"] == network
    else:
        assert "network" not in descriptor
