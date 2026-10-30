"""Persistent unprivileged shells with bounded local control messages."""

import base64
import hashlib
import json
import os
import pwd
import queue
import select
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path


class SessionNotFound(RuntimeError):
    pass


LIMIT = 128 * 1024


def address(identifier):
    root = Path(tempfile.gettempdir()) / f"arker-daytona-sessions-{os.getuid()}"
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if not root.is_dir() or root.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("Session directory is not private")
    return root / hashlib.sha256(identifier.encode()).hexdigest()


def receive(connection):
    data = bytearray()
    while not data.endswith(b"\n"):
        chunk = connection.recv(min(65536, LIMIT + 1 - len(data)))
        if not chunk:
            raise RuntimeError("Session control disconnected")
        data.extend(chunk)
        if len(data) > LIMIT:
            raise ValueError("Session control message too large")
    return json.loads(data)


def serve(identifier, instance=None):
    directory = address(identifier)
    directory.mkdir(mode=0o700)
    commands = {}
    pending = queue.Queue()
    stopping = threading.Event()
    try:
        shell = subprocess.Popen([pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"], stdin=subprocess.PIPE)
    except BaseException:
        shutil.rmtree(directory)
        raise

    def execute():
        while not stopping.is_set():
            command = pending.get()
            if command is None:
                break
            folder = directory / command["id"]
            fifo = command["input_fd"]
            command["running"] = True
            outputs = {
                stream: os.open(folder / (stream + "-pipe"), os.O_RDWR | os.O_NONBLOCK)
                for stream in ("stdout", "stderr")
            }
            continuing = dict.fromkeys(outputs, False)

            def capture(final=False, outputs=outputs, continuing=continuing, folder=folder, lock=command["lock"]):
                readable, _, _ = select.select(list(outputs.values()), [], [], 0 if final else 0.01)
                for stream, fd in outputs.items():
                    data = os.read(fd, 32768) if fd in readable else b""
                    with lock, (folder / stream).open("ab") as log, (folder / "output").open("ab") as combined:
                        for part in data.splitlines(keepends=True):
                            if not continuing[stream]:
                                combined.write(b"\1\1\1" if stream == "stdout" else b"\2\2\2")
                            log.write(part)
                            combined.write(part)
                            continuing[stream] = not part.endswith(b"\n")
                        if final and not readable and continuing[stream]:
                            log.write(b"\n")
                            combined.write(b"\n")
                            continuing[stream] = False
                return bool(readable)

            line = (
                f". {shlex.quote(str(folder / 'script'))} < {shlex.quote(str(folder / 'input'))} "
                f"> {shlex.quote(str(folder / 'stdout-pipe'))} 2> {shlex.quote(str(folder / 'stderr-pipe'))}; "
                f"printf '%s' \"$?\" > {shlex.quote(str(folder / 'exit'))}\n"
            )
            try:
                shell.stdin.write(line.encode())
                shell.stdin.flush()
                code = None
                while code is None and shell.poll() is None and not stopping.is_set():
                    capture()
                    try:
                        value = (folder / "exit").read_text()
                        code = int(value) if value else None
                    except FileNotFoundError:
                        pass
                while not stopping.is_set() and capture(final=True):
                    pass
                command["exit_code"] = code if code is not None else (shell.poll() if shell.poll() is not None else 137)
            except (BrokenPipeError, OSError):
                command["exit_code"] = shell.poll() if shell.poll() is not None else 1
            finally:
                with command["lock"]:
                    command["running"] = False
                    command.pop("input_fd", None)
                    os.close(fifo)
                for fd in outputs.values():
                    os.close(fd)

    def public(command):
        return {key: command[key] for key in ("id", "command", "exit_code")}

    def dispatch(request):
        action = request["action"]
        if action == "get":
            return {"session_id": identifier, "commands": [public(c) for c in commands.values()]}
        if action == "delete":
            if request.get("instance") is not None and request["instance"] != instance:
                raise RuntimeError("Session creation identity does not match")
            stopping.set()
            pending.put(None)
            shell.kill()
            worker.join()
            shutil.rmtree(directory)
            return None
        if action == "execute":
            if shell.poll() is not None:
                raise RuntimeError("Session shell has exited")
            command = {"id": uuid.uuid4().hex, "command": request["command"], "exit_code": None, "running": False}
            folder = directory / command["id"]
            folder.mkdir(mode=0o700)
            (folder / "script").write_text(command["command"] + "\n")
            for stream in ("stdout", "stderr", "output"):
                (folder / stream).touch(mode=0o600)
            for stream in ("stdout", "stderr"):
                os.mkfifo(folder / (stream + "-pipe"), 0o600)
            os.mkfifo(folder / "input", 0o600)
            command["lock"] = threading.Lock()
            command["input_fd"] = os.open(folder / "input", os.O_RDWR | os.O_NONBLOCK)
            command["suppress_input_echo"] = bool(request.get("suppress_input_echo"))
            commands[command["id"]] = command
            pending.put(command)
            return public(command)
        command = commands[request["command_id"]]
        if action == "command":
            return public(command)
        if action == "input":
            with command["lock"]:
                if command["exit_code"] is not None:
                    raise RuntimeError("Command has completed")
                data = base64.b64decode(request["data"], validate=True)
                try:
                    written = os.write(command["input_fd"], data)
                    if written and not command["suppress_input_echo"]:
                        folder = directory / command["id"]
                        with (folder / "stdout").open("ab") as log, (folder / "output").open("ab") as combined:
                            log.write(data[:written])
                            combined.write(b"\1\1\1" + data[:written])
                    return {"written": written}
                except BlockingIOError:
                    return {"written": 0}
        if action == "read":
            if (
                request["stream"] not in ("stdout", "stderr", "output")
                or type(request["offset"]) is not int
                or request["offset"] < 0
            ):
                raise ValueError("Invalid stream read")
            with (directory / command["id"] / request["stream"]).open("rb") as reader:
                reader.seek(request["offset"])
                data = reader.read(32768)
                end = reader.tell() == os.fstat(reader.fileno()).st_size
            return {"data": base64.b64encode(data).decode(), "eof": end and command["exit_code"] is not None}
        raise ValueError("Unknown session action")

    server = socket.socket(socket.AF_UNIX)
    server.bind(str(directory / "control"))
    server.listen(8)
    server.settimeout(0.1)
    worker = threading.Thread(target=execute)
    worker.start()
    try:
        while not stopping.is_set():
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            with connection:
                connection.settimeout(10)
                try:
                    response = {"result": dispatch(receive(connection))}
                except Exception as error:
                    response = {
                        "error": str(error),
                        "code": "not_found" if isinstance(error, (KeyError, FileNotFoundError)) else None,
                    }
                connection.sendall((json.dumps(response) + "\n").encode())
    finally:
        stopping.set()
        pending.put(None)
        shell.kill()
        shell.wait()
        worker.join()
        server.close()
        for command in commands.values():
            descriptor = command.pop("input_fd", None)
            if descriptor is not None:
                os.close(descriptor)
        shutil.rmtree(directory, ignore_errors=True)


def call(request):
    if request["action"] == "list":
        root = address("").parent
        result = []
        for path in root.glob("*/control"):
            try:
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.settimeout(5)
                    connection.connect(str(path))
                    connection.sendall(b'{"action":"get"}\n')
                    result.append(receive(connection)["result"])
            except (OSError, KeyError, RuntimeError):
                continue
        return result
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(10)
        connection.connect(str(address(request["session_id"]) / "control"))
        connection.sendall((json.dumps(request) + "\n").encode())
        response = receive(connection)
    if "error" in response:
        raise (SessionNotFound if response.get("code") == "not_found" else RuntimeError)(response["error"])
    return response["result"]


if __name__ == "__main__":
    if sys.argv[1] == "serve":
        serve(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        account = pwd.getpwnam("daytona")
        if account.pw_uid == 0:
            raise PermissionError("Daytona sessions require a non-root daytona account")
        os.initgroups(account.pw_name, account.pw_gid)
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
        try:
            print(json.dumps({"result": call(json.loads(base64.b64decode(sys.argv[2])))}))
        except Exception as error:
            print(
                json.dumps(
                    {
                        "error": str(error),
                        "code": "not_found"
                        if isinstance(error, (SessionNotFound, FileNotFoundError, ConnectionRefusedError))
                        else None,
                    }
                )
            )
            sys.exit(1)
