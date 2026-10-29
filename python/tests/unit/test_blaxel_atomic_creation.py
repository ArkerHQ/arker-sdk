from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import arker.blaxel as blaxel


@pytest.mark.parametrize("asynchronous", [False, True])
def test_atomic_creation_forwards_one_native_create_without_lookup(monkeypatch, asynchronous):
    calls = []
    native = SimpleNamespace(metadata=SimpleNamespace(name="named"))

    class Native:
        @staticmethod
        def create(sandbox, **kwargs):
            calls.append((sandbox, kwargs))
            return native

        @staticmethod
        def get(*args, **kwargs):
            pytest.fail("atomic creation must not perform a preliminary lookup")

    monkeypatch.setattr(blaxel.SyncSandboxInstance, "_native_class", staticmethod(lambda: Native))
    monkeypatch.setattr(blaxel, "routed_create", lambda vendor, config, callbacks: (vendor, callbacks[vendor]()))
    monkeypatch.setattr(blaxel, "routed_get", lambda *a, **k: pytest.fail("unexpected lookup"))
    request = {"name": "named", "memory": 2048}
    if asynchronous:

        class AsyncNative:
            @staticmethod
            async def create(sandbox, **kwargs):
                return Native.create(sandbox, **kwargs)

        monkeypatch.setattr(blaxel, "import_optional", lambda name: SimpleNamespace(SandboxInstance=AsyncNative))

        async def wrap(cls, sandbox):
            return sandbox

        monkeypatch.setattr(blaxel.SandboxInstance, "_wrap", classmethod(wrap))
        result = asyncio.run(blaxel.SandboxInstance.create_if_not_exists(request, timeout=37))
    else:
        result = blaxel.SyncSandboxInstance.create_if_not_exists(request, timeout=37)
    assert result._native is native
    assert calls == [(request, {"create_if_not_exist": True, "timeout": 37})]


def test_native_atomic_creation_error_is_not_retried_as_plain_create(monkeypatch):
    refusal = RuntimeError("name cannot be used")
    calls = []

    class Native:
        @staticmethod
        def create(sandbox, **kwargs):
            calls.append(kwargs)
            raise refusal

    monkeypatch.setattr(blaxel.SyncSandboxInstance, "_native_class", staticmethod(lambda: Native))
    monkeypatch.setattr(blaxel, "routed_create", lambda vendor, config, callbacks: (vendor, callbacks[vendor]()))
    with pytest.raises(RuntimeError) as error:
        blaxel.SyncSandboxInstance.create_if_not_exists({"name": "named"})
    assert error.value is refusal
    assert calls == [{"create_if_not_exist": True}]


@pytest.fixture
def named_client(monkeypatch):
    from arker import _compat_blaxel_create as creation

    monkeypatch.delenv("ARKER_BASE_URL", raising=False)
    calls = []
    wire = {
        "vm_id": "winner",
        "name": "named",
        "description": None,
        "owner_org_id": "org",
        "created_at": "2026-01-01T00:00:00Z",
        "public": False,
        "state": "idle",
        "sessions": [],
        "network": {},
        "resources": {},
    }
    client = SimpleNamespace(_control_base_url="https://control.example/api", region="region-one", provider="aws")
    client._request = lambda *args, **kwargs: calls.append((args, kwargs)) or wire
    client.fork = lambda **kwargs: pytest.fail("must use global coordinator")
    return creation, client, calls


@pytest.mark.parametrize("mode", [False, True])
def test_named_creation_uses_global_authority_and_retains_returned_identity(named_client, mode):
    creation, client, calls = named_client
    vm = creation.fork_named(client, {"name": "named", "source_vm_name": "source"}, config={}, get_or_create=mode)
    assert vm.id == "winner"
    assert vm.base_url == client._control_base_url
    args, options = calls[0]
    assert args == ("POST", "/v1/named-fork", {"name": "named", "source_vm_name": "source"})
    assert options["extra_headers"] == {
        "Arker-VM-Name": "named",
        "Arker-VM-Name-Mode": "get_or_create" if mode else "create",
        "x-arker-vm-region": "region-one",
        "x-arker-vm-provider": "aws",
    }
    assert options["base_url"] == client._control_base_url


