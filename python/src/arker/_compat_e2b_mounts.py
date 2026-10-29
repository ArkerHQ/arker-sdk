"""E2B volume resource routing and Arker filesystem mounts.

Mount strings are names, never filesystem IDs. Attach returns mount records, not
proof of guest readiness. Storage has the filesystem backend's coherence model,
not a promise of cross-VM POSIX locking or immediate cache invalidation.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any

from ._compat import (
    UnsupportedCompatibilityOption,
    arker_client,
    format_not_found,
    get_or_none,
    pop_arker_config,
    routed_create,
    routed_get,
    routed_list,
)
from .computer import Arker, ArkerError


def _field(value: Any, key: str) -> Any:
    return value[key] if isinstance(value, dict) else getattr(value, key)


def _same_client(left: Any, right: Any) -> bool:
    return left is right or (
        isinstance(left, Arker)
        and isinstance(right, Arker)
        and (left.base_url, left.control_base_url, left._api_key)
        == (right.base_url, right.control_base_url, right._api_key)
    )


def _name(name: str) -> None:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-](?:[A-Za-z0-9_.-]{0,126}[A-Za-z0-9_-])?", name):
        raise ValueError("Volume name must be 1-128 ASCII letters, digits, '_', '-' or interior '.'")


def _path(path: str) -> None:
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path == "/"
        or ".." in path
        or "\0" in path
        or len(path.encode()) > 256
        or any(part in ("", ".") for part in path[1:].split("/"))
    ):
        raise ValueError("Mount path must be canonical, absolute, non-root, at most 256 bytes, and contain no '..'")


@dataclass(frozen=True)
class ArkerVolume:
    """Explicit Arker identity. No native volume token or content API is emulated."""

    volume_id: str
    name: str
    _client: Any = field(default=None, repr=False, compare=False)

    @property
    def token(self) -> str:
        raise UnsupportedCompatibilityOption("Arker volumes do not issue E2B volume-content tokens")

    def get_info(self, path: str, **opts: Any) -> Any:
        raise UnsupportedCompatibilityOption("Volume.get_info(path) requires the E2B content API; use sandbox.files")

    def list(self, path: str, **opts: Any) -> Any:
        raise UnsupportedCompatibilityOption("Volume.list(path) requires the E2B content API; use sandbox.files")

    def __getattr__(self, name: str) -> Any:
        if name in {"read", "write", "remove", "rename", "make_dir", "exists", "download_url", "upload_url"}:
            raise UnsupportedCompatibilityOption(
                "Use sandbox.files on an attached Arker volume; the E2B content API is unavailable"
            )
        raise AttributeError(name)


class Volumes:
    """Bind to an already-routed Arker client. Native Volume objects stay native.

    create is strict creation, with server-enforced name uniqueness. by_name never
    creates on a miss: listings can lag or be truncated by the service.
    """

    def __init__(self, client: Any):
        self.client = client

    def _volume(self, info: Any) -> ArkerVolume:
        return ArkerVolume(_field(info, "filesystem_id"), _field(info, "name"), self.client)

    def create(self, name: str) -> ArkerVolume:
        _name(name)
        return self._volume(self.client.create_filesystem(name=name))

    def connect(self, volume_id: str) -> ArkerVolume:
        return self._volume(self.client.get_filesystem(volume_id))

    def get_info(self, volume_id: str) -> ArkerVolume:
        return self.connect(volume_id)

    def list(self) -> list[ArkerVolume]:
        result = []
        cursor = None
        seen = set()
        while True:
            page = self.client.list_filesystems(cursor=cursor)
            result.extend(self._volume(info) for info in _field(page, "filesystems"))
            cursor = _field(page, "next_cursor")
            if not cursor:
                return result
            if cursor in seen:
                raise RuntimeError("Filesystem listing repeated a cursor")
            seen.add(cursor)

    def by_name(self, name: str) -> ArkerVolume:
        _name(name)
        matches = [volume for volume in self.list() if volume.name == name]
        if not matches:
            raise LookupError("Volume name was not found in the filesystem listing; connect by Arker ID if known")
        if len(matches) != 1:
            raise ValueError("Volume name is ambiguous")
        return self.connect(matches[0].volume_id)

    def destroy(self, volume_id: str) -> bool:
        try:
            return _field(self.client.delete_filesystem(volume_id), "deleted")
        except ArkerError as error:
            if error.status == 404:
                return False
            raise

    def resolve(self, volume_mounts: Mapping[str, ArkerVolume | str]) -> dict[str, ArkerVolume]:
        """Preflight every entry before any mount mutation; never reinterpret native handles."""
        for path, volume in volume_mounts.items():
            _path(path)
            if isinstance(volume, str):
                _name(volume)
            if not isinstance(volume, (str, ArkerVolume)):
                raise TypeError("Native Volume handles must use the native backend")
            if (
                isinstance(volume, ArkerVolume)
                and volume._client is not None
                and not _same_client(volume._client, self.client)
            ):
                raise ValueError(
                    "Volume belongs to another Arker client; explicitly connect its ID on the selected client"
                )
            if isinstance(volume, str):
                _name(volume)
        resolved = {
            path: self.by_name(volume) if isinstance(volume, str) else self.connect(volume.volume_id)
            for path, volume in volume_mounts.items()
        }
        if len({volume.volume_id for volume in resolved.values()}) != len(resolved):
            raise ValueError("A filesystem can only be mounted once per VM")
        paths = list(resolved)
        if any(a != b and b.startswith(a + "/") for a in paths for b in paths):
            raise ValueError("Overlapping mount paths are unsupported")
        return resolved

    def attach(self, vm: Any, volume_mounts: Mapping[str, ArkerVolume | str]) -> list[Any]:
        """Register mounts. Partial failures propagate; the caller owns VM cleanup.

        Do not detach on failure: an attachment may have pre-existed this call.
        A successful response does not certify guest mount readiness.
        """
        resolved = self.resolve(volume_mounts)
        return [vm.create_mount(filesystem_id=volume.volume_id, path=path) for path, volume in resolved.items()]


class AsyncVolumes:
    """Async facade over the synchronous Arker client, without blocking the loop."""

    def __init__(self, client: Any):
        self._sync = Volumes(client)

    async def create(self, name: str) -> ArkerVolume:
        return await asyncio.to_thread(self._sync.create, name)

    async def connect(self, volume_id: str) -> ArkerVolume:
        return await asyncio.to_thread(self._sync.connect, volume_id)

    async def get_info(self, volume_id: str) -> ArkerVolume:
        return await asyncio.to_thread(self._sync.get_info, volume_id)

    async def list(self) -> list[ArkerVolume]:
        return await asyncio.to_thread(self._sync.list)

    async def by_name(self, name: str) -> ArkerVolume:
        return await asyncio.to_thread(self._sync.by_name, name)

    async def destroy(self, volume_id: str) -> bool:
        return await asyncio.to_thread(self._sync.destroy, volume_id)

    async def resolve(self, volume_mounts: Mapping[str, ArkerVolume | str]) -> dict[str, ArkerVolume]:
        return await asyncio.to_thread(self._sync.resolve, volume_mounts)

    async def attach(self, vm: Any, volume_mounts: Mapping[str, ArkerVolume | str]) -> list[Any]:
        # Cancellation cannot stop an in-flight HTTP call; wait for settlement so
        # the caller can safely delete its newly-created VM after cancellation.
        task = asyncio.create_task(asyncio.to_thread(self._sync.attach, vm, volume_mounts))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await task
            raise


def _volume_options(options: dict[str, Any]) -> None:
    if options:
        raise UnsupportedCompatibilityOption(
            "Configure Arker through the arker option; per-call E2B connection options are unsupported"
        )


def _native_volume(async_mode: bool = False) -> Any:
    from ._compat import import_optional

    module = import_optional("e2b")
    if module is None:
        raise ImportError("Install e2b to use the native volume backend")
    return module.AsyncVolume if async_mode else module.Volume


class Volume:
    """Volume lifecycle follows the shared router, using a separate resource cache."""

    _native_volume = staticmethod(_native_volume)

    @classmethod
    def _services(cls, opts: dict[str, Any]):
        options = dict(opts)
        config = pop_arker_config(options)

        def arker():
            _volume_options(options)
            return Volumes(arker_client(config))

        return config, options, arker

    @classmethod
    def create(cls, name: str, **opts: Any) -> Any:
        config, options, arker = cls._services(opts)
        _, volume = routed_create(
            "e2b",
            config,
            {"arker": lambda: arker().create(name), "e2b": lambda: cls._native_volume().create(name, **options)},
            resource="e2b-volume",
            resource_id=lambda result: result.volume_id,
        )
        return volume

    @classmethod
    def _lookup(cls, volume_id: str, opts: dict[str, Any], method: str = "connect"):
        config, options, arker = cls._services(opts)
        found = routed_get(
            volume_id,
            "e2b",
            config,
            {
                "arker": lambda: get_or_none(lambda: getattr(arker(), method)(volume_id)),
                "e2b": lambda: get_or_none(lambda: getattr(cls._native_volume(), method)(volume_id, **options)),
            },
            resource="e2b-volume",
        )
        return found, options, arker

    @classmethod
    def connect(cls, volume_id: str, **opts: Any) -> Any:
        found, _, _ = cls._lookup(volume_id, opts)
        if found is None:
            raise format_not_found("Volume", volume_id)
        return found[1]

    @classmethod
    def get_info(cls, volume_id: str, **opts: Any) -> Any:
        found, _, _ = cls._lookup(volume_id, opts, "get_info")
        if found is None:
            raise format_not_found("Volume", volume_id)
        return found[1]

    @classmethod
    def list(cls, **opts: Any) -> Any:
        config, options, arker = cls._services(opts)
        return routed_list(
            "e2b",
            config,
            {
                "arker": lambda: arker().list(),
                "e2b": lambda: cls._native_volume().list(**options),
            },
        )

    @classmethod
    def destroy(cls, volume_id: str, **opts: Any) -> bool:
        found, options, arker = cls._lookup(volume_id, opts)
        if found is None:
            return False
        return arker().destroy(volume_id) if found[0] == "arker" else cls._native_volume().destroy(volume_id, **options)


class AsyncVolume:
    """Async volume routing with native coroutines kept on their original loop."""

    @classmethod
    async def _route(cls, method: str, *args: Any, **opts: Any) -> Any:
        from ._compat import import_optional
        from ._compat_async import NativeBridge, route

        module = import_optional("e2b")
        native = getattr(module, "AsyncVolume", None)
        native_options = dict(opts)
        pop_arker_config(native_options)
        bridge = (
            NativeBridge(
                native,
                asyncio.get_running_loop(),
                creation_cleanup=lambda volume: native.destroy(volume.volume_id, **native_options),
            )
            if native is not None
            else None
        )

        class Routed(Volume):
            @staticmethod
            def _native_volume():
                if bridge is None:
                    raise ImportError("Install e2b to use native volumes")
                return bridge

        return await route(
            lambda: getattr(Routed, method)(*args, **opts),
            bridges=[bridge] if bridge else [],
            cleanup=(
                lambda volume: (
                    volume._client.delete_filesystem(volume.volume_id)
                    if isinstance(volume, ArkerVolume)
                    else native.destroy(volume.volume_id, **native_options)
                )
            )
            if method == "create"
            else None,
        )

    @classmethod
    async def create(cls, name: str, **opts: Any) -> Any:
        return await cls._route("create", name, **opts)

    @classmethod
    async def connect(cls, volume_id: str, **opts: Any) -> Any:
        return await cls._route("connect", volume_id, **opts)

    @classmethod
    async def get_info(cls, volume_id: str, **opts: Any) -> Any:
        return await cls._route("get_info", volume_id, **opts)

    @classmethod
    async def list(cls, **opts: Any) -> Any:
        return await cls._route("list", **opts)

    @classmethod
    async def destroy(cls, volume_id: str, **opts: Any) -> bool:
        return await cls._route("destroy", volume_id, **opts)


def check_mount_backend(mounts: Any, provider: str) -> None:
    from ._compat import import_optional

    if mounts is None:
        return
    if not isinstance(mounts, Mapping):
        raise TypeError("volume_mounts must be a mapping of paths to volumes or names")
    module = import_optional("e2b")
    native_types = tuple(
        kind
        for kind in (getattr(module, "Volume", None), getattr(module, "AsyncVolume", None))
        if isinstance(kind, type)
    )
    for path, volume in mounts.items():
        if not isinstance(path, str):
            raise TypeError("Volume mount paths must be strings")
        if provider == "arker":
            _path(path)
            if isinstance(volume, str):
                _name(volume)
            if not isinstance(volume, (str, ArkerVolume)):
                raise UnsupportedCompatibilityOption("Native Volume handles require the native E2B sandbox backend")
        elif isinstance(volume, ArkerVolume):
            raise UnsupportedCompatibilityOption("Arker Volume handles require the Arker sandbox backend")
        elif not isinstance(volume, (str, *native_types)):
            raise TypeError("Expected a volume name or native Volume handle")


def mount_store(client: Any, mounts: Mapping[str, ArkerVolume | str]) -> Volumes:
    """Use the handle's storage scope; mount creation still enforces VM ownership."""
    bound = [value._client for value in mounts.values() if isinstance(value, ArkerVolume) and value._client is not None]
    if bound and any(not _same_client(value, bound[0]) for value in bound):
        raise ValueError("Volume handles belong to different Arker clients")
    return Volumes(bound[0] if bound else client)


# Check the kernel mount table, not directory existence: a failed attachment can
# leave an ordinary directory behind. The creation timeout bounds convergence.
_MOUNT_READY = files("arker").joinpath("_guest_e2b_mount_ready.py").read_text()


def mount_ready_command(paths: Any, timeout: float) -> str:
    import shlex

    return (
        "python3 -I -c "
        + shlex.quote(_MOUNT_READY)
        + " "
        + " ".join(shlex.quote(str(value)) for value in (timeout, *paths))
    )
