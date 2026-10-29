import asyncio
import inspect
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from async_lifecycle_helpers import configure_router as configure_router
from e2b import AsyncSandbox, AsyncVolume, Sandbox, Volume
from e2b_facade_helpers import delegation_response

from arker._compat_e2b_mounts import ArkerVolume, Volumes
from arker.computer import ArkerError


def filesystem(identifier="fs-one", name="data"):
    return SimpleNamespace(filesystem_id=identifier, name=name)


@pytest.fixture
def client():
    client = Mock()
    client.create_filesystem.return_value = filesystem()
    client.get_filesystem.side_effect = lambda identifier: filesystem(identifier)
    client.list_filesystems.return_value = SimpleNamespace(filesystems=[filesystem()], next_cursor=None)
    client.delete_filesystem.return_value = SimpleNamespace(deleted=True)
    return client


def test_installed_native_signatures():
    for volume in (Volume, AsyncVolume):
        assert "name" in inspect.signature(volume.create).parameters
        assert "volume_id" in inspect.signature(volume.connect).parameters
        assert "volume_id" in inspect.signature(volume.destroy).parameters
    for sandbox in (Sandbox, AsyncSandbox):
        assert "volume_mounts" in inspect.signature(sandbox.create).parameters
    assert inspect.iscoroutinefunction(AsyncVolume.create)


def test_real_create_connect_and_mount_requests(client):
    volumes = Volumes(client)
    handle = volumes.create("data")
    assert handle == ArkerVolume("fs-one", "data")
    client.create_filesystem.assert_called_once_with(name="data")
    vm = Mock()
    vm.create_mount.return_value = {"mount_id": "mount-one"}
    assert volumes.attach(vm, {"/shared": handle}) == [{"mount_id": "mount-one"}]
    vm.create_mount.assert_called_once_with(filesystem_id="fs-one", path="/shared")
    assert volumes.destroy(handle.volume_id) is True
    client.delete_filesystem.assert_called_once_with("fs-one")


def test_names_are_exact_and_paginated_never_ids(client):
    client.list_filesystems.side_effect = [
        SimpleNamespace(filesystems=[filesystem("fs-other", "data-extra")], next_cursor="page2"),
        SimpleNamespace(filesystems=[filesystem()], next_cursor=None),
    ]
    assert Volumes(client).by_name("data").volume_id == "fs-one"
    assert client.list_filesystems.call_args_list[1].kwargs == {"cursor": "page2"}
    client.list_filesystems.side_effect = None
    with pytest.raises(LookupError):
        Volumes(client).resolve({"/shared": "fs-one"})
    client.create_filesystem.assert_not_called()


def test_native_handles_rejected_before_any_requests(client):
    vm = Mock()
    for native in (Volume("fs-one", "data"), AsyncVolume("fs-one", "data")):
        with pytest.raises(TypeError, match="native backend"):
            Volumes(client).attach(vm, {"/first": "data", "/second": native})
    assert client.mock_calls == []
    vm.create_mount.assert_not_called()


@pytest.mark.parametrize("path", ["/", "relative", "/a/../b", "/a..b", "/a//b", "/a/.", "/a/", "/\0", "/" + "é" * 128])
def test_path_preflight_prevents_partial_mount(client, path):
    vm = Mock()
    with pytest.raises(ValueError):
        Volumes(client).attach(vm, {"/valid": "data", path: "other"})
    assert client.mock_calls == []
    vm.create_mount.assert_not_called()


@pytest.mark.parametrize("name", ["", ".hidden", "trailing.", "a/b", "é", "a" * 129])
def test_name_validation(client, name):
    with pytest.raises(ValueError):
        Volumes(client).create(name)
    client.create_filesystem.assert_not_called()


def test_ambiguity_repeated_cursor_and_duplicate_mount(client):
    volumes = Volumes(client)
    vm = Mock()
    with pytest.raises(ValueError, match="once per VM"):
        volumes.attach(vm, {"/a": "data", "/b": ArkerVolume("fs-one", "data")})
    vm.create_mount.assert_not_called()
    client.list_filesystems.return_value = SimpleNamespace(
        filesystems=[filesystem(), filesystem("fs-two")], next_cursor=None
    )
    with pytest.raises(ValueError, match="ambiguous"):
        volumes.by_name("data")
    client.list_filesystems.return_value = SimpleNamespace(filesystems=[], next_cursor="loop")
    with pytest.raises(RuntimeError, match="cursor"):
        volumes.list()


