from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import shlex
import threading
import time
import uuid
from importlib.resources import files
from typing import Any

from ._compat import CommandResult, import_optional
from .computer import REQUEST_TIMEOUT_S, VM, CompletedRunResult


@dataclasses.dataclass(frozen=True)
class ProcessResult(CommandResult):
    name: str
    command: str
    working_dir: str
    pid: str
    status: str
    started_at: str
    completed_at: str
    logs: str
    stdin: bool

    def to_dict(self) -> dict[str, Any]:
        values = dataclasses.asdict(self)
        for snake, camel in (
            ("exit_code", "exitCode"),
            ("working_dir", "workingDir"),
            ("started_at", "startedAt"),
            ("completed_at", "completedAt"),
        ):
            values[camel] = values.pop(snake)
        return values


def result(value: dict[str, Any]) -> ProcessResult | Any:
    models = import_optional("blaxel.core.sandbox.client.models")
    if models is not None:
        return models.ProcessResponse.from_dict(value)
    values = dict(value)
    for snake, camel in (
        ("exit_code", "exitCode"),
        ("working_dir", "workingDir"),
        ("started_at", "startedAt"),
        ("completed_at", "completedAt"),
    ):
        values[snake] = values.pop(camel)
    return ProcessResult(**values)


@dataclasses.dataclass(frozen=True)
class SuccessResponse:
    message: str
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"message": self.message, **({"path": self.path} if self.path is not None else {})}


def success(value):
    models = import_optional("blaxel.core.sandbox.client.models")
    return models.SuccessResponse.from_dict(value) if models is not None else SuccessResponse(**value)


class GuestProcessError(RuntimeError):
    def __init__(self, message, code=None):
        self.code = code
        super().__init__(message)


class GuestProcesses:
    def __init__(self, vm: VM) -> None:
        self._vm = vm
        self._installed = False
        self._lock = threading.Lock()
        self._control_lock = threading.Lock()
        self._control_session = None
        self._source = files("arker").joinpath("_guest_process.py").read_text()
        self._path = f"/tmp/arker-compat-process-{hashlib.sha256(self._source.encode()).hexdigest()[:16]}.py"

    def _call(self, action: str, **values: Any) -> Any:
        with self._lock:
            if not self._installed:
                self._vm.sync(self._path, self._source)
                self._installed = True
        payload = base64.b64encode(json.dumps({"action": action, **values}).encode()).decode()
        if action == "exec":
            session = self._vm.create_session()
            response = self._vm.run(
                f"python3 {shlex.quote(self._path)} supervise {shlex.quote(payload)}",
                session_id=session.session_id,
                time_to_background=0,
            )
            deadline = time.monotonic() + REQUEST_TIMEOUT_S
            while response.type == "background":
                current = self._vm.get_run(response.run_id)
                if "\n" in current.stdout:
                    response = CommandResult(stdout=current.stdout.splitlines()[0], stderr=current.stderr, exit_code=0)
                    break
                if current.state not in {"pending", "running"}:
                    raise RuntimeError(current.stderr or "Guest process supervisor stopped before reporting readiness")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Guest process supervisor did not report readiness")
                time.sleep(0.02)
        else:
            with self._control_lock:
                if self._control_session is None:
                    self._control_session = self._vm.create_session().session_id
                response = self._vm.run(
                    f"python3 {shlex.quote(self._path)} call {shlex.quote(payload)}",
                    session_id=self._control_session,
                )
                if not isinstance(response, CompletedRunResult):
                    raise RuntimeError("Guest process control did not complete synchronously")
        try:
            parsed = json.loads(response.stdout)
        except ValueError as error:
            raise RuntimeError(response.stderr or "Guest process agent returned invalid JSON") from error
        if "error" in parsed:
            raise GuestProcessError(parsed["error"], parsed.get("code"))
        if response.exit_code != 0:
            raise RuntimeError(response.stderr or "Guest process agent failed")
        return parsed["result"]

    def exec(self, request: dict[str, Any]) -> ProcessResult:
        values = dict(request)
        callbacks = {key: values.pop(key, None) for key in ("on_log", "on_stdout", "on_stderr")}
        wait = values.pop("waitForCompletion", False)
        if values.get("name") is None:
            values["name"] = uuid.uuid4().hex
        values["_requestId"] = uuid.uuid4().hex
        current = result(self._call("exec", **values))
        if wait:
            current = self._wait(current.name, -1, 50, callbacks)
        return current

    def get(
        self, identifier: str, *, retry: bool = True, timeout: float | None = None, raw_exit_code: bool = False
    ) -> ProcessResult:
        return result(self._call("get", identifier=identifier, **({"rawExitCode": True} if raw_exit_code else {})))

    def list(self) -> list[ProcessResult]:
        return [result(value) for value in self._call("list")]

    def _wait(self, identifier, max_wait, interval, callbacks=None):
        if max_wait < -1 or interval <= 0:
            raise ValueError("max_wait must be -1 or nonnegative; interval must be positive")
        deadline = None if max_wait == -1 else time.monotonic() + max_wait / 1000
        offsets = {"stdout": 0, "stderr": 0}
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for process {identifier}")
            current = self.get(identifier)
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(f"Timed out waiting for process {identifier}")
            for stream in offsets:
                data = getattr(current, stream)[offsets[stream] :]
                offsets[stream] += len(data)
                for key in (f"on_{stream}", "on_log"):
                    callback = (callbacks or {}).get(key)
                    if data and callback:
                        callback(data)
            if current.status in {"completed", "failed", "stopped", "killed"}:
                return current
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for process {identifier}")
            time.sleep(
                interval / 1000 if deadline is None else min(interval / 1000, max(0, deadline - time.monotonic()))
            )

    def wait(self, identifier: str, max_wait: int = 60000, interval: int = 1000) -> ProcessResult:
        return self._wait(identifier, max_wait, interval)

    def kill(self, identifier: str) -> Any:
        return success(self._call("kill", identifier=identifier))

    def stop(self, identifier: str) -> Any:
        return success(self._call("stop", identifier=identifier))

    def logs(self, identifier: str, log_type: str = "all") -> str:
        if log_type not in {"stdout", "stderr", "all"}:
            raise ValueError("Unsupported log type")
        current = self.get(identifier)
        return current.logs if log_type == "all" else getattr(current, log_type)

    def write_stdin(self, identifier: str, data: str | bytes) -> Any:
        raw = data.encode() if isinstance(data, str) else data
        return success(self._call("writeStdin", identifier=identifier, data=base64.b64encode(raw).decode()))

    def close_stdin(self, identifier: str) -> Any:
        return success(self._call("closeStdin", identifier=identifier))
