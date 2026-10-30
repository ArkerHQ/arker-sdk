from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import subprocess
import types
import uuid
from pathlib import Path

import pytest

from arker._compat_process import GuestProcesses, ProcessResult
from arker.computer import BackgroundRunResult, CompletedRunResult


class LocalVM:
    """Exercise the real wire script and supervisor without a cloud dependency."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.calls = []
        self.runs = {}

    def create_session(self):
        return types.SimpleNamespace(session_id=uuid.uuid4().hex)

    def get_run(self, run_id):
        process, stdout, stderr = self.runs[run_id]
        code = process.poll()
        return types.SimpleNamespace(
            stdout=stdout.read_text(),
            stderr=stderr.read_text(),
            exit_code=code,
            state="running" if code is None else "completed" if code == 0 else "failed",
        )

    def sync(self, path, content):
        (self.directory / Path(path).name).write_text(content)

    def run(self, command, **options):
        import os

        # The production path is content-addressed in /tmp; local tests keep
        # their process state and installed agent inside the pytest directory.
        command = command.replace("/tmp/arker-compat-process-", str(self.directory / "arker-compat-process-"))
        self.calls.append(options)
        if options.get("time_to_background") == 0:
            run_id = uuid.uuid4().hex
            stdout, stderr = (self.directory / f"{run_id}.{stream}" for stream in ("out", "err"))
            with stdout.open("w") as out, stderr.open("w") as err:
                process = subprocess.Popen(
                    command,
                    shell=True,
                    stdout=out,
                    stderr=err,
                    env={**os.environ, "ARKER_COMPAT_PROCESS_ROOT": str(self.directory / "processes")},
                )
            self.runs[run_id] = process, stdout, stderr
            return BackgroundRunResult(run_id=run_id, session_id=options.get("session_id"))
        response = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            timeout=10,
            env={**os.environ, "ARKER_COMPAT_PROCESS_ROOT": str(self.directory / "processes")},
        )
        return CompletedRunResult(
            stdout=response.stdout.decode(),
            stderr=response.stderr.decode(),
            stdout_bytes=response.stdout,
            stderr_bytes=response.stderr,
            exit_code=response.returncode,
        )


@pytest.fixture
def processes(tmp_path):
    manager = GuestProcesses(LocalVM(tmp_path))
    yield manager, tmp_path
    for process in manager.list():
        if process.status == "running":
            manager.kill(process.name)
            manager.wait(process.name, max_wait=5000, interval=10)
    for process, _, _ in manager._vm.runs.values():
        process.wait(timeout=5)


def test_background_reconnect_stdin_eof_and_logs(processes):
    manager, directory = processes
    started = manager.exec(
        {
            "command": "cat; printf error >&2",
            "name": "input",
            "stdin": True,
            "workingDir": str(directory),
            "waitForCompletion": False,
        }
    )
    assert started.status == "running"
    reconnected = GuestProcesses(LocalVM(directory))
    assert reconnected.get(started.pid).name == "input"
    reconnected.write_stdin("input", "hello λ\n")
    reconnected.write_stdin("input", b"second\n")
    reconnected.close_stdin("input")
    finished = reconnected.wait("input", max_wait=5000, interval=10)
    assert finished.status == "completed"
    assert finished.exit_code == 0
    assert finished.stdout == "hello λ\nsecond\n"
    assert finished.stderr == "error"
    assert reconnected.logs("input", "stdout") == finished.stdout
    assert reconnected.logs("input", "stderr") == "error"
    assert any(item.name == "input" for item in reconnected.list())
    with pytest.raises(RuntimeError, match="stdin is closed") as raised:
        reconnected.write_stdin("input", "late")
    assert getattr(raised.value, "code", None) == "process_stdin_closed"


def test_missing_process_has_a_structured_code_instead_of_message_matching(processes):
    manager, _ = processes
    with pytest.raises(RuntimeError) as raised:
        manager.get("never-created")
    assert getattr(raised.value, "code", None) == "process_not_found"


@pytest.mark.parametrize("missing_cwd", [False, True])
def test_argv_start_failure_can_be_observed_as_a_terminal_process(processes, missing_cwd):
    manager, directory = processes
    current = manager.exec(
        {
            "command": "missing startup",
            "argv": ["pwd"] if missing_cwd else ["arker-certainly-missing-executable"],
            "workingDir": str(directory / "missing") if missing_cwd else str(directory),
            "spawnFailureExitCode": 128,
        }
    )
    assert current.status == "failed"
    assert current.exit_code == 128
    assert current.stdout == ""
    assert "No such file or directory" in current.stderr
    assert manager.get(current.pid).exit_code == 128


def test_argv_execution_does_not_invoke_an_outer_shell(processes):
    manager, directory = processes
    current = manager.exec(
        {
            "command": "literal argv",
            "argv": ["sh", "-c", 'printf "%s" "$1"; kill -TERM $$', "sh", "literal ; $HOME"],
            "workingDir": str(directory),
        }
    )
    finished = manager.wait(current.pid, max_wait=5000, interval=10)
    assert finished.stdout == "literal ; $HOME"
    assert manager.get(current.pid, raw_exit_code=True).exit_code == -15


@pytest.mark.parametrize(
    "options",
    [
        {"argv": []},
        {"argv": "pwd"},
        {"argv": [None]},
        {"spawnFailureExitCode": True},
        {"spawnFailureExitCode": 0},
        {"spawnFailureExitCode": 256},
    ],
)
def test_invalid_argv_and_spawn_failure_options_do_not_start_a_process(processes, options):
    manager, directory = processes
    with pytest.raises(RuntimeError):
        manager.exec({"command": "touch should-not-exist", "workingDir": str(directory), **options})
    assert not (directory / "should-not-exist").exists()


@pytest.mark.parametrize("double_fork", [False, True])
def test_preserved_descendants_keep_the_supervisor_run_alive(processes, double_fork):
    manager, directory = processes
    release = directory / "release-descendant"
    marker = directory / "descendant-finished"
    argv = (
        [
            "python3",
            "-c",
            "import os,sys,time\n"
            "if os.fork(): os._exit(0)\n"
            "os.setsid()\n"
            "if os.fork(): os._exit(0)\n"
            "while not os.path.exists(sys.argv[1]): time.sleep(0.01)\n"
            "os.write(1,b'late'); open(sys.argv[2],'w').close()\n",
            str(release),
            str(marker),
        ]
        if double_fork
        else [
            "sh",
            "-c",
            '(while test ! -e "$1"; do sleep 0.01; done; printf late; touch "$2") & exit 0',
            "sh",
            str(release),
            str(marker),
        ]
    )
    try:
        current = manager.exec(
            {
                "command": "detached descendant",
                "argv": argv,
                "workingDir": str(directory),
                "preserveDescendants": True,
            }
        )
        manager.wait(current.pid, max_wait=5000, interval=10)
        supervisor = next(iter(manager._vm.runs.values()))[0]
        with pytest.raises(subprocess.TimeoutExpired):
            supervisor.wait(timeout=0.5)
    finally:
        release.touch()
    supervisor.wait(timeout=5)
    assert marker.exists()
    assert manager.get(current.pid).stdout == "late"


def test_process_output_supports_bounded_binary_reads_at_byte_offsets(processes):
    manager, directory = processes
    started = manager.exec(
        {
            "command": "python3 -c 'import sys; sys.stdout.buffer.write(bytes(range(256))*257)'",
            "workingDir": str(directory),
        }
    )
    manager.wait(started.name, max_wait=5000, interval=10)
    payload = bytearray()
    offset = 0
    while True:
        chunk = manager._call("readStream", identifier=started.pid, stream="stdout", offset=offset, limit=32771)
        data = base64.b64decode(chunk["data"])
        assert len(data) <= 32771
        assert chunk["nextOffset"] == offset + len(data)
        payload.extend(data)
        offset = chunk["nextOffset"]
        if chunk["eof"]:
            break
    assert payload == bytes(range(256)) * 257


def test_large_binary_stdin_does_not_exceed_guest_shell_argument_limits(processes):
    manager, directory = processes
    started = manager.exec({"command": "cat", "stdin": True, "workingDir": str(directory)})
    payload = bytes(range(256)) * 1024
    manager.write_stdin(started.pid, payload)
    manager.close_stdin(started.pid)
    manager.wait(started.pid, max_wait=5000, interval=10)
    output = bytearray()
    while True:
        chunk = manager._call("readStream", identifier=started.pid, stream="stdout", offset=len(output))
        output.extend(base64.b64decode(chunk["data"]))
        if chunk["eof"]:
            break
    assert output == payload


@pytest.mark.parametrize(
    "values",
    [
        {"stream": "../state"},
        {"offset": -1},
        {"offset": True},
        {"limit": 0},
        {"limit": 65537},
    ],
)
def test_process_output_rejects_invalid_streams_and_bounds(processes, values):
    manager, directory = processes
    started = manager.exec({"command": "true", "workingDir": str(directory)})
    request = {"identifier": started.pid, "stream": "stdout", "offset": 0, "limit": 113, **values}
    with pytest.raises(RuntimeError, match=r"Invalid process (stream|output bounds)"):
        manager._call("readStream", **request)


def test_background_process_holds_an_arker_run_until_it_finishes(processes):
    manager, directory = processes
    process = manager.exec({"command": "sleep 30", "workingDir": str(directory)})
    assert any(call.get("time_to_background") == 0 for call in manager._vm.calls)
    assert any(run.poll() is None for run, _, _ in manager._vm.runs.values())
    manager.kill(process.name)
    manager.wait(process.name, max_wait=5000, interval=10)


def test_process_control_uses_a_separate_reused_session_not_the_application_session(processes):
    manager, directory = processes
    process = manager.exec({"command": "cat", "stdin": True, "workingDir": str(directory)})
    manager.get(process.name)
    manager.get(process.name)
    manager.close_stdin(process.name)
    manager.wait(process.name, max_wait=5000, interval=10)
    supervisors = {call["session_id"] for call in manager._vm.calls if call.get("time_to_background") == 0}
    controls = [call.get("session_id") for call in manager._vm.calls if call.get("time_to_background") != 0]
    assert controls and all(controls)
    assert len(set(controls)) == 1
    assert set(controls).isdisjoint(supervisors)


@pytest.mark.parametrize("command, raw_code", [("exit 137", 137), ("exec sh -c 'kill -KILL $$'", -9)])
def test_internal_raw_exit_preserves_signal_vs_explicit_exit_without_changing_blaxel(processes, command, raw_code):
    manager, directory = processes
    current = manager.exec({"command": command, "workingDir": str(directory), "waitForCompletion": True})
    assert current.exit_code == 137
    assert manager.get(current.name, raw_exit_code=True).exit_code == raw_code
    assert not any(key.startswith("_") for key in current.to_dict())


def test_foreground_callbacks_and_nonzero_exit(processes):
    manager, directory = processes
    chunks = []
    finished = manager.exec(
        {
            "command": "printf output; printf problem >&2; exit 7",
            "name": "finite",
            "workingDir": str(directory),
            "waitForCompletion": True,
            "on_stdout": chunks.append,
        }
    )
    assert type(finished) is ProcessResult
    assert finished.exit_code == 7
    assert finished.status == "failed"
    assert "".join(chunks) == "output"
    assert finished.to_dict()["workingDir"] == str(directory)


def test_wait_timeout_does_not_kill_and_name_does_not_reexecute(processes):
    manager, directory = processes
    manager.exec({"command": "sleep 30", "name": "slow", "workingDir": str(directory)})
    with pytest.raises(TimeoutError):
        manager.wait("slow", max_wait=1, interval=10)
    assert manager.get("slow").status == "running"
    with pytest.raises(RuntimeError):
        manager.exec({"command": "printf duplicate", "name": "slow", "workingDir": str(directory)})
    manager.kill("slow")
    assert manager.wait("slow", max_wait=5000, interval=10).status == "killed"


def test_execution_timeout_kills_process(processes):
    manager, directory = processes
    finished = manager.exec(
        {"command": "sleep 30", "timeout": 100, "workingDir": str(directory), "waitForCompletion": True}
    )
    assert finished.status == "killed"
    assert finished.exit_code != 0


@pytest.mark.parametrize("command, expected", [("exit 137", 137), ("exec sh -c 'kill -KILL $$'", -1)])
def test_blaxel_signal_exit_keeps_explicit_positive_exit_codes(processes, command, expected):
    manager, directory = processes
    current = manager.exec(
        {"command": command, "workingDir": str(directory), "waitForCompletion": True, "signalExitCode": -1}
    )
    assert current.exit_code == expected
    assert manager.get(current.name).exit_code == expected


def test_replayed_transport_request_recovers_same_process(processes):
    manager, directory = processes
    request = {"command": "sleep 30", "name": "replay", "workingDir": str(directory), "_requestId": "nonce"}
    first = manager._call("exec", **request)
    second = manager._call("exec", **request)
    assert first["pid"] == second["pid"]
    with pytest.raises(RuntimeError, match="already exists"):
        manager._call("exec", **{**request, "_requestId": "different-intent"})


def test_concurrent_replayed_request_starts_exactly_one_process(processes):
    manager, directory = processes
    request = {
        "command": "sleep 30",
        "name": "concurrent-replay",
        "workingDir": str(directory),
        "_requestId": "same-transport-request",
    }
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: manager._call("exec", **request), range(2)))
    assert results[0]["pid"] == results[1]["pid"]
    assert len([item for item in manager.list() if item.name == "concurrent-replay"]) == 1


def test_replay_recovers_process_state_created_by_older_agent(processes):
    manager, directory = processes
    request = {
        "command": "sleep 30",
        "name": "legacy-replay",
        "workingDir": str(directory),
        "_requestId": "legacy-transport-request",
    }
    first = manager._call("exec", **request)
    process_directory = directory / "processes" / hashlib.sha256(b"legacy-replay").hexdigest()
    (process_directory / "request").unlink()
    assert manager._call("exec", **request)["pid"] == first["pid"]


def test_concurrent_processes_isolate_working_directory_and_environment(processes):
    manager, directory = processes
    first, second = directory / "first", directory / "second"
    first.mkdir()
    second.mkdir()

    def launch(name, cwd, value):
        return manager.exec(
            {
                "command": "printf '%s|' \"$PROCESS_VALUE\"; pwd; sleep 0.1",
                "name": name,
                "workingDir": str(cwd),
                "env": {"PROCESS_VALUE": value},
                "waitForCompletion": True,
            }
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(launch, "isolated-first", first, "one"),
            executor.submit(launch, "isolated-second", second, "two"),
        ]
        outputs = [future.result().stdout for future in futures]
    assert outputs == [f"one|{first}\n", f"two|{second}\n"]


@pytest.mark.parametrize("prior_error", [None, "original worker failure"])
def test_modal_control_cleanup_failure_publishes_completion_and_diagnostic(tmp_path, monkeypatch, prior_error):
    import json
    import os
    import runpy

    import arker

    guest = runpy.run_path(str(Path(arker.__file__).with_name("_guest_process.py")))
    control = "exec-" + "a" * 32 + ".json"
    unlink = Path.unlink

    def denied(path, *args, **kwargs):
        if path == Path("/root/.arker-modal") / control:
            raise PermissionError("synthetic control cleanup denied")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    (tmp_path / "input").touch()
    if prior_error:
        (tmp_path / "error").write_text(json.dumps({"error": prior_error}))
    read_fd, write_fd = os.pipe()
    try:
        guest["worker"](
            {
                "_directory": str(tmp_path),
                "command": "exit 17",
                "workingDir": str(tmp_path),
                "name": "cleanup-failure",
                "_modalControl": control,
            },
            write_fd,
        )
    finally:
        os.close(read_fd)
    assert (tmp_path / "worker-complete").exists()
    assert json.loads((tmp_path / "state").read_text())["exitCode"] == 17
    assert "synthetic control cleanup denied" in (tmp_path / "stderr").read_text()
    diagnostic = json.loads((tmp_path / "error").read_text())["error"]
    assert "synthetic control cleanup denied" in diagnostic
    if prior_error:
        assert prior_error in diagnostic


@pytest.mark.parametrize("preserve_descendants", [False, True])
def test_supervisor_reports_cleanup_error_before_completion(tmp_path, preserve_descendants):
    import json
    import os
    import sys

    import arker

    control = "exec-" + "b" * 32 + ".json"
    private = tmp_path / "private"
    (private / control).mkdir(parents=True)
    script = tmp_path / "guest.py"
    script.write_text(
        Path(arker.__file__).with_name("_guest_process.py").read_text().replace("/root/.arker-modal", str(private))
    )
    payload = {
        "action": "exec",
        "name": "cleanup-failure",
        "command": "exit 17",
        "workingDir": str(tmp_path),
        "preserveDescendants": preserve_descendants,
        "_modalControl": control,
    }
    result = subprocess.run(
        [sys.executable, str(script), "supervise", base64.b64encode(json.dumps(payload).encode()).decode()],
        env={**os.environ, "ARKER_COMPAT_PROCESS_ROOT": str(tmp_path / "processes")},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert "Process cleanup failed" in json.loads(result.stdout.splitlines()[-1])["error"]
    directory = tmp_path / "processes" / hashlib.sha256(payload["name"].encode()).hexdigest()
    assert (directory / "worker-complete").exists()
    assert json.loads((directory / "state").read_text())["exitCode"] == 17


def test_process_identity_rejects_actual_unreaped_zombie():
    import os
    import runpy
    import select
    import signal
    import sys

    import arker

    identity = runpy.run_path(str(Path(arker.__file__).with_name("_guest_process.py")))["identity"]
    child = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"])
    descriptor = os.pidfd_open(child.pid)
    try:
        live_identity = identity(child.pid)
        assert live_identity is not None
        assert live_identity == Path(f"/proc/{child.pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
        os.kill(child.pid, signal.SIGKILL)
        poller = select.poll()
        poller.register(descriptor, select.POLLIN)
        assert poller.poll(5000), "owned process did not exit after SIGKILL"
        result = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        assert result.si_status == signal.SIGKILL
        assert Path(f"/proc/{child.pid}/stat").read_text().rsplit(") ", 1)[1].split()[0] == "Z"
        assert identity(child.pid) is None
    finally:
        child.kill()
        child.wait(timeout=5)
        os.close(descriptor)


@pytest.mark.parametrize("preserve_descendants", [False, True])
def test_supervisor_reports_actual_worker_sigkill(tmp_path, preserve_descendants):
    import contextlib
    import json
    import os
    import select
    import signal
    import sys

    import arker

    payload = {
        "action": "exec",
        "name": "killed-worker",
        "command": "pause",
        "argv": [sys.executable, "-c", "import signal; signal.pause()"],
        "workingDir": str(tmp_path),
        "preserveDescendants": preserve_descendants,
    }
    supervisor = subprocess.Popen(
        [
            sys.executable,
            str(Path(arker.__file__).with_name("_guest_process.py")),
            "supervise",
            base64.b64encode(json.dumps(payload).encode()).decode(),
        ],
        env={**os.environ, "ARKER_COMPAT_PROCESS_ROOT": str(tmp_path / "processes")},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    directory = tmp_path / "processes" / hashlib.sha256(payload["name"].encode()).hexdigest()
    state = None
    try:
        assert select.select([supervisor.stdout], [], [], 5)[0], "supervisor did not publish readiness"
        ready = json.loads(supervisor.stdout.readline())
        assert ready["result"]["status"] == "running"
        state = json.loads((directory / "state").read_text())
        assert not (directory / "worker-complete").exists()
        os.kill(state["_workerPid"], signal.SIGKILL)
        output, _ = supervisor.communicate(timeout=5)
        assert supervisor.returncode == 1
        assert json.loads(output.splitlines()[-1])["error"] == "Process supervisor exited before completion"
        assert not (directory / "worker-complete").exists()
    finally:
        if state is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(int(state["pid"]), signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                os.kill(state["_workerPid"], signal.SIGKILL)
        if supervisor.poll() is None:
            supervisor.kill()
        supervisor.communicate(timeout=5)


def test_real_terminal_controlling_session_resize_and_eof(processes):
    import json
    import sys
    import time

    manager, directory = processes
    program = """import os,sys,termios,json,signal