def test_overlap_and_partial_failure_do_not_detach_existing_mount(client):
    volumes = Volumes(client)
    vm = Mock()
    mounts = {"/a": ArkerVolume("fs-one", "data"), "/a/b": ArkerVolume("fs-two", "other")}
    with pytest.raises(ValueError, match="Overlapping"):
        volumes.attach(vm, mounts)
    vm.create_mount.assert_not_called()
    vm.create_mount.side_effect = [{"mount_id": "existing"}, RuntimeError("attach failed")]
    with pytest.raises(RuntimeError, match="attach failed"):
        volumes.attach(vm, {"/a": mounts["/a"], "/b": mounts["/a/b"]})
    vm.delete_mount.assert_not_called()
    client.delete_filesystem.assert_not_called()


def test_create_conflict_and_delete_errors_propagate(client):
    conflict = ArkerError(code="already_exists", message="Name exists", status=409)
    client.create_filesystem.side_effect = conflict
    with pytest.raises(ArkerError) as error:
        Volumes(client).create("data")
    assert error.value is conflict
    client.list_filesystems.assert_not_called()
    client.delete_filesystem.side_effect = ArkerError(code="not_found", message="Missing", status=404)
    assert Volumes(client).destroy("missing") is False
    client.delete_filesystem.side_effect = conflict
    with pytest.raises(ArkerError):
        Volumes(client).destroy("fs-one")


def test_exported_async_volume_lifecycle_runs_off_loop(client, configure_router, monkeypatch):
    import arker._compat_e2b_mounts as mounts
    import arker.e2b as shim
    from arker._compat import UnsupportedCompatibilityOption

    configure_router(["arker"])
    monkeypatch.setattr(mounts, "arker_client", lambda _config: client)
    loop_thread = threading.get_ident()
    for name in ("create_filesystem", "get_filesystem", "list_filesystems", "delete_filesystem"):
        method = getattr(client, name)
        original = method.side_effect
        result = method.return_value

        def off_loop(*args, _original=original, _result=result, **kwargs):
            assert threading.get_ident() != loop_thread
            return _original(*args, **kwargs) if _original else _result

        method.side_effect = off_loop

    async def run():
        volume = await shim.AsyncVolume.create("data")
        assert volume._client is client
        reused = await shim.AsyncVolume.connect(volume.volume_id)
        assert reused == volume and reused._client is client
        assert await shim.AsyncVolume.get_info(volume.volume_id) == volume
        assert await shim.AsyncVolume.list() == [volume]
        for method in (volume.list, volume.get_info):
            with pytest.raises(UnsupportedCompatibilityOption, match="content API"):
                method("/")
        assert await shim.AsyncVolume.destroy(volume.volume_id)

    asyncio.run(run())
    client.create_filesystem.assert_called_once_with(name="data")
    client.delete_filesystem.assert_called_once_with("fs-one")


