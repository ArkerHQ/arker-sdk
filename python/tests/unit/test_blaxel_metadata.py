from types import SimpleNamespace

import pytest

native = pytest.importorskip("blaxel.core.client.models.metadata")


def test_metadata_uses_native_model_and_authoritative_birth():
    from arker.blaxel import SyncSandboxInstance

    vm = SimpleNamespace(
        id="vm-test",
        name="named",
        created_at="2026-01-01T00:00:00Z",
        metadata={"arker_blaxel": {"version": 1, "labels": {"purpose": "test"}}},
    )
    metadata = SyncSandboxInstance(vm=vm).metadata
    assert isinstance(metadata, native.Metadata)
    assert metadata.to_dict() == {"name": "named", "createdAt": vm.created_at, "labels": {"purpose": "test"}}


def test_creation_records_labels_even_when_empty(monkeypatch):
    import arker.blaxel as facade
    from arker import _compat_blaxel_data_plane as plane

    captured = []
    vm = SimpleNamespace(id="vm-test", name="named", created_at=None, metadata={})
    monkeypatch.setattr(facade, "routed_create", lambda vendor, config, callbacks: ("arker", callbacks["arker"]()))
    monkeypatch.setattr(facade, "arker_create", lambda *args, **kwargs: captured.append(kwargs) or vm)
    monkeypatch.setattr(plane, "validate_native", lambda: None)
    monkeypatch.setattr(facade.SyncSandboxInstance, "_plane", lambda self: None)
    for labels in ({"purpose": "test"}, {}):
        facade.SyncSandboxInstance.create({"name": "named", "labels": labels})
        assert captured[-1]["metadata"] == {"arker_blaxel": {"version": 1, "labels": labels}}


def test_committed_response_loss_retains_native_read_error_identity():
    import httpx
    import httpx2

    from arker._compat_blaxel_errors import control_call
    from arker.computer import ArkerError

    def lost():
        try:
            raise httpx2.ReadError(
                "response lost",
                request=httpx2.Request("POST", "https://custom.example/v1/named-fork", content=b"payload"),
            )
        except httpx2.ReadError as cause:
            raise ArkerError("unknown_outcome", "creation outcome unknown", 0) from cause

    with pytest.raises(httpx.ReadError) as caught:
        control_call("create", lost)
    assert caught.value.request.content == b""
    assert caught.value.request.method == "POST"
    assert str(caught.value.request.url) == "https://custom.example/v1/named-fork"


def test_requestless_transport_error_keeps_native_type():
    import httpx
    import httpx2

    from arker._compat_blaxel_errors import translate_error
    from arker.computer import ArkerError

    error = ArkerError("unknown_outcome", "lost", 0)
    error.__cause__ = httpx2.ReadError("lost")
    mapped = translate_error(error, "create")
    assert isinstance(mapped, httpx.ReadError)
    with pytest.raises(RuntimeError, match="request"):
        _ = mapped.request


def test_transport_conversion_does_not_consume_stream():
    import httpx
    import httpx2

    from arker._compat_blaxel_errors import translate_error
    from arker.computer import ArkerError

    consumed = []

    def chunks():
        consumed.append(True)
        yield b"payload"

    request = httpx2.Request("POST", "https://custom.example", content=chunks(), extensions={"test": True})
    error = ArkerError("unknown_outcome", "lost", 0)
    error.__cause__ = httpx2.ReadError("lost", request=request)
    mapped = translate_error(error, "create")
    assert isinstance(mapped, httpx.ReadError)
    assert not consumed
    assert mapped.request.stream is not request.stream
    assert mapped.request.extensions == {}


