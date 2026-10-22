"""Run the same Blaxel program with exactly its import changed."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import uuid

import pytest
from compat_evidence import record

PROGRAM = """
from blaxel.core.sandbox import SyncSandboxInstance
from blaxel.core.sandbox import ProcessRequestWithLog
from blaxel.core.sandbox.types import SandboxCreateConfiguration
from blaxel.core.sandbox.client.models import ProcessRequestEnv

sandbox = SyncSandboxInstance.create_if_not_exists(SandboxCreateConfiguration(name=sandbox_name))
try:
    assert sandbox.fs.ls("/blaxel").path == "/blaxel"
    response = sandbox.process.exec(ProcessRequestWithLog(
        command="printf '%s:%s' \\"$PWD\\" \\"$IMPORT_ONLY\\"; printf diagnostic >&2",
        working_dir="/tmp",
        env=ProcessRequestEnv.from_dict({"IMPORT_ONLY": "preserved"}),
        wait_for_completion=True,
        timeout=10000,
    ))
    assert response.stdout == "/tmp:preserved", response.stdout
    assert response.stderr == "diagnostic", response.stderr
    assert response.exit_code == 0
    isolated = sandbox.process.exec(ProcessRequestWithLog(
        command="printf '%s:%s' \\"$PWD\\" \\"${IMPORT_ONLY-unset}\\"",
        wait_for_completion=True,
    ))
    assert isolated.stdout == "/blaxel:unset", isolated.stdout
    sandbox.process.exec(ProcessRequestWithLog(
        command="sleep 0.2; printf within-deadline", name="seconds-timeout", timeout=1,
        wait_for_completion=False,
    ))
    within_deadline = sandbox.process.wait("seconds-timeout", max_wait=10000, interval=100)
    assert (within_deadline.status, within_deadline.exit_code, within_deadline.stdout) == (
        "completed", 0, "within-deadline",
    )
    sandbox.process.exec(ProcessRequestWithLog(
        command="while :; do sleep 1; done", name="execution-deadline", timeout=1,
        wait_for_completion=False,
    ))
    expired = sandbox.process.wait("execution-deadline", max_wait=10000, interval=100)
    assert (expired.status, expired.exit_code) == ("killed", -1)
    failed = sandbox.process.exec(ProcessRequestWithLog(
        command="printf failure-out; printf failure-err >&2; exit 17", wait_for_completion=True,
    ))
    assert (failed.stdout, failed.stderr, failed.exit_code, failed.status) == (
        "failure-out", "failure-err", 17, "failed",
    )
    made = sandbox.fs.mkdir("/blaxel/import-only")
    assert made.to_dict() == {"message": "Directory created successfully", "path": "/blaxel/import-only"}
    sandbox.fs.write("/blaxel/import-only/probe.txt", "first")
    sandbox.fs.write("/blaxel/import-only/probe.txt", "second")
    assert sandbox.fs.read("/blaxel/import-only/probe.txt") == "second"
    listing = sandbox.fs.ls("/blaxel/import-only")
    assert any(entry.name == "probe.txt" for entry in listing.files)
    for path, display in [
        ("/blaxel/import-only/", "/blaxel/import-only/"),
        ("/blaxel//import-only", "/blaxel/import-only"),
    ]:
        absolute = sandbox.fs.ls(path)
        assert (absolute.name, absolute.path) == ("import-only", display)
        probe = next(entry for entry in absolute.files if entry.name == "probe.txt")
        assert probe.path == "/blaxel/import-only/probe.txt"
    root = sandbox.fs.ls("/")
    assert (root.name, root.path) == ("/", "/")
    odd_names = ["line" + chr(10) + "break", "tab" + chr(9) + "file", "-leading", ".hidden"]
    import shlex
    guest_code = (
        "from pathlib import Path; root = Path('/blaxel/import-only'); "
        "[(root / name).write_text('literal-name') for name in " + repr(odd_names) + "]"
    )
    created = sandbox.process.exec(ProcessRequestWithLog(
        command="python3 -c " + shlex.quote(guest_code), wait_for_completion=True,
    ))
    assert created.exit_code == 0, created.stderr
    listing = sandbox.fs.ls("/blaxel/import-only")
    actual_names = {entry.name for entry in listing.files}
    expected_names = {"probe.txt", *odd_names}
    assert actual_names == expected_names, (sorted(actual_names), sorted(expected_names))
    payload = bytes(range(256))
    binary_path = "/blaxel/import-only/binary"
    uploaded = sandbox.fs.write_binary(binary_path, payload)
    assert uploaded.to_dict() == {"message": "Binary file uploaded successfully", "path": binary_path}
    assert sandbox.fs.read_binary(binary_path) == payload
    sandbox.fs.write_binary(binary_path, bytearray(payload))
    assert sandbox.fs.read_binary(binary_path) == payload
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as local_directory:
        local_source = Path(local_directory) / "input"
        local_source.write_bytes(payload)
        sandbox.fs.write_binary(binary_path, str(local_source))
        local_destination = Path(local_directory) / "output"
        sandbox.fs.download(binary_path, str(local_destination), mode=0o600)
        assert local_destination.read_bytes() == payload
        assert local_destination.stat().st_mode & 0o777 == 0o600
    sandbox.fs.write_binary(binary_path, b"")
    assert sandbox.fs.read_binary(binary_path) == b""
    try:
        sandbox.fs.rm("/blaxel/import-only")
    except Exception:
        pass
    else:
        raise AssertionError("nonrecursive deletion removed a nonempty directory")
    assert sandbox.fs.read("/blaxel/import-only/probe.txt") == "second"
    deleted = sandbox.fs.rm(binary_path)
    assert deleted.to_dict() == {"message": "File deleted successfully", "path": binary_path}
    reconnected = SyncSandboxInstance.get(sandbox_name)
    assert reconnected.fs.read("/blaxel/import-only/probe.txt") == "second"
    import time
    unattended_path = "/blaxel/import-only/unattended"
    unattended = sandbox.process.exec(ProcessRequestWithLog(
        command=f"sleep 2; printf done > {unattended_path}", name="unattended", wait_for_completion=False,
    ))
    assert unattended.status == "running"
    time.sleep(8)
    assert sandbox.fs.read(unattended_path) == "done"
    started = sandbox.process.exec(ProcessRequestWithLog(
        command="cat; printf stdin-error >&2", name="stdin-probe", stdin=True, wait_for_completion=False,
    ))
    assert started.status == "running", started.status
    assert reconnected.process.get("stdin-probe").pid == started.pid
    reconnected.process.write_stdin("stdin-probe", "hello λ\\n")
    reconnected.process.close_stdin("stdin-probe")
    finished = reconnected.process.wait("stdin-probe", max_wait=10000, interval=100)
    assert finished.status == "completed", finished.status
    assert finished.stdout == "hello λ\\n", finished.stdout
    assert finished.stderr == "stdin-error", finished.stderr
    assert reconnected.process.logs("stdin-probe", "stdout") == "hello λ\\n"
    assert any(item.name == "stdin-probe" for item in reconnected.process.list())
    sandbox.process.exec(ProcessRequestWithLog(command="sleep 30", name="kill-probe", wait_for_completion=False))
    timed_out = False
    try:
        reconnected.process.wait("kill-probe", max_wait=1, interval=1)
    except Exception as error:
        assert "tim" in str(error).lower(), str(error)
        timed_out = True
    assert timed_out
    assert reconnected.process.get("kill-probe").status == "running"
    reconnected.process.kill("kill-probe")
    assert reconnected.process.wait("kill-probe", max_wait=10000, interval=100).status == "killed"
