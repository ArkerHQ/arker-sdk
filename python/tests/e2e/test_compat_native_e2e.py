from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from arker.blaxel import SyncSandboxInstance
from arker.daytona import Daytona
from arker.e2b import Sandbox as E2BSandbox
from arker.modal import Sandbox as ModalSandbox

pytest.importorskip("e2b")
native_modal = pytest.importorskip("modal")
pytest.importorskip("daytona")
pytest.importorskip("blaxel")


def _router(provider: str, directory: Path) -> str:
    path = directory / f"{provider}.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "priority", "order": [provider]},
                "create": {"strategy": "priority", "order": [provider]},
            }
        )
    )
    return str(path)


@pytest.mark.skipif(
    not all(
        os.environ.get(name)
        for name in ("E2B_API_KEY", "DAYTONA_API_KEY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "BL_API_KEY")
    ),
    reason="native provider credentials are required",
)
def test_native_provider_surfaces(tmp_path: Path) -> None:
    e2b = E2BSandbox.create(router_config_path=_router("e2b", tmp_path))
    try:
        result = e2b.commands.run("pwd && printf python-e2b")
        assert result.exit_code == 0
        assert "/home/user" in result.stdout
        assert "python-e2b" in result.stdout
    finally:
        e2b.kill()

    daytona_client = Daytona(router_config_path=_router("daytona", tmp_path))
    daytona = daytona_client.create()
    try:
        result = daytona.process.exec("pwd && printf python-daytona")
        assert result.exit_code == 0
        assert "/home/daytona" in result.result
        assert "python-daytona" in result.result
    finally:
        daytona.delete()

    modal = ModalSandbox.create(
        app=native_modal.App.lookup("arker-python-shim-live", create_if_missing=True),
        router_config_path=_router("modal", tmp_path),
    )
    try:
        process = modal.exec("sh", "-lc", "pwd && printf python-modal")
        output = process.stdout.read()
        assert process.wait() == 0
        assert output.startswith("/\n")
        assert "python-modal" in output
    finally:
        modal.terminate()

    blaxel = SyncSandboxInstance.create_if_not_exists(
        {"name": f"arker-shim-{uuid.uuid4().hex[:12]}", "region": "us-pdx-1"},
        timeout=50,
        router_config_path=_router("blaxel", tmp_path),
    )
    try:
        result = blaxel.process.exec("pwd && printf python-blaxel")
        assert result.exit_code == 0
        assert "/blaxel" in result.stdout
        assert "python-blaxel" in result.stdout
        blaxel.fs.mkdir("/blaxel/arker-shim")
        blaxel.fs.write("/blaxel/arker-shim/probe.txt", "persistent")
        assert blaxel.fs.read("/blaxel/arker-shim/probe.txt") == "persistent"
        assert any(file.name == "probe.txt" for file in blaxel.fs.ls("/blaxel/arker-shim").files)
        assert callable(blaxel.fetch)
    finally:
        blaxel.delete()
