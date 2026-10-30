from types import SimpleNamespace

import pytest

pytest.importorskip("blaxel.core.sandbox")
from blaxel.core.client.models import SandboxForkResponse, SandboxSpec

from arker import _compat_blaxel_create as creation
from arker import blaxel as facade


def vm(ttl="5m", seconds=300):
    return SimpleNamespace(
        id="vm-test",
        name="source",
        created_at=None,
        metadata={"arker_blaxel": {"version": 1, "labels": {}, "ttl": ttl}},
        policies={
            "rules": {"computesdk-blaxel-lifetime": {"type": "schedule", "action": "delete", "after_created": seconds}}
        },
        base_url="https://custom.example/api",
        _client=SimpleNamespace(_control_base_url="https://control.example/api"),
    )


def test_ttl_is_native_spec_and_survives_reconnect():
    source = vm("300s")
    for box in (facade.SyncSandboxInstance(vm=source), facade.SyncSandboxInstance(vm=source)):
        assert isinstance(box.spec, SandboxSpec)
        assert box.spec.to_dict() == {"runtime": {"ttl": "300s"}}


@pytest.mark.parametrize("ttl,seconds", [(None, 300), ("5m", 600), ("invalid", 300)])
def test_ttl_does_not_invent_default_or_hide_policy_drift(ttl, seconds):
    assert facade.SyncSandboxInstance(vm=vm(ttl, seconds)).spec.to_dict() == {}


def test_creation_persists_ttl_and_reuse_returns_winner(monkeypatch):
    winner = vm("300s")
    captured = []
    monkeypatch.setattr(facade, "routed_create", lambda vendor, config, callbacks: ("arker", callbacks["arker"]()))
    monkeypatch.setattr(facade, "arker_create", lambda *args, **kwargs: captured.append(kwargs) or winner)
    monkeypatch.setattr(facade.SyncSandboxInstance, "_plane", lambda self: None)
    from arker import _compat_blaxel_data_plane

    monkeypatch.setattr(_compat_blaxel_data_plane, "validate_native", lambda: None)
    box = facade.SyncSandboxInstance.create({"name": "source", "ttl": "10m"}, create_if_not_exist=True)
    assert captured[0]["metadata"]["arker_blaxel"]["ttl"] == "10m"
    assert box.spec.runtime.ttl == "300s"


def test_fork_uses_actual_source_and_native_response_without_bootstrap(monkeypatch):
    source = vm()
    requests = []

    def fork(client, request, **options):
        assert client is source._client
        assert options["config"]["control_base_url"] == client._control_base_url
        requests.append(request)
        return SimpleNamespace(name="child")

    monkeypatch.setattr(creation, "fork_named", fork)
    result = facade.SyncSandboxInstance(
        vm=source, arker_config={"control_base_url": source._client._control_base_url}
    ).fork("child")
    assert isinstance(result, SandboxForkResponse)
    assert result.to_dict() == {"name": "child", "type": "sandbox", "snapshotId": ""}
    assert requests == [{"source_vm_id": "vm-test", "name": "child"}]


@pytest.mark.parametrize(
    "options", [{"target_type": "application"}, {"snapshot_id": "snapshot"}, {"envs": []}, {"lifecycle": {}}]
)
def test_unsupported_fork_fails_before_mutation(monkeypatch, options):
    monkeypatch.setattr(creation, "fork_named", lambda *a, **kw: pytest.fail("unexpected mutation"))
    with pytest.raises(facade.UnsupportedCompatibilityOption):
        facade.SyncSandboxInstance(vm=vm()).fork("child", **options)


def test_native_fork_options_and_result_are_untouched():
    result = object()
    calls = []
    native = SimpleNamespace(id="source", fork=lambda *a, **kw: calls.append((a, kw)) or result)
    assert facade.SyncSandboxInstance(native=native).fork("child", target_type="application", port=8080) is result
    assert calls == [(("child",), {"target_type": "application", "port": 8080})]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_get_rehydrates_ttl_and_async_fork_uses_shared_path(monkeypatch, asynchronous):
    import asyncio

    from arker import _compat_blaxel_data_plane

    source = vm("300s")
    monkeypatch.setattr(facade, "routed_get", lambda *a: ("arker", source))
    monkeypatch.setattr(facade.SyncSandboxInstance, "_plane", lambda self: None)
    monkeypatch.setattr(
        _compat_blaxel_data_plane, "BlaxelDataPlane", lambda *a, **kw: SimpleNamespace(process=None, fs=None)
    )
    monkeypatch.setattr(creation, "fork_named", lambda *a, **kw: SimpleNamespace(name="child"))

    async def run():
        box = await facade.SandboxInstance.get("source")
        assert box.spec.runtime.ttl == "300s"
        assert (await box.fork("child")).to_dict() == {"name": "child", "type": "sandbox", "snapshotId": ""}

    if asynchronous:
        asyncio.run(run())
    else:
        assert facade.SyncSandboxInstance.get("source").spec.runtime.ttl == "300s"


def test_custom_origin_without_explicit_coordinator_rejects_fork(monkeypatch):
    monkeypatch.delenv("ARKER_CONTROL_BASE_URL", raising=False)
    box = facade.SyncSandboxInstance(vm=vm(), arker_config={"base_url": "https://custom.example/api"})
    with pytest.raises(ValueError, match="control_base_url"):
        box.fork("child")


@pytest.mark.parametrize("asynchronous", [False, True])
def test_pinned_native_fork_request_and_response_contract(monkeypatch, asynchronous):
    import asyncio
    import importlib

    from blaxel.core.client.models import Metadata, Sandbox, SandboxRuntime
    from blaxel.core.sandbox import SandboxInstance, SyncSandboxInstance

    cls = SandboxInstance if asynchronous else SyncSandboxInstance
    module = importlib.import_module(cls.__module__)
    expected = SandboxForkResponse.from_dict({"name": "child", "type": "sandbox", "snapshotId": ""})
    requests = []

    def capture(name, *, client, body):
        requests.append((name, body.to_dict()))
        return expected

    async def capture_async(*args, **kwargs):
        return capture(*args, **kwargs)

    monkeypatch.setattr(module, "fork_sandbox", capture_async if asynchronous else capture)
    native = cls(Sandbox(metadata=Metadata(name="source"), spec=SandboxSpec(runtime=SandboxRuntime(ttl="300s"))))
    assert native.spec.runtime.ttl == "300s"
    result = asyncio.run(native.fork("child")) if asynchronous else native.fork("child")
    assert result is expected
    assert requests == [("source", {"targetName": "child", "targetType": "sandbox"})]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 500])
def test_fork_error_class_matches_native_parser(status):
    import httpx
    from blaxel.core.client.api.compute import fork_sandbox
    from blaxel.core.client.client import client
    from blaxel.core.sandbox.default.sandbox import _unwrap_response

    from arker._compat_blaxel_errors import translate_error
    from arker.computer import ArkerError

    response = httpx.Response(status, json={"code": status, "error": "failure"})
    with pytest.raises(Exception) as native:
        _unwrap_response(fork_sandbox._parse_response(client=client, response=response), "fork")
    mapped = translate_error(ArkerError("conflict", "failure", status), "fork")
    assert type(mapped) is type(native.value)
    assert mapped.status_code == native.value.status_code
