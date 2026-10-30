"""Daytona's filesystem API over identity-bound guest operations."""

from __future__ import annotations

import json
import os
import shlex
import stat
from importlib.resources import files

from ._compat import UnsupportedMixin, run_command
from ._daytona_request import async_request_timeout, request_timeout

_SOURCE = files("arker").joinpath("_guest_daytona_filesystem.py").read_text()


def guest_command(request):
    return (
        'setpriv --reuid=daytona --regid="$(id -g daytona)" --init-groups -- python3 -I -c '
        + shlex.quote(_SOURCE)
        + " "
        + shlex.quote(json.dumps(request))
    )


def local_file(path, data=None):
    from daytona.common.errors import DaytonaError

    writing = data is not None
    try:
        if writing:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        flags = os.O_NONBLOCK | (os.O_WRONLY | os.O_CREAT if writing else os.O_RDONLY)
        fd = os.open(path, flags, 0o666)
        with os.fdopen(fd, "wb" if writing else "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("local transfers require a regular file")
            if writing:
                stream.truncate(0)
                stream.write(data)
                return None
            return stream.read()
    except (OSError, ValueError) as error:
        raise DaytonaError(str(error)) from error


class FileSystem(UnsupportedMixin):
    _compat_name = "daytona filesystem"

    def __init__(self, vm, *, asynchronous=False):
        self._vm = vm
        self._asynchronous = asynchronous

    def _run(self, command):
        from daytona.common.errors import DaytonaError, create_daytona_error

        result = run_command(self._vm, command)
        if result.exit_code:
            try:
                error = json.loads(result.stderr)
            except ValueError:
                raise DaytonaError(result.stderr or "Daytona filesystem command failed") from None
            raise create_daytona_error(error["message"], status_code=error["status"])
        return result.stdout

    def _command(self, operation, path, **options):
        if not isinstance(path, str) or not path or "\0" in path:
            from daytona.common.errors import DaytonaBadRequestError

            raise DaytonaBadRequestError("path must be a nonempty string without NUL", status_code=400)
        return guest_command(dict(operation=operation, path=path, **options))

    @request_timeout
    def _call(self, operation, path, request_timeout=None, **options):
        return json.loads(self._run(self._command(operation, path, **options)))

    def _transfer(self, operation, path, data=None, *, timeout=1800):
        from ._daytona_streams import download, upload

        if operation == "upload":
            return upload(self, data, path, None, None, timeout)
        return b"".join(download(self, path, None, None, timeout))

    def create_folder(self, path: str, mode: str, request_timeout: float | None = None) -> None:
        self._call("mkdir", path, request_timeout, mode=mode)

    def delete_file(self, path: str, recursive: bool = False, request_timeout: float | None = None) -> None:
        self._call("remove", path, request_timeout, recursive=recursive)

    def get_file_info(self, path: str, request_timeout: float | None = None):
        return self._info(self._call("info", path, request_timeout))

    def _info(self, value):
        if self._asynchronous:
            from daytona_toolbox_api_client_async import FileInfo
        else:
            from daytona_toolbox_api_client import FileInfo
        return FileInfo.from_dict(value)

    def list_files(self, path: str, depth: int | None = None, request_timeout: float | None = None):
        if depth is not None and (type(depth) is not int or depth < 1):
            from daytona.common.errors import DaytonaError

            raise DaytonaError("depth must be at least 1")
        return [self._info(value) for value in self._call("list", path, request_timeout, depth=depth)]

    def find_files(self, path: str, pattern: str, request_timeout: float | None = None):
        if self._asynchronous:
            from daytona_toolbox_api_client_async import Match
        else:
            from daytona_toolbox_api_client import Match
        return [Match.from_dict(value) for value in self._call("find", path, request_timeout, pattern=pattern)]

    def search_files(self, path: str, pattern: str, request_timeout: float | None = None):
        if self._asynchronous:
            from daytona_toolbox_api_client_async import SearchFilesResponse
        else:
            from daytona_toolbox_api_client import SearchFilesResponse
        return SearchFilesResponse.from_dict(self._call("search", path, request_timeout, pattern=pattern))

    def move_files(self, source: str, destination: str, request_timeout: float | None = None) -> None:
        self._call("move", source, request_timeout, destination=destination)

    def set_file_permissions(
        self,
        path: str,
        mode: str | None = None,
        owner: str | None = None,
        group: str | None = None,
        request_timeout: float | None = None,
    ) -> None:
        self._call("permissions", path, request_timeout, mode=mode, owner=owner, group=group)

    def download_file_stream(self, remote_path, timeout=1800, on_progress=None, cancel_event=None):
        from ._daytona_streams import download

        return download(self, remote_path, on_progress, cancel_event, timeout)

    def upload_file_stream(self, source, remote_path, timeout=1800, on_progress=None, cancel_event=None):
        from ._daytona_streams import upload

        return upload(self, source, remote_path, on_progress, cancel_event, timeout)

    def upload_file(self, src: str | bytes, dst: str, timeout: int = 1800) -> None:
        if not isinstance(src, (str, bytes, bytearray)):
            raise TypeError("source must be bytes or a local path")
        data = local_file(src) if isinstance(src, str) else bytes(src)
        self._transfer("upload", dst, data, timeout=timeout)

    def upload_files(self, files, timeout: int = 1800) -> None:
        for item in files:
            self.upload_file(item.source, item.destination, timeout)

    def download_file(self, *args: str) -> bytes | None:
        if not 1 <= len(args) <= 3:
            raise TypeError("download_file expects a remote path, optional local path and timeout")
        local = args[1] if len(args) > 1 and isinstance(args[1], str) else None
        timeout = args[2] if len(args) == 3 else args[1] if len(args) == 2 and local is None else 1800
        data = self._transfer("download", args[0], timeout=timeout)
        if local is None:
            return data
        local_file(local, data)
        return None

    def download_files(self, files, timeout: int = 1800):
        from daytona.common.errors import DaytonaError
        from daytona.common.filesystem import FileDownloadErrorDetails, FileDownloadResponse

        results = []
        for item in files:
            try:
                result = (
                    self.download_file(item.source, item.destination, timeout)
                    if item.destination
                    else self.download_file(item.source, timeout)
                )
                results.append(FileDownloadResponse(source=item.source, result=item.destination or result))
            except DaytonaError as error:
                results.append(
                    FileDownloadResponse(
                        source=item.source,
                        error=str(error),
                        error_details=FileDownloadErrorDetails(
                            message=str(error),
                            status_code=error.status_code,
                            code=error.code,
                            source=error.source,
                        ),
                    )
                )
        return results


class AsyncFileSystem:
    def __init__(self, vm):
        self._sync = FileSystem(vm, asynchronous=True)

    async def _metadata(self, function, *args):
        from ._compat_async import route

        async with async_request_timeout(function, args, {}) as requests:
            return await route(lambda: function(*args), bridges=(requests,), drain_on_cancel=True)

    async def create_folder(self, path: str, mode: str, request_timeout: float | None = None) -> None:
        return await self._metadata(self._sync.create_folder, path, mode, request_timeout)

    async def delete_file(self, path: str, recursive: bool = False, request_timeout: float | None = None) -> None:
        return await self._metadata(self._sync.delete_file, path, recursive, request_timeout)

    async def get_file_info(self, path: str, request_timeout: float | None = None):
        return await self._metadata(self._sync.get_file_info, path, request_timeout)

    async def list_files(self, path: str, depth: int | None = None, request_timeout: float | None = None):
        return await self._metadata(self._sync.list_files, path, depth, request_timeout)

    async def find_files(self, path: str, pattern: str, request_timeout: float | None = None):
        return await self._metadata(self._sync.find_files, path, pattern, request_timeout)

    async def search_files(self, path: str, pattern: str, request_timeout: float | None = None):
        return await self._metadata(self._sync.search_files, path, pattern, request_timeout)

    async def move_files(self, source: str, destination: str, request_timeout: float | None = None) -> None:
        return await self._metadata(self._sync.move_files, source, destination, request_timeout)

    async def set_file_permissions(
        self,
        path: str,
        mode: str | None = None,
        owner: str | None = None,
        group: str | None = None,
        request_timeout: float | None = None,
    ) -> None:
        return await self._metadata(self._sync.set_file_permissions, path, mode, owner, group, request_timeout)

    async def download_file_stream(self, remote_path, timeout=1800, on_progress=None, cancel_event=None):
        from ._daytona_streams import async_download

        return async_download(self._sync, remote_path, on_progress, cancel_event, timeout)

    async def upload_file_stream(self, source, remote_path, timeout=1800, on_progress=None, cancel_event=None):
        from ._daytona_streams import async_upload

        await async_upload(self._sync, source, remote_path, on_progress, cancel_event, timeout)

    async def upload_file(self, src: str | bytes, dst: str, timeout: int = 1800) -> None:
        from ._compat_async import call

        return await call(self._sync.upload_file, src, dst, timeout)

    async def upload_files(self, files, timeout: int = 1800) -> None:
        from ._compat_async import call

        return await call(self._sync.upload_files, files, timeout)

    async def download_file(self, *args: str) -> bytes | None:
        from ._compat_async import call

        return await call(self._sync.download_file, *args)

    async def download_files(self, files, timeout: int = 1800):
        from ._compat_async import call

        return await call(self._sync.download_files, files, timeout)
