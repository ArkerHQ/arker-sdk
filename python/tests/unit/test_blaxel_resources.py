import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("blaxel.core")
from blaxel.core.client.types import UNSET

from arker import _compat_blaxel_resources as resources
from arker import blaxel
from arker.computer import ArkerError


class Filesystems:
    def __init__(self):
        self.files = {}
        self.deleted = []
        self.calls = []
        self.refuse_delete = False

    def create_filesystem(self, *, name):
        self.calls.append(("create", name))
        if any(file.name == name for file in self.files.values()):
            raise ArkerError("already_exists", "exists", 409)
        file = SimpleNamespace(filesystem_id=f"dir-{len(self.files)}", name=name, region="region-one")
        self.files[file.filesystem_id] = file
        return file

    def get_filesystem(self, identifier):
        self.calls.append(("get", identifier))
        if identifier not in self.files:
            raise ArkerError("not_found", "missing", 404)
        return self.files[identifier]

    def list_filesystems(self, *, name_prefix=None, cursor=None, limit=None):
        self.calls.append(("list", cursor))
        values = [file for file in self.files.values() if name_prefix is None or file.name.startswith(name_prefix)]
        start = int(cursor or 0)
        page = values[start : start + (limit or 1)]
        next_cursor = str(start + len(page)) if start + len(page) < len(values) else None
        return SimpleNamespace(filesystems=page, next_cursor=next_cursor)

    def delete_filesystem(self, identifier, *, only_if_unmounted):
        assert only_if_unmounted is True
        self.deleted.append(identifier)
        if self.refuse_delete:
            raise ArkerError("resource_busy", "mounted elsewhere", 409)
        self.files.pop(identifier)
        return SimpleNamespace(deleted=True)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "router.json"
    backend = Filesystems()
    monkeypatch.setattr(resources, "arker_client", lambda config: backend)
    monkeypatch.setenv("COMPUTESDK_ROUTER_CONFIG", str(path))
    monkeypatch.setenv("ARKER_API_KEY", "test")

    def policy(create=("arker",), existing=("arker",)):
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "create": {"strategy": "priority", "order": create},
                    "existing": {"strategy": "priority", "order": existing},
                }
            )
        )

    policy()
    return backend, policy


