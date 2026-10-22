from __future__ import annotations

import posixpath
import re
import shlex
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.resources import files
from typing import Any, Literal, overload

from ._compat import (
    CommandResult as _CommonCommandResult,
)
from ._compat import (
    CompatibilityCreationNotStartedError,
    UnsupportedCompatibilityOption,
    UnsupportedMixin,
    _InstanceOrClassMethod,
    arker_create,
    arker_get,
    compat_network_policies,
    format_not_found,
    get_or_none,
    import_optional,
    resolve_compat_path,
    routed_create,
    routed_get,
    run_command,
)
from ._compat_e2b_async import AsyncSandbox as AsyncSandbox
from ._compat_process import GuestProcessError, GuestProcesses
from .computer import VM, ArkerError

_FILE_IO = files("arker").joinpath("_guest_e2b_file.py").read_text()

_E2B_USER = "user"
_E2B_HOME = "/home/user"
_E2B_DEFAULT_TIMEOUT_SECONDS = 300
_E2B_MAX_TIMEOUT_SECONDS = 86_400
_E2B_LIFETIME_POLICY_ID = "computesdk-e2b-lifetime"


def _identity_command(
    command: str,
    *,
    user: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    selected = user or _E2B_USER
    if selected not in {_E2B_USER, "root"}:
        raise UnsupportedCompatibilityOption("Arker E2B commands support only the default user or root")
    home = _E2B_HOME if selected == _E2B_USER else "/root"
    working_directory = cwd or _E2B_HOME
    exports = {"HOME": home, "USER": selected, "LOGNAME": selected, **(env or {})}
    assignments = []
    for key, value in exports.items():
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
            raise ValueError(f"Invalid environment variable name: {key}")
        assignments.append(f"export {key}={shlex.quote(value)};")
    shell = f"cd {shlex.quote(working_directory)} && {' '.join(assignments)} exec /bin/bash -c {shlex.quote(command)}"
    return f"setpriv --reuid={selected} --regid={selected} --init-groups -- sh -c {shlex.quote(shell)}"


def _validate_e2b_identity(vm: VM) -> None:
    check = run_command(
        vm,
        "command -v setpriv >/dev/null "
        "&& test \"$(getent passwd user)\" = 'user:x:1000:1000::/home/user:/bin/bash' "
        "&& test \"$(getent group user)\" = 'user:x:1000:' "
        "&& test \"$(stat -c '%u:%g:%a' /home/user)\" = '1000:1000:755'",
    )
    if check.exit_code != 0:
        raise UnsupportedCompatibilityOption(
            "Arker E2B compatibility requires an explicitly configured source VM with "
            "user:user at UID/GID 1000 and /home/user owned 1000:1000 mode 0755"
        )


def _filesystem_command(command: str, *, user: str | None) -> str:
    return command if user is None else _identity_command(command, user=user, cwd="/")


@dataclass(frozen=True)
class _CommandResult(_CommonCommandResult):
    error: str | None = None


class _CommandExitException(RuntimeError):
    def __init__(self, *, stdout: str, stderr: str, exit_code: int, error: str | None = None):
        self.stdout, self.stderr, self.exit_code, self.error = stdout, stderr, exit_code, error
        super().__init__(f"Command exited with code {exit_code} and error:\n{stderr}")


@dataclass(frozen=True)
class _WriteInfo:
    name: str
    type: Any
    path: str
    metadata: dict[str, str] | None = None


@dataclass(frozen=True)
class _EntryInfo(_WriteInfo):
    size: int = 0
    mode: int = 0
    permissions: str = ""
    owner: str = ""
    group: str = ""
    modified_time: datetime | None = None
    symlink_target: str | None = None


def _file_type(name: str) -> Any:
    module = import_optional("e2b")
    enum_type = getattr(module, "FileType", None) if module is not None else None
    return getattr(enum_type, name, name.lower()) if enum_type is not None else name.lower()


def _write_info(path: str, metadata: dict[str, str] | None = None) -> Any:
    module = import_optional("e2b")
    info_type = getattr(module, "WriteInfo", _WriteInfo) if module is not None else _WriteInfo
    resolved = resolve_compat_path(path, "/home/user")
    return info_type(
        name=resolved.rstrip("/").rsplit("/", 1)[-1],
        type=_file_type("FILE"),
        path=resolved,
        metadata=metadata,
    )


def __getattr__(name: str) -> Any:
    module = import_optional("e2b")
    if module is not None:
        return getattr(module, name)
    if name == "CommandResult":
        return _CommandResult
    if name == "CommandExitException":
        return _CommandExitException
    raise AttributeError(name)


class Commands(UnsupportedMixin):
    _compat_name = "e2b"

    def __init__(self, vm: VM | None = None, native: Any | None = None) -> None:
        self._vm = vm
        self._native = native
        self._processes = GuestProcesses(vm) if vm is not None else None

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        envs: dict[str, str] | None = None,
        timeout: int | None = None,
        background: bool = False,
        stdin: bool | None = None,
        on_stdout: Any = None,
        on_stderr: Any = None,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> _CommonCommandResult | Any:
        if env is not None and envs is not None:
            raise TypeError("Pass envs, not both env and envs")
        environment = envs if envs is not None else env
        if self._vm is not None:
            if request_timeout is not None:
                raise UnsupportedCompatibilityOption("E2B per-request timeout options are not yet supported on Arker")
            if background or stdin or on_stdout is not None or on_stderr is not None:
                from ._compat_e2b import command_handle, require_native_handle

                require_native_handle()
                current = self._processes.exec(
                    {
                        "command": "exec " + _identity_command(command, user=user, cwd=cwd, env=environment),
                        "workingDir": "/",
                        "env": {},
                        "stdin": bool(stdin),
                    }
                )
                handle = command_handle(self._processes, current, timeout)
                return handle if background else handle.wait(on_stdout=on_stdout, on_stderr=on_stderr)
            full_command = _identity_command(command, user=user, cwd=cwd, env=environment)
            result = run_command(self._vm, full_command, timeout=timeout)
            module = import_optional("e2b")
            fields = {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.exit_code, "error": None}
            if result.exit_code != 0:
                fields["error"] = f"exit status {result.exit_code}"
                error_type = getattr(module, "CommandExitException", _CommandExitException)
                raise error_type(**fields)
            return getattr(module, "CommandResult", _CommandResult)(**fields)
        native_options = {
            **({"cwd": cwd} if cwd is not None else {}),
            **({"envs": environment} if environment is not None else {}),
            **({"timeout": timeout} if timeout is not None else {}),
            **({"background": True} if background else {}),
            **({"stdin": stdin} if stdin is not None else {}),
            **({"on_stdout": on_stdout} if on_stdout is not None else {}),
            **({"on_stderr": on_stderr} if on_stderr is not None else {}),
            **({"user": user} if user is not None else {}),
            **({"request_timeout": request_timeout} if request_timeout is not None else {}),
        }
        return self._native.run(command, **native_options)

    def connect(self, pid: int, timeout: float | None = 60, request_timeout: float | None = None):
        if self._vm is None:
            return self._native.connect(pid, timeout=timeout, request_timeout=request_timeout)
        if request_timeout is not None:
            raise UnsupportedCompatibilityOption("Per-request command timeouts are not yet supported on Arker")
        from ._compat_e2b import command_handle, process_control

        current = process_control(self._processes.get, str(pid))
        return command_handle(self._processes, current, timeout)

    def kill(self, pid: int, request_timeout: float | None = None) -> bool:
        if self._vm is None:
            return self._native.kill(pid, request_timeout=request_timeout)
        if request_timeout is not None:
            raise UnsupportedCompatibilityOption("Per-request command timeouts are not yet supported on Arker")
        try:
            if self._processes.get(str(pid)).status != "running":
                return False
        except GuestProcessError as error:
            if error.code == "process_not_found":
                return False
            raise
        self._processes.kill(str(pid))
        return True

    def send_stdin(self, pid: int, data: str | bytes, request_timeout: float | None = None) -> None:
        if self._vm is None:
            self._native.send_stdin(pid, data, request_timeout=request_timeout)
            return
        if request_timeout is not None:
            raise UnsupportedCompatibilityOption("Per-request stdin timeouts are not yet supported on Arker")
        from ._compat_e2b import process_control

        process_control(self._processes.write_stdin, str(pid), data)

    def close_stdin(self, pid: int, request_timeout: float | None = None) -> None:
        if self._vm is None:
            self._native.close_stdin(pid, request_timeout=request_timeout)
            return
        if request_timeout is not None:
            raise UnsupportedCompatibilityOption("Per-request stdin timeouts are not yet supported on Arker")
        from ._compat_e2b import process_control

        process_control(self._processes.close_stdin, str(pid))


class Files(UnsupportedMixin):
    _compat_name = "e2b"

    def __init__(self, vm: VM | None = None, native: Any | None = None) -> None:
        self._vm = vm
        self._native = native

    @overload
    def read(self, path: str, format: Literal["text"] = "text") -> str: ...

    @overload
    def read(self, path: str, format: Literal["bytes"]) -> bytearray: ...

    def read(self, path: str, format: str = "text") -> str | bytearray:
        if self._vm is None:
            return self._native.read(path, format=format)
        if format not in {"text", "bytes"}:
            raise UnsupportedCompatibilityOption(
                "Arker files.read supports buffered text/bytes formats, not this format"
            )
        raw = self._vm.sync(resolve_compat_path(path, _E2B_HOME))
        if raw is None:
            raise RuntimeError("Arker file read returned no data")
        return bytearray(raw) if format == "bytes" else raw.decode("utf-8", "replace")

    def write(
        self,
        path: str,
        content: str | bytes,
        user: str | None = None,
        request_timeout: float | None = None,
        gzip: bool = False,
        use_octet_stream: bool | None = None,
        metadata: dict[str, str] | None = None,
    ) -> Any:
        if self._vm is None:
            return self._native.write(
                path,
                content,
                user=user,
                request_timeout=request_timeout,
                gzip=gzip,
                use_octet_stream=use_octet_stream,
                metadata=metadata,
            )
        unsupported = [
            name
            for name, enabled in (
                ("request_timeout", request_timeout is not None),
                ("gzip", gzip),
                ("use_octet_stream", use_octet_stream is not None),
                ("metadata", metadata is not None),
            )
            if enabled
        ]
        if unsupported:
            raise UnsupportedCompatibilityOption(
                "Arker files.write cannot represent option(s): " + ", ".join(unsupported)
            )
        resolved = resolve_compat_path(path, "/home/user")
        directory = f"/tmp/.computesdk-e2b-write-{uuid.uuid4().hex}"
        staged = directory + "/data"
        # Validate identity before allocating anything. The shell opens stdin as
        # root; the destination is opened only after dropping privileges.
        user_command = (
            _identity_command(
                "python3 -c " + shlex.quote(_FILE_IO) + " write " + shlex.quote(resolved), user=user, cwd="/"
            )
            + " < "
            + shlex.quote(staged)
        )
        created = run_command(self._vm, "mkdir -m 700 -- " + shlex.quote(directory))
        if created.exit_code != 0:
            raise RuntimeError(created.stderr or "private staging directory creation failed")
        write_error: BaseException | None = None
        try:
            reserved = run_command(self._vm, "umask 077; set -C; : > " + shlex.quote(staged))
            if reserved.exit_code != 0:
                raise RuntimeError(reserved.stderr or "private staging file creation failed")
            self._vm.sync(staged, content)
            written = run_command(
                self._vm,
                user_command,
            )
            if written.exit_code != 0:
                raise RuntimeError(written.stderr or f"write failed with exit {written.exit_code}")
        except BaseException as error:
            write_error = error
        cleanup_error: BaseException | None = None
        try:
            cleanup = run_command(self._vm, "rm -rf -- " + shlex.quote(directory))
            if cleanup.exit_code != 0:
                cleanup_error = RuntimeError(
                    cleanup.stderr or f"temporary file cleanup failed with exit {cleanup.exit_code}"
                )
        except BaseException as error:
            cleanup_error = error
        if write_error is not None:
            raise write_error
        if cleanup_error is not None:
            raise cleanup_error
        return _write_info(path)

    def write_files(
        self,
        files: list[Any],
        user: str | None = None,
        request_timeout: float | None = None,
        gzip: bool = False,
        use_octet_stream: bool | None = None,
        metadata: dict[str, str] | None = None,
    ) -> list[Any]:
        if self._vm is None:
            return self._native.write_files(
                files,
                user=user,
                request_timeout=request_timeout,
                gzip=gzip,
                use_octet_stream=use_octet_stream,
                metadata=metadata,
            )
        return [
            self.write(
                file["path"] if isinstance(file, dict) else file.path,
                file["data"] if isinstance(file, dict) else file.data,
                user=user,
                request_timeout=request_timeout,
                gzip=gzip,
                use_octet_stream=use_octet_stream,
                metadata=metadata,
            )
            for file in files
        ]

    def get_info(self, path: str, user: str | None = None, request_timeout: float | None = None) -> Any:
        if self._vm is None:
            return self._native.get_info(path, user=user, request_timeout=request_timeout)
        if request_timeout is not None:
            raise UnsupportedCompatibilityOption("Arker files.get_info does not support request_timeout")
        resolved = resolve_compat_path(path, "/home/user")
        result = run_command(
            self._vm,
            _filesystem_command(
                "stat --printf '%n\\037%s\\037%f\\037%A\\037%U\\037%G\\037%Y\\037%F' -- " + shlex.quote(resolved),
                user=user,
            ),
        )
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or f"stat failed with exit {result.exit_code}")
        values = result.stdout.split("\x1f")
        if len(values) != 8:
            raise RuntimeError("Incomplete file information response")
        name, size, mode, permissions, owner, group, modified, description = values
        if "symbolic link" in description:
            file_type = _file_type("SYMLINK")
            target_result = run_command(
                self._vm,
                _filesystem_command("readlink -- " + shlex.quote(resolved), user=user),
            )
            symlink_target = target_result.stdout.rstrip("\n") if target_result.exit_code == 0 else None
        else:
            file_type = _file_type("DIR" if "directory" in description else "FILE")
            symlink_target = None
        module = import_optional("e2b")
        info_type = getattr(module, "EntryInfo", _EntryInfo) if module is not None else _EntryInfo
        return info_type(
            name=name.rstrip("/").rsplit("/", 1)[-1],
            type=file_type,
            path=name,
            size=int(size),
            mode=int(mode, 16),
            permissions=permissions,
            owner=owner,
            group=group,
            modified_time=datetime.fromtimestamp(int(modified), tz=timezone.utc),
            symlink_target=symlink_target,
            metadata=None,
        )

    def rename(
        self,
        old_path: str,
        new_path: str,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> Any:
        if self._vm is None:
            return self._native.rename(old_path, new_path, user=user, request_timeout=request_timeout)
        if request_timeout is not None:
            raise UnsupportedCompatibilityOption("Arker files.rename does not support request_timeout")
        old_resolved = resolve_compat_path(old_path, "/home/user")
        new_resolved = resolve_compat_path(new_path, "/home/user")
        result = run_command(
            self._vm,
            _filesystem_command(
                f"mkdir -p -- {shlex.quote(posixpath.dirname(new_resolved))} && "
                f"mv -- {shlex.quote(old_resolved)} {shlex.quote(new_resolved)}",
                user=user,
            ),
        )
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or f"rename failed with exit {result.exit_code}")
        return self.get_info(new_resolved)

    def make_dir(self, path: str) -> None:
        if self._vm is None:
            if hasattr(self._native, "make_dir"):
                return self._native.make_dir(path)
            return self._native.makeDir(path)
        result = run_command(
            self._vm,
            _identity_command(f"mkdir -p {shlex.quote(resolve_compat_path(path, _E2B_HOME))}", cwd="/"),
        )
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or f"mkdir failed with exit {result.exit_code}")
        return None

    def makeDir(self, path: str) -> None:
        return self.make_dir(path)

    def list(self, path: str) -> list[Any]:
        if self._vm is None:
            return self._native.list(path)
        quoted = shlex.quote(resolve_compat_path(path, "/home/user"))
        script = (
            f"cd {quoted} 2>/dev/null || exit 3; "
            "for e in * .*; do "
            '[ "$e" = "." ] && continue; [ "$e" = ".." ] && continue; [ -e "$e" ] || continue; '
            'if [ -d "$e" ]; then t=dir; else t=file; fi; '
            'printf \'%s\t%s\n\' "$t" "$e"; done'
        )
        result = run_command(self._vm, script)
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or f"list failed with exit {result.exit_code}")
        names = [line.split("\t", 1)[1] for line in result.stdout.splitlines() if "\t" in line]
        base = resolve_compat_path(path, _E2B_HOME)
        return [self.get_info(posixpath.join(base, name)) for name in names]

    def exists(self, path: str) -> bool:
        if self._vm is None:
            return bool(self._native.exists(path))
        return (
            run_command(
                self._vm,
                f"test -e {shlex.quote(resolve_compat_path(path, _E2B_HOME))}",
            ).exit_code
            == 0
        )

    def remove(self, path: str) -> None:
        if self._vm is None:
            result = self._native.remove(path)
            return None if result is None else result
        result = run_command(
            self._vm,
            f"rm -rf {shlex.quote(resolve_compat_path(path, _E2B_HOME))}",
        )
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or f"remove failed with exit {result.exit_code}")
        return None


