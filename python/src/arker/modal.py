"""Modal-compatible sandboxes backed by Arker VMs.

Replace ``import modal`` with ``from arker import modal``. What this layer
cannot honor raises an exception rather than doing something else.
"""

from __future__ import annotations

import dataclasses
import enum
import importlib.util
import math
import os
import pathlib
import posixpath
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any, ClassVar

from ._aio import AsyncMethods, iterate
from ._compat import (
    CompatSandbox,
    Egress,
    Entry,
    Errors,
    Process,
    UnsupportedMixin,
    Volumes,
    import_optional,
    missing_export,
    reject_options,
    unsupported,
)
from .build import BuildError, _resolve_sources
from .build_spec import Arg, Cmd, Copy, DockerfileError, Entrypoint, Env, Expose, Label, Run, Workdir, parse_dockerfile
from .computer import VM
from .dockerignore import load_dockerignore, parse_dockerignore

DEFAULT_TIMEOUT = 300
WATCH_INTERVAL = 0.5
# Options Modal uses to reach its own service; they have no meaning on Arker.
MODAL_CONNECTION_OPTIONS = {"client", "environment_name", "verbose"}


class Error(Exception):
    pass


class InvalidError(Error):
    pass


class NotFoundError(Error):
    pass


class ExecutionError(Error):
    pass


class TimeoutError(Error):
    pass


class ExecTimeoutError(TimeoutError):
    pass


class NotSupportedError(InvalidError, AttributeError):
    pass


class SandboxFilesystemNotFoundError(Error):
    pass


ERRORS = Errors(
    provider="Modal",
    unsupported=NotSupportedError,
    invalid=InvalidError,
    not_found=NotFoundError,
    file_not_found=SandboxFilesystemNotFoundError,
    timeout=ExecTimeoutError,
    failed=ExecutionError,
)


class _Layer(UnsupportedMixin):
    _compat_errors = ERRORS


def __getattr__(name: str) -> Any:
    return missing_export(ERRORS, __name__, name)


class App:
    """Accepted wherever Modal takes an app; Arker sandboxes need none."""

    def __init__(self, name: str | None = None, **_: Any) -> None:
        self.name = name

    @staticmethod
    def lookup(name: str, **_: Any) -> App:
        return App(name)


class Secret:
    def __init__(self, env: dict[str, str]) -> None:
        self.env = env

    @staticmethod
    def from_dict(env_dict: dict[str, str | None] | None = None) -> Secret:
        return Secret({name: value for name, value in (env_dict or {}).items() if value is not None})

    @staticmethod
    def from_local_environ(env_keys: list[str]) -> Secret:
        missing = [key for key in env_keys if key not in os.environ]
        if missing:
            raise InvalidError(
                f"Could not find local environment variable '{missing[0]}' for Secret.from_local_environ"
            )
        return Secret({key: os.environ[key] for key in env_keys})

    @staticmethod
    def from_dotenv(path: str | os.PathLike | None = None, *, filename: str = ".env", **_: Any) -> Secret:
        """The first ``filename`` found from ``path`` (a file or directory; by
        default the current directory) up to the root; empty if there is none."""
        start = os.path.abspath(path if path is not None else os.getcwd())
        directory = start if os.path.isdir(start) else os.path.dirname(start)
        while True:
            candidate = os.path.join(directory, filename)
            if os.path.isfile(candidate):
                with open(candidate, encoding="utf-8") as handle:
                    return Secret(_dotenv(handle.read()))
            if os.path.dirname(directory) == directory:
                return Secret({})
            directory = os.path.dirname(directory)


def _dotenv(text: str) -> dict[str, str]:
    """``KEY=value`` lines, optionally ``export``ed and quoted; ``#`` starts a comment."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if value[:1] in ("'", '"') and value.endswith(value[0]) and len(value) > 1:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key.strip()] = value
    return values


class _VolumeManager:
    @staticmethod
    def delete(name: str, *, allow_missing: bool = False, arker: dict[str, Any] | None = None, **_: Any) -> None:
        volumes = Volumes(ERRORS, arker)
        volume = volumes.find(name)
        if (volume is None or not volumes.delete(volume.filesystem_id)) and not allow_missing:
            raise NotFoundError(f"Volume {name!r} not found")


class Volume(_Layer):
    """A persistent volume, as an Arker filesystem, mounted with
    ``Sandbox.create(volumes={path: volume})``. Writes are visible to every
    sandbox that mounts it at once, so ``commit`` and ``reload`` have nothing to do."""

    _compat_path = "Volume"
    _compat_hints: ClassVar[dict[str, str]] = dict.fromkeys(
        ("listdir", "iterdir", "read_file", "remove_file", "copy_files", "batch_upload"),
        "mount the volume in a sandbox and use its files there.",
    )
    objects = _VolumeManager

    def __init__(self, name: str, create_if_missing: bool = False) -> None:
        self.name = name
        self._create_if_missing = create_if_missing

    @staticmethod
    def from_name(name: str, *, create_if_missing: bool = False, **options: Any) -> Volume:
        """Looked up, and created if allowed, when a sandbox mounts it."""
        reject_options(ERRORS, "Volume.from_name", options, ignored={"environment_name", "client", "version"})
        return Volume(name, create_if_missing)

    def _id(self, arker: dict[str, Any] | None) -> str:
        return Volumes(ERRORS, arker).get(self.name, create=self._create_if_missing).filesystem_id

    def commit(self) -> None:
        pass

    def reload(self) -> None:
        pass


def _flatten(items: tuple[str | list[str], ...]) -> list[str]:
    return [item for entry in items for item in (entry if isinstance(entry, list) else [entry])]


# Where build steps put the local files they need.
BUILD_DIR = "/tmp/arker-modal-build"
# Where add_local_python_source puts modules, as Modal does; added to PYTHONPATH.
PYTHON_SOURCE_DIR = "/root"


def _ignore(ignore: Any) -> Callable[[str], bool]:
    """Modal's ``ignore``: dockerignore-style patterns, or a predicate on a relative Path."""
    if callable(ignore):
        return lambda path: bool(ignore(pathlib.Path(path)))
    return parse_dockerignore(ignore).ignores