finally:
    SyncSandboxInstance.delete(sandbox_name)
"""


def _async_program(source: str) -> str:
    class AwaitSandboxCalls(ast.NodeTransformer):
        def visit_Call(self, node):
            node = self.generic_visit(node)
            target = node.func
            while isinstance(target, ast.Attribute):
                target = target.value
            if isinstance(target, ast.Name) and target.id in {"SandboxInstance", "sandbox", "reconnected"}:
                return ast.copy_location(ast.Await(value=node), node)
            return node

    tree = AwaitSandboxCalls().visit(ast.parse(source.replace("SyncSandboxInstance", "SandboxInstance")))
    return "async def program():\n" + "\n".join("    " + line for line in ast.unparse(tree).splitlines())


def _trace_program(source: str) -> str:
    class TraceCalls(ast.NodeTransformer):
        def visit_Call(self, node):
            node = self.generic_visit(node)
            target = node.func
            parts = []
            while isinstance(target, ast.Attribute):
                parts.append(target.attr)
                target = target.value
            if isinstance(target, ast.Name) and target.id in {
                "SandboxInstance",
                "SyncSandboxInstance",
                "sandbox",
                "reconnected",
            }:
                api = ".".join(["SandboxInstance", *reversed(parts)])
                return ast.copy_location(
                    ast.Call(
                        func=ast.Name(id="__compat_trace__", ctx=ast.Load()),
                        args=[ast.Constant(value=api), node],
                        keywords=[],
                    ),
                    node,
                )
            return node

    return ast.unparse(ast.fix_missing_locations(TraceCalls().visit(ast.parse(source))))


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("provider,shim", [("blaxel", False), ("blaxel", True), ("arker", True)])
def test_import_only(provider, shim, asynchronous, tmp_path, monkeypatch) -> None:
    pytest.importorskip("blaxel")
    if not os.environ.get("BL_API_KEY") or (provider == "arker" and not os.environ.get("ARKER_API_KEY")):
        pytest.skip("live Blaxel and Arker credentials are required")
    config = tmp_path / "router.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "priority", "order": [provider]},
                "create": {"strategy": "priority", "order": [provider]},
            }
        )
    )
    monkeypatch.setenv("COMPUTESDK_ROUTER_CONFIG", str(config))
    source = PROGRAM
    if shim:
        source = source.replace(
            "from blaxel.core.sandbox import SyncSandboxInstance",
            "from arker.blaxel import SyncSandboxInstance",
        )
    namespace = {"sandbox_name": f"arker-import-{uuid.uuid4().hex[:12]}"}
    called = set()

    def trace(api, result):
        called.add(api)
        return result

    namespace["__compat_trace__"] = trace
    source = _trace_program(source)
    if asynchronous:
        source = _async_program(source)
    outcome = "failed"
    try:
        exec(compile(source, "blaxel_import_only.py", "exec"), namespace)
        if asynchronous:
            asyncio.run(namespace["program"]())
        outcome = "passed"
    finally:
        record(
            "blaxel",
            "async" if asynchronous else "sync",
            "reference" if not shim else provider if provider == "arker" else "native",
            "import-only-files-and-command-isolation",
            sorted(called),
            outcome,
        )