def call(method, *args, **kwargs):
    value = method(*args, **kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


@pytest.mark.parametrize("name", ["SyncDriveInstance", "DriveInstance", "SyncVolumeInstance", "VolumeInstance"])
def test_native_results_keep_imported_resource_instance_checks(name):
    from blaxel.core.client.models import Drive, DriveSpec, Metadata, Volume, VolumeSpec

    cls = getattr(blaxel, name)
    model = (
        Drive(metadata=Metadata(name="data"), spec=DriveSpec())
        if cls._kind == "drive"
        else Volume(metadata=Metadata(name="data"), spec=VolumeSpec())
    )
    native = cls._native(model)
    assert isinstance(native, cls)
    assert not isinstance(object(), cls)

    class Specialized(cls):
        pass

    assert not isinstance(native, Specialized)
    assert isinstance(Specialized(model), Specialized)


@pytest.mark.parametrize("name", ["SyncDriveInstance", "DriveInstance", "SyncVolumeInstance", "VolumeInstance"])
def test_one_import_routes_resource_lifecycle_and_native_models(setup, name):
    backend, _ = setup
    cls = getattr(blaxel, name)
    if cls._kind == "volume":
        backend.create_filesystem(name="shared")
        item = call(cls.get, "shared")
    else:
        item = call(cls.create, {"name": "shared"})
    assert isinstance(item, cls._native)
    assert item.name == "shared"
    assert item.region == "region-one"
    assert item.status is UNSET
    assert item.metadata.display_name is UNSET
    assert item.spec.to_dict() == {"region": "region-one"}
    again = call(cls.get, "shared")
    assert again.name == "shared"
    result = call(again.delete)
    assert result.metadata.name == "shared"
    assert backend.files == {}
    assert backend.deleted == ["dir-0"]


@pytest.mark.parametrize("name", ["SyncDriveInstance", "DriveInstance", "SyncVolumeInstance", "VolumeInstance"])
def test_list_is_native_paginated_and_next_page_preserves_routing(setup, name):
    backend, _ = setup
    for value in ["first", "second", "third"]:
        backend.create_filesystem(name=value)
    cls = getattr(blaxel, name)
    page = call(cls.list, limit=1)
    assert page.has_more
    assert [item.name for item in page.data] == ["first"]
    page = call(page.next_page)
    assert [item.name for item in page.data] == ["second"]
    page = call(page.next_page)
    assert [item.name for item in page.data] == ["third"]
    assert not page.has_more


@pytest.mark.parametrize("name", ["SyncDriveInstance", "DriveInstance", "SyncVolumeInstance", "VolumeInstance"])
def test_conditional_delete_refusal_preserves_resource(setup, name):
    backend, _ = setup
    cls = getattr(blaxel, name)
    if cls._kind == "volume":
        backend.create_filesystem(name="shared")
        item = call(cls.get, "shared")
    else:
        item = call(cls.create, {"name": "shared"})
    backend.refuse_delete = True
    expected = resources.DriveAPIError if cls._kind == "drive" else resources.VolumeAPIError
    with pytest.raises(expected) as error:
        call(cls.delete, "shared")
    assert error.value.status_code == 409
    assert len(backend.files) == 1
    assert backend.deleted == [item._owner[1]]


@pytest.mark.parametrize("name", ["SyncDriveInstance", "DriveInstance", "SyncVolumeInstance", "VolumeInstance"])
def test_native_only_route_preserves_arguments_return_and_loop(setup, monkeypatch, name):
    backend, policy = setup
    policy(("blaxel",), ("blaxel",))
    cls = getattr(blaxel, name)
    expected = object()
    arguments = []
    value = {"name": "native", "permissions": [{"custom": True}], "region": "vendor-region"}

    def native(config):
        arguments.append(config)
        return expected

    async def async_native(config):
        assert asyncio.get_running_loop()
        return native(config)

    monkeypatch.setattr(cls._native, "create", async_native if cls._asynchronous else native)
    assert call(cls.create, value) is expected
    assert arguments == [value]
    assert backend.calls == []


@pytest.mark.parametrize(
    "value",
    [
        {"name": "x", "permissions": []},
        {"name": "x", "size": 1},
        {"name": "x", "labels": {}},
        {"name": "x", "template": "custom"},
    ],
)
def test_unsupported_fields_fail_before_mutation(setup, value):
    backend, _ = setup
    with pytest.raises(resources.UnsupportedCompatibilityOption):
        resources.SyncDriveInstance.create(value)
    assert backend.calls == []


def test_instance_delete_keeps_captured_owner_after_router_changes(setup, monkeypatch):
    backend, policy = setup
    item = resources.SyncDriveInstance.create({"name": "shared"})
    policy(("blaxel",), ("blaxel",))
    monkeypatch.setattr(resources.NativeSyncDrive, "delete", lambda _: pytest.fail("borrowed native resource deleted"))
    item.delete()
    assert backend.files == {}


def test_static_delete_refuses_cross_provider_name_ambiguity(setup, monkeypatch):
    backend, policy = setup
    resources.SyncDriveInstance.create({"name": "shared"})
    policy(existing=("arker", "blaxel"))
    monkeypatch.setattr(resources.NativeSyncDrive, "get", lambda _: SimpleNamespace(name="shared"))
    with pytest.raises(Exception, match=r"ambiguous|multiple"):
        resources.SyncDriveInstance.delete("shared")
    assert backend.deleted == []


def test_arker_update_does_not_fall_through_to_native(setup, monkeypatch):
    backend, _ = setup
    item = resources.SyncDriveInstance.create({"name": "shared"})
    monkeypatch.setattr(resources.NativeSyncDrive, "update", lambda *a: pytest.fail("native mutation"))
    with pytest.raises(resources.UnsupportedCompatibilityOption):
        item.update({"labels": {"new": "value"}})
    assert len(backend.files) == 1


@pytest.mark.parametrize("name", ["SyncDriveInstance", "DriveInstance", "SyncVolumeInstance", "VolumeInstance"])
def test_native_keyword_names_and_static_delete(setup, name):
    backend, _ = setup
    cls = getattr(blaxel, name)
    if cls._kind == "volume":
        backend.create_filesystem(name="shared")
    else:
        call(cls.create, {"name": "shared"})
    key = cls._kind + "_name"
    item = call(cls.get, **{key: "shared"})
    assert item.name == "shared"
    assert inspect.iscoroutinefunction(cls.delete) == cls._asynchronous
    call(cls.delete, **{key: "shared"})
    assert backend.files == {}


@pytest.mark.parametrize("name", ["SyncDriveInstance", "DriveInstance", "SyncVolumeInstance", "VolumeInstance"])
def test_mixed_listing_keeps_provider_order_and_cursor_scope(setup, monkeypatch, name):
    from blaxel.core.client.models import PaginationMeta
    from blaxel.core.client.pagination import AsyncPaginatedList, PaginatedList

    backend, policy = setup
    cls = getattr(blaxel, name)
    backend.create_filesystem(name="arker-one")
    policy(existing=("arker", "blaxel"))
    expected = SimpleNamespace(name="native-one")
    calls = []

    def native(limit=50, cursor=None):
        calls.append((limit, cursor))
        page_type = AsyncPaginatedList if cls._asynchronous else PaginatedList
        return page_type([expected], meta=PaginationMeta(has_more=False))

    async def native_async(limit=50, cursor=None):
        return native(limit, cursor)

    monkeypatch.setattr(cls._native, "list", native_async if cls._asynchronous else native)
    page = call(cls.list, limit=1)
    assert [item.name for item in page] == ["arker-one"]
    token = page.next_cursor
    assert calls == []
    following = call(page.next_page)
    assert following.data == [expected]
    assert calls == [(1, None)]
    assert not following.has_more
    policy(existing=("blaxel", "arker"))
    with pytest.raises(ValueError, match="Cursor"):
        call(cls.list, cursor=token)


def test_empty_provider_does_not_hide_following_resources(setup, monkeypatch):
    from blaxel.core.client.pagination import PaginatedList

    _, policy = setup
    policy(existing=("arker", "blaxel"))
    expected = object()
    monkeypatch.setattr(resources.NativeSyncDrive, "list", lambda **_: PaginatedList([expected]))
    assert resources.SyncDriveInstance.list().data == [expected]


def test_create_unknown_outcome_never_falls_back_or_replays(setup, monkeypatch):
    backend, policy = setup
    policy(create=("arker", "blaxel"))
    creates = []

    def failed(*, name):
        creates.append(name)
        raise ArkerError("unavailable", "outcome unknown", 0)

    monkeypatch.setattr(backend, "create_filesystem", failed)
    monkeypatch.setattr(resources.NativeSyncDrive, "create", lambda _: pytest.fail("unsafe fallback"))
    with pytest.raises(resources.DriveAPIError) as error:
        resources.SyncDriveInstance.create({"name": "shared"})
    assert error.value.status_code == 0
    assert creates == ["shared"]


def test_one_import_does_not_bypass_selected_arker_route(setup, monkeypatch):
    backend, _ = setup
    monkeypatch.setattr(resources.NativeSyncDrive, "create", lambda _: pytest.fail("native bypass"))
    from arker.blaxel import SyncDriveInstance

    result = SyncDriveInstance.create({"name": "shared"})
    assert result.name == "shared"
    assert len(backend.files) == 1


@pytest.mark.parametrize("cls", [resources.SyncVolumeInstance, resources.VolumeInstance])
def test_volume_default_capacity_rejected_before_mutation(setup, cls):
    backend, _ = setup
    with pytest.raises(resources.UnsupportedCompatibilityOption, match="capacity"):
        call(cls.create, {"name": "shared"})
    assert backend.calls == []


@pytest.mark.parametrize("cls", [resources.DriveInstance, resources.VolumeInstance])
def test_cancelled_lookup_cannot_start_delete(setup, monkeypatch, cls):
    import threading

    backend, _ = setup
    backend.create_filesystem(name="shared")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    get = backend.get_filesystem
    original_call = resources._Service._call

    def blocked(identifier):
        entered.set()
        assert release.wait(5)
        return get(identifier)

    def tracked(self, function, *args, **kwargs):
        try:
            return original_call(self, function, *args, **kwargs)
        finally:
            if function is blocked:
                finished.set()

    monkeypatch.setattr(backend, "get_filesystem", blocked)
    monkeypatch.setattr(resources._Service, "_call", tracked)

    async def exercise():
        task = asyncio.create_task(cls.delete("shared"))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 5)

    asyncio.run(exercise())
    assert backend.deleted == []
    assert len(backend.files) == 1