def test_busy_generation_reconciles_without_lookup_or_new_identity(named_client, monkeypatch):
    from arker.computer import ArkerError

    creation, client, _calls = named_client
    success = client._request
    attempts = []

    def pending(*args, **kwargs):
        attempts.append(args)
        if len(attempts) == 1:
            raise ArkerError("resource_busy", "pending", 409)
        return success(*args, **kwargs)

    client._request = pending
    monkeypatch.setattr(creation.time, "sleep", lambda _: None)
    assert creation.fork_named(client, {"name": "named"}, config={}, get_or_create=True).id == "winner"
    assert attempts == [("POST", "/v1/named-fork", {"name": "named"})] * 2


@pytest.mark.parametrize(
    "status,code", [(401, "unauthorized"), (403, "forbidden"), (404, "not_found"), (409, "idempotency_conflict")]
)
def test_authority_errors_never_fall_back_or_publish_cache(named_client, monkeypatch, status, code):
    import arker._compat as compat
    from arker.computer import ArkerError

    creation, client, _ = named_client
    error = ArkerError(code, "refused", status)

    def reject(*args, **kwargs):
        assert args[1] == "/v1/named-fork"
        raise error

    client._request = reject
    monkeypatch.setattr(
        compat,
        "router_config",
        lambda *args, **kwargs: SimpleNamespace(create=SimpleNamespace(strategy="priority", order=["arker", "blaxel"])),
    )
    monkeypatch.setattr(compat, "_cache", lambda *args: pytest.fail("failed creation cached"))
    with pytest.raises(ArkerError) as caught:
        compat.routed_create(
            "blaxel",
            {},
            {
                "arker": lambda: creation.fork_named(client, {"name": "named"}, config={}),
                "blaxel": lambda: pytest.fail("authority error fell back"),
            },
        )
    assert caught.value is error


def test_strict_duplicate_maps_only_vm_conflict(named_client):
    SandboxAPIError = pytest.importorskip("blaxel.core.sandbox").SandboxAPIError

    from arker.computer import ArkerError

    creation, client, _ = named_client

    def reject(*args, **kwargs):
        raise ArkerError("conflict", "exists", 409, raw={"details": {"resource": "vm"}})

    client._request = reject
    with pytest.raises(SandboxAPIError) as caught:
        creation.fork_named(client, {"name": "named"}, config={})
    assert caught.value.status_code == 409
    assert caught.value.code == "SANDBOX_ALREADY_EXISTS"


def test_custom_regional_url_requires_explicit_global_authority(named_client):
    creation, client, calls = named_client
    with pytest.raises(ValueError, match="control_base_url"):
        creation.fork_named(client, {"name": "named"}, config={"base_url": "https://regional.example/api"})
    assert calls == []


@pytest.mark.parametrize("asynchronous", [False, True])
def test_arker_get_or_create_uses_atomic_callback_and_preserves_existing_on_bootstrap_failure(
    monkeypatch, named_client, asynchronous
):
    _creation, client, calls = named_client
    from arker import _compat
    from arker import _compat_blaxel_data_plane as plane

    monkeypatch.setattr(_compat, "arker_client", lambda config: client)
    monkeypatch.setattr(plane, "validate_native", lambda: None)
    monkeypatch.setattr(blaxel, "routed_create", lambda vendor, config, callbacks: ("arker", callbacks["arker"]()))

    def fail(self):
        raise RuntimeError("bootstrap refused")

    monkeypatch.setattr(blaxel.SyncSandboxInstance, "_plane", fail)
    with pytest.raises(RuntimeError, match="bootstrap refused"):
        if asynchronous:
            asyncio.run(blaxel.SandboxInstance.create_if_not_exists({"name": "named"}, arker={"source": "source"}))
        else:
            blaxel.SyncSandboxInstance.create_if_not_exists({"name": "named"}, arker={"source": "source"})
    assert len(calls) == 1
    assert calls[0][1]["extra_headers"]["Arker-VM-Name-Mode"] == "get_or_create"


