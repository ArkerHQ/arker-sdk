import asyncio
from types import SimpleNamespace

import pytest
from async_lifecycle_helpers import configure_router as configure_router

pytest.importorskip("daytona")

from daytona.common.volume import Volume

from arker import _daytona_volumes as volumes
from arker import daytona
from arker._compat import UnsupportedCompatibilityOption
from arker.computer import ArkerError


@pytest.fixture(autouse=True)
def native_mock_credentials(monkeypatch):
    # Native service doubles represent configured clients in these routing tests.
    monkeypatch.setenv("DAYTONA_API_KEY", "daytona-synthetic")


@pytest.fixture
def store(configure_router, monkeypatch):
    configure_router(["arker"])
    rows, actions = {}, []

    def create(*, name):
        if any(row.name == name for row in rows.values()):
            raise ArkerError("already_exists", "duplicate", 409)
        row = SimpleNamespace(
            filesystem_id=f"dir_{len(rows)}", name=name, owner_org_id="org", created_at="2026-01-01T00:00:00Z"
        )
        rows[row.filesystem_id] = row
        actions.append(("create", name))
        return row

    def get(identifier):
        if identifier not in rows:
            raise ArkerError("not_found", "missing", 404)
        return rows[identifier]

    def delete(identifier):
        get(identifier)
        del rows[identifier]
        actions.append(("delete", identifier))

    client = SimpleNamespace(
        create_filesystem=create,
        get_filesystem=get,
        delete_filesystem=delete,
        list_filesystems=lambda **kw: SimpleNamespace(filesystems=list(rows.values()), next_cursor=None),
    )
    monkeypatch.setattr(volumes, "arker_client", lambda config: client)
    return client, rows, actions


@pytest.mark.parametrize("asynchronous", [False, True])
def test_volume_lifecycle_native_types_and_committed_namespace_metadata(store, asynchronous):
    async def program():
        service = (daytona.AsyncDaytona() if asynchronous else daytona.Daytona()).volume

        async def call(method, *args, **kwargs):
            value = method(*args, **kwargs)
            return await value if asynchronous else value

        volume = await call(service.create, "shared")
        assert isinstance(volume, Volume)
        assert volume.state.value == "ready" and volume.error_reason is None
        assert volume.created_at == volume.updated_at
        assert volume.organization_id == "org" and volume.last_used_at is None
        assert (await call(service.get, "shared", create=True)).id == volume.id
        assert [v.id for v in await call(service.list)] == [volume.id]
        with pytest.raises(UnsupportedCompatibilityOption, match="atomically"):
            await call(service.delete, volume)
        store[0].delete_filesystem(volume.id)
        from daytona.common.errors import DaytonaNotFoundError

        with pytest.raises(DaytonaNotFoundError):
            await call(service.get, "shared")
        assert (await call(service.get, "new", create=True)).name == "new"

    asyncio.run(program())
    assert [action[0] for action in store[2]] == ["create", "delete", "create"]


def test_volume_forbidden_lookup_does_not_create_or_fallback(store, monkeypatch):
    def forbidden(**kwargs):
        raise ArkerError("forbidden", "denied", 403)

    monkeypatch.setattr(store[0], "list_filesystems", forbidden)
    from daytona.common.errors import DaytonaForbiddenError

    with pytest.raises(DaytonaForbiddenError):
        daytona.Daytona().volume.get("missing", create=True)
    assert not store[2]


def test_native_volume_identity_and_arguments_stay_native(configure_router, monkeypatch):
    configure_router(["daytona"])
    native_volume = Volume(
        id="native-id",
        name="native",
        organization_id="native-org",
        state="ready",
        created_at="c",
        updated_at="u",
        error_reason=None,
    )
    calls = []
    native = SimpleNamespace(
        create=lambda name: native_volume,
        get=lambda name: native_volume,
        list=lambda: [native_volume],
        delete=lambda value: calls.append(value),
    )
    monkeypatch.setattr(daytona.Daytona, "_native_client", lambda self: SimpleNamespace(volume=native))
    service = daytona.Daytona().volume
    assert service.create("native") is native_volume
    assert service.get("native") is native_volume
    assert service.list()[0] is native_volume
    service.delete(native_volume)
    assert calls == [native_volume]