@pytest.mark.parametrize("kind", ["ReadTimeout", "ConnectTimeout"])
def test_direct_timeout_translated_once_without_sensitive_request_metadata(kind):
    import httpx
    import httpx2

    from arker._compat_blaxel_errors import control_call

    request = httpx2.Request(
        "POST",
        "https://user:password@custom.example/create?token=secret#secret",
        headers={"Authorization": "Bearer secret", "Cookie": "secret", "Content-Length": "123"},
        content=b"secret",
        extensions={"secret": "secret"},
    )
    calls = []
    original = getattr(httpx2, kind)("timeout", request=request)

    def fail():
        calls.append(True)
        raise original

    with pytest.raises(getattr(httpx, kind)) as caught:
        control_call("create", fail)
    assert calls == [True]
    assert caught.value.__cause__ is original
    diagnostic = caught.value.request
    assert str(diagnostic.url) == "https://custom.example/create"
    assert diagnostic.method == "POST"
    assert "authorization" not in diagnostic.headers
    assert "cookie" not in diagnostic.headers
    assert diagnostic.headers.get("content-length") != "123"
    assert diagnostic.content == b""
    assert diagnostic.extensions == {}


def test_unknown_transport_subclass_preserved_without_replay():
    import httpx2

    from arker._compat_blaxel_errors import control_call

    class CustomFailure(httpx2.ReadError):
        pass

    original = CustomFailure("unknown")
    calls = []

    def fail():
        calls.append(True)
        raise original

    with pytest.raises(CustomFailure) as caught:
        control_call("create", fail)
    assert caught.value is original
    assert calls == [True]


def test_atomic_loser_and_reconnect_keep_winner_labels(monkeypatch):
    import arker.blaxel as facade
    from arker import _compat_blaxel_data_plane as plane

    winner = None
    requests = []

    def create(*args, **kwargs):
        nonlocal winner
        requests.append(kwargs["metadata"])
        if winner is None:
            winner = SimpleNamespace(
                id="vm-test", name="named", created_at="2026-01-01T00:00:00Z", metadata=kwargs["metadata"]
            )
        return winner

    monkeypatch.setattr(facade, "routed_create", lambda vendor, config, callbacks: ("arker", callbacks["arker"]()))
    monkeypatch.setattr(facade, "routed_get", lambda name, vendor, config, callbacks: ("arker", callbacks["arker"]()))
    monkeypatch.setattr(facade, "arker_create", create)
    monkeypatch.setattr(facade, "arker_get", lambda *args: winner)
    monkeypatch.setattr(plane, "validate_native", lambda: None)
    monkeypatch.setattr(facade.SyncSandboxInstance, "_plane", lambda self: None)
    first = facade.SyncSandboxInstance.create_if_not_exists({"name": "named", "labels": {"owner": "winner"}})
    second = facade.SyncSandboxInstance.create_if_not_exists({"name": "named", "labels": {"owner": "loser"}})
    reconnected = facade.SyncSandboxInstance.get("named")
    assert requests[0] != requests[1]
    for instance in (first, second, reconnected):
        assert instance.metadata.to_dict() == {
            "name": "named",
            "createdAt": "2026-01-01T00:00:00Z",
            "labels": {"owner": "winner"},
        }


@pytest.mark.parametrize("wrapped", [False, True])
def test_missing_optional_httpx_preserves_original(monkeypatch, wrapped):
    import sys

    import httpx2

    from arker._compat_blaxel_errors import translate_error
    from arker.computer import ArkerError

    original = httpx2.ReadError("lost")
    if wrapped:
        wrapper = ArkerError("unknown_outcome", "unknown", 0)
        wrapper.__cause__ = original
        original = wrapper
    monkeypatch.setitem(sys.modules, "httpx", None)
    assert translate_error(original, "create") is original
    unrelated = ArkerError("internal_error", "failed", 500)
    assert translate_error(unrelated, "create") is unrelated


@pytest.mark.parametrize("missing", [True, False])
def test_unrepresentable_native_constructor_preserves_original(monkeypatch, missing):
    import httpx
    import httpx2

    from arker._compat_blaxel_errors import translate_error

    class Unrepresentable(httpx.RequestError):
        def __init__(self, message):
            raise TypeError("different constructor")

    if missing:
        monkeypatch.delattr(httpx, "ReadError")
    else:
        monkeypatch.setattr(httpx, "ReadError", Unrepresentable)
    original = httpx2.ReadError("lost")
    assert translate_error(original, "create") is original