print(json.dumps({"tty": [os.isatty(i) for i in range(3)],
 "control": os.tcgetpgrp(0)==os.getpgrp(), "size": list(os.get_terminal_size())}),flush=True)
signal.signal(signal.SIGWINCH, lambda *_: print("RESIZED:"+str(tuple(os.get_terminal_size())),flush=True))
print("STDERR",file=sys.stderr,flush=True)
data=sys.stdin.read()
print("INPUT:"+repr(data),flush=True)
sys.exit(7)
"""
    started = manager.exec(
        {
            "command": "terminal",
            "argv": [sys.executable, "-c", program],
            "workingDir": str(directory),
            "stdin": True,
            "tty": {"rows": 24, "cols": 80},
        }
    )
    try:
        deadline = time.monotonic() + 5
        while "STDERR" not in (current := manager.get(started.name)).stdout:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        first = json.loads(current.stdout.splitlines()[0])
        assert first == {"tty": [True, True, True], "control": True, "size": [80, 24]}
        assert current.stderr == ""
        manager.resize(started.name, 37, 91)
        assert manager.get(started.name).tty == {"rows": 37, "cols": 91}
        while "RESIZED:(91, 37)" not in manager.get(started.name).stdout:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        manager.write_stdin(started.name, b"hello\n")
        manager.write_stdin(started.name, b"\x04")
        manager.close_stdin(started.name)
        result = manager.wait(started.name, max_wait=5000, interval=10)
        assert result.exitCode == 7
        assert "INPUT:'hello\\n'" in result.stdout
        assert result.stderr == ""
    finally:
        if manager.get(started.name).status == "running":
            manager.kill(started.name)


@pytest.mark.parametrize("size", [{"rows": True, "cols": 80}, {"rows": -1, "cols": 80}, {"rows": 24}, None])
def test_terminal_rejects_invalid_size_before_launch(processes, size):
    manager, directory = processes
    with pytest.raises(Exception, match="Terminal"):
        manager.exec({"command": "touch unexpected", "workingDir": str(directory), "tty": size})
    assert not (directory / "unexpected").exists()


def test_zero_size_terminal_writer_close_does_not_inject_eof(processes):
    import sys

    manager, directory = processes
    program = """import os,signal,sys
