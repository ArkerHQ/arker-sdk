"""Run filesystem search assertions unchanged across all Blaxel routes."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from compat_evidence import record_scenario
from test_blaxel_import_only import _async_program, _trace_program

PROGRAM = """
from blaxel.core.sandbox import SyncSandboxInstance
sandbox = SyncSandboxInstance.create({"name": sandbox_name})
try:
    path = "/blaxel/search.txt"
    sandbox.fs.write(path, "before\\nA Needle here\\nafter\\nlast needle\\n")
    response = sandbox.fs.grep("needle", "/blaxel", context_lines=1, file_pattern="search.txt")
    assert response.to_dict() == {
        "query": "needle", "total": 2,
        "matches": [
            {"path": "search.txt", "line": 2, "column": 3, "text": "A Needle here",
             "context": "before\\nA Needle here\\nafter"},
            {"path": "search.txt", "line": 4, "column": 6, "text": "last needle",
             "context": "after\\nlast needle\\n"},
        ],
    }, response.to_dict()
    sensitive = sandbox.fs.grep("needle", "/blaxel", case_sensitive=True, file_pattern="search.txt")
    assert sensitive.total == 1
    assert "context" not in sensitive.matches[0].to_dict()
    sandbox.fs.write(path, "Needle\\n" * 105)
    assert sandbox.fs.grep("needle", "/blaxel", file_pattern="search.txt").total == 100
    assert sandbox.fs.grep("needle", "/blaxel", file_pattern="search.txt", max_results=3).total == 3
finally:
    sandbox.delete()
"""


@pytest.mark.parametrize("asynchronous", [True])
@pytest.mark.parametrize("provider,shim", [("blaxel", False), ("blaxel", True), ("arker", True)])
def test_grep_import_only(provider, shim, asynchronous, live_router, monkeypatch):
    assert os.environ.get("BL_API_KEY"), "BL_API_KEY is required for live parity tests"
    if provider == "arker":
        assert os.environ.get("ARKER_API_KEY"), "ARKER_API_KEY is required for the Arker route"
    source = PROGRAM.replace("from blaxel.core.sandbox import", "from arker.blaxel import") if shim else PROGRAM
    called = set()

    def trace(api, result):
        called.add(api)
        return result

    namespace = {"sandbox_name": f"grep-{uuid.uuid4().hex[:12]}", "__compat_trace__": trace}
    source = _trace_program(source)
    if asynchronous:
        source = _async_program(source)
    with record_scenario("blaxel", "async" if asynchronous else "sync", provider, shim, "import-only-grep", called):
        exec(compile(source, "blaxel_grep_import_only.py", "exec"), namespace)
        if asynchronous:
            asyncio.run(namespace["program"]())
