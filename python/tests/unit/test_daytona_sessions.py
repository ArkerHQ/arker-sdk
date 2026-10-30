from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import os
import shlex
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from arker._daytona_sessions import SessionProcess


@pytest.fixture
def session(tmp_path):
    source = Path(__file__).parents[2] / "src/arker/_guest_daytona_session.py"
    spec = importlib.util.spec_from_file_location("guest_session", source)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    identifier = "test-" + uuid.uuid4().hex
    # Emulate a login account even when the test runner is the non-login nobody user.
    login_shell = tmp_path / "login-shell"
    selected = tmp_path / "selected-shell"
    login_shell.write_text("#!/bin/sh\nprintf selected > " + shlex.quote(str(selected)) + "\nexec /bin/sh\n")
    login_shell.chmod(0o700)
    launcher = """import pwd, runpy, sys
lookup = pwd.getpwuid

def account(uid):
    fields = list(lookup(uid))
    fields[6] = sys.argv[3]
    return pwd.struct_passwd(fields)

pwd.getpwuid = account
runpy.run_path(sys.argv[1])["serve"](sys.argv[2])
"""
    process = subprocess.Popen(
        [sys.executable, "-c", launcher, str(source), identifier, str(login_shell)],
        cwd=tmp_path,
        start_new_session=True,
    )
    owner = SessionProcess()
    owner._session_call = lambda action, request_timeout=None, **values: helper.call({"action": action, **values})
    try:
        deadline = time.monotonic() + 5
        while not (helper.address(identifier) / "control").exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        yield owner, identifier
    finally:
        try:
            helper.call({"action": "delete", "session_id": identifier})
            process.wait(timeout=5)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def test_persistent_shell_preserves_cwd_exports_and_functions(session, tmp_path):
    owner, identifier = session
    result = owner.execute_session_command(
        identifier,
        SimpleNamespace(command="export KEPT=yes; cd /tmp; retained() { printf function; }; printf first"),
        timeout=5,
    )
    assert result.stdout == "first\n"
    assert result.output == "\1\1\1first\n"
    result = owner.execute_session_command(
        identifier,
        SimpleNamespace(
            command='printf \'%s:%s:\' "$KEPT" "$PWD"; retained; test -t 0; printf \'tty=%s\' "$?"; (exit 7)'
        ),
        timeout=5,
    )
    assert result.stdout == "yes:/tmp:functiontty=1\n"
    assert result.exit_code == 7
    assert len(owner.get_session(identifier).commands) == 2


def test_streaming_input_and_awaited_callbacks(session):
    owner, identifier = session

    async def exercise():
        result = owner.execute_session_command(
            identifier,
            SimpleNamespace(
                command="printf ready; read value; printf '%s' \"$value\"; printf error >&2",
                run_async=True,
                suppress_input_echo=True,
            ),
        )
        deadline = time.monotonic() + 5
        while True:
            try:
                owner.send_session_command_input(identifier, result.cmd_id, "hello\n")
                break
            except RuntimeError as error:
                assert "not running" in str(error)
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
        output, errors = [], []

        async def stdout(text):
            await asyncio.sleep(0)
            output.append(text)

        await asyncio.wait_for(
            owner.get_session_command_logs_async(identifier, result.cmd_id, stdout, errors.append),
            5,
        )
        assert "".join(output) == "readyhello\n"
        assert "".join(errors) == "error\n"
        assert owner.get_session_command(identifier, result.cmd_id).exit_code == 0

    asyncio.run(exercise())


def test_stream_reader_cancellation_does_not_cancel_command(session):
    owner, identifier = session

    async def exercise():
        result = owner.execute_session_command(
            identifier,
            SimpleNamespace(
                command="read value; printf '%s' \"$value\"",
                run_async=True,
                suppress_input_echo=True,
            ),
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                owner.get_session_command_logs_async(identifier, result.cmd_id, lambda _: None, lambda _: None),
                0.05,
            )
        assert owner.get_session_command(identifier, result.cmd_id).exit_code is None
        owner.send_session_command_input(identifier, result.cmd_id, "survived\n")
        chunks = []
        await asyncio.wait_for(
            owner.get_session_command_logs_async(identifier, result.cmd_id, chunks.append, lambda _: None),
            5,
        )
        assert "".join(chunks) == "survived\n"

    asyncio.run(exercise())


def test_large_completed_line_streams_before_command_completion(session):
    owner, identifier = session

    async def exercise():
        result = owner.execute_session_command(
            identifier,
            SimpleNamespace(
                command="head -c 2097152 /dev/zero | tr '\\0' X; printf '\\n'; read done",
                run_async=True,
                suppress_input_echo=True,
            ),
        )
        received = 0
        ready = asyncio.Event()

        def output(text):
            nonlocal received
            received += len(text)
            if received >= 2097152:
                ready.set()

        pending = asyncio.create_task(
            owner.get_session_command_logs_async(identifier, result.cmd_id, output, lambda _: None)
        )
        try:
            await asyncio.wait_for(ready.wait(), 10)
            assert owner.get_session_command(identifier, result.cmd_id).exit_code is None
            owner.send_session_command_input(identifier, result.cmd_id, "done\n")
            await asyncio.wait_for(pending, 5)
            assert received == 2097153
        finally:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending

    asyncio.run(exercise())