assert tuple(os.get_terminal_size()) == (0, 0)
def deadline(*_):
    print("reader-still-open",flush=True)
    sys.exit(13)
signal.signal(signal.SIGALRM, deadline)
signal.alarm(2)
sys.stdin.read()
print("unexpected-eof",flush=True)
"""
    started = manager.exec(
        {
            "command": "terminal-input",
            "argv": [sys.executable, "-c", program],
            "workingDir": str(directory),
            "stdin": True,
            "tty": {"rows": 0, "cols": 0},
        }
    )
    manager.close_stdin(started.name)
    result = manager.wait(started.name, max_wait=5000, interval=10)
    assert result.exitCode == 13
    assert "reader-still-open" in result.stdout
    assert "unexpected-eof" not in result.stdout


@pytest.mark.parametrize("exit_before_resize", [False, True])
def test_resize_waits_for_its_ioctl_ack_or_reports_exit(processes, exit_before_resize):
    import json
    import os
    import signal
    import time

    manager, directory = processes
    started = manager.exec(
        {
            "name": "resize-ack",
            "command": "cat",
            "stdin": True,
            "workingDir": str(directory),
            "tty": {"rows": 24, "cols": 80},
        }
    )
    state_dir = directory / "processes" / hashlib.sha256(started.name.encode()).hexdigest()
    state = json.loads((state_dir / "state").read_text())
    worker_pid = state["_workerPid"]
    os.kill(worker_pid, signal.SIGSTOP)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        (state_dir / "resize-ack").write_text(json.dumps({"id": "stale-ack"}))
        pending = pool.submit(manager.resize, started.name, 51, 103)
        deadline = time.monotonic() + 5
        while not (state_dir / "resize").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        with pytest.raises(concurrent.futures.TimeoutError):
            pending.result(timeout=0.1)
        assert json.loads((state_dir / "state").read_text())["tty"] == {"rows": 24, "cols": 80}
        if exit_before_resize:
            os.killpg(int(started.pid), signal.SIGKILL)
            # Wait for the actual child termination while its supervisor is stopped.
            while Path(f"/proc/{started.pid}/stat").read_text().rsplit(") ", 1)[1].split()[0] != "Z":
                assert time.monotonic() < deadline
                time.sleep(0.01)
        os.kill(worker_pid, signal.SIGCONT)
        if exit_before_resize:
            with pytest.raises(Exception, match="terminal has exited"):
                pending.result(timeout=5)
        else:
            pending.result(timeout=5)
            assert manager.get(started.name).tty == {"rows": 51, "cols": 103}
            request = json.loads((state_dir / "resize").read_text())
            assert json.loads((state_dir / "resize-ack").read_text())["id"] == request["id"]
    finally:
        with __import__("contextlib").suppress(ProcessLookupError):
            os.kill(worker_pid, signal.SIGCONT)
        pool.shutdown(wait=True)


@pytest.mark.parametrize("output_lifetime", ["leader", "writers"])
def test_terminal_descendant_output_lifetime_matches_provider(processes, output_lifetime):
    import json
    import sys

    manager, directory = processes
    report = directory / "descendant-report"
    program = """import os,sys,time,signal,json