class Git:
    """Bind E2B's Git command translation to either provider's command runner."""

    def __init__(self, commands: Commands) -> None:
        module = import_optional("e2b")
        native_git = getattr(module, "Git", None) if module is not None else None
        if native_git is None:
            self._delegate = None
        else:
            self._delegate = native_git(commands)

    def __getattr__(self, name: str) -> Any:
        if self._delegate is None:
            raise AttributeError("E2B Git compatibility requires the optional 'arker[e2b]' dependency")
        return getattr(self._delegate, name)


class Sandbox(UnsupportedMixin):
    _compat_name = "e2b"

    def __init__(self, vm: VM | None = None, native: Any | None = None) -> None:
        self._vm = vm
        self._native = native
        self.sandbox_id = vm.id if vm is not None else getattr(native, "sandbox_id", getattr(native, "id", None))
        self.sandboxId = self.sandbox_id
        self.commands = Commands(vm, getattr(native, "commands", None) if native is not None else None)
        native_files = getattr(native, "files", None) if native is not None else None
        self.files = native_files if native_files is not None else Files(vm)
        native_git = getattr(native, "git", None) if native is not None else None
        self.git = native_git if native_git is not None else Git(self.commands)

    def get_host(self, port: int) -> str:
        if self._vm is None:
            if hasattr(self._native, "get_host"):
                return self._native.get_host(port)
            return self._native.getHost(port)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("port must be an integer from 1 through 65535")
        vm = self._vm if self._vm.hostname else self._vm.refresh()
        if not vm.hostname:
            raise UnsupportedCompatibilityOption("Arker did not return an inbound hostname for this VM")
        if port == 8080:
            return vm.hostname
        first, separator, remainder = vm.hostname.partition(".")
        return f"{first}-{port}{separator}{remainder}"

    def getHost(self, port: int) -> str:
        return self.get_host(port)

    @staticmethod
    def _native_sandbox() -> Any | None:
        module = import_optional("e2b")
        return getattr(module, "Sandbox", None) if module is not None else None

    @classmethod
    def create(cls, template_id: str | None = None, **kwargs: Any) -> Sandbox:
        timeout = kwargs.pop("timeout", None)
        timeout_aliases = sorted({"timeout_ms", "timeoutMs"}.intersection(kwargs))
        if timeout_aliases:
            raise TypeError(
                f"Sandbox.create does not accept {', '.join(timeout_aliases)}; "
                "the E2B Python API uses timeout in seconds"
            )
        allow_internet_access = kwargs.pop("allow_internet_access", kwargs.pop("allowInternetAccess", None))
        arker_config = kwargs.pop("arker", None) or {}
        router_path = kwargs.pop("router_config_path", kwargs.pop("routerConfigPath", None))
        if router_path:
            arker_config = {**arker_config, "router_config_path": router_path}
        native = cls._native_sandbox()

        def create_native() -> Any:
            if native is None:
                raise CompatibilityCreationNotStartedError("Native E2B SDK is not installed")
            native_options = dict(kwargs)
            if allow_internet_access is not None:
                native_options["allow_internet_access"] = allow_internet_access
            if timeout is not None:
                native_options["timeout"] = timeout
            if template_id is not None:
                return native.create(template_id, **native_options)
            return native.create(**native_options)

        provider, result = routed_create(
            "e2b",
            arker_config,
            {
                "arker": lambda: cls._create_arker(
                    arker_config,
                    template_id,
                    timeout,
                    allow_internet_access,
                    kwargs,
                ),
                "e2b": create_native,
            },
        )
        if provider == "arker":
            try:
                _validate_e2b_identity(result)
            except BaseException:
                with suppress(BaseException):
                    result.delete()
                raise
        return cls(vm=result if provider == "arker" else None, native=result if provider == "e2b" else None)

    @staticmethod
    def _create_arker(
        arker_config: dict[str, Any],
        template_id: str | None,
        timeout: int | None,
        allow_internet_access: bool | None,
        native_options: dict[str, Any],
    ) -> VM:
        unsupported = sorted(native_options)
        if unsupported:
            raise UnsupportedCompatibilityOption(
                f"Sandbox.create option(s) are not supported by Arker: {', '.join(unsupported)}"
            )
        lifetime = _E2B_DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout
        if isinstance(lifetime, bool) or not isinstance(lifetime, int):
            raise TypeError("timeout must be an integer number of seconds")
        if not 1 <= lifetime <= _E2B_MAX_TIMEOUT_SECONDS:
            raise ValueError(f"timeout must be between 1 and {_E2B_MAX_TIMEOUT_SECONDS} seconds")
        configured_rules = (arker_config.get("policies") or {}).get("rules", {})
        if _E2B_LIFETIME_POLICY_ID in configured_rules:
            raise ValueError(f"policy id {_E2B_LIFETIME_POLICY_ID!r} is reserved for the E2B sandbox lifetime")
        network = compat_network_policies(block_all=allow_internet_access is False)
        policies = {
            "rules": {
                **((network or {}).get("rules", {})),
                _E2B_LIFETIME_POLICY_ID: {
                    "type": "schedule",
                    "after_created": lifetime,
                    "action": "delete",
                },
            }
        }
        return arker_create(
            arker_config,
            template_id=template_id,
            policies=policies,
        )

    @classmethod
    def _lookup(cls, sandbox_id: str, kwargs: dict[str, Any]):
        arker_config = kwargs.pop("arker", None) or {}
        router_path = kwargs.pop("router_config_path", kwargs.pop("routerConfigPath", None))
        if router_path:
            arker_config = {**arker_config, "router_config_path": router_path}
        if kwargs:
            unsupported = ", ".join(sorted(kwargs))
            raise TypeError(f"Sandbox lookup received unsupported option(s): {unsupported}")
        native = cls._native_sandbox()
        found = routed_get(
            sandbox_id,
            "e2b",
            arker_config,
            {
                "arker": lambda: arker_get(sandbox_id, arker_config),
                "e2b": lambda: get_or_none(lambda: native.get_info(sandbox_id)) if native is not None else None,
            },
        )
        return found, native

    @classmethod
    def connect(cls, sandbox_id: str, **kwargs: Any) -> Sandbox:
        found, native = cls._lookup(sandbox_id, kwargs)
        if found is not None:
            provider, result = found
            if provider == "arker":
                _validate_e2b_identity(result)
            return cls(
                vm=result if provider == "arker" else None,
                native=native.connect(sandbox_id) if provider == "e2b" else None,
            )
        raise format_not_found("Sandbox", sandbox_id)

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.kill()

    @classmethod
    def _class_kill(cls, sandbox_id: str | Sandbox, **kwargs: Any) -> bool:
        if isinstance(sandbox_id, cls):
            return sandbox_id.kill(**kwargs)
        found, native = cls._lookup(sandbox_id, kwargs)
        if found is None:
            return False
        provider, result = found
        return native.kill(sandbox_id) if provider == "e2b" else cls(vm=result).kill()

    @_InstanceOrClassMethod
    def kill(self) -> bool:
        if self._vm is not None:
            try:
                self._vm.refresh()
                return self._vm.delete().deleted
            except ArkerError as error:
                if error.status == 404:
                    return False
                raise
        if hasattr(self._native, "kill"):
            return self._native.kill()
        raise AttributeError("Native E2B sandbox does not expose kill().")
