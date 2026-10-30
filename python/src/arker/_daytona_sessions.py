"""Daytona persistent sessions and incremental command log readers."""

from __future__ import annotations

import asyncio
import base64
import codecs
import hashlib
import inspect
import json
import shlex
import threading
import time
import uuid
from importlib.resources import files
from types import SimpleNamespace

from ._compat import UnsupportedCompatibilityOption, import_optional, run_command
from ._compat_process import GuestProcesses
from .computer import CompletedRunResult

_PREFIX = "daytona-session:"


def model(name, **values):
    module = import_optional(
        "daytona.common.process"
        if name in {"SessionExecuteResponse", "SessionCommandLogsResponse"}
        else "daytona_toolbox_api_client"
    )
    if module is not None:
        return getattr(module, name)(**values)
    if name == "Session" and isinstance(values.get("commands"), list):
        values["commands"] = [model("Command", **command) for command in values["commands"]]
    return SimpleNamespace(**values)


class SessionProcess:
    def _session_source(self):
        source = files("arker").joinpath("_guest_daytona_session.py").read_text()
        path = "/usr/local/lib/arker-compat/daytona-session-" + hashlib.sha256(source.encode()).hexdigest()[:16] + ".py"
        if getattr(self, "_session_path", None) != path:
            self._vm.sync(path, source)
            self._session_path = path
        return path

    def _session_call(self, action, request_timeout=None, **values):
        if request_timeout is not None:
            if request_timeout < 0:
                raise ValueError("request_timeout must be nonnegative")
            raise UnsupportedCompatibilityOption("Daytona session request_timeout is not supported by this transport")
        payload = base64.b64encode(json.dumps({"action": action, **values}).encode()).decode()
        lock = self.__dict__.setdefault("_session_control_lock", threading.Lock())
        with lock:
            if not hasattr(self, "_session_control_id"):
                self._session_control_id = self._vm.create_session().session_id
            response = self._vm.run(
                "python3 -I " + shlex.quote(self._session_source()) + " call " + shlex.quote(payload),
                session_id=self._session_control_id,
            )
            if not isinstance(response, CompletedRunResult):
                raise RuntimeError("Session control did not complete synchronously")
        parsed = json.loads(response.stdout)
        if "error" in parsed:
            native = import_optional("daytona.common.errors")
            if native is not None:
                if parsed.get("code") == "input_closed":
                    raise native.DaytonaInternalServerError(parsed["error"], status_code=500)
                if parsed.get("code") == "session_ended":
                    raise native.DaytonaSessionEndedError(parsed["error"], status_code=410)
                if parsed.get("code") == "not_found":
                    raise native.DaytonaNotFoundError(parsed["error"], status_code=404)
                raise native.DaytonaError(parsed["error"])
            raise RuntimeError(parsed["error"])
        return parsed["result"]

    def create_session(self, session_id, request_timeout=None):
        self._create_session_owned(session_id, request_timeout)

    def _create_session_owned(self, session_id, request_timeout=None):
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("Session ID must be a nonempty string")
        if request_timeout is not None:
            raise UnsupportedCompatibilityOption("Daytona session request_timeout is not supported by this transport")
        path = self._session_source()
        account_source = files("arker").joinpath("_guest_daytona_pty.py").read_text()
        home, _ = json.loads(run_command(self._vm, "python3 -I -c " + shlex.quote(account_source)).stdout)
        manager = GuestProcesses(self._vm)
        name = _PREFIX + uuid.uuid4().hex
        started = manager.exec(
            {
                "name": name,
                "exclusiveName": _PREFIX + session_id,
                "command": json.dumps({"id": session_id}),
                "argv": ["python3", "-I", path, "serve", session_id, name],
                "user": "daytona",
                "workingDir": home,
                "preserveDescendants": True,
                "env": {"HOME": home, "USER": "daytona", "LOGNAME": "daytona"},
            }
        )
        try:
            deadline = time.monotonic() + (request_timeout or 30)
            while True:
                try:
                    self.get_session(session_id, request_timeout)
                    return started.name
                except Exception:
                    current = manager.get(started.name)
                    if current.status != "running" or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
        except BaseException as primary:
            primary.session_id = session_id
            primary.process_id = started.name
            try:
                manager.kill(started.name)
            except BaseException as cleanup:
                raise primary from cleanup
            raise

    def _discard_created_session(self, session_id, identifier):
        manager = GuestProcesses(self._vm)
        try:
            self._session_call("delete", session_id=session_id, instance=identifier)
        finally:
            manager.kill(identifier)

    def get_session(self, session_id, request_timeout=None):
        return model("Session", **self._session_call("get", request_timeout, session_id=session_id))

    def list_sessions(self, request_timeout=None):
        return [model("Session", **value) for value in self._session_call("list", request_timeout)]

    def get_session_command(self, session_id, command_id, request_timeout=None):
        return model(
            "Command", **self._session_call("command", request_timeout, session_id=session_id, command_id=command_id)
        )

    def execute_session_command(self, session_id, req, timeout=None):
        command = self._session_call(
            "execute",
            session_id=session_id,
            command=req.command,
            suppress_input_echo=bool(getattr(req, "suppress_input_echo", False)),
        )
        if getattr(req, "run_async", False):
            return model(
                "SessionExecuteResponse", cmd_id=command["id"], output="", stdout="", stderr="", exit_code=None
            )
        deadline = None if not timeout else time.monotonic() + timeout
        while command["exit_code"] is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Timed out waiting for session command; command continues running")
            time.sleep(0.02)
            command = self._session_call("command", session_id=session_id, command_id=command["id"])
        logs = self.get_session_command_logs(session_id, command["id"])
        return model(
            "SessionExecuteResponse",
            cmd_id=command["id"],
            exit_code=command["exit_code"],
            output=logs.output,
            stdout=logs.stdout,
            stderr=logs.stderr,
        )

    def get_session_command_logs(self, session_id, command_id, request_timeout=None):
        streams = {}
        for stream in ("stdout", "stderr", "output"):
            data = bytearray()
            while True:
                chunk = self._session_call(
                    "read",
                    request_timeout,
                    session_id=session_id,
                    command_id=command_id,
                    stream=stream,
                    offset=len(data),
                )
                decoded = base64.b64decode(chunk["data"])
                data.extend(decoded)
                if len(decoded) < 32768:
                    break
            streams[stream] = data.decode("utf-8", errors="replace")
        return model("SessionCommandLogsResponse", **streams)

    async def get_session_command_logs_async(self, session_id, command_id, on_stdout, on_stderr):
        offsets = {"stdout": 0, "stderr": 0}
        decoders = {stream: codecs.getincrementaldecoder("utf-8")("replace") for stream in offsets}
        finished = set()
        while len(finished) < 2:
            for stream, callback in (("stdout", on_stdout), ("stderr", on_stderr)):
                if stream in finished:
                    continue
                chunk = await asyncio.to_thread(
                    self._session_call,
                    "read",
                    session_id=session_id,
                    command_id=command_id,
                    stream=stream,
                    offset=offsets[stream],
                )
                data = base64.b64decode(chunk["data"])
                offsets[stream] += len(data)
                text = decoders[stream].decode(data, final=chunk["eof"])
                if text:
                    returned = callback(text)
                    if inspect.isawaitable(returned):
                        await returned
                if chunk["eof"]:
                    finished.add(stream)
            if len(finished) < 2:
                await asyncio.sleep(0.02)

    def send_session_command_input(self, session_id, command_id, data, request_timeout=None):
        encoded = data.encode()
        offset = 0
        while offset < len(encoded):
            response = self._session_call(
                "input",
                request_timeout,
                session_id=session_id,
                command_id=command_id,
                data=base64.b64encode(encoded[offset : offset + 32768]).decode(),
            )
            offset += response["written"]
            if response["written"] == 0:
                time.sleep(0.02)

    def delete_session(self, session_id, request_timeout=None):
        manager = GuestProcesses(self._vm)
        current = [
            p
            for p in manager.list(include_output=False)
            if p.name.startswith(_PREFIX) and json.loads(p.command)["id"] == session_id
        ]
        self._session_call("delete", request_timeout, session_id=session_id)
        for process in current:
            manager.kill(process.name)