@pytest.mark.parametrize("mount_fails", [False, True])
def test_cancelled_exported_sandbox_cleans_up_after_mount_settles(client, monkeypatch, mount_fails):
    import arker.e2b as shim
    from arker import _compat_e2b_bootstrap as bootstrap

    started, release, deleted = threading.Event(), threading.Event(), threading.Event()
    events = []
    loop_thread = threading.get_ident()
    vm = Mock(id="child", _client=client, base_url="https://worker.example")

    def request(method, path, body, **kwargs):
        assert (method, path, body) == ("POST", "/v1/vms/child/file-transfer-delegation", {})
        assert kwargs == {"base_url": vm.base_url}
        events.append("delegation")
        return delegation_response()

    client._request.side_effect = request

    def mount(**kwargs):
        assert threading.get_ident() != loop_thread
        assert kwargs == {"filesystem_id": "fs-one", "path": "/workspace/shared"}
        started.set()
        assert release.wait(5)
        events.append("settled")
        if mount_fails:
            raise RuntimeError("attach failed")
        return {"mount_id": "mount-one"}

    def delete(**options):
        events.append("deleted")
        deleted.set()
        return SimpleNamespace(deleted=True)

    vm.create_mount.side_effect = mount
    vm.delete.side_effect = delete
    monkeypatch.setattr(shim, "arker_create", lambda *a, **kw: vm)
    monkeypatch.setattr(shim, "routed_create", lambda _name, _config, creators: ("arker", creators["arker"]()))
    monkeypatch.setattr(bootstrap, "bootstrap_envd", lambda current: (object(), {}))
    monkeypatch.setattr(shim, "run_command", lambda *a, **kw: SimpleNamespace(exit_code=0))

    async def run():
        task = asyncio.create_task(shim.AsyncSandbox.create("ubuntu", volume_mounts={"/workspace/shared": "data"}))
        try:
            if not await asyncio.to_thread(started.wait, 5):
                await task
                pytest.fail("Sandbox creation did not attach the mount")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            vm.delete.assert_not_called()
        finally:
            release.set()
        assert await asyncio.to_thread(deleted.wait, 5)

    asyncio.run(run())
    assert events == ["delegation", "settled", "deleted"]
    client._request.assert_called_once()
    vm.delete.assert_called_once()
    client.delete_filesystem.assert_not_called()


def test_backend_store_create_mount_reconnect_destroy(client):

    # The routed facade returns these backend handles for Arker storage.
    Volume = Volumes(client)
    volume = Volume.create("data")
    assert isinstance(volume, ArkerVolume)
    assert volume.volume_id == "fs-one"
    assert volume.name == "data"
    mounts = {"/workspace/shared": volume}
    vm = Mock()
    Volumes(client).attach(vm, mounts)
    vm.create_mount.assert_called_once_with(filesystem_id=volume.volume_id, path="/workspace/shared")
    # A new backend store can reconnect explicitly without an ownership cache.
    ReconnectedVolume = Volumes(client)
    reused = ReconnectedVolume.connect(volume.volume_id)
    assert reused.volume_id == volume.volume_id
    assert Volume.get_info(volume.volume_id).name == "data"
    assert [info.volume_id for info in Volume.list()] == [volume.volume_id]
    assert ReconnectedVolume.destroy(reused.volume_id)


def test_facade_never_uses_sandbox_routing_or_switches_client(client, monkeypatch):
    from arker import _compat

    forbidden = Mock(side_effect=AssertionError("sandbox routing must not be used"))
    monkeypatch.setattr(_compat, "routed_get", forbidden)
    monkeypatch.setattr(_compat, "routed_create", forbidden)
    Volume = Volumes(client)
    vm_shaped_id = "01h00000000000000000000000"
    assert Volume.connect(vm_shaped_id).volume_id == vm_shaped_id
    client.get_filesystem.assert_called_with(vm_shaped_id)
    other_client = Mock()
    volume = Volume.create("data")
    with pytest.raises(ValueError, match="another Arker client"):
        Volumes(other_client).attach(Mock(), {"/workspace/shared": volume})
    assert other_client.mock_calls == []
    client.get_filesystem.side_effect = ArkerError(code="not_found", message="missing", status=404)
    with pytest.raises(ArkerError):
        Volume.connect("native-looking-id")
    forbidden.assert_not_called()


