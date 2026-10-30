from __future__ import annotations

import asyncio
import types

import pytest
from test_modal_native import private_guest as private_guest


@pytest.mark.parametrize("aio", [False, True])
@pytest.mark.parametrize("pty", [False, True])
def test_modal_pty_uses_native_terminal_defaults_and_private_control(monkeypatch, aio, pty):
    pytest.importorskip("modal")
    from modal._output import pty as native_pty
    from modal.container_process import ContainerProcess

    from arker import _modal_defaults
    from arker._compat_process import GuestProcesses
    from arker.modal import Sandbox

    monkeypatch.setattr(native_pty, "get_winsz", lambda: (31, 93))
    monkeypatch.setenv("TERM", "caller-term")
    monkeypatch.setenv("COLORTERM", "caller-color")
    monkeypatch.setenv("TERM_PROGRAM", "caller-program")
    prepared = []
    requests = []

    def prepare(vm, argv, env, workdir):
        prepared.append((argv, env, workdir))
        return ["private-control-reference"], "owned-control.json"

    def spawn(self, request):
        requests.append(request)
        return types.SimpleNamespace(pid="owned-process")

    monkeypatch.setattr(_modal_defaults, "prepare_exec", prepare)
    monkeypatch.setattr(GuestProcesses, "exec", spawn)
    sandbox = Sandbox(vm=types.SimpleNamespace(id="owned-vm", metadata={}))
    options = {
        "pty": pty,
        "env": {"TERM": "explicit-term", "SECRET": "synthetic-value"},
        "workdir": "/tmp",
        "text": False,
    }
    process = asyncio.run(sandbox.exec.aio("cat", **options)) if aio else sandbox.exec("cat", **options)
    assert isinstance(process, ContainerProcess)
    assert prepared == [
        (
            ("cat",),
            {
                **({"COLORTERM": "caller-color", "TERM_PROGRAM": "caller-program"} if pty else {}),
                "TERM": "explicit-term",
                "SECRET": "synthetic-value",
            },
            "/tmp",
        )
    ]
    assert requests[0]["argv"] == ["private-control-reference"]
    assert requests[0]["env"] == {}
    assert "synthetic-value" not in repr(requests)
    assert requests[0].get("tty") == ({"rows": 31, "cols": 93} if pty else None)
    assert requests[0]["preserveDescendants"] is True
    assert requests[0]["_modalControl"] == "owned-control.json"


@pytest.mark.parametrize("aio", [False, True])
def test_modal_pty_real_guest_streams_and_exit(private_guest, aio):
    pytest.importorskip("modal")
    from arker.modal import Sandbox

    command = (
        "import os,json;print(json.dumps([os.isatty(i) for i in range(3)]));"
        "os.write(2,b'error-marker\\n');raise SystemExit(7)"
    )
    sandbox = Sandbox(vm=private_guest)

    async def run():
        process = await sandbox.exec.aio("python3", "-c", command, pty=True)
        output = await process.stdout.read.aio()
        error = await process.stderr.read.aio()
        code = await process.wait.aio()
        return output, error, code

    if aio:
        output, error, code = asyncio.run(run())
    else:
        process = sandbox.exec("python3", "-c", command, pty=True)
        output, error, code = process.stdout.read(), process.stderr.read(), process.wait()
    assert output == "[true, true, true]\r\nerror-marker\r\n"
    assert error == ""
    assert code == 7
    assert list((private_guest.directory / "guest-root" / ".arker-modal").glob("exec-*.json")) == []
