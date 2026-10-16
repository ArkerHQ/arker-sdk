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
    sandbox.fs.mkdir("/blaxel/import-only")
    sandbox.fs.write("/blaxel/import-only/probe.txt", "first")
    sandbox.fs.write("/blaxel/import-only/probe.txt", "second")
    assert sandbox.fs.read("/blaxel/import-only/probe.txt") == "second"
    listing = sandbox.fs.ls("/blaxel/import-only")
    assert any(entry.name == "probe.txt" for entry in listing.files)
    reconnected = SyncSandboxInstance.get(sandbox_name)
    assert reconnected.fs.read("/blaxel/import-only/probe.txt") == "second"
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
