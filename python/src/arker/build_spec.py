"""Parse a Dockerfile into the instruction list the SDK build driver walks.

Text in, instructions out - no network, no VM, no filesystem. The driver in
``build.py`` is what executes them: ``FROM`` becomes a real image fork, ``RUN``
becomes ``vm.run(...)``, ``COPY`` becomes a directory sync into the VM.

## Why this lives in the SDK

``COPY`` sources are local, so Dockerfile builds are driven by the SDK.
``COPY`` uses ``vm.sync_dir(...)`` and sends only changed files.

## Scope

Supported: ``FROM``, ``RUN``, ``COPY``, ``ADD <url>``, ``ENV``, ``WORKDIR``,
``USER``, ``ARG``, ``LABEL``, ``EXPOSE``, ``ENTRYPOINT``, ``CMD``, ``SHELL``.

``SHELL`` replaces the interpreter for the shell form of later ``RUN``,
``ENTRYPOINT`` and ``CMD``, as it does in Docker. It is state, not a step:
nothing executes it. Exec form is left alone throughout, because it does not go
through an interpreter at all.

Refused by name, rather than silently dropped:

* **Multi-stage builds** - more than one ``FROM``, or a ``COPY --from=``.
  Resolving stage aliases and deciding what a final stage means for a single
  long-lived VM is its own feature.
* **``ARG``-substituted ``FROM``** - ``FROM`` resolves through a real image
  fork, so it has to be a literal reference.
* **Everything else** (``VOLUME``, ``ONBUILD``, ``HEALTHCHECK``,
  ``STOPSIGNAL``, ``MAINTAINER``, ...) - named in the error so an unsupported
  directive never passes silently.

``${VAR}`` substitution into instruction text is not performed. A ``RUN`` sees
``$VAR`` as an ordinary shell variable because the shell expands it; a
``WORKDIR /app-$VERSION`` does not, and is left as written.
"""

from __future__ import annotations

import io
import json
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field

from dockerfile_parse import DockerfileParser

__all__ = [
    "Add",
    "Arg",
    "Cmd",
    "Copy",
    "DockerfileError",
    "Entrypoint",
    "Env",
    "Expose",
    "Label",
    "ParsedDockerfile",
    "Run",
    "Step",
    "User",
    "Workdir",
    "parse_dockerfile",
]


class DockerfileError(ValueError):
    """A Dockerfile this SDK will not build, with the reason named."""


@dataclass(frozen=True)
class Run:
    """``RUN <cmd>`` - executed against the VM via ``vm.run``."""

    command: str


@dataclass(frozen=True)
class Copy:
    """``COPY <src>... <dest>`` - synced from the build context into the VM.

    ``sources`` are as written, relative to the context root; the driver
    resolves and globs them locally. ``chown`` is carried rather than dropped
    so the driver can apply it after the sync.
    """

    sources: list[str]
    dest: str
    chown: str | None = None


@dataclass(frozen=True)
class Add:
    """``ADD <url> <dest>`` - fetched inside the guest.

    Only the URL form is supported. A local-source ``ADD`` differs from
    ``COPY`` mainly by auto-extracting archives, which this does not do, so it
    is refused rather than silently behaving like ``COPY``.
    """

    url: str
    dest: str
    checksum: str | None = None


@dataclass(frozen=True)
class Env:
    """``ENV k=v [k=v ...]`` - exported for later ``RUN``s and persisted."""

    pairs: list[tuple[str, str]]


@dataclass(frozen=True)
class Workdir:
    """``WORKDIR <dir>`` - created if missing, then ``cd``'d into."""

    path: str


@dataclass(frozen=True)
class User:
    """``USER <name>`` - subsequent ``RUN``s execute as this user."""

    name: str


@dataclass(frozen=True)
class Arg:
    """``ARG name[=default]``.

    There is no ``--build-arg`` channel, so a declared default is the only
    value it can take; one without a default is inert, matching Docker's own
    unset-without-override behaviour.
    """

    name: str
    default: str | None = None


@dataclass(frozen=True)
class Label:
    """``LABEL k=v`` - recorded, no runtime effect on a VM."""

    pairs: list[tuple[str, str]]


@dataclass(frozen=True)
class Expose:
    """``EXPOSE <port>`` - recorded; VMs are reached via run/SSH, not -p."""

    port: str


@dataclass(frozen=True)
class Entrypoint:
    """``ENTRYPOINT`` - composed with ``CMD`` by the driver."""

    command: str


