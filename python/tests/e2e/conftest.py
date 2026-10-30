from __future__ import annotations

import json

import pytest


@pytest.fixture
def live_router(provider, tmp_path, monkeypatch):
    path = tmp_path / "router.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "priority", "order": [provider]},
                "create": {"strategy": "priority", "order": [provider]},
            }
        )
    )
    monkeypatch.setenv("COMPUTESDK_ROUTER_CONFIG", str(path))
