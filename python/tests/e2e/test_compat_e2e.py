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
    finally:
        sandbox.delete()


def test_e2b_live() -> None:
    sandbox = E2BSandbox.create()
    try:
        result = sandbox.commands.run("printf e2b-python-live")
        assert result.stdout == "e2b-python-live"
    finally:
        sandbox.kill()


def test_modal_live() -> None:
    sandbox = ModalSandbox.create()
    try:
        process = sandbox.exec("printf", "modal-python-live")
        assert process.stdout.read() == "modal-python-live"
    finally:
        sandbox.terminate()


def test_blaxel_live() -> None:
    sandbox = BlaxelSandbox.create()
    try:
        result = sandbox.process.exec("printf blaxel-python-live")
        assert result.stdout == "blaxel-python-live"
        sandbox.fs.write("/tmp/arker-blaxel-python-live.txt", "ok")
        assert sandbox.fs.read("/tmp/arker-blaxel-python-live.txt") == "ok"
    finally:
        sandbox.delete()
