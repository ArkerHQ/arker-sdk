"""Run one async Daytona program with only its client import changed."""

import asyncio
import os

import pytest
from compat_evidence import record_scenario

PROGRAM = r"""
from daytona import AsyncDaytona
from daytona.common.process import ExecuteResponse

async def program():
    async with AsyncDaytona() as client:
        sandbox = await client.create()
        try:
            result = await sandbox.process.exec("pwd")
            assert result.exit_code == 0
            assert isinstance(result, ExecuteResponse)
            assert result.artifacts.stdout == result.result
            assert result.artifacts.charts == []
            assert result.result.strip() == "/home/daytona", repr(result.result)
            result = await sandbox.process.exec(
                'printf "%s:" "$VALUE"; pwd', cwd="/tmp", env={"VALUE": "async"})
            assert (result.exit_code, result.result) == (0, "async:/tmp\n")
            connected = await client.get(sandbox.id)
            assert connected.id == sandbox.id
            result = await connected.process.exec('printf "%s:" "${VALUE-unset}"; pwd')
            assert (result.exit_code, result.result) == (0, "unset:/home/daytona\n")
            result = await connected.process.exec("printf nonzero; exit 17")
            assert (result.exit_code, result.result) == (17, "nonzero")
        finally:
            await client.delete(sandbox)
"""


@pytest.mark.parametrize("provider,shim", [("daytona", False), ("daytona", True), ("arker", True)])
def test_async_daytona_import_only(provider, shim, live_router, monkeypatch):
    pytest.importorskip("daytona")
    if not os.environ.get("DAYTONA_API_KEY" if provider == "daytona" else "ARKER_API_KEY"):
        pytest.skip("Live credentials required")
    if provider == "arker":
        monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    source = (
        PROGRAM.replace("from daytona import AsyncDaytona", "from arker.daytona import AsyncDaytona")
        if shim
        else PROGRAM
    )
    namespace = {}
    exec(compile(source, "daytona_async_import_only.py", "exec"), namespace)
    with record_scenario(
        "daytona",
        "async",
        provider,
        shim,
        "import-only-async-create-get-exec-delete",
        [
            "Daytona.create",
            "Daytona.get",
            "Daytona.delete",
            "Daytona.close",
            "Daytona.__aenter__",
            "Daytona.__aexit__",
            "Sandbox.id",
            "Sandbox.process.exec",
        ],
    ):
        asyncio.run(namespace["program"]())