def _not_python(path: str) -> bool:
    return not path.endswith(".py") or "__pycache__" in path.split("/")


def _pip_options(
    find_links: str | None = None,
    index_url: str | None = None,
    extra_index_url: str | None = None,
    pre: bool = False,
    extra_options: str = "",
) -> str:
    flags = [("--find-links", find_links), ("--index-url", index_url), ("--extra-index-url", extra_index_url)]
    options = [part for flag, value in flags if value for part in (flag, value)] + (["--pre"] if pre else [])
    return " ".join(filter(None, [shlex.join(options), extra_options]))


@dataclasses.dataclass(frozen=True)
class _Step:
    """A build step: a shell command run with ``envs``, or local files copied to ``remote_path``."""

    command: str | None = None
    envs: tuple[tuple[str, str], ...] = ()
    local_path: str | None = None
    remote_path: str | None = None
    ignore: Callable[[str], bool] | None = None

    def apply(self, sandbox: CompatSandbox) -> None:
        if self.local_path is not None:
            sandbox.upload(self.local_path, self.remote_path, ignore=self.ignore)
            return
        result = sandbox.run(self.command, envs=dict(self.envs))
        if result.exit_code != 0:
            raise ExecutionError(f"Image build step failed ({result.exit_code}): {self.command}\n{result.stderr}")


