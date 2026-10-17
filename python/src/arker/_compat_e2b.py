from __future__ import annotations

import json
import time
from importlib.resources import files

from ._compat import UnsupportedCompatibilityOption
from ._compat_process import GuestProcessError

# E2B's Linux guest reports Go syscall.Signal strings, not the client's OS dialect.
# https://github.com/golang/go/blob/master/src/syscall/zerrors_linux_amd64.go
SIGNAL_MESSAGES = json.loads(files("arker").joinpath("_linux_signals.json").read_text())


def exit_fields(result):
    code = result.exit_code
    error = None
    if code < 0:
        description = SIGNAL_MESSAGES.get(str(-code), f"signal {-code}")
        error = f"signal: {description}"
        code = -1
    elif code != 0:
        error = f"exit status {code}"
    return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": code, "error": error}


def require_native_handle():
    try:
        from e2b import CommandHandle
        from e2b.envd.process import process_pb
        from protobuf import Oneof
    except ImportError as error:
        raise UnsupportedCompatibilityOption("E2B command handles require the e2b extra (SDK 2.56 or later)") from error
    return CommandHandle, process_pb, Oneof


def process_control(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except GuestProcessError as error:
        if error.code != "process_not_found":
            raise
        from e2b import NotFoundException

        raise NotFoundException(str(error)) from error


def command_handle(processes, current, timeout):
    native_handle, proto, oneof = require_native_handle()
    identifier = str(current.pid)
    deadline = None if timeout in (None, 0) else time.monotonic() + timeout

    def events():
        from e2b import TimeoutException

        offsets = {"stdout": 0, "stderr": 0}
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutException("Timed out waiting for command")
            value = process_control(processes.get, identifier, raw_exit_code=True)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutException("Timed out waiting for command")
            for stream in offsets:
                text = getattr(value, stream)
                chunk = text[offsets[stream] :]
                offsets[stream] = len(text)
                if chunk:
                    yield proto.StartResponse(
                        event=proto.ProcessEvent(
                            event=oneof(
                                "data",
                                proto.ProcessEvent.DataEvent(output=oneof(stream, chunk.encode())),
                            )
                        )
                    )
            if value.status != "running":
                fields = exit_fields(value)
                end = proto.ProcessEvent.EndEvent(exit_code=fields["exit_code"], exited=True, error=fields["error"])
                yield proto.StartResponse(event=proto.ProcessEvent(event=oneof("end", end)))
                return
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            time.sleep(0.05 if remaining is None else min(0.05, remaining))

    def kill():
        try:
            if processes.get(identifier).status != "running":
                return False
        except GuestProcessError as error:
            if error.code == "process_not_found":
                return False
            raise
        processes.kill(identifier)
        return True

    def stdin(data, request_timeout=None):
        if request_timeout is not None:
            raise UnsupportedCompatibilityOption("Per-request stdin timeouts are not yet supported on Arker")
        process_control(processes.write_stdin, identifier, data)

    def eof(request_timeout=None):
        if request_timeout is not None:
            raise UnsupportedCompatibilityOption("Per-request stdin timeouts are not yet supported on Arker")
        process_control(processes.close_stdin, identifier)

    return native_handle(
        pid=int(current.pid),
        events=events(),
        handle_kill=kill,
        handle_send_stdin=stdin,
        handle_close_stdin=eof,
    )
