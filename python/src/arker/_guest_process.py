"""Guest process protocol shared by the Python and TypeScript shims.

Only the standard library is used. State lives in the VM, so reconnecting SDK
clients can observe a process without executing its command again.
"""

from __future__ import annotations

import base64
import codecs
import contextlib
import datetime
import errno
import fcntl
import hashlib
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import uuid
from pathlib import Path

ROOT = Path(os.environ.get("ARKER_COMPAT_PROCESS_ROOT", "/tmp/arker-compat-processes"))
TERMINAL = {"completed", "failed", "stopped", "killed"}


class ProcessNotFound(ValueError):
    pass


class ProcessStdinClosed(ValueError):
    pass


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def identity(pid):
    try:
        # The parenthesized process name may contain spaces and parentheses.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return None if fields[0] in {"Z", "X"} else fields[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def snapshot(directory, *, raw_exit_code=False):
    state = json.loads((directory / "state").read_text())
    for stream in ("stdout", "stderr"):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        state[stream] = decoder.decode((directory / stream).read_bytes(), final=state["status"] in TERMINAL)
    state["logs"] = state["stdout"] + state["stderr"]
    if raw_exit_code and state["status"] in TERMINAL:
        if "_returnCode" not in state:
            raise ValueError("Raw exit status is unavailable for this legacy process")
        state["exitCode"] = state["_returnCode"]
    return {key: value for key, value in state.items() if not key.startswith("_")}


def locate(identifier):
    direct = ROOT / hashlib.sha256(str(identifier).encode()).hexdigest()
    if (direct / "state").exists():
        return direct
    for directory in ROOT.glob("*"):
        try:
            state = json.loads((directory / "state").read_text())
            if str(state["pid"]) == str(identifier):
                return directory
        except (OSError, ValueError, KeyError):
            continue
    raise ProcessNotFound(f"Process {identifier!r} not found")


def terminal_size(value):
    if not isinstance(value, dict) or set(value) != {"rows", "cols"}:
        raise ValueError("Terminal size requires rows and cols")
    if any(type(value[key]) is not int or not 0 <= value[key] <= 65535 for key in ("rows", "cols")):
        raise ValueError("Terminal dimensions must be integers between 0 and 65535")
    return struct.pack("HHHH", value["rows"], value["cols"], 0, 0)


def claim_terminal():
    # The supervisor is single-threaded; this runs in its forked child before exec.
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def drain_terminal(fd, output):
    # Bound each pass so a continuous writer cannot starve input or resize.
    for _ in range(16):
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return False
        except OSError as error:
            if error.errno == errno.EIO:
                return True
            raise
        if not data:
            return True
        output.write(data)
    return False


def apply_terminal_resize(directory, master, state, resize_id):
    path = directory / "resize"
    if not path.exists():
        return resize_id
    update = json.loads(path.read_text())
    if update["id"] == resize_id:
        return resize_id
    acknowledgement = {"id": update["id"]}
    try:
        fcntl.ioctl(master, termios.TIOCSWINSZ, terminal_size(update["tty"]))
        state["tty"] = update["tty"]
        write_json(directory / "state", state)
    except OSError as error:
        acknowledgement["error"] = str(error)
    write_json(directory / "resize-ack", acknowledgement)
    return update["id"]


def terminate_descendants(directory):
    termination = directory / "termination"
    if not termination.exists():
        return
    action = json.loads(termination.read_text())["status"]
    selected = signal.SIGKILL if action == "killed" else signal.SIGTERM
    parent = os.getpid()
    children = Path(f"/proc/{parent}/task/{parent}/children").read_text().split()
    for child_pid in children:
        handle = None
        try:
            handle = os.pidfd_open(int(child_pid))
            fields = Path(f"/proc/{child_pid}/stat").read_text().rsplit(") ", 1)[1].split()
            # A vanished child PID may already have been reused. Only signal an
            # owned direct child through its pinned handle; orphans are adopted
            # by this subreaper and processed on the next pass.
            if int(fields[1]) == parent:
                signal.pidfd_send_signal(handle, selected)
        except (FileNotFoundError, ProcessLookupError):
            pass
        finally:
            if handle is not None:
                os.close(handle)


def reap_descendants(directory):
    while True:
        terminate_descendants(directory)
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        except InterruptedError:
            continue
        if pid == 0:
            time.sleep(0.02)