def test_native_facade_preserves_vendor_classes_and_options_are_not_swallowed(
    client, tmp_path, configure_router, monkeypatch
):
    import arker._compat_e2b_mounts as mounts
    from arker._compat import UnsupportedCompatibilityOption

    assert mounts._native_volume() is Volume
    assert mounts._native_volume(True) is AsyncVolume
    configure_router(cache_path=tmp_path / "routes.json", order=["arker"])
    monkeypatch.setattr(mounts, "arker_client", lambda _config: client)
    BoundVolume = mounts.Volume
    with pytest.raises(UnsupportedCompatibilityOption, match="per-call"):
        BoundVolume.create("data", api_key="different-route")
    client.create_filesystem.assert_not_called()
    volume = BoundVolume.create("data")
    with pytest.raises(UnsupportedCompatibilityOption, match="tokens"):
        _ = volume.token
    with pytest.raises(UnsupportedCompatibilityOption, match="tokens"):
        _ = BoundVolume.get_info(volume.volume_id).token
    for method in (volume.list, volume.get_info):
        with pytest.raises(UnsupportedCompatibilityOption, match="content API"):
            method("/")
    with pytest.raises(UnsupportedCompatibilityOption, match="content API"):
        volume.read("/file")


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("failure", [None, "attach", "readiness"])
def test_sandbox_creation_mounts_waits_and_cleans_child(client, monkeypatch, async_mode, failure):
    import arker.e2b as shim

    events = []
    vm = Mock(id="child", _client=client, base_url="https://worker.example")

    def request(method, path, body, **kwargs):
        assert (method, path, body) == ("POST", "/v1/vms/child/file-transfer-delegation", {})
        assert kwargs == {"base_url": vm.base_url}
        events.append("delegation")
        return delegation_response()

    client._request.side_effect = request
    vm.delete.side_effect = lambda: events.append("delete")

    def attach(**kwargs):
        events.append("attach")
        assert kwargs == {"filesystem_id": "fs-one", "path": "/workspace/shared"}
        if failure == "attach":
            raise RuntimeError("attach failed")
        return {"mount_id": "mount-one"}

    vm.create_mount.side_effect = attach
    monkeypatch.setattr(shim, "arker_create", lambda *a, **kw: vm)
    monkeypatch.setattr(shim, "routed_create", lambda _name, _config, creators: ("arker", creators["arker"]()))
    from arker import _compat_e2b_bootstrap as bootstrap
    from arker import _compat_e2b_data_plane as data_plane

    monkeypatch.setattr(bootstrap, "bootstrap_envd", lambda current: (events.append("bootstrap") or object(), {}))
    monkeypatch.setattr(
        data_plane,
        "create_data_plane",
        lambda *a, **k: SimpleNamespace(commands=object(), files=object(), git=object(), close=lambda: None),
    )

    def ready(_vm, command, **kwargs):
        events.append("readiness")
        assert "/proc/self/mountinfo" in command
        assert "/workspace/shared" in command
        return SimpleNamespace(exit_code=1 if failure == "readiness" else 0)

    monkeypatch.setattr(shim, "run_command", ready)
    volume = Volumes(client).create("data")

    def create():
        options = {"volume_mounts": {"/workspace/shared": volume}}
        return (
            asyncio.run(shim.AsyncSandbox.create("ubuntu", **options))
            if async_mode
            else shim.Sandbox.create("ubuntu", **options)
        )

    if failure:
        with pytest.raises(RuntimeError):
            create()
        assert events == [
            "bootstrap",
            "delegation",
            "attach",
            *(["readiness"] if failure == "readiness" else []),
            "delete",
        ]
        vm.delete.assert_called_once()
    else:
        assert create().sandbox_id == "child"
        assert events == ["bootstrap", "delegation", "attach", "readiness"]
        vm.delete.assert_not_called()
    client._request.assert_called_once()
    client.delete_filesystem.assert_not_called()


def test_sandbox_backend_mismatch_refuses_before_creation(client, monkeypatch):
    import arker.e2b as shim

    monkeypatch.setattr(shim, "arker_create", Mock(side_effect=AssertionError("must not fork")))
    with pytest.raises(shim.UnsupportedCompatibilityOption, match="native E2B"):
        shim.Sandbox._create_arker({}, None, None, None, {"volume_mounts": {"/workspace/shared": Volume("id", "data")}})
    native = Mock()
    monkeypatch.setattr(shim.Sandbox, "_native_sandbox", lambda: native)
    monkeypatch.setattr(shim, "routed_create", lambda _n, _c, creators: ("e2b", creators["e2b"]()))
    with pytest.raises(shim.UnsupportedCompatibilityOption, match="Arker sandbox"):
        shim.Sandbox.create(volume_mounts={"/workspace/shared": Volumes(client).create("data")})
    native.create.assert_not_called()


