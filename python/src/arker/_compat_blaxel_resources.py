"""Route writable storage resources without changing native global clients."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import uuid
from functools import wraps

from blaxel.core import (
    DriveAPIError,
    VolumeAPIError,
)
from blaxel.core import (
    DriveInstance as NativeDrive,
)
from blaxel.core import (
    SyncDriveInstance as NativeSyncDrive,
)
from blaxel.core import (
    SyncVolumeInstance as NativeSyncVolume,
)
from blaxel.core import (
    VolumeInstance as NativeVolume,
)
from blaxel.core.client.models import Drive, DriveSpec, Metadata, PaginationMeta, Volume, VolumeSpec
from blaxel.core.client.pagination import AsyncPaginatedList, PaginatedList
from blaxel.core.client.types import UNSET

from ._compat import (
    UnsupportedCompatibilityOption,
    arker_client,
    pop_arker_config,
    routed_create,
    routed_get,
    router_config,
)
from ._compat_async import NativeBridge, RoutingCancelled, route
from .computer import ArkerError


def _name(config):
    if hasattr(config, "to_dict"):
        values = config.to_dict()
    elif isinstance(config, dict):
        values = dict(config)
    else:
        values = {key: value for key, value in vars(config).items() if value is not None}
    if "metadata" in values or "spec" in values:
        metadata, spec = values.get("metadata") or {}, values.get("spec") or {}
        if set(values) - {"metadata", "spec"} or set(metadata) - {"name"} or spec:
            raise UnsupportedCompatibilityOption("Arker storage supports only a resource name")
        values = metadata
    if set(values) - {"name"}:
        raise UnsupportedCompatibilityOption("Arker storage supports only a resource name")
    name = values.get("name") or "drive-" + uuid.uuid4().hex[:8]
    if not isinstance(name, str) or re.fullmatch(r"[a-z0-9-]{1,49}", name) is None:
        raise TypeError("Drive name must be 1-49 lowercase alphanumeric or hyphen characters")
    return name


def _missing(call):
    try:
        return call()
    except Exception as error:
        if getattr(error, "status_code", getattr(error, "status", None)) == 404:
            return None
        raise


class _Service:
    def __init__(self, cls, config, native, cancelled=None):
        self.cls, self.config, self.native = cls, config, native
        self.cancelled = cancelled
        self.error = DriveAPIError if cls._kind == "drive" else VolumeAPIError

    def _check(self):
        if self.cancelled is not None and self.cancelled.is_set():
            raise RoutingCancelled("Storage operation was cancelled")

    def _call(self, function, *args, **kwargs):
        self._check()
        try:
            result = function(*args, **kwargs)
            self._check()
            return result
        except ArkerError as error:
            raise self.error(error.message, status_code=error.status, code=error.code) from error

    def _wrap(self, file, client):
        model = Drive if self.cls._kind == "drive" else Volume
        spec = DriveSpec if self.cls._kind == "drive" else VolumeSpec
        value = model(metadata=Metadata(name=file.name), spec=spec(region=file.region or UNSET))
        result = self.cls(value)
        result._owner = (client, file.filesystem_id)
        result._config = self.config
        return result

    def create(self, config):
        def create_arker():
            if self.cls._kind == "volume":
                raise UnsupportedCompatibilityOption(
                    "Arker filesystems cannot enforce native volume capacity, including its default size"
                )
            name = _name(config)
            client = arker_client(self.config)
            return self._wrap(self._call(client.create_filesystem, name=name), client)

        return routed_create(
            "blaxel",
            self.config,
            {
                "arker": create_arker,
                "blaxel": lambda: self.native.create(config),
            },
            resource="blaxel-" + self.cls._kind,
            resource_id=lambda item: item.metadata.name,
        )[1]

    def _get_arker(self, name):
        client = arker_client(self.config)
        found, cursor, seen = None, None, set()
        while True:
            page = self._call(client.list_filesystems, name_prefix=name, cursor=cursor)
            for file in page.filesystems:
                if file.name == name:
                    if found and found.filesystem_id != file.filesystem_id:
                        raise ValueError("Storage name is ambiguous")
                    found = file
            cursor = page.next_cursor
            if not cursor:
                break
            if cursor in seen:
                raise RuntimeError("Filesystem cursor repeated")
            seen.add(cursor)
        if not found:
            return None
        current = self._call(client.get_filesystem, found.filesystem_id)
        if current.name != name:
            raise RuntimeError("Storage identity changed during lookup")
        return self._wrap(current, client)

    def _resolve(self, name, *, unique=False):
        result = routed_get(
            name,
            "blaxel",
            self.config,
            {
                "arker": lambda: _missing(lambda: self._get_arker(name)),
                "blaxel": lambda: _missing(lambda: self.native.get(name)),
            },
            resource="blaxel-" + self.cls._kind,
            require_unique=unique,
        )
        if result is None:
            raise self.error("Storage resource not found", status_code=404, code="NOT_FOUND")
        return result

    def get(self, name):
        return self._resolve(name)[1]

    def delete(self, name, owned=None):
        provider, item = ("arker", owned) if owned is not None else self._resolve(name, unique=True)
        if provider == "blaxel":
            return self.native.delete(name)
        client, filesystem_id = item._owner
        removed = self._call(client.delete_filesystem, filesystem_id, only_if_unmounted=True)
        if not removed.deleted:
            raise self.error("Filesystem deletion was not confirmed", status_code=409, code="CONFLICT")
        return getattr(item, self.cls._kind)

    def update(self, name, updates, owned=None):
        provider, _ = ("arker", owned) if owned is not None else self._resolve(name, unique=True)
        if provider == "arker":
            raise UnsupportedCompatibilityOption("Filesystem metadata and capacity updates are unsupported")
        return self.native.update(name, updates)

    def list(self, limit=50, cursor=None):
        order = tuple(router_config(self.config, "blaxel").existing.order)
        if not order or set(order) - {"arker", "blaxel"}:
            raise ValueError("Unsupported storage listing providers")
        if order == ("blaxel",):
            return self.native.list(limit=limit, cursor=cursor)
        index, backend_cursor = 0, cursor
        if len(order) > 1 and cursor:
            try:
                kind, previous, index, backend_cursor = json.loads(base64.urlsafe_b64decode(cursor))
                if (
                    kind != self.cls._kind
                    or tuple(previous) != order
                    or type(index) is not int
                    or not 0 <= index < len(order)
                ):
                    raise ValueError
                if backend_cursor is not None and not isinstance(backend_cursor, str):
                    raise ValueError
            except (ValueError, TypeError, UnicodeError) as error:
                raise ValueError("Cursor does not belong to this storage listing") from error
        seen = set()
        while True:
            if (index, backend_cursor) in seen:
                raise RuntimeError("Storage listing cursor repeated")
            seen.add((index, backend_cursor))
            if order[index] == "arker":
                client = arker_client(self.config)
                page = self._call(client.list_filesystems, limit=limit, cursor=backend_cursor)
                items = [self._wrap(file, client) for file in page.filesystems]
                next_cursor = page.next_cursor
            else:
                page = self.native.list(limit=limit, cursor=backend_cursor)
                items, next_cursor = list(page), page.next_cursor
            if not next_cursor:
                index += 1
            backend_cursor = next_cursor
            more = index < len(order)
            if items or not more:
                break
        if more:
            token = (
                backend_cursor
                if len(order) == 1
                else base64.urlsafe_b64encode(
                    json.dumps([self.cls._kind, order, index, backend_cursor]).encode()
                ).decode()
            )
        else:
            token = None
        page_type = AsyncPaginatedList if self.cls._asynchronous else PaginatedList
        return page_type(
            items,
            meta=PaginationMeta(has_more=more, next_cursor=token or ""),
            fetch_next=lambda token: self.cls.list(limit, token, arker=self.config),
        )


class _BoundOperation:
    def __init__(self, method):
        self.method = method

    def __get__(self, instance, owner):
        if instance is not None and not hasattr(instance, "_owner"):
            return getattr(super(_Resources, instance), self.method)
        native = getattr(owner._native, self.method)

        def invoke(*args, **kwargs):
            if instance is None:
                if args:
                    name, *args = args
                else:
                    name = kwargs.pop(owner._kind + "_name")
                return owner._dispatch(self.method, name, *args, **kwargs)
            return owner._dispatch(
                self.method, instance.metadata.name, *args, owned=instance, arker=instance._config, **kwargs
            )

        if owner._asynchronous:

            @wraps(native)
            async def asynchronous(*args, **kwargs):
                return await invoke(*args, **kwargs)

            return asynchronous
        return wraps(native)(invoke)


class _Resources:
    delete = _BoundOperation("delete")
    update = _BoundOperation("update")
    _asynchronous = False

    @classmethod
    def _dispatch(cls, operation, *args, **kwargs):
        config = pop_arker_config(kwargs)
        return getattr(_Service(cls, config, cls._native), operation)(*args, **kwargs)

    @classmethod
    def create(cls, config, **kwargs):
        return cls._dispatch("create", config, **kwargs)

    @classmethod
    def get(cls, name, **kwargs):
        return cls._dispatch("get", name, **kwargs)

    @classmethod
    def list(cls, limit=50, cursor=None, **kwargs):
        return cls._dispatch("list", limit, cursor, **kwargs)


class _AsyncResources(_Resources):
    _asynchronous = True

    @classmethod
    async def _dispatch(cls, operation, *args, **kwargs):
        config = pop_arker_config(kwargs)
        bridge = NativeBridge(cls._native, asyncio.get_running_loop())
        return await route(
            lambda: getattr(_Service(cls, config, bridge, bridge.cancelled), operation)(*args, **kwargs),
            bridges=(bridge,),
        )

    @classmethod
    async def create(cls, config, **kwargs):
        return await cls._dispatch("create", config, **kwargs)

    @classmethod
    async def get(cls, name, **kwargs):
        return await cls._dispatch("get", name, **kwargs)

    @classmethod
    async def list(cls, limit=50, cursor=None, **kwargs):
        return await cls._dispatch("list", limit, cursor, **kwargs)


class SyncDriveInstance(_Resources, NativeSyncDrive):
    _native, _kind = NativeSyncDrive, "drive"

    @classmethod
    def get(cls, drive_name: str, **kwargs):
        return cls._dispatch("get", drive_name, **kwargs)


class DriveInstance(_AsyncResources, NativeDrive):
    _native, _kind = NativeDrive, "drive"

    @classmethod
    async def get(cls, drive_name: str, **kwargs):
        return await cls._dispatch("get", drive_name, **kwargs)


class SyncVolumeInstance(_Resources, NativeSyncVolume):
    _native, _kind = NativeSyncVolume, "volume"

    @classmethod
    def get(cls, volume_name: str, **kwargs):
        return cls._dispatch("get", volume_name, **kwargs)


class VolumeInstance(_AsyncResources, NativeVolume):
    _native, _kind = NativeVolume, "volume"

    @classmethod
    async def get(cls, volume_name: str, **kwargs):
        return await cls._dispatch("get", volume_name, **kwargs)
