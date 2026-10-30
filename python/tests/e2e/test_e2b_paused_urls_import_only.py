"""Signed file URLs must not resume an explicitly paused sandbox."""

import asyncio
import os

import pytest
from compat_evidence import record_scenario

PROGRAM = r"""
from e2b import Sandbox
import inspect
import uuid
import httpx

async def result(value):
    return await value if inspect.isawaitable(value) else value

async def program():
    sandbox = await result(Sandbox.create())
    try:
        prefix = '/home/user/paused-url-' + uuid.uuid4().hex
        original = b'preserved before explicit pause'
        uploaded = b'written only after explicit resume'
        await result(sandbox.files.write(prefix + '-read', original))
        download = sandbox.download_url(prefix + '-read')
        upload = sandbox.upload_url(prefix + '-write')
        assert isinstance(download, str) and isinstance(upload, str)
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            before = await client.get(download)
            assert before.status_code == 200 and before.content == original
            assert await result(sandbox.pause(mode=mode)) is True
            for operation in ('read', 'write'):
                response = (
                    await client.get(download) if operation == 'read'
                    else await client.post(upload, files={'file': ('ignored', uploaded)})
                )
                assert response.status_code == 502, response.status_code
                assert response.headers['content-type'].split(';', 1)[0] == 'application/json'
                assert response.json() == {
                    'sandboxId': sandbox.sandbox_id,
                    'message': 'The sandbox was not found',
                    'code': 502,
                }
                assert await result(sandbox.is_running()) is False
                info = await result(sandbox.get_info())
                assert str(info.state) == 'paused'
            sandbox = await result(Sandbox.connect(sandbox.sandbox_id))
            assert not await result(sandbox.files.exists(prefix + '-write'))
            after = await client.get(download)
            assert after.status_code == 200 and after.content == original
            response = await client.post(upload, files={'file': ('ignored', uploaded)})
            assert response.status_code == 200, response.status_code
            assert bytes(await result(sandbox.files.read(prefix + '-write', format='bytes'))) == uploaded
    finally:
        await result(sandbox.kill())
"""


@pytest.mark.parametrize("mode", ["full", "filesystem"])
@pytest.mark.parametrize("style", ["sync", "async"])
@pytest.mark.parametrize(
    "provider,shim", [("e2b", False), ("e2b", True), ("arker", True)], ids=["reference", "native", "arker"]
)
def test_paused_file_urls(mode, style, provider, shim, live_router, monkeypatch):
    for name in ("E2B_API_KEY", "ARKER_API_KEY", "COMPAT_COVERAGE_RUN", "COMPAT_COVERAGE_EVIDENCE"):
        assert os.environ.get(name), f"Missing live prerequisite: {name}"
    if provider == "arker":
        monkeypatch.delenv("E2B_API_KEY")
    source = PROGRAM
    if style == "async":
        source = source.replace("from e2b import Sandbox", "from e2b import AsyncSandbox as Sandbox")
    if shim:
        source = source.replace("from e2b import", "from arker.e2b import")
    apis = [
        "Sandbox." + name
        for name in (
            "create",
            "files.write",
            "files.read",
            "files.exists",
            "download_url",
            "upload_url",
            "pause",
            "is_running",
            "get_info",
            "connect",
            "kill",
        )
    ]
    with record_scenario("e2b", style, provider, shim, "paused-file-urls-" + mode, apis):
        namespace = {"mode": mode}
        exec(compile(source, __file__, "exec"), namespace)
        asyncio.run(namespace["program"]())
