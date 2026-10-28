from __future__ import annotations

import asyncio
import posixpath
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._compat import (
    CommandResult,
    CompatibilityCreationNotStartedError,
    CompatibilityNotFoundError,
    UnsupportedCompatibilityOption,
    UnsupportedMixin,
    arker_create,
    arker_get,
    format_not_found,
    get_or_none,
    import_optional,
    pop_arker_config,
    resolve_compat_path,
    routed_create,
    routed_get,
    run_command,
)
from ._compat_process import GuestProcesses, success
from .computer import VM


def __getattr__(name: str) -> Any:
    for module_name in ("blaxel.core.sandbox", "blaxel.core"):
        module = import_optional(module_name)
        if module is not None:
            try:
                return getattr(module, name)
            except AttributeError:
                pass
    raise AttributeError(name)


@dataclass(frozen=True)
class DirectoryEntry:
    name: str
    path: str
    size: int | None = None
    permissions: str | None = None
    owner: str | None = None
    group: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class Directory:
    name: str
    path: str
    files: list[DirectoryEntry]
    subdirectories: list[DirectoryEntry]


@dataclass(frozen=True)
class SandboxMetadata:
    name: str


@dataclass(frozen=True)
class FindMatch:
    path: str
    type_: str


@dataclass(frozen=True)
class FindResponse:
    matches: list[FindMatch]
    total: int


@dataclass(frozen=True)
class ContentSearchMatch:
    column: int
    line: int
    path: str
    text: str
    context: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in vars(self).items() if value is not None}


@dataclass(frozen=True)
class ContentSearchResponse:
    matches: list[ContentSearchMatch]
    query: str
    total: int

    def to_dict(self) -> dict[str, Any]:
        return {"matches": [match.to_dict() for match in self.matches], "query": self.query, "total": self.total}


@dataclass(frozen=True)
class CopyResponse:
    message: str
    source: str
    destination: str


def _filesystem_display_path(path: str) -> str:
    return re.sub(r"/+", "/", resolve_compat_path(path, "/blaxel"))


def _filesystem_path(path: str) -> str:
    return posixpath.normpath(_filesystem_display_path(path))


def _filesystem_file_path(path: str) -> str:
    if path.startswith("/") or "/" not in path:
        return _filesystem_path(path)
    return posixpath.normpath("/" + path)


def _request_values(request: Any) -> dict[str, Any]:
    if isinstance(request, dict):
        return dict(request)
    if hasattr(request, "to_dict"):
        values = request.to_dict()
        for key in ("on_log", "on_stdout", "on_stderr"):
            callback = getattr(request, key, None)
            if callback is not None:
                values[key] = callback
        return values
    return {key: value for key, value in vars(request).items() if value is not None}


class Process:
    def __init__(self, vm: VM | None = None, native: Any | None = None) -> None:
        self._vm = vm
        self._native = native
        self._guest = GuestProcesses(vm) if vm is not None else None

    def exec(self, request: Any) -> CommandResult | Any:
        if self._vm is None:
            if not isinstance(request, (str, dict)):
                return self._native.exec(request)
            module = import_optional("blaxel.core.sandbox")
            request_type = getattr(module, "ProcessRequestWithLog", None) if module is not None else None
            if request_type is None:
                return self._native.exec(request)
            values = {"command": request} if isinstance(request, str) else dict(request)
            if "workingDir" in values:
                values["working_dir"] = values.pop("workingDir")
            if "envs" in values:
                values["env"] = values.pop("envs")
            if isinstance(request, str):
                values.setdefault("wait_for_completion", True)
            return self._native.exec(request_type(**values))
        if isinstance(request, str):
            command = f"mkdir -p /blaxel && cd /blaxel && {request}"
            timeout = None
        else:
            request = _request_values(request)
            aliases = {"working_dir": "workingDir", "envs": "env", "wait_for_completion": "waitForCompletion"}
            for key, alias in aliases.items():
                if key in request:
                    request[alias] = request.pop(key)
            supported = {
                "command",
                "cmd",
                "workingDir",
                "env",
                "timeout",
                "waitForCompletion",
                "name",
                "stdin",
                "on_log",
                "on_stdout",
                "on_stderr",
            }
            unknown = sorted(set(request) - supported)
            if unknown:
                raise UnsupportedCompatibilityOption(
                    "Arker process.exec cannot represent option(s): " + ", ".join(unknown)
                )
            command = request.get("command") or request.get("cmd")
            if isinstance(command, list):
                command = " ".join(shlex.quote(str(value)) for value in command)
            if not isinstance(command, str):
                raise TypeError("process.exec requires a command string or list")
            request["command"] = command
            request.pop("cmd", None)
            if request.get("timeout") is not None:
                request["timeout"] *= 1000
            request["signalExitCode"] = -1
            return self._guest.exec(request)
        return run_command(self._vm, command, timeout=timeout)

    def __getattr__(self, name: str) -> Any:
        if self._native is not None:
            return getattr(self._native, name)
        if self._guest is not None and hasattr(self._guest, name):
            return getattr(self._guest, name)
        raise AttributeError(f"process.{name} is not supported by the Arker compatibility layer for blaxel")


