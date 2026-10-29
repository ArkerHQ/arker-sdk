"""Route Daytona volume namespaces without translating provider identities."""

from __future__ import annotations

from functools import lru_cache

from ._compat import (
    UnsupportedCompatibilityOption,
    arker_client,
    get_or_none,
    routed_create,
    routed_get,
    routed_list,
    router_config,
)
from ._compat_e2b_mounts import _path
from .computer import ArkerError


@lru_cache(maxsize=1)
def _arker_volume_type():
    from daytona.common.volume import Volume
    from pydantic import PrivateAttr

    class ArkerVolume(Volume):
        _client = PrivateAttr()

    return ArkerVolume


def _error(call, *args, **kwargs):
    from daytona.common.errors import create_daytona_error

    try:
        return call(*args, **kwargs)
    except ArkerError as error:
        raise create_daytona_error(str(error), status_code=error.status) from error


def _volume(info, client):
    # A successful filesystem create commits the marker and immutable catalog
    # metadata synchronously. Guest mount readiness is a separate operation.
    result = _arker_volume_type()(
        id=info.filesystem_id,
        name=info.name,
        organization_id=info.owner_org_id,
        state="ready",
        created_at=info.created_at,
        updated_at=info.created_at,
        last_used_at=None,
        error_reason=None,
    )
    result._client = client
    return result


class VolumeService:
    def __init__(self, config, native):
        self.config, self.native = config, native

    def _list(self):
        client = arker_client(self.config)
        result, seen, cursor = [], set(), None
        while True:
            page = _error(client.list_filesystems, cursor=cursor)
            result.extend(_volume(info, client) for info in page.filesystems)
            cursor = page.next_cursor
            if not cursor:
                if len(page.filesystems) >= 500:
                    raise UnsupportedCompatibilityOption("Filesystem listing may be truncated at the server limit")
                return result
            if cursor in seen:
                raise RuntimeError("Filesystem listing repeated a cursor")
            seen.add(cursor)

    def _get(self, name):
        from daytona.common.errors import DaytonaNotFoundError

        matches = [volume for volume in self._list() if volume.name == name]
        if not matches:
            raise DaytonaNotFoundError("Volume not found", status_code=404)
        if len(matches) != 1:
            raise ValueError("Volume name is ambiguous")
        found = matches[0]
        return _volume(_error(found._client.get_filesystem, found.id), found._client)

    def create(self, name):
        def create_arker():
            client = arker_client(self.config)
            try:
                return _volume(client.create_filesystem(name=name), client)
            except ArkerError as error:
                from daytona.common.errors import create_daytona_error

                status = 400 if error.status == 409 and error.code == "already_exists" else error.status
                raise create_daytona_error(str(error), status_code=status) from error

        return routed_create(
            "daytona",
            self.config,
            {"arker": create_arker, "daytona": lambda: self.native().create(name)},
            resource="daytona-volume",
            resource_id=lambda volume: volume.id,
        )[1]

    def get(self, name, create=False):
        from daytona.common.errors import DaytonaNotFoundError

        found = routed_get(
            name,
            "daytona",
            self.config,
            {
                "arker": lambda: get_or_none(lambda: self._get(name)),
                "daytona": lambda: get_or_none(lambda: self.native().get(name)),
            },
            resource="daytona-volume-name",
            require_unique=True,
        )
        if found is not None:
            return found[1]
        if create:
            return self.create(name)
        raise DaytonaNotFoundError("Volume not found", status_code=404)

    def _mount_owner(self, identifier):
        from daytona.common.errors import DaytonaNotFoundError

        def native():
            matches = [v for v in self.native().list() if identifier in (v.id, v.name)]
            if len(matches) > 1:
                raise ValueError("Volume mount identifier matches multiple native volumes")
            return matches[0] if matches else None

        found = routed_get(
            identifier,
            "daytona",
            self.config,
            {"arker": lambda: get_or_none(lambda: self._get(identifier)), "daytona": native},
            resource="daytona-volume-mount",
            require_unique=True,
        )
        if found is None:
            raise DaytonaNotFoundError("Volume not found", status_code=404)
        return found[1]

    def list(self):
        return routed_list("daytona", self.config, {"arker": self._list, "daytona": lambda: self.native().list()})

    def delete(self, volume):
        from daytona.common.volume import Volume

        if isinstance(volume, _arker_volume_type()):
            raise UnsupportedCompatibilityOption(
                "Arker filesystem deletion cannot atomically refuse mounted volumes; use the explicit filesystem API"
            )
        if isinstance(volume, Volume):
            if volume.id.startswith("dir_"):
                raise UnsupportedCompatibilityOption("An Arker volume requires its original provider-bound handle")
            self.native().delete(volume)
        else:
            raise TypeError("Expected a Daytona Volume")