def test_native_mounts_and_volume_options_are_delegated_unchanged(monkeypatch):
    import arker.e2b as shim

    volume = Volume("native-id", "data")
    native = Mock()
    native.create.return_value = SimpleNamespace(sandbox_id="native")
    monkeypatch.setattr(shim.Sandbox, "_native_sandbox", lambda: native)
    monkeypatch.setattr(shim, "routed_create", lambda _n, _c, creators: ("e2b", creators["e2b"]()))
    mounts = {"/workspace/shared": volume}
    shim.Sandbox.create(volume_mounts=mounts)
    assert native.create.call_args.kwargs["volume_mounts"] is mounts
    monkeypatch.setattr(Volume, "create", Mock(return_value=volume))
    assert shim.Volume.create("data", api_key="synthetic") is volume
    Volume.create.assert_called_once_with("data", api_key="synthetic")


def test_readiness_script_rejects_plain_directory(tmp_path):
    import subprocess

    from arker._compat_e2b_mounts import mount_ready_command

    result = subprocess.run(mount_ready_command([str(tmp_path)], 0), shell=True, capture_output=True, text=True)
    assert result.returncode != 0
    assert "readiness timed out" in result.stderr


def test_failed_mount_cleanup_is_reported(client, monkeypatch):
    import arker.e2b as shim

    vm = Mock(id="child", _client=client)
    vm.create_mount.side_effect = RuntimeError("attach failed")
    vm.delete.side_effect = RuntimeError("delete failed")
    monkeypatch.setattr(shim, "routed_create", lambda *a, **kw: ("arker", vm))
    monkeypatch.setattr(shim.Sandbox, "_prepare_data_plane", lambda self: None)
    with pytest.raises(RuntimeError, match=r"^Sandbox creation and cleanup failed for VM child$"):
        shim.Sandbox.create(volume_mounts={"/workspace/shared": "data"})
    client.delete_filesystem.assert_not_called()


@pytest.mark.parametrize("async_mode", [False, True])
def test_import_only_volume_lifecycle_uses_arker_router(client, tmp_path, configure_router, monkeypatch, async_mode):
    import arker._compat_e2b_mounts as mounts
    import arker.e2b as shim

    cache = configure_router(cache_path=tmp_path / "routes.json", order=["arker"])
    monkeypatch.setattr(mounts, "arker_client", lambda _config: client)

    async def run():
        api = shim.AsyncVolume if async_mode else shim.Volume

        async def call(method, *args):
            result = getattr(api, method)(*args)
            return await result if async_mode else result

        volume = await call("create", "data")
        assert isinstance(volume, ArkerVolume)
        assert (await call("connect", volume.volume_id)).volume_id == volume.volume_id
        assert (await call("get_info", volume.volume_id)).name == "data"
        assert len(await call("list")) == 1
        assert await call("destroy", volume.volume_id)
        client.delete_filesystem.assert_called_once_with(volume.volume_id)

    asyncio.run(run())
    import json

    entries = json.loads(cache.read_text())["entries"]
    assert "e2b-volume:fs-one" in entries
    assert "fs-one" not in entries
    assert "data" not in entries


@pytest.mark.parametrize("async_mode", [False, True])
def test_volume_native_route_ignores_vm_cache_and_grammar(client, tmp_path, configure_router, monkeypatch, async_mode):
    import json
    from unittest.mock import AsyncMock

    import arker._compat_e2b_mounts as mounts
    import arker.e2b as shim

    identifier = "01h00000000000000000000000"
    cache = configure_router(cache_path=tmp_path / "routes.json", order=["e2b", "arker"])
    cache.write_text(json.dumps({"version": 1, "entries": {identifier: {"provider": "arker", "accessedAt": 1}}}))
    native_class = AsyncVolume if async_mode else Volume
    handle = native_class(identifier, "data")
    make_mock = AsyncMock if async_mode else Mock
    for method, result in [("create", handle), ("connect", handle), ("get_info", handle), ("destroy", True)]:
        monkeypatch.setattr(native_class, method, make_mock(return_value=result))
    unexpected = Mock(side_effect=AssertionError("Native volume must not use Arker"))
    monkeypatch.setattr(mounts, "arker_client", unexpected)

    async def run():
        api = shim.AsyncVolume if async_mode else shim.Volume
        for method, arg in [
            ("create", "data"),
            ("connect", identifier),
            ("get_info", identifier),
            ("destroy", identifier),
        ]:
            result = getattr(api, method)(arg)
            if async_mode:
                result = await result
            assert result is (True if method == "destroy" else handle)

    asyncio.run(run())
    unexpected.assert_not_called()
    assert json.loads(cache.read_text())["entries"][identifier]["provider"] == "arker"
    assert json.loads(cache.read_text())["entries"]["e2b-volume:" + identifier]["provider"] == "e2b"