def test_cancelled_accepted_creation_preserves_resource_without_replay(setup, monkeypatch):
    import threading

    backend, _ = setup
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    create = backend.create_filesystem
    original_call = resources._Service._call

    def accepted(*, name):
        result = create(name=name)
        entered.set()
        assert release.wait(5)
        return result

    def tracked(self, function, *args, **kwargs):
        try:
            return original_call(self, function, *args, **kwargs)
        finally:
            if function is accepted:
                finished.set()

    monkeypatch.setattr(backend, "create_filesystem", accepted)
    monkeypatch.setattr(resources._Service, "_call", tracked)

    async def exercise():
        task = asyncio.create_task(resources.DriveInstance.create({"name": "shared"}))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 5)

    asyncio.run(exercise())
    assert len(backend.files) == 1
    assert backend.deleted == []
    assert backend.calls == [("create", "shared")]


@pytest.mark.parametrize("cls", [resources.SyncDriveInstance, resources.DriveInstance])
def test_default_drive_name_matches_native_generation(setup, monkeypatch, cls):
    import importlib
    from uuid import UUID

    native_module = importlib.import_module("blaxel.core.drive.drive")
    monkeypatch.setattr(native_module.uuid, "uuid4", lambda: UUID(int=0))
    monkeypatch.setattr(native_module, "create_drive_sync", lambda **kwargs: kwargs["body"])

    async def create_drive(**kwargs):
        return kwargs["body"]

    monkeypatch.setattr(native_module, "create_drive", create_drive)
    expected = call(cls._native.create, {})
    actual = call(cls.create, {})
    assert actual.name == expected.name == "drive-00000000"


