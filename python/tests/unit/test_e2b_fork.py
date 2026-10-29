import asyncio
import builtins
import inspect
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from e2b import InvalidArgumentException, RateLimitException

from arker._compat_e2b_fork import fork_arker, fork_arker_async
from arker._compat_e2b_lifecycle import LIFETIME_POLICY_ID
from arker._compat_e2b_network import APP, SECRET, traffic_policies
from arker.computer import ArkerError
from arker.e2b import AsyncSandbox, Sandbox


def fixture():
    source = Mock(id="parent")
    source.metadata = {
        "arker_e2b": {
            "version": 1,
            "metadata": {"team": "test"},
            "source": {"kind": "vm_name", "value": "source"},
            "envd_version": "0.10.2",
        }
    }
    source.policies, _ = traffic_policies({}, {}, False)
    source.refresh.return_value = source
    checkpoint = Mock(id="checkpoint")
    children = [Mock(id=f"child-{i}") for i in range(2)]
    source.fork.return_value = checkpoint
    checkpoint.fork.side_effect = children
    prepare = lambda vm: SimpleNamespace(_vm=vm, _data_plane=None)
    return source, checkpoint, children, prepare


def test_capture_once_no_checkpoint_execution_and_private_tokens():
    parent, checkpoint, children, prepare = fixture()
    before = parent.policies.copy()
    results = fork_arker(parent, prepare, count=2, timeout=42, request_timeout=7)
    assert [r._vm for r in results] == children
    parent.fork.assert_called_once()
    assert parent.fork.call_args.kwargs["metadata"] == {
        "arker_e2b_checkpoint": {"version": 1, "source_vm_id": "parent"}
    }
    assert parent.policies == before
    assert parent.fork.call_args.kwargs["immutable"] is True
    removed = parent.fork.call_args.kwargs["policies"]["rules"]
    assert removed[APP] is None and removed[LIFETIME_POLICY_ID]["action"] == "delete"
    tokens = []
    for call, child in zip(checkpoint.fork.call_args_list, children, strict=True):
        assert 0 < call.kwargs["request_timeout"] <= 7
        assert call.kwargs["metadata"] == parent.metadata
        policies = call.kwargs["policies"]
        assert policies["rules"][LIFETIME_POLICY_ID]["after_created"] == 42
        tokens.append(policies["secrets"][SECRET])
        assert child._e2b_traffic_token == tokens[-1]
    assert len({*tokens, before["secrets"][SECRET]}) == 3
    assert [c[0] for c in checkpoint.mock_calls] == ["fork", "fork", "delete"]
    for child in children:
        child.delete.assert_not_called()


def test_child_failure_is_independent_and_cleans_child():
    parent, checkpoint, children, prepare = fixture()

    def initialize(vm):
        if vm is children[0]:
            raise ArkerError("capacity_unavailable", "capacity", 429)
        return prepare(vm)

    values = fork_arker(parent, initialize, count=2)
    assert isinstance(values[0], RateLimitException)
    assert values[1]._vm is children[1]
    children[0].delete.assert_called_once()
    children[1].delete.assert_not_called()
    checkpoint.delete.assert_called_once()


@pytest.mark.parametrize("count", [0, 21, True, 1.5, "2"])
def test_validation_before_capture(count):
    parent, _, _, prepare = fixture()
    with pytest.raises(InvalidArgumentException):
        fork_arker(parent, prepare, count=count)
    assert not parent.mock_calls


def test_checkpoint_cleanup_failure_does_not_lose_successful_children():
    parent, checkpoint, children, prepare = fixture()
    checkpoint.delete.side_effect = RuntimeError("delete checkpoint")
    with pytest.raises(builtins.ExceptionGroup, match="cleanup was incomplete"):
        fork_arker(parent, prepare, count=2)
    for child in children:
        child.delete.assert_called_once()


def test_facade_sync_uses_same_capture_and_native_owner(monkeypatch):
    parent, _checkpoint, _children, _ = fixture()
    monkeypatch.setattr(Sandbox, "_prepare_data_plane", lambda self: None)
    values = Sandbox(vm=parent).fork(count=2)
    assert all(isinstance(value, Sandbox) for value in values)
    native = Mock()
    error = RuntimeError("native child")
    native.fork.return_value = [error, SimpleNamespace(sandbox_id="native-child")]
    values = Sandbox(native=native).fork(timeout=19, count=2, api_key="native-key")
    native.fork.assert_called_once_with(timeout=19, count=2, api_key="native-key")
    assert values[0] is error and values[1]._native.sandbox_id == "native-child"
    assert list(inspect.signature(Sandbox.fork).parameters) == ["sandbox_id", "timeout", "count", "opts"]
    assert inspect.iscoroutinefunction(AsyncSandbox.fork)