@dataclass(frozen=True)
class Cmd:
    """``CMD`` - arguments to ``ENTRYPOINT``, or the whole command alone."""

    command: str


Step = Run | Copy | Add | Env | Workdir | User | Arg | Label | Expose | Entrypoint | Cmd


@dataclass
class ParsedDockerfile:
    """A Dockerfile that passed validation."""

    base_image: str
    steps: list[Step] = field(default_factory=list)


#: Directives that carry no execution semantics for a VM but are accepted so a
#: real-world Dockerfile parses. They are recorded, never silently dropped.
_INERT = {"LABEL", "EXPOSE"}

#: Everything this SDK knows about. Anything else is named in the error.
_KNOWN = {
    "FROM",
    "RUN",
    "COPY",
    "ADD",
    "ENV",
    "WORKDIR",
    "USER",
    "ARG",
    "ENTRYPOINT",
    "CMD",
    "SHELL",
} | _INERT


def _instructions(text: str) -> Iterator[tuple[str, str]]:
    """Yield ``(DIRECTIVE, argument)`` pairs from Dockerfile text.

    Lexing is delegated to ``dockerfile-parse`` rather than hand-rolled. That
    layer - line continuations, comments (including a ``#`` line *inside* a
    continued instruction), the ``# escape=`` parser directive, CRLF - is
    exactly where a hand-written splitter goes quietly wrong, and it is the
    part this library does well. It hands back the instruction NAME and its
    raw argument string; parsing the arguments (flags, exec-form JSON,
    ``key=value``) is still ours, because the library does not go that deep.

    It is also what Harbor and every sandbox provider it ships (e2b, modal,
    runloop, novita, blaxel, beam, …) use for this same job, and Arker is
    itself a Harbor provider.
    """
    parser = DockerfileParser(fileobj=io.BytesIO())
    parser.content = text
    for instruction in parser.structure:
        directive = str(instruction.get("instruction", "")).upper()
        if directive == "COMMENT":
            continue
        yield directive, str(instruction.get("value", "")).strip()


def _exec_form(argument: str) -> list[str] | None:
    """Return the argv of an exec-form instruction, or None if shell-form."""
    if not argument.startswith("["):
        return None
    try:
        parsed = json.loads(argument)
    except ValueError:
        return None
    if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
        return parsed
    return None


def _command_line(argument: str, shell: list[str] | None) -> str:
    """Render RUN/ENTRYPOINT/CMD as one shell command line.

    Exec form does not go through a shell in Docker, but the only execution
    primitive we have is "a command line in a shell", so each element is quoted
    and joined - which invokes the same argv a direct exec would.

    ``shell`` is the interpreter set by a preceding ``SHELL``. Docker applies it
    to the SHELL FORM of all three instructions, and to exec form of none of
    them, which is what the branch below encodes. Absent a ``SHELL`` the
    argument is passed through untouched, so the guest's own default shell runs
    it exactly as before.
    """
    argv = _exec_form(argument)
    if argv is not None:
        return " ".join(shlex.quote(item) for item in argv)
    if shell is None:
        return argument
    return " ".join(shlex.quote(part) for part in [*shell, argument])


def _validate_env_key(key: str, directive: str) -> str:
    """A key is a bare token in `export k=v` and CANNOT be quoted.

    So it is the one interpolation in the whole driver that quoting cannot
    make safe, and it has to be validated instead. `ENV a;id;b=1` parses
    happily and would emit `export a;id;b=1`. Docker refuses the same input
    with "invalid environment variable name".
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise DockerfileError(
            f"{directive} name {key!r} is not a valid environment variable name "
            "(letters, digits and underscore, not starting with a digit)"
        )
    return key


def _key_value_pairs(argument: str, directive: str) -> list[tuple[str, str]]:
    """Parse ``k=v k=v`` and the legacy ``ENV key value`` single-pair form."""
    try:
        tokens = shlex.split(argument)
    except ValueError as error:
        raise DockerfileError(f"{directive} is not parseable: {error}") from error
    if not tokens:
        raise DockerfileError(f"{directive} requires at least one key")
    if "=" not in tokens[0]:
        # Legacy `ENV key value with spaces` - everything after the key is the
        # value, so it cannot be split into further pairs.
        if len(tokens) < 2:
            raise DockerfileError(f"{directive} {tokens[0]} has no value")
        return [(_validate_env_key(tokens[0], directive), " ".join(tokens[1:]))]
    pairs = []
    for token in tokens:
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise DockerfileError(f"{directive} expects key=value, got: {token}")
        pairs.append((_validate_env_key(key, directive), value))
    return pairs


def _split_flags(argument: str, directive: str) -> tuple[dict[str, str], list[str]]:
    """Peel leading ``--flag=value`` tokens off an instruction's arguments."""
    try:
        tokens = shlex.split(argument)
    except ValueError as error:
        raise DockerfileError(f"{directive} is not parseable: {error}") from error
    flags: dict[str, str] = {}
    while tokens and tokens[0].startswith("--"):
        name, _, value = tokens.pop(0)[2:].partition("=")
        flags[name] = value
    return flags, tokens