def resolve_mounts(volumes, service):
    if volumes is None:
        return []
    if not isinstance(volumes, list):
        raise TypeError("volumes must be a list")
    client = arker_client(service.config) if volumes else None
    resolved, paths = [], set()
    for mount in volumes:
        value = mount.model_dump(exclude_none=True, exclude_unset=True) if hasattr(mount, "model_dump") else mount
        if isinstance(value, dict) and value.get("additional_properties") == {}:
            value = {k: v for k, v in value.items() if k != "additional_properties"}
        if not isinstance(value, dict) or set(value) - {"volume_id", "mount_path", "subpath"}:
            raise ValueError("Invalid Daytona volume mount")
        if value.get("subpath") not in (None, ""):
            raise UnsupportedCompatibilityOption("Arker volume mounts do not support subpath")
        path, identifier = value.get("mount_path"), value.get("volume_id")
        _path(path)
        if path in paths:
            raise ValueError("Duplicate volume mount path")
        paths.add(path)
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("volume_id must be a nonempty string")
        if identifier.startswith("dir_"):
            info = _error(client.get_filesystem, identifier)
        else:
            # Names resolve through the shared router; a native match cannot be
            # attached to an Arker VM even when another backend has a same-name row.
            volume = service._mount_owner(identifier)
            if not isinstance(volume, _arker_volume_type()):
                raise UnsupportedCompatibilityOption("Native Daytona volumes require a native sandbox")
            info = _error(client.get_filesystem, volume.id)
        resolved.append((info.filesystem_id, path))
    return resolved


def check_native_mounts(args, kwargs, service):
    params = args[0] if args else kwargs.get("params", kwargs)
    volumes = params.get("volumes") if isinstance(params, dict) else getattr(params, "volumes", None)
    for mount in volumes or []:
        identifier = mount.get("volume_id") if isinstance(mount, dict) else getattr(mount, "volume_id", None)
        if isinstance(identifier, str) and identifier.startswith("dir_"):
            raise UnsupportedCompatibilityOption("Arker filesystem IDs require an Arker sandbox")
        if "arker" in router_config(service.config, "daytona").existing.order and isinstance(
            service._mount_owner(identifier), _arker_volume_type()
        ):
            raise UnsupportedCompatibilityOption("Arker volumes require an Arker sandbox")


class NativeVolumeOperations:
    def __init__(self, owner):
        self.owner = owner

    async def create(self, name):
        return await self.owner._native_client().volume.create(name)

    async def get(self, name):
        return await self.owner._native_client().volume.get(name)

    async def list(self):
        return await self.owner._native_client().volume.list()

    async def delete(self, volume):
        return await self.owner._native_client().volume.delete(volume)


class AsyncVolumeService:
    def __init__(self, owner):
        self.owner = owner

    async def _route(self, method, *args):
        from ._compat_async import NativeBridge, RoutingCancelled, route

        class CreationBridge(NativeBridge):
            def cancel(self):
                # A cancelled response does not prove the server did not create a volume.
                self.cancelled.set()

            def __getattr__(self, name):
                invoke = super().__getattr__(name)

                def guarded(*args, **kwargs):
                    try:
                        return invoke(*args, **kwargs)
                    except Exception as error:
                        if self.cancelled.is_set():
                            raise RoutingCancelled("Volume creation was cancelled") from error
                        raise

                return guarded

        native = NativeVolumeOperations(self.owner)
        bridge_type = CreationBridge if method == "create" else NativeBridge
        bridge = bridge_type(native, self.owner._bind_loop())
        service = VolumeService(self.owner._configuration._arker_config, lambda: bridge)

        async def cleanup(volume):
            try:
                if isinstance(volume, _arker_volume_type()):
                    from ._compat_async import call

                    await call(_error, volume._client.delete_filesystem, volume.id)
                else:
                    await native.delete(volume)
            except Exception as error:
                raise RuntimeError(f"Volume creation cleanup failed for {volume.id}") from error

        return await route(
            lambda: getattr(service, method)(*args),
            bridges=[bridge],
            cleanup=cleanup if method == "create" else None,
            drain_on_cancel=method == "create",
        )

    async def create(self, name):
        return await self._route("create", name)

    async def get(self, name, create=False):
        from daytona.common.errors import DaytonaNotFoundError

        try:
            return await self._route("get", name)
        except DaytonaNotFoundError:
            if not create:
                raise
            return await self.create(name)

    async def list(self):
        return await self._route("list")

    async def delete(self, volume):
        return await self._route("delete", volume)