@dataclasses.dataclass(frozen=True)
class Image(_Layer):
    """A registry image (or the default VM) plus build steps run after the fork,
    each time a sandbox is created from it."""

    _registry: str | None = None
    _steps: tuple[_Step, ...] = ()
    _env: tuple[tuple[str, str], ...] = ()
    _workdir: str | None = None
    # The VM a filesystem snapshot was taken into.
    _snapshot: VM | None = None
    _entrypoint: str | None = None
    _cmd: str | None = None

    _compat_path = "Image"
    # Build steps run each time a sandbox is created from the image, so a build is never cached.
    force_build = False

    @property
    def object_id(self) -> str | None:
        return self._snapshot.id if self._snapshot else None

    @staticmethod
    def from_id(image_id: str, client: Any = None, *, arker: dict[str, Any] | None = None) -> Image:
        """The image of a sandbox's filesystem snapshot, by its ``object_id``."""
        snapshot = CompatSandbox.connect(ERRORS, image_id, arker)
        if snapshot is None or snapshot.snapshot_of is None:
            raise NotFoundError(f"Image {image_id} not found")
        return Image(_snapshot=snapshot.vm)

    def build(self, app: Any = None) -> Image:
        """The image as it is: its build steps run when a sandbox is created from it."""
        return self

    @staticmethod
    def debian_slim(python_version: str | None = None, force_build: bool = False) -> Image:
        version = python_version or f"{sys.version_info.major}.{sys.version_info.minor}"
        return Image(f"python:{version}-slim")

    @staticmethod
    def from_registry(tag: str, **options: Any) -> Image:
        reject_options(ERRORS, "Image.from_registry", options, ignored={"force_build"})
        return Image(tag)

    @staticmethod
    def from_dockerfile(
        path: str | os.PathLike,
        *,
        context_dir: str | os.PathLike | None = None,
        build_args: dict[str, str] | None = None,
        env: dict[str, str | None] | None = None,
        secrets: list[Secret] | None = None,
        ignore: Any = None,
        **options: Any,
    ) -> Image:
        """``FROM`` an image, then its instructions as build steps. ``COPY`` reads
        from ``context_dir``, by default the current directory, less what ``ignore``
        or, without it, the context's ``.dockerignore`` excludes."""
        reject_options(ERRORS, "Image.from_dockerfile", options, ignored={"force_build"})
        with open(path, encoding="utf-8") as handle:
            parsed = _parse(handle.read())
        return Image(parsed.base_image)._dockerfile(parsed.steps, context_dir, build_args, _env(env, secrets), ignore)

    def _then(
        self, command: str, env: dict[str, str | None] | None = None, secrets: list[Secret] | None = None
    ) -> Image:
        if self._workdir:
            command = f"cd {shlex.quote(self._workdir)} && {command}"
        step = _Step(command, tuple(_env(env, secrets).items()))
        return dataclasses.replace(self, _steps=(*self._steps, step))

    def _copy(
        self, local_path: str | os.PathLike, remote_path: str, ignore: Callable[[str], bool] | None = None
    ) -> Image:
        local_path = os.fspath(local_path)
        if not os.path.exists(local_path):
            raise InvalidError(f"Local path {local_path} does not exist.")
        if remote_path.endswith("/") and os.path.isfile(local_path):
            remote_path += os.path.basename(local_path)
        remote_path = posixpath.normpath(posixpath.join(self._workdir or "/", remote_path))
        return dataclasses.replace(
            self, _steps=(*self._steps, _Step(local_path=local_path, remote_path=remote_path, ignore=ignore))
        )

    def run_commands(
        self,
        *commands: str | list[str],
        env: dict[str, str | None] | None = None,
        secrets: list[Secret] | None = None,
        **options: Any,
    ) -> Image:
        reject_options(ERRORS, "Image.run_commands", options, ignored={"force_build"})
        image = self
        for command in _flatten(commands):
            image = image._then(command, env, secrets)
        return image

    def pip_install(
        self,
        *packages: str | list[str],
        find_links: str | None = None,
        index_url: str | None = None,
        extra_index_url: str | None = None,
        pre: bool = False,
        extra_options: str = "",
        env: dict[str, str | None] | None = None,
        secrets: list[Secret] | None = None,
        **options: Any,
    ) -> Image:
        reject_options(ERRORS, "Image.pip_install", options, ignored={"force_build"})
        names = _flatten(packages)
        if not names:
            return self
        flags = _pip_options(find_links, index_url, extra_index_url, pre, extra_options)
        return self._then(f"python3 -m pip install {shlex.join(names)} {flags}", env, secrets)

    def pip_install_from_requirements(
        self,
        requirements_txt: str | os.PathLike,
        find_links: str | None = None,
        *,
        index_url: str | None = None,
        extra_index_url: str | None = None,
        pre: bool = False,
        extra_options: str = "",
        env: dict[str, str | None] | None = None,
        secrets: list[Secret] | None = None,
        **options: Any,
    ) -> Image:
        reject_options(ERRORS, "Image.pip_install_from_requirements", options, ignored={"force_build"})
        remote = f"{BUILD_DIR}/{uuid.uuid4().hex}/requirements.txt"
        flags = _pip_options(find_links, index_url, extra_index_url, pre, extra_options)
        return self._copy(requirements_txt, remote)._then(f"python3 -m pip install -r {remote} {flags}", env, secrets)

    def pip_install_from_pyproject(
        self,
        pyproject_toml: str | os.PathLike,
        optional_dependencies: list[str] | None = None,
        **options: Any,
    ) -> Image:
        """Install the project's dependencies, and those of the named optional groups; not the project itself."""
        with open(pyproject_toml, "rb") as handle:
            project = _toml(handle.read()).get("project", {})
        groups = project.get("optional-dependencies", {})
        packages = project.get("dependencies", []) + [
            dep for name in optional_dependencies or [] for dep in groups.get(name, [])
        ]
        reject_options(ERRORS, "Image.pip_install_from_pyproject", {"gpu": options.pop("gpu", None)})
        return self.pip_install(*packages, **options)

    def _uv(self, uv_version: str | None) -> Image:
        return self._then(f"python3 -m pip install --quiet {shlex.quote(f'uv=={uv_version}' if uv_version else 'uv')}")

    def uv_pip_install(
        self,
        *packages: str | list[str],
        requirements: list[str] | None = None,
        find_links: str | None = None,
        index_url: str | None = None,
        extra_index_url: str | None = None,
        pre: bool = False,
        extra_options: str = "",
        uv_version: str | None = None,
        env: dict[str, str | None] | None = None,
        secrets: list[Secret] | None = None,
        **options: Any,
    ) -> Image:
        reject_options(ERRORS, "Image.uv_pip_install", options, ignored={"force_build"})
        image = self._uv(uv_version)
        args = _flatten(packages)
        for requirement in requirements or []:
            remote = f"{BUILD_DIR}/{uuid.uuid4().hex}/{os.path.basename(requirement)}"
            image = image._copy(requirement, remote)
            args += ["-r", remote]
        flags = _pip_options(find_links, index_url, extra_index_url, extra_options=extra_options)
        flags += " --prerelease=allow" if pre else ""
        return image._then(f"python3 -m uv pip install --system {shlex.join(args)} {flags}", env, secrets)

    def uv_sync(
        self,
        uv_project_dir: str | os.PathLike = "./",
        *,
        groups: list[str] | None = None,
        extras: list[str] | None = None,
        frozen: bool = True,
        extra_options: str = "",
        uv_version: str | None = None,
        env: dict[str, str | None] | None = None,
        secrets: list[Secret] | None = None,
        **options: Any,
    ) -> Image:
        """Install the project's locked dependencies into the image's Python; not the project itself."""
        reject_options(ERRORS, "Image.uv_sync", options, ignored={"force_build"})
        remote = f"{BUILD_DIR}/{uuid.uuid4().hex}"
        image = self._uv(uv_version)._copy(os.path.join(uv_project_dir, "pyproject.toml"), f"{remote}/pyproject.toml")
        lockfile = os.path.join(uv_project_dir, "uv.lock")
        if os.path.exists(lockfile):
            image = image._copy(lockfile, f"{remote}/uv.lock")
        # --inexact keeps what earlier steps installed in the same Python.
        args = ["--no-install-project", "--inexact"] + (["--frozen"] if frozen else [])
        args += [part for group in groups or [] for part in ("--group", group)]
        args += [part for extra in extras or [] for part in ("--extra", extra)]
        prefix = "UV_PROJECT_ENVIRONMENT=$(python3 -c 'import sys; print(sys.prefix)')"
        command = f"cd {remote} && {prefix} python3 -m uv sync {shlex.join(args)} {extra_options}"
        return image._then(command, env, secrets)

    def apt_install(
        self,
        *packages: str | list[str],
        env: dict[str, str | None] | None = None,
        secrets: list[Secret] | None = None,
        **options: Any,
    ) -> Image:
        reject_options(ERRORS, "Image.apt_install", options, ignored={"force_build"})
        names = shlex.join(_flatten(packages))
        command = f"apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y {names}"
        return self._then(command, env, secrets)

    def add_local_file(self, local_path: str | os.PathLike, remote_path: str, *, copy: bool = False) -> Image:
        """Copied in when each sandbox is created; ``copy`` changes nothing."""
        return self._copy(local_path, remote_path)

    def add_local_dir(
        self, local_path: str | os.PathLike, remote_path: str, *, copy: bool = False, ignore: Any = ()
    ) -> Image:
        """Copied in when each sandbox is created; ``copy`` changes nothing."""
        return self._copy(local_path, remote_path, _ignore(ignore))

    def add_local_python_source(self, *modules: str, copy: bool = False, ignore: Any = None) -> Image:
        """The local packages and modules, importable in the sandbox from ``/root``.
        By default only ``.py`` files are copied."""
        image = self
        for module in modules:
            spec = importlib.util.find_spec(module)
            if spec is None or spec.origin is None:
                raise InvalidError(f"Module {module!r} is not importable locally.")
            if spec.submodule_search_locations:
                directory = next(iter(spec.submodule_search_locations))
                rules = _not_python if ignore is None else _ignore(ignore)
                image = image._copy(directory, f"{PYTHON_SOURCE_DIR}/{module}", rules)
            else:
                image = image._copy(spec.origin, f"{PYTHON_SOURCE_DIR}/{module}.py")
        python_path = dict(image._env).get("PYTHONPATH")
        return image if python_path else image.env({"PYTHONPATH": PYTHON_SOURCE_DIR})

    def dockerfile_commands(
        self,
        *dockerfile_commands: str | list[str],
        context_dir: str | os.PathLike | None = None,
        build_args: dict[str, str] | None = None,
        env: dict[str, str | None] | None = None,
        secrets: list[Secret] | None = None,
        ignore: Any = None,
        **options: Any,
    ) -> Image:
        """Apply Dockerfile instructions as build steps. ``COPY`` reads from
        ``context_dir``, by default the current directory, less what ``ignore``
        or, without it, the context's ``.dockerignore`` excludes."""
        reject_options(ERRORS, "Image.dockerfile_commands", options, ignored={"force_build"})
        parsed = _parse("FROM scratch\n" + "\n".join(_flatten(dockerfile_commands)))
        return self._dockerfile(parsed.steps, context_dir, build_args, _env(env, secrets), ignore)

    def _dockerfile(
        self,
        steps: list[Any],
        context_dir: str | os.PathLike | None,
        build_args: dict[str, str] | None,
        envs: dict[str, str],
        ignore: Any,
    ) -> Image:
        context = os.path.realpath(os.fspath(context_dir or os.getcwd()))
        ignored = load_dockerignore(context).ignores if ignore is None else _ignore(ignore)
        build_env = dict(envs)
        image = self
        for step in steps:
            if isinstance(step, Run):
                image = image._then(step.command, build_env)
            elif isinstance(step, Env):
                image = image.env(dict(step.pairs))
                build_env.update(step.pairs)
            elif isinstance(step, Arg):
                value = (build_args or {}).get(step.name, step.default)
                if value is not None:
                    build_env[step.name] = value
            elif isinstance(step, Workdir):
                image = image.workdir(posixpath.join(image._workdir or "/", step.path))
            elif isinstance(step, Copy):
                reject_options(ERRORS, "Dockerfile COPY", {"--chown": step.chown})
                sources = [
                    (path, os.path.relpath(path, context).replace(os.sep, "/"))
                    for source in step.sources
                    for path in _context_paths(source, context)
                ]
                sources = [(path, relative) for path, relative in sources if relative == "." or not ignored(relative)]
                # Docker copies into a directory for several sources, or a destination like "." or "dir/".
                into = len(sources) > 1 or step.dest.endswith(("/", "."))
                for path, relative in sources:
                    # The ignore rules name paths in the context; a directory's upload names them in the directory.
                    prefix = "" if relative == "." else f"{relative}/"
                    image = image._copy(
                        path,
                        posixpath.join(step.dest, "") if into else step.dest,
                        (lambda name, prefix=prefix: ignored(prefix + name)) if os.path.isdir(path) else None,
                    )
            elif isinstance(step, Entrypoint):
                image = dataclasses.replace(image, _entrypoint=step.command)
            elif isinstance(step, Cmd):
                image = dataclasses.replace(image, _cmd=step.command)
            elif not isinstance(step, (Label, Expose)):
                raise unsupported(ERRORS, f"Dockerfile {type(step).__name__.upper()}")
        return image

    def entrypoint(self, entrypoint_commands: list[str]) -> Image:
        """Run before a sandbox's command, as Docker's ``ENTRYPOINT`` is."""
        return dataclasses.replace(self, _entrypoint=shlex.join(entrypoint_commands))

    def cmd(self, cmd: list[str]) -> Image:
        """A sandbox's command, when it is created with none."""
        return dataclasses.replace(self, _cmd=shlex.join(cmd))

    def env(self, vars: dict[str, str]) -> Image:
        return dataclasses.replace(self, _env=(*self._env, *vars.items()))

    def workdir(self, path: str | os.PathLike) -> Image:
        path = os.fspath(path)
        return dataclasses.replace(self._then(f"mkdir -p {shlex.quote(path)}"), _workdir=path)

    def _command(self, args: tuple[str, ...]) -> str | None:
        """The sandbox's command: ``ENTRYPOINT`` then the arguments, else ``CMD``."""
        arguments = shlex.join(args) if args else (self._cmd if self._entrypoint else None)
        command = " ".join(filter(None, [self._entrypoint, arguments]))
        return command or None


