"""Daytona terminal handles backed by the shared guest process supervisor."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import inspect
import json
import shlex
import threading
import time
import uuid
from importlib.resources import files
from types import SimpleNamespace

from ._compat import import_optional, run_command
from ._compat_async import route
from ._compat_process import GuestProcesses
from ._daytona_request import request_timeout as response_timeout

_PREFIX = "daytona-pty:"
_TERMINAL = {"completed", "failed", "killed", "stopped"}


def _error(message):
    module = import_optional("daytona.common.errors")
    return module.DaytonaConnectionError(message) if module else RuntimeError(message)


def _result(code, error):
    module = import_optional("daytona.common.pty")
    return module.PtyResult(exit_code=code, error=error) if module else SimpleNamespace(exit_code=code, error=error)


def _size(value):
    rows, cols = (24, 80) if value is None else (value.rows, value.cols)
    if any(type(n) is not int or not 0 <= n <= 65535 for n in (rows, cols)):
        raise ValueError("PTY rows and cols must be integers between 0 and 65535")
    return {"rows": rows, "cols": cols}


class PtyHandle:
    def __init__(self, owner, session_id, identifier=None, offset=0):
        self._owner = owner
        self._identifier = identifier or _PREFIX + session_id
        self.session_id = session_id
        self.exit_code = None
        self.error = None
        self._connected = True
        self._offset = offset
        self._reader_lock = threading.Lock()

    def is_connected(self):
        return self._connected

    def wait_for_connection(self, timeout=10.0):
        if not self._connected:
            raise _error("PTY is not connected")

    def send_input(self, data):
        if not self._connected:
            raise _error("PTY is not connected")
        self._owner._pty_processes.write_stdin(self._identifier, data)

    def resize(self, pty_size):
        return self._owner.resize_pty_session(self.session_id, pty_size)

    def kill(self):
        self._owner._pty_processes.kill(self._identifier)

    def _read(self):
        with self._reader_lock:
            response = self._owner._pty_processes._call(
                "readStream", identifier=self._identifier, stream="stdout", offset=self._offset
            )
            self._offset = response["nextOffset"]
            if response["eof"]:
                self.exit_code = response["exitCode"]
                self.error = response.get("terminationSignal") or ("non-zero exit" if self.exit_code else None)
                self._connected = False
            return base64.b64decode(response["data"])

    def __iter__(self):
        while self._connected:
            data = self._read()
            if data:
                yield data
            elif self._connected:
                time.sleep(0.02)

    def wait(self, on_data=None, timeout=None):
        deadline = None if not timeout else time.monotonic() + timeout
        try:
            while self._connected and (deadline is None or time.monotonic() < deadline):
                data = self._read()
                if data and on_data:
                    on_data(data)
                if not data and self._connected:
                    time.sleep(0.02)
        except Exception as error:
            self.error = str(error)
        return _result(self.exit_code, self.error)

    def disconnect(self):
        self._connected = False


class AsyncPtyHandle:
    def __init__(self, handle, on_data):
        self._handle = handle
        self._on_data = on_data
        self._in_callback = contextvars.ContextVar("daytona_terminal_callback", default=False)
        self._reader = asyncio.create_task(self._read())

    @property
    def session_id(self):
        return self._handle.session_id

    @property
    def exit_code(self):
        return self._handle.exit_code

    @property
    def error(self):
        return self._handle.error

    def is_connected(self):
        return self._handle.is_connected()

    async def wait_for_connection(self, timeout=10.0):
        self._handle.wait_for_connection(timeout)

    async def _read(self):
        try:
            while self.is_connected():
                data = await asyncio.to_thread(self._handle._read)
                if data and self._on_data:
                    token = self._in_callback.set(True)
                    try:
                        returned = self._on_data(data)
                        if inspect.isawaitable(returned):
                            await returned
                    finally:
                        self._in_callback.reset(token)
                if not data and self.is_connected():
                    await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._handle.error = str(error)
            self._handle.disconnect()

    async def send_input(self, data):
        await asyncio.to_thread(self._handle.send_input, data)

    async def resize(self, pty_size):
        return await asyncio.to_thread(self._handle.resize, pty_size)

    async def kill(self):
        await asyncio.to_thread(self._handle.kill)

    async def wait(self):
        await asyncio.shield(self._reader)
        return _result(self.exit_code, self.error)

    async def disconnect(self):
        self._handle.disconnect()
        # Join any bounded read already in flight, rather than orphaning its thread.
        if not self._in_callback.get():
            await self._reader


class PtyProcess:
    @property
    def _pty_processes(self):
        if not hasattr(self, "_terminal_manager"):
            self._terminal_manager = GuestProcesses(self._vm)
        return self._terminal_manager

    def create_pty_session(self, id, cwd=None, envs=None, pty_size=None):
        if not isinstance(id, str) or not id:
            raise ValueError("PTY session ID must be a nonempty string")
        size = _size(pty_size)
        # Resolve the sandbox account rather than relying on the host command user.
        source = files("arker").joinpath("_guest_daytona_pty.py").read_text()
        account = run_command(self._vm, "python3 -I -c " + shlex.quote(source) + " account")
        home, shell = json.loads(account.stdout)
        environment = {"HOME": home, "USER": "daytona", "LOGNAME": "daytona", "TERM": "xterm-256color", **(envs or {})}
        directory = cwd or home
        started = self._pty_processes.exec(
            {
                "name": _PREFIX + uuid.uuid4().hex,
                "exclusiveName": _PREFIX + id,
                "command": json.dumps({"id": id, "cwd": directory, "envs": {"TERM": "xterm-256color", **(envs or {})}}),
                "argv": [shell or "/bin/sh", "-i"],
                "user": "daytona",
                "workingDir": directory,
                "stdin": True,
                "preserveDescendants": True,
                "ttyOutput": "writers",
                "env": environment,
                "tty": size,
            }
        )
        return PtyHandle(self, id, started.name)

    def connect_pty_session(self, session_id):
        current = self._current(session_id)
        stream = self._pty_processes._call("readStream", identifier=current.name, stream="stdout", offset=0, limit=1)
        return PtyHandle(self, session_id, current.name, stream["size"])

    def _pty_info(self, current):
        metadata = json.loads(current.command)
        values = {
            "id": metadata["id"],
            "active": current.status not in _TERMINAL,
            "created_at": current.started_at,
            "cwd": metadata["cwd"],
            "envs": metadata["envs"],
            "rows": current.tty["rows"],
            "cols": current.tty["cols"],
            "lazy_start": False,
        }
        module = import_optional("daytona_toolbox_api_client")
        return module.PtySessionInfo(**values) if module else SimpleNamespace(**values)

    def _current(self, session_id):
        for current in self._pty_processes.list(include_output=False):
            if (
                current.name.startswith(_PREFIX)
                and current.status not in _TERMINAL
                and json.loads(current.command)["id"] == session_id
            ):
                return current
        module = import_optional("daytona.common.errors")
        if module:
            raise module.DaytonaNotFoundError("PTY session not found", status_code=404)
        raise ValueError("PTY session not found")

    @response_timeout
    def get_pty_session_info(self, session_id, request_timeout=None):
        return self._pty_info(self._current(session_id))

    @response_timeout
    def list_pty_sessions(self, request_timeout=None):
        return [
            self._pty_info(p)
            for p in self._pty_processes.list(include_output=False)
            if p.name.startswith(_PREFIX) and p.status not in _TERMINAL
        ]

    @response_timeout
    def kill_pty_session(self, session_id, request_timeout=None):
        self._pty_processes.kill(self._current(session_id).name)

    @response_timeout
    def resize_pty_session(self, session_id, pty_size, request_timeout=None):
        size = _size(pty_size)
        self._pty_processes.resize(self._current(session_id).name, **size)
        info = self.get_pty_session_info(session_id, request_timeout)
        info.rows, info.cols = size["rows"], size["cols"]
        return info


class AsyncPtyProcess:
    async def create_pty_session(self, id, on_data, cwd=None, envs=None, pty_size=None):
        def discard(handle):
            try:
                handle.kill()
            finally:
                handle.disconnect()

        handle = await route(
            lambda: self._process.create_pty_session(id, cwd, envs, pty_size),
            cleanup=discard,
            drain_on_cancel=True,
        )
        return AsyncPtyHandle(handle, on_data)

    async def connect_pty_session(self, session_id, on_data):
        handle = await route(
            lambda: self._process.connect_pty_session(session_id),
            cleanup=lambda created: created.disconnect(),
            drain_on_cancel=True,
        )
        return AsyncPtyHandle(handle, on_data)