def worker(request, ready_fd):
    directory = Path(request["_directory"])
    command = request["command"]
    cwd = request.get("workingDir") or "/blaxel"
    if cwd == "/blaxel":
        Path(cwd).mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, **(request.get("env") or {})}
    state = {
        "name": request["name"],
        "command": command,
        "workingDir": cwd,
        "pid": request["name"],
        "status": "running",
        "exitCode": 0,
        "startedAt": timestamp(),
        "completedAt": "",
        "stdin": bool(request.get("stdin")),
        "_identity": None,
        "_request_id": request.get("_requestId"),
        "_resizeAck": True,
        "_preserveDescendants": request.get("preserveDescendants") is True,
        "_workerPid": os.getpid(),
        "_workerIdentity": identity(os.getpid()),
    }
    with (
        (directory / "stdout").open("ab", buffering=0) as stdout,
        (directory / "stderr").open("ab", buffering=0) as stderr,
    ):
        child = None
        master = slave = None
        try:
            if request.get("preserveDescendants"):
                import ctypes

                # Adopt orphaned descendants even when they double-fork or setsid.
                if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
                    raise OSError(ctypes.get_errno(), "Cannot supervise detached process descendants")
            if "tty" in request:
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, terminal_size(request["tty"]))
                os.set_blocking(master, False)
                state["tty"] = request["tty"]
            child = subprocess.Popen(
                request.get("argv", command),
                shell="argv" not in request,
                executable=None if "argv" in request else "/bin/sh",
                cwd=cwd,
                env=environment,
                stdout=slave if slave is not None else stdout,
                stderr=slave if slave is not None else stderr,
                stdin=slave if slave is not None else (subprocess.PIPE if request.get("stdin") else subprocess.DEVNULL),
                start_new_session=slave is None,
                preexec_fn=claim_terminal if slave is not None else None,
            )
            if slave is not None:
                os.close(slave)
                slave = None
            state.update(pid=str(child.pid), _identity=identity(child.pid))
            write_json(directory / "state", state)
            os.write(ready_fd, b"1")
            os.close(ready_fd)
            ready_fd = -1
            started = time.monotonic()
            pending = b""
            terminal_eof = False
            terminal_closed = False
            resize_id = None
            if child.stdin:
                os.set_blocking(child.stdin.fileno(), False)
            with (directory / "input").open("rb") as incoming:
                while child.poll() is None:
                    if (request.get("timeout") or 0) > 0 and (time.monotonic() - started) * 1000 >= request["timeout"]:
                        write_json(directory / "termination", {"status": "killed"})
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(child.pid, signal.SIGKILL)
                    if master is not None:
                        terminal_closed = drain_terminal(master, stdout)
                        resize_id = apply_terminal_resize(directory, master, state, resize_id)
                        if request.get("stdin") and not terminal_eof:
                            pending += incoming.read(65536 - min(len(pending), 65536))
                            if not pending and (directory / "eof").exists():
                                # A terminal has no input half-close. Closing the SDK
                                # writer neither sends VEOF nor closes the output master.
                                terminal_eof = True
                        if pending:
                            with contextlib.suppress(BlockingIOError):
                                pending = pending[os.write(master, pending) :]
                    if child.stdin and not child.stdin.closed:
                        pending += incoming.read(65536 - min(len(pending), 65536))
                        if pending:
                            try:
                                count = os.write(child.stdin.fileno(), pending)
                                pending = pending[count:]
                            except BlockingIOError:
                                pass
                            except BrokenPipeError:
                                child.stdin.close()
                                (directory / "eof").touch()
                        elif (directory / "eof").exists():
                            child.stdin.close()
                    if master is not None and not terminal_closed:
                        select.select([master], [], [], 0.02)
                    else:
                        time.sleep(0.02)
            code = child.wait()
            if master is not None:
                if request.get("ttyOutput") == "writers":
                    while not drain_terminal(master, stdout):
                        resize_id = apply_terminal_resize(directory, master, state, resize_id)
                        terminate_descendants(directory)
                        select.select([master], [], [], 0.02)
                else:
                    drain_terminal(master, stdout)
            state["exitCode"] = code if code >= 0 else request.get("signalExitCode", 128 - code)
            state["_returnCode"] = code
            termination = directory / "termination"
            state["status"] = (
                json.loads(termination.read_text())["status"]
                if termination.exists()
                else ("completed" if code == 0 else "failed")
            )
            state["completedAt"] = timestamp()
            write_json(directory / "state", state)
        except Exception as error:
            if child is None and isinstance(error, OSError) and "spawnFailureExitCode" in request:
                code = request["spawnFailureExitCode"]
                stderr.write((str(error) + "\n").encode())
                state.update(status="failed", exitCode=code, _returnCode=code, completedAt=timestamp())
                write_json(directory / "state", state)
                os.write(ready_fd, b"1")
                return
            if child is not None and child.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            write_json(directory / "error", {"error": str(error)})
        finally:
            try:
                if ready_fd >= 0:
                    os.close(ready_fd)
                if request.get("preserveDescendants"):
                    reap_descendants(directory)
                control = request.get("_modalControl", "")
                if isinstance(control, str) and re.fullmatch(r"exec-[0-9a-f]{32}\.json", control):
                    with contextlib.suppress(FileNotFoundError):
                        (Path("/root/.arker-modal") / control).unlink()
            except Exception as error:
                diagnostic = f"Process cleanup failed: {type(error).__name__}: {error}"
                stderr.write((diagnostic + "\n").encode())
                error_path = directory / "error"
                if error_path.exists():
                    diagnostic = json.loads(error_path.read_text())["error"] + "; " + diagnostic
                write_json(error_path, {"error": diagnostic})
            finally:
                for fd in (master, slave):
                    if fd is not None:
                        os.close(fd)
                (directory / "worker-complete").touch()


