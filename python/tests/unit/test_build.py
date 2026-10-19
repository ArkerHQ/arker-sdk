"""The build driver: walking parsed instructions against a VM.

These tests use a fake VM that records calls, so they cover the part that is
actually ours - ordering, shell state, and how COPY resolves against the build
context - without needing a real VM.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
from types import SimpleNamespace

import pytest

from arker.build import BuildError, apply_steps
from arker.build_spec import parse_dockerfile


class FakeVM:
    """Records what the driver asks a VM to do."""

    def __init__(self, exit_codes=None):
        self.calls: list[tuple] = []
        self.ignores: list = []
        self.run_kwargs: list[dict] = []
        # Per-command exit codes, keyed by a substring of the command.
        self.exit_codes = exit_codes or {}

    def run(self, command, **kwargs):
        self.calls.append(("run", command))
        self.run_kwargs.append(kwargs)
        code = 0
        for needle, value in self.exit_codes.items():
            if needle in command:
                code = value
        return SimpleNamespace(exit_code=code, stdout="", stderr="")

    def sync(self, path, data=None):
        self.calls.append(("sync", path, len(data) if data else None))
        return

    def sync_dir(self, local_dir, remote_dir, **kwargs):
        self.calls.append(("sync_dir", os.path.basename(local_dir.rstrip("/")), remote_dir))
        self.ignores.append(kwargs.get("ignore") or (lambda rel: False))
        return

    @property
    def commands(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] == "run"]


@pytest.fixture
def context(tmp_path):
    (tmp_path / "app.js").write_text("console.log(1)\n")
    (tmp_path / "package.json").write_text("{}\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "index.js").write_text("//\n")
    return tmp_path


def build(text, context_root):
    vm = FakeVM()
    apply_steps(vm, parse_dockerfile(text).steps, str(context_root))
    return vm


def test_run_executes_in_order(context):
    vm = build("FROM x\nRUN one\nRUN two\n", context)
    assert vm.commands == ["one", "two"]


def test_workdir_is_created_then_entered(context):
    """Docker creates a WORKDIR that doesn't exist; a bare `cd` would fail."""
    vm = build("FROM x\nWORKDIR /app\nRUN pwd\n", context)
    assert vm.commands[0] == "mkdir -p /app && cd /app"
    assert vm.commands[1] == "pwd"


def test_env_is_exported_before_later_runs(context):
    vm = build("FROM x\nENV A=1 B=2\nRUN echo $A\n", context)
    assert vm.commands[0] == "export A=1 B=2"
    assert vm.commands[1] == "echo $A"


def test_env_values_are_quoted(context):
    """An unquoted value with a space would export only the first word."""
    vm = build('FROM x\nENV GREETING="hello world"\n', context)
    assert vm.commands == ["export GREETING='hello world'"]


def test_arg_with_a_default_behaves_like_env(context):
    vm = build("FROM x\nARG VERSION=1.2\nRUN echo $VERSION\n", context)
    assert vm.commands[0] == "export VERSION=1.2"


def test_arg_without_a_default_is_inert(context):
    """Matches Docker: an ARG with no default and no --build-arg is unset."""
    vm = build("FROM x\nARG NOVAL\nRUN echo hi\n", context)
    assert vm.commands == ["echo hi"]


def test_user_wraps_subsequent_runs(context):
    vm = build("FROM x\nUSER app\nRUN whoami\n", context)
    assert vm.commands == ["su -p app -c whoami"]


def test_user_does_not_wrap_earlier_runs(context):
    vm = build("FROM x\nRUN first\nUSER app\nRUN second\n", context)
    assert vm.commands[0] == "first"
    assert vm.commands[1].startswith("su -p app")


def test_copy_a_single_file_writes_it(context):
    vm = build("FROM x\nCOPY app.js /srv/app.js\n", context)
    assert ("sync", "/srv/app.js", len("console.log(1)\n")) in vm.calls


