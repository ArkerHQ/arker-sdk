import asyncio
from types import SimpleNamespace

import pytest
from async_lifecycle_helpers import configure_router as configure_router

from arker import daytona
from arker._compat import UnsupportedCompatibilityOption


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("volumes", [None, []])
def test_empty_mount_configuration_creates_without_mount_operations(
    configure_router, monkeypatch, asynchronous, volumes
):
    configure_router(["arker"])
    calls = []
    monkeypatch.setattr(daytona, "arker_create", lambda *a, **kw: calls.append(kw) or SimpleNamespace(id="owned"))
    if asynchronous:
        result = asyncio.run(daytona.AsyncDaytona().create(volumes=volumes))
    else:
        result = daytona.Daytona().create(volumes=volumes)
    assert result.id == "owned"
    assert len(calls) == 1 and "volumes" not in calls[0]


@pytest.mark.parametrize("volumes", [[{"volume_id": "native-id", "mount_path": "/data"}], {}, ()])
def test_nonempty_or_invalid_mount_configuration_refused_before_create(configure_router, monkeypatch, volumes):
    configure_router(["arker"])
    monkeypatch.setattr(daytona, "arker_create", lambda *a, **kw: pytest.fail("must reject before creating"))
    with pytest.raises(UnsupportedCompatibilityOption, match="volumes"):
        daytona.Daytona().create(volumes=volumes)