def test_explicit_arker_handle_delete_never_routes_to_native(store, configure_router):
    volume = daytona.Daytona().volume.create("shared")
    configure_router(["daytona"])
    with pytest.raises(UnsupportedCompatibilityOption, match="atomically"):
        daytona.Daytona().volume.delete(volume)
    assert store[2] == [("create", "shared")]


@pytest.mark.parametrize("failure", [None, "mount", "readiness"])
def test_whole_mount_resolves_before_create_waits_as_daytona_and_cleans_failure(store, monkeypatch, failure):
    from daytona import VolumeMount

    volume = daytona.Daytona().volume.create("shared")
    actions = []

    def attach(**options):
        actions.append(("mount", options))
        if failure == "mount":
            raise RuntimeError("mount failed")

    vm = SimpleNamespace(id="owned", create_mount=attach, delete=lambda: actions.append(("delete",)))
    monkeypatch.setattr(daytona, "arker_create", lambda *a, **kw: actions.append(("create",)) or vm)

    def run(vm, command):
        assert "setpriv --reuid=daytona" in command
        actions.append(("ready",))
        return SimpleNamespace(exit_code=1 if failure == "readiness" else 0)

    monkeypatch.setattr(daytona, "run_command", run)
    mount = VolumeMount(volume_id=volume.id, mount_path="/data")
    if failure:
        with pytest.raises(RuntimeError):
            daytona.Daytona().create(volumes=[mount])
        assert actions[-1] == ("delete",)
    else:
        assert daytona.Daytona().create(volumes=[mount]).id == "owned"
        assert [a[0] for a in actions] == ["create", "mount", "ready"]
    assert actions[1] == ("mount", {"filesystem_id": volume.id, "path": "/data"})


def test_subpath_rejected_before_vm_creation(store, monkeypatch):
    from daytona import VolumeMount

    monkeypatch.setattr(daytona, "arker_create", lambda *a, **kw: pytest.fail("no VM creation"))
    with pytest.raises(UnsupportedCompatibilityOption, match="subpath"):
        daytona.Daytona().create(volumes=[VolumeMount(volume_id="dir_owned", mount_path="/data", subpath="secret")])


def test_native_sandbox_rejects_arker_id_before_native_creation(configure_router, monkeypatch):
    configure_router(["daytona"])
    monkeypatch.setattr(daytona.Daytona, "_native_client", lambda self: pytest.fail("no native call"))
    with pytest.raises(UnsupportedCompatibilityOption, match="Arker filesystem"):
        daytona.Daytona().create(volumes=[{"volume_id": "dir_owned", "mount_path": "/data"}])


def test_async_native_volume_stays_on_original_loop(configure_router, monkeypatch):
    configure_router(["daytona"])
    native_volume = Volume(
        id="native",
        name="native",
        organization_id="org",
        state="ready",
        created_at="c",
        updated_at="u",
        error_reason=None,
    )
    calls = []

    async def program():
        loop = asyncio.get_running_loop()

        async def create(name):
            assert asyncio.get_running_loop() is loop
            calls.append(name)
            return native_volume

        owner = daytona.AsyncDaytona()
        monkeypatch.setattr(owner, "_native_client", lambda: SimpleNamespace(volume=SimpleNamespace(create=create)))
        assert await owner.volume.create("native") is native_volume

    asyncio.run(program())
    assert calls == ["native"]


def test_mount_cleanup_failure_preserves_owned_id_and_original_error(store, monkeypatch):
    volume = daytona.Daytona().volume.create("shared")
    primary = RuntimeError("mount failed")
    cleanup = RuntimeError("delete failed")

    def attach(**kwargs):
        raise primary

    def delete():
        raise cleanup

    vm = SimpleNamespace(id="owned", create_mount=attach, delete=delete)
    monkeypatch.setattr(daytona, "arker_create", lambda *a, **kw: vm)
    with pytest.raises(RuntimeError, match="owned") as caught:
        daytona.Daytona().create(volumes=[{"volume_id": volume.id, "mount_path": "/data"}])
    assert caught.value.__cause__ is cleanup
    assert cleanup.__context__ is primary