class FileSystem:
    def __init__(self, vm: VM | None = None, native: Any | None = None) -> None:
        self._vm = vm
        self._native = native

    def read(self, path: str) -> str:
        if self._vm is None:
            return self._native.read(path)
        return self._vm.sync(_filesystem_file_path(path)).decode("utf-8", "replace")

    def write(self, path: str, content: str | bytes) -> Any:
        if self._vm is None:
            return self._native.write(path, content)
        resolved = _filesystem_file_path(path)
        self._vm.sync(resolved, content)
        return success({"message": "File created/updated successfully", "path": _filesystem_display_path(path)})

    def write_binary(self, path: str, content: bytes | bytearray | str) -> Any:
        if self._vm is None:
            return self._native.write_binary(path, content)
        raw = Path(content).read_bytes() if isinstance(content, str) else bytes(content)
        resolved = _filesystem_file_path(path)
        self._vm.sync(resolved, raw)
        return success({"message": "Binary file uploaded successfully", "path": _filesystem_display_path(path)})

    def read_binary(self, path: str) -> bytes:
        if self._vm is None:
            return self._native.read_binary(path)
        return self._vm.sync(_filesystem_file_path(path))

    def download(self, src: str, destination_path: str, mode: int = 0o644) -> None:
        if self._vm is None:
            return self._native.download(src, destination_path, mode=mode)
        destination = Path(destination_path)
        destination.write_bytes(self.read_binary(src))
        destination.chmod(mode)
        return None

    def mkdir(self, path: str, permissions: str = "0755") -> Any:
        if self._vm is None:
            return self._native.mkdir(path, permissions=permissions)
        if re.fullmatch(r"0?[0-7]{3}", permissions) is None:
            raise ValueError("fs.mkdir permissions must be a three- or four-digit octal mode")
        resolved = _filesystem_path(path)
        quoted = shlex.quote(resolved)
        result = run_command(self._vm, f"mkdir -p -- {quoted} && chmod -- {permissions} {quoted}")
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or f"mkdir failed with exit {result.exit_code}")
        return success({"message": "Directory created successfully", "path": _filesystem_display_path(path)})

    def ls(self, path: str) -> Any:
        if self._vm is None:
            return self._native.ls(path)
        display = _filesystem_display_path(path)
        resolved = _filesystem_path(path)
        result = run_command(
            self._vm,
            # find sanitizes control characters on a terminal. The success
            # marker preserves producer failure detection through the pipe.
            f"(find {shlex.quote(resolved)} -mindepth 1 -maxdepth 1 "
            "-printf '%y\\0%f\\0%s\\0%m\\0%u\\0%g\\0%TY-%Tm-%TdT%TH:%TM:%TS%Tz\\0' "
            "&& printf 'complete\\0') | cat",
        )
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or f"list failed with exit {result.exit_code}")
        files: list[DirectoryEntry] = []
        subdirectories: list[DirectoryEntry] = []
        fields = result.stdout.split("\0")
        if fields[-2:] != ["complete", ""]:
            raise RuntimeError(result.stderr or "Incomplete directory listing response")
        records = fields[:-2]
        width = 7 if len(records) % 7 == 0 else 2 if len(records) % 2 == 0 else 0
        if width == 0:
            raise RuntimeError(result.stderr or "Incomplete directory listing response")
        entries = [records[index : index + width] for index in range(0, len(records), width)]
        for values in sorted(entries, key=lambda entry: entry[1]):
            kind, name = values[:2]
            metadata = (
                {
                    "size": int(values[2]),
                    "permissions": values[3],
                    "owner": values[4],
                    "group": values[5],
                    "last_modified": values[6],
                }
                if width == 7 and kind != "d"
                else {}
            )
            entry = DirectoryEntry(
                name=name,
                path=posixpath.normpath(posixpath.join(display, name)),
                **metadata,
            )
            (subdirectories if kind == "d" else files).append(entry)
        return Directory(
            name=posixpath.basename(display.rstrip("/")) or "/",
            path=display,
            files=files,
            subdirectories=subdirectories,
        )

    def rm(self, path: str, recursive: bool = False) -> Any:
        if self._vm is None:
            return self._native.rm(path, recursive=recursive)
        resolved = _filesystem_path(path)
        quoted = shlex.quote(resolved)
        directory_command = "rm -r" if recursive else "rmdir"
        result = run_command(
            self._vm,
            f"if [ -d {quoted} ] && [ ! -L {quoted} ]; then "
            f"{directory_command} -- {quoted} && printf directory; "
            f"else rm -- {quoted} && printf file; fi",
        )
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or f"remove failed with exit {result.exit_code}")
        kind = "Directory" if result.stdout == "directory" else "File"
        return success({"message": f"{kind} deleted successfully", "path": _filesystem_display_path(path)})

    def cp(self, source: str, destination: str, max_wait: int = 180000) -> Any:
        if self._vm is None:
            return self._native.cp(source, destination, max_wait=max_wait)
        if max_wait <= 0:
            raise ValueError("max_wait must be positive")
        resolved_source = _filesystem_file_path(source)
        resolved_destination = _filesystem_file_path(destination)
        result = run_command(
            self._vm,
            f"cp -r -- {shlex.quote(resolved_source)} {shlex.quote(resolved_destination)}",
            timeout=(max_wait + 999) // 1000,
        )
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or f"Could not copy {source} to {destination}")
        return CopyResponse(message="Files copied", source=source, destination=destination)

    def write_tree(self, files: list[Any], destination_path: str | None = None) -> Any:
        if self._vm is None:
            return self._native.write_tree(files, destination_path)
        destination = destination_path or ""
        for file in files:
            values = file if isinstance(file, dict) else vars(file)
            path = values.get("path")
            content = values.get("content")
            if not isinstance(path, str) or not isinstance(content, (str, bytes)):
                raise TypeError("write_tree files require string path and string/bytes content")
            self.write(posixpath.join(destination, path), content)
        return self.ls(destination or "/blaxel")

    def find(
        self,
        path: str,
        type: str | None = None,
        patterns: list[str] | None = None,
        max_results: int | None = None,
        exclude_dirs: list[str] | None = None,
        exclude_hidden: bool | None = None,
    ) -> FindResponse | Any:
        if self._vm is None:
            return self._native.find(
                path,
                type=type,
                patterns=patterns,
                max_results=max_results,
                exclude_dirs=exclude_dirs,
                exclude_hidden=exclude_hidden,
            )
        if type not in {None, "file", "directory"}:
            raise ValueError("find type must be 'file' or 'directory'")
        if max_results is not None and max_results < 0:
            raise ValueError("max_results must be nonnegative")
        root = _filesystem_path(path)
        predicates: list[str] = []
        if type is not None:
            predicates.extend(["-type", "f" if type == "file" else "d"])
        if patterns:
            names = " -o ".join(f"-name {shlex.quote(pattern)}" for pattern in patterns)
            predicates.append(f"\\( {names} \\)")
        for directory in exclude_dirs or []:
            predicates.append(f"! -path {shlex.quote(f'*/{directory}/*')}")
        if exclude_hidden:
            predicates.append("! -path '*/.*'")
        command = f"find {shlex.quote(root)} -mindepth 1 {' '.join(predicates)} -printf '%y\\0%p\\0'"
        result = run_command(self._vm, command)
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or f"find failed with exit {result.exit_code}")
        fields = result.stdout.split("\0")
        if fields[-1:] != [""] or len(fields[:-1]) % 2:
            raise RuntimeError("Incomplete find response")
        matches = [
            FindMatch(path=name, type_="directory" if kind == "d" else "file")
            for kind, name in zip(fields[:-1:2], fields[1:-1:2], strict=True)
        ]
        if max_results is not None:
            matches = matches[:max_results]
        return FindResponse(matches=matches, total=len(matches))

    def grep(
        self,
        query: str,
        path: str = "/",
        case_sensitive: bool | None = None,
        context_lines: int | None = None,
        max_results: int | None = None,
        file_pattern: str | None = None,
        exclude_dirs: list[str] | None = None,
    ) -> ContentSearchResponse | Any:
        if self._vm is None:
            return self._native.grep(
                query,
                path,
                case_sensitive=case_sensitive,
                context_lines=context_lines,
                max_results=max_results,
                file_pattern=file_pattern,
                exclude_dirs=exclude_dirs,
            )
        if context_lines is not None and context_lines < 0:
            raise ValueError("context_lines must be nonnegative")
        if max_results is not None and max_results < 0:
            raise ValueError("max_results must be nonnegative")
        flags = ["-R", "-n", "-H", "--null", "-F", "-I"]
        if case_sensitive is not True:
            flags.append("-i")
        if file_pattern:
            flags.extend(["--include", file_pattern])
        for directory in exclude_dirs or []:
            flags.extend(["--exclude-dir", directory])
        command = " ".join(
            [
                "grep",
                *(shlex.quote(flag) for flag in flags),
                "--",
                shlex.quote(query),
                shlex.quote(_filesystem_path(path)),
            ]
        )
        result = run_command(self._vm, f"test -d {shlex.quote(_filesystem_path(path))} || exit 2; {command}")
        if result.exit_code not in {0, 1}:
            raise RuntimeError(result.stderr or f"grep failed with exit {result.exit_code}")
        matches: list[ContentSearchMatch] = []
        contents: dict[str, list[str]] = {}
        limit = 100 if max_results is None else max_results
        offset = 0
        for record in re.finditer(r"([^\0]+)\0([0-9]+):([^\n]*)(?:\n|$)", result.stdout):
            if record.start() != offset:
                raise RuntimeError("Incomplete grep response")
            offset = record.end()
            if len(matches) >= limit:
                continue
            filename, line_text, text = record.groups()
            line = int(line_text)
            needle = query if case_sensitive is True else query.lower()
            haystack = text if case_sensitive is True else text.lower()
            context = None
            if context_lines:
                if filename not in contents:
                    contents[filename] = self.read(filename).split("\n")
                context = "\n".join(contents[filename][max(0, line - context_lines - 1) : line + context_lines])
            matches.append(
                ContentSearchMatch(
                    column=max(haystack.find(needle), 0) + 1,
                    line=line,
                    path=posixpath.relpath(filename, _filesystem_path(path)),
                    text=text,
                    context=context,
                )
            )
        if offset != len(result.stdout):
            raise RuntimeError("Incomplete grep response")
        response = ContentSearchResponse(matches=matches, query=query, total=len(matches))
        models = import_optional("blaxel.core.sandbox.client.models")
        return models.ContentSearchResponse.from_dict(response.to_dict()) if models is not None else response

    def watch(self, path: str, callback: Any, options: dict[str, Any] | None = None) -> Any:
        if self._vm is None:
            return self._native.watch(path, callback, options)
        raise UnsupportedCompatibilityOption(
            "Arker fs.watch requires an ordered filesystem event stream and is not supported"
        )

    def search(self, query: str, path: str = "/", options: dict[str, Any] | None = None) -> Any:
        if self._vm is None:
            return self._native.search(query, path, options)
        raise UnsupportedCompatibilityOption(
            "Arker fs.search cannot reproduce Blaxel's server-side fuzzy scoring and ordering"
        )

    def __getattr__(self, name: str) -> Any:
        if self._native is not None:
            return getattr(self._native, name)
        raise AttributeError(f"fs.{name} is not supported by the Arker compatibility layer for blaxel")


