"""The single path from every sandbox compatibility layer to Arker.

``arker.e2b``, ``arker.modal`` and ``arker.daytona`` only translate their
provider's API into :class:`CompatSandbox` calls and its results back into the
provider's types. Every Arker request they make goes through here, so a fix
here fixes all of them.
"""

from __future__ import annotations

import base64
import codecs
import contextlib
import dataclasses
import functools
import hashlib
import importlib
import ipaddress
import itertools
import json
import math
import os
import posixpath
import queue
import re
import secrets
import shlex
import stat
import threading
import time
import urllib.parse
import uuid
import warnings
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

from .computer import VM, Arker, ArkerError, Filesystem

ARKER_CLIENT_KEYS = {"api_key", "base_url", "control_base_url", "region", "provider", "retry"}

# Public base VM closest to the images other sandbox providers default to:
# Python with pip, Node with npm, git and curl.
DEFAULT_SOURCE = "ubuntu-coding"
# Sandbox-level envs live in the VM, so they survive reconnecting.
ENV_FILE = "/etc/arker-compat.env"
# Marks the Arker sessions that stand for a provider's named sessions.
SESSION_NAME_ENV = "ARKER_COMPAT_SESSION"
_SESSION_SETUP = f"[ -f {ENV_FILE} ] && . {ENV_FILE}; true"
# Files that may hold env values. Commands run as root and /root is private to
# it, which a file's own mode cannot do: sync does not set modes.
PRIVATE_DIR = "/root/.arker-compat"
# One directory per started process or terminal, named by its Arker session:
# its pid, how it was started, and its stdin pipe.
PROCESS_DIR = f"{PRIVATE_DIR}/sessions"
TIMEOUT_RULE = "compat-timeout"
SUSPEND_RULE = "compat-suspend"
EGRESS_RULE = "compat-egress"
PORT_RULE = "compat-port"
# Inbound rules that let in requests carrying a token: signed file and port URLs.
GRANT_RULE = "compat-grant"
# Above every port rule, so a request with a token is let in before a port rule refuses it.
GRANT_PRIORITY = 200_000
# Where the server behind signed file URLs listens in the VM.
FILE_SERVER_PORT = 49983
FILE_SERVER_PATH = "/opt/arker-compat/file_server.py"
MIB = 1024 * 1024
LIST_PAGE_SIZE = 1000
POLL_INTERVAL = 0.5
# Rules of one type need distinct priorities; ports are at most 65535.
PORT_RULE_PRIORITY = 100_000
# The API's limit on a VM description, where sandbox metadata is kept.
MAX_DESCRIPTION = 1024
DOCS = "https://arker.ai/docs"
ISSUES = "https://github.com/ArkerHQ/arker-sdk/issues"

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TIMED_OUT = re.compile(r"command timed out after \d+ms\n?$")


@dataclasses.dataclass(frozen=True)
class Errors:
    """The provider's exception classes, raised by the core in its place."""

    provider: str
    unsupported: type[Exception]
    invalid: type[Exception]
    not_found: type[Exception]
    file_not_found: type[Exception]
    timeout: type[Exception]
    failed: type[Exception]
    auth: type[Exception] | None = None
    rate_limit: type[Exception] | None = None


def _provider_error(errors: Errors, error: ArkerError) -> Exception:
    if error.status in (401, 403) and errors.auth:
        return errors.auth(str(error))
    if error.status == 429 and errors.rate_limit:
        return errors.rate_limit(str(error))
    return errors.failed(str(error))


def _translated(method: Callable[..., Any]) -> Callable[..., Any]:
    """Raise Arker API errors the method does not handle as the provider's error."""

    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except ArkerError as error:
            raise _provider_error(self._errors, error) from error

    return wrapper


@dataclasses.dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int
    error: str | None = None


@dataclasses.dataclass(frozen=True)
class Entry:
    name: str
    path: str
    is_dir: bool
    size: int
    mode: int = 0
    owner: str = ""
    group: str = ""
    modified: float = 0.0
    symlink_target: str | None = None

    @property
    def permissions(self) -> str:
        """``rwxr-xr-x`` style."""
        return stat.filemode(self.mode)[1:]


@dataclasses.dataclass(frozen=True)
class ProcessInfo:
    pid: int
    session_id: str
    cmd: str
    cwd: str | None
    envs: dict[str, str]
    pty: bool
    name: str | None = None
    # A terminal's (cols, rows).
    size: tuple[int, int] | None = None


@dataclasses.dataclass(frozen=True)
class Change:
    """A difference between two :meth:`CompatSandbox.snapshot` results."""

    path: str
    kind: str  # "create", "write", "chmod" or "remove"


def unsupported(errors: Errors, feature: str, hint: str | None = None) -> Exception:
    """The error for anything the layer does not cover: what it was, what to do
    instead, and where to ask for it."""
    lines = [f"{feature} is not supported by Arker's {errors.provider} compatibility layer yet."]
    if hint:
        lines.append(f"  Instead: {hint}")
    lines += [
        f"  Use Arker directly: the sandbox's `.arker` attribute is its Arker VM ({DOCS}/quickstart).",
        f"  What this layer covers: {DOCS}/migration#migrate-from-{errors.provider.lower()}",
        f"  Need it? Open an issue: {ISSUES}",
    ]
    return errors.unsupported("\n".join(lines))


class UnsupportedImportError(ImportError):
    pass


def missing_export(errors: Errors, module: str, name: str) -> Any:
    """Module ``__getattr__``. An ImportError, since ``from m import name``
    replaces any AttributeError with a message of its own."""
    if name.startswith("_"):
        raise AttributeError(f"module {module!r} has no attribute {name!r}")
    raise UnsupportedImportError(str(unsupported(errors, name)), name=module)


