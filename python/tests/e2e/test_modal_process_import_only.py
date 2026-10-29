"""Compare Modal process behavior with only the Sandbox import changed."""

import os

import pytest
from compat_evidence import record_scenario

PROGRAM = r"""
from modal import Sandbox
from modal.container_process import ContainerProcess
from modal.exception import InvalidError
import time

def program(identifier):
    sandbox = Sandbox.from_id(identifier)
    assert sandbox.object_id == identifier
    process = sandbox.exec("sh", "-c", "cat; printf error >&2; exit 17", text=False)
    assert isinstance(process, ContainerProcess)
    assert process.poll() is None
    try:
        process.returncode
    except InvalidError:
        pass
    else:
        raise AssertionError("returncode must require poll/wait completion")
    data = bytes(range(256)) * 1024
    process.stdin.write(data)
    process.stdin.write_eof()
    process.stdin.drain()
    assert process.stdout.read() == data
    assert process.stderr.read() == b"error"
    assert process.wait() == 17
    assert process.returncode == 17
    assert process.stdout.read() == b""
    process = sandbox.exec("sh", "-c", 'printf "%s|%s" "$HOME" "$VALUE"', env={"HOME":None, "VALUE":"set"})
    assert process.stdout.read() == "/root|set"
    assert process.wait() == 0
    process = sandbox.exec("sh", "-c", 'printf "%s|%s" "${VALUE-unset}" "$1"', "sh", "literal value")
    assert process.stdout.read() == "unset|literal value"
    assert process.wait() == 0
    for command, options, diagnostic in [
        (("arker-certainly-missing-executable",), {}, "arker-certainly-missing-executable"),
        (("pwd",), {"workdir":"/arker-certainly-missing-directory"}, "/arker-certainly-missing-directory"),
    ]:
        process = sandbox.exec(*command, timeout=5, **options)
        assert isinstance(process, ContainerProcess)
        assert process.wait() == 128
        assert process.stdout.read() == ""
        assert diagnostic in process.stderr.read()
    descendant = sandbox.exec("sh", "-c", "(sleep 1; printf late; touch /tmp/arker-descendant-probe)& exit 0")
    assert descendant.wait() == 0
    time.sleep(2)
    assert descendant.stdout.read() == "late"
    marker = sandbox.exec("sh", "-c", "test -e /tmp/arker-descendant-probe")
    assert marker.wait() == 0
"""


@pytest.mark.parametrize("provider,shim", [("modal", False), ("modal", True), ("arker", True)])
def test_modal_process_import_only(provider, shim, live_router, monkeypatch):
    modal = pytest.importorskip("modal")
    required = ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET") if provider == "modal" else ("ARKER_API_KEY",)
    if not all(os.environ.get(key) for key in required):
        pytest.skip("Live credentials required")
    if provider == "modal":
        fixture = modal.Sandbox.create(
            app=modal.App.lookup("arker-compat-image-test", create_if_missing=True), timeout=180
        )
        identifier = fixture.object_id
        cleanup = fixture.terminate
    else:
        from arker import Arker

        fixture = Arker().fork(
            source_vm_name=os.environ.get("ARKER_SOURCE", "ubuntu"),
            policies={"policies": [{"type": "inbound", "action": "deny"}, {"type": "outbound", "action": "deny"}]},
        )
        identifier = fixture.id
        cleanup = fixture.delete
        monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
        monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    with record_scenario(
        "modal",
        "sync",
        provider,
        shim,
        "import-only-process-binary-env-startup",
        [
            "Sandbox.from_id",
            "Sandbox.object_id",
            "Sandbox.exec",
            "ContainerProcess.poll",
            "ContainerProcess.wait",
            "ContainerProcess.returncode",
            "ContainerProcess.stdout",
            "ContainerProcess.stderr",
            "ContainerProcess.stdin",
            "ContainerProcess.stdout.read",
            "ContainerProcess.stderr.read",
            "ContainerProcess.stdin.write",
            "ContainerProcess.stdin.write_eof",
            "ContainerProcess.stdin.drain",
        ],
    ):
        try:
            source = (
                PROGRAM.replace("from modal import Sandbox", "from arker.modal import Sandbox") if shim else PROGRAM
            )
            namespace = {}
            exec(compile(source, "modal_process_import_only.py", "exec"), namespace)
            namespace["program"](identifier)
        finally:
            cleanup()


ASYNC_PROGRAM = r"""
from modal import Sandbox
from modal.container_process import ContainerProcess

async def program(options):
    sandbox = await Sandbox.create.aio(**options)
    try:
        found = await Sandbox.from_id.aio(sandbox.object_id)
        assert found.object_id == sandbox.object_id
        process = await found.exec.aio("sh", "-c", "cat; printf error >&2; exit 17", text=False)
        assert isinstance(process, ContainerProcess)
        data = bytes(range(256)) * 1024
        process.stdin.write(data)
        process.stdin.write_eof()
        await process.stdin.drain.aio()
        assert await process.stdout.read.aio() == data
        assert await process.stderr.read.aio() == b"error"
        assert await process.wait.aio() == 17
        assert await process.stdout.read.aio() == b""
    finally:
        await sandbox.terminate.aio()
"""


@pytest.mark.parametrize("provider,shim", [("modal", False), ("modal", True), ("arker", True)])
def test_modal_async_process_import_only(provider, shim, live_router, monkeypatch):
    import asyncio
    from contextlib import nullcontext

    modal = pytest.importorskip("modal")
    required = ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET") if provider == "modal" else ("ARKER_API_KEY",)
    if not all(os.environ.get(key) for key in required):
        pytest.skip("Live credentials required")
    app = modal.App("arker-compat-async-process") if provider == "modal" else None
    options = {"app": app, "timeout": 180} if app else {}
    if provider == "arker":
        monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
        monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    with (
        record_scenario(
            "modal",
            "async",
            provider,
            shim,
            "import-only-async-create-process-cleanup",
            [
                "Sandbox.create",
                "Sandbox.from_id",
                "Sandbox.exec",
                "Sandbox.terminate",
                "ContainerProcess.stdin.drain",
                "ContainerProcess.stdout.read",
                "ContainerProcess.stderr.read",
                "ContainerProcess.wait",
            ],
        ),
        app.run() if app else nullcontext(),
    ):
        source = (
            ASYNC_PROGRAM.replace("from modal import Sandbox", "from arker.modal import Sandbox")
            if shim
            else ASYNC_PROGRAM
        )
        namespace = {}
        exec(compile(source, "modal_async_process_import_only.py", "exec"), namespace)
        asyncio.run(namespace["program"](options))