class _AsyncNetwork:
    def __init__(self, vm: VM):
        self._vm = vm
        self._client = None

    def get_client(self):
        from ._compat_network import async_client

        if self._client is None:
            self._client = async_client(self._vm)
        return self._client

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(self, port: int, path: str = "/", method: str = "GET", **kwargs):
        from ._compat_network import request_path

        return await self.get_client().request(method, request_path(port, path), **kwargs)


class _Network:
    def __init__(self, vm: VM):
        self._vm = vm

    def fetch(self, port: int, path: str = "/", method: str = "GET", **kwargs):
        from ._compat_network import fetch

        return fetch(self._vm, port, path, method=method, **kwargs)


class SyncSandboxInstance(UnsupportedMixin):
    _compat_name = "blaxel"

    def __init__(self, vm: VM | None = None, native: Any | None = None) -> None:
        self._vm = vm
        self._native = native
        metadata = getattr(native, "metadata", None)
        metadata_name = metadata.get("name") if isinstance(metadata, dict) else getattr(metadata, "name", None)
        self.id = (
            vm.id
            if vm is not None
            else getattr(native, "id", None) or getattr(native, "sandbox_id", None) or metadata_name
        )
        self.metadata = SandboxMetadata(name=vm.name or vm.id) if vm is not None else metadata
        self.process = Process(vm, getattr(native, "process", None))
        native_fs = getattr(native, "fs", getattr(native, "filesystem", None))
        self.fs = native_fs if native_fs is not None else FileSystem(vm)
        self.network = getattr(native, "network", None) if native is not None else _Network(vm)

    def fetch(self, port: int, path: str = "/", method: str = "GET", **kwargs):
        if self._native is not None:
            return self._native.fetch(port, path, method=method, **kwargs)
        return self.network.fetch(port, path, method=method, **kwargs)

    @staticmethod
    def _native_class() -> Any | None:
        module = import_optional("blaxel.core.sandbox")
        return getattr(module, "SyncSandboxInstance", None) if module is not None else None

    @classmethod
    def create(cls, sandbox: Any = None, **kwargs: Any) -> SyncSandboxInstance:
        arker_config = pop_arker_config(kwargs)
        native = cls._native_class()

        def create_native() -> Any:
            if native is None:
                raise CompatibilityCreationNotStartedError("Native Blaxel SDK is not installed")
            return native.create(sandbox, **kwargs)

        values = (
            {key: value for key, value in _request_values(sandbox).items() if value is not None}
            if sandbox is not None
            else {}
        )

        def create_arker() -> VM:
            supported = {"name", "image", "memory", "ttl", "envs", "runtime"}
            unknown = sorted(set(values) - supported)
            if kwargs or unknown:
                names = [*sorted(kwargs), *unknown]
                raise UnsupportedCompatibilityOption(
                    f"SandboxInstance.create received unsupported option(s) for Arker: {', '.join(names)}"
                )
            unsupported = [key for key in ("ttl", "envs", "runtime") if values.get(key) is not None]
            if unsupported:
                names = ", ".join(unsupported)
                raise UnsupportedCompatibilityOption(
                    f"SandboxInstance.create option(s) are not supported by Arker: {names}"
                )
            memory = values.get("memory")
            return arker_create(
                arker_config,
                name=values.get("name"),
                image=values.get("image"),
                resources={"memory_mib": memory} if memory is not None else None,
            )

        provider, result = routed_create(
            "blaxel",
            arker_config,
            {
                "arker": create_arker,
                "blaxel": create_native,
            },
        )
        if provider == "arker":
            try:
                initialized = run_command(result, "mkdir -p /blaxel")
                if initialized.exit_code != 0:
                    raise RuntimeError(initialized.stderr or "could not initialize /blaxel")
            except Exception:
                result.delete()
                raise
        return cls(vm=result if provider == "arker" else None, native=result if provider == "blaxel" else None)

    @classmethod
    def get(cls, sandbox_id: str, **kwargs: Any) -> SyncSandboxInstance:
        arker_config = pop_arker_config(kwargs)
        if kwargs:
            raise TypeError(f"SandboxInstance.get received unsupported option(s): {', '.join(sorted(kwargs))}")
        native = cls._native_class()
        found = routed_get(
            sandbox_id,
            "blaxel",
            arker_config,
            {
                "arker": lambda: arker_get(sandbox_id, arker_config),
                "blaxel": lambda: get_or_none(lambda: native.get(sandbox_id)) if native is not None else None,
            },
        )
        if found is None:
            raise format_not_found("Sandbox", sandbox_id)
        provider, result = found
        return cls(vm=result if provider == "arker" else None, native=result if provider == "blaxel" else None)

    @classmethod
    def create_if_not_exists(cls, sandbox: Any = None, **kwargs: Any) -> SyncSandboxInstance:
        values = (
            {key: value for key, value in _request_values(sandbox).items() if value is not None}
            if sandbox is not None
            else {}
        )
        name = values.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("create_if_not_exists requires a Blaxel sandbox name")
        lookup_kwargs = {
            key: value for key, value in kwargs.items() if key in {"arker", "router_config_path", "routerConfigPath"}
        }
        try:
            return cls.get(name, **lookup_kwargs)
        except CompatibilityNotFoundError:
            pass
        return cls.create(sandbox, **kwargs)

    def delete(self) -> None:
        if isinstance(self, str):
            SyncSandboxInstance.get(self).delete()
            return
        if self._vm is not None:
            self._vm.delete()
            return
        native = self._native_class()
        if native is not None and self.id:
            native.delete(self.id)
            return
        raise AttributeError("Native Blaxel sandbox does not expose delete().")


