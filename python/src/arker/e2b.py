"""E2B-compatible sandboxes backed by Arker VMs.

Replace ``from e2b import ...`` with ``from arker.e2b import ...``. What this
layer cannot honor raises an exception rather than doing something else.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import enum
import functools
import inspect
import itertools
import json
import posixpath
import urllib.parse
import uuid
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from typing import Any, ClassVar, Generic, Literal, TypeVar

from ._aio import AsyncWrapper, asynchronous, call, deliver, iterate
from ._compat import (
    POLL_INTERVAL,
    Change,
    CommandResult,
    CompatGit,
    CompatSandbox,
    Egress,
    Entry,
    Errors,
    GitFileChange,
    Process,
    Terminal,
    UnsupportedMixin,
    Volumes,
    import_optional,
    missing_export,
    new_port_auth,
    parse_time,
    reject_options,
    split_addresses,
    unsupported,
)
from .computer import VM

# E2B runs commands, and resolves relative paths, from this directory.
HOME = "/home/user"
DEFAULT_SANDBOX_TIMEOUT = 300
DEFAULT_COMMAND_TIMEOUT = 60
ALL_TRAFFIC = "0.0.0.0/0"
# E2B lets a sandbox that allows domains reach this nameserver unless told otherwise.
# Requests to the ports of a sandbox created with allow_public_traffic=False carry its token in this header.
TRAFFIC_TOKEN_HEADER = "e2b-traffic-access-token"
# E2B's own page size when listing.
DEFAULT_PAGE_SIZE = 100
# Options E2B uses to reach its own service; they have no meaning on Arker.
E2B_CONNECTION_OPTIONS = {
    "access_token",
    "api_key",
    "debug",
    "domain",
    "headers",
    "idempotency_key",
    "logger",
    "proxy",
    "request_timeout",
    "secure",
}
# Options that only shape how E2B moves file data.
FILE_TRANSPORT_OPTIONS = {"gzip", "request_timeout", "use_octet_stream"}


class SandboxException(Exception):
    pass


class TimeoutException(SandboxException):
    pass


class InvalidArgumentException(SandboxException):
    pass


class NotFoundException(SandboxException):
    pass


class TemplateException(NotFoundException):
    pass


class SandboxNotFoundException(NotFoundException):
    pass


class FileNotFoundException(NotFoundException):
    pass


class AuthenticationException(SandboxException):
    pass


class RateLimitException(SandboxException):
    pass


class ServiceBusyException(SandboxException):
    pass


class GuestUnresponsiveException(SandboxException):
    pass


class SandboxNotRunningException(SandboxException):
    pass


class NotSupportedException(InvalidArgumentException, AttributeError):
    pass


class CommandExitException(SandboxException):
    def __init__(self, result: CommandResult) -> None:
        super().__init__(f"Command exited with code {result.exit_code} and error:\n{result.stderr}")
        self.stdout = result.stdout
        self.stderr = result.stderr
        self.exit_code = result.exit_code
        self.error = result.error


ERRORS = Errors(
    provider="E2B",
    unsupported=NotSupportedException,
    invalid=InvalidArgumentException,
    not_found=TemplateException,
    file_not_found=FileNotFoundException,
    timeout=TimeoutException,
    failed=SandboxException,
    auth=AuthenticationException,
    rate_limit=RateLimitException,
)


@dataclasses.dataclass(frozen=True)
class SandboxNetworkSelectorContext:
    all_traffic: str
    rules: dict[str, Any]


def _selector(value: Any) -> list[str]:
    if callable(value):
        value = value(SandboxNetworkSelectorContext(all_traffic=ALL_TRAFFIC, rules={}))
    return list(value or [])


def _egress(network: dict[str, Any] | None, allow_internet_access: bool | None) -> Egress | None:
    """E2B's network options as Arker outbound rules: allow beats deny, and
    domains are allowed only with all other traffic denied."""
    options = dict(network or {})
    allow_ips, allow_hosts = split_addresses(_selector(options.pop("allow_out", None)))
    deny_ips, deny_hosts = split_addresses(_selector(options.pop("deny_out", None)))
    options.pop("allow_public_traffic", None)
    # Arker answers a sandbox's DNS queries itself, for the names its rules allow,
    # whichever nameserver they are sent to.
    reject_options(ERRORS, "network", options, inert={"dns_servers"})
    if deny_hosts:
        raise InvalidArgumentException(f"deny_out takes IPs and CIDR blocks, not domains: {', '.join(deny_hosts)}")
    if "*" in allow_hosts:
        raise unsupported(ERRORS, "network allow_out ['*']", "list the domains to allow.")
    deny_all = allow_internet_access is False or ALL_TRAFFIC in deny_ips
    if allow_hosts and not deny_all:
        raise InvalidArgumentException("Allowing domains in allow_out needs deny_out=[ALL_TRAFFIC].")
    if not (allow_ips or allow_hosts or deny_ips or deny_all):
        return None
    return Egress(
        allow_ips=tuple(allow_ips),
        allow_hosts=tuple(allow_hosts),
        deny_ips=tuple(ip for ip in deny_ips if ip != ALL_TRAFFIC),
        deny_all=deny_all,
    )


def _public_traffic(network: dict[str, Any] | None) -> bool:
    """Whether ports take requests without the sandbox's traffic token, as they do by default."""
    return (network or {}).get("allow_public_traffic") is not False


class _Layer(UnsupportedMixin):
    _compat_errors = ERRORS


def __getattr__(name: str) -> Any:
    return missing_export(ERRORS, __name__, name)


class FileType(str, enum.Enum):
    FILE = "file"
    DIR = "dir"


@dataclasses.dataclass(frozen=True)
class EntryInfo:
    name: str
    type: FileType
    path: str
    size: int = 0
    mode: int = 0
    permissions: str = ""
    owner: str = ""
    group: str = ""
    modified_time: datetime | None = None
    symlink_target: str | None = None


class FilesystemEventType(enum.Enum):
    CHMOD = "chmod"
    CREATE = "create"
    REMOVE = "remove"
    RENAME = "rename"
    WRITE = "write"


@dataclasses.dataclass
class FilesystemEvent:
    name: str
    type: FilesystemEventType
    entry: EntryInfo | None = None


@dataclasses.dataclass(frozen=True)
class ProcessInfo:
    pid: int
    tag: str | None
    cmd: str
    args: list[str]
    envs: dict[str, str]
    cwd: str | None = None


@dataclasses.dataclass(frozen=True)
class PtySize:
    rows: int
    cols: int


@dataclasses.dataclass(frozen=True)
class WriteInfo:
    name: str
    type: FileType | None
    path: str


@dataclasses.dataclass(frozen=True)
class SandboxInfo:
    sandbox_id: str
    template_id: str | None
    name: str | None
    metadata: dict[str, str]
    started_at: str | None
    state: str | None


def _sandbox_info(vm: VM, metadata: dict[str, str]) -> SandboxInfo:
    return SandboxInfo(
        sandbox_id=vm.id,
        template_id=vm.root_source_vm_name,
        name=vm.name,
        metadata=metadata,
        started_at=vm.created_at,
        state=vm.state,
    )


@dataclasses.dataclass
class SandboxQuery:
    metadata: dict[str, str] | None = None
    # Arker VMs resume when used, so every sandbox counts as running.
    state: list[str] | None = None
    started_after: datetime | None = None
    template: str | None = None


@dataclasses.dataclass(frozen=True)
class SnapshotInfo:
    snapshot_id: str
    names: list[str] = dataclasses.field(default_factory=list)


