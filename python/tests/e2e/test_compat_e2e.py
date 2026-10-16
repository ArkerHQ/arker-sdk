"""Live compatibility shim smoke tests.

Run with:

    ARKER_API_KEY=... python -m pytest tests/e2e/test_compat_e2e.py
"""

from __future__ import annotations

import os

import pytest

from arker.blaxel import SandboxInstance as BlaxelSandbox
from arker.daytona import Daytona
from arker.e2b import Sandbox as E2BSandbox
from arker.modal import Sandbox as ModalSandbox

if not os.environ.get("ARKER_API_KEY"):
    pytest.skip("live Arker credentials are not configured", allow_module_level=True)

if not os.environ.get("ARKER_BASE_URL"):
    os.environ.setdefault("ARKER_PROVIDER", "aws")
    os.environ.setdefault("ARKER_REGION", "us-east-1")


def test_daytona_live() -> None:
    daytona = Daytona()
    sandbox = daytona.create()
    try:
        response = sandbox.process.exec("printf daytona-python-live")
        assert response.result == "daytona-python-live"
        assert sandbox.process.exec("pwd").result.strip() == "/home/daytona"
    finally:
        sandbox.delete()


def test_e2b_live() -> None:
    sandbox = E2BSandbox.create()
    try:
        result = sandbox.commands.run("printf e2b-python-live")
        assert result.stdout == "e2b-python-live"
        assert sandbox.commands.run("pwd").stdout.strip() == "/home/user"
    finally:
        sandbox.kill()


def test_modal_live() -> None:
    sandbox = ModalSandbox.create()
    try:
        process = sandbox.exec("printf", "modal-python-live")
        assert process.stdout.read() == "modal-python-live"
        assert sandbox.exec("pwd").stdout.read().strip() == "/"
    finally:
        sandbox.terminate()


def test_blaxel_live() -> None:
    sandbox = BlaxelSandbox.create()
    try:
        result = sandbox.process.exec("printf blaxel-python-live")
        assert result.stdout == "blaxel-python-live"
        assert sandbox.process.exec("pwd").stdout.strip() == "/blaxel"
        sandbox.fs.write("/tmp/arker-blaxel-python-live.txt", "ok")
        assert sandbox.fs.read("/tmp/arker-blaxel-python-live.txt") == "ok"
    finally:
        sandbox.delete()


@pytest.mark.parametrize(
    ("create", "run", "destroy"),
    [
        (
            lambda: E2BSandbox.create(allow_internet_access=False),
            lambda sandbox, command: sandbox.commands.run(command),
            lambda sandbox: sandbox.kill(),
        ),
        (
            lambda: Daytona().create(network_block_all=True),
            lambda sandbox, command: sandbox.process.exec(command),
            lambda sandbox: sandbox.delete(),
        ),
        (
            lambda: ModalSandbox.create(block_network=True),
            lambda sandbox, command: sandbox.exec("sh", "-lc", command),
            lambda sandbox: sandbox.terminate(),
        ),
    ],
)
def test_outbound_block_policy_live(create, run, destroy) -> None:
    sandbox = create()
    try:
        result = run(sandbox, "curl -fsS --max-time 5 https://example.com >/dev/null")
        exit_code = result.exit_code if hasattr(result, "exit_code") else result.wait()
        assert exit_code != 0
    finally:
        destroy(sandbox)