def test_copy_a_directory_syncs_its_contents(context):
    """Docker copies a directory's CONTENTS into the destination."""
    vm = build("FROM x\nCOPY src /app/src\n", context)
    assert ("sync_dir", "src", "/app/src") in vm.calls


def test_copy_the_whole_context(context):
    vm = build("FROM x\nCOPY . /app\n", context)
    assert ("sync_dir", os.path.basename(str(context)), "/app") in vm.calls


def test_copy_expands_a_glob(context):
    vm = build("FROM x\nCOPY package*.json /app/\n", context)
    synced = [c for c in vm.calls if c[0] == "sync"]
    assert any(c[1] == "/app/package.json" for c in synced), vm.calls


def test_copy_refuses_a_source_outside_the_context(context):
    """The context root is the boundary, exactly as `docker build` treats it."""
    with pytest.raises(BuildError, match="outside the build context"):
        build("FROM x\nCOPY ../secrets /app\n", context)


def test_copy_refuses_a_source_that_does_not_exist(context):
    with pytest.raises(BuildError, match="no such file"):
        build("FROM x\nCOPY nope.txt /app\n", context)


def test_copy_applies_chown_after_the_sync(context):
    vm = build("FROM x\nCOPY --chown=app:app src /app/src\n", context)
    assert vm.commands[-1] == "chown -R app:app /app/src"


def test_copy_runs_as_root_even_after_user(context):
    """Docker's COPY writes as root regardless of USER unless --chown says
    otherwise, so the sync must not be wrapped in the USER shell."""
    vm = build("FROM x\nUSER app\nCOPY app.js /srv/app.js\n", context)
    assert any(c[0] == "sync" for c in vm.calls)
    assert not any("su -p" in c for c in vm.commands)