ready,notify=os.pipe()
if os.fork():
 os.close(notify);os.read(ready,1);print("leader",flush=True);os._exit(7)
os.close(ready)
os.setsid()
written=0
def done(error):
 with open(sys.argv[1],"w") as f:json.dump({"written":written,"error":error},f)
 os._exit(0)
signal.signal(signal.SIGALRM,lambda *_:done("timeout"))
os.write(notify,b"1");os.close(notify)
signal.alarm(2)
time.sleep(.1)
try:
 while written<2*1024*1024:written+=os.write(1,b"X"*min(65536,2*1024*1024-written))
 done(None)
except OSError as e:done(e.errno)
"""
    current = manager.exec(
        {
            "command": "detached terminal writer",
            "argv": [sys.executable, "-c", program, str(report)],
            "workingDir": str(directory),
            "preserveDescendants": True,
            "tty": {"rows": 24, "cols": 80},
            "ttyOutput": output_lifetime,
        }
    )
    result = manager.wait(current.name, max_wait=5000, interval=10)
    assert result.exit_code == 7
    supervisor = next(iter(manager._vm.runs.values()))[0]
    supervisor.wait(timeout=5)
    child = json.loads(report.read_text())
    if output_lifetime == "writers":
        assert result.stdout == "leader\r\n" + "X" * (2 * 1024 * 1024)
        assert child == {"written": 2 * 1024 * 1024, "error": None}
    else:
        assert result.stdout == "leader\r\n"
        assert 0 <= child["written"] < 2 * 1024 * 1024
        assert child["error"] == "timeout"


def test_kill_after_terminal_leader_exit_reaps_detached_descendant(processes):
    import json
    import os
    import sys
    import time

    manager, directory = processes
    marker = directory / "detached-pid"
    program = """import os,sys,time
