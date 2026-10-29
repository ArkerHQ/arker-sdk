import asyncio
import copy
from types import SimpleNamespace

import pytest

import arker.e2b as shim
from arker._compat import UnsupportedCompatibilityOption


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "lifecycle",
    [{}, {"auto_resume": False}, {"on_timeout": "kill"}, {"on_timeout": {"action": "kill"}, "auto_resume": False}],
)
def test_arker_creation_keeps_native_credentials_out_of_requests(monkeypatch, asynchronous, lifecycle):
    from arker.computer import Arker

    monkeypatch.setenv("ARKER_API_KEY", "arker-only")
    monkeypatch.delenv("COMPUTESDK_ROUTER_CONFIG", raising=False)
    calls = []
    child = SimpleNamespace(id="child")

    def get_vm(client, source):
        assert client._api_key == "arker-only"
        assert client._e2b_request_timeout == 0
        calls.append(("get", source))
        return SimpleNamespace(policies=None)

    def fork(client, **request):
        assert client._api_key == "arker-only"
        assert client._e2b_request_timeout == 0
        calls.append(("fork", request))
        return child

    monkeypatch.setattr(Arker, "get_vm", get_vm)
    monkeypatch.setattr(Arker, "fork", fork)
    marker = RuntimeError("stop before bootstrap")

    def stop(*args, **kwargs):
        raise marker

    monkeypatch.setattr("arker._compat_e2b_bootstrap.bootstrap_envd", stop)
    child.delete = lambda: None
    options = {
        "api_key": "native-only",
        "secure": False,
        "validate_api_key": False,
        "debug": False,
        "lifecycle": lifecycle,
        "request_timeout": 0,
    }
    original = copy.deepcopy(options)
    with pytest.raises(RuntimeError) as caught:
        if asynchronous:
            asyncio.run(shim.AsyncSandbox.create(template="selected-template", **options))
        else:
            shim.Sandbox.create(template="selected-template", **options)
    assert caught.value is marker
    request = calls[-1][1]
    assert request["source_vm_name"] == "selected-template"
    assert request["policies"]["rules"]["computesdk-e2b-lifetime"] == {
        "type": "schedule",
        "after_created": 300,
        "action": "delete",
    }
    assert request["policies"]["rules"]["computesdk-e2b-envd"]["auth"] == "arker"
    assert "native-only" not in repr(calls)
    assert options == original


@pytest.mark.parametrize(
    "options",
    [
        {"debug": True},
        {"lifecycle": {"on_timeout": "pause"}},
        {"lifecycle": {"on_timeout": {"action": "pause", "mode": "filesystem"}}},
        {"lifecycle": {"auto_resume": True}},
        {"lifecycle": {"auto_resume": 0}},
        {"lifecycle": {"on_timeout": {"action": "kill", "mode": "full"}}},
        {"lifecycle": {"unknown": False}},
        {"proxy": "unsupported"},
        {"headers": {}},
    ],
)
def test_unsupported_options_fail_before_arker_client(monkeypatch, options):
    monkeypatch.setattr("arker._compat.arker_client", lambda *a: pytest.fail("must not create client"))
    with pytest.raises(UnsupportedCompatibilityOption):
        shim.Sandbox._create_arker({}, "template", None, None, options)


def test_native_credential_cannot_authenticate_arker(monkeypatch):
    monkeypatch.delenv("ARKER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="api_key is required"):
        shim.Sandbox._create_arker({}, "template", None, None, {"api_key": "native-only"})


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_options_are_forwarded_without_mutation(monkeypatch, asynchronous):
    import e2b

    monkeypatch.delenv("ARKER_API_KEY", raising=False)
    monkeypatch.delenv("COMPUTESDK_ROUTER_CONFIG", raising=False)
    options = {
        "api_key": "native-only",
        "secure": False,
        "validate_api_key": True,
        "debug": False,
        "lifecycle": {"on_timeout": "pause", "auto_resume": True},
        "request_timeout": 0,
    }
    seen = []
    result = SimpleNamespace(sandbox_id="native", commands=object(), files=object(), git=object())

    def create(*args, **kwargs):
        seen.append((args, kwargs))
        return result

    async def create_async(*args, **kwargs):
        return create(*args, **kwargs)

    monkeypatch.setattr(
        e2b.AsyncSandbox if asynchronous else e2b.Sandbox, "create", create_async if asynchronous else create
    )
    sandbox = (
        asyncio.run(shim.AsyncSandbox.create(template="native-template", **options))
        if asynchronous
        else shim.Sandbox.create(template="native-template", **options)
    )
    assert sandbox.sandbox_id == "native"
    assert seen == [(("native-template",), options)]
