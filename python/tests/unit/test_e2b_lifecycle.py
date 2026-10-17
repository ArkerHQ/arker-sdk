import types

import pytest

import arker.e2b as shim


@pytest.mark.parametrize("deleted", [True, False])
def test_kill_preserves_provider_boolean(deleted):
    assert (
        shim.Sandbox(
            vm=types.SimpleNamespace(
                id="vm", refresh=lambda: None, delete=lambda: types.SimpleNamespace(deleted=deleted)
            )
        ).kill()
        is deleted
    )
    assert shim.Sandbox(native=types.SimpleNamespace(sandbox_id="native", kill=lambda: deleted)).kill() is deleted


def test_context_manager_cleans_up_without_swallowing_user_error():
    calls = []
    sandbox = shim.Sandbox(native=types.SimpleNamespace(sandbox_id="native", kill=lambda: calls.append("kill")))
    failure = RuntimeError("application failed")
    with pytest.raises(RuntimeError) as raised, sandbox as entered:
        assert entered is sandbox
        raise failure
    assert raised.value is failure
    assert calls == ["kill"]


@pytest.mark.parametrize("status", [404, 403, 503])
def test_kill_only_treats_authoritative_absence_as_false(status):
    from arker.computer import ArkerError

    error = ArkerError("request_failed", "request failed", status=status)

    def delete():
        raise error

    sandbox = shim.Sandbox(vm=types.SimpleNamespace(id="vm", refresh=lambda: None, delete=delete))
    if status == 404:
        assert sandbox.kill() is False
    else:
        with pytest.raises(ArkerError) as raised:
            sandbox.kill()
        assert raised.value is error


def test_connect_probes_metadata_before_connecting_only_the_winner(monkeypatch):
    calls = []
    native = types.SimpleNamespace(
        get_info=lambda identifier: calls.append(("info", identifier)) or object(),
        connect=lambda identifier: (
            calls.append(("connect", identifier)) or types.SimpleNamespace(sandbox_id=identifier)
        ),
    )
    monkeypatch.setattr(shim.Sandbox, "_native_sandbox", staticmethod(lambda: native))

    def route(identifier, provider, config, getters):
        getters["e2b"]()
        assert calls == [("info", identifier)]
        return "arker", types.SimpleNamespace(id="arker-winner")

    monkeypatch.setattr(shim, "routed_get", route)
    assert shim.Sandbox.connect("shared-id").sandbox_id == "arker-winner"
    assert calls == [("info", "shared-id")]


def test_connect_resolves_native_handle_after_native_ownership_wins(monkeypatch):
    calls = []
    native = types.SimpleNamespace(
        get_info=lambda identifier: calls.append("info") or object(),
        connect=lambda identifier: calls.append("connect") or types.SimpleNamespace(sandbox_id=identifier),
    )
    monkeypatch.setattr(shim.Sandbox, "_native_sandbox", staticmethod(lambda: native))
    monkeypatch.setattr(shim, "routed_get", lambda identifier, provider, config, getters: ("e2b", getters["e2b"]()))
    assert shim.Sandbox.connect("native-id").sandbox_id == "native-id"
    assert calls == ["info", "connect"]


@pytest.mark.parametrize("owner", ["e2b", "arker", None])
def test_static_kill_uses_shared_ownership_without_connecting(monkeypatch, owner):
    calls = []
    native = types.SimpleNamespace(
        get_info=lambda identifier: calls.append("info") or object(),
        connect=lambda identifier: pytest.fail("Deletion must not connect"),
        kill=lambda identifier: calls.append(("native-kill", identifier)) or True,
    )
    vm = types.SimpleNamespace(
        id="shared-id",
        refresh=lambda: None,
        delete=lambda: calls.append("arker-kill") or types.SimpleNamespace(deleted=True),
    )
    monkeypatch.setattr(shim.Sandbox, "_native_sandbox", staticmethod(lambda: native))

    def lookup(identifier, provider, config, getters):
        assert identifier == "shared-id"
        if owner == "e2b":
            return owner, getters[owner]()
        return (owner, vm) if owner else None

    monkeypatch.setattr(shim, "routed_get", lookup)
    assert shim.Sandbox.kill(sandbox_id="shared-id") is (owner is not None)
    assert calls == (["info", ("native-kill", "shared-id")] if owner == "e2b" else ["arker-kill"] if owner else [])