@pytest.mark.parametrize("get_or_create", [False, True])
def test_lost_committed_response_does_not_turn_strict_creation_into_duplicate(monkeypatch, get_or_create):
    import json

    import httpx2 as httpx

    import arker.computer as transport
    from arker._compat_blaxel_create import fork_named
    from arker.computer import Arker, ArkerError

    monkeypatch.delenv("ARKER_BASE_URL", raising=False)
    calls = []
    winner = {
        "vm_id": "committed",
        "name": "named",
        "description": None,
        "owner_org_id": "org",
        "created_at": "2026-01-01T00:00:00Z",
        "public": False,
        "state": "idle",
        "sessions": [],
        "network": {},
        "resources": {},
    }

    def http(method, url, headers, body, **kwargs):
        calls.append((method, url, dict(headers), json.loads(body)))
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("committed response lost")
        if get_or_create:
            return 200, json.dumps(winner).encode()
        return 409, json.dumps(
            {"error": {"code": "conflict", "message": "exists", "details": {"resource": "vm"}}}
        ).encode()

    monkeypatch.setattr(transport, "_http", http)
    client = Arker(
        api_key="ark_test",
        control_base_url="https://control.example/api",
        retry={"attempts": 3, "base_delay_s": 0, "jitter_s": 0},
    )
    if get_or_create:
        result = fork_named(client, {"name": "named"}, config={}, get_or_create=True)
        assert result.id == "committed"
        assert len(calls) == 2
    else:
        with pytest.raises(ArkerError, match="outcome is unknown") as error:
            fork_named(client, {"name": "named"}, config={})
        assert error.value.status == 0
        assert len(calls) == 1
    assert all(call[0] == "POST" for call in calls)
    assert all("Idempotency-Key" not in call[2] for call in calls)


def test_concurrent_callers_keep_independent_modes_and_return_authoritative_winner(monkeypatch):
    import concurrent.futures
    import json
    import threading

    SandboxAPIError = pytest.importorskip("blaxel.core.sandbox").SandboxAPIError

    import arker.computer as transport
    from arker._compat_blaxel_create import fork_named
    from arker.computer import Arker

    monkeypatch.delenv("ARKER_BASE_URL", raising=False)
    barrier, lock = threading.Barrier(3), threading.Lock()
    requests, stored = [], []

    def http(method, url, headers, body, **kwargs):
        request = json.loads(body)
        barrier.wait(timeout=5)
        with lock:
            requests.append(headers["Arker-VM-Name-Mode"])
            if not stored:
                stored.append(
                    {
                        "vm_id": "winner",
                        "name": "named",
                        "description": None,
                        "owner_org_id": "org",
                        "created_at": "2026-01-01T00:00:00Z",
                        "public": False,
                        "state": "idle",
                        "sessions": [],
                        "network": {},
                        "resources": {},
                        "metadata": request["metadata"],
                    }
                )
            elif headers["Arker-VM-Name-Mode"] == "create":
                return 409, json.dumps(
                    {"error": {"code": "conflict", "message": "exists", "details": {"resource": "vm"}}}
                ).encode()
            return 200, json.dumps(stored[0]).encode()

    monkeypatch.setattr(transport, "_http", http)

    def create(index):
        client = Arker(api_key="ark_test", control_base_url="https://control.example/api", retry=False)
        try:
            vm = fork_named(
                client, {"name": "named", "metadata": {"contender": index}}, config={}, get_or_create=index != 0
            )
            return vm.id, vm.metadata
        except SandboxAPIError as error:
            return error.status_code, error.code

    with concurrent.futures.ThreadPoolExecutor(3) as pool:
        results = list(pool.map(create, range(3)))
    assert sorted(requests) == ["create", "get_or_create", "get_or_create"]
    assert results[1:] == [("winner", stored[0]["metadata"])] * 2
    assert results[0] in [("winner", stored[0]["metadata"]), (409, "SANDBOX_ALREADY_EXISTS")]


def test_old_gateway_cannot_silently_create_an_ordinary_vm(named_client):
    from arker.computer import ArkerError

    creation, client, _ = named_client
    calls = []
    mutations = []

    def legacy_gateway(method, path, body, **kwargs):
        calls.append(path)
        if path == "/v1/fork":
            mutations.append(body)
            pytest.fail("named creation reached ordinary fork")
        raise ArkerError("not_found", "Unknown route", 404)

    client._request = legacy_gateway
    for mode in (False, True):
        with pytest.raises(ArkerError) as error:
            creation.fork_named(client, {"name": "named"}, config={}, get_or_create=mode)
        assert error.value.status == 404
    assert calls == ["/v1/named-fork", "/v1/named-fork"]
    assert mutations == []