def dispatch(request):
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    action = request["action"]
    if action == "exec":
        if "tty" in request:
            terminal_size(request["tty"])
        if "ttyOutput" in request and (
            request["ttyOutput"] not in {"leader", "writers"}
            or "tty" not in request
            or (request["ttyOutput"] == "writers" and request.get("preserveDescendants") is not True)
        ):
            raise ValueError("Invalid terminal output lifetime")
        if "signalExitCode" in request and (
            type(request["signalExitCode"]) is not int or request["signalExitCode"] != -1
        ):
            raise ValueError("Invalid signal exit code")
        if "preserveDescendants" in request and type(request["preserveDescendants"]) is not bool:
            raise ValueError("Invalid descendant supervision option")
        if "argv" in request and (
            not isinstance(request["argv"], list)
            or not request["argv"]
            or not all(isinstance(part, str) for part in request["argv"])
        ):
            raise ValueError("Process argv must be a nonempty list of strings")
        if "spawnFailureExitCode" in request and (
            type(request["spawnFailureExitCode"]) is not int or not 1 <= request["spawnFailureExitCode"] <= 255
        ):
            raise ValueError("Invalid process spawn-failure exit code")
        if request.get("name") is None:
            request["name"] = uuid.uuid4().hex
        if not isinstance(request["name"], str) or not request["name"]:
            raise ValueError("Process name must be a nonempty string")
        directory = ROOT / hashlib.sha256(request["name"].encode()).hexdigest()
        with (ROOT / ".exec-lock").open("a+") as root_lock:
            fcntl.flock(root_lock, fcntl.LOCK_EX)
            created = False
            try:
                directory.mkdir(mode=0o700)
                created = True
                request["_directory"] = str(directory)
                for name in ("stdout", "stderr", "input", "lock"):
                    (directory / name).touch(mode=0o600)
                write_json(directory / "request", request)
            except FileExistsError:
                pass
            process_lock = (directory / "lock").open("r+")
            fcntl.flock(process_lock, fcntl.LOCK_EX)
        with process_lock:
            if not created:
                try:
                    original = json.loads((directory / "request").read_text())
                except (FileNotFoundError, ValueError):
                    try:
                        original = json.loads((directory / "state").read_text())
                    except (FileNotFoundError, ValueError):
                        original = {}
                original_request_id = original.get("_requestId", original.get("_request_id"))
                if request.get("_requestId") and original_request_id == request["_requestId"]:
                    if (directory / "error").exists():
                        raise RuntimeError(json.loads((directory / "error").read_text())["error"])
                    return snapshot(directory)
                raise ValueError(f"Process {request['name']!r} already exists") from None
            read_fd, write_fd = os.pipe()
            try:
                subprocess.Popen(
                    [
                        sys.executable,
                        __file__,
                        "worker",
                        base64.b64encode(json.dumps(request).encode()).decode(),
                        str(write_fd),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    pass_fds=(write_fd,),
                )
            finally:
                os.close(write_fd)
            try:
                os.read(read_fd, 1)
            finally:
                os.close(read_fd)
            if (directory / "error").exists():
                raise RuntimeError(json.loads((directory / "error").read_text())["error"])
            return snapshot(directory)
    if action == "list":
        return [snapshot(path) for path in ROOT.glob("*") if (path / "state").exists()]
    directory = locate(request["identifier"])
    if action == "readStream":
        stream = request.get("stream")
        if stream not in {"stdout", "stderr"}:
            raise ValueError("Invalid process stream")
        offset, limit = request.get("offset", 0), request.get("limit", 65536)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 65536:
            raise ValueError("Invalid process output bounds")
        state = json.loads((directory / "state").read_text())
        with (directory / stream).open("rb") as output:
            output.seek(offset)
            data = output.read(limit)
            end = output.tell()
            size = os.fstat(output.fileno()).st_size
        return {
            "data": base64.b64encode(data).decode(),
            "nextOffset": end,
            "eof": state["status"] in TERMINAL and end >= size,
        }
    if action == "resize":
        terminal_size(request.get("tty"))
        with (directory / "resize-lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads((directory / "state").read_text())
            if "tty" not in state:
                raise ValueError("Process has no terminal")
            if not state.get("_resizeAck"):
                raise ValueError("Terminal resize acknowledgement is unavailable for this process")
            if state["status"] in TERMINAL:
                raise ValueError("Process terminal has exited")
            request_id = uuid.uuid4().hex
            write_json(directory / "resize", {"id": request_id, "tty": request["tty"]})
            while True:
                ack_path = directory / "resize-ack"
                if ack_path.exists():
                    acknowledgement = json.loads(ack_path.read_text())
                    if acknowledgement["id"] == request_id:
                        if "error" in acknowledgement:
                            raise RuntimeError(acknowledgement["error"])
                        return {"message": "Terminal resized"}
                error_path = directory / "error"
                if error_path.exists():
                    raise RuntimeError(json.loads(error_path.read_text())["error"])
                state = json.loads((directory / "state").read_text())
                if state["status"] in TERMINAL:
                    raise ValueError("Process terminal has exited")
                if identity(state["_workerPid"]) != state["_workerIdentity"]:
                    raise RuntimeError("Process supervisor exited before resizing the terminal")
                time.sleep(0.01)
    if action == "get":
        return snapshot(directory, raw_exit_code=request.get("rawExitCode") is True)
    if action in {"kill", "stop"}:
        state = json.loads((directory / "state").read_text())
        if (state["status"] not in TERMINAL or state.get("_preserveDescendants")) and (
            identity(state["_workerPid"]) == state["_workerIdentity"]
        ):
            write_json(directory / "termination", {"status": "killed" if action == "kill" else "stopped"})
            if identity(state["pid"]) == state["_identity"]:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(int(state["pid"]), signal.SIGKILL if action == "kill" else signal.SIGTERM)
        return {"message": "Process " + action + " requested"}
    if action in {"writeStdin", "closeStdin"}:
        with (directory / "lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads((directory / "state").read_text())
            if not state["stdin"]:
                raise ValueError("Process stdin was not enabled")
            if action == "closeStdin":
                (directory / "eof").touch()
            else:
                if state["status"] in TERMINAL or (directory / "eof").exists():
                    raise ProcessStdinClosed("Process stdin is closed")
                with (directory / "input").open("ab", buffering=0) as incoming:
                    incoming.write(base64.b64decode(request["data"]))
        return {"message": "Process stdin updated"}
    raise ValueError(f"Unknown process operation {action}")


if __name__ == "__main__":
    payload = json.loads(base64.b64decode(sys.argv[2]))
    if sys.argv[1] == "worker":
        worker(payload, int(sys.argv[3]))
    else:
        try:
            current = dispatch(payload)
            print(json.dumps({"result": current}), flush=True)
            if sys.argv[1] == "supervise":
                directory = locate(current["name"])
                while True:
                    if (directory / "worker-complete").exists():
                        if (directory / "error").exists():
                            raise RuntimeError(json.loads((directory / "error").read_text())["error"])
                        break
                    state = json.loads((directory / "state").read_text())
                    if (
                        identity(state["_workerPid"]) != state["_workerIdentity"]
                        and not (directory / "worker-complete").exists()
                    ):
                        raise RuntimeError("Process supervisor exited before completion")
                    time.sleep(0.02)
        except Exception as error:
            print(
                json.dumps(
                    {
                        "error": str(error),
                        **({"code": "process_not_found"} if isinstance(error, ProcessNotFound) else {}),
                        **({"code": "process_stdin_closed"} if isinstance(error, ProcessStdinClosed) else {}),
                    }
                )
            )
            sys.exit(1)
