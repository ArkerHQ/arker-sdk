import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("daytona")

import daytona as native
from daytona.common.errors import DaytonaAuthenticationError, DaytonaForbiddenError

from arker import _daytona_volumes as volumes
from arker import daytona


@pytest.fixture(autouse=True)
def no_native_credentials(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in ("DAYTONA_API_KEY", "DAYTONA_JWT_TOKEN", "DAYTONA_ORGANIZATION_ID", "COMPUTESDK_ROUTER_CONFIG"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ARKER_API_KEY", "arker-synthetic")


@pytest.fixture
def arker_volume(monkeypatch):
    row = SimpleNamespace(
        filesystem_id="dir_owned", name="shared", owner_org_id="org", created_at="2026-01-01T00:00:00Z"
    )
    client = SimpleNamespace(
        list_filesystems=lambda **kw: SimpleNamespace(filesystems=[row], next_cursor=None),
        get_filesystem=lambda identifier: row,
    )
    monkeypatch.setattr(volumes, "arker_client", lambda config: client)
    return row


@pytest.mark.parametrize("asynchronous", [False, True])
def test_one_arker_key_get_and_list_do_not_construct_unauthenticated_native(arker_volume, asynchronous):
    with pytest.raises(DaytonaAuthenticationError):
        native.Daytona()

    async def run():
        owner = daytona.AsyncDaytona() if asynchronous else daytona.Daytona()
        if asynchronous:
            found = await owner.volume.get("shared")
            listed = await owner.volume.list()
            assert owner._native is None
        else:
            found = owner.volume.get("shared")
            listed = owner.volume.list()
            assert owner._native is None
        assert found.id == arker_volume.filesystem_id
        assert [v.id for v in listed] == [found.id]

    asyncio.run(run())


def test_explicit_uncredentialed_native_provider_is_rejected(arker_volume, monkeypatch, tmp_path):
    import json

    path = tmp_path / "router.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "priority", "order": ["arker", "daytona"]},
                "create": {"strategy": "priority", "order": ["arker"]},
            }
        )
    )
    monkeypatch.setenv("COMPUTESDK_ROUTER_CONFIG", str(path))
    with pytest.raises(ValueError, match='unknown provider "daytona"'):
        daytona.Daytona().volume.get("shared")


@pytest.mark.parametrize("source", ["config-key", "config-jwt", "env-key", "env-jwt", "dotenv", "dotenv-local"])
def test_native_auth_sources_remain_eligible(source, monkeypatch, tmp_path):
    config = None
    if source == "config-key":
        config = native.DaytonaConfig(api_key="synthetic")
    elif source == "config-jwt":
        config = native.DaytonaConfig(jwt_token="synthetic", organization_id="org")
    elif source == "env-key":
        monkeypatch.setenv("DAYTONA_API_KEY", "synthetic")
    elif source == "env-jwt":
        monkeypatch.setenv("DAYTONA_JWT_TOKEN", "synthetic")
    else:
        (tmp_path / (".env" if source == "dotenv" else ".env.local")).write_text("DAYTONA_API_KEY=synthetic\n")
    assert daytona.Daytona(config)._native_volume_available()


@pytest.mark.parametrize("error_type", [DaytonaAuthenticationError, DaytonaForbiddenError])
def test_configured_native_auth_errors_are_not_absence(arker_volume, monkeypatch, error_type):
    monkeypatch.setenv("DAYTONA_API_KEY", "invalid-synthetic")
    error = error_type("refused", status_code=401 if error_type is DaytonaAuthenticationError else 403)

    def fail(*args):
        raise error

    monkeypatch.setattr(
        daytona.Daytona, "_native_client", lambda self: SimpleNamespace(volume=SimpleNamespace(get=fail, list=fail))
    )
    with pytest.raises(error_type) as caught:
        daytona.Daytona().volume.get("shared")
    assert caught.value is error
    with pytest.raises(error_type):
        daytona.Daytona().volume.list()


def test_partial_jwt_is_not_silently_ignored(arker_volume, monkeypatch):
    monkeypatch.setenv("DAYTONA_JWT_TOKEN", "synthetic")
    with pytest.raises(DaytonaAuthenticationError, match="ORGANIZATION_ID"):
        daytona.Daytona().volume.get("shared")
