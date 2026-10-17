"""Guest process protocol shared by the Python and TypeScript shims.

Only the standard library is used. State lives in the VM, so reconnecting SDK
clients can observe a process without executing its command again.
"""

from __future__ import annotations

import base64
import codecs
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(os.environ.get("ARKER_COMPAT_PROCESS_ROOT", "/tmp/arker-compat-processes"))
TERMINAL = {"completed", "failed", "stopped", "killed"}


class ProcessNotFound(ValueError):
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
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
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
        "_preserveDescendants": request.get("preserveDescendants") is True,
        "_workerPid": os.getpid(),
        "_workerIdentity": identity(os.getpid()),
    }
    with (
        (directory / "stdout").open("ab", buffering=0) as stdout,
        (directory / "stderr").open("ab", buffering=0) as stderr,
    ):
        child = None
        try:
            if request.get("preserveDescendants"):
                import ctypes

                # Adopt orphaned descendants even when they double-fork or setsid.
                if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
                    raise OSError(ctypes.get_errno(), "Cannot supervise detached process descendants")
            child = subprocess.Popen(
                request.get("argv", command),
                shell="argv" not in request,
                executable=None if "argv" in request else "/bin/sh",
                cwd=cwd,
                env=environment,
                stdout=stdout,
                stderr=stderr,
                stdin=subprocess.PIPE if request.get("stdin") else subprocess.DEVNULL,
                start_new_session=True,
            )
            state.update(pid=str(child.pid), _identity=identity(child.pid))
            write_json(directory / "state", state)
            os.write(ready_fd, b"1")
            os.close(ready_fd)
            ready_fd = -1
            started = time.monotonic()
            pending = b""
            if child.stdin:
                os.set_blocking(child.stdin.fileno(), False)
            with (directory / "input").open("rb") as incoming:
                while child.poll() is None:
                    if (request.get("timeout") or 0) > 0 and (time.monotonic() - started) * 1000 >= request["timeout"]:
                        write_json(directory / "termination", {"status": "killed"})
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(child.pid, signal.SIGKILL)
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
                    time.sleep(0.02)
            code = child.wait()
            state["exitCode"] = code if code >= 0 else 128 - code
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
            if ready_fd >= 0:
                os.close(ready_fd)
            if request.get("preserveDescendants"):
                while True:
                    try:
                        os.waitpid(-1, 0)
                    except ChildProcessError:
                        break
                    except InterruptedError:
                        continue
            (directory / "worker-complete").touch()


def dispatch(request):
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    action = request["action"]
    if action == "exec":
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
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            state = json.loads((directory / "state").read_text())
            if request.get("_requestId") and state.get("_request_id") == request["_requestId"]:
                return snapshot(directory)
            raise ValueError(f"Process {request['name']!r} already exists") from None
        request["_directory"] = str(directory)
        for name in ("stdout", "stderr", "input", "lock"):
            (directory / name).touch(mode=0o600)
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
    if action == "get":
        return snapshot(directory, raw_exit_code=request.get("rawExitCode") is True)
    if action in {"kill", "stop"}:
        state = json.loads((directory / "state").read_text())
        if state["status"] not in TERMINAL and identity(state["pid"]) == state["_identity"]:
            write_json(directory / "termination", {"status": "killed" if action == "kill" else "stopped"})
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
                    raise ValueError("Process stdin is closed")
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
                    state = json.loads((directory / "state").read_text())
                    if state.get("_preserveDescendants"):
                        if (directory / "worker-complete").exists():
                            break
                        if identity(state["_workerPid"]) != state["_workerIdentity"]:
                            raise RuntimeError("Process supervisor exited before its descendants completed")
                    elif state["status"] in TERMINAL:
                        break
                    if (directory / "error").exists():
                        raise RuntimeError(json.loads((directory / "error").read_text())["error"])
                    time.sleep(0.02)
        except Exception as error:
            print(
                json.dumps(
                    {
                        "error": str(error),
                        **({"code": "process_not_found"} if isinstance(error, ProcessNotFound) else {}),
                    }
                )
            )
            sys.exit(1)
