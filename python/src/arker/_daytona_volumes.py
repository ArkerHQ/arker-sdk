"""Route Daytona volume namespaces without translating provider identities."""

from __future__ import annotations

import os
import unicodedata
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
    def __init__(self, config, native, *, native_available=True):
        self.config, self.native = config, native
        self.native_available = native_available

    def _providers(self, **handlers):
        if not self.native_available:
            handlers.pop("daytona", None)
        if not ((self.config or {}).get("api_key") or os.environ.get("ARKER_API_KEY")):
            handlers.pop("arker", None)
        return handlers

    def _list(self):
        client = arker_client(self.config)
        result, seen, cursor = [], set(), "0"
        while True:
            page = _error(client.list_filesystems, cursor=cursor, limit=100)
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
            self._providers(arker=create_arker, daytona=lambda: self.native().create(name)),
            resource="daytona-volume",
            resource_id=lambda volume: volume.id,
        )[1]

    def get(self, name, create=False):
        from daytona.common.errors import DaytonaNotFoundError

        found = routed_get(
            name,
            "daytona",
            self.config,
            self._providers(
                arker=lambda: get_or_none(lambda: self._get(name)),
                daytona=lambda: get_or_none(lambda: self.native().get(name)),
            ),
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
            self._providers(arker=lambda: get_or_none(lambda: self._get(identifier)), daytona=native),
            resource="daytona-volume-mount",
            require_unique=True,
        )
        if found is None:
            raise DaytonaNotFoundError("Volume not found", status_code=404)
        return found[1]

    def list(self):
        return routed_list(
            "daytona", self.config, self._providers(arker=self._list, daytona=lambda: self.native().list())
        )

    def delete(self, volume):
        from daytona.common.volume import Volume

        if isinstance(volume, _arker_volume_type()):
            _error(volume._client.delete_filesystem, volume.id, only_if_unmounted=True)
            return
        if isinstance(volume, Volume):
            if volume.id.startswith("dir_"):
                raise UnsupportedCompatibilityOption("An Arker volume requires its original provider-bound handle")
            self.native().delete(volume)
        else:
            raise TypeError("Expected a Daytona Volume")


def _mount_path(path, *, relative):
    label = "subpath" if relative else "Mount path"
    if not isinstance(path, str) or not path or len(path.encode()) > (512 if relative else 256):
        raise ValueError(f"{label} must be a bounded nonempty string")
    components = path.split("/") if relative else path[1:].split("/")
    if path.startswith("/") == relative or any(part in ("", ".", "..") for part in components):
        raise ValueError(f"{label} must be canonical and {'relative' if relative else 'absolute and non-root'}")
    if "\0" in path or (relative and ("\\" in path or any(unicodedata.category(char) == "Cc" for char in path))):
        raise ValueError(f"{label} contains unsupported characters")
    if relative and ("*" in path or "?" in path or "${" in path):
        raise ValueError("subpath contains IAM policy syntax")


def _normalize_arker_mounts(volumes):
    if volumes is None:
        return []
    if not isinstance(volumes, list):
        raise TypeError("volumes must be a list")
    normalized, paths = [], set()
    for mount in volumes:
        value = mount.model_dump(exclude_none=True, exclude_unset=True) if hasattr(mount, "model_dump") else mount
        if isinstance(value, dict) and value.get("additional_properties") == {}:
            value = {k: v for k, v in value.items() if k != "additional_properties"}
        if not isinstance(value, dict) or set(value) - {"volume_id", "mount_path", "subpath"}:
            raise ValueError("Invalid Daytona volume mount")
        subpath = value.get("subpath")
        if subpath not in (None, ""):
            _mount_path(subpath, relative=True)
        path, identifier = value.get("mount_path"), value.get("volume_id")
        _mount_path(path, relative=False)
        if path in paths:
            raise ValueError("Duplicate volume mount path")
        paths.add(path)
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("volume_id must be a nonempty string")
        normalized.append(value)
    return normalized


def resolve_mounts(volumes, service):
    mounts = _normalize_arker_mounts(volumes)
    client = arker_client(service.config) if mounts else None
    resolved = []
    for value in mounts:
        identifier, path, subpath = value["volume_id"], value["mount_path"], value.get("subpath")
        if identifier.startswith("dir_"):
            info = _error(client.get_filesystem, identifier)
        else:
            # Names resolve through the shared router; a native match cannot be
            # attached to an Arker VM even when another backend has a same-name row.
            volume = service._mount_owner(identifier)
            if not isinstance(volume, _arker_volume_type()):
                raise UnsupportedCompatibilityOption("Native Daytona volumes require a native sandbox")
            info = _error(client.get_filesystem, volume.id)
        resolved.append(
            {"filesystem_id": info.filesystem_id, "path": path, **({"subpath": subpath} if subpath else {})}
        )
    return resolved


def check_native_mounts(args, kwargs, service):
    params = args[0] if args else kwargs.get("params", kwargs)
    volumes = params.get("volumes") if isinstance(params, dict) else getattr(params, "volumes", None)
    from daytona.common.volume import VolumeMount
    from pydantic import TypeAdapter

    # Preserve the vendor model's coercion and accepted path semantics.
    mounts = TypeAdapter(list[VolumeMount]).validate_python(volumes) if volumes is not None else []
    for mount in mounts:
        identifier = mount.volume_id
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
        service = VolumeService(
            self.owner._configuration._arker_config,
            lambda: bridge,
            native_available=self.owner._native is not None or self.owner._configuration._native_volume_available(),
        )

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
