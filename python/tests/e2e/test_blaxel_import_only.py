"""Run the same Blaxel program with exactly its import changed."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import uuid

import pytest

PROGRAM = """
from blaxel.core.sandbox import SyncSandboxInstance
from blaxel.core.sandbox import ProcessRequestWithLog
from blaxel.core.sandbox.types import SandboxCreateConfiguration
from blaxel.core.sandbox.client.models import ProcessRequestEnv

sandbox = SyncSandboxInstance.create_if_not_exists(SandboxCreateConfiguration(name=sandbox_name))
try:
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
finally:
    sandbox.delete()
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
    if asynchronous:
        source = _async_program(source)
    exec(compile(source, "blaxel_import_only.py", "exec"), namespace)
    if asynchronous:
        asyncio.run(namespace["program"]())