def test_typed_create_params_preserve_native_mount_fields(store, monkeypatch):
    from daytona import CreateSandboxFromSnapshotParams, VolumeMount

    volume = daytona.Daytona().volume.create("typed")
    actions = []
    vm = SimpleNamespace(id="owned", create_mount=lambda **kw: actions.append(kw))
    monkeypatch.setattr(daytona, "arker_create", lambda *a, **kw: vm)
    monkeypatch.setattr(daytona, "run_command", lambda *a: SimpleNamespace(exit_code=0))
    daytona.Daytona().create(
        CreateSandboxFromSnapshotParams(volumes=[VolumeMount(volume_id=volume.id, mount_path="/data")])
    )
    assert actions == [{"filesystem_id": volume.id, "path": "/data"}]


def test_truncated_listing_cannot_be_mistaken_for_absence_or_complete_inventory(store, monkeypatch):
    row = store[0].create_filesystem(name="one")
    monkeypatch.setattr(
        store[0], "list_filesystems", lambda **kw: SimpleNamespace(filesystems=[row] * 500, next_cursor=None)
    )
    service = daytona.Daytona().volume
    with pytest.raises(UnsupportedCompatibilityOption, match="truncated"):
        service.list()
    with pytest.raises(UnsupportedCompatibilityOption, match="truncated"):
        service.get("missing", create=True)
    assert len(store[2]) == 1


@pytest.mark.parametrize("strategy", ["priority", "race"])
def test_same_name_across_providers_is_ambiguous_even_with_cached_owner(
    store, configure_router, monkeypatch, tmp_path, strategy
):
    from arker._router import JsonRouteCache

    daytona.Daytona().volume.create("shared")
    cache_path = tmp_path / "owners.json"
    configure_router(["arker", "daytona"], existing_strategy=strategy, cache_path=str(cache_path))
    JsonRouteCache(str(cache_path)).set("daytona-volume-name:shared", "arker")
    native = SimpleNamespace(get=lambda name: SimpleNamespace(id="native", name=name))
    monkeypatch.setattr(daytona.Daytona, "_native_client", lambda self: SimpleNamespace(volume=native))
    with pytest.raises(ValueError, match="ambiguous"):
        daytona.Daytona().volume.get("shared", create=True)
    assert store[2] == [("create", "shared")]


def test_duplicate_create_has_native_bad_request_identity(store):
    from daytona.common.errors import DaytonaBadRequestError

    service = daytona.Daytona().volume
    service.create("duplicate")
    with pytest.raises(DaytonaBadRequestError) as caught:
        service.create("duplicate")
    assert caught.value.status_code == 400


def test_auth_failure_cannot_fall_through_to_same_named_native_volume(store, configure_router, monkeypatch):
    from daytona.common.errors import DaytonaForbiddenError

    configure_router(["arker", "daytona"])

    def forbidden(**kwargs):
        raise ArkerError("forbidden", "denied", 403)

    monkeypatch.setattr(store[0], "list_filesystems", forbidden)
    native = SimpleNamespace(get=lambda name: SimpleNamespace(id="native", name=name))
    monkeypatch.setattr(daytona.Daytona, "_native_client", lambda self: SimpleNamespace(volume=native))
    with pytest.raises(DaytonaForbiddenError):
        daytona.Daytona().volume.get("same", create=True)
    assert not store[2]


def test_native_sandbox_cannot_mount_same_named_arker_volume(store, configure_router, monkeypatch):
    daytona.Daytona().volume.create("shared")
    configure_router(["arker", "daytona"], create_order=["daytona"])
    native = SimpleNamespace(
        volume=SimpleNamespace(list=list), create=lambda *a, **kw: pytest.fail("native create must not run")
    )
    monkeypatch.setattr(daytona.Daytona, "_native_client", lambda self: native)
    with pytest.raises(UnsupportedCompatibilityOption, match="Arker volumes"):
        daytona.Daytona().create(volumes=[{"volume_id": "shared", "mount_path": "/data"}])