T = TypeVar("T")


class _Paginator(Generic[T]):
    """Pages of ``limit`` items; ``next_items`` while ``has_next``."""

    def __init__(self, items: Iterable[T], limit: int | None, cursor: Callable[[T], str] | None = None) -> None:
        self._items = iter(items)
        self._limit = limit or DEFAULT_PAGE_SIZE
        self._cursor = cursor
        self._buffer: list[T] = []
        self.has_next = True
        self.next_token: str | None = None

    def next_items(self, **_: Any) -> list[T]:
        if not self.has_next:
            raise SandboxException("No more items to fetch")
        # One item past the page tells whether another page follows.
        page = self._buffer + list(itertools.islice(self._items, self._limit + 1 - len(self._buffer)))
        self._buffer = page[self._limit :]
        self.has_next = bool(self._buffer)
        page = page[: self._limit]
        self.next_token = self._cursor(page[-1]) if self.has_next and self._cursor else None
        return page


class SandboxPaginator(_Paginator[SandboxInfo]):
    pass


class SnapshotPaginator(_Paginator[SnapshotInfo]):
    pass


def _list_key(info: SandboxInfo) -> tuple[str, str]:
    """Creation order: created_at is in whole seconds, and the ULID that ends a VM id breaks ties."""
    return (info.started_at or "", info.sandbox_id.rpartition("-")[2])


def _list_token(info: SandboxInfo) -> str:
    return base64.urlsafe_b64encode(json.dumps(_list_key(info)).encode()).decode()


def _after_token(token: str) -> tuple[str, str]:
    try:
        started_at, ulid = json.loads(base64.urlsafe_b64decode(token.encode()))
    except (ValueError, TypeError) as error:
        raise InvalidArgumentException(f"Invalid next_token {token!r}") from error
    return (started_at, ulid)


def _matches(sandbox: CompatSandbox, query: SandboxQuery) -> bool:
    vm = sandbox.vm
    return (
        (query.metadata or {}).items() <= sandbox.settings.items()
        and (not query.state or "running" in query.state)
        and (query.started_after is None or parse_time(vm.created_at) >= query.started_after.astimezone())
        and (query.template is None or query.template == vm.root_source_vm_name)
    )


def _absolute(path: str) -> str:
    return posixpath.join(HOME, path)


def _entry_info(entry: Entry) -> EntryInfo:
    return EntryInfo(
        name=entry.name,
        type=FileType.DIR if entry.is_dir else FileType.FILE,
        path=entry.path,
        size=entry.size,
        mode=entry.mode,
        permissions=entry.permissions,
        owner=entry.owner,
        group=entry.group,
        modified_time=datetime.fromtimestamp(entry.modified, timezone.utc),
        symlink_target=entry.symlink_target,
    )


def _check_options(method: str, options: dict[str, Any], user: str | None = None) -> None:
    """Commands and files are root's on Arker, so another ``user`` warns that it is ignored."""
    reject_options(
        ERRORS,
        method,
        {**options, "user": None if user in (None, "root") else user},
        ignored=FILE_TRANSPORT_OPTIONS,
        inert={"user"},
    )


def _checked(result: CommandResult) -> CommandResult:
    if result.exit_code != 0:
        raise CommandExitException(result)
    return result


def _to_bytes(data: Any) -> bytes:
    if isinstance(data, str):
        return data.encode()
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if hasattr(data, "read"):
        return _to_bytes(data.read())
    return b"".join(_to_bytes(chunk) for chunk in data)


