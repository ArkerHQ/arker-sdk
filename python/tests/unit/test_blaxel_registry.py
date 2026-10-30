import asyncio
import base64
import copy
import json
from types import SimpleNamespace

import pytest

from arker import _compat as compat
from arker import _compat_blaxel_create as creation
from arker import blaxel


def docker_config(registry="registry.example"):
    return {"auths": {registry: {"auth": base64.b64encode(b"user:password:with:colon").decode()}}}


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("encoded", [False, True])
def test_docker_credentials_only_reach_matching_image_pull(monkeypatch, asynchronous, encoded):
    from arker import _compat_blaxel_data_plane as plane

    calls = []
    config = {"source": "source"}
    document = docker_config()
    original = copy.deepcopy(document)
    monkeypatch.setattr(blaxel, "routed_create", lambda _, config, handlers: ("arker", handlers["arker"]()))
    monkeypatch.setattr(compat, "arker_client", lambda config: None)
    vm = SimpleNamespace(id="vm-test", name="named", metadata={})
    monkeypatch.setattr(creation, "fork_named", lambda _, request, **kwargs: calls.append(request) or vm)
    monkeypatch.setattr(plane, "validate_native", lambda: None)
    monkeypatch.setattr(blaxel.SyncSandboxInstance, "_plane", lambda self: None)

    async def wrap(cls, box):
        return box

    monkeypatch.setattr(blaxel.SandboxInstance, "_wrap", classmethod(wrap))
    request = {
        "image": "registry.example/team/image:tag",
        "docker_config": json.dumps(document) if encoded else document,
    }
    if asynchronous:
        asyncio.run(blaxel.SandboxInstance.create(request, arker=config))
    else:
        blaxel.SyncSandboxInstance.create(request, arker=config)
    assert calls[0]["registry_auth"] == {"username": "user", "password": "password:with:colon"}
    assert "password" not in json.dumps({key: value for key, value in calls[0].items() if key != "registry_auth"})
    assert config == {"source": "source"} and document == original


@pytest.mark.parametrize(
    "value,image",
    [
        (docker_config(), "other.example/team/image"),
        (docker_config(), None),
        (docker_config(), "team/image"),
        (docker_config("https:"), "https://registry.example/image"),
        (docker_config("user@registry.example"), "user@registry.example/image"),
        ({"credsStore": "secret-helper"}, "registry.example/image"),
        ({"auths": {"registry.example": {"identitytoken": "secret-token"}}}, "registry.example/image"),
        ({"auths": {"registry.example": {"auth": "secret-invalid"}}}, "registry.example/image"),
        ("secret-invalid-json", "registry.example/image"),
    ],
)
def test_unrepresentable_credentials_fail_before_mutation(monkeypatch, value, image):
    monkeypatch.setattr(blaxel, "routed_create", lambda _, config, handlers: handlers["arker"]())
    monkeypatch.setattr(blaxel, "arker_create", lambda *a, **kw: pytest.fail("unexpected mutation"))
    with pytest.raises(compat.UnsupportedCompatibilityOption) as error:
        blaxel.SyncSandboxInstance.create({"image": image, "docker_config": value})
    assert "secret" not in str(error.value)


def test_configured_registry_credentials_are_not_overwritten():
    with pytest.raises(compat.UnsupportedCompatibilityOption, match="conflicts"):
        creation.registry_configuration(
            docker_config(), "registry.example/image", {"registry_auth": {"username": "owner"}}
        )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("encoded", [False, True])
def test_pinned_native_docker_configuration_wire_passthrough(monkeypatch, asynchronous, encoded):
    import importlib

    native = pytest.importorskip("blaxel.core.sandbox")
    target = native.SandboxInstance if asynchronous else native.SyncSandboxInstance
    module = importlib.import_module(target.__module__)
    captured = []

    class Observed(Exception):
        pass

    def capture(**kwargs):
        captured.append(kwargs["body"].to_dict())
        raise Observed()

    async def async_capture(**kwargs):
        capture(**kwargs)

    monkeypatch.setattr(module, "create_sandbox", async_capture if asynchronous else capture)
    monkeypatch.setattr(blaxel, "routed_create", lambda vendor, config, handlers: (vendor, handlers[vendor]()))
    document = docker_config()
    request = {
        "name": "named",
        "region": "us-pdx-1",
        "image": "registry.example/team/image",
        "docker_config": json.dumps(document) if encoded else document,
    }
    for cls in (target, blaxel.SandboxInstance if asynchronous else blaxel.SyncSandboxInstance):
        with pytest.raises(Observed):
            if asynchronous:
                asyncio.run(cls.create(request))
            else:
                cls.create(request)
    assert captured[0] == captured[1]
    assert json.loads(captured[0]["spec"]["runtime"]["dockerConfig"]) == document