def test_a_failed_add_fetch_aborts_the_build(context, monkeypatch):
    """A URL that cannot be fetched is a build failure, named by URL."""
    import arker.build as build_mod

    def boom(url, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(build_mod.urllib.request, "urlopen", boom)
    vm = FakeVM()
    steps = parse_dockerfile("FROM x\nADD https://example.com/f /f\n").steps
    with pytest.raises(BuildError, match=re.escape("https://example.com/f")):
        apply_steps(vm, steps, str(context))


def test_inert_instructions_produce_no_calls(context):
    vm = build('FROM x\nLABEL a=b\nEXPOSE 8080\nCMD ["true"]\n', context)
    assert vm.calls == []


def test_a_failing_run_aborts_the_build(context):
    """Docker fails a build on a non-zero RUN, and so must this.

    Without it a Dockerfile whose `RUN npm install` fails hands back a VM that
    looks built and is not, with every later instruction applied on top of the
    failure.
    """
    vm = FakeVM(exit_codes={"boom": 17})
    steps = parse_dockerfile("FROM x\nRUN ok-one\nRUN boom\nRUN never\n").steps
    with pytest.raises(BuildError, match="17"):
        apply_steps(vm, steps, str(context))
    assert vm.commands == ["ok-one", "boom"], "the step after the failure must not run"


def test_a_failing_copy_chown_aborts_the_build(context):
    """The chown after a COPY is a real command and can fail like any other."""
    vm = FakeVM(exit_codes={"chown": 1})
    steps = parse_dockerfile("FROM x\nCOPY --chown=nope:nope app.js /a.js\n").steps
    with pytest.raises(BuildError):
        apply_steps(vm, steps, str(context))


def test_add_url_is_fetched_by_the_client_not_the_guest(context, monkeypatch):
    """Docker downloads an ADD url on the BUILDER and copies the bytes in, so
    the image needs no curl or wget. Fetching inside the guest instead made
    `ADD` fail on any minimal base (ubuntu:24.04 ships neither), which is a
    divergence from Docker, not a limitation of it.

    The client has network access. Fetch there, then sync the bytes like COPY.
    """
    import arker.build as build_mod

    monkeypatch.setattr(
        build_mod.urllib.request,
        "urlopen",
        lambda url, timeout=None: contextlib.closing(io.BytesIO(b"remote-bytes")),
    )
    vm = FakeVM()
    steps = parse_dockerfile("FROM x\nADD https://example.com/f.txt /opt/f.txt\n").steps
    apply_steps(vm, steps, str(context))

    assert ("sync", "/opt/f.txt", len(b"remote-bytes")) in vm.calls, vm.calls
    # Nothing is asked of the guest: no curl, no wget, no shell at all.
    assert vm.commands == [], vm.commands


def test_dockerignore_excludes_files_from_a_copied_directory(context):
    """`COPY . /app` must not ship .git or .env into the VM."""
    (context / ".dockerignore").write_text(".git\nsecrets.env\n")
    (context / "secrets.env").write_text("TOKEN=hunter2\n")
    (context / ".git").mkdir()
    (context / ".git" / "config").write_text("[remote]\n")

    vm = FakeVM()
    apply_steps(vm, parse_dockerfile("FROM x\nCOPY . /app\n").steps, str(context))

    sync_dirs = [c for c in vm.calls if c[0] == "sync_dir"]
    assert len(sync_dirs) == 1, vm.calls
    ignore = vm.ignores[-1]
    assert ignore(".git/config"), "an ignored directory's contents must be excluded"
    assert ignore("secrets.env")
    assert not ignore("app.js")


def test_dockerignore_survives_a_symlinked_context_root(tmp_path, context):
    """Regression: the context reached through a symlink (macOS /tmp ->
    /private/tmp) made every rule silently stop matching.

    `_resolve_sources` returns realpath'd paths, so comparing them against an
    unresolved root produced a relpath like `../../private/tmp/...`, which
    matches no pattern. The failure mode is the dangerous one: the build
    succeeds and quietly copies everything.
    """
    (context / ".dockerignore").write_text("secrets.env\n")
    (context / "secrets.env").write_text("TOKEN=hunter2\n")

    link = tmp_path / "via-symlink"
    if link.exists() or link.is_symlink():
        link.unlink()
    os.symlink(str(context), str(link))

    vm = FakeVM()
    apply_steps(vm, parse_dockerfile("FROM x\nCOPY . /app\n").steps, str(link))
    assert vm.ignores[-1]("secrets.env"), "rules must still apply through a symlink"


def test_env_with_a_variable_reference_expands(context):
    """`ENV PATH=/opt/bin:$PATH` is one of the most common Dockerfile lines.

    Single-quoting the value set the literal string `$PATH`, breaking every
    later RUN and every run() the customer makes - with a "command not found"
    three steps away from the cause.
    """
    vm = build("FROM x\nENV PATH=/opt/bin:$PATH\n", context)
    assert vm.commands == ['export PATH="/opt/bin:$PATH"']


def test_env_without_a_variable_stays_single_quoted(context):
    """Only `$` needs the weaker quoting; everything else keeps the strong form."""
    vm = build('FROM x\nENV MSG="hello world"\n', context)
    assert vm.commands == ["export MSG='hello world'"]


def test_env_and_workdir_after_user_are_not_wrapped(context):
    """`su -c` is a fresh process, so wrapping an export or a cd threw the
    state away: `ENV` after `USER` silently did nothing at all.

    Docker treats ENV/WORKDIR as metadata that USER does not affect.
    """
    vm = build("FROM x\nUSER app\nENV AFTER=1\nWORKDIR /srv\nRUN hi\n", context)
    assert vm.commands == [
        "export AFTER=1",
        "mkdir -p /srv && cd /srv",
        "su -p app -c hi",
    ]


def test_copy_preserves_the_executable_bit(context):
    script = context / "entrypoint.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)

    vm = FakeVM()
    steps = parse_dockerfile("FROM x\nCOPY entrypoint.sh /app/e.sh\n").steps
    apply_steps(vm, steps, str(context))

    assert any("chmod" in c and "+x" in c for c in vm.commands), vm.commands


def test_copy_leaves_a_plain_file_unexecutable(context):
    vm = FakeVM()
    steps = parse_dockerfile("FROM x\nCOPY app.js /app/app.js\n").steps
    apply_steps(vm, steps, str(context))

    assert not any("chmod" in c for c in vm.commands), vm.commands


def test_multiple_directory_sources_merge_into_the_destination(context):
    for name in ("x", "y"):
        directory = context / name
        directory.mkdir()
        (directory / f"{name}.txt").write_text(name)

    vm = FakeVM()
    apply_steps(vm, parse_dockerfile("FROM x\nCOPY x y /dest/\n").steps, str(context))

    targets = [c[2] for c in vm.calls if c[0] == "sync_dir"]
    assert targets == ["/dest", "/dest"], targets


def test_add_checksum_mismatch_fails_the_build(context, monkeypatch):
    import arker.build as build_mod

    monkeypatch.setattr(
        build_mod.urllib.request,
        "urlopen",
        lambda url, timeout=None: contextlib.closing(io.BytesIO(b"tampered")),
    )
    wrong = "sha256:" + "0" * 64
    steps = parse_dockerfile(f"FROM x\nADD --checksum={wrong} https://example.com/f /f\n").steps
    with pytest.raises(BuildError, match="checksum"):
        apply_steps(FakeVM(), steps, str(context))


def test_add_checksum_match_is_accepted(context, monkeypatch):
    import hashlib

    import arker.build as build_mod

    payload = b"trusted"
    monkeypatch.setattr(
        build_mod.urllib.request,
        "urlopen",
        lambda url, timeout=None: contextlib.closing(io.BytesIO(payload)),
    )
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    steps = parse_dockerfile(f"FROM x\nADD --checksum={digest} https://example.com/f /f\n").steps

    vm = FakeVM()
    apply_steps(vm, steps, str(context))
    assert ("sync", "/f", len(payload)) in vm.calls, vm.calls


def test_copy_dir_to_a_relative_dest_resolves_against_workdir(context):
    """Relative COPY destinations resolve against WORKDIR."""
    vm = build("FROM x\nWORKDIR /app\nCOPY src/ ./\n", context)
    dests = [c[2] for c in vm.calls if c[0] == "sync_dir"]
    assert dests == ["/app"], dests


def test_copy_file_to_a_relative_dest_resolves_against_workdir(context):
    vm = build("FROM x\nWORKDIR /app\nCOPY app.js ./\n", context)
    dests = [c[1] for c in vm.calls if c[0] == "sync"]
    assert dests == ["/app/app.js"], dests


def test_copy_to_a_relative_subdir_resolves_against_workdir(context):
    vm = build("FROM x\nWORKDIR /app\nCOPY src/ ./lib\n", context)
    dests = [c[2] for c in vm.calls if c[0] == "sync_dir"]
    assert dests == ["/app/lib"], dests


def test_copy_with_no_workdir_resolves_against_root(context):
    """Docker's implicit WORKDIR is /."""
    vm = build("FROM x\nCOPY src/ ./\n", context)
    dests = [c[2] for c in vm.calls if c[0] == "sync_dir"]
    assert dests == ["/"], dests


def test_absolute_copy_dest_is_unchanged_by_workdir(context):
    vm = build("FROM x\nWORKDIR /app\nCOPY src/ /opt/src\n", context)
    dests = [c[2] for c in vm.calls if c[0] == "sync_dir"]
    assert dests == ["/opt/src"], dests


def test_build_steps_inherit_the_fork_queueing_window(context):
    vm = FakeVM()
    steps = parse_dockerfile("FROM ubuntu\nRUN echo one\nRUN echo two\n").steps

    apply_steps(vm, steps, str(context), queueing_timeout=900)

    assert vm.commands, "expected the driver to issue RUN commands"
    assert all(kw.get("queueing_timeout") == 900 for kw in vm.run_kwargs)


def test_build_steps_omit_the_window_when_the_fork_had_none(context):
    vm = FakeVM()
    steps = parse_dockerfile("FROM ubuntu\nRUN echo one\n").steps

    apply_steps(vm, steps, str(context))

    assert all(kw.get("queueing_timeout") is None for kw in vm.run_kwargs)