def _context_paths(source: str, context: str) -> list[str]:
    """The real paths a ``COPY`` source names, refusing any outside the build context."""
    try:
        return _resolve_sources(source, context)
    except BuildError as error:
        raise InvalidError(str(error)) from error


def _parse(dockerfile: str) -> Any:
    try:
        return parse_dockerfile(dockerfile)
    except DockerfileError as error:
        raise InvalidError(str(error)) from error


def _toml(data: bytes) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError as error:
        raise InvalidError("Reading pyproject.toml needs Python 3.11 or later.") from error
    return tomllib.loads(data.decode())


class StreamType(enum.Enum):
    DEVNULL = subprocess.DEVNULL
    PIPE = subprocess.PIPE
    STDOUT = subprocess.STDOUT


class StreamReader:
    """``stdout`` or ``stderr`` of a process: ``read()`` it, or iterate its lines as they arrive.
    With ``text=False`` both give bytes."""

    def __init__(self, process: Process, index: int, text: bool = True) -> None:
        self._process = process
        self._index = index
        self._text = text

    def read(self) -> str | bytes:
        result = self._process.wait()
        output = (result.stdout, result.stderr)[self._index]
        return output if self._text else output.encode()

    def __iter__(self) -> Iterator[str | bytes]:
        pending = ""
        for chunk in self._process.follow():
            pending += chunk[self._index]
            *lines, pending = pending.split("\n")
            yield from (self._out(line + "\n") for line in lines)
        if pending:
            yield self._out(pending)

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return iterate(iter(self))

    def _out(self, line: str) -> str | bytes:
        return line if self._text else line.encode()


