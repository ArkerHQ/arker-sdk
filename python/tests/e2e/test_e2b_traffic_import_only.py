"""Compare public defaults and private application tokens without logging credentials."""

import asyncio
import os

import pytest
from compat_evidence import record_scenario
from compat_program import async_program

PROGRAM = r"""
import httpx
import shlex
from e2b import Sandbox

sandbox = Sandbox.create(network={"allow_public_traffic": False}) if private else Sandbox.create()
try:
    token = sandbox.traffic_access_token
    assert isinstance(token, str) and token if private else token is None
    for phase in ("created", "reconnect", "full", "filesystem"):
        if phase in ("full", "filesystem"):
            assert sandbox.pause(mode=phase)
        if phase != "created":
            sandbox = Sandbox.connect(sandbox.sandbox_id)
            assert sandbox.traffic_access_token == token
        if phase in ("created", "filesystem"):
            sandbox.commands.run(
                "python3 -m http.server 8080 --bind 0.0.0.0 >/tmp/traffic-server.log 2>&1",
                background=True, timeout=0,
            )
            readiness = "".join([
                "import socket,time; deadline=time.monotonic()+20\n",
                "while True:\n try:\n",
                "  socket.create_connection(('127.0.0.1',8080),timeout=1).close(); break\n",
                " except OSError:\n  assert time.monotonic()<deadline; time.sleep(.05)\n",
            ])
            sandbox.commands.run("python3 -c " + shlex.quote(readiness), timeout=25)
        get_host = sandbox.get_host
        url = "https://" + get_host(8080) + "/"
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            response = client.get(url)
            assert response.status_code == (403 if private else 200)
            if private:
                assert client.get(url, headers={"e2b-traffic-access-token": "incorrect"}).status_code == 403
                assert client.get(url, headers={"E2B-Traffic-Access-Token": token}).status_code == 200
                assert client.get(url, headers={"X-Access-Token": token}).status_code == 403
        info = sandbox.get_info()
        assert info.network is not None and info.network["allow_public_traffic"] is not private
finally:
    sandbox.kill()
"""
ASYNC_PROGRAM = async_program(PROGRAM, rename={"Sandbox": "AsyncSandbox"}, targets={"AsyncSandbox", "sandbox"})


@pytest.mark.parametrize("private", [False, True], ids=["public", "private"])
@pytest.mark.parametrize("style", ["sync", "async"])
@pytest.mark.parametrize(
    "provider,shim", [("e2b", False), ("e2b", True), ("arker", True)], ids=["reference", "native", "arker"]
)
def test_application_traffic(private, style, provider, shim, live_router, monkeypatch):
    for key in ("E2B_API_KEY", "ARKER_API_KEY", "COMPAT_COVERAGE_RUN", "COMPAT_COVERAGE_EVIDENCE"):
        assert os.environ.get(key), f"Missing live prerequisite: {key}"
    if provider == "arker":
        monkeypatch.delenv("E2B_API_KEY")
    apis = [
        "Sandbox." + name
        for name in (
            "create",
            "connect",
            "pause",
            "kill",
            "commands.run",
            "get_host",
            "get_info",
            "traffic_access_token",
        )
    ]
    source = ASYNC_PROGRAM if style == "async" else PROGRAM
    if shim:
        source = source.replace("from e2b import Sandbox\n", "from arker.e2b import Sandbox\n")
        source = source.replace("from e2b import AsyncSandbox\n", "from arker.e2b import AsyncSandbox\n")
    with record_scenario(
        "e2b", style, provider, shim, "application-traffic-" + ("private" if private else "public"), apis
    ):
        namespace = {"private": private}
        exec(compile(source, __file__, "exec"), namespace)
        if style == "async":
            asyncio.run(namespace["program"]())