@pytest.mark.parametrize("cls", [resources.SyncDriveInstance, resources.DriveInstance])
@pytest.mark.parametrize("name", ["Upper", "space name", "a" * 50])
def test_drive_names_respect_native_metadata_contract_before_mutation(setup, cls, name):
    backend, _ = setup
    with pytest.raises(TypeError, match="name"):
        call(cls.create, {"name": name})
    assert backend.calls == []


@pytest.mark.parametrize(
    "cls",
    [resources.SyncDriveInstance, resources.DriveInstance, resources.SyncVolumeInstance, resources.VolumeInstance],
)
def test_mixed_native_page_iteration_is_lazy(setup, monkeypatch, cls):
    from blaxel.core.client.models import PaginationMeta
    from blaxel.core.client.pagination import AsyncPaginatedList, PaginatedList

    backend, policy = setup
    policy(existing=("blaxel", "arker"))
    first, second = object(), object()
    requests = []
    page_type = AsyncPaginatedList if cls._asynchronous else PaginatedList

    def native_page(limit=50, cursor=None):
        requests.append(cursor)
        return page_type(
            [first if cursor is None else second],
            meta=PaginationMeta(has_more=cursor is None, next_cursor="next" if cursor is None else ""),
            fetch_next=lambda _: pytest.fail("list(page) must not fetch another native page"),
        )

    async def async_page(**kwargs):
        return native_page(**kwargs)

    monkeypatch.setattr(cls._native, "list", async_page if cls._asynchronous else native_page)
    page = call(cls.list, limit=1)
    assert list(page) == [first]
    assert requests == [None]
    assert backend.calls == []
    next_page = call(page.next_page)
    assert list(next_page) == [second]
    assert requests == [None, "next"]
    assert backend.calls == []