class StreamWriter:
    """A process's stdin: :meth:`write` buffers, :meth:`drain` sends, and
    after :meth:`write_eof` the next drain closes it."""

    def __init__(self, process: Process) -> None:
        self._process = process
        self._buffer = b""
        self._eof = False

    def write(self, data: bytes | bytearray | memoryview | str) -> None:
        if self._eof:
            raise ValueError("Stdin is closed. Cannot write to it.")
        self._buffer += data.encode() if isinstance(data, str) else bytes(data)

    def write_eof(self) -> None:
        self._eof = True

    def drain(self) -> None:
        if self._buffer:
            self._process.send_stdin(self._buffer)
            self._buffer = b""
        if self._eof:
            self._process.close_stdin()


class ContainerProcess(_Layer):
    _compat_path = "ContainerProcess"

    def __init__(self, process: Process, text: bool = True) -> None:
        self._process = process
        self.stdout = StreamReader(process, 0, text)
        self.stderr = StreamReader(process, 1, text)
        self.stdin = StreamWriter(process)

    @property
    def returncode(self) -> int | None:
        return self.poll()

    def poll(self) -> int | None:
        return self._process.poll()

    def wait(self) -> int:
        return self._process.wait().exit_code


class Tunnel:
    def __init__(self, host: str) -> None:
        self.host = host
        self.port = 443

    @property
    def url(self) -> str:
        return f"https://{self.host}"

    @property
    def tls_socket(self) -> tuple[str, int]:
        return self.host, self.port


class _File:
    """A file opened with ``Sandbox.open``; writes land when it is flushed or closed."""

    def __init__(self, sandbox: CompatSandbox, path: str, mode: str) -> None:
        self._sandbox = sandbox
        self._path = path
        self._binary = "b" in mode
        self._writable = any(flag in mode for flag in "wa+")
        if "r" in mode or "a" in mode:
            try:
                self._data = sandbox.read(path)
            except SandboxFilesystemNotFoundError:
                if "r" in mode:
                    raise
                self._data = b""
        else:
            self._data = b""

    def read(self) -> str | bytes:
        return self._data if self._binary else self._data.decode()

    def write(self, data: str | bytes) -> None:
        if not self._writable:
            raise InvalidError("File is not open for writing.")
        self._data += data if isinstance(data, bytes) else data.encode()

    def flush(self) -> None:
        if self._writable:
            self._sandbox.write(self._path, self._data)

    def close(self) -> None:
        self.flush()

    def __enter__(self) -> _File:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