def test_default_input_echo_and_completed_input_refusal(session):
    owner, identifier = session
    result = owner.execute_session_command(
        identifier, SimpleNamespace(command="read value; printf answer", run_async=True)
    )
    owner.send_session_command_input(identifier, result.cmd_id, "hello\n")
    deadline = time.monotonic() + 5
    while owner.get_session_command(identifier, result.cmd_id).exit_code is None:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert owner.get_session_command_logs(identifier, result.cmd_id).stdout == "hello\nanswer\n"
    with pytest.raises(RuntimeError, match="completed"):
        owner.send_session_command_input(identifier, result.cmd_id, "extra")


def test_wait_timeout_leaves_command_running_and_retrievable(session):
    owner, identifier = session
    with pytest.raises(TimeoutError, match="continues running"):
        owner.execute_session_command(identifier, SimpleNamespace(command="read value; printf kept"), timeout=0.05)
    command = owner.get_session(identifier).commands[0]
    assert command.exit_code is None
    owner.send_session_command_input(identifier, command.id, "go\n")
    deadline = time.monotonic() + 5
    while owner.get_session_command(identifier, command.id).exit_code is None:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert owner.get_session_command_logs(identifier, command.id).stdout.endswith("kept\n")


def test_invalid_request_timeout_refuses_before_guest_work():
    owner = SessionProcess()
    owner._vm = SimpleNamespace(sync=lambda *_: pytest.fail("must not stage"))
    with pytest.raises(Exception, match="finite non-negative"):
        owner.create_session("test", request_timeout=-1)
    with pytest.raises(Exception, match="finite non-negative"):
        owner.get_session("test", request_timeout=float("nan"))


def test_async_cancelled_session_creation_reconciles_exact_admission():
    import threading

    from arker._compat_daytona_async import _AsyncProcess

    async def exercise():
        entered, release, cleaned = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        discarded = []

        def create(*_):
            entered.set()
            assert release.wait(5)
            return "unique-admitted-process"

        def discard(session_id, identifier):
            discarded.append((session_id, identifier))
            cleaned.set()

        process = _AsyncProcess(SimpleNamespace(_create_session_owned=create, _discard_created_session=discard))
        pending = asyncio.create_task(process.create_session("session"))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            pending.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert await asyncio.to_thread(cleaned.wait, 5)
            assert discarded == [("session", "unique-admitted-process")]
        finally:
            release.set()

    asyncio.run(exercise())


def test_creation_cleanup_failure_preserves_readiness_error_and_owned_id(monkeypatch):
    import arker._daytona_sessions as module

    primary, cleanup = RuntimeError("readiness failed"), RuntimeError("kill failed")
    killed = []

    class Manager:
        def __init__(self, vm):
            pass

        def exec(self, request):
            return SimpleNamespace(name=request["name"])

        def get(self, name):
            return SimpleNamespace(status="failed")

        def kill(self, name):
            killed.append(name)
            raise cleanup

    owner = SessionProcess()
    owner._vm = object()
    owner._session_source = lambda: "/source.py"
    owner.get_session = lambda *_: (_ for _ in ()).throw(primary)
    monkeypatch.setattr(module, "GuestProcesses", Manager)
    monkeypatch.setattr(module, "run_command", lambda *_: SimpleNamespace(stdout='["/tmp", "/bin/sh"]'))
    with pytest.raises(RuntimeError) as raised:
        owner.create_session("public-session")
    assert raised.value is primary
    assert raised.value.__cause__ is cleanup
    assert raised.value.session_id == "public-session"
    assert raised.value.process_id == killed[0]
    assert len(killed) == 1
    assert killed[0].startswith("daytona-session:")
    assert killed[0] != "public-session"


def test_session_uses_account_shell_and_preserves_runner_identity(session, tmp_path):
    owner, identifier = session
    result = owner.execute_session_command(
        identifier,
        SimpleNamespace(command='printf \'%s:%s\' "$(id -u)" "$(id -g)"'),
        timeout=5,
    )
    assert result.stdout == f"{os.getuid()}:{os.getgid()}\n"
    assert (tmp_path / "selected-shell").read_text() == "selected"


def test_session_commands_keep_attributes_without_native_models(session, monkeypatch):
    from arker import _daytona_sessions

    monkeypatch.setattr(_daytona_sessions, "import_optional", lambda _: None)
    owner, identifier = session
    result = owner.execute_session_command(identifier, SimpleNamespace(command="printf output"), timeout=5)
    command = owner.get_session(identifier).commands[0]
    assert command.id == result.cmd_id
    assert command.exit_code == 0
    listed = next(item for item in owner.list_sessions() if item.session_id == identifier)
    assert listed.commands[0].id == command.id
    assert owner.get_session_command(identifier, command.id).exit_code == command.exit_code


def test_partial_lines_publish_only_when_complete_with_separate_stream_tags(session, tmp_path):
    owner, identifier = session
    marker = tmp_path / "partial-ready"
    command = owner.execute_session_command(
        identifier,
        SimpleNamespace(
            command=f"printf first; printf err >&2; touch {marker}; read value; printf second; printf rest >&2",
            run_async=True,
            suppress_input_echo=True,
        ),
    )
    deadline = time.monotonic() + 5
    while not marker.exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    before = owner.get_session_command_logs(identifier, command.cmd_id)
    assert (before.stdout, before.stderr, before.output) == ("", "", "")
    owner.send_session_command_input(identifier, command.cmd_id, "go\n")
    while owner.get_session_command(identifier, command.cmd_id).exit_code is None:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    after = owner.get_session_command_logs(identifier, command.cmd_id)
    assert after.stdout == "firstsecond\n"
    assert after.stderr == "errrest\n"
    out, err = "\1\1\1firstsecond\n", "\2\2\2errrest\n"
    assert after.output in (out + err, err + out)
