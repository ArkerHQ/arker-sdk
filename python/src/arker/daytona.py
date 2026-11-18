"""Daytona-compatible sandboxes backed by Arker VMs.

Replace ``from daytona import ...`` with ``from arker.daytona import ...``.
What this layer cannot honor raises an exception rather than doing something else.
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
import shlex
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

from ._aio import AsyncWrapper, asynchronous, call, deliver, iterate
from ._compat import (
    CodeContexts,
    CommandResult,
    CompatGit,
    CompatSandbox,
    Egress,
    Entry,
    Errors,
    GitFileChange,
    Terminal,
    UnsupportedMixin,
    Volumes,
    blocks_all_egress,
    import_optional,
    missing_export,
    parse_time,
    reject_options,
)
from ._compat import Process as CompatProcess
from .computer import VM, Filesystem

CODE_DIR = "/tmp/arker-daytona-code"
# How long Daytona's signed URLs work unless told otherwise, in seconds.
DEFAULT_FILE_URL_TTL = 3600
DEFAULT_PREVIEW_URL_TTL = 60

# File extension and runner per Daytona language.
LANGUAGES = {
    "python": ("py", "python3"),
    "javascript": ("js", "node"),
    "typescript": ("ts", "node --no-warnings --experimental-strip-types"),
}

# Accepted with a warning: Arker suspends idle VMs, runs as root, and places VMs itself.
INERT_PARAMS = {
    "auto_archive_interval",
    "auto_delete_interval",
    "auto_stop_interval",
    "ephemeral",
    "os_user",
    "target",
    "timeout",
}


class DaytonaError(Exception):
    pass


class DaytonaValidationError(DaytonaError):
    pass


class DaytonaNotFoundError(DaytonaError):
    pass


class DaytonaTimeoutError(DaytonaError):
    pass


class DaytonaNotSupportedError(DaytonaValidationError, AttributeError):
    pass


ERRORS = Errors(
    provider="Daytona",
    unsupported=DaytonaNotSupportedError,
    invalid=DaytonaValidationError,
    not_found=DaytonaNotFoundError,
    file_not_found=DaytonaNotFoundError,
    timeout=DaytonaTimeoutError,
    failed=DaytonaError,
)


class _Layer(UnsupportedMixin):
    _compat_errors = ERRORS


def __getattr__(name: str) -> Any:
    return missing_export(ERRORS, __name__, name)


@dataclasses.dataclass
class DaytonaConfig:
    """Daytona credentials; used only to reach sandboxes that still live on Daytona."""

    api_key: str | None = None
    api_url: str | None = None
    target: str | None = None


@dataclasses.dataclass
class Resources:
    cpu: int | None = None
    memory: int | None = None  # GiB
    disk: int | None = None
    gpu: int | None = None


class _Params:
    """Daytona's create parameters; any keyword is kept and checked on create."""

    def __init__(self, **params: Any) -> None:
        self.__dict__.update(params)


class CreateSandboxFromSnapshotParams(_Params):
    pass


class CreateSandboxFromImageParams(_Params):
    pass


class ListSandboxesQuery(_Params):
    """Filters for ``Daytona.list``: ``id`` and ``name`` prefixes, ``labels``,
    ``states``, ``snapshots``, ``is_public``, CPU, memory and disk bounds,
    creation and last activity times, and ``sort`` and ``order``."""


class CreateSnapshotParams(_Params):
    """``name``, ``image`` (a registry image name) and ``resources``."""


@dataclasses.dataclass
class CodeRunParams:
    argv: list[str] | None = None
    env: dict[str, str] | None = None


@dataclasses.dataclass(frozen=True)
class ExecuteResponse:
    exit_code: int
    result: str

    @property
    def artifacts(self) -> None:
        return None


@dataclasses.dataclass(frozen=True)
class FileInfo:
    name: str
    is_dir: bool
    size: int
    mode: str = ""
    permissions: str = ""
    owner: str = ""
    group: str = ""
    mod_time: str = ""
    modified_at: str | None = None
    path: str | None = None


@dataclasses.dataclass(frozen=True)
class Match:
    file: str
    line: int
    content: str


@dataclasses.dataclass(frozen=True)
class SearchFilesResponse:
    files: list[str]


@dataclasses.dataclass(frozen=True)
class ReplaceResult:
    file: str | None = None
    success: bool | None = None
    error: str | None = None


@dataclasses.dataclass
class FileUpload:
    source: bytes | str
    destination: str


@dataclasses.dataclass
class FileDownloadRequest:
    source: str
    destination: str | None = None


@dataclasses.dataclass
class FileDownloadResponse:
    source: str
    result: str | bytes | None = None
    error: str | None = None


@dataclasses.dataclass
class SessionExecuteRequest:
    command: str
    run_async: bool | None = None
    var_async: bool | None = None
    suppress_input_echo: bool | None = None


@dataclasses.dataclass(frozen=True)
class SessionExecuteResponse:
    cmd_id: str
    output: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None


@dataclasses.dataclass(frozen=True)
class SessionCommandLogsResponse:
    output: str | None = None
    stdout: str | None = None
    stderr: str | None = None


@dataclasses.dataclass(frozen=True)
class Command:
    id: str
    command: str
    exit_code: int | None = None


@dataclasses.dataclass(frozen=True)
class Session:
    session_id: str
    commands: list[Command]


@dataclasses.dataclass(frozen=True)
class PtySize:
    rows: int
    cols: int


@dataclasses.dataclass(frozen=True)
class PtyResult:
    exit_code: int | None
    error: str | None = None


@dataclasses.dataclass(frozen=True)
class PtySessionInfo:
    id: str
    cwd: str
    envs: dict[str, str]
    cols: int
    rows: int
    active: bool = True
    lazy_start: bool = False


@dataclasses.dataclass(frozen=True)
class SignedPortPreviewUrl:
    sandbox_id: str
    port: int
    token: str
    url: str


class PtyHandle(_Layer):
    """A terminal from ``process.create_pty_session``; iterating yields its output bytes."""

    _compat_path = "PtyHandle"

    def __init__(self, terminal: Terminal, session_id: str, process: Process) -> None:
        self._terminal = terminal
        self._process = process
        self.session_id = session_id
        self.exit_code: int | None = None
        self.error: str | None = None

    @property
    def is_connected(self) -> bool:
        return self.exit_code is None and self.error is None

    def wait_for_connection(self, timeout: float = 10.0) -> None:
        """The connection is open once the handle exists."""

    def send_input(self, data: str | bytes) -> None:
        self._terminal.send(data)

    def resize(self, pty_size: PtySize) -> PtySessionInfo:
        return self._process.resize_pty_session(self.session_id, pty_size)

    def kill(self) -> None:
        self._process.kill_pty_session(self.session_id)

    def __iter__(self) -> Iterator[bytes]:
        return self._terminal.output()

    def wait(self, on_data: Callable[[bytes], None] | None = None, timeout: float | None = None) -> PtyResult:
        for data in self._terminal.output():
            if on_data is not None:
                on_data(data)
        try:
            self.exit_code = self._terminal.wait().exit_code
        except DaytonaError as error:
            self.error = str(error)
        return PtyResult(exit_code=self.exit_code, error=self.error)

    def disconnect(self) -> None:
        self._terminal.detach()