class _UnsupportedClass(type):
    def __getattr__(cls, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        raise unsupported(cls._compat_errors, f"{cls._compat_path}.{name}", cls._compat_hints.get(name))


class UnsupportedMixin(metaclass=_UnsupportedClass):
    """Name what is missing, on the class and its instances, instead of a bare AttributeError.
    The provider's unsupported error is also an AttributeError, so ``hasattr`` still works."""

    _compat_errors: Errors
    _compat_path: str
    _compat_hints: ClassVar[dict[str, str]] = {}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        raise unsupported(self._compat_errors, f"{self._compat_path}.{name}", self._compat_hints.get(name))


@dataclasses.dataclass(frozen=True)
class Egress:
    """Outbound traffic, as sandbox providers describe it: an allow beats a
    deny, and what neither names is allowed unless ``deny_all``.
    ``allow_hosts`` are exact names, or ``*.name`` for one label under it."""

    allow_ips: tuple[str, ...] = ()
    allow_hosts: tuple[str, ...] = ()
    deny_ips: tuple[str, ...] = ()
    deny_all: bool = False


def split_addresses(entries: list[str]) -> tuple[list[str], list[str]]:
    """Split entries into ``(ips_and_cidrs, hostnames)``."""
    ips: list[str] = []
    hosts: list[str] = []
    for entry in entries:
        try:
            ipaddress.ip_network(entry, strict=False)
            ips.append(entry)
        except ValueError:
            hosts.append(entry)
    return ips, hosts


def _egress_rules(egress: Egress) -> list[dict[str, Any]]:
    """Arker outbound rules, highest priority first. Arker matches a host and
    every name under it, so each exact host has a deny for ``*.host`` just
    above; deeper names come first so that deny never hides another allow.
    ``*.`` covers one label, so names two or more labels under an exact host
    stay allowed."""

    def rule(action: str, **match: list[str]) -> dict[str, Any]:
        return {"type": "outbound", "action": action, **({"match": match} if match else {})}

    wildcards = [host for host in egress.allow_hosts if host.startswith("*.")]
    exact = sorted((host for host in egress.allow_hosts if not host.startswith("*.")), key=lambda h: -h.count("."))
    rules = [rule("allow", hosts=wildcards)] if wildcards else []
    for host in exact:
        rules += [rule("deny", hosts=[f"*.{host}"]), rule("allow", hosts=[host])]
    if egress.allow_ips:
        rules.append(rule("allow", ips=list(egress.allow_ips)))
    if egress.deny_ips:
        rules.append(rule("deny", ips=list(egress.deny_ips)))
    if egress.deny_all:
        rules.append(rule("deny"))
    elif rules:
        # An Arker allow rule denies what no rule matches; providers allow it.
        rules.append(rule("allow"))
    return rules


def egress_policy(egress: Egress) -> dict[str, Any]:
    rules = _egress_rules(egress)
    return {
        f"{EGRESS_RULE}-{index}": {**rule, "priority": (len(rules) - index) * 10} for index, rule in enumerate(rules)
    }


def new_port_auth(header: str) -> dict[str, str]:
    """A fresh token that requests to the sandbox's ports must carry in ``header``."""
    return {"header": header, "token": secrets.token_urlsafe(32)}


def blocks_all_egress(vm: VM) -> bool:
    """Whether the outbound rules a compatibility layer set on ``vm`` deny all traffic."""
    rules = (vm.policies.rules if vm.policies else None) or {}
    egress = [rule for rule_id, rule in rules.items() if rule_id.startswith(f"{EGRESS_RULE}-")]
    if len(egress) != 1 or egress[0].action != "deny":
        return False
    # A catch-all rule reads back with a match whose fields are all unset.
    return egress[0].match is None or not any(vars(egress[0].match).values())


def _grant_id(kind: str, port: int, token: str) -> str:
    return f"{GRANT_RULE}-{kind}-{port}-{hashlib.sha256(token.encode()).hexdigest()[:16]}"


def import_optional(module_name: str) -> Any | None:
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name != module_name:
            raise
        return None


_warned: set[str] = set()


def reject_options(
    errors: Errors,
    method: str,
    options: dict[str, Any],
    ignored: set[str] = frozenset(),
    inert: set[str] = frozenset(),
) -> None:
    """Raise for options the layer cannot honour. ``ignored`` options only
    configure the provider's own client; ``inert`` ones have no Arker meaning,
    and setting one warns once."""
    given = sorted(name for name, value in options.items() if value is not None and name not in ignored)
    for name in (name for name in given if name in inert):
        feature = f"{method}({name}=...)"
        if feature not in _warned:
            _warned.add(feature)
            warnings.warn(f"{feature} has no effect on Arker and is ignored.", stacklevel=3)
    rejected = [name for name in given if name not in inert]
    if rejected:
        raise unsupported(errors, f"{method}({', '.join(f'{name}=...' for name in rejected)})")


def arker_client(config: dict[str, Any] | None = None) -> Arker:
    return Arker(**{key: value for key, value in (config or {}).items() if key in ARKER_CLIENT_KEYS})


def arker_source(config: dict[str, Any] | None = None) -> str:
    cfg = config or {}
    return str(
        cfg.get("source") or os.environ.get("ARKER_SOURCE") or os.environ.get("ARKER_SOURCE_VM") or DEFAULT_SOURCE
    )


def _paged(fetch: Callable[[str | None], Any], items: str) -> Iterator[Any]:
    """Every item of a cursor-paginated listing; ``items`` names a page's list of them."""
    cursor = None
    while True:
        page = fetch(cursor)
        yield from getattr(page, items)
        cursor = page.next_cursor
        if not cursor:
            return


def _schedule_rule(action: str, *, after_created: int | None = None, at: datetime | None = None) -> dict[str, Any]:
    clock = {"after_created": after_created} if at is None else {"at": at.isoformat()}
    return {"type": "schedule", **clock, "action": action}


class Process:
    """A command started in a session of its own; see :meth:`CompatSandbox.start`.

    The session is closed when the command ends, and :meth:`kill` closes it
    early, which stops everything the command started. A process in a session
    it does not own (``owns_session=False``) leaves the session alone."""

    def __init__(
        self,
        sandbox: CompatSandbox,
        run_id: str,
        session_id: str,
        timeout: float | None,
        *,
        owns_session: bool = True,
        stdin: bool = False,
    ) -> None:
        self._sandbox = sandbox
        self._errors = sandbox._errors
        self.run_id = run_id
        self.session_id = session_id
        self._owns_session = owns_session
        self._stdin = stdin
        self._timeout = timeout
        self._pid: int | None = None
        self._stdout = b""
        self._stderr = b""
        self._state = "running"
        self._exit_code: int | None = None
        self._fail_reason: str | None = None

    @_translated
    def _refresh(self) -> None:
        if self.done:
            return
        run = self._sandbox.vm.get_run(self.run_id)
        self._stdout, self._stderr = run.stdout_bytes, run.stderr_bytes
        self._state, self._exit_code, self._fail_reason = run.state, run.exit_code, run.fail_reason
        if self.done and self._owns_session:
            self._close_session()

    def _close_session(self) -> bool:
        try:
            self._sandbox.vm.delete_session(self.session_id)
        except ArkerError as error:
            if error.status == 404:
                return False
            raise
        return True

    @property
    def done(self) -> bool:
        return self._state not in ("pending", "running")

    def poll(self) -> int | None:
        """The exit code, or None while the command runs."""
        self._refresh()
        return self.result().exit_code if self.done else None

    def follow(self) -> Iterator[tuple[str, str]]:
        """Yield ``(stdout, stderr)`` text as it arrives, from the start of the
        output, until the command ends. Each call reads independently."""
        decoders = [codecs.getincrementaldecoder("utf-8")("replace") for _ in range(2)]
        offsets = [0, 0]
        while True:
            self._refresh()
            chunk = []
            for index, data in enumerate((self._stdout, self._stderr)):
                chunk.append(decoders[index].decode(data[offsets[index] :], final=self.done))
                offsets[index] = len(data)
            if any(chunk):
                yield chunk[0], chunk[1]
            if self.done:
                return
            time.sleep(POLL_INTERVAL)

    def wait(
        self,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> CommandResult:
        for stdout, stderr in self.follow():
            _emit(on_stdout, stdout)
            _emit(on_stderr, stderr)
        return self.result()

    def result(self) -> CommandResult:
        return self._sandbox._finish(
            _decode(self._stdout), _decode(self._stderr), self._exit_code, self._state, self._fail_reason, self._timeout
        )

    @property
    def stdout(self) -> str:
        self._refresh()
        return _decode(self._stdout)

    @property
    def stderr(self) -> str:
        self._refresh()
        return _decode(self._stderr)

    @property
    def pid(self) -> int:
        """The command's process id in the VM, once it has started."""
        while self._pid is None:
            self._pid = self._sandbox._process_pid(self.session_id)
            if self._pid is None:
                self._refresh()
                if self.done:
                    # It may have recorded its pid and exited since the read above.
                    self._pid = self._sandbox._process_pid(self.session_id)
                    if self._pid is not None:
                        break
                    self.result()
                    raise self._errors.failed("The command ended before it reported its process id.")
                time.sleep(POLL_INTERVAL / 5)
        return self._pid

    def send_stdin(self, data: str | bytes) -> None:
        if not self._once_piped(lambda: self._sandbox.send_stdin(self.session_id, data)):
            raise self._errors.invalid("The command has ended, or its stdin is closed.")

    def close_stdin(self) -> None:
        """Close stdin, so the command reads end of file; nothing to do once it has ended."""
        self._once_piped(lambda: self._sandbox.close_stdin(self.session_id))

    def _once_piped(self, action: Callable[[], bool]) -> bool:
        """Do ``action`` once the command has set up its stdin pipe; False if it ended first."""
        if not self._stdin:
            raise self._errors.invalid("The command was started without stdin.")
        while not action():
            self._refresh()
            if self.done:
                return False
            time.sleep(POLL_INTERVAL / 5)
        return True

    @_translated
    def kill(self) -> bool:
        """Stop the command and everything it started; False if it had already ended."""
        if self.done:
            return False
        killed = self._close_session()
        self._refresh()
        return killed


class Terminal:
    """An interactive shell attached over a WebSocket; see :meth:`CompatSandbox.open_terminal`.

    Output is delivered to ``on_data`` and to :meth:`output`. Detaching leaves
    the shell running; :meth:`kill` ends it."""

    def __init__(
        self,
        sandbox: CompatSandbox,
        session_id: str,
        pid: int,
        *,
        cols: int | None = None,
        rows: int | None = None,
        on_data: Callable[[bytes], None] | None = None,
    ) -> None:
        self._sandbox = sandbox
        self.session_id = session_id
        self.pid = pid
        self._on_data = on_data
        self._chunks: queue.Queue[bytes | None] = queue.Queue()
        self._closed = threading.Event()
        try:
            self._connection = sandbox.vm.connect_pty(
                session_id=session_id, cols=cols, rows=rows, on_data=self._receive, on_close=self._close
            )
        except ArkerError as error:
            raise _provider_error(sandbox._errors, error) from error

    def _receive(self, data: bytes) -> None:
        if self._on_data is not None:
            self._on_data(data)
        self._chunks.put(data)

    def _close(self, _event: Any) -> None:
        self._closed.set()
        self._chunks.put(None)

    def output(self) -> Iterator[bytes]:
        """Terminal output as it arrives, until the shell exits or is detached."""
        while (chunk := self._chunks.get()) is not None:
            yield chunk
        # Put the end back, so the next reader ends too instead of waiting forever.
        self._chunks.put(None)

    def send(self, data: str | bytes) -> None:
        self._connection.send_input(data)

    def resize(self, cols: int, rows: int) -> None:
        self._sandbox.resize_terminal(self.session_id, cols, rows)

    def wait(self) -> CommandResult:
        """Wait for the shell to exit; a killed shell is an error."""
        self._closed.wait()
        exit_code = self._sandbox._terminal_exit_code(self.session_id)
        if exit_code is None:
            raise self._sandbox._errors.failed("The terminal was killed.")
        return CommandResult(stdout="", stderr="", exit_code=exit_code)

    def detach(self) -> None:
        self._connection.close()

    def kill(self) -> bool:
        return self._sandbox.kill_session(self.session_id)


def _process_dir(session_id: str) -> str:
    return f"{PROCESS_DIR}/{session_id}"


def _open_stdin(directory: str) -> str:
    """Shell lines making ``directory/stdin`` a pipe that stays open until :func:`_release_stdin`."""
    return (
        f"rm -f {directory}/stdin; mkfifo {directory}/stdin"
        f" && (sleep infinity > {directory}/stdin & echo $! > {directory}/holder)\n"
    )


def _release_stdin(directory: str) -> str:
    return f"kill $(cat {directory}/holder 2>/dev/null) 2>/dev/null; rm -f {directory}/stdin"


def _piped(directory: str, cmd: str) -> str:
    """``cmd`` run in the session's own shell with stdin from a pipe."""
    return (
        f"mkdir -p {directory}\n{_open_stdin(directory)}{{\n{cmd}\n}} < {directory}/stdin; "
        f"__arker_status=$?; {_release_stdin(directory)}; (exit $__arker_status)"
    )


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def _emit(callback: Callable[[str], None] | None, text: str) -> None:
    if callback is not None and text:
        callback(text)


class CompatSandbox:
    """One Arker VM, driven the way sandbox providers expect."""

    def __init__(self, vm: VM, errors: Errors) -> None:
        described = _description(vm)
        self.vm = vm
        self.id = vm.id
        self._errors = errors
        self._on_timeout = described.get("on_timeout", "delete")
        # The metadata as of loading the VM; metadata() reads it fresh.
        self.settings: dict[str, Any] = described.get("compat") or {}
        self.snapshot_of: str | None = described.get("snapshot_of")
        # The header and token every request to an exposed port must carry, if any.
        self.port_auth: dict[str, str] | None = described.get("port_auth")

    @classmethod
    def create(
        cls,
        errors: Errors,
        *,
        config: dict[str, Any] | None = None,
        source: str | None = None,
        image: str | None = None,
        envs: dict[str, str] | None = None,
        cpus: int | None = None,
        memory_mib: int | None = None,
        disk_mib: int | None = None,
        delete_after: int | None = None,
        on_timeout: str = "delete",
        egress: Egress | None = None,
        metadata: dict[str, Any] | None = None,
        name: str | None = None,
        id_env: str | None = None,
        parent: VM | None = None,
        keep_network: bool = False,
        snapshot_of: str | None = None,
        volumes: dict[str, str] | None = None,
        port_auth: dict[str, str] | None = None,
    ) -> CompatSandbox:
        """Fork ``parent``, else ``image``, else ``source``, else the configured default VM.

        ``delete_after`` seconds after creation the VM is deleted, or suspended
        when ``on_timeout`` is ``"suspend"``. ``id_env`` names an environment
        variable that commands see holding the sandbox id. A sandbox forked from
        a VM keeps its sandbox envs. It takes none of the rules this layer set on
        that VM, so it serves no port and limits no traffic it did not ask for;
        with ``keep_network`` it keeps the VM's exposed ports and outbound rules,
        though never its grants (see :meth:`file_url`), whose tokens are the VM's.
        ``snapshot_of`` marks the new VM as a snapshot of that sandbox, listed
        apart from sandboxes. ``volumes`` maps mount paths to the ids of volumes
        to mount there. With ``port_auth`` (see :func:`new_port_auth`), exposed
        ports let in only requests carrying its token."""
        _exports(errors, envs or {})
        forks_vm = parent is not None or not image
        rules: dict[str, Any] = {}
        if forks_vm:
            # A fork's policies are merged into its source's, so the source's
            # rules apply to the fork unless replaced.
            for rule_id in _layer_rule_ids(_source_vm(errors, config, parent, source)):
                if rule_id.startswith(f"{EGRESS_RULE}-"):
                    keep = keep_network and not egress
                else:
                    keep = keep_network and rule_id.startswith(f"{PORT_RULE}-")
                if not keep:
                    rules[rule_id] = None
        if delete_after:
            rules[TIMEOUT_RULE] = _schedule_rule(on_timeout, after_created=math.ceil(delete_after))
        if egress:
            rules.update(egress_policy(egress))
        options: dict[str, Any] = (
            {} if parent else {"image": image} if image else {"source_vm_name": source or arker_source(config)}
        )
        if rules:
            options["policies"] = {"rules": rules}
        resources = {"vcpu": cpus, "memory_mib": memory_mib, "disk_mib": disk_mib}
        if any(resources.values()):
            options["resources"] = {key: value for key, value in resources.items() if value}
        options["description"] = _description_json(
            errors,
            {
                "layer": errors.provider.lower(),
                **({"compat": metadata} if metadata else {}),
                **({"on_timeout": on_timeout} if on_timeout != "delete" else {}),
                **({"snapshot_of": snapshot_of} if snapshot_of else {}),
                **({"port_auth": port_auth} if port_auth else {}),
            },
        )
        name = name or (config or {}).get("name")
        if name:
            options["name"] = name
        try:
            vm = parent.fork(**options) if parent else arker_client(config).fork(**options)
        except ArkerError as error:
            if error.status == 404 and source:
                raise errors.not_found(
                    f"No Arker VM named {source!r} to start from. Name a VM you have prepared, "
                    "or omit it to start from the default image."
                ) from error
            raise _provider_error(errors, error) from error
        sandbox = cls(vm, errors)
        new_envs = {**(envs or {}), **({id_env: vm.id} if id_env else {})}
        try:
            if new_envs:
                inherited = sandbox.env() if forks_vm else {}
                sandbox.write(ENV_FILE, _exports(errors, {**inherited, **new_envs}))
            for path, volume_id in (volumes or {}).items():
                sandbox.mount(volume_id, path)
        except BaseException:
            sandbox.discard()
            raise
        return sandbox

    @classmethod
    def find(
        cls, errors: Errors, config: dict[str, Any] | None = None, *, snapshots: bool = False
    ) -> Iterator[CompatSandbox]:
        """The sandboxes (or, with ``snapshots``, the snapshots) this provider's layer created."""
        client = arker_client(config)
        layer = errors.provider.lower()
        # VMs created while paging can shift a later page onto ones already seen.
        seen: set[str] = set()
        try:
            for vm in _paged(lambda cursor: client.list_vms(cursor=cursor, limit=LIST_PAGE_SIZE), "vms"):
                described = _description(vm)
                if vm.id in seen or described.get("layer") != layer or ("snapshot_of" in described) != snapshots:
                    continue
                seen.add(vm.id)
                yield cls(vm, errors)
        except ArkerError as error:
            raise _provider_error(errors, error) from error

    def fork(self, **options: Any) -> CompatSandbox:
        """A new sandbox from this one's disk and memory, with its metadata, envs,
        exposed ports and outbound rules, as a copy of the sandbox would have.
        ``options`` are :meth:`create`'s."""
        options.setdefault("metadata", self.metadata())
        options.setdefault("on_timeout", self._on_timeout)
        options.setdefault("port_auth", self.port_auth)
        return CompatSandbox.create(self._errors, parent=self.vm, keep_network=True, **options)

    def snapshot(self, name: str, delete_after: float | None = None) -> CompatSandbox:
        """A copy of this sandbox that new sandboxes can start from by its name,
        deleted ``delete_after`` seconds from now when given."""
        return self.fork(name=name, snapshot_of=self.id, delete_after=delete_after, on_timeout="delete")

    @classmethod
    def connect(cls, errors: Errors, vm_id: str, config: dict[str, Any] | None = None) -> CompatSandbox | None:
        """The sandbox with id or name ``vm_id``, or None when Arker has no such VM."""
        try:
            vm = arker_client(config).get_vm(vm_id)
            vm.id = vm.vm_id or vm.id
            return cls(vm, errors)
        except ArkerError as error:
            if error.status in (400, 404):
                return None
            raise _provider_error(errors, error) from error

    def metadata(self) -> dict[str, Any]:
        return _description(self.info()).get("compat") or {}

    @_translated
    def info(self) -> VM:
        return self.vm.refresh()

    def discard(self) -> None:
        """Delete the VM while another error is on its way out, without hiding that error."""
        with contextlib.suppress(Exception):
            self.vm.delete()

    @_translated
    def is_alive(self) -> bool:
        try:
            self.vm.refresh()
        except ArkerError as error:
            if error.status == 404:
                return False
            raise
        return True

    @_translated
    def delete(self) -> bool:
        """False when the VM was already gone. Deleting a missing VM succeeds
        on Arker, so check that it exists first."""
        if not self.is_alive():
            return False
        try:
            self.vm.delete()
        except ArkerError as error:
            if error.status == 404:
                return False
            raise
        return True

    @_translated
    def set_timeout(self, seconds: float) -> None:
        at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        self.vm.update(policies={"rules": {TIMEOUT_RULE: _schedule_rule(self._on_timeout, at=at)}})

    @_translated
    def mount(self, volume_id: str, path: str) -> None:
        _mount(self, volume_id, path)

    @_translated
    def restrict_egress(self, egress: Egress) -> None:
        self.vm.update(policies={"rules": egress_policy(egress)})

    @_translated
    def clear_timeout(self) -> None:
        self.vm.update(policies={"rules": {TIMEOUT_RULE: None}})

    @_translated
    def suspend(self) -> None:
        """Suspend the VM now if it is running. It resumes when next used."""
        rule = _schedule_rule("suspend", at=datetime.now(timezone.utc))
        self.vm.update(policies={"rules": {SUSPEND_RULE: rule}})

    def _rules(self, prefix: str) -> dict[str, Any]:
        policies = self.info().policies
        rules = (policies.rules if policies else None) or {}
        return {rule_id: rule for rule_id, rule in rules.items() if rule_id.startswith(prefix)}

    @_translated
    def update_egress(self, egress: Egress | None) -> None:
        """Replace the outbound rules this layer set; None allows all traffic."""
        rules: dict[str, Any] = dict.fromkeys(self._rules(f"{EGRESS_RULE}-"))
        rules.update(egress_policy(egress) if egress else {})
        if rules:
            self.vm.update(policies={"rules": rules})

    def set_ports_public(self, public: bool) -> None:
        """Change who may reach the ports already exposed, keeping the addresses each admits."""
        for rule_id, rule in self._rules(f"{PORT_RULE}-").items():
            from_ips = (rule.match.ips if rule.match else None) or ()
            self.expose(int(rule_id.rpartition("-")[2]), public=public, from_ips=from_ips)

    @_translated
    def update_metadata(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Merge ``changes`` into the sandbox metadata and return all of it."""
        described = _description(self.info())
        described["compat"] = {**(described.get("compat") or {}), **changes}
        self.vm.update(description=_description_json(self._errors, described))
        self.settings = described["compat"]
        return self.settings

    @_translated
    def resize(self, *, cpus: int | None = None, memory_mib: int | None = None, disk_mib: int | None = None) -> None:
        self.vm.update(vcpu=cpus, memory_mib=memory_mib, disk_mib=disk_mib)

    def env(self) -> dict[str, str]:
        """The environment variables every command in the sandbox sees."""
        try:
            text = self.read(ENV_FILE).decode()
        except self._errors.file_not_found:
            return {}
        try:
            # The whole file, not line by line: a quoted value may span lines.
            words = shlex.split(text)
        except ValueError as error:
            raise self._errors.failed(f"{ENV_FILE} is not a list of exports: {error}") from error
        pairs = (word.partition("=") for word in words if word != "export")
        return {name: value for name, _, value in pairs}

    def update_env(self, envs: dict[str, str], unset: Iterable[str] = ()) -> None:
        current = {name: value for name, value in self.env().items() if name not in set(unset)}
        self.write(ENV_FILE, _exports(self._errors, {**current, **envs}))

    def home_dir(self) -> str:
        return self._shell('printf %s "$HOME"')

    def work_dir(self) -> str:
        """The directory commands start in when given none."""
        return self._shell("pwd").strip()

    @_translated
    def expose(self, port: int, *, public: bool = True, from_ips: Iterable[str] = ()) -> str:
        """Open ``port`` (to callers in ``from_ips`` when given) and return its host
        name. A non-public port needs ``Authorization: Bearer $ARKER_API_KEY`` from
        the VM owner's organization."""
        match: dict[str, Any] = {"ports": [port], **({"ips": list(from_ips)} if from_ips else {})}
        if self.port_auth:
            match["headers"] = {self.port_auth["header"]: [self.port_auth["token"]]}
        rule = {
            "type": "inbound",
            "priority": PORT_RULE_PRIORITY + port,
            "match": match,
            "action": "allow",
            "auth": "open" if public else "arker",
        }
        self.vm.update(policies={"rules": {f"{PORT_RULE}-{port}": rule}})
        return self.port_host(port)

    def port_host(self, port: int) -> str:
        hostname = self.vm.hostname or self.vm.refresh().hostname
        if not hostname:
            raise self._errors.failed("This Arker region does not expose guest ports.")
        vm_label, _, domain = hostname.partition(".")
        return f"{vm_label}-{port}.{domain}"

    def _grant(self, kind: str, port: int, token: str, match: dict[str, Any], expires_in: float | None) -> None:
        """Let requests to ``port`` that meet ``match`` in, for ``expires_in`` seconds
        when given. Grants that have expired are removed as this one is added."""
        policies = self.info().policies
        rules = (policies.rules if policies else None) or {}
        now = datetime.now(timezone.utc)
        changes: dict[str, Any] = {
            rule_id: None
            for rule_id, rule in rules.items()
            if rule_id.startswith(GRANT_RULE) and rule.expires_at and parse_time(rule.expires_at) <= now
        }
        used = {rule.priority for rule in rules.values() if rule.type == "inbound"}
        rule: dict[str, Any] = {
            "type": "inbound",
            "priority": next(priority for priority in itertools.count(GRANT_PRIORITY) if priority not in used),
            "match": {"ports": [port], **match},
            "action": "allow",
        }
        if expires_in:
            rule["expires_at"] = (now + timedelta(seconds=expires_in)).isoformat()
        changes[_grant_id(kind, port, token)] = rule
        self.vm.update(policies={"rules": changes})

    def _revoke(self, prefix: str) -> None:
        stale = dict.fromkeys(self._rules(prefix))
        if stale:
            self.vm.update(policies={"rules": stale})

    @_translated
    def file_url(self, path: str, *, write: bool, expires_in: float | None = None) -> str:
        """A URL that needs no credentials: a GET of it downloads ``path``, or with
        ``write`` a POST of it (the file, or a multipart/form-data body holding it)
        uploads to ``path``. It works for ``expires_in`` seconds when given, else
        until :meth:`revoke_file_urls`; forks and snapshots do not inherit it."""
        if not path.startswith("/"):
            raise self._errors.invalid(f"A file URL needs an absolute path, not {path!r}.")
        self._serve_files()
        token = secrets.token_urlsafe(32)
        match = {
            "methods": ["POST" if write else "GET"],
            "paths": ["/files"],
            "query_params": {"path": [path], "signature": [token]},
        }
        self._grant("file", FILE_SERVER_PORT, token, match, expires_in)
        query = urllib.parse.urlencode({"path": path, "signature": token}, quote_via=urllib.parse.quote)
        return f"https://{self.port_host(FILE_SERVER_PORT)}/files?{query}"

    @_translated
    def revoke_file_urls(self) -> None:
        """Make every URL from :meth:`file_url` stop working."""
        self._revoke(f"{GRANT_RULE}-file-")

    def _serve_files(self) -> None:
        """Start the server behind file URLs, unless it is running."""
        with open(os.path.join(os.path.dirname(__file__), "_file_server.py"), "rb") as server:
            self.write(FILE_SERVER_PATH, server.read())
        listening = f"(exec 3<>/dev/tcp/127.0.0.1/{FILE_SERVER_PORT}) 2>/dev/null"
        self._shell(
            f"{listening} && exit 0\n"
            f"setsid nohup python3 {FILE_SERVER_PATH} {FILE_SERVER_PORT} > /dev/null 2>&1 < /dev/null &\n"
            f"for _ in $(seq 100); do {listening} && exit 0; sleep 0.1; done\n"
            "echo 'The file server did not start.' >&2; exit 1"
        )

    @_translated
    def signed_port_url(self, port: int, expires_in: float | None) -> tuple[str, str]:
        """A URL to ``port`` that needs no other credential, and its token; see :meth:`revoke_port_url`."""
        token = secrets.token_urlsafe(32)
        self._grant("port", port, token, {"query_params": {"arker_token": [token]}}, expires_in)
        return f"https://{self.port_host(port)}/?arker_token={token}", token

    @_translated
    def revoke_port_url(self, port: int, token: str) -> None:
        self._revoke(_grant_id("port", port, token))

    def _private_file(self, data: str) -> str:
        """A new file only root can read, for values kept out of a command's text,
        which the runs API returns to anyone in the organization who can list runs."""
        path = f"{PRIVATE_DIR}/envs/{uuid.uuid4().hex}"
        self.write(path, data)
        return path

    def _load_envs(self, envs: dict[str, str] | None) -> str:
        """Shell lines exporting ``envs`` from a private file, removed once read."""
        if not envs:
            return ""
        path = self._private_file(_exports(self._errors, envs))
        return f". {path}; rm -f {path}\n"

    def _script(self, cmd: str, cwd: str | None, envs: dict[str, str] | None, prelude: str = "") -> str:
        # A subshell, so cd and exports never leak into the next command.
        cd = f"cd {shlex.quote(cwd)} || exit 1\n" if cwd else ""
        return f"(\n[ -f {ENV_FILE} ] && . {ENV_FILE}\n{self._load_envs(envs)}{prelude}{cd}{cmd}\n)"

    @_translated
    def run(
        self, cmd: str, *, cwd: str | None = None, envs: dict[str, str] | None = None, timeout: float | None = None
    ) -> CommandResult:
        """Run ``cmd`` in bash and wait for it. A non-zero exit is a result, not an error."""
        done = self.vm.run(self._script(cmd, cwd, envs), timeout=_kill_bound(timeout))
        return self._finish(done.stdout, done.stderr, done.exit_code, done.state, done.fail_reason, timeout)

    @_translated
    def start(
        self,
        cmd: str,
        *,
        cwd: str | None = None,
        envs: dict[str, str] | None = None,
        timeout: float | None = None,
        stdin: bool = False,
    ) -> Process:
        """Start ``cmd`` in a session of its own, so other commands do not wait behind it.
        With ``stdin``, its standard input is a pipe fed by :meth:`send_stdin`."""
        session = self.vm.create_session()
        directory = _process_dir(session.session_id)
        self.write(f"{directory}/info", json.dumps({"cmd": cmd, "cwd": cwd, "envs": envs or {}}))
        prelude = f"echo $BASHPID > {directory}/pid || exit 1\n"
        # The info holds the envs' values, so it goes when the command ends.
        cleanup = f"rm -f {directory}/info; touch {directory}/done"
        if stdin:
            cleanup = f"{_release_stdin(directory)}; {cleanup}"
            cmd = f"{{\n{cmd}\n}} < {directory}/stdin"
        prelude += f"trap {shlex.quote(cleanup)} EXIT\n" + (_open_stdin(directory) if stdin else "")
        started = self.vm.run(
            self._script(cmd, cwd, envs, prelude),
            session_id=session.session_id,
            timeout=_kill_bound(timeout),
            time_to_background=0,
        )
        return Process(self, started.run_id, session.session_id, timeout, stdin=stdin)

    def _process_pid(self, session_id: str) -> int | None:
        pid = self.run(f"cat {_process_dir(session_id)}/pid 2>/dev/null").stdout.strip()
        return int(pid) if pid else None

    def processes(self) -> list[ProcessInfo]:
        """Commands and terminals this layer started that are still running."""
        listing = self._shell(
            f"cd {PROCESS_DIR} 2>/dev/null || exit 0\n"
            "for s in *; do [ -e $s/done ] && continue; p=$(cat $s/pid 2>/dev/null) && kill -0 $p 2>/dev/null"
            " || continue; printf '%s\\t%s\\n' $p $s; done"
        )
        found = []
        for line in listing.splitlines():
            pid, session_id = line.split("\t")
            try:
                # Read apart from the listing, whose output the runs API keeps.
                details = json.loads(self.read(f"{_process_dir(session_id)}/info"))
            except self._errors.file_not_found:
                continue
            found.append(
                ProcessInfo(
                    pid=int(pid),
                    session_id=session_id,
                    cmd=details["cmd"],
                    cwd=details.get("cwd"),
                    envs=details.get("envs") or {},
                    pty=bool(details.get("pty")),
                    name=details.get("name"),
                    size=tuple(details["size"]) if details.get("size") else None,
                )
            )
        return found

    def find_process(self, pid: int) -> ProcessInfo | None:
        return next((info for info in self.processes() if info.pid == pid), None)

    @_translated
    def attach(self, pid: int) -> Process | None:
        """The running command ``pid``, or None if no such command runs."""
        info = self.find_process(pid)
        if info is None or info.pty:
            return None
        run = next((run for run in self._runs(state="running") if run.session_id == info.session_id), None)
        return Process(self, run.run_id, info.session_id, None) if run else None

    @_translated
    def kill_session(self, session_id: str) -> bool:
        """End the session and everything running in it; False if it was gone."""
        try:
            self.vm.delete_session(session_id)
        except ArkerError as error:
            if error.status == 404:
                return False
            raise
        return True

    def kill_process(self, pid: int) -> bool:
        info = self.find_process(pid)
        return info is not None and self.kill_session(info.session_id)

    def send_stdin(self, session_id: str, data: str | bytes) -> bool:
        """Write to the stdin pipe of the command in the session; False if it has none open."""
        encoded = base64.b64encode(data.encode() if isinstance(data, str) else bytes(data)).decode()
        return self._piped(session_id, f"echo {encoded} | base64 -d > {_process_dir(session_id)}/stdin")

    def close_stdin(self, session_id: str) -> bool:
        return self._piped(session_id, _release_stdin(_process_dir(session_id)))

    def _piped(self, session_id: str, script: str) -> bool:
        result = self.run(f"[ -p {_process_dir(session_id)}/stdin ] || exit 44; {script}")
        if result.exit_code == 44:
            return False
        if result.exit_code != 0:
            raise self._errors.failed(result.stderr.strip() or f"stdin failed with exit {result.exit_code}")
        return True

    @_translated
    def _runs(self, state: str | None = None) -> list[Any]:
        return list(_paged(lambda cursor: self.vm.list_runs(cursor=cursor, state=state), "runs"))

    @_translated
    def open_terminal(
        self,
        *,
        cols: int,
        rows: int,
        cwd: str | None = None,
        envs: dict[str, str] | None = None,
        on_data: Callable[[bytes], None] | None = None,
        name: str | None = None,
    ) -> Terminal:
        """Start an interactive shell in a session of its own and attach to it."""
        session = self.vm.create_session(cwd=cwd)
        directory = _process_dir(session.session_id)
        info = {"cmd": "/bin/bash", "cwd": cwd, "envs": envs or {}, "pty": True, "name": name, "size": [cols, rows]}
        self.write(f"{directory}/info", json.dumps(info))
        # The session's shell is the terminal's: what this run sets up, the terminal keeps.
        setup = (
            f"echo $$ > {directory}/pid\n"
            f"trap {shlex.quote(f'echo $? > {directory}/exit; rm -f {directory}/info')} EXIT\n"
            f"[ -f {ENV_FILE} ] && . {ENV_FILE}\n{self._load_envs(envs)}echo $$"
        )
        pid = int(self.vm.run(setup, session_id=session.session_id).stdout.strip())
        return Terminal(self, session.session_id, pid, cols=cols, rows=rows, on_data=on_data)

    def attach_terminal(self, info: ProcessInfo, on_data: Callable[[bytes], None] | None = None) -> Terminal:
        return Terminal(self, info.session_id, info.pid, on_data=on_data)

    @_translated
    def resize_terminal(self, session_id: str, cols: int, rows: int) -> None:
        self.vm.update_session(session_id, cols=cols, rows=rows)
        info_path = f"{_process_dir(session_id)}/info"
        info = json.loads(self.read(info_path))
        self.write(info_path, json.dumps({**info, "size": [cols, rows]}))

    @_translated
    def named_sessions(self) -> dict[str, str]:
        """Sessions made by :meth:`create_named_session`, by name, to their Arker session id."""
        named: dict[str, str] = {}
        for session in _paged(lambda cursor: self.vm.list_sessions(cursor=cursor), "sessions"):
            name = (session.env or {}).get(SESSION_NAME_ENV)
            if name:
                named[name] = session.session_id
        return named

    @_translated
    def create_named_session(self, name: str) -> str:
        """A shell that keeps its directory and variables from one command to the next."""
        if name in self.named_sessions():
            raise self._errors.invalid(f"Session {name!r} already exists.")
        session = self.vm.create_session(env={SESSION_NAME_ENV: name})
        self.vm.run(_SESSION_SETUP, session_id=session.session_id)
        return session.session_id

    @_translated
    def run_in_session(self, session_id: str, cmd: str, *, timeout: float | None = None) -> tuple[str, CommandResult]:
        """Run ``cmd`` in the session's shell and wait; returns the run id and result."""
        done = self.vm.run(cmd, session_id=session_id, timeout=_kill_bound(timeout))
        result = self._finish(done.stdout, done.stderr, done.exit_code, done.state, done.fail_reason, timeout)
        return done.run_id or "", result

    @_translated
    def start_in_session(self, session_id: str, cmd: str) -> Process:
        """Start ``cmd`` in the session's shell, its stdin a pipe fed by :meth:`send_stdin`."""
        started = self.vm.run(_piped(_process_dir(session_id), cmd), session_id=session_id, time_to_background=0)
        return Process(self, started.run_id, session_id, None, owns_session=False, stdin=True)

    def session_process(self, session_id: str, run_id: str) -> Process:
        return Process(self, run_id, session_id, None, owns_session=False, stdin=True)

    def session_commands(self, session_id: str) -> list[tuple[str, str, int | None]]:
        """``(run id, command, exit code)`` of each command run in the session, oldest first."""
        before, _, after = _piped(_process_dir(session_id), "\0").partition("\0")
        commands = []
        for run in sorted(self._runs(), key=lambda run: run.started_at):
            if run.session_id != session_id or run.command == _SESSION_SETUP:
                continue
            command = run.command or ""
            if command.startswith(before) and command.endswith(after):
                command = command[len(before) : -len(after)]
            commands.append((run.run_id, command, run.exit_code))
        return commands

    def _terminal_exit_code(self, session_id: str) -> int | None:
        code = self.run(f"cat {_process_dir(session_id)}/exit 2>/dev/null").stdout.strip()
        return int(code) if code else None

    def _finish(
        self,
        stdout: str,
        stderr: str,
        exit_code: int | None,
        state: str,
        fail_reason: str | None,
        timeout: float | None,
    ) -> CommandResult:
        if state == "failed":
            raise self._errors.failed(fail_reason or "The command could not be run.")
        if state == "cancelled" or exit_code is None:
            raise self._errors.failed("The command was killed.")
        if timeout and exit_code == 124 and _TIMED_OUT.search(stderr):
            raise self._errors.timeout(f"Command timed out after {timeout}s.")
        return CommandResult(stdout=stdout, stderr=stderr, exit_code=exit_code)

    def _shell(self, script: str) -> str:
        """Run a helper script this layer wrote; any failure is the layer's error."""
        result = self.run(script)
        if result.exit_code != 0:
            raise self._errors.failed(result.stderr.strip() or f"exit {result.exit_code}")
        return result.stdout

    @_translated
    def read(self, path: str) -> bytes:
        try:
            return self.vm.sync(path) or b""
        except ArkerError as error:
            if error.status == 404:
                raise self._errors.file_not_found(f"File not found: {path}") from error
            raise

    @_translated
    def write(self, path: str, data: bytes | str) -> None:
        """Write ``data`` to ``path``, creating parent directories."""
        self.vm.sync(path, data)

    @_translated
    def upload(self, local_path: str, remote_path: str, *, ignore: Callable[[str], bool] | None = None) -> None:
        """Copy a local file, or a directory's contents less what ``ignore``
        rejects (by ``/``-separated relative path), to ``remote_path``."""
        if os.path.isdir(local_path):
            self.vm.sync_dir(local_path, remote_path, ignore=ignore)
            return
        with open(local_path, "rb") as handle:
            self.vm.sync(remote_path, handle.read())

    def list(self, path: str, depth: int | None = 1) -> list[Entry]:
        """Entries under directory ``path``, ``depth`` levels deep (every level when None)."""
        quoted = shlex.quote(path)
        max_depth = f"-maxdepth {int(depth)} " if depth else ""
        result = self.run(f"[ -d {quoted} ] || exit 44; find {quoted} -mindepth 1 {max_depth}-printf {_FIND_FORMAT}")
        if result.exit_code == 44:
            raise self._errors.file_not_found(f"Directory not found: {path}")
        if result.exit_code != 0:
            raise self._errors.failed(result.stderr.strip() or f"list failed with exit {result.exit_code}")
        return [_entry(line) for line in result.stdout.split("\0") if line]

    def stat(self, path: str) -> Entry:
        quoted = shlex.quote(path)
        exists = f"[ -e {quoted} ] || [ -L {quoted} ] || exit 44"
        result = self.run(f"{exists}; find {quoted} -maxdepth 0 -printf {_FIND_FORMAT}")
        if result.exit_code == 44:
            raise self._errors.file_not_found(f"No such file or directory: {path}")
        if result.exit_code != 0:
            raise self._errors.failed(result.stderr.strip() or f"stat failed with exit {result.exit_code}")
        return _entry(result.stdout.rstrip("\0"))

    def changes(self, path: str, before: dict[str, Entry], *, recursive: bool) -> tuple[list[Change], dict[str, Entry]]:
        """What changed under ``path`` since ``before`` (an earlier result's second
        element, or ``{}`` to start), and the entries now there."""
        after = {entry.path: entry for entry in self.list(path, None if recursive else 1)}
        found = [Change(entry_path, "remove") for entry_path in before if entry_path not in after]
        for entry_path, entry in after.items():
            old = before.get(entry_path)
            if old is None:
                found.append(Change(entry_path, "create"))
            elif (old.size, old.modified) != (entry.size, entry.modified):
                found.append(Change(entry_path, "write"))
            elif old.mode != entry.mode:
                found.append(Change(entry_path, "chmod"))
        return found, after

    def path_exists(self, path: str) -> bool:
        return self.run(f"test -e {shlex.quote(path)}").exit_code == 0

    def is_dir(self, path: str) -> bool:
        return self.run(f"test -d {shlex.quote(path)}").exit_code == 0

    def make_dir(self, path: str, *, parents: bool = True) -> bool:
        """Create ``path`` (and, with ``parents``, its parents); False if it already existed."""
        quoted = shlex.quote(path)
        mkdir = "mkdir -p" if parents else "mkdir"
        return self._shell(f"[ -d {quoted} ] && echo existed || {mkdir} {quoted}").strip() != "existed"

    def remove(self, path: str, *, recursive: bool = True) -> None:
        """Remove ``path``; without ``recursive``, a non-empty directory is an error."""
        quoted = shlex.quote(path)
        if recursive:
            self._shell(f"rm -rf {quoted}")
            return
        result = self.run(f"[ -e {quoted} ] || exit 44; if [ -d {quoted} ]; then rmdir {quoted}; else rm {quoted}; fi")
        if result.exit_code == 44:
            raise self._errors.file_not_found(f"No such file or directory: {path}")
        if result.exit_code != 0:
            raise self._errors.failed(result.stderr.strip() or f"remove failed with exit {result.exit_code}")

    def rename(self, old_path: str, new_path: str) -> None:
        self._shell(f"mv {shlex.quote(old_path)} {shlex.quote(new_path)}")


def parse_time(text: str) -> datetime:
    """An API timestamp; ``fromisoformat`` takes a trailing ``Z`` only from Python 3.11."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _description_json(errors: Errors, described: dict[str, Any]) -> str:
    text = json.dumps(described)
    if len(text) > MAX_DESCRIPTION:
        raise errors.invalid(f"Sandbox metadata is too large: at most {MAX_DESCRIPTION} characters as JSON.")
    return text


# Type, size, permission bits, owner, group, mtime, link target, path; NUL-ended
# since paths may hold tabs and newlines.
_FIND_FORMAT = "'%y\\t%s\\t%m\\t%u\\t%g\\t%T@\\t%l\\t%p\\0'"


def _entry(line: str) -> Entry:
    kind, size, mode, owner, group, modified, target, path = line.split("\t", 7)
    return Entry(
        name=posixpath.basename(path.rstrip("/")) or path,
        path=path,
        is_dir=kind == "d",
        size=int(size),
        mode=int(mode, 8),
        owner=owner,
        group=group,
        modified=float(modified),
        symlink_target=target or None,
    )


def _source_vm(errors: Errors, config: dict[str, Any] | None, parent: VM | None, source: str | None) -> VM | None:
    """The VM a sandbox is forked from, as it is now; None when there is no such
    VM, which the fork itself then reports."""
    try:
        return parent.refresh() if parent else arker_client(config).get_vm(source or arker_source(config))
    except ArkerError as error:
        if error.status in (400, 404):
            return None
        raise _provider_error(errors, error) from error


def _layer_rule_ids(vm: VM | None) -> list[str]:
    rules = vm.policies.rules if vm and vm.policies else None
    prefixes = (TIMEOUT_RULE, SUSPEND_RULE, f"{EGRESS_RULE}-", f"{PORT_RULE}-", f"{GRANT_RULE}-")
    return [rule_id for rule_id in rules or {} if rule_id.startswith(prefixes)]


def _description(vm: VM) -> dict[str, Any]:
    try:
        parsed = json.loads(vm.description or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _exports(errors: Errors, envs: dict[str, str]) -> str:
    invalid = [name for name in envs if not _ENV_NAME.match(name)]
    if invalid:
        raise errors.invalid(f"Invalid environment variable name(s): {', '.join(map(repr, invalid))}")
    return "".join(f"export {name}={shlex.quote(str(value))}\n" for name, value in envs.items())


def _kill_bound(timeout: float | None) -> int | None:
    return math.ceil(timeout) if timeout else None


# Git: every provider's git module, on the git CLI in the VM.

# Never wait for a password prompt no one can answer.
GIT = "GIT_TERMINAL_PROMPT=0 git"
# Answers git's credential request from the environment, so a password is
# never written to disk or into a remote URL.
_CREDENTIAL_HELPER = '!f() { echo "username=$ARKER_GIT_USERNAME"; echo "password=$ARKER_GIT_PASSWORD"; }; f'


@dataclasses.dataclass(frozen=True)
class GitFileChange:
    name: str
    index_status: str
    worktree_status: str
    renamed_from: str | None = None


@dataclasses.dataclass(frozen=True)
class GitState:
    current_branch: str | None
    upstream: str | None
    ahead: int
    behind: int
    detached: bool
    files: list[GitFileChange]


def _parse_git_status(output: str) -> GitState:
    """Parse ``git status --porcelain=1 --branch -z``: NUL-separated, paths
    unquoted, and a rename's source as the entry after it."""
    entries = output.split("\0")
    branch, upstream, ahead, behind, detached = None, None, 0, 0, False
    if entries[0].startswith("## "):
        head, _, counts = entries.pop(0)[3:].partition(" [")
        head = head.removeprefix("No commits yet on ").removeprefix("Initial commit on ")
        if head.startswith("HEAD"):
            detached = True
        else:
            branch, _, upstream = head.partition("...")
            upstream = upstream or None
        for part in counts.rstrip("]").split(", "):
            word, _, number = part.partition(" ")
            if word == "ahead":
                ahead = int(number)
            elif word == "behind":
                behind = int(number)
    files = []
    rest = iter(entries)
    for entry in rest:
        if entry:
            renamed_from = next(rest) if "R" in entry[:2] or "C" in entry[:2] else None
            files.append(GitFileChange(entry[3:], entry[0], entry[1], renamed_from))
    return GitState(branch, upstream, ahead, behind, detached, files)


class CompatGit:
    """Git operations in a sandbox. A failed git command raises ``fail(result)``,
    or the provider's error naming git's own message."""

    def __init__(
        self,
        sandbox: CompatSandbox,
        cwd: str | None = None,
        fail: Callable[[CommandResult], Exception] | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._cwd = cwd
        self._fail = fail or (lambda result: sandbox._errors.failed(result.stderr.strip() or result.stdout.strip()))

    def run(
        self,
        args: list[str],
        path: str | None = None,
        *,
        username: str | None = None,
        password: str | None = None,
        envs: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        check: bool = True,
    ) -> CommandResult:
        """``git [-C path] args``; with ``username``/``password``, git asks for them only through the environment."""
        envs = dict(envs or {})
        options = ["-C", path] if path else []
        if username is not None or password is not None:
            options += ["-c", "credential.helper=", "-c", f"credential.helper={_CREDENTIAL_HELPER}"]
            envs.update(ARKER_GIT_USERNAME=username or "", ARKER_GIT_PASSWORD=password or "")
        result = self._sandbox.run(
            f"{GIT} {shlex.join(options + args)}", cwd=cwd or self._cwd, envs=envs, timeout=timeout
        )
        if check and result.exit_code != 0:
            raise self._fail(result)
        return result

    def clone(
        self,
        url: str,
        path: str | None = None,
        *,
        branch: str | None = None,
        depth: int | None = None,
        commit: str | None = None,
        insecure_skip_tls: bool = False,
        username: str | None = None,
        password: str | None = None,
        **options: Any,
    ) -> CommandResult:
        args = (["-c", "http.sslVerify=false"] if insecure_skip_tls else []) + ["clone"]
        args += ["--branch", self._ref(branch)] if branch else []
        args += ["--depth", str(depth)] if depth else []
        args += ["--", url] + ([path] if path else [])
        result = self.run(args, username=username, password=password, **options)
        if commit:
            target = path or posixpath.basename(url.rstrip("/")).removesuffix(".git")
            self.run(["checkout", self._ref(commit)], target, **options)
        return result

    def _ref(self, name: str) -> str:
        """``name``, refused if git would read it as an option."""
        if name.startswith("-"):
            raise self._sandbox._errors.invalid(f"Invalid git branch or ref {name!r}: it starts with '-'.")
        return name

    def init(
        self, path: str, *, bare: bool = False, initial_branch: str | None = None, **options: Any
    ) -> CommandResult:
        args = (
            ["init"] + (["--bare"] if bare else []) + (["--initial-branch", initial_branch] if initial_branch else [])
        )
        return self.run([*args, path], **options)

    def status(self, path: str, **options: Any) -> GitState:
        return _parse_git_status(self.run(["status", "--porcelain=1", "--branch", "-z"], path, **options).stdout)

    def branches(self, path: str, **options: Any) -> tuple[list[str], str | None]:
        """Local branch names, and the checked-out one."""
        output = self.run(["branch", "--format=%(HEAD) %(refname:short)"], path, **options).stdout
        lines = [line for line in output.splitlines() if line.strip()]
        current = next((line[2:] for line in lines if line.startswith("* ")), None)
        return [line[2:] for line in lines], current

    def create_branch(self, path: str, name: str, **options: Any) -> CommandResult:
        return self.run(["checkout", "-b", self._ref(name)], path, **options)

    def checkout_branch(self, path: str, name: str, **options: Any) -> CommandResult:
        return self.run(["checkout", self._ref(name)], path, **options)

    def delete_branch(self, path: str, name: str, *, force: bool = False, **options: Any) -> CommandResult:
        return self.run(["branch", "-D" if force else "-d", self._ref(name)], path, **options)

    def add(self, path: str, files: list[str] | None = None, **options: Any) -> CommandResult:
        return self.run(["add", "--", *files] if files else ["add", "--all"], path, **options)

    def commit(
        self,
        path: str,
        message: str,
        *,
        author_name: str | None = None,
        author_email: str | None = None,
        allow_empty: bool = False,
        **options: Any,
    ) -> CommandResult:
        identity = []
        if author_name:
            identity += ["-c", f"user.name={author_name}"]
        if author_email:
            identity += ["-c", f"user.email={author_email}"]
        return self.run(
            [*identity, "commit", "-m", message] + (["--allow-empty"] if allow_empty else []), path, **options
        )

    def reset(
        self,
        path: str,
        *,
        mode: str | None = None,
        target: str | None = None,
        paths: list[str] | None = None,
        **options: Any,
    ) -> CommandResult:
        args = ["reset"] + ([f"--{mode}"] if mode else []) + ([self._ref(target)] if target else [])
        return self.run(args + (["--", *paths] if paths else []), path, **options)

    def restore(
        self,
        path: str,
        paths: list[str],
        *,
        staged: bool | None = None,
        worktree: bool | None = None,
        source: str | None = None,
        **options: Any,
    ) -> CommandResult:
        args = ["restore"] + (["--staged"] if staged else []) + (["--worktree"] if worktree else [])
        return self.run(args + ([f"--source={source}"] if source else []) + ["--", *paths], path, **options)

    def push(
        self,
        path: str,
        *,
        remote: str | None = None,
        branch: str | None = None,
        set_upstream: bool = False,
        **options: Any,
    ) -> CommandResult:
        if set_upstream or branch:
            upstream = ["--set-upstream"] if set_upstream else []
            return self.run(
                ["push", *upstream, self._ref(remote or "origin"), self._ref(branch or "HEAD")], path, **options
            )
        return self.run(["push"] + ([self._ref(remote)] if remote else []), path, **options)

    def pull(self, path: str, *, remote: str | None = None, branch: str | None = None, **options: Any) -> CommandResult:
        return self.run(
            ["pull"]
            + ([self._ref(remote or "origin")] if remote or branch else [])
            + ([self._ref(branch)] if branch else []),
            path,
            **options,
        )

    def remote_add(
        self, path: str, name: str, url: str, *, fetch: bool = False, overwrite: bool = False, **options: Any
    ) -> CommandResult:
        exists = overwrite and self.remote_get(path, name, **options) is not None
        result = self.run(["remote", "set-url" if exists else "add", name, url], path, **options)
        return self.run(["fetch", name], path, **options) if fetch else result

    def remote_get(self, path: str, name: str, **options: Any) -> str | None:
        result = self.run(["remote", "get-url", name], path, check=False, **options)
        return result.stdout.strip() if result.exit_code == 0 else None

    def remotes(self, path: str, **options: Any) -> list[tuple[str, str]]:
        """``(name, url)`` per remote."""
        output = self.run(["remote", "-v"], path, **options).stdout
        return [(line.split()[0], line.split()[1]) for line in output.splitlines() if line.endswith("(fetch)")]

    def set_config(
        self, key: str, value: str, *, scope: str = "global", path: str | None = None, **options: Any
    ) -> CommandResult:
        return self.run(["config", _config_scope(self._sandbox, scope, path), key, value], path, **options)

    def get_config(self, key: str, *, scope: str = "global", path: str | None = None, **options: Any) -> str | None:
        args = ["config", _config_scope(self._sandbox, scope, path), "--get", key]
        result = self.run(args, path, check=False, **options)
        return result.stdout.strip() if result.exit_code == 0 else None

    def configure_user(
        self, name: str, email: str, *, scope: str = "global", path: str | None = None, **options: Any
    ) -> CommandResult:
        self.set_config("user.name", name, scope=scope, path=path, **options)
        return self.set_config("user.email", email, scope=scope, path=path, **options)

    def dangerously_authenticate(
        self, username: str, password: str, *, host: str = "github.com", protocol: str = "https", **options: Any
    ) -> CommandResult:
        """Store the credentials in plain text in the VM, for every later git command."""
        self.set_config("credential.helper", "store", **options)
        credential = f"protocol={protocol}\nhost={host}\nusername={username}\npassword={password}\n"
        envs = {**(options.pop("envs", None) or {}), "ARKER_GIT_CREDENTIAL": credential}
        script = f'printf %s "$ARKER_GIT_CREDENTIAL" | {GIT} credential approve'
        result = self._sandbox.run(script, cwd=options.get("cwd") or self._cwd, envs=envs)
        if result.exit_code != 0:
            raise self._fail(result)
        return result


def _config_scope(sandbox: CompatSandbox, scope: str, path: str | None) -> str:
    if scope not in ("global", "local", "system"):
        raise sandbox._errors.invalid(f"Git config scope must be global, local or system, not {scope!r}.")
    if scope == "local" and not path:
        raise sandbox._errors.invalid("A local git config scope needs the repository path.")
    return f"--{scope}"


# Code contexts: stateful Python interpreters, one per Arker session, for
# every provider's code interpreter. A context's id is its session's id.

INTERPRETER_DIR = "/opt/arker-compat"
INTERPRETER_READY = "<arker-interpreter-ready>"
# Marks an interpreter's session, and whether it is the sandbox's default context.
INTERPRETER_ENV = "ARKER_INTERPRETER"
# The language of an interpreter's session; sessions without it run Python.
INTERPRETER_LANGUAGE_ENV = "ARKER_INTERPRETER_LANGUAGE"
# Per language: the driver this package carries, and how the VM runs it.
INTERPRETERS = {
    "python": ("_interpreter.py", "python3 -u {path}"),
    "javascript": ("_interpreter.js", "node --no-warnings {path}"),
    "typescript": ("_interpreter.js", "node --no-warnings {path} typescript"),
}


@dataclasses.dataclass(frozen=True)
class CodeContext:
    id: str
    cwd: str
    default: bool
    language: str = "python"


@dataclasses.dataclass(frozen=True)
class CodeResult:
    """One displayed value: ``data`` maps a format (``text``, ``html``, ``png``...) to its content."""

    data: dict[str, Any]
    main: bool


@dataclasses.dataclass(frozen=True)
class CodeError:
    name: str
    value: str
    traceback: str


@dataclasses.dataclass(frozen=True)
class CodeExecution:
    results: list[CodeResult]
    stdout: str
    stderr: str
    error: CodeError | None
    count: int


class CodeContexts:
    """A sandbox's code contexts, each a Python, JavaScript or TypeScript
    interpreter (see :data:`INTERPRETERS`). Each keeps its variables and imports
    from one run to the next until it is deleted or restarted, or a run times out."""

    def __init__(self, sandbox: CompatSandbox) -> None:
        self._sandbox = sandbox
        self._errors = sandbox._errors
        self._defaults: dict[str, str] = {}

    @_translated
    def create(self, cwd: str | None = None, *, default: bool = False, language: str = "python") -> CodeContext:
        vm = self._sandbox.vm
        env = {INTERPRETER_ENV: "default" if default else "context", INTERPRETER_LANGUAGE_ENV: language}
        session = vm.create_session(env=env, cwd=cwd)
        try:
            self._start(session.session_id, language)
        except BaseException:
            with contextlib.suppress(ArkerError):
                vm.delete_session(session.session_id)
            raise
        return CodeContext(session.session_id, session.cwd, default, language)

    @_translated
    def list(self) -> list[CodeContext]:
        contexts = []
        for session in _paged(lambda cursor: self._sandbox.vm.list_sessions(cursor=cursor), "sessions"):
            env = session.env or {}
            kind = env.get(INTERPRETER_ENV)
            if kind:
                language = env.get(INTERPRETER_LANGUAGE_ENV, "python")
                contexts.append(CodeContext(session.session_id, session.cwd, kind == "default", language))
        return contexts

    @_translated
    def delete(self, context_id: str) -> None:
        try:
            self._sandbox.vm.delete_session(context_id)
        except ArkerError as error:
            if error.status == 404:
                raise self._errors.not_found(f"Code context {context_id} not found") from error
            raise

    def restart(self, context_id: str) -> None:
        """Start the context's interpreter afresh, forgetting its state."""
        self._request(context_id, {"restart": True}, None)

    def run(
        self,
        code: str,
        context_id: str | None = None,
        *,
        cwd: str | None = None,
        envs: dict[str, str] | None = None,
        timeout: float | None = None,
        language: str = "python",
    ) -> CodeExecution:
        """Run ``code`` in a context, by default the sandbox's default context for
        ``language``, created in ``cwd`` on first use. Errors in the code are part of the result."""
        context_id = context_id or self._default_context(cwd, language)
        request = {"code": code, "envs_file": self._sandbox._private_file(json.dumps(envs)) if envs else None}
        reply = self._request(context_id, request, timeout)
        error = reply["error"]
        return CodeExecution(
            results=[CodeResult(result["data"], result["main"]) for result in reply["results"]],
            stdout=reply["stdout"],
            stderr=reply["stderr"],
            error=CodeError(**error) if error else None,
            count=reply["count"],
        )

    def _default_context(self, cwd: str | None, language: str) -> str:
        if language not in self._defaults:
            existing = (context.id for context in self.list() if context.default and context.language == language)
            self._defaults[language] = next(existing, None) or self.create(cwd, default=True, language=language).id
        return self._defaults[language]

    def _language(self, context_id: str) -> str:
        return next((context.language for context in self.list() if context.id == context_id), "python")

    @_translated
    def _start(self, session_id: str, language: str) -> None:
        driver_name, command = INTERPRETERS[language]
        path = f"{INTERPRETER_DIR}/{driver_name.lstrip('_')}"
        with open(os.path.join(os.path.dirname(__file__), driver_name), "rb") as driver:
            self._sandbox.write(path, driver.read())
        script = self._sandbox._script(command.format(path=path), None, None)
        started = self._sandbox.vm.run(script, session_id=session_id, end_symbol=INTERPRETER_READY)
        if not started.stdout.endswith(INTERPRETER_READY):
            raise self._errors.failed(f"The code interpreter did not start: {started.stderr or started.stdout}")

    @_translated
    def _request(self, context_id: str, request: dict[str, Any], timeout: float | None) -> dict[str, Any]:
        line = "#" + json.dumps(request)
        for attempt in range(2):
            try:
                done = self._sandbox.vm.run(
                    line, session_id=context_id, end_symbol=INTERPRETER_READY, timeout=_kill_bound(timeout)
                )
            except ArkerError as error:
                if error.status == 404:
                    raise self._errors.not_found(f"Code context {context_id} not found") from error
                raise
            if done.stdout.endswith(INTERPRETER_READY):
                # A restart replies with the prompt alone.
                reply = done.stdout.removesuffix(INTERPRETER_READY).strip()
                return json.loads(reply.splitlines()[-1]) if reply else {}
            if timeout and done.exit_code == 124 and _TIMED_OUT.search(done.stderr):
                raise self._errors.timeout(f"Code execution timed out after {timeout}s; the context's state is lost.")
            # The interpreter is gone (it timed out, or the VM restarted without
            # its memory), and the session's shell read the request as a comment.
            if attempt == 0:
                self._start(context_id, self._language(context_id))
        raise self._errors.failed(f"The code interpreter is not responding: {done.stderr or done.stdout}")


# Volumes: Arker filesystems, which persist apart from any VM and mount into many.

MOUNT_WAIT = 120


class Volumes:
    """Named volumes, as Arker filesystems. Names are unique per organization."""

    def __init__(self, errors: Errors, config: dict[str, Any] | None = None) -> None:
        self._errors = errors
        self._arker = arker_client(config)

    @_translated
    def list(self) -> list[Filesystem]:
        return list(_paged(lambda cursor: self._arker.list_filesystems(cursor=cursor), "filesystems"))

    @_translated
    def find(self, name: str) -> Filesystem | None:
        listed = _paged(lambda cursor: self._arker.list_filesystems(cursor=cursor, name_prefix=name), "filesystems")
        return next((volume for volume in listed if volume.name == name), None)

    @_translated
    def get(self, name: str, *, create: bool = False) -> Filesystem:
        volume = self.find(name)
        if volume is not None:
            return volume
        if not create:
            raise self._errors.not_found(f"Volume {name!r} not found")
        try:
            return self._arker.create_filesystem(name=name)
        except ArkerError as error:
            # Another caller created it first.
            if error.status == 409:
                return self.get(name)
            raise

    @_translated
    def create(self, name: str) -> Filesystem:
        try:
            return self._arker.create_filesystem(name=name)
        except ArkerError as error:
            if error.status == 409:
                raise self._errors.invalid(f"Volume {name!r} already exists") from error
            raise

    @_translated
    def by_id(self, volume_id: str) -> Filesystem:
        try:
            return self._arker.get_filesystem(volume_id)
        except ArkerError as error:
            if error.status in (400, 404):
                raise self._errors.not_found(f"Volume {volume_id} not found") from error
            raise

    @_translated
    def delete(self, volume_id: str) -> bool:
        """Delete a volume and its data; False if there was no such volume."""
        try:
            self._arker.delete_filesystem(volume_id)
        except ArkerError as error:
            if error.status == 404:
                return False
            raise
        return True


def _mount(sandbox: CompatSandbox, volume_id: str, path: str) -> None:
    """Mount a volume at ``path`` and wait until it serves files. The VM
    attaches a mount when it next runs, so the wait runs in the VM."""
    vm = sandbox.vm
    mount = vm.create_mount(filesystem_id=volume_id, path=path)
    polls = int(MOUNT_WAIT / POLL_INTERVAL)
    wait = (
        f"for _ in $(seq {polls}); do mountpoint -q {shlex.quote(path)} && exit 0; sleep {POLL_INTERVAL}; done; exit 1"
    )
    if sandbox.run(wait).exit_code != 0:
        mount = next((m for m in vm.list_mounts(filesystem_id=volume_id).mounts if m.mount_id == mount.mount_id), mount)
        raise sandbox._errors.failed(f"Volume {volume_id} did not mount at {path}: {mount.state_detail or mount.state}")