def test_foreign_namespace_mount_does_not_fallback_or_create_vm(store, monkeypatch):
    from daytona.common.errors import DaytonaForbiddenError

    def forbidden(identifier):
        raise ArkerError("forbidden", "tenant mismatch", 403)

    monkeypatch.setattr(store[0], "get_filesystem", forbidden)
    monkeypatch.setattr(daytona, "arker_create", lambda *a, **kw: pytest.fail("must not create"))
    with pytest.raises(DaytonaForbiddenError):
        daytona.Daytona().create(volumes=[{"volume_id": "dir_foreign", "mount_path": "/data"}])


def test_cancelled_native_volume_creation_drains_and_deletes_before_close(configure_router, monkeypatch):
    configure_router(["daytona"])

    async def program():
        entered, release = asyncio.Event(), asyncio.Event()
        actions = []
        volume = SimpleNamespace(id="native-late")

        async def create(name):
            actions.append("create")
            entered.set()
            await release.wait()
            return volume

        async def delete(value):
            assert value is volume
            actions.append("delete")

        async def close():
            actions.append("close")

        client = daytona.AsyncDaytona()
        client._native = SimpleNamespace(volume=SimpleNamespace(create=create, delete=delete), close=close)
        pending = asyncio.create_task(client.volume.create("late"))
        await asyncio.wait_for(entered.wait(), 2)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 2)
        await client.close()
        assert actions == ["create", "delete", "close"]

    asyncio.run(program())


@pytest.mark.parametrize("path", ["../escape", "/data/../escape", "/data//nested", "/", "/data/."])
def test_mount_path_traversal_refused_before_namespace_lookup(store, monkeypatch, path):
    monkeypatch.setattr(store[0], "get_filesystem", lambda *a: pytest.fail("no lookup"))
    monkeypatch.setattr(daytona, "arker_create", lambda *a, **kw: pytest.fail("no VM creation"))
    with pytest.raises(ValueError, match="Mount path"):
        daytona.Daytona().create(volumes=[{"volume_id": "dir_owned", "mount_path": path}])


@pytest.mark.parametrize("subpath", ["child", "../escape", "/absolute", "a/../../escape", "a\0b"])
def test_subtree_is_never_emulated_or_admitted_before_backend_support(store, monkeypatch, subpath):
    monkeypatch.setattr(daytona, "arker_create", lambda *a, **kw: pytest.fail("no VM creation"))
    with pytest.raises(UnsupportedCompatibilityOption, match="subpath"):
        daytona.Daytona().create(volumes=[{"volume_id": "dir_owned", "mount_path": "/data", "subpath": subpath}])


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_late_cancel_drains_routing_bookkeeping_and_reports_cleanup_failure(
    configure_router, monkeypatch, cleanup_fails
):
    import threading

    configure_router(["daytona"])
    bookkeeping, release = threading.Event(), threading.Event()
    original = volumes.routed_create

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        bookkeeping.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(volumes, "routed_create", delayed)

    async def program():
        actions = []
        volume = SimpleNamespace(id="owned-late-volume")

        async def create(name):
            return volume

        async def delete(value):
            assert value is volume
            actions.append("delete")
            if cleanup_fails:
                raise RuntimeError("native deletion failed")

        async def close():
            actions.append("close")

        client = daytona.AsyncDaytona()
        client._native = SimpleNamespace(volume=SimpleNamespace(create=create, delete=delete), close=close)
        pending = asyncio.create_task(client.volume.create("late"))
        try:
            assert await asyncio.to_thread(bookkeeping.wait, 3)
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
            pending.cancel()
            release.set()
            if cleanup_fails:
                with pytest.raises(RuntimeError, match="owned-late-volume"):
                    await pending
            else:
                with pytest.raises(asyncio.CancelledError):
                    await pending
            await client.close()
            assert actions == ["delete", "close"]
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(program())


def test_volume_name_validation_uses_backend_error_contract(store, monkeypatch):
    from daytona.common.errors import DaytonaBadRequestError

    names = []

    def reject(*, name):
        names.append(name)
        raise ArkerError("bad_request", "invalid filesystem name", 400)

    monkeypatch.setattr(store[0], "create_filesystem", reject)
    with pytest.raises(DaytonaBadRequestError) as caught:
        daytona.Daytona().volume.create("name with spaces")
    assert caught.value.status_code == 400
    assert names == ["name with spaces"]