class FileType(enum.Enum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"


@dataclasses.dataclass(frozen=True)
class FileInfo:
    name: str
    path: str
    type: FileType
    size: int
    mode: int
    permissions: str
    owner: str
    group: str
    modified_time: float
    symlink_target: str | None

    def is_file(self) -> bool:
        return self.type == FileType.FILE

    def is_dir(self) -> bool:
        return self.type == FileType.DIRECTORY

    def is_symlink(self) -> bool:
        return self.type == FileType.SYMLINK


class FileWatchEventType(enum.Enum):
    Unknown = "Unknown"
    Access = "Access"
    Create = "Create"
    Modify = "Modify"
    Remove = "Remove"


@dataclasses.dataclass(frozen=True)
class FileWatchEvent:
    paths: list[str]
    type: FileWatchEventType


_WATCH_EVENT_TYPES = {
    "create": FileWatchEventType.Create,
    "write": FileWatchEventType.Modify,
    "chmod": FileWatchEventType.Modify,
    "remove": FileWatchEventType.Remove,
}


def _file_info(entry: Entry) -> FileInfo:
    kind = FileType.SYMLINK if entry.symlink_target else FileType.DIRECTORY if entry.is_dir else FileType.FILE
    return FileInfo(
        name=entry.name,
        path=entry.path,
        type=kind,
        size=entry.size,
        mode=entry.mode,
        permissions=entry.permissions,
        owner=entry.owner,
        group=entry.group,
        modified_time=entry.modified,
        symlink_target=entry.symlink_target,
    )


class SandboxFilesystem(_Layer):
    """``sandbox.filesystem``: files in the sandbox, by absolute path."""

    _compat_path = "Sandbox.filesystem"

    def __init__(self, sandbox: CompatSandbox) -> None:
        self._sandbox = sandbox

    def read_bytes(self, remote_path: str) -> bytes:
        return self._sandbox.read(remote_path)

    def read_text(self, remote_path: str) -> str:
        return self._sandbox.read(remote_path).decode()

    def write_bytes(self, data: bytes | bytearray | memoryview, remote_path: str) -> None:
        self._sandbox.write(remote_path, bytes(data))

    def write_text(self, data: str, remote_path: str) -> None:
        self._sandbox.write(remote_path, data)

    def copy_from_local(self, local_path: str | os.PathLike, remote_path: str) -> None:
        with open(local_path, "rb") as handle:
            self._sandbox.write(remote_path, handle.read())

    def copy_to_local(self, remote_path: str, local_path: str | os.PathLike) -> None:
        data = self._sandbox.read(remote_path)
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        with open(local_path, "wb") as handle:
            handle.write(data)

    def list_files(self, remote_path: str) -> list[FileInfo]:
        return [_file_info(entry) for entry in self._sandbox.list(remote_path)]

    def stat(self, remote_path: str) -> FileInfo:
        return _file_info(self._sandbox.stat(remote_path))

    def make_directory(self, remote_path: str, *, create_parents: bool = True) -> None:
        self._sandbox.make_dir(remote_path, parents=create_parents)

    def remove(self, remote_path: str, *, recursive: bool = False) -> None:
        self._sandbox.remove(remote_path, recursive=recursive)

    def watch(
        self,
        remote_path: str,
        *,
        filter: list[FileWatchEventType] | None = None,
        recursive: bool = False,
        timeout: int | None = None,
    ) -> Iterator[FileWatchEvent]:
        """Watch a directory by polling its listing; a rename reads as a remove and a create."""
        if not self._sandbox.is_dir(remote_path):
            raise unsupported(ERRORS, "Sandbox.filesystem.watch on a file", "watch its directory.")
        deadline = None if timeout is None else time.monotonic() + timeout
        _, entries = self._sandbox.changes(remote_path, {}, recursive=recursive)
        while deadline is None or time.monotonic() < deadline:
            time.sleep(WATCH_INTERVAL)
            changes, entries = self._sandbox.changes(remote_path, entries, recursive=recursive)
            for change in changes:
                event = FileWatchEvent([posixpath.normpath(change.path)], _WATCH_EVENT_TYPES[change.kind])
                if filter is None or event.type in filter:
                    yield event


class Sandbox(_Layer):
    """A Modal sandbox running as an Arker VM. Like Modal, it is deleted
    ``timeout`` seconds (default 300) after it is created."""

    _compat_path = "Sandbox"

    def __init__(self, sandbox: CompatSandbox, settings: dict[str, Any], entrypoint: Process | None = None) -> None:
        self._sandbox = sandbox
        self._workdir = settings.get("workdir")
        self._ports = settings.get("ports", [])
        self._inbound = settings.get("inbound", [])
        self._entrypoint = entrypoint
        self._terminated_code: int | None = None
        self.object_id = sandbox.id
        self.filesystem = SandboxFilesystem(sandbox)
        self._streams = ContainerProcess(entrypoint) if entrypoint else None

    @property
    def stdout(self) -> StreamReader:
        return self._entrypoint_streams().stdout

    @property
    def stderr(self) -> StreamReader:
        return self._entrypoint_streams().stderr

    @property
    def stdin(self) -> StreamWriter:
        return self._entrypoint_streams().stdin

    def _entrypoint_streams(self) -> ContainerProcess:
        if self._streams is None:
            raise InvalidError("This sandbox has no entrypoint command; pass one to Sandbox.create().")
        return self._streams

    @property
    def arker(self) -> VM:
        """The Arker VM behind this sandbox, for anything this layer does not cover."""
        return self._sandbox.vm

    @staticmethod
    def create(
        *entrypoint_args: str,
        app: App | None = None,
        image: Image | None = None,
        env: dict[str, str | None] | None = None,
        secrets: list[Secret] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        workdir: str | None = None,
        cpu: float | None = None,
        memory: int | None = None,
        block_network: bool = False,
        cidr_allowlist: list[str] | None = None,
        outbound_cidr_allowlist: list[str] | None = None,
        outbound_domain_allowlist: list[str] | None = None,
        inbound_cidr_allowlist: list[str] | None = None,
        encrypted_ports: list[int] | tuple[int, ...] = (),
        name: str | None = None,
        tags: dict[str, str] | None = None,
        volumes: dict[str, Volume] | None = None,
        arker: dict[str, Any] | None = None,
        **options: Any,
    ) -> Sandbox:
        reject_options(ERRORS, "Sandbox.create", options, ignored=MODAL_CONNECTION_OPTIONS)
        if cidr_allowlist is not None and outbound_cidr_allowlist is not None:
            raise InvalidError("Cannot specify both `cidr_allowlist` and `outbound_cidr_allowlist`.")
        if any(not isinstance(volume, Volume) for volume in (volumes or {}).values()):
            raise InvalidError("Only volumes from Volume.from_name() can be mounted on Arker.")
        image = image or Image()
        envs = {**dict(image._env), **_env(env, secrets)}
        workdir = workdir or image._workdir
        settings = {
            "workdir": workdir,
            "ports": list(encrypted_ports),
            **({"inbound": list(inbound_cidr_allowlist)} if inbound_cidr_allowlist else {}),
            **({"tags": tags} if tags else {}),
        }
        sandbox = CompatSandbox.create(
            ERRORS,
            config=arker,
            image=image._registry,
            parent=image._snapshot,
            envs=envs,
            cpus=math.ceil(cpu) if cpu else None,
            memory_mib=memory,
            delete_after=timeout or DEFAULT_TIMEOUT,
            metadata=settings,
            name=name,
            volumes={path: volume._id(arker) for path, volume in (volumes or {}).items()},
        )
        try:
            for step in image._steps:
                step.apply(sandbox)
            egress = _egress(block_network, outbound_cidr_allowlist or cidr_allowlist, outbound_domain_allowlist)
            if egress:
                # After the build steps, which may need to download packages.
                sandbox.restrict_egress(egress)
            for port in encrypted_ports:
                sandbox.expose(port, from_ips=settings.get("inbound", ()))
            command = image._command(entrypoint_args)
            entrypoint = sandbox.start(command, cwd=workdir, stdin=True) if command else None
        except BaseException:
            sandbox.discard()
            raise
        return Sandbox(sandbox, settings, entrypoint)

    @staticmethod
    def from_id(sandbox_id: str, *, arker: dict[str, Any] | None = None, **options: Any) -> Any:
        """An id Arker does not know is handed to the ``modal`` package when it
        is installed, so existing Modal sandboxes keep working."""
        sandbox = CompatSandbox.connect(ERRORS, sandbox_id, arker)
        if sandbox is None:
            native = import_optional("modal")
            if native is None:
                raise NotFoundError(f"Sandbox {sandbox_id} not found")
            return native.Sandbox.from_id(sandbox_id, **options)
        return Sandbox(sandbox, sandbox.metadata())

    @staticmethod
    def from_name(app_name: str, name: str, *, arker: dict[str, Any] | None = None, **options: Any) -> Any:
        """The sandbox named ``name``; Arker names are unique in an organization, so
        ``app_name`` is not needed. A name Arker does not know is handed to the
        ``modal`` package when it is installed."""
        sandbox = CompatSandbox.connect(ERRORS, name, arker)
        if sandbox is None:
            native = import_optional("modal")
            if native is None:
                raise NotFoundError(f"Sandbox {name!r} not found in app {app_name!r}")
            return native.Sandbox.from_name(app_name, name, **options)
        return Sandbox(sandbox, sandbox.metadata())

    @staticmethod
    def list(
        *,
        app_id: str | None = None,
        tags: dict[str, str] | None = None,
        arker: dict[str, Any] | None = None,
        **options: Any,
    ) -> Iterator[Sandbox]:
        """The sandboxes this layer created that have at least ``tags``. Arker
        sandboxes belong to no app, so ``app_id`` does not narrow the list."""
        reject_options(
            ERRORS, "Sandbox.list", {"app_id": app_id, **options}, ignored=MODAL_CONNECTION_OPTIONS, inert={"app_id"}
        )
        for sandbox in CompatSandbox.find(ERRORS, arker):
            if (tags or {}).items() <= (sandbox.settings.get("tags") or {}).items():
                yield Sandbox(sandbox, sandbox.settings)

    def set_tags(self, tags: dict[str, str], *, client: Any = None) -> None:
        self._sandbox.update_metadata({"tags": tags})

    def get_tags(self) -> dict[str, str]:
        return self._sandbox.metadata().get("tags") or {}

    def snapshot_filesystem(self, timeout: int = 55, *, ttl: int | None = 30 * 24 * 3600) -> Image:
        """An image of this sandbox's files (and memory) for ``Sandbox.create``,
        deleted ``ttl`` seconds from now unless ``ttl`` is None."""
        snapshot = self._sandbox.snapshot(f"modal-snapshot-{uuid.uuid4().hex[:12]}", delete_after=ttl)
        return Image(_snapshot=snapshot.vm)

    def reload_volumes(self, *, timeout: int = 55) -> None:
        """Volumes are Arker filesystems, which every sandbox sees written at once, so there is nothing to reload."""

    def wait_until_ready(self, *, timeout: int = 300) -> None:
        """A sandbox is ready once created, so this returns at once."""
        if timeout <= 0:
            raise InvalidError(f"`timeout` must be positive, got: {timeout}")

    def detach(self) -> None:
        """This layer holds no connection to a sandbox, so there is nothing to close."""

    def exec(
        self,
        *args: str,
        workdir: str | None = None,
        env: dict[str, str | None] | None = None,
        secrets: list[Secret] | None = None,
        timeout: int | None = None,
        stdout: StreamType = StreamType.PIPE,
        stderr: StreamType = StreamType.PIPE,
        text: bool = True,
        bufsize: int = -1,
        pty: bool = False,
        **options: Any,
    ) -> ContainerProcess:
        """Start a command; it runs alongside other processes in the sandbox."""
        streams = {"stdout": stdout, "stderr": stderr}
        reject_options(
            ERRORS,
            "Sandbox.exec",
            {
                **options,
                "pty": pty or None,
                **{name: stream for name, stream in streams.items() if stream == StreamType.STDOUT},
            },
        )
        command = shlex.join(args)
        if stdout == StreamType.DEVNULL:
            command += " >/dev/null"
        if stderr == StreamType.DEVNULL:
            command += " 2>/dev/null"
        process = self._sandbox.start(
            command, cwd=workdir or self._workdir, envs=_env(env, secrets), timeout=timeout, stdin=True
        )
        return ContainerProcess(process, text)

    def open(self, path: str, mode: str = "r") -> _File:
        return _File(self._sandbox, path, mode)

    def ls(self, path: str) -> list[str]:
        return [entry.name for entry in self._sandbox.list(path)]

    def mkdir(self, path: str, parents: bool = False) -> None:
        self._sandbox.make_dir(path, parents=parents)

    def rm(self, path: str, recursive: bool = False) -> None:
        self._sandbox.remove(path, recursive=recursive)

    def tunnels(self, timeout: int = 50) -> dict[int, Tunnel]:
        return {port: Tunnel(self._sandbox.expose(port, from_ips=self._inbound)) for port in self._ports}

    @property
    def returncode(self) -> int | None:
        return self.poll()

    def poll(self) -> int | None:
        if self._terminated_code is not None:
            return self._terminated_code
        return self._entrypoint.poll() if self._entrypoint else None

    def wait(self, raise_on_termination: bool = True) -> None:
        if self._entrypoint is None:
            raise InvalidError("This sandbox has no entrypoint command to wait for; pass one to Sandbox.create().")
        self._entrypoint.wait()

    def terminate(self, *, wait: bool = False) -> int | None:
        """Delete the sandbox. With ``wait``, return its exit code: the
        entrypoint's if it had finished, else 137 as Modal reports a terminated sandbox."""
        finished = self.poll()
        self._sandbox.delete()
        self._terminated_code = 137 if finished is None else finished
        return self._terminated_code if wait else None


def _egress(block_network: bool, cidrs: list[str] | None, domains: list[str] | None) -> Egress | None:
    """Modal's network options: any allowlist denies what it does not name."""
    if block_network:
        return Egress(deny_all=True)
    if cidrs is None and domains is None:
        return None
    return Egress(allow_ips=tuple(cidrs or ()), allow_hosts=tuple(domains or ()), deny_all=True)


def _env(env: dict[str, str | None] | None, secrets: list[Secret] | None) -> dict[str, str]:
    merged = {name: value for name, value in (env or {}).items() if value is not None}
    for secret in secrets or []:
        if not isinstance(secret, Secret):
            raise InvalidError("Only secrets from Secret.from_dict() are supported on Arker.")
        merged.update(secret.env)
    return merged


# Each blocking call also has an ``.aio`` variant, awaited without blocking the
# event loop, as Modal's are: ``await Sandbox.create.aio()``, ``await process.wait.aio()``.
AsyncMethods.install(App, "lookup")
AsyncMethods.install(_VolumeManager, "delete")
AsyncMethods.install(Volume, "commit", "reload")
AsyncMethods.install(StreamReader, "read")
AsyncMethods.install(StreamWriter, "drain")
AsyncMethods.install(ContainerProcess, "poll", "wait")
AsyncMethods.install(_File, "read", "write", "flush", "close")
AsyncMethods.install(
    SandboxFilesystem,
    "read_bytes",
    "read_text",
    "write_bytes",
    "write_text",
    "copy_from_local",
    "copy_to_local",
    "list_files",
    "stat",
    "make_directory",
    "remove",
    "watch",
)
AsyncMethods.install(
    Sandbox,
    "create",
    "from_id",
    "from_name",
    "list",
    "set_tags",
    "get_tags",
    "snapshot_filesystem",
    "wait_until_ready",
    "detach",
    "exec",
    "open",
    "ls",
    "mkdir",
    "rm",
    "tunnels",
    "poll",
    "wait",
    "terminate",
)