def test_named_fork_reuses_ordinary_policy_read_to_write_normalization(monkeypatch):
    import copy
    import json

    import arker.computer as transport
    from arker._compat_blaxel_create import fork_named
    from arker.computer import Arker

    monkeypatch.delenv("ARKER_BASE_URL", raising=False)
    policy = {
        "hostname": "response-only",
        "mitm_domains": ["response.example"],
        "warnings": [],
        "outbound_guards": {
            "floor": {"type": "outbound_guard", "allow_cidrs": ["10.0.0.0/8"]},
            "removed": {"type": "outbound_guard", "allow_cidrs": []},
        },
        "rules": {"removed": None, "custom": {"type": "schedule", "after_created": 60, "action": "delete"}},
        "secrets": {"remove": None, "token": "synthetic"},
    }
    original = copy.deepcopy(policy)
    calls = []

    def http(method, url, headers, body, **kwargs):
        calls.append((dict(headers), json.loads(body)))
        return 200, json.dumps(
            {
                "vm_id": "winner",
                "name": "named",
                "description": None,
                "owner_org_id": "org",
                "created_at": "2026-01-01T00:00:00Z",
                "public": False,
                "state": "idle",
                "sessions": [],
                "network": {},
                "resources": {},
            }
        ).encode()

    monkeypatch.setattr(transport, "_http", http)
    client = Arker(
        api_key="ark_test",
        control_base_url="https://control.example/api",
        provider="aws",
        region="region-one",
        retry=False,
    )
    request = {"name": "named", "source_vm_name": "source", "policies": policy}
    client.fork(**request, idempotency=True)
    fork_named(client, request, config={})
    expected = {"rules": {"floor": policy["outbound_guards"]["floor"], **policy["rules"]}, "secrets": policy["secrets"]}
    assert calls[0][1]["policies"] == calls[1][1]["policies"] == expected
    assert "Idempotency-Key" in calls[0][0]
    assert "Idempotency-Key" not in calls[1][0]
    assert calls[1][0]["Arker-VM-Name-Mode"] == "create"
    assert policy == original


def test_cancel_during_busy_retry_prevents_another_creation_post(named_client, monkeypatch):
    import threading

    from arker._compat_async import RoutingCancelled
    from arker.computer import ArkerError

    creation, client, _ = named_client
    cancelled = threading.Event()
    attempts = []

    def busy(*args, **kwargs):
        attempts.append(args)
        raise ArkerError("resource_busy", "pending", 409)

    client._request = busy
    monkeypatch.setattr(creation.time, "sleep", lambda _: cancelled.set())
    with pytest.raises(RoutingCancelled):
        creation.fork_named(client, {"name": "named"}, config={}, cancelled=cancelled.is_set)
    assert len(attempts) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "name,message",
    [
        (123, "Failed to parse sandbox body"),
        (0, "Failed to parse sandbox body"),
        (False, "Failed to parse sandbox body"),
        ("a" * 50, "Invalid sandbox configuration: metadata.name is too long (maximum 49 characters)"),
        (
            "Upper",
            (
                "Invalid sandbox configuration: metadata.name must contain only "
                "lower case alphanumeric characters and hyphens"
            ),
        ),
        (
            "under_score",
            (
                "Invalid sandbox configuration: metadata.name must contain only "
                "lower case alphanumeric characters and hyphens"
            ),
        ),
    ],
)
def test_name_validation_matches_native_before_arker_preparation(monkeypatch, asynchronous, name, message):
    SandboxAPIError = pytest.importorskip("blaxel.core.sandbox").SandboxAPIError
    monkeypatch.setattr(blaxel, "routed_create", lambda vendor, config, handlers: ("arker", handlers["arker"]()))
    monkeypatch.setattr(blaxel, "arker_create", lambda *a, **k: pytest.fail("invalid name reached fork preparation"))
    with pytest.raises(SandboxAPIError) as error:
        if asynchronous:
            asyncio.run(blaxel.SandboxInstance.create({"name": name}))
        else:
            blaxel.SyncSandboxInstance.create({"name": name})
    assert error.value.status_code == 400
    assert error.value.body == {"code": "VALIDATION_FAILED", "message": message, "status_code": 400}


def test_name_length49_reaches_named_creation(named_client):
    creation, client, calls = named_client
    creation.fork_named(client, {"name": "a" * 49}, config={})
    assert calls[0][0][2]["name"] == "a" * 49