class _AsyncComponent:
    def __init__(self, component: Any) -> None:
        self._component = component

    def __getattr__(self, name: str) -> Any:
        member = getattr(self._component, name)
        if not callable(member):
            return member

        async def call(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.to_thread(member, *args, **kwargs)

        return call


class _AsyncProcess(_AsyncComponent):
    async def exec(self, request: Any) -> Any:
        values = (
            {"command": request, "wait_for_completion": True} if isinstance(request, str) else _request_values(request)
        )
        waiting = values.pop("wait_for_completion", values.pop("waitForCompletion", False))
        callbacks = {key: values.pop(key, None) for key in ("on_stdout", "on_stderr", "on_log")}
        values["waitForCompletion"] = False
        current = await asyncio.to_thread(self._component.exec, values)
        return await self._wait(current.name, -1, 50, callbacks) if waiting else current

    async def _wait(self, identifier, max_wait, interval, callbacks=None):
        if max_wait < -1 or interval <= 0:
            raise ValueError("max_wait must be -1 or nonnegative; interval must be positive")
        deadline = None if max_wait == -1 else time.monotonic() + max_wait / 1000
        offsets = {"stdout": 0, "stderr": 0}
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for process {identifier}")
            current = await asyncio.to_thread(self._component.get, identifier)
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(f"Timed out waiting for process {identifier}")
            for stream in offsets:
                data = getattr(current, stream)[offsets[stream] :]
                offsets[stream] += len(data)
                for key in (f"on_{stream}", "on_log"):
                    callback = (callbacks or {}).get(key)
                    if data and callback:
                        callback(data)
            if current.status != "running":
                return current
            delay = interval / 1000 if deadline is None else max(0, min(interval / 1000, deadline - time.monotonic()))
            await asyncio.sleep(delay)

    async def wait(self, identifier: str, max_wait: int = 60000, interval: int = 1000) -> Any:
        return await self._wait(identifier, max_wait, interval)


class SandboxInstance:
    """Awaitable Blaxel surface using the same router and adapters as sync calls."""

    def __init__(self, sandbox: SyncSandboxInstance, native: Any = None) -> None:
        self._sandbox = sandbox
        self._native = native
        self.id = sandbox.id
        self.process = native.process if native is not None else _AsyncProcess(sandbox.process)
        self.fs = native.fs if native is not None else _AsyncComponent(sandbox.fs)
        self.network = native.network if native is not None else _AsyncNetwork(sandbox._vm)

    async def fetch(self, port: int, path: str = "/", method: str = "GET", **kwargs):
        if self._native is not None:
            return await self._native.fetch(port, path, method=method, **kwargs)
        return await self.network.fetch(port, path, method=method, **kwargs)

    @classmethod
    async def _wrap(cls, sandbox: SyncSandboxInstance) -> SandboxInstance:
        native = None
        if sandbox._vm is None:
            module = import_optional("blaxel.core.sandbox")
            native = await module.SandboxInstance.get(sandbox.id)
        return cls(sandbox, native)

    @classmethod
    async def create(cls, sandbox: Any = None, **kwargs: Any) -> SandboxInstance:
        return await cls._wrap(await asyncio.to_thread(SyncSandboxInstance.create, sandbox, **kwargs))

    @classmethod
    async def create_if_not_exists(cls, sandbox: Any = None, **kwargs: Any) -> SandboxInstance:
        return await cls._wrap(await asyncio.to_thread(SyncSandboxInstance.create_if_not_exists, sandbox, **kwargs))

    @classmethod
    async def get(cls, sandbox_id: str, **kwargs: Any) -> SandboxInstance:
        return await cls._wrap(await asyncio.to_thread(SyncSandboxInstance.get, sandbox_id, **kwargs))

    async def delete(self) -> None:
        if isinstance(self, str):
            await asyncio.to_thread(SyncSandboxInstance.delete, self)
            return
        try:
            if self._native is None:
                await self.network.aclose()
        finally:
            await asyncio.to_thread(self._sandbox.delete)

    def __getattr__(self, name: str) -> Any:
        if self._native is not None:
            return getattr(self._native, name)
        return getattr(_AsyncComponent(self._sandbox), name)
