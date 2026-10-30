"""Writable native Blaxel drive bindings backed by shared filesystems."""

from __future__ import annotations

import asyncio
import threading
import time
from urllib.parse import quote, urlencode

from blaxel.core.sandbox import SandboxDrive, SyncSandboxDrive
from blaxel.core.sandbox.client.models import DriveMountInfo, DriveMountResponse, DriveUnmountResponse

from .computer import VM, ArkerError, _compute_base_url


def _path(value: str, *, relative=False) -> str:
    if not isinstance(value, str) or "\0" in value or "\\" in value:
        raise ValueError("Drive paths must be filesystem paths")
    if not value.startswith("/") and not relative:
        raise ValueError("Drive path must start with /")
    parts = [part for part in value.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("Drive paths cannot contain traversal")
    return "/" + "/".join(parts)


class _Bindings:
    def __init__(self, vm: VM, cancelled: threading.Event | None = None):
        self.vm = vm
        self.cancelled = cancelled or threading.Event()

    def _check(self):
        if self.cancelled.is_set():
            raise asyncio.CancelledError

    def _request(self, method, path, body=None, *, filesystem=False):
        self._check()
        vm, client = self.vm, self.vm._client
        base = vm.base_url
        if filesystem:
            if not vm.provider or not vm.region:
                raise RuntimeError("Drive operations require known sandbox placement")
            base = client.base_url if client._explicit_base_url else _compute_base_url(vm.provider, vm.region)
        result = client._request(method, path, body, base_url=base, retry_network_failures=method == "GET")
        self._check()
        return result

    def _pages(self, path, key, *, filesystem=False, **query):
        cursor, seen = None, set()
        while True:
            params = {**query, **({"cursor": cursor} if cursor else {})}
            page = self._request("GET", path + ("?" + urlencode(params) if params else ""), filesystem=filesystem)
            yield from page[key]
            cursor = page.get("next_cursor")
            if not cursor:
                return
            if cursor in seen:
                raise RuntimeError("Drive pagination cursor repeated")
            seen.add(cursor)

    def _file(self, filesystem_id):
        file = self._request("GET", "/v1/filesystems/" + quote(filesystem_id, safe=""), filesystem=True)
        if file.get("region") != self.vm.region or file.get("provider") != self.vm.provider:
            raise RuntimeError("Drive placement differs from sandbox placement")
        return file

    def _named(self, name):
        matches = {
            file["filesystem_id"]
            for file in self._pages("/v1/filesystems", "filesystems", filesystem=True, name_prefix=name)
            if file["name"] == name
        }
        if len(matches) != 1:
            raise Exception("Mount drive failed: drive name does not identify one filesystem")
        file = self._file(matches.pop())
        if file["name"] != name:
            raise Exception("Mount drive failed: drive identity changed during lookup")
        return file

    @property
    def _mounts_path(self):
        return "/v1/vms/" + quote(self.vm.id, safe="") + "/mounts"

    def _mounts(self):
        return list(self._pages(self._mounts_path, "mounts"))

    @staticmethod
    def _binding(file, mount):
        return {
            "drive_name": file["name"],
            "mount_path": mount["path"],
            "drive_path": _path("/" + (mount.get("subpath") or "")),
            "read_only": False,
        }

    def mount(self, drive_name, mount_path, drive_path="/", read_only=False):
        if read_only is not False:
            raise ValueError("Arker drive mounts support only read_only=False")
        mount_path, drive_path = _path(mount_path, relative=True), _path(drive_path)
        if mount_path == "/":
            raise ValueError("Drive mounts cannot replace the root filesystem")
        if not isinstance(drive_name, str) or not drive_name:
            raise ValueError("Drive name must be a nonempty string")
        file = self._named(drive_name)

        def current():
            rows = [row for row in self._mounts() if _path(row["path"]) == mount_path]
            if len(rows) > 1 or (
                rows
                and (
                    rows[0]["filesystem_id"] != file["filesystem_id"]
                    or _path("/" + (rows[0].get("subpath") or "")) != drive_path
                )
            ):
                raise Exception("Mount drive failed: mount path is already used by another binding")
            return rows[0] if rows else None

        row = current()
        if row is None:
            body = {"filesystem_id": file["filesystem_id"], "path": mount_path}
            if drive_path != "/":
                body["subpath"] = drive_path[1:]
            try:
                self._request("POST", self._mounts_path, body)
            except Exception as error:
                status = getattr(error, "status", None)
                if status is not None and 400 <= status < 500 and status not in {408, 409}:
                    raise
                try:
                    row = current()
                except Exception:
                    raise error from None
                if row is None:
                    raise
        deadline = time.monotonic() + 30
        while True:
            row = current()
            if not row:
                raise Exception("Mount drive failed: mount disappeared before readiness")
            if row.get("state") == "mounted":
                return DriveMountResponse(
                    **self._binding(file, row), success=True, message="Drive mounted successfully"
                )
            if row.get("state") != "attaching":
                raise Exception("Mount drive failed: " + (row.get("state_detail") or "mount is not ready"))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Drive mount readiness timed out; the mount may continue")
            self.cancelled.wait(min(0.1, remaining))
            self._check()

    def list(self):
        return [
            DriveMountInfo(**self._binding(self._file(row["filesystem_id"]), row))
            for row in self._mounts()
            if row.get("state") == "mounted"
        ]

    def unmount(self, mount_path):
        target = _path(mount_path, relative=True)
        rows = [row for row in self._mounts() if _path(row["path"]) == target]
        if len(rows) != 1:
            raise Exception("Unmount drive failed: mount path does not identify one mount")
        result = self._request("DELETE", self._mounts_path + "/" + quote(rows[0]["mount_id"], safe=""))
        if not result.get("deleted"):
            raise Exception("Unmount drive failed: deletion was not confirmed")
        return DriveUnmountResponse(mount_path=target, success=True, message="Drive unmounted successfully")


def _call(vm, method, args, cancelled=None):
    try:
        return getattr(_Bindings(vm, cancelled), method)(*args)
    except ArkerError as error:
        label = {"mount": "Mount drive", "unmount": "Unmount drive", "list": "List drives"}[method]
        raise Exception(f"{label} failed: {error}") from error


class SyncDrives(SyncSandboxDrive):
    def __init__(self, vm: VM, config):
        super().__init__(config)
        self._vm = vm

    def mount(
        self, drive_name: str, mount_path: str, drive_path: str = "/", read_only: bool = False
    ) -> DriveMountResponse:
        return _call(self._vm, "mount", (drive_name, mount_path, drive_path, read_only))

    def list(self) -> list[DriveMountInfo]:
        return _call(self._vm, "list", ())

    def unmount(self, mount_path: str) -> DriveUnmountResponse:
        return _call(self._vm, "unmount", (mount_path,))


class AsyncDrives(SandboxDrive):
    def __init__(self, vm: VM, config):
        super().__init__(config)
        self._vm = vm

    async def _call(self, method, *args):
        cancelled = threading.Event()
        try:
            return await asyncio.to_thread(_call, self._vm, method, args, cancelled)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def mount(
        self, drive_name: str, mount_path: str, drive_path: str = "/", read_only: bool = False
    ) -> DriveMountResponse:
        return await self._call("mount", drive_name, mount_path, drive_path, read_only)

    async def list(self) -> list[DriveMountInfo]:
        return await self._call("list")

    async def unmount(self, mount_path: str) -> DriveUnmountResponse:
        return await self._call("unmount", mount_path)