@dataclasses.dataclass(frozen=True)
class PortPreviewUrl:
    url: str
    token: str


def _fields(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    return dict(vars(value))


def _egress(block_all: bool | None, allow_list: str | None, domain_allow_list: str | None = None) -> Egress | None:
    """Daytona's network settings: an allow list denies what it does not name."""
    if block_all:
        return Egress(deny_all=True)
    if allow_list is None and domain_allow_list is None:
        return None
    ips = [cidr.strip() for cidr in (allow_list or "").split(",") if cidr.strip()]
    hosts = [host.strip() for host in (domain_allow_list or "").split(",") if host.strip()]
    return Egress(allow_ips=tuple(ips), allow_hosts=tuple(hosts), deny_all=True)


def _response(result: CommandResult) -> ExecuteResponse:
    return ExecuteResponse(exit_code=result.exit_code, result=result.stdout)


def _file_info(entry: Entry) -> FileInfo:
    kind = "l" if entry.symlink_target else "d" if entry.is_dir else "-"
    modified = datetime.fromtimestamp(entry.modified, timezone.utc)
    return FileInfo(
        name=entry.name,
        is_dir=entry.is_dir,
        size=entry.size,
        mode=kind + entry.permissions,
        permissions=f"{entry.mode:04o}",
        owner=entry.owner,
        group=entry.group,
        mod_time=modified.strftime("%Y-%m-%d %H:%M:%S.%f +0000 UTC"),
        modified_at=modified.isoformat(),
        path=entry.path,
    )


def _read_source(source: Any) -> bytes:
    """Bytes, a local file path, or a readable stream."""
    if isinstance(source, str):
        with open(source, "rb") as handle:
            return handle.read()
    if hasattr(source, "read"):
        return source.read()
    return bytes(source)


class Process(_Layer):
    _compat_path = "sandbox.process"

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    def exec(
        self, command: str, cwd: str | None = None, env: dict[str, str] | None = None, timeout: int | None = None
    ) -> ExecuteResponse:
        """Run ``command``; ``result`` holds stdout and stderr together, as on Daytona."""
        return _response(self._sandbox._compat.run(f"(\n{command}\n) 2>&1", cwd=cwd, envs=env, timeout=timeout))

    def code_run(self, code: str, params: CodeRunParams | None = None, timeout: int | None = None) -> ExecuteResponse:
        """Run ``code`` with the sandbox's language. Charts are not captured."""
        extension, runner = LANGUAGES[self._sandbox._language]
        path = f"{CODE_DIR}/{uuid.uuid4().hex}.{extension}"
        self._sandbox._compat.write(path, code)
        options = _fields(params)
        argv = shlex.join(str(arg) for arg in options.get("argv") or [])
        return self.exec(
            f"{runner} {path} {argv}; status=$?; rm -f {path}; exit $status", env=options.get("env"), timeout=timeout
        )

    def create_session(self, session_id: str, request_timeout: float | None = None) -> None:
        """A shell that keeps its directory and variables from one command to the next."""
        self._sandbox._compat.create_named_session(session_id)

    def _session(self, session_id: str) -> str:
        arker_session = self._sandbox._compat.named_sessions().get(session_id)
        if arker_session is None:
            raise DaytonaNotFoundError(f"Session {session_id} not found")
        return arker_session

    def get_session(self, session_id: str, request_timeout: float | None = None) -> Session:
        return self._described(session_id, self._session(session_id))

    def _described(self, session_id: str, arker_session: str) -> Session:
        commands = self._sandbox._compat.session_commands(arker_session)
        return Session(session_id, [Command(run_id, command, exit_code) for run_id, command, exit_code in commands])

    def list_sessions(self, request_timeout: float | None = None) -> list[Session]:
        return [self._described(name, arker) for name, arker in self._sandbox._compat.named_sessions().items()]

    def delete_session(self, session_id: str, request_timeout: float | None = None) -> None:
        self._sandbox._compat.kill_session(self._session(session_id))

    def execute_session_command(
        self, session_id: str, req: SessionExecuteRequest, timeout: int | None = None
    ) -> SessionExecuteResponse:
        """Run a command in the session's shell. With ``run_async`` it returns at
        once and takes input from ``send_session_command_input``. ``output`` is
        stdout followed by stderr; Arker keeps the two apart."""
        compat = self._sandbox._compat
        arker_session = self._session(session_id)
        if req.run_async or req.var_async:
            return SessionExecuteResponse(cmd_id=compat.start_in_session(arker_session, req.command).run_id)
        run_id, result = compat.run_in_session(arker_session, req.command, timeout=timeout)
        return SessionExecuteResponse(
            cmd_id=run_id,
            output=result.stdout + result.stderr,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
        )

    def _command(self, session_id: str, command_id: str) -> CompatProcess:
        return self._sandbox._compat.session_process(self._session(session_id), command_id)

    def get_session_command(self, session_id: str, command_id: str, request_timeout: float | None = None) -> Command:
        for run_id, command, exit_code in self._sandbox._compat.session_commands(self._session(session_id)):
            if run_id == command_id:
                return Command(run_id, command, exit_code)
        raise DaytonaNotFoundError(f"Command {command_id} not found in session {session_id}")

    def get_session_command_logs(
        self, session_id: str, command_id: str, request_timeout: float | None = None
    ) -> SessionCommandLogsResponse:
        process = self._command(session_id, command_id)
        stdout, stderr = process.stdout, process.stderr
        return SessionCommandLogsResponse(output=stdout + stderr, stdout=stdout, stderr=stderr)

    async def get_session_command_logs_async(
        self,
        session_id: str,
        command_id: str,
        on_stdout: Callable[[str], Any],
        on_stderr: Callable[[str], Any],
    ) -> None:
        """Deliver the command's output to the callbacks as it arrives, until it ends."""
        chunks = await asyncio.to_thread(lambda: self._command(session_id, command_id).follow())
        while (chunk := await asyncio.to_thread(next, chunks, None)) is not None:
            for callback, text in zip((on_stdout, on_stderr), chunk, strict=True):
                if text and asyncio.iscoroutine(delivered := callback(text)):
                    await delivered

    def send_session_command_input(
        self, session_id: str, command_id: str, data: str, request_timeout: float | None = None
    ) -> None:
        """Write ``data`` to the stdin of a command started with ``run_async``."""
        self._command(session_id, command_id).send_stdin(data)

    def create_pty_session(
        self,
        id: str,
        cwd: str | None = None,
        envs: dict[str, str] | None = None,
        pty_size: PtySize | None = None,
    ) -> PtyHandle:
        compat = self._sandbox._compat
        if any(info.name == id for info in compat.processes()):
            raise DaytonaError(f"PTY session {id} already exists.")
        size = pty_size or PtySize(rows=24, cols=80)
        terminal = compat.open_terminal(cols=size.cols, rows=size.rows, cwd=cwd, envs=envs, name=id)
        return PtyHandle(terminal, id, self)

    def connect_pty_session(self, session_id: str) -> PtyHandle:
        return PtyHandle(self._sandbox._compat.attach_terminal(self._pty(session_id)), session_id, self)

    def kill_pty_session(self, session_id: str, request_timeout: float | None = None) -> None:
        self._sandbox._compat.kill_session(self._pty(session_id).session_id)

    def resize_pty_session(
        self, session_id: str, pty_size: PtySize, request_timeout: float | None = None
    ) -> PtySessionInfo:
        info = self._pty(session_id)
        self._sandbox._compat.resize_terminal(info.session_id, pty_size.cols, pty_size.rows)
        return PtySessionInfo(session_id, info.cwd or "", info.envs, pty_size.cols, pty_size.rows)

    def _pty(self, session_id: str) -> Any:
        info = next((info for info in self._sandbox._compat.processes() if info.pty and info.name == session_id), None)
        if info is None:
            raise DaytonaNotFoundError(f"PTY session {session_id} not found")
        return info

    def list_pty_sessions(self, request_timeout: float | None = None) -> list[PtySessionInfo]:
        """The terminals made with :meth:`create_pty_session` that are still running."""
        return [_pty_info(info) for info in self._sandbox._compat.processes() if info.pty and info.name]

    def get_pty_session_info(self, session_id: str, request_timeout: float | None = None) -> PtySessionInfo:
        return _pty_info(self._pty(session_id))


def _pty_info(info: Any) -> PtySessionInfo:
    cols, rows = info.size or (0, 0)
    return PtySessionInfo(info.name, info.cwd or "", info.envs, cols, rows)


class FileSystem(_Layer):
    _compat_path = "sandbox.fs"

    def __init__(self, compat: CompatSandbox) -> None:
        self._compat = compat

    def upload_file(self, file: bytes | str, remote_path: str, timeout: int | None = None) -> None:
        """Upload ``file``: its bytes, or the path of a local file."""
        self._compat.write(remote_path, _read_source(file))

    def upload_files(self, files: list[FileUpload], timeout: int | None = None) -> None:
        for upload in files:
            self.upload_file(upload.source, upload.destination)

    def upload_file_stream(self, source: Any, remote_path: str, timeout: int | None = None, **options: Any) -> None:
        """Upload bytes, a local file path or a readable stream; the whole file is sent at once."""
        reject_options(ERRORS, "fs.upload_file_stream", options)
        self._compat.write(remote_path, _read_source(source))

    def download_file(
        self, remote_path: str, local_path: str | None = None, timeout: int | None = None
    ) -> bytes | None:
        data = self._compat.read(remote_path)
        if local_path is None:
            return data
        with open(local_path, "wb") as handle:
            handle.write(data)
        return None

    def download_files(
        self, files: list[FileDownloadRequest], timeout: int | None = None
    ) -> list[FileDownloadResponse]:
        """Download each file; a failure is reported in its response, not raised."""
        responses = []
        for request in files:
            try:
                result = self.download_file(request.source, request.destination)
            except DaytonaError as error:
                responses.append(FileDownloadResponse(request.source, error=str(error)))
                continue
            responses.append(FileDownloadResponse(request.source, result=request.destination or result))
        return responses

    def download_file_stream(self, remote_path: str, timeout: int | None = None, **options: Any) -> Iterator[bytes]:
        """The file's content, fetched whole and yielded as one chunk."""
        reject_options(ERRORS, "fs.download_file_stream", options)
        return iter([self._compat.read(remote_path)])

    def list_files(self, path: str, depth: int | None = None, request_timeout: float | None = None) -> list[FileInfo]:
        return [_file_info(entry) for entry in self._compat.list(path, depth or 1)]

    def get_file_info(self, path: str, request_timeout: float | None = None) -> FileInfo:
        return _file_info(self._compat.stat(path))

    def create_folder(self, path: str, mode: str = "755") -> None:
        self._compat.make_dir(path)
        self.set_file_permissions(path, mode=mode)

    def set_file_permissions(
        self,
        path: str,
        mode: str | None = None,
        owner: str | None = None,
        group: str | None = None,
        request_timeout: float | None = None,
    ) -> None:
        quoted = shlex.quote(path)
        steps = [f"chmod {shlex.quote(mode)} {quoted}"] if mode else []
        if owner or group:
            steps.append(f"chown {shlex.quote((owner or '') + (':' + group if group else ''))} {quoted}")
        if steps:
            self._shell(" && ".join(steps))

    def find_files(self, path: str, pattern: str, request_timeout: float | None = None) -> list[Match]:
        """Lines matching ``pattern``, a grep regular expression, in ``path`` and below."""
        result = self._compat.run(f"grep -rnIZ -e {shlex.quote(pattern)} -- {shlex.quote(path)}")
        if result.exit_code > 1:
            raise DaytonaError(result.stderr.strip() or f"grep failed with exit {result.exit_code}")
        matches = []
        for line in result.stdout.splitlines():
            file, _, rest = line.partition("\0")
            number, _, content = rest.partition(":")
            matches.append(Match(file=file, line=int(number), content=content))
        return matches

    def search_files(self, path: str, pattern: str, request_timeout: float | None = None) -> SearchFilesResponse:
        """Paths in and below ``path`` whose name matches the glob ``pattern``."""
        found = self._shell(f"find {shlex.quote(path)} -mindepth 1 -name {shlex.quote(pattern)} -print0")
        return SearchFilesResponse(files=[file for file in found.split("\0") if file])

    def replace_in_files(
        self, files: list[str], pattern: str, new_value: str, request_timeout: float | None = None
    ) -> list[ReplaceResult]:
        """Replace every occurrence of the text ``pattern`` in each file."""
        results = []
        for file in files:
            try:
                text = self._compat.read(file).decode()
                self._compat.write(file, text.replace(pattern, new_value))
            except (DaytonaError, UnicodeDecodeError) as error:
                results.append(ReplaceResult(file=file, success=False, error=str(error)))
                continue
            results.append(ReplaceResult(file=file, success=True))
        return results

    def delete_file(self, path: str, recursive: bool = False) -> None:
        self._compat.remove(path, recursive=recursive)

    def move_files(self, source: str, destination: str) -> None:
        self._compat.rename(source, destination)

    def _shell(self, script: str) -> str:
        result = self._compat.run(script)
        if result.exit_code != 0:
            raise DaytonaError(result.stderr.strip() or f"exit {result.exit_code}")
        return result.stdout


# Daytona's name for each porcelain status letter.
GIT_FILE_STATES = {
    " ": "Unmodified",
    "?": "Untracked",
    "M": "Modified",
    "A": "Added",
    "D": "Deleted",
    "R": "Renamed",
    "C": "Copied",
    "U": "Updated but unmerged",
}


@dataclasses.dataclass(frozen=True)
class FileStatus:
    name: str
    staging: str
    worktree: str
    extra: str = ""


@dataclasses.dataclass(frozen=True)
class GitStatus:
    current_branch: str
    file_status: list[FileStatus]
    ahead: int = 0
    behind: int = 0
    branch_published: bool = False
    detached: bool = False
    upstream: str | None = None


@dataclasses.dataclass(frozen=True)
class ListBranchResponse:
    branches: list[str]
    current: str | None = None


@dataclasses.dataclass(frozen=True)
class GitRemote:
    name: str
    url: str


@dataclasses.dataclass(frozen=True)
class ListRemotesResponse:
    remotes: list[GitRemote]


@dataclasses.dataclass(frozen=True)
class GitCommitResponse:
    sha: str


def _file_status(change: GitFileChange) -> FileStatus:
    def state(letter: str) -> str:
        return GIT_FILE_STATES.get(letter, "Unmodified")

    return FileStatus(change.name, state(change.index_status), state(change.worktree_status), change.renamed_from or "")


class Git(_Layer):
    """Git in the sandbox, run as the git CLI. Relative paths are under the working directory."""

    _compat_path = "sandbox.git"

    def __init__(self, compat: CompatSandbox) -> None:
        self._git = CompatGit(compat)

    def clone(
        self,
        url: str,
        path: str,
        branch: str | None = None,
        commit_id: str | None = None,
        username: str | None = None,
        password: str | None = None,
        insecure_skip_tls: bool | None = None,
        depth: int | None = None,
        request_timeout: float | None = None,
    ) -> None:
        credentials = {"username": username, "password": password} if username or password else {}
        self._git.clone(
            url,
            path,
            branch=branch,
            depth=depth,
            commit=commit_id,
            insecure_skip_tls=bool(insecure_skip_tls),
            **credentials,
        )

    def init(
        self, path: str, bare: bool = False, initial_branch: str | None = None, request_timeout: float | None = None
    ) -> None:
        self._git.init(path, bare=bare, initial_branch=initial_branch)

    def status(self, path: str, request_timeout: float | None = None) -> GitStatus:
        state = self._git.status(path)
        return GitStatus(
            current_branch=state.current_branch or "",
            file_status=[_file_status(change) for change in state.files],
            ahead=state.ahead,
            behind=state.behind,
            branch_published=state.upstream is not None,
            detached=state.detached,
            upstream=state.upstream,
        )

    def branches(self, path: str, request_timeout: float | None = None) -> ListBranchResponse:
        return ListBranchResponse(*self._git.branches(path))

    def create_branch(self, path: str, name: str, request_timeout: float | None = None) -> None:
        self._git.create_branch(path, name)

    def checkout_branch(self, path: str, branch: str, request_timeout: float | None = None) -> None:
        self._git.checkout_branch(path, branch)

    def delete_branch(self, path: str, name: str, request_timeout: float | None = None) -> None:
        self._git.delete_branch(path, name)

    def add(self, path: str, files: list[str], request_timeout: float | None = None) -> None:
        self._git.add(path, files)

    def commit(
        self,
        path: str,
        message: str,
        author: str,
        email: str,
        allow_empty: bool = False,
        request_timeout: float | None = None,
    ) -> GitCommitResponse:
        self._git.commit(path, message, author_name=author, author_email=email, allow_empty=allow_empty)
        return GitCommitResponse(self._git.run(["rev-parse", "HEAD"], path).stdout.strip())

    def push(
        self,
        path: str,
        username: str | None = None,
        password: str | None = None,
        branch: str | None = None,
        remote: str | None = None,
        set_upstream: bool = False,
        request_timeout: float | None = None,
    ) -> None:
        credentials = {"username": username, "password": password} if username or password else {}
        self._git.push(path, remote=remote, branch=branch, set_upstream=set_upstream, **credentials)

    def pull(
        self,
        path: str,
        username: str | None = None,
        password: str | None = None,
        branch: str | None = None,
        remote: str | None = None,
        request_timeout: float | None = None,
    ) -> None:
        credentials = {"username": username, "password": password} if username or password else {}
        self._git.pull(path, remote=remote, branch=branch, **credentials)

    def reset(
        self,
        path: str,
        mode: str | None = None,
        target: str | None = None,
        files: list[str] | None = None,
        request_timeout: float | None = None,
    ) -> None:
        self._git.reset(path, mode=mode, target=target, paths=files)

    def restore(
        self,
        path: str,
        files: list[str],
        staged: bool | None = None,
        worktree: bool | None = None,
        source: str | None = None,
        request_timeout: float | None = None,
    ) -> None:
        self._git.restore(path, files, staged=staged, worktree=worktree, source=source)

    def remote_add(
        self,
        path: str,
        name: str,
        url: str,
        fetch: bool = False,
        overwrite: bool = False,
        request_timeout: float | None = None,
    ) -> None:
        self._git.remote_add(path, name, url, fetch=fetch, overwrite=overwrite)

    def remotes(self, path: str, request_timeout: float | None = None) -> ListRemotesResponse:
        return ListRemotesResponse([GitRemote(name, url) for name, url in self._git.remotes(path)])

    def remote_get(self, path: str, name: str, request_timeout: float | None = None) -> str | None:
        return self._git.remote_get(path, name)

    def set_config(
        self, key: str, value: str, scope: str = "global", path: str | None = None, request_timeout: float | None = None
    ) -> None:
        self._git.set_config(key, value, scope=scope, path=path)

    def get_config(
        self, key: str, scope: str = "global", path: str | None = None, request_timeout: float | None = None
    ) -> str | None:
        return self._git.get_config(key, scope=scope, path=path)

    def configure_user(
        self,
        name: str,
        email: str,
        scope: str = "global",
        path: str | None = None,
        request_timeout: float | None = None,
    ) -> None:
        self._git.configure_user(name, email, scope=scope, path=path)

    def dangerously_authenticate(
        self,
        username: str,
        password: str,
        host: str | None = None,
        protocol: str | None = None,
        request_timeout: float | None = None,
    ) -> None:
        self._git.dangerously_authenticate(username, password, host=host or "github.com", protocol=protocol or "https")


# Daytona's code interpreter runs for at most this long unless told otherwise.
DEFAULT_CODE_TIMEOUT = 600


@dataclasses.dataclass(frozen=True)
class OutputMessage:
    output: str


@dataclasses.dataclass(frozen=True)
class ExecutionError:
    name: str
    value: str
    traceback: str = ""


@dataclasses.dataclass(frozen=True)
class ExecutionResult:
    stdout: str = ""
    stderr: str = ""
    error: ExecutionError | None = None


@dataclasses.dataclass(frozen=True)
class InterpreterContext:
    id: str
    cwd: str
    language: str = "python"
    active: bool = True


class CodeInterpreter(_Layer):
    """A stateful Python interpreter: variables and imports carry over between
    runs in one context. Output reaches the callbacks once a run finishes."""

    _compat_path = "sandbox.code_interpreter"

    def __init__(self, compat: CompatSandbox) -> None:
        self._contexts = CodeContexts(compat)

    def run_code(
        self,
        code: str,
        *,
        context: InterpreterContext | None = None,
        on_stdout: Callable[[OutputMessage], Any] | None = None,
        on_stderr: Callable[[OutputMessage], Any] | None = None,
        on_error: Callable[[ExecutionError], Any] | None = None,
        envs: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> ExecutionResult:
        timeout = None if timeout == 0 else (timeout or DEFAULT_CODE_TIMEOUT)
        done = self._contexts.run(code, context and context.id, envs=envs, timeout=timeout)
        error = ExecutionError(done.error.name, done.error.value, done.error.traceback) if done.error else None
        if on_stdout and done.stdout:
            on_stdout(OutputMessage(done.stdout))
        if on_stderr and done.stderr:
            on_stderr(OutputMessage(done.stderr))
        if on_error and error:
            on_error(error)
        return ExecutionResult(done.stdout, done.stderr, error)

    def create_context(self, cwd: str | None = None, request_timeout: float | None = None) -> InterpreterContext:
        context = self._contexts.create(cwd)
        return InterpreterContext(context.id, context.cwd)

    def list_contexts(self, request_timeout: float | None = None) -> list[InterpreterContext]:
        """The contexts made with :meth:`create_context`; the default context is not listed."""
        return [InterpreterContext(context.id, context.cwd) for context in self._contexts.list() if not context.default]

    def delete_context(self, context: InterpreterContext, request_timeout: float | None = None) -> None:
        self._contexts.delete(context.id)


class Sandbox(_Layer):
    """A Daytona sandbox running as an Arker VM. Arker suspends a VM when it is
    idle and resumes it when used, so stopping, pausing and archiving all
    suspend it, and starting or recovering it needs nothing."""

    _compat_path = "Sandbox"

    def __init__(self, compat: CompatSandbox, settings: dict[str, Any], env: dict[str, str] | None = None) -> None:
        self._compat = compat
        self._language = settings.get("language", "python")
        self.id = compat.id
        self.name = compat.vm.name
        self.labels: dict[str, str] = settings.get("labels", {})
        self.public: bool = settings.get("public", False)
        self.network_block_all = blocks_all_egress(compat.vm)
        # Read with refresh_data(); listing does not fetch it.
        self.env = env
        self.state = "started"
        self.process = Process(self)
        self.fs = FileSystem(compat)
        self.git = Git(compat)
        self.code_interpreter = CodeInterpreter(compat)

    @property
    def arker(self) -> VM:
        """The Arker VM behind this sandbox, for anything this layer does not cover."""
        return self._compat.vm

    def get_preview_link(self, port: int) -> PortPreviewUrl:
        """Open ``port``. A non-public sandbox's port takes
        ``Authorization: Bearer $ARKER_API_KEY`` in place of a preview token."""
        return PortPreviewUrl(url=f"https://{self._compat.expose(port, public=self.public)}", token="")

    def delete(self, timeout: float | None = None, wait: bool = False) -> None:
        self._compat.delete()

    def start(self, timeout: float | None = 60) -> None:
        self.state = "started"

    def recover(self, timeout: float | None = 60) -> None:
        self.state = "started"

    def stop(self, timeout: float | None = 60, force: bool = False) -> None:
        self._compat.suspend()
        self.state = "stopped"

    def pause(self, timeout: float = 60) -> None:
        self._compat.suspend()
        self.state = "paused"

    def archive(self, request_timeout: float | None = None) -> None:
        self._compat.suspend()
        self.state = "archived"

    def wait_for_sandbox_start(self, timeout: float | None = 60) -> None:
        pass

    def wait_for_sandbox_stop(self, timeout: float | None = 60) -> None:
        pass

    def refresh_data(self, request_timeout: float | None = None) -> None:
        settings = self._compat.metadata()
        self.labels = settings.get("labels", {})
        self.public = settings.get("public", False)
        self.network_block_all = blocks_all_egress(self._compat.info())
        self.env = self._compat.env()

    def refresh_activity(self, request_timeout: float | None = None) -> None:
        """Nothing stops an idle Arker sandbox for good, so there is no timer to reset."""

    def set_labels(self, labels: dict[str, str], request_timeout: float | None = None) -> dict[str, str]:
        self.labels = self._compat.update_metadata({"labels": labels})["labels"]
        return self.labels

    def set_public(self, public: bool, request_timeout: float | None = None) -> None:
        """Make the ports already open, and those opened later, public or private."""
        self._compat.update_metadata({"public": public})
        self._compat.set_ports_public(public)
        self.public = public

    def update_network_settings(
        self,
        *,
        network_block_all: bool | None = None,
        network_allow_list: str | None = None,
        domain_allow_list: str | None = None,
        request_timeout: float | None = None,
    ) -> None:
        if network_block_all is None and network_allow_list is None and domain_allow_list is None:
            raise DaytonaValidationError(
                "At least one of network_block_all, network_allow_list or domain_allow_list must be set"
            )
        egress = _egress(network_block_all, network_allow_list, domain_allow_list)
        self._compat.update_egress(egress)
        self.network_block_all = egress == Egress(deny_all=True)

    def resize(self, resources: Resources | dict[str, Any], timeout: float | None = 60) -> None:
        """Change CPUs, and memory and disk in GiB."""
        fields = _fields(resources)
        reject_options(ERRORS, "Sandbox.resize", {"gpu": fields.get("gpu")})
        self._compat.resize(
            cpus=fields.get("cpu"),
            memory_mib=fields["memory"] * 1024 if fields.get("memory") else None,
            disk_mib=fields["disk"] * 1024 if fields.get("disk") else None,
        )

    def wait_for_resize_complete(self, timeout: float | None = 60) -> None:
        """Nothing to wait for: :meth:`resize` returns once the sandbox is resized."""

    def set_ttl(self, ttl_minutes: int, request_timeout: float | None = None) -> None:
        """Delete the sandbox ``ttl_minutes`` from now, whatever its state; 0 cancels it."""
        if ttl_minutes < 0:
            raise DaytonaValidationError("TTL must be a non-negative integer")
        if ttl_minutes:
            self._compat.set_timeout(ttl_minutes * 60)
        else:
            self._compat.clear_timeout()

    def _idle_interval(self, method: str, interval: int, *, allow_negative: bool = False) -> None:
        """Arker suspends an idle sandbox and resumes it when used, by itself, and
        does not track how long one has been stopped, so these timers do nothing."""
        if interval < 0 and not allow_negative:
            raise DaytonaValidationError(f"The {method} interval must be a non-negative integer")
        reject_options(ERRORS, f"Sandbox.{method}", {"interval": interval}, inert={"interval"})

    def set_autostop_interval(self, interval: int, request_timeout: float | None = None) -> None:
        self._idle_interval("set_autostop_interval", interval)

    def set_auto_pause_interval(self, interval: int) -> None:
        self._idle_interval("set_auto_pause_interval", interval)

    def set_auto_archive_interval(self, interval: int, request_timeout: float | None = None) -> None:
        self._idle_interval("set_auto_archive_interval", interval)

    def set_auto_delete_interval(self, interval: int, request_timeout: float | None = None) -> None:
        """Ignored with a warning; :meth:`set_ttl` deletes the sandbox at a set time."""
        self._idle_interval("set_auto_delete_interval", interval, allow_negative=True)

    def download_url(self, path: str, ttl_seconds: int | None = None) -> str:
        """A URL that downloads ``path`` with no credentials, for ``ttl_seconds``
        (default an hour; zero or less, until :meth:`rotate_signing_key`)."""
        return self._compat.file_url(path, write=False, expires_in=_ttl(ttl_seconds))

    def upload_url(self, path: str, ttl_seconds: int | None = None) -> str:
        """A URL that writes a POSTed multipart/form-data file to ``path`` with no credentials."""
        return self._compat.file_url(path, write=True, expires_in=_ttl(ttl_seconds))

    def rotate_signing_key(self) -> None:
        """Make every URL from :meth:`download_url` and :meth:`upload_url` stop working."""
        self._compat.revoke_file_urls()

    def create_signed_preview_url(
        self, port: int, expires_in_seconds: int | None = None, request_timeout: float | None = None
    ) -> SignedPortPreviewUrl:
        """A URL to ``port`` that needs no other credential, for ``expires_in_seconds``
        (default 60). The token is the URL's ``arker_token`` query parameter; a
        browser that opens the URL keeps it in a cookie for later requests."""
        url, token = self._compat.signed_port_url(port, expires_in_seconds or DEFAULT_PREVIEW_URL_TTL)
        return SignedPortPreviewUrl(self.id, port, token, url)

    def expire_signed_preview_url(self, port: int, token: str, request_timeout: float | None = None) -> None:
        self._compat.revoke_port_url(port, token)

    def update_env(self, env: dict[str, str], *, unset: list[str] | None = None) -> None:
        """Change the environment of commands started from now on."""
        self._compat.update_env(env, unset or ())
        self.env = None

    def get_user_home_dir(self) -> str:
        return self._compat.home_dir()

    def get_user_root_dir(self) -> str:
        return self.get_user_home_dir()

    def get_work_dir(self) -> str:
        return self._compat.work_dir()

    def create_snapshot(self, name: str, timeout: float | None = 60) -> None:
        """Snapshot the sandbox; create sandboxes from it with
        ``CreateSandboxFromSnapshotParams(snapshot=name)``."""
        self._compat.snapshot(name)

    def fork(self, name: str | None = None, timeout: float | None = 60) -> Sandbox:
        """A new sandbox from this one's files and memory, with its settings."""
        forked = self._compat.fork(name=name)
        return Sandbox(forked, forked.settings, self.env)


def _ttl(ttl_seconds: int | None) -> int | None:
    """Seconds a signed file URL works, or None for no expiry, as Daytona reads ``ttl_seconds``."""
    if ttl_seconds is None:
        return DEFAULT_FILE_URL_TTL
    return ttl_seconds if ttl_seconds > 0 else None


@dataclasses.dataclass(frozen=True)
class Snapshot:
    """A sandbox's files and memory, or a registry image, that sandboxes start
    from by name: an Arker VM, so it is always active."""

    id: str
    name: str
    image_name: str | None
    state: str
    cpu: int | None
    mem: int | None
    disk: int | None
    created_at: str | None
    source_sandbox_id: str | None
    gpu: int = 0
    entrypoint: list[str] | None = None
    general: bool = False
    error_reason: str | None = None


@dataclasses.dataclass(frozen=True)
class PaginatedSnapshots:
    items: list[Snapshot]
    total: int
    page: int
    total_pages: int


def _snapshot(compat: CompatSandbox) -> Snapshot:
    vm = compat.vm
    resources = vm.resources
    return Snapshot(
        id=vm.id,
        name=vm.name or vm.id,
        image_name=compat.settings.get("image") or vm.root_source_vm_name,
        state="active",
        cpu=resources.vcpu if resources else None,
        mem=resources.memory_mib // 1024 if resources and resources.memory_mib else None,
        disk=resources.disk_mib // 1024 if resources and resources.disk_mib else None,
        created_at=vm.created_at,
        source_sandbox_id=compat.snapshot_of if compat.snapshot_of != SNAPSHOT_OF_IMAGE else None,
    )


# Marks a snapshot made from an image rather than a sandbox.
SNAPSHOT_OF_IMAGE = "image"


class SnapshotService(_Layer):
    """Snapshots: those of sandboxes (:meth:`Sandbox.create_snapshot`), and those
    made here from registry images. Sandboxes start from one with
    ``CreateSandboxFromSnapshotParams(snapshot=name)``."""

    _compat_path = "daytona.snapshot"

    def __init__(self, arker: dict[str, Any] | None) -> None:
        self._arker = arker

    def create(
        self, params: Any, *, on_logs: Callable[[str], None] | None = None, timeout: float | None = 0
    ) -> Snapshot:
        """A snapshot of a registry image, ready as soon as this returns."""
        fields = _fields(params)
        name, image = fields.pop("name"), fields.pop("image")
        if not isinstance(image, str):
            raise DaytonaValidationError("Only image names are supported on Arker, not Image builders.")
        resources = _fields(fields.pop("resources", None))
        reject_options(ERRORS, "Resources", {"gpu": resources.get("gpu"), "gpu_type": resources.get("gpu_type")})
        reject_options(ERRORS, "CreateSnapshotParams", fields, ignored={"region_id"}, inert={"sandbox_class"})
        if on_logs:
            on_logs(f"Creating snapshot {name} from {image}")
        compat = CompatSandbox.create(
            ERRORS,
            config=self._arker,
            image=image,
            cpus=resources.get("cpu"),
            memory_mib=resources["memory"] * 1024 if resources.get("memory") else None,
            disk_mib=resources["disk"] * 1024 if resources.get("disk") else None,
            metadata={"image": image},
            name=name,
            snapshot_of=SNAPSHOT_OF_IMAGE,
        )
        if on_logs:
            on_logs(f"Created snapshot {name} (active)")
        return _snapshot(compat)

    def _all(self) -> list[CompatSandbox]:
        return list(CompatSandbox.find(ERRORS, self._arker, snapshots=True))

    def list(
        self, page: int | None = None, limit: int | None = None, source_sandbox_id: str | None = None
    ) -> PaginatedSnapshots:
        """Snapshots, oldest first, ``limit`` (default 100) to a page."""
        if page is not None and page < 1:
            raise DaytonaValidationError("page must be a positive integer")
        if limit is not None and limit < 1:
            raise DaytonaValidationError("limit must be a positive integer")
        page, limit = page or 1, limit or 100
        found = [_snapshot(compat) for compat in self._all()]
        found = [snapshot for snapshot in found if source_sandbox_id in (None, snapshot.source_sandbox_id)]
        found.sort(key=lambda snapshot: (snapshot.created_at or "", snapshot.id.rpartition("-")[2]))
        items = found[(page - 1) * limit : page * limit]
        return PaginatedSnapshots(items, len(found), page, max(1, math.ceil(len(found) / limit)))

    def _find(self, snapshot: Snapshot | str) -> CompatSandbox:
        name = snapshot.id if isinstance(snapshot, Snapshot) else snapshot
        compat = CompatSandbox.connect(ERRORS, name, self._arker)
        if compat is None or compat.snapshot_of is None:
            raise DaytonaNotFoundError(f"Snapshot {name} not found")
        return compat

    def get(self, name: str) -> Snapshot:
        """A snapshot by name or id."""
        return _snapshot(self._find(name))

    def delete(self, snapshot: Snapshot | str) -> None:
        self._find(snapshot).delete()

    def activate(self, snapshot: Snapshot | str) -> Snapshot:
        """Snapshots never go inactive on Arker, so this only looks it up."""
        return _snapshot(self._find(snapshot))


@dataclasses.dataclass
class VolumeMount:
    volume_id: str
    mount_path: str
    subpath: str | None = None


@dataclasses.dataclass(frozen=True)
class Volume:
    """A shared volume, as an Arker filesystem."""

    id: str
    name: str
    organization_id: str
    state: str
    created_at: str


def _volume(filesystem: Filesystem) -> Volume:
    return Volume(filesystem.filesystem_id, filesystem.name, filesystem.owner_org_id, "ready", filesystem.created_at)


class VolumeService(_Layer):
    _compat_path = "daytona.volume"

    def __init__(self, arker: dict[str, Any] | None) -> None:
        self._volumes = Volumes(ERRORS, arker)

    def list(self) -> list[Volume]:
        return [_volume(volume) for volume in self._volumes.list()]

    def get(self, name: str, create: bool = False) -> Volume:
        return _volume(self._volumes.get(name, create=create))

    def create(self, name: str) -> Volume:
        return _volume(self._volumes.create(name))

    def delete(self, volume: Volume) -> None:
        if not self._volumes.delete(volume.id):
            raise DaytonaNotFoundError(f"Volume {volume.id} not found")

    def mounts(self, mounts: list[Any] | None) -> dict[str, str] | None:
        """Mount path to volume id, for mounts naming a volume by id or by name."""
        resolved = {}
        for mount in mounts or []:
            fields = _fields(mount)
            reject_options(ERRORS, "VolumeMount", {"subpath": fields.get("subpath")})
            found = self._volumes.find(fields["volume_id"])
            resolved[fields["mount_path"]] = (
                found.filesystem_id if found else self._volumes.by_id(fields["volume_id"]).filesystem_id
            )
        return resolved or None


def _value(option: Any) -> Any:
    """An enum option's value, as Daytona's query takes either."""
    return getattr(option, "value", option)


def _resource(compat: CompatSandbox, name: str, divisor: int = 1) -> float:
    resources = compat.vm.resources
    value = getattr(resources, name, None) if resources else None
    return (value or 0) / divisor


def _gib(name: str) -> Callable[[CompatSandbox], float]:
    return lambda compat: _resource(compat, name, 1024)


def _cpus(compat: CompatSandbox) -> float:
    return _resource(compat, "vcpu")


SORT_KEYS: dict[str, Callable[[CompatSandbox], Any]] = {
    "name": lambda compat: compat.vm.name or "",
    "cpu": _cpus,
    "memoryGib": _gib("memory_mib"),
    "diskGib": _gib("disk_mib"),
    "lastActivityAt": lambda compat: compat.vm.last_active_at or "",
    "createdAt": lambda compat: (compat.vm.created_at or "", compat.id.rpartition("-")[2]),
}


def _list_filter(filters: dict[str, Any]) -> Callable[[CompatSandbox], bool]:
    """A test for the filters ``Daytona.list`` takes, which it removes from ``filters``."""
    tests: list[Callable[[CompatSandbox], bool]] = []
    id_prefix = (filters.pop("id", None) or "").lower()
    name_prefix = (filters.pop("name", None) or "").lower()
    labels = filters.pop("labels", None) or {}
    tests.append(lambda compat: compat.id.lower().startswith(id_prefix))
    tests.append(lambda compat: (compat.vm.name or "").lower().startswith(name_prefix))
    tests.append(lambda compat: labels.items() <= (compat.settings.get("labels") or {}).items())
    states = filters.pop("states", None)
    if states is not None:
        started = "started" in {_value(state) for state in states}
        tests.append(lambda _: started)
    snapshots = filters.pop("snapshots", None)
    if snapshots is not None:
        tests.append(lambda compat: compat.settings.get("snapshot") in snapshots)
    is_public = filters.pop("is_public", None)
    if is_public is not None:
        tests.append(lambda compat: bool(compat.settings.get("public")) == is_public)
    for name, measure in (("cpu", _cpus), ("memory_gib", _gib("memory_mib")), ("disk_gib", _gib("disk_mib"))):
        low, high = filters.pop(f"min_{name}", None), filters.pop(f"max_{name}", None)
        if low is not None:
            tests.append(lambda compat, low=low, measure=measure: measure(compat) >= low)
        if high is not None:
            tests.append(lambda compat, high=high, measure=measure: measure(compat) <= high)
    for name, attribute in (("created_at", "created_at"), ("last_activity", "last_active_at")):
        after, before = filters.pop(f"{name}_after", None), filters.pop(f"{name}_before", None)
        for bound, later in ((after, True), (before, False)):
            if bound is not None:
                tests.append(
                    lambda compat, bound=bound, later=later, attribute=attribute: _on_side(
                        getattr(compat.vm, attribute), bound, later
                    )
                )
    return lambda compat: all(test(compat) for test in tests)


def _on_side(text: str | None, bound: datetime, later: bool) -> bool:
    if not text:
        return False
    moment = parse_time(text)
    bound = bound if bound.tzinfo else bound.replace(tzinfo=timezone.utc)
    return moment > bound if later else moment < bound


class Daytona(_Layer):
    _compat_path = "Daytona"

    def __init__(self, config: DaytonaConfig | None = None, *, arker: dict[str, Any] | None = None) -> None:
        self._config = config
        self._arker = arker
        self.volume = VolumeService(arker)
        self.snapshot = SnapshotService(arker)

    def create(self, params: Any = None, *, timeout: float | None = None, **kwargs: Any) -> Sandbox:
        reject_options(ERRORS, "Daytona.create", kwargs, ignored={"on_snapshot_create_logs"})
        options = _fields(params)
        language = options.pop("language", None) or "python"
        if language not in LANGUAGES:
            raise DaytonaValidationError(f"Unsupported language {language!r}; use one of {', '.join(LANGUAGES)}.")
        image = options.pop("image", None)
        if image is not None and not isinstance(image, str):
            raise DaytonaValidationError("Only image names are supported on Arker, not Image builders.")
        resources = _fields(options.pop("resources", None))
        reject_options(ERRORS, "Resources", {"gpu": resources.get("gpu")})
        snapshot = options.pop("snapshot", None)
        settings = {
            "language": language,
            "labels": options.pop("labels", None) or {},
            "public": bool(options.pop("public", False)),
            **({"snapshot": snapshot} if snapshot else {}),
        }
        envs = options.pop("env_vars", None)
        volumes = self.volume.mounts(options.pop("volumes", None))
        name = options.pop("name", None)
        egress = _egress(options.pop("network_block_all", False), options.pop("network_allow_list", None))
        reject_options(ERRORS, "Daytona.create", options, inert=INERT_PARAMS)
        compat = CompatSandbox.create(
            ERRORS,
            config=self._arker,
            source=snapshot,
            image=image,
            envs=envs,
            cpus=resources.get("cpu"),
            memory_mib=resources["memory"] * 1024 if resources.get("memory") else None,
            disk_mib=resources["disk"] * 1024 if resources.get("disk") else None,
            egress=egress,
            metadata=settings,
            name=name,
            volumes=volumes,
        )
        return Sandbox(compat, settings, envs or {})

    def get(self, sandbox_id_or_name: str, request_timeout: float | None = None) -> Any:
        """An id or name Arker does not know is handed to the ``daytona`` package
        when it is installed, so existing Daytona sandboxes keep working."""
        compat = CompatSandbox.connect(ERRORS, sandbox_id_or_name, self._arker)
        if compat is None:
            return self._native(sandbox_id_or_name).get(sandbox_id_or_name)
        return Sandbox(compat, compat.metadata(), compat.env())

    def list(
        self, query: ListSandboxesQuery | dict[str, Any] | None = None, request_timeout: float | None = None
    ) -> Iterator[Sandbox]:
        """The sandboxes this layer created that match ``query`` (see :class:`ListSandboxesQuery`).
        Every sandbox counts as started: Arker resumes a suspended one when it is used."""
        filters = _fields(query)
        sort, order = _value(filters.pop("sort", None)), _value(filters.pop("order", None))
        matches = _list_filter(filters)
        reject_options(ERRORS, "ListSandboxesQuery", filters, ignored={"limit"})
        found = (compat for compat in CompatSandbox.find(ERRORS, self._arker) if matches(compat))
        if sort:
            if sort not in SORT_KEYS:
                raise DaytonaValidationError(f"Cannot sort by {sort!r}; use one of {', '.join(SORT_KEYS)}.")
            found = iter(sorted(found, key=SORT_KEYS[sort], reverse=order == "desc"))
        for compat in found:
            yield Sandbox(compat, compat.settings)

    def start(self, sandbox: Sandbox, timeout: float = 60) -> None:
        sandbox.start(timeout)

    def stop(self, sandbox: Sandbox, timeout: float = 60) -> None:
        sandbox.stop(timeout)

    def delete(self, sandbox: Any, timeout: float | None = None) -> None:
        if isinstance(sandbox, Sandbox):
            sandbox.delete()
            return
        self.get(sandbox.id if hasattr(sandbox, "id") else sandbox).delete()

    def _native(self, sandbox_id: str, client: str = "Daytona") -> Any:
        native = import_optional("daytona")
        if native is None:
            raise DaytonaNotFoundError(f"Sandbox {sandbox_id} not found")
        config = native.DaytonaConfig(**dataclasses.asdict(self._config)) if self._config else None
        return getattr(native, client)(config)


# The async API: ``AsyncDaytona`` and what it hands out are the sync classes
# above, awaited in worker threads; only what Daytona's async API does
# differently is written out here.


class _AsyncLayer(AsyncWrapper):
    _compat_errors = ERRORS


class AsyncPtyHandle(_AsyncLayer):
    """A terminal whose output a background task passes to ``on_data`` as it arrives."""

    _compat_path = "AsyncPtyHandle"

    def __init__(self, handle: PtyHandle, on_data: Callable[[bytes], Any]) -> None:
        super().__init__(handle)
        self._task = asyncio.create_task(self._follow(on_data))

    async def _follow(self, on_data: Callable[[bytes], Any]) -> None:
        async for data in iterate(iter(self._sync)):
            await deliver(on_data, data)

    def is_connected(self) -> bool:
        return self._sync.is_connected

    async def wait(self) -> PtyResult:
        await asyncio.wait([self._task])
        return await call(self._sync.wait)


class AsyncProcess(_AsyncLayer, wraps=Process):
    async def create_pty_session(
        self,
        id: str,
        on_data: Callable[[bytes], Any],
        cwd: str | None = None,
        envs: dict[str, str] | None = None,
        pty_size: PtySize | None = None,
    ) -> AsyncPtyHandle:
        return AsyncPtyHandle(await call(self._sync.create_pty_session, id, cwd, envs, pty_size), on_data)

    async def connect_pty_session(self, session_id: str, on_data: Callable[[bytes], Any]) -> AsyncPtyHandle:
        return AsyncPtyHandle(await call(self._sync.connect_pty_session, session_id), on_data)


class AsyncFileSystem(_AsyncLayer, wraps=FileSystem):
    pass


class AsyncGit(_AsyncLayer, wraps=Git):
    pass


class AsyncCodeInterpreter(_AsyncLayer, wraps=CodeInterpreter):
    pass


class AsyncVolumeService(_AsyncLayer, wraps=VolumeService):
    pass


class AsyncSnapshotService(_AsyncLayer, wraps=SnapshotService):
    pass


class AsyncSandbox(_AsyncLayer, wraps=Sandbox):
    _compat_path = "AsyncSandbox"


class AsyncDaytona(_AsyncLayer, wraps=Daytona):
    """:class:`Daytona` with coroutine methods, as ``daytona.AsyncDaytona``;
    ``list`` is an async iterator."""

    _compat_path = "AsyncDaytona"

    def __init__(self, config: DaytonaConfig | None = None, *, arker: dict[str, Any] | None = None) -> None:
        super().__init__(Daytona(config, arker=arker))

    async def get(self, sandbox_id_or_name: str, request_timeout: float | None = None) -> Any:
        """Like :meth:`Daytona.get`, handing what Arker does not know to ``daytona.AsyncDaytona``."""
        unknown = import_optional("daytona") is not None and (
            await call(CompatSandbox.connect, ERRORS, sandbox_id_or_name, self._sync._arker) is None
        )
        if unknown:
            return await self._sync._native(sandbox_id_or_name, "AsyncDaytona").get(sandbox_id_or_name)
        return await asynchronous(self._sync.get)(sandbox_id_or_name)

    async def close(self) -> None:
        """This layer holds no connection, so there is nothing to close."""

    async def __aenter__(self) -> AsyncDaytona:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()