@pytest.mark.parametrize("repeat_cancel", [False, True])
def test_async_cancel_inflight_fork_cleans_late_child_offloop(repeat_cancel):
    async def scenario():
        parent, checkpoint, children, prepare = fixture()
        entered, release = threading.Event(), threading.Event()
        loop_thread = threading.get_ident()

        def create(**kwargs):
            assert threading.get_ident() != loop_thread
            entered.set()
            release.wait(5)
            return children[0]

        checkpoint.fork.side_effect = create
        task = asyncio.create_task(fork_arker_async(parent, prepare, count=2))
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        if repeat_cancel:
            await asyncio.sleep(0)
            task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        checkpoint.fork.assert_called_once()
        children[0].delete.assert_called_once()
        checkpoint.delete.assert_called_once()

    asyncio.run(scenario())


def test_async_facade_child_descriptors_and_urls(monkeypatch):
    import base64
    import hashlib
    import hmac
    import json
    from urllib.parse import parse_qs, urlsplit

    import arker._compat_e2b_bootstrap as bootstrap
    import arker._compat_e2b_data_plane as data_plane
    from arker._compat_e2b_async import _dispose_fork_clients

    def encode(value):
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    async def scenario():
        parent, _checkpoint, children, _ = fixture()
        loop_thread = threading.get_ident()
        inherited_token = "inherited-envd-token"
        parent_authority = Mock()
        parent._e2b_file_delegation = parent_authority
        parent._client._request.side_effect = AssertionError("Parent authority must not be requested")
        reconnect = Mock(side_effect=AssertionError("Fork must prepare the child directly"))
        monkeypatch.setattr(Sandbox, "connect", reconnect)
        monkeypatch.setattr(AsyncSandbox, "connect", reconnect)

        def boot(vm):
            assert threading.get_ident() != loop_thread
            return SimpleNamespace(vm_id=vm.id, envd_access_token=inherited_token), {"ready": True}

        monkeypatch.setattr(bootstrap, "bootstrap_envd", boot)
        planes = []
        keys = {}
        for child in children:
            child.base_url = "https://regional.example.test"
            child._e2b_file_delegation = parent_authority
            keys[child.id] = hashlib.sha256(child.id.encode()).digest()

            def delegate(method, path, body, *, base_url, child=child):
                assert threading.get_ident() != loop_thread
                assert (method, path, body, base_url) == (
                    "POST",
                    f"/v1/vms/{child.id}/file-transfer-delegation",
                    {},
                    child.base_url,
                )
                return {
                    "redemption_url": f"https://regional.example.test/api/v1/vms/{child.id}/file-transfer",
                    "context": encode(child.id.encode()),
                    "signing_key": encode(keys[child.id]),
                    "operations": ["read", "write"],
                    "expires_at": None,
                }

            child._client._request.side_effect = delegate

        class Plane:
            commands = files = git = None

            def __init__(self, vm_id):
                self.vm_id = vm_id

            async def aclose(self):
                planes.append(self.vm_id)

        monkeypatch.setattr(
            data_plane, "create_data_plane", lambda descriptor, key, asynchronous: Plane(descriptor.vm_id)
        )
        values = await AsyncSandbox(Sandbox(vm=parent)).fork(count=2)
        assert all(isinstance(value, AsyncSandbox) for value in values)
        for value, child in zip(values, children, strict=True):
            child._client._request.assert_called_once()
            child._client._request.reset_mock()
            child._client._request.side_effect = AssertionError("URL signing must stay local")
            parsed = urlsplit(value.download_url("file", user="image-user"))
            assert parsed.path == f"/api/v1/vms/{child.id}/file-transfer"
            ticket = parse_qs(parsed.query)["ticket"][0].removeprefix("ark_file_v1_")
            context, payload, tag = ticket.split(".")
            assert context == encode(child.id.encode())
            message = b"arker:file-transfer:v1\0" + f"{context}.{payload}".encode()
            assert tag == encode(hmac.digest(keys[child.id], message, "sha256"))
            body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            expected = base64.b64encode(hashlib.sha256(f"file:read:image-user:{inherited_token}".encode()).digest())
            assert body["envd_signature"] == "v1_" + expected.decode().rstrip("=")
            assert (body["path"], body["operation"], body["user"]) == ("file", "read", "image-user")
            child._client._request.assert_not_called()
        reconnect.assert_not_called()
        parent._client._request.assert_not_called()
        parent_authority.sign.assert_not_called()
        await _dispose_fork_clients(values)
        assert planes == [c.id for c in children]
        for child in children:
            child.delete.assert_called_once()

    asyncio.run(scenario())


