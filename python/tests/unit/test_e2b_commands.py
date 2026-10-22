import os
import signal
import subprocess
import types
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

import arker.e2b as shim
from arker._compat import CommandResult


def test_identity_command_exec_preserves_pid_and_signal_target():
    command = shim._identity_command(
        'printf \'%s %s\\n\' "$$" "$(id -u)"; exec sleep 30',
        user="root",
    )
    process = subprocess.Popen(
        ["bash", "-c", "exec " + command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    pid, actual_uid = process.stdout.readline().split()
    assert (int(pid), int(actual_uid)) == (process.pid, 0)
    os.kill(process.pid, signal.SIGTERM)
    assert process.wait(timeout=2) == -signal.SIGTERM


def test_native_creation_does_not_override_an_omitted_internet_policy(monkeypatch):
    calls = []

    class Native:
        @classmethod
        def create(cls, **options):
            calls.append(options)
            return types.SimpleNamespace(sandbox_id="native")

    monkeypatch.setattr(shim.Sandbox, "_native_sandbox", staticmethod(lambda: Native))
    monkeypatch.setattr(shim, "routed_create", lambda provider, config, creators: ("e2b", creators["e2b"]()))
    shim.Sandbox.create()
    assert calls == [{}]


def test_native_command_environment_uses_the_vendor_envs_option():
    calls = []
    native = types.SimpleNamespace(run=lambda command, **options: calls.append((command, options)))
    shim.Commands(native=native).run("echo value", envs={"VALUE": "kept"})
    assert calls == [("echo value", {"envs": {"VALUE": "kept"}})]


def test_arker_command_accepts_vendor_environment_and_raises_vendor_exit(monkeypatch):
    calls = []

    def run(vm, command, **options):
        calls.append(command)
        return CommandResult(stdout="output", stderr="failure", exit_code=17)

    monkeypatch.setattr(shim, "run_command", run)
    with pytest.raises(shim.CommandExitException) as raised:
        shim.Commands(vm=object()).run("exit 17", envs={"VALUE": "kept"})
    assert raised.value.exit_code == 17
    assert raised.value.stdout == "output"
    assert raised.value.stderr == "failure"
    assert raised.value.error == "exit status 17"
    assert len(calls) == 1
    assert calls[0].startswith("setpriv --reuid=user --regid=user --init-groups -- sh -c ")
    assert "export HOME=/home/user" in calls[0]
    assert "export VALUE=kept" in calls[0]
    assert "cd /home/user" in calls[0]


def test_background_returns_a_native_handle_with_stdin_eof_and_typed_wait(monkeypatch):
    native = pytest.importorskip("e2b")
    calls = []
    current = types.SimpleNamespace(
        pid="42", name="job", status="completed", stdout="hello\n", stderr="diagnostic", exit_code=17
    )

    class Processes:
        def __init__(self, vm):
            pass

        def exec(self, request):
            calls.append(request)
            return current

        def get(self, identifier, **options):
            assert str(identifier) in {"42", "job"}
            return current

        def write_stdin(self, identifier, data):
            calls.append(("stdin", str(identifier), data))

        def close_stdin(self, identifier):
            calls.append(("eof", str(identifier)))

    monkeypatch.setattr(shim, "GuestProcesses", Processes, raising=False)
    commands = shim.Commands(vm=object())
    handle = commands.run("cat; printf diagnostic >&2; exit 17", background=True, stdin=True)
    assert isinstance(handle, native.CommandHandle)
    assert handle.pid == 42
    handle.send_stdin("hello\n")
    handle.close_stdin()
    with pytest.raises(native.CommandExitException) as raised:
        handle.wait()
    assert (raised.value.stdout, raised.value.stderr, raised.value.exit_code, raised.value.error) == (
        "hello\n",
        "diagnostic",
        17,
        "exit status 17",
    )
    assert ("stdin", "42", "hello\n") in calls
    assert ("eof", "42") in calls


def test_kill_only_returns_false_for_a_structured_missing_process(monkeypatch):
    from arker._compat_process import GuestProcessError

    def fail(error):
        raise error

    commands = shim.Commands(vm=object())
    commands._processes = types.SimpleNamespace(get=lambda _: fail(GuestProcessError("missing", "process_not_found")))
    assert commands.kill(2147483647) is False
    unavailable = RuntimeError("connection refused")
    commands._processes.get = lambda _: fail(unavailable)
    with pytest.raises(RuntimeError) as raised:
        commands.kill(42)
    assert raised.value is unavailable


def test_background_handle_supports_simultaneous_waiters(monkeypatch):
    pytest.importorskip("e2b")
    entered = Event()
    release = Event()
    current = types.SimpleNamespace(pid="42", name="job", status="completed", stdout="done\n", stderr="", exit_code=0)

    class Processes:
        def get(self, identifier, **options):
            assert identifier == "42"
            entered.set()
            assert release.wait(timeout=1)
            return current

    from arker._compat_e2b import command_handle

    handle = command_handle(Processes(), current, timeout=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(handle.wait)
        assert entered.wait(timeout=1)
        second = pool.submit(handle.wait)
        release.set()
        results = [first.result(timeout=1), second.result(timeout=1)]

    assert [(result.stdout, result.exit_code) for result in results] == [("done\n", 0), ("done\n", 0)]