def parse_dockerfile(text: str) -> ParsedDockerfile:
    """Parse and validate ``text``, or raise :class:`DockerfileError`."""
    base_image: str | None = None
    steps: list[Step] = []
    from_count = 0
    shell: list[str] | None = None

    for directive, argument in _instructions(text):
        if directive not in _KNOWN:
            raise DockerfileError(
                f"`{directive}` is not supported; supported directives are "
                f"{', '.join(sorted(_KNOWN))} (single-stage only)"
            )

        if directive == "FROM":
            from_count += 1
            if from_count > 1:
                raise DockerfileError(
                    "multi-stage dockerfiles are not supported (found more than one "
                    "FROM): flatten to a single stage, or fork from an already-built "
                    "image reference"
                )
            image = argument.split(" AS ")[0].split(" as ")[0].strip()
            if "$" in image:
                raise DockerfileError(
                    f"FROM referencing a build ARG (`{image}`) is not supported: give a "
                    "literal base image reference, for example `FROM ubuntu:24.04`"
                )
            if not image:
                raise DockerfileError("FROM requires an image reference")
            base_image = image

        elif directive == "RUN":
            if not argument:
                raise DockerfileError("RUN requires a command")
            steps.append(Run(_command_line(argument, shell)))

        elif directive == "COPY":
            flags, tokens = _split_flags(argument, "COPY")
            if "from" in flags:
                raise DockerfileError(
                    f"COPY --from={flags['from']} is not supported: cross-stage copies "
                    "need multi-stage builds, which this path does not implement"
                )
            if len(tokens) < 2:
                raise DockerfileError(f"COPY requires at least one source and a destination, got: {argument}")
            steps.append(Copy(tokens[:-1], tokens[-1], chown=flags.get("chown")))

        elif directive == "ADD":
            flags, tokens = _split_flags(argument, "ADD")
            if len(tokens) != 2:
                raise DockerfileError(
                    f"ADD {argument} is not supported: only `ADD <url> <dest>` is, with a "
                    "single URL source and destination"
                )
            source, dest = tokens
            if not source.startswith(("http://", "https://")):
                raise DockerfileError(
                    f"ADD {source} is not supported: only a URL source is. Use COPY for "
                    "local files - note ADD's archive auto-extraction is not implemented, "
                    "which is why a local ADD is refused rather than treated as COPY"
                )
            steps.append(Add(source, dest, checksum=flags.get("checksum")))

        elif directive == "ENV":
            steps.append(Env(_key_value_pairs(argument, "ENV")))

        elif directive == "WORKDIR":
            if not argument:
                raise DockerfileError("WORKDIR requires a directory")
            steps.append(Workdir(argument))

        elif directive == "USER":
            if not argument:
                raise DockerfileError("USER requires a username")
            steps.append(User(argument))

        elif directive == "ARG":
            name, sep, default = argument.partition("=")
            name = name.strip()
            if not name:
                raise DockerfileError("ARG requires a name")
            _validate_env_key(name, "ARG")
            steps.append(Arg(name, default.strip() if sep else None))

        elif directive == "LABEL":
            steps.append(Label(_key_value_pairs(argument, "LABEL")))

        elif directive == "EXPOSE":
            if not argument:
                raise DockerfileError("EXPOSE requires a port")
            steps.append(Expose(argument))

        elif directive == "ENTRYPOINT":
            steps.append(Entrypoint(_command_line(argument, shell)))

        elif directive == "CMD":
            steps.append(Cmd(_command_line(argument, shell)))

        elif directive == "SHELL":
            argv = _exec_form(argument)
            if argv is None:
                raise DockerfileError('SHELL must be given in exec form, for example `SHELL ["/bin/bash", "-c"]`')
            if not argv:
                raise DockerfileError("SHELL requires at least one element, the interpreter to run")
            shell = argv

    if base_image is None:
        raise DockerfileError("dockerfile has no FROM instruction")
    return ParsedDockerfile(base_image=base_image, steps=steps)