def test_async_cleanup_attempts_all_handles_and_vms():
    from arker._compat_e2b_async import _dispose_fork_clients

    async def scenario():
        calls = []

        class Plane:
            async def aclose(self):
                calls.append("close")
                raise RuntimeError("close failed")

        def delete():
            calls.append("delete")
            raise RuntimeError("delete failed")

        handles = [
            SimpleNamespace(_data_plane=Plane(), _sandbox=SimpleNamespace(_vm=SimpleNamespace(delete=delete)))
            for _ in range(2)
        ]
        with pytest.raises(builtins.ExceptionGroup) as caught:
            await _dispose_fork_clients(handles)
        assert len(caught.value.exceptions) == 4
        assert calls == ["close", "delete", "close", "delete"]

    asyncio.run(scenario())


def test_class_fork_does_not_connect_or_change_parent_timeout(monkeypatch):
    parent, checkpoint, children, _ = fixture()
    monkeypatch.setattr(Sandbox, "_lookup", lambda *args: (("arker", parent), None))
    monkeypatch.setattr(Sandbox, "_prepare_data_plane", lambda self: None)
    values = Sandbox.fork("parent", count=2)
    assert [v._vm for v in values] == children
    parent.update.assert_not_called()
    checkpoint.update.assert_not_called()


def test_redacted_managed_ingress_reconstructed_custom_ingress_refused_before_capture():
    parent, checkpoint, _children, prepare = fixture()
    rule = parent.policies["rules"][APP]
    del rule["auth"]
    rule["match"]["headers"]["e2b-traffic-access-token"] = ["***"]
    results = fork_arker(parent, prepare, count=2)
    assert len(results) == 2
    for call in checkpoint.fork.call_args_list:
        app = call.kwargs["policies"]["rules"][APP]
        assert app["auth"] == "open"
        assert app["match"]["headers"]["e2b-traffic-access-token"] == ["${secret:" + SECRET + "}"]
    parent, checkpoint, _, prepare = fixture()
    parent.policies["rules"]["custom"] = {
        "type": "inbound",
        "action": "allow",
        "match": {"ports": [8080], "headers": {"secret": ["***"]}},
    }
    with pytest.raises(TypeError, match="custom inbound"):
        fork_arker(parent, prepare)
    parent.fork.assert_not_called()


def test_atomic_capture_failure_prevents_fanout():
    parent, checkpoint, _, prepare = fixture()
    parent.fork.side_effect = RuntimeError("capture refused")
    with pytest.raises(RuntimeError, match="capture refused"):
        fork_arker(parent, prepare)
    checkpoint.fork.assert_not_called()
    checkpoint.delete.assert_not_called()


def test_sync_cleanup_collects_close_and_delete_errors():
    from arker._compat_e2b_fork import dispose

    vm = Mock()
    vm.delete.side_effect = RuntimeError("delete failed")
    plane = Mock()
    plane.close.side_effect = RuntimeError("close failed")
    with pytest.raises(builtins.ExceptionGroup) as caught:
        dispose(SimpleNamespace(_vm=vm, _data_plane=plane))
    assert [str(error) for error in caught.value.exceptions] == ["close failed", "delete failed"]
    vm.delete.assert_called_once()


