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