def test_volume_only_404_allows_next_lookup_provider(client, tmp_path, configure_router, monkeypatch):
    from e2b.exceptions import AuthenticationException, VolumeNotFoundException

    import arker._compat_e2b_mounts as mounts
    import arker.e2b as shim

    configure_router(cache_path=tmp_path / "routes.json", order=["e2b", "arker"])
    make_client = Mock(return_value=client)
    monkeypatch.setattr(mounts, "arker_client", make_client)
    denied = AuthenticationException("denied")
    denied.status_code = 403
    monkeypatch.setattr(Volume, "connect", Mock(side_effect=denied))
    with pytest.raises(AuthenticationException) as caught:
        shim.Volume.connect("shared-id")
    assert caught.value is denied
    make_client.assert_not_called()
    monkeypatch.setattr(Volume, "connect", Mock(side_effect=VolumeNotFoundException("missing")))
    assert isinstance(shim.Volume.connect("shared-id"), ArkerVolume)


def test_weighted_volume_create_uses_policy_without_retrying_remote_failure(
    client, tmp_path, configure_router, monkeypatch
):
    import arker._compat_e2b_mounts as mounts
    import arker.e2b as shim

    configure_router(
        cache_path=tmp_path / "routes.json",
        order=["arker", "e2b"],
        create={
            "strategy": "weighted",
            "weights": {"arker": 1},
            "fallback": ["e2b"],
        },
    )
    monkeypatch.setattr(mounts, "arker_client", lambda _config: client)
    client.create_filesystem.side_effect = ArkerError(code="unavailable", message="uncertain", status=503)
    native = Mock(side_effect=AssertionError("Do not retry a remote create"))
    monkeypatch.setattr(Volume, "create", native)
    with pytest.raises(ArkerError):
        shim.Volume.create("data")
    native.assert_not_called()


def test_volume_list_uses_configured_backends_and_propagates_errors(client, tmp_path, configure_router, monkeypatch):
    import arker._compat_e2b_mounts as mounts
    import arker.e2b as shim

    configure_router(cache_path=tmp_path / "routes.json", order=["arker", "e2b"])
    monkeypatch.setattr(mounts, "arker_client", lambda _config: client)
    native = Volume("native-id", "data")
    monkeypatch.setattr(Volume, "list", Mock(return_value=[native]))
    listed = shim.Volume.list()
    assert isinstance(listed[0], ArkerVolume)
    assert listed[1] is native
    denied = RuntimeError("list denied")
    monkeypatch.setattr(Volume, "list", Mock(side_effect=denied))
    with pytest.raises(RuntimeError) as caught:
        shim.Volume.list()
    assert caught.value is denied


def test_separate_routed_client_instances_can_mount_same_scope(monkeypatch):
    from arker import Arker
    from arker._compat_e2b_mounts import mount_store

    first = Arker(api_key="synthetic", base_url="https://example.invalid")
    second = Arker(api_key="synthetic", base_url="https://example.invalid")
    monkeypatch.setattr(first, "get_filesystem", lambda identifier: filesystem(identifier))
    mounts = {"/a": ArkerVolume("fs-one", "data", first), "/b": ArkerVolume("fs-two", "other", second)}
    assert len(mount_store(first, mounts).resolve(mounts)) == 2
