"""The same private HTTP program on native Blaxel and both shim routes."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest
from compat_evidence import record
from test_blaxel_import_only import _async_program, _trace_program

SERVER = Path(__file__).resolve().parents[3].joinpath("compatibility/http-service.py").read_text()

PROGRAM = """
from blaxel.core.sandbox import SyncSandboxInstance, ProcessRequestWithLog
import time
sandbox = SyncSandboxInstance.create({"name": sandbox_name})
try:
    __compat_configure__(sandbox)
    sandbox.fs.write("/tmp/compat-fetch-service.py", server_source)
    sandbox.process.exec(ProcessRequestWithLog(
        command="python3 /tmp/compat-fetch-service.py", name="fetch-service", wait_for_completion=False,
    ))
    deadline = time.monotonic() + 30
    while "fetch-service-ready" not in sandbox.process.get("fetch-service").stdout:
        assert time.monotonic() < deadline, "HTTP service did not become ready"
        time.sleep(0.1)
    body = bytes(range(256)) * 1024
    response = sandbox.fetch(18082, "echo?x=1", method="POST", content=body, headers={
        "Authorization": "Bearer application-only", "x-api-key": "application-key",
    })
    assert response.status_code == 207
    assert response.content == body
    assert response.headers["x-echo-authorization"] == "Bearer application-only"
    assert response.headers["x-echo-key"] == "application-key"
    chunked = sandbox.network.fetch(18082, "/redirect", follow_redirects=True)
    assert chunked.status_code == 418, (chunked.status_code, chunked.url.path)
    assert chunked.content == b"eof\\x00\\xff\\x01"
    assert len(chunked.history) == 1
    sandbox.network.fetch(18082, "/cookies/set")
    cookie = sandbox.network.fetch(18082, "/cookies/check")
    assert cookie.text == ("compat-session=kept" if asynchronous else "")
    assert sandbox.network.fetch(18083, "/cookies/check").text == ""
finally:
    sandbox.delete()
"""


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("provider,shim", [("blaxel", False), ("blaxel", True), ("arker", True)])
def test_fetch_import_only(provider, shim, asynchronous, tmp_path, monkeypatch):
    pytest.importorskip("blaxel")
    if not os.environ.get("BL_API_KEY") or (provider == "arker" and not os.environ.get("ARKER_API_KEY")):
        pytest.skip("live credentials required")
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
    source = (
        PROGRAM.replace(
            "from blaxel.core.sandbox import SyncSandboxInstance, ProcessRequestWithLog",
            "from arker.blaxel import SyncSandboxInstance\nfrom blaxel.core.sandbox import ProcessRequestWithLog",
        )
        if shim
        else PROGRAM
    )
    called = set()

    def trace(api, result):
        called.add(api)
        return result

    def configure(sandbox):
        if provider == "arker":
            sync = getattr(sandbox, "_sandbox", sandbox)
            sync._vm.set_policies(
                {
                    "policies": [
                        {"type": "inbound", "action": "deny"},
                        {"type": "outbound", "action": "deny"},
                    ]
                }
            )

    namespace = {
        "asynchronous": asynchronous,
        "sandbox_name": f"arker-fetch-{uuid.uuid4().hex[:12]}",
        "server_source": SERVER,
        "__compat_trace__": trace,
        "__compat_configure__": configure,
    }
    source = _trace_program(source)
    if asynchronous:
        source = _async_program(source)
    outcome = "failed"
    try:
        exec(compile(source, "blaxel_fetch_import_only.py", "exec"), namespace)
        if asynchronous:
            asyncio.run(namespace["program"]())
        outcome = "passed"
    finally:
        record(
            "blaxel",
            "async" if asynchronous else "sync",
            "reference" if not shim else "arker" if provider == "arker" else "native",
            "import-only-private-fetch",
            sorted(called),
            outcome,
        )