ready,notify=os.pipe()
if os.fork():
 os.close(notify);os.read(ready,1);os._exit(0)
os.close(ready)
os.setsid()
with open(sys.argv[1],"w") as f:f.write(str(os.getpid()))
os.write(notify,b"1");os.close(notify)
while True:time.sleep(1)
"""
    current = manager.exec(
        {
            "command": "detached terminal",
            "argv": [sys.executable, "-c", program, str(marker)],
            "workingDir": str(directory),
            "preserveDescendants": True,
            "tty": {"rows": 24, "cols": 80},
        }
    )
    manager.wait(current.name, max_wait=5000, interval=10)
    deadline = time.monotonic() + 5
    while not marker.exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    pid = int(marker.read_text())
    state_dir = directory / "processes" / hashlib.sha256(current.name.encode()).hexdigest()
    state = json.loads((state_dir / "state").read_text())
    try:
        os.kill(pid, 0)
        manager.kill(current.name)
        supervisor = next(iter(manager._vm.runs.values()))[0]
        supervisor.wait(timeout=5)
        assert not Path(f"/proc/{pid}").exists()
        assert (state_dir / "worker-complete").exists()
    finally:
        # Keep a failing implementation from leaving this test's adopted child alive.
        if Path(f"/proc/{pid}").exists():
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
            if int(fields[1]) == state["_workerPid"]:
                os.kill(pid, 9)


def test_resize_after_leader_exit_updates_live_descendant_terminal(processes):
    import json
    import sys
    import time

    manager, directory = processes
    release = directory / "inspect-terminal"
    report = directory / "terminal-size"
    program = """import os,sys,time,json