class CommandHandle(_Layer):
    """A command started with ``commands.run(..., background=True)``."""

    _compat_path = "CommandHandle"

    def __init__(
        self,
        process: Process,
        on_stdout: Callable[[str], None] | None,
        on_stderr: Callable[[str], None] | None,
    ) -> None:
        self._process = process
        self._on_stdout = on_stdout
        self._on_stderr = on_stderr
        self._disconnected = False

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def stdout(self) -> str:
        return self._process.stdout

    @property
    def stderr(self) -> str:
        return self._process.stderr

    @property
    def exit_code(self) -> int | None:
        return self._process.poll()

    def wait(
        self,
        on_pty: Callable[[bytes], None] | None = None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> CommandResult:
        """Wait for the command, delivering output to the callbacks as it arrives."""
        return _checked(self._process.wait(on_stdout or self._on_stdout, on_stderr or self._on_stderr))

    def kill(self) -> bool:
        return self._process.kill()

    def send_stdin(self, data: str | bytes, request_timeout: float | None = None) -> None:
        self._process.send_stdin(data)

    def close_stdin(self, request_timeout: float | None = None) -> None:
        self._process.close_stdin()

    def disconnect(self) -> None:
        """Stop reading output; the command keeps running. Reconnect with ``commands.connect``."""
        self._disconnected = True

    def __iter__(self) -> Iterator[tuple[str | None, str | None, None]]:
        """Yield ``(stdout, None, None)`` or ``(None, stderr, None)`` chunks as they arrive."""
        for stdout, stderr in self._process.follow():
            if self._disconnected:
                return
            if stdout:
                yield stdout, None, None
            if stderr:
                yield None, stderr, None


class PtyHandle(_Layer):
    """A terminal from ``sandbox.pty.create``; iterating yields ``(None, None, bytes)``."""

    _compat_path = "CommandHandle"

    def __init__(self, terminal: Terminal) -> None:
        self._terminal = terminal
        self.pid = terminal.pid

    def __iter__(self) -> Iterator[tuple[None, None, bytes]]:
        for chunk in self._terminal.output():
            yield None, None, chunk

    def wait(
        self,
        on_pty: Callable[[bytes], None] | None = None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> CommandResult:
        """Wait for the shell to exit, passing its output to ``on_pty``."""
        for _, _, chunk in self:
            if on_pty is not None:
                on_pty(chunk)
        return _checked(self._terminal.wait())

    def kill(self) -> bool:
        return self._terminal.kill()

    def disconnect(self) -> None:
        self._terminal.detach()


class Commands(_Layer):
    _compat_path = "sandbox.commands"

    def __init__(self, sandbox: CompatSandbox) -> None:
        self._sandbox = sandbox

    def run(
        self,
        cmd: str,
        background: bool | None = None,
        envs: dict[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        stdin: bool | None = None,
        timeout: float | None = DEFAULT_COMMAND_TIMEOUT,
        request_timeout: float | None = None,
        *,
        retain_output: bool = True,
        shell: Any = None,
    ) -> CommandResult | CommandHandle:
        """Run ``cmd`` in bash as root. Without ``background``, ``on_stdout`` and
        ``on_stderr`` receive the output once the command finishes."""
        _check_options("commands.run", {"shell": shell}, user)
        if background or stdin:
            process = self._sandbox.start(cmd, cwd=cwd or HOME, envs=envs, timeout=timeout, stdin=bool(stdin))
            handle = CommandHandle(process, on_stdout, on_stderr)
            return handle if background else handle.wait()
        result = self._sandbox.run(cmd, cwd=cwd or HOME, envs=envs, timeout=timeout)
        if on_stdout and result.stdout:
            on_stdout(result.stdout)
        if on_stderr and result.stderr:
            on_stderr(result.stderr)
        return _checked(result)

    def list(self, request_timeout: float | None = None) -> list[ProcessInfo]:
        """Commands and terminals started through this layer that are still running."""
        return [
            ProcessInfo(
                pid=info.pid,
                tag=None,
                cmd="/bin/bash",
                args=["-i", "-l"] if info.pty else ["-l", "-c", info.cmd],
                envs=info.envs,
                cwd=info.cwd,
            )
            for info in self._sandbox.processes()
        ]

    def kill(self, pid: int, request_timeout: float | None = None) -> bool:
        return self._sandbox.kill_process(pid)

    def connect(
        self,
        pid: int,
        timeout: float | None = 60,
        request_timeout: float | None = None,
        *,
        retain_output: bool = True,
    ) -> CommandHandle:
        process = self._sandbox.attach(pid)
        if process is None:
            raise NotFoundException(f"Process with pid {pid} not found")
        return CommandHandle(process, None, None)

    def send_stdin(self, pid: int, data: str | bytes, request_timeout: float | None = None) -> None:
        self._piped(self._sandbox.send_stdin(self._session(pid), data), pid)

    def stream_input(self, pid: int, data: Iterable[str | bytes], *, timeout: float | None = None) -> None:
        if isinstance(data, (str, bytes)):
            raise InvalidArgumentException("stream_input takes an iterable of chunks, not a single str or bytes.")
        session_id = self._session(pid)
        for chunk in data:
            self._piped(self._sandbox.send_stdin(session_id, chunk), pid)

    def close_stdin(self, pid: int, request_timeout: float | None = None) -> None:
        self._piped(self._sandbox.close_stdin(self._session(pid)), pid)

    @staticmethod
    def _piped(done: bool, pid: int) -> None:
        if not done:
            raise InvalidArgumentException(f"Process {pid} has no open stdin; start it with stdin=True.")

    def _session(self, pid: int) -> str:
        info = self._sandbox.find_process(pid)
        if info is None:
            raise NotFoundException(f"Process with pid {pid} not found")
        return info.session_id


class Pty(_Layer):
    """Interactive terminals, each an Arker PTY session. ``timeout`` is accepted
    but not enforced: a terminal runs until it exits or is killed."""

    _compat_path = "sandbox.pty"

    def __init__(self, sandbox: CompatSandbox) -> None:
        self._sandbox = sandbox
        self._terminals: dict[int, Terminal] = {}

    def create(
        self,
        size: PtySize,
        user: str | None = None,
        cwd: str | None = None,
        envs: dict[str, str] | None = None,
        timeout: float | None = 60,
        request_timeout: float | None = None,
    ) -> PtyHandle:
        _check_options("pty.create", {}, user)
        envs = {"TERM": "xterm-256color", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", **(envs or {})}
        terminal = self._sandbox.open_terminal(cols=size.cols, rows=size.rows, cwd=cwd or HOME, envs=envs)
        self._terminals[terminal.pid] = terminal
        return PtyHandle(terminal)

    def connect(self, pid: int, timeout: float | None = 60, request_timeout: float | None = None) -> PtyHandle:
        return PtyHandle(self._terminal(pid))

    def send_stdin(self, pid: int, data: bytes, request_timeout: float | None = None) -> None:
        self._terminal(pid).send(data)

    def stream_input(self, pid: int, data: Iterable[str | bytes], *, timeout: float | None = None) -> None:
        terminal = self._terminal(pid)
        for chunk in data:
            terminal.send(chunk)

    def resize(self, pid: int, size: PtySize, request_timeout: float | None = None) -> None:
        self._terminal(pid).resize(size.cols, size.rows)

    def kill(self, pid: int, request_timeout: float | None = None) -> bool:
        self._terminals.pop(pid, None)
        return self._sandbox.kill_process(pid)

    def _terminal(self, pid: int) -> Terminal:
        if pid not in self._terminals:
            info = self._sandbox.find_process(pid)
            if info is None or not info.pty:
                raise NotFoundException(f"PTY with pid {pid} not found")
            self._terminals[pid] = self._sandbox.attach_terminal(info)
        return self._terminals[pid]


class WatchHandle(_Layer):
    """Changes under a directory since the last :meth:`get_new_events`, found by
    comparing listings: a rename reads as a remove and a create."""

    _compat_path = "WatchHandle"

    def __init__(self, sandbox: CompatSandbox, path: str, recursive: bool) -> None:
        self._sandbox = sandbox
        self._path = path
        self._recursive = recursive
        self._stopped = False
        _, self._entries = sandbox.changes(path, {}, recursive=recursive)

    def get_new_events(self, request_timeout: float | None = None) -> list[FilesystemEvent]:
        if self._stopped:
            raise SandboxException("The watcher is already stopped")
        changes, self._entries = self._sandbox.changes(self._path, self._entries, recursive=self._recursive)
        return [self._event(change) for change in changes]

    def _event(self, change: Change) -> FilesystemEvent:
        return FilesystemEvent(posixpath.relpath(change.path, self._path), FilesystemEventType(change.kind))

    def stop(self, request_timeout: float | None = None) -> None:
        self._stopped = True


class Files(_Layer):
    _compat_path = "sandbox.files"

    def __init__(self, sandbox: CompatSandbox) -> None:
        self._sandbox = sandbox

    def read(
        self, path: str, format: str = "text", user: str | None = None, **options: Any
    ) -> str | bytearray | Iterable[bytes]:
        _check_options("files.read", options, user)
        data = self._sandbox.read(_absolute(path))
        if format == "bytes":
            return bytearray(data)
        if format == "stream":
            return iter([data])
        return data.decode("utf-8", "replace")

    def write(self, path: str, data: Any, user: str | None = None, **options: Any) -> WriteInfo:
        _check_options("files.write", options, user)
        full = _absolute(path)
        self._sandbox.write(full, _to_bytes(data))
        return WriteInfo(name=posixpath.basename(full), type=FileType.FILE, path=full)

    def write_files(self, files: Iterable[Any], user: str | None = None, **options: Any) -> list[WriteInfo]:
        _check_options("files.write_files", options, user)

        def field(entry: Any, name: str) -> Any:
            return entry[name] if isinstance(entry, dict) else getattr(entry, name)

        return [self.write(field(entry, "path"), field(entry, "data")) for entry in files]

    def list(self, path: str, depth: int | None = 1, user: str | None = None, **options: Any) -> list[EntryInfo]:
        _check_options("files.list", options, user)
        return [_entry_info(entry) for entry in self._sandbox.list(_absolute(path), depth or 1)]

    def exists(self, path: str, user: str | None = None, **options: Any) -> bool:
        _check_options("files.exists", options, user)
        return self._sandbox.path_exists(_absolute(path))

    def get_info(self, path: str, user: str | None = None, **options: Any) -> EntryInfo:
        _check_options("files.get_info", options, user)
        return _entry_info(self._sandbox.stat(_absolute(path)))

    def make_dir(self, path: str, user: str | None = None, **options: Any) -> bool:
        _check_options("files.make_dir", options, user)
        return self._sandbox.make_dir(_absolute(path))

    def remove(self, path: str, user: str | None = None, **options: Any) -> None:
        _check_options("files.remove", options, user)
        self._sandbox.remove(_absolute(path))

    def rename(self, old_path: str, new_path: str, user: str | None = None, **options: Any) -> EntryInfo:
        _check_options("files.rename", options, user)
        full = _absolute(new_path)
        self._sandbox.rename(_absolute(old_path), full)
        return _entry_info(self._sandbox.stat(full))

    def watch_dir(
        self,
        path: str,
        user: str | None = None,
        request_timeout: float | None = None,
        recursive: bool = False,
        include_entry: bool = False,
        allow_network_mounts: bool = False,
    ) -> WatchHandle:
        """Watch by polling: each ``get_new_events`` compares a fresh listing with the last."""
        _check_options("files.watch_dir", {"include_entry": include_entry or None}, user)
        full = _absolute(path)
        if not self._sandbox.is_dir(full):
            raise NotFoundException(f"Directory not found: {full}")
        return WatchHandle(self._sandbox, full, recursive)


def _by_id_variant(method: Callable[..., Any]) -> Callable[..., Any]:
    """``method`` called on the class with the sandbox's id in place of the sandbox."""

    def on_class(
        cls: type[Sandbox], sandbox_id: str, *args: Any, arker: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return method(cls._by_id(sandbox_id, arker), *args, **kwargs)

    return on_class


class GitAuthException(AuthenticationException):
    pass


class GitUpstreamException(SandboxException):
    pass


GitResetMode = Literal["soft", "mixed", "hard", "merge", "keep"]

# Words in git's output that mean it was refused credentials, or has no upstream to use.
_GIT_AUTH_FAILURES = (
    "authentication failed",
    "terminal prompts disabled",
    "could not read username",
    "invalid username or password",
    "access denied",
    "permission denied",
    "not authorized",
)
_GIT_NO_UPSTREAM = ("no upstream", "no tracking information", "please specify which branch")


def _git_failure(result: CommandResult) -> Exception:
    output = f"{result.stderr}\n{result.stdout}".lower()
    if any(snippet in output for snippet in _GIT_AUTH_FAILURES):
        return GitAuthException(result.stderr.strip() or "Git authentication failed.")
    if any(snippet in output for snippet in _GIT_NO_UPSTREAM):
        return GitUpstreamException(result.stderr.strip())
    return CommandExitException(result)


@dataclasses.dataclass
class GitFileStatus:
    name: str
    status: str
    index_status: str
    working_tree_status: str
    staged: bool
    renamed_from: str | None = None


# A porcelain status letter's name; the first that applies to either column wins.
_GIT_FILE_STATES = (
    ("U", "conflict"),
    ("R", "renamed"),
    ("C", "copied"),
    ("D", "deleted"),
    ("A", "added"),
    ("M", "modified"),
    ("T", "typechange"),
    ("?", "untracked"),
)


def _file_status(change: GitFileChange) -> GitFileStatus:
    letters = {change.index_status, change.worktree_status}
    status = next((name for letter, name in _GIT_FILE_STATES if letter in letters), "unknown")
    return GitFileStatus(
        name=change.name,
        status=status,
        index_status=change.index_status,
        working_tree_status=change.worktree_status,
        staged=change.index_status not in (" ", "?"),
        renamed_from=change.renamed_from,
    )


@dataclasses.dataclass
class GitStatus:
    current_branch: str | None
    upstream: str | None
    ahead: int
    behind: int
    detached: bool
    file_status: list[GitFileStatus]

    @property
    def is_clean(self) -> bool:
        return not self.file_status

    @property
    def has_changes(self) -> bool:
        return bool(self.file_status)

    @property
    def has_staged(self) -> bool:
        return any(item.staged for item in self.file_status)

    @property
    def has_untracked(self) -> bool:
        return any(item.status == "untracked" for item in self.file_status)

    @property
    def has_conflicts(self) -> bool:
        return any(item.status == "conflict" for item in self.file_status)

    @property
    def total_count(self) -> int:
        return len(self.file_status)

    @property
    def staged_count(self) -> int:
        return sum(item.staged for item in self.file_status)

    @property
    def unstaged_count(self) -> int:
        return self.total_count - self.staged_count

    @property
    def untracked_count(self) -> int:
        return sum(item.status == "untracked" for item in self.file_status)

    @property
    def conflict_count(self) -> int:
        return sum(item.status == "conflict" for item in self.file_status)


@dataclasses.dataclass
class GitBranches:
    branches: list[str]
    current_branch: str | None


def _git_options(method: str, options: dict[str, Any]) -> dict[str, Any]:
    """E2B's per-call options: ``envs``, ``cwd`` and ``timeout`` apply; ``user`` and ``request_timeout`` do not."""
    reject_options(ERRORS, f"git.{method}", options, ignored={"envs", "cwd", "timeout", "user", "request_timeout"})
    return {name: options[name] for name in ("envs", "cwd", "timeout") if options.get(name) is not None}


class Git(_Layer):
    """Git in the sandbox, run as the git CLI. Relative paths are under /home/user."""

    _compat_path = "sandbox.git"

    def __init__(self, sandbox: CompatSandbox) -> None:
        self._git = CompatGit(sandbox, cwd=HOME, fail=_git_failure)

    def clone(
        self,
        url: str,
        path: str | None = None,
        branch: str | None = None,
        depth: int | None = None,
        username: str | None = None,
        password: str | None = None,
        dangerously_store_credentials: bool = False,
        **opts: Any,
    ) -> CommandResult:
        if username and not password:
            raise InvalidArgumentException("A password or token is required with a username.")
        result = self._git.clone(
            url, path, branch=branch, depth=depth, username=username, password=password, **_git_options("clone", opts)
        )
        if dangerously_store_credentials and username:
            host = urllib.parse.urlparse(url)
            self.dangerously_authenticate(username, password, host=host.netloc, protocol=host.scheme, **opts)
        return result

    def init(self, path: str, bare: bool = False, initial_branch: str | None = None, **opts: Any) -> CommandResult:
        return self._git.init(path, bare=bare, initial_branch=initial_branch, **_git_options("init", opts))

    def remote_add(
        self, path: str, name: str, url: str, fetch: bool = False, overwrite: bool = False, **opts: Any
    ) -> CommandResult:
        return self._git.remote_add(
            path, name, url, fetch=fetch, overwrite=overwrite, **_git_options("remote_add", opts)
        )

    def remote_get(self, path: str, name: str, **opts: Any) -> str | None:
        return self._git.remote_get(path, name, **_git_options("remote_get", opts))

    def status(self, path: str, **opts: Any) -> GitStatus:
        state = self._git.status(path, **_git_options("status", opts))
        return GitStatus(
            current_branch=state.current_branch,
            upstream=state.upstream,
            ahead=state.ahead,
            behind=state.behind,
            detached=state.detached,
            file_status=[_file_status(change) for change in state.files],
        )

    def branches(self, path: str, **opts: Any) -> GitBranches:
        return GitBranches(*self._git.branches(path, **_git_options("branches", opts)))

    def create_branch(self, path: str, branch: str, **opts: Any) -> CommandResult:
        return self._git.create_branch(path, branch, **_git_options("create_branch", opts))

    def checkout_branch(self, path: str, branch: str, **opts: Any) -> CommandResult:
        return self._git.checkout_branch(path, branch, **_git_options("checkout_branch", opts))

    def delete_branch(self, path: str, branch: str, force: bool = False, **opts: Any) -> CommandResult:
        return self._git.delete_branch(path, branch, force=force, **_git_options("delete_branch", opts))

    def add(self, path: str, files: list[str] | None = None, all: bool = True, **opts: Any) -> CommandResult:
        return self._git.add(path, files, **_git_options("add", opts))

    def commit(
        self,
        path: str,
        message: str,
        author_name: str | None = None,
        author_email: str | None = None,
        allow_empty: bool = False,
        **opts: Any,
    ) -> CommandResult:
        return self._git.commit(
            path,
            message,
            author_name=author_name,
            author_email=author_email,
            allow_empty=allow_empty,
            **_git_options("commit", opts),
        )

    def reset(
        self,
        path: str,
        mode: GitResetMode | None = None,
        target: str | None = None,
        paths: list[str] | None = None,
        **opts: Any,
    ) -> CommandResult:
        return self._git.reset(path, mode=mode, target=target, paths=paths, **_git_options("reset", opts))

    def restore(
        self,
        path: str,
        paths: list[str],
        staged: bool | None = None,
        worktree: bool | None = None,
        source: str | None = None,
        **opts: Any,
    ) -> CommandResult:
        options = _git_options("restore", opts)
        return self._git.restore(path, paths, staged=staged, worktree=worktree, source=source, **options)

    def push(
        self,
        path: str,
        remote: str | None = None,
        branch: str | None = None,
        set_upstream: bool = True,
        username: str | None = None,
        password: str | None = None,
        **opts: Any,
    ) -> CommandResult:
        options = _git_options("push", opts)
        upstream = set_upstream and not self._git.status(path, **options).upstream
        credentials = {"username": username, "password": password} if username or password else {}
        return self._git.push(path, remote=remote, branch=branch, set_upstream=upstream, **credentials, **options)

    def pull(
        self,
        path: str,
        remote: str | None = None,
        branch: str | None = None,
        username: str | None = None,
        password: str | None = None,
        **opts: Any,
    ) -> CommandResult:
        credentials = {"username": username, "password": password} if username or password else {}
        return self._git.pull(path, remote=remote, branch=branch, **credentials, **_git_options("pull", opts))

    def set_config(
        self, key: str, value: str, scope: str = "global", path: str | None = None, **opts: Any
    ) -> CommandResult:
        return self._git.set_config(key, value, scope=scope, path=path, **_git_options("set_config", opts))

    def get_config(self, key: str, scope: str = "global", path: str | None = None, **opts: Any) -> str | None:
        return self._git.get_config(key, scope=scope, path=path, **_git_options("get_config", opts))

    def dangerously_authenticate(
        self, username: str, password: str, host: str = "github.com", protocol: str = "https", **opts: Any
    ) -> CommandResult:
        options = _git_options("dangerously_authenticate", opts)
        return self._git.dangerously_authenticate(username, password, host=host, protocol=protocol, **options)

    def configure_user(
        self, name: str, email: str, scope: str = "global", path: str | None = None, **opts: Any
    ) -> CommandResult:
        return self._git.configure_user(name, email, scope=scope, path=path, **_git_options("configure_user", opts))


class _ClassOrInstance:
    """A method E2B lets you call on the class with a sandbox id, or on a sandbox."""

    def __init__(self, on_class: Callable[..., Any], on_instance: Callable[..., Any]) -> None:
        self._on_class = on_class
        self._on_instance = on_instance

    def __get__(self, obj: Any, cls: type) -> Callable[..., Any]:
        return functools.partial(self._on_instance, obj) if obj is not None else functools.partial(self._on_class, cls)


class VolumeException(SandboxException):
    pass


class VolumeNotFoundException(NotFoundException):
    pass


VOLUME_FILES_HINT = "mount the volume with Sandbox.create(volume_mounts={path: volume}) and use sandbox.files."
VOLUME_ERRORS = dataclasses.replace(ERRORS, not_found=VolumeNotFoundException, invalid=VolumeException)


@dataclasses.dataclass(frozen=True)
class VolumeInfo:
    volume_id: str
    name: str


def _volume_files_unsupported(name: str) -> Callable[..., Any]:
    """A Volume method on its files, which on Arker are reached through a sandbox."""

    def method(self: Any, *_: Any, **__: Any) -> Any:
        raise unsupported(ERRORS, f"Volume.{name}", VOLUME_FILES_HINT)

    return method


class Volume(_Layer):
    """A persistent volume, as an Arker filesystem: mount it into sandboxes
    with ``volume_mounts``. Its files are read and written through a sandbox."""

    _compat_path = "Volume"
    _compat_hints: ClassVar[dict[str, str]] = dict.fromkeys(
        ("make_dir", "exists", "update_metadata", "read_file", "write_file", "remove"), VOLUME_FILES_HINT
    )

    def __init__(self, volume_id: str, name: str) -> None:
        self.volume_id = volume_id
        self.name = name
        self.token = None

    @classmethod
    def create(cls, name: str, *, arker: dict[str, Any] | None = None, **_: Any) -> Volume:
        volume = Volumes(VOLUME_ERRORS, arker).create(name)
        return cls(volume.filesystem_id, volume.name)

    @classmethod
    def connect(cls, volume_id: str, *, arker: dict[str, Any] | None = None, **_: Any) -> Volume:
        volume = Volumes(VOLUME_ERRORS, arker).by_id(volume_id)
        return cls(volume.filesystem_id, volume.name)

    def _info_by_id(cls, volume_id: str, *, arker: dict[str, Any] | None = None, **_: Any) -> VolumeInfo:
        volume = Volumes(VOLUME_ERRORS, arker).by_id(volume_id)
        return VolumeInfo(volume.filesystem_id, volume.name)

    def _list_volumes(cls, *, arker: dict[str, Any] | None = None, **_: Any) -> list[VolumeInfo]:
        return [VolumeInfo(volume.filesystem_id, volume.name) for volume in Volumes(VOLUME_ERRORS, arker).list()]

    get_info = _ClassOrInstance(_info_by_id, _volume_files_unsupported("get_info"))
    list = _ClassOrInstance(_list_volumes, _volume_files_unsupported("list"))

    @classmethod
    def destroy(cls, volume_id: str, *, arker: dict[str, Any] | None = None, **_: Any) -> bool:
        return Volumes(VOLUME_ERRORS, arker).delete(volume_id)


def _volume_ids(volume_mounts: dict[str, Any] | None, arker: dict[str, Any] | None) -> dict[str, str] | None:
    """Mount path to volume id, for volumes given by object or by name."""
    if not volume_mounts:
        return None
    volumes = Volumes(VOLUME_ERRORS, arker)
    return {
        path: volume.volume_id if isinstance(volume, Volume) else volumes.get(volume).filesystem_id
        for path, volume in volume_mounts.items()
    }


class Sandbox(_Layer):
    """An E2B sandbox running as an Arker VM.

    Like E2B, a sandbox is deleted ``timeout`` seconds (default 300) after it
    is created; extend it with :meth:`set_timeout`. ``lifecycle={"on_timeout":
    "pause"}`` keeps it instead, as does :meth:`pause`.
    """

    _compat_path = "Sandbox"

    def __init__(
        self,
        template: str | None = None,
        timeout: int | None = None,
        metadata: dict[str, str] | None = None,
        envs: dict[str, str] | None = None,
        allow_internet_access: bool | None = None,
        lifecycle: dict[str, Any] | None = None,
        *,
        network: dict[str, Any] | None = None,
        cpu_count: int | None = None,
        volume_mounts: dict[str, Any] | None = None,
        arker: dict[str, Any] | None = None,
        **opts: Any,
    ) -> None:
        reject_options(ERRORS, "Sandbox", opts, ignored=E2B_CONNECTION_OPTIONS)
        self._bind(
            CompatSandbox.create(
                ERRORS,
                config=arker,
                source=None if template in (None, "base") else template,
                envs={"E2B_SANDBOX": "true", **(envs or {})},
                cpus=cpu_count,
                delete_after=timeout or DEFAULT_SANDBOX_TIMEOUT,
                on_timeout="suspend" if (lifecycle or {}).get("on_timeout") == "pause" else "delete",
                egress=_egress(network, allow_internet_access),
                metadata=metadata,
                id_env="E2B_SANDBOX_ID",
                volumes=_volume_ids(volume_mounts, arker),
                port_auth=None if _public_traffic(network) else new_port_auth(TRAFFIC_TOKEN_HEADER),
            )
        )

    def _bind(self, sandbox: CompatSandbox) -> None:
        self._sandbox = sandbox
        self.sandbox_id = sandbox.id
        self.commands = Commands(sandbox)
        self.files = Files(sandbox)
        self.git = Git(sandbox)
        self.pty = Pty(sandbox)

    @classmethod
    def create(cls, template: str | None = None, **opts: Any) -> Sandbox:
        return cls(template, **opts)

    @property
    def arker(self) -> VM:
        """The Arker VM behind this sandbox, for anything this layer does not cover."""
        return self._sandbox.vm

    def _connect_by_id(
        cls,
        sandbox_id: str,
        timeout: int | None = None,
        *,
        on_resume: str = "restore",
        arker: dict[str, Any] | None = None,
        **opts: Any,
    ) -> Any:
        """Reconnect to a sandbox. An id Arker does not know is handed to the
        ``e2b`` package when it is installed, so existing E2B sandboxes keep working."""
        compat = CompatSandbox.connect(ERRORS, sandbox_id, arker)
        if compat is None:
            native = import_optional("e2b")
            if native is None:
                raise SandboxNotFoundException(f"Sandbox {sandbox_id} not found")
            if timeout is not None:
                opts["timeout"] = timeout
            return native.Sandbox.connect(sandbox_id, on_resume=on_resume, **opts)
        sandbox = cls.__new__(cls)
        sandbox._bind(compat)
        return sandbox._connect_self(timeout, on_resume=on_resume)

    def _connect_self(self, timeout: int | None = None, *, on_resume: str = "restore", **_: Any) -> Sandbox:
        """Resume this sandbox; Arker VMs resume on their own when used, memory included."""
        if on_resume != "restore":
            raise unsupported(ERRORS, f"connect(on_resume={on_resume!r})", "Arker resumes a sandbox with its memory.")
        if timeout:
            self.set_timeout(timeout)
        return self

    connect = _ClassOrInstance(_connect_by_id, _connect_self)

    @classmethod
    def _by_id(cls, sandbox_id: str, arker: dict[str, Any] | None) -> Sandbox:
        compat = CompatSandbox.connect(ERRORS, sandbox_id, arker)
        if compat is None:
            raise SandboxNotFoundException(f"Sandbox {sandbox_id} not found")
        sandbox = cls.__new__(cls)
        sandbox._bind(compat)
        return sandbox

    def _kill_by_id(cls, sandbox_id: str, *, arker: dict[str, Any] | None = None, **opts: Any) -> bool:
        compat = CompatSandbox.connect(ERRORS, sandbox_id, arker)
        if compat is not None:
            return compat.delete()
        native = import_optional("e2b")
        return bool(native.Sandbox.kill(sandbox_id, **opts)) if native is not None else False

    def _kill_self(self, **_: Any) -> bool:
        return self._sandbox.delete()

    kill = _ClassOrInstance(_kill_by_id, _kill_self)

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, *_: Any) -> None:
        self.kill()

    def set_timeout(self, timeout: int, **_: Any) -> None:
        self._sandbox.set_timeout(timeout)

    def _pause_self(self, keep_memory: bool | None = None, *, mode: str | None = None, **opts: Any) -> bool:
        """Suspend the sandbox and cancel its timeout. Arker keeps memory as well
        as files, whatever ``mode`` asks. Resume with :meth:`connect`."""
        reject_options(
            ERRORS,
            "pause",
            {
                "keep_memory": keep_memory if keep_memory is False else None,
                "mode": mode if mode != "full" else None,
                **opts,
            },
            ignored=E2B_CONNECTION_OPTIONS | {"strict"},
            inert={"keep_memory", "mode"},
        )
        self._sandbox.clear_timeout()
        self._sandbox.suspend()
        return True

    pause = _ClassOrInstance(_by_id_variant(_pause_self), _pause_self)
    beta_pause = pause

    def is_running(self, **_: Any) -> bool:
        return self._sandbox.is_alive()

    def get_info(self, **_: Any) -> SandboxInfo:
        return _sandbox_info(self._sandbox.info(), self._sandbox.metadata())

    def get_host(self, port: int) -> str:
        """Expose ``port`` and return its host name. Created with
        ``network={"allow_public_traffic": False}``, the sandbox lets in only
        requests carrying :attr:`traffic_access_token` in the
        ``e2b-traffic-access-token`` header; otherwise the port is public, as on E2B."""
        return self._sandbox.expose(port)

    @property
    def traffic_access_token(self) -> str | None:
        """The token requests to the sandbox's ports must carry, or None when they are public."""
        return self._sandbox.port_auth["token"] if self._sandbox.port_auth else None

    def download_url(self, path: str, user: str | None = None, use_signature_expiration: int | None = None) -> str:
        """A URL that downloads ``path`` with no credentials, for ``use_signature_expiration``
        seconds when given, else while the sandbox lives. Forks do not inherit it."""
        _check_options("download_url", {}, user)
        return self._sandbox.file_url(_absolute(path), write=False, expires_in=use_signature_expiration)

    def upload_url(self, path: str, user: str | None = None, use_signature_expiration: int | None = None) -> str:
        """A URL that writes a POSTed multipart/form-data file to ``path`` with no
        credentials, for ``use_signature_expiration`` seconds when given."""
        _check_options("upload_url", {}, user)
        return self._sandbox.file_url(_absolute(path), write=True, expires_in=use_signature_expiration)

    @classmethod
    def list(
        cls,
        query: SandboxQuery | None = None,
        limit: int | None = None,
        next_token: str | None = None,
        order: str | None = None,
        *,
        arker: dict[str, Any] | None = None,
        **opts: Any,
    ) -> SandboxPaginator:
        """The sandboxes this layer created, filtered by ``query``, oldest first
        unless ``order`` is ``"desc"``. A paginator's ``next_token`` continues the
        listing from where its last page ended."""
        reject_options(ERRORS, "Sandbox.list", opts, ignored=E2B_CONNECTION_OPTIONS)
        if order not in (None, "asc", "desc"):
            raise InvalidArgumentException(f"Invalid order {order!r}, expected 'asc' or 'desc'")
        query = query or SandboxQuery()
        descending = order == "desc"
        found = [
            _sandbox_info(sandbox.vm, sandbox.settings)
            for sandbox in CompatSandbox.find(ERRORS, arker)
            if _matches(sandbox, query)
        ]
        found.sort(key=_list_key, reverse=descending)
        if next_token:
            after = _after_token(next_token)
            found = [info for info in found if (_list_key(info) < after if descending else _list_key(info) > after)]
        return SandboxPaginator(found, limit, _list_token)

    def _fork_self(self, timeout: int | None = None, count: int | None = None, **_: Any) -> list[Sandbox | Exception]:
        """New sandboxes from this one's files and memory; a failed fork is an item, not an error."""
        forks: list[Sandbox | Exception] = []
        for _index in range(count or 1):
            try:
                compat = self._sandbox.fork(delete_after=timeout or DEFAULT_SANDBOX_TIMEOUT, id_env="E2B_SANDBOX_ID")
            except SandboxException as error:
                forks.append(error)
                continue
            sandbox = type(self).__new__(type(self))
            sandbox._bind(compat)
            forks.append(sandbox)
        return forks

    fork = _ClassOrInstance(_by_id_variant(_fork_self), _fork_self)

    def _create_snapshot_self(self, name: str | None = None, *, mode: str | None = None, **opts: Any) -> SnapshotInfo:
        """Snapshot the sandbox's files and memory. Start a sandbox from it with
        ``Sandbox.create(snapshot.snapshot_id)``."""
        reject_options(
            ERRORS, "create_snapshot", {"mode": mode, **opts}, ignored=E2B_CONNECTION_OPTIONS, inert={"mode"}
        )
        snapshot = self._sandbox.snapshot(name or f"e2b-snapshot-{uuid.uuid4().hex[:12]}")
        return _snapshot_info(snapshot)

    create_snapshot = _ClassOrInstance(_by_id_variant(_create_snapshot_self), _create_snapshot_self)

    def _list_snapshots_by_id(
        cls,
        sandbox_id: str | None = None,
        limit: int | None = None,
        next_token: str | None = None,
        name: str | None = None,
        *,
        arker: dict[str, Any] | None = None,
        **opts: Any,
    ) -> SnapshotPaginator:
        reject_options(ERRORS, "list_snapshots", {"next_token": next_token, **opts}, ignored=E2B_CONNECTION_OPTIONS)
        snapshots = (
            _snapshot_info(snapshot)
            for snapshot in CompatSandbox.find(ERRORS, arker, snapshots=True)
            if sandbox_id in (None, snapshot.snapshot_of) and name in (None, snapshot.vm.name)
        )
        return SnapshotPaginator(snapshots, limit)

    def _list_snapshots_self(
        self, limit: int | None = None, next_token: str | None = None, name: str | None = None, **opts: Any
    ) -> SnapshotPaginator:
        return type(self)._list_snapshots_by_id(self.sandbox_id, limit, next_token, name, **opts)

    list_snapshots = _ClassOrInstance(_list_snapshots_by_id, _list_snapshots_self)

    @classmethod
    def delete_snapshot(cls, snapshot_id: str, *, arker: dict[str, Any] | None = None, **_: Any) -> bool:
        """Delete a snapshot; False when there is no such snapshot."""
        snapshot = CompatSandbox.connect(ERRORS, snapshot_id, arker)
        return snapshot is not None and snapshot.snapshot_of is not None and snapshot.delete()

    def _update_network_self(self, network: dict[str, Any], **_: Any) -> None:
        """Replace the sandbox's outbound rules; what ``network`` leaves out is cleared."""
        public = network.get("allow_public_traffic")
        if public is not None and public != (self._sandbox.port_auth is None):
            raise unsupported(
                ERRORS, "update_network(allow_public_traffic=...)", "set it with Sandbox.create(network=...)."
            )
        self._sandbox.update_egress(_egress(network, None))

    update_network = _ClassOrInstance(_by_id_variant(_update_network_self), _update_network_self)


def _snapshot_info(snapshot: CompatSandbox) -> SnapshotInfo:
    return SnapshotInfo(snapshot_id=snapshot.vm.name, names=[snapshot.vm.name])


# The async API: ``AsyncSandbox`` and what it hands out are the sync classes
# above, awaited in worker threads; only what E2B's async API does differently
# is written out here.


class _AsyncLayer(AsyncWrapper):
    _compat_errors = ERRORS


class AsyncCommandHandle(_AsyncLayer):
    """A running command or terminal. A background task reads its output as it
    arrives, passes it to the callbacks and keeps it for ``stdout`` and ``stderr``."""

    _compat_path = "AsyncCommandHandle"

    def __init__(
        self,
        handle: CommandHandle | PtyHandle,
        on_stdout: Callable[[str], Any] | None = None,
        on_stderr: Callable[[str], Any] | None = None,
        on_pty: Callable[[bytes], Any] | None = None,
    ) -> None:
        super().__init__(handle)
        self._output: tuple[list[str], list[str]] = ([], [])
        self._outcome: CommandResult | CommandExitException | None = None
        self._error: Exception | None = None
        self._task = asyncio.create_task(self._follow(on_stdout, on_stderr, on_pty))

    @classmethod
    async def _started(cls, handle: CommandHandle | PtyHandle, **callbacks: Any) -> AsyncCommandHandle:
        # Known before the handle is returned, so reading ``pid`` never blocks.
        await call(getattr, handle, "pid")
        return cls(handle, **callbacks)

    async def _follow(
        self,
        on_stdout: Callable[[str], Any] | None,
        on_stderr: Callable[[str], Any] | None,
        on_pty: Callable[[bytes], Any] | None,
    ) -> None:
        try:
            async for stdout, stderr, pty in iterate(iter(self._sync)):
                for output, callback, text in zip(self._output, (on_stdout, on_stderr), (stdout, stderr), strict=True):
                    if text:
                        output.append(text)
                        await deliver(callback, text)
                if pty:
                    await deliver(on_pty, pty)
            self._outcome = await call(self._sync.wait)
        except CommandExitException as error:
            self._outcome = self._error = error
        except Exception as error:
            self._error = error

    @property
    def pid(self) -> int:
        return self._sync.pid

    @property
    def stdout(self) -> str:
        return "".join(self._output[0])

    @property
    def stderr(self) -> str:
        return "".join(self._output[1])

    @property
    def exit_code(self) -> int | None:
        """None while the command runs."""
        return self._outcome.exit_code if self._outcome is not None else None

    @property
    def error(self) -> str | None:
        return self._outcome.error if self._outcome is not None else None

    async def wait(self) -> CommandResult:
        """The result once the command ends; a non-zero exit raises ``CommandExitException``."""
        await asyncio.wait([self._task])
        if self._error is not None:
            raise self._error
        if not isinstance(self._outcome, CommandResult):
            raise SandboxException("Disconnected before the command ended; reconnect with commands.connect.")
        return self._outcome

    async def disconnect(self) -> None:
        """Stop reading output; the command keeps running."""
        self._task.cancel()
        await asyncio.wait([self._task])
        await call(self._sync.disconnect)


class AsyncCommandInputHandle:
    """Input for a command or terminal: each ``send`` is written, in order, before the next."""

    def __init__(self, send: Callable[[str | bytes], None]) -> None:
        self._send = send
        self._lock = asyncio.Lock()
        self._closed = False

    async def send(self, data: str | bytes) -> None:
        if self._closed:
            raise SandboxException(
                "Cannot send input after the input handle is closed. Open a new one "
                "with `sandbox.commands.stream_input(pid)` or `sandbox.pty.stream_input(pid)`."
            )
        async with self._lock:
            await call(self._send, data)

    async def close(self) -> None:
        """Stop taking input once what was sent is written. Stdin stays open; ``close_stdin`` closes it."""
        self._closed = True
        async with self._lock:
            pass

    async def __aenter__(self) -> AsyncCommandInputHandle:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()


class AsyncCommands(_AsyncLayer, wraps=Commands):
    async def run(self, cmd: str, background: bool | None = None, *args: Any, **kwargs: Any) -> Any:
        """Like :meth:`Commands.run`; callbacks may be coroutines. In the background,
        the returned handle delivers output to them as it arrives."""
        arguments = inspect.signature(self._sync.run).bind(cmd, background, *args, **kwargs).arguments
        if not background:
            return await call(self._sync.run, **arguments)
        callbacks = {"on_stdout": arguments.pop("on_stdout", None), "on_stderr": arguments.pop("on_stderr", None)}
        return await AsyncCommandHandle._started(await call(self._sync.run, **arguments), **callbacks)

    async def connect(
        self,
        pid: int,
        timeout: float | None = 60,
        request_timeout: float | None = None,
        on_stdout: Callable[[str], Any] | None = None,
        on_stderr: Callable[[str], Any] | None = None,
        *,
        retain_output: bool = True,
    ) -> AsyncCommandHandle:
        handle = await call(self._sync.connect, pid, timeout, request_timeout, retain_output=retain_output)
        return await AsyncCommandHandle._started(handle, on_stdout=on_stdout, on_stderr=on_stderr)

    def stream_input(self, pid: int, *, timeout: float | None = None) -> AsyncCommandInputHandle:
        return AsyncCommandInputHandle(functools.partial(self._sync.send_stdin, pid))


class AsyncPty(_AsyncLayer, wraps=Pty):
    async def create(self, size: PtySize, on_data: Callable[[bytes], Any], *args: Any, **kwargs: Any) -> Any:
        return await AsyncCommandHandle._started(await call(self._sync.create, size, *args, **kwargs), on_pty=on_data)

    async def connect(self, pid: int, on_data: Callable[[bytes], Any], *args: Any, **kwargs: Any) -> Any:
        return await AsyncCommandHandle._started(await call(self._sync.connect, pid, *args, **kwargs), on_pty=on_data)

    def stream_input(self, pid: int, *, timeout: float | None = None) -> AsyncCommandInputHandle:
        return AsyncCommandInputHandle(functools.partial(self._sync.send_stdin, pid))


class AsyncWatchHandle(_AsyncLayer):
    """A directory watch: a background task polls as :class:`WatchHandle` does
    and passes each event to ``on_event``, and ``on_exit`` when it ends."""

    _compat_path = "AsyncWatchHandle"

    def __init__(
        self,
        handle: WatchHandle,
        on_event: Callable[[FilesystemEvent], Any],
        on_exit: Callable[[Exception | None], Any] | None,
        timeout: float | None,
    ) -> None:
        super().__init__(handle)
        self._task = asyncio.create_task(self._watch(on_event, on_exit, timeout))

    async def _watch(
        self,
        on_event: Callable[[FilesystemEvent], Any],
        on_exit: Callable[[Exception | None], Any] | None,
        timeout: float | None,
    ) -> None:
        error: Exception | None = None
        try:
            await asyncio.wait_for(self._poll(on_event), timeout or None)
        except asyncio.TimeoutError:
            error = TimeoutException(f"Watching stopped after its {timeout}s timeout.")
        except asyncio.CancelledError:
            await self._exited(on_exit, None)
            raise
        except Exception as failure:
            error = failure
        await self._exited(on_exit, error)

    async def _poll(self, on_event: Callable[[FilesystemEvent], Any]) -> None:
        while True:
            for event in await call(self._sync.get_new_events):
                await deliver(on_event, event)
            await asyncio.sleep(POLL_INTERVAL)

    @staticmethod
    async def _exited(on_exit: Callable[[Exception | None], Any] | None, error: Exception | None) -> None:
        # Nothing awaits this task, so an error from on_exit has nowhere to go.
        with contextlib.suppress(Exception):
            await deliver(on_exit, error)

    async def stop(self) -> None:
        self._task.cancel()
        await asyncio.wait([self._task])
        self._sync.stop()


class AsyncFiles(_AsyncLayer, wraps=Files):
    async def watch_dir(
        self,
        path: str,
        on_event: Callable[[FilesystemEvent], Any],
        on_exit: Callable[[Exception | None], Any] | None = None,
        user: str | None = None,
        request_timeout: float | None = None,
        timeout: float | None = 60,
        recursive: bool = False,
        include_entry: bool = False,
        allow_network_mounts: bool = False,
    ) -> AsyncWatchHandle:
        """Watch by polling, passing each change to ``on_event``; ``timeout`` 0 watches until stopped."""
        handle = await call(
            self._sync.watch_dir,
            path,
            user,
            request_timeout,
            recursive=recursive,
            include_entry=include_entry,
            allow_network_mounts=allow_network_mounts,
        )
        return AsyncWatchHandle(handle, on_event, on_exit, timeout)


class AsyncGit(_AsyncLayer, wraps=Git):
    pass


class _AsyncPaginator(Generic[T]):
    """``await next_items()`` while ``has_next``; the listing is fetched by the first call."""

    def __init__(self, listing: Callable[[], _Paginator[T]]) -> None:
        self._listing = listing
        self._pages: _Paginator[T] | None = None

    @property
    def has_next(self) -> bool:
        return self._pages is None or self._pages.has_next

    @property
    def next_token(self) -> str | None:
        return self._pages.next_token if self._pages else None

    async def next_items(self, **_: Any) -> list[T]:
        if self._pages is None:
            self._pages = await call(self._listing)
        return self._pages.next_items()


class AsyncSandboxPaginator(_AsyncPaginator[SandboxInfo]):
    pass


class AsyncSnapshotPaginator(_AsyncPaginator[SnapshotInfo]):
    pass


class AsyncVolume(_AsyncLayer, wraps=Volume):
    _compat_path = "AsyncVolume"


class AsyncSandbox(_AsyncLayer, wraps=Sandbox):
    """:class:`Sandbox` with coroutine methods, as ``e2b.AsyncSandbox``:
    ``sandbox = await AsyncSandbox.create()``."""

    _compat_path = "AsyncSandbox"

    async def _connect_by_id(
        cls, sandbox_id: str, timeout: int | None = None, *, arker: dict[str, Any] | None = None, **opts: Any
    ) -> Any:
        """Like :meth:`Sandbox.connect`, handing ids Arker does not know to ``e2b.AsyncSandbox``."""
        native = import_optional("e2b")
        if native is not None and await call(CompatSandbox.connect, ERRORS, sandbox_id, arker) is None:
            return await native.AsyncSandbox.connect(sandbox_id, **({"timeout": timeout} if timeout else {}), **opts)
        return await asynchronous(cls._sync_type.connect)(sandbox_id, timeout, arker=arker, **opts)

    async def _connect_self(self, *args: Any, **kwargs: Any) -> AsyncSandbox:
        await call(self._sync.connect, *args, **kwargs)
        return self

    connect = _ClassOrInstance(_connect_by_id, _connect_self)

    @classmethod
    def list(cls, *args: Any, **kwargs: Any) -> AsyncSandboxPaginator:
        return AsyncSandboxPaginator(functools.partial(cls._sync_type.list, *args, **kwargs))

    def _list_snapshots_by_id(cls, *args: Any, **kwargs: Any) -> AsyncSnapshotPaginator:
        return AsyncSnapshotPaginator(functools.partial(cls._sync_type.list_snapshots, *args, **kwargs))

    def _list_snapshots_self(self, *args: Any, **kwargs: Any) -> AsyncSnapshotPaginator:
        return AsyncSnapshotPaginator(functools.partial(self._sync.list_snapshots, *args, **kwargs))

    list_snapshots = _ClassOrInstance(_list_snapshots_by_id, _list_snapshots_self)

    def get_host(self, port: int) -> str:
        """Not a coroutine, as in E2B: it opens the port, so it waits for that."""
        return self._sync.get_host(port)

    async def __aenter__(self) -> AsyncSandbox:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.kill()