def test_checkpoint_deadline_is_the_finite_request_budget(monkeypatch):
    import arker._compat_e2b_fork as helper

    monkeypatch.setattr(helper.time, "monotonic", lambda: 10)
    parent, checkpoint, _, prepare = fixture()
    fork_arker(parent, prepare, request_timeout=7)
    retention = parent.fork.call_args.kwargs["policies"]["rules"][LIFETIME_POLICY_ID]["after_created"]
    assert retention == 7
    assert checkpoint.fork.call_args.kwargs["request_timeout"] == 7
    parent, _, _, prepare = fixture()
    with pytest.raises(TypeError, match="finite request_timeout"):
        fork_arker(parent, prepare, request_timeout=0)
    parent.fork.assert_not_called()


def test_all_children_capture_before_readiness_and_checkpoint_is_already_deleted():
    parent, checkpoint, _children, prepare = fixture()

    def ready(vm):
        assert checkpoint.fork.call_count == 2
        checkpoint.delete.assert_called_once()
        return prepare(vm)

    assert len(fork_arker(parent, ready, count=2)) == 2


def test_exhausted_capture_budget_stops_fanout_and_retains_ordered_child_errors(monkeypatch):
    import httpx

    import arker._compat_e2b_fork as helper

    now = [10.0]
    monkeypatch.setattr(helper.time, "monotonic", lambda: now[0])
    parent, checkpoint, children, prepare = fixture()

    def capture(**kwargs):
        now[0] += 8
        return children[0]

    checkpoint.fork.side_effect = capture
    values = fork_arker(parent, prepare, count=2, request_timeout=7)
    assert values[0]._vm is children[0]
    assert isinstance(values[1], httpx.ReadTimeout)
    checkpoint.fork.assert_called_once()
    checkpoint.delete.assert_called_once()


@pytest.mark.parametrize("asynchronous_list", [False, True])
def test_inventory_during_fanout_excludes_checkpoint_and_keeps_child_metadata(asynchronous_list):
    from arker._compat_e2b_inventory import AsyncInventoryPaginator, InventoryPaginator

    async def scenario():
        parent, checkpoint, children, prepare = fixture()
        rows = [parent]
        blocked, release = threading.Event(), threading.Event()
        for row in [parent, checkpoint, *children]:
            row.base_url = "https://regional.example.test"
            row.created_at = "2026-01-01T00:00:00Z"
            row.resources = {"vcpu": 2, "memory_mib": 512}
            row.refresh.return_value = row
            row._client._request.side_effect = lambda *args, row=row, **kwargs: {
                "vm_id": row.id,
                "state": "idle",
                "mode": None,
                "customer_paused": False,
                "started_at": "2026-01-01T00:00:00Z",
            }
        parent.policies["rules"][LIFETIME_POLICY_ID] = {
            "type": "schedule",
            "after_created": 300,
            "action": "delete",
        }

        def capture(**options):
            checkpoint.metadata = options["metadata"]
            checkpoint.immutable = options["immutable"]
            rows.append(checkpoint)
            return checkpoint

        def child_fork(**options):
            index = len(rows) - 2
            if index == 1:
                blocked.set()
                assert release.wait(5)
            child = children[index]
            child.metadata, child.policies = options["metadata"], options["policies"]
            rows.append(child)
            return child

        def listing(**options):
            expected = options["metadata_filter"]["arker_e2b"]
            assert expected == {"version": 1, "metadata": {}}
            selected = [row for row in rows if row.metadata.get("arker_e2b", {}).get("version") == expected["version"]]
            return SimpleNamespace(vms=selected, next_cursor=None)

        parent.fork.side_effect = capture
        checkpoint.fork.side_effect = child_fork
        checkpoint.delete.side_effect = lambda: rows.remove(checkpoint)
        running = asyncio.create_task(fork_arker_async(parent, prepare, count=2))
        try:
            assert await asyncio.to_thread(blocked.wait, 5)
            assert checkpoint in rows and checkpoint.immutable
            assert checkpoint.metadata == {"arker_e2b_checkpoint": {"version": 1, "source_vm_id": parent.id}}
            pages = InventoryPaginator(SimpleNamespace(list_vms=listing))
            if asynchronous_list:
                visible = await AsyncInventoryPaginator(pages).next_items()
            else:
                visible = pages.next_items()
            assert [row.sandbox_id for row in visible] == [parent.id, children[0].id]
            assert all(row.metadata == {"team": "test"} and row.template_id == "source" for row in visible)
        finally:
            release.set()
            results = await running
        assert [result._vm for result in results] == children
        assert checkpoint not in rows
        assert all(child.metadata == parent.metadata for child in children)

    asyncio.run(scenario())