ready,notify=os.pipe()
if os.fork():
 os.close(notify);os.read(ready,1);os._exit(0)
os.close(ready);os.setsid();os.write(notify,b"1");os.close(notify)
while not os.path.exists(sys.argv[1]):time.sleep(.01)
with open(sys.argv[2],"w") as f:json.dump(list(os.get_terminal_size(1)),f)
os._exit(0)
"""
    started = manager.exec(
        {
            "command": "resize detached terminal",
            "argv": [sys.executable, "-c", program, str(release), str(report)],
            "workingDir": str(directory),
            "preserveDescendants": True,
            "tty": {"rows": 24, "cols": 80},
            "ttyOutput": "writers",
        }
    )
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        deadline = time.monotonic() + 5
        while Path(f"/proc/{started.pid}").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert manager.get(started.name).status == "running"
        pending = pool.submit(manager.resize, started.name, 41, 99)
        pending.result(timeout=2)
        release.touch()
        manager.wait(started.name, max_wait=5000, interval=10)
        assert json.loads(report.read_text()) == [99, 41]
    finally:
        release.touch()
        pool.shutdown(wait=True)


def test_resize_reports_worker_error_even_while_worker_identity_is_alive(processes):
    import json
    import os
    import signal
    import time

    manager, directory = processes
    started = manager.exec(
        {"command": "cat", "stdin": True, "workingDir": str(directory), "tty": {"rows": 24, "cols": 80}}
    )
    state_dir = directory / "processes" / hashlib.sha256(started.name.encode()).hexdigest()
    state = json.loads((state_dir / "state").read_text())
    worker_pid = state["_workerPid"]
    os.kill(worker_pid, signal.SIGSTOP)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        pending = pool.submit(manager.resize, started.name, 41, 99)
        deadline = time.monotonic() + 5
        while not (state_dir / "resize").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        temporary = state_dir / "injected-error"
        temporary.write_text(json.dumps({"error": "terminal worker failed"}))
        temporary.replace(state_dir / "error")
        with pytest.raises(Exception, match="terminal worker failed"):
            pending.result(timeout=2)
        assert json.loads((state_dir / "state").read_text())["status"] == "running"
        os.kill(worker_pid, 0)
    finally:
        os.kill(worker_pid, signal.SIGCONT)
        pool.shutdown(wait=True)


@pytest.mark.parametrize("missing", ["pidfd_open", "pidfd_send_signal", "kernel"])
def test_descendant_support_is_checked_before_process_launch(tmp_path, monkeypatch, missing):
    import importlib.util
    import os
    import signal

    source = Path(__file__).parents[2] / "src/arker/_guest_process.py"
    spec = importlib.util.spec_from_file_location("guest_pidfd_preflight", source)
    guest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guest)
    monkeypatch.setattr(guest, "ROOT", tmp_path)
    if missing == "kernel":

        def unavailable(*args):
            raise OSError("pidfd unavailable")

        monkeypatch.setattr(os, "pidfd_open", unavailable)
    else:
        monkeypatch.delattr(os if missing == "pidfd_open" else signal, missing)
    with pytest.raises(RuntimeError, match="Descendant supervision requires"):
        guest.dispatch({"action": "exec", "preserveDescendants": True, "command": "touch forbidden"})
    assert list(tmp_path.iterdir()) == []
