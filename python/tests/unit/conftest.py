from __future__ import annotations

import pytest
from http_fixtures import http_server as http_server

ARKER_ENV_VARS = (
    "ARKER_API_KEY",
    "ARKER_BASE_URL",
    "ARKER_CONTROL_BASE_URL",
    "ARKER_PROVIDER",
    "ARKER_REGION",
    "ARKER_SOURCE_VM_ID",
)


@pytest.fixture(autouse=True)
def _clean_arker_env(monkeypatch):
    """Unit tests exercise env resolution itself; ambient ARKER_* must not leak in."""
    for name in ARKER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
