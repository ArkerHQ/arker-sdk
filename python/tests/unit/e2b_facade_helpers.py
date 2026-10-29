"""Explicit component seam for tests confined to control-plane routing."""

import base64
from types import SimpleNamespace

import pytest

from arker import _compat_e2b_bootstrap as bootstrap
from arker import _compat_e2b_data_plane as data_plane


@pytest.fixture
def mock_envd_bootstrap(monkeypatch):
    calls = []

    def prepare(vm, **options):
        calls.append(vm.id)
        if not hasattr(vm, "_client"):
            vm._client = SimpleNamespace(_api_key="synthetic")
        elif not hasattr(vm._client, "_api_key"):
            vm._client._api_key = "synthetic"
        return data_plane.EnvdDescriptor("https://envd.invalid", "0.10.2", "synthetic-guest"), {"ready": True}

    async def prepare_async(vm, **options):
        return prepare(vm, **options)

    def components(*args, **options):
        async def aclose():
            pass

        return SimpleNamespace(
            commands=object(), files=object(), git=object(), pty=object(), close=lambda: None, aclose=aclose
        )

    monkeypatch.setattr(bootstrap, "bootstrap_envd", prepare)
    monkeypatch.setattr(bootstrap, "bootstrap_envd_async", prepare_async)
    monkeypatch.setattr(data_plane, "create_data_plane", components)
    return calls


def delegation_response(**updates):
    return {
        "redemption_url": "https://regional.example.test/api/v1/vms/canonical/file-transfer",
        "context": "b3BhcXVl",
        "signing_key": base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("="),
        "operations": ["read", "write"],
        "expires_at": None,
        **updates,
    }
