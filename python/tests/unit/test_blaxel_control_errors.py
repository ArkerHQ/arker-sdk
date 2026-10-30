import asyncio
import importlib
from types import SimpleNamespace

import pytest

from arker._compat_blaxel_errors import control_call, creation_error, not_found, translate_error
from arker.computer import ArkerError


@pytest.mark.parametrize("operation", ["create", "get", "delete"])
@pytest.mark.parametrize("status", [401, 403])
def test_control_error_matches_native_parser_fields(monkeypatch, operation, status):
    pytest.importorskip("blaxel.core.sandbox")
    from blaxel.core.client.models.error import Error
    from blaxel.core.client.models.sandbox_error import SandboxError
    from blaxel.core.sandbox import SandboxAPIError
    from blaxel.core.sandbox import SyncSandboxInstance as Native

    original = ArkerError("forbidden", "controlled refusal", status)
    body = (
        {"code": original.code, "message": original.message, "status_code": status}
        if operation == "create"
        else {"code": status, "error": original.message}
    )
    parsed = (SandboxError if operation == "create" else Error).from_dict(body)
    module = importlib.import_module(Native.__module__)
    monkeypatch.setattr(module, f"{operation}_sandbox", lambda *args, **kwargs: parsed)
    with pytest.raises(SandboxAPIError) as native:
        if operation == "create":
            Native.create({"name": "controlled", "region": "us-pdx-1"})
        else:
            getattr(Native, operation)("controlled")
    mapped = translate_error(original, operation)
    assert type(mapped) is type(native.value)
    assert (mapped.status_code, mapped.code, mapped.body) == (
        native.value.status_code,
        native.value.code,
        native.value.body,
    )


def test_known_missing_and_duplicate_shapes_match_native_classes():
    pytest.importorskip("blaxel.core.sandbox")
    from blaxel.core.sandbox import SandboxAPIError

    missing = not_found()
    assert isinstance(missing, SandboxAPIError)
    assert (missing.status_code, missing.code, missing.body) == (
        404,
        "Sandbox not found",
        {"code": 404, "error": "Sandbox not found"},
    )
    duplicate = creation_error("exists", 409, "SANDBOX_ALREADY_EXISTS")
    assert isinstance(duplicate, SandboxAPIError)
    assert duplicate.body == {"code": "SANDBOX_ALREADY_EXISTS", "message": "exists", "status_code": 409}


@pytest.mark.parametrize(
    "status,code",
    [(0, "unavailable"), (408, "unavailable"), (504, "unavailable"), (409, "invalid_state"), (404, "not_found")],
)
def test_create_uncertain_or_unrelated_errors_retain_identity(status, code):
    error = ArkerError(code, "original", status)
    assert translate_error(error, "create") is error


def test_native_errors_are_never_translated_again():
    pytest.importorskip("blaxel.core.sandbox")
    error = creation_error("native", 403, "UNAUTHORIZED")
    assert translate_error(error, "create") is error


def test_mapped_authority_error_never_falls_back(monkeypatch):
    import arker._compat as compat

    pytest.importorskip("blaxel.core.sandbox")
    from blaxel.core.sandbox import SandboxAPIError

    error = ArkerError("forbidden", "denied", 403)

    def refuse():
        raise error

    monkeypatch.setattr(
        compat,
        "router_config",
        lambda *args, **kwargs: SimpleNamespace(create=SimpleNamespace(strategy="priority", order=["arker", "blaxel"])),
    )
    with pytest.raises(SandboxAPIError) as mapped:
        compat.routed_create(
            "blaxel",
            {},
            {"arker": lambda: control_call("create", refuse), "blaxel": lambda: pytest.fail("auth refusal fell back")},
        )
    assert mapped.value.__cause__ is error
    assert mapped.value.status_code == 403


def test_facade_uses_atomic_delete_result_for_missing(monkeypatch):
    from arker.blaxel import SyncSandboxInstance

    pytest.importorskip("blaxel.core.sandbox")
    from blaxel.core.sandbox import SandboxAPIError

    vm = SimpleNamespace(id="missing", name="missing", delete=lambda: SimpleNamespace(deleted=False))
    with pytest.raises(SandboxAPIError) as error:
        SyncSandboxInstance(vm=vm).delete()
    assert error.value.status_code == 404


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("outcome", ["deleted", "missing", "disappeared"])
def test_class_delete_only_looks_up_control_plane(monkeypatch, asynchronous, outcome):
    import arker.blaxel as facade

    pytest.importorskip("blaxel.core.sandbox")
    from blaxel.core.sandbox import SandboxAPIError

    calls = []

    class PausedVM:
        id = "paused"
        name = "paused"
        customer_paused = True
        created_at = None
        metadata = None

        def delete(self):
            calls.append("delete")
            return SimpleNamespace(deleted=outcome != "disappeared")

        def __getattr__(self, name):
            pytest.fail(f"Unexpected paused VM access: {name}")

    def lookup(sandbox_id, config):
        assert sandbox_id == "paused"
        calls.append("lookup")
        return None if outcome == "missing" else PausedVM()

    def route(sandbox_id, provider, config, getters):
        vm = getters["arker"]()
        return None if vm is None else ("arker", vm)

    def forbidden(*args, **kwargs):
        pytest.fail("Class deletion must not initialize the guest data plane")

    monkeypatch.setattr(facade, "arker_get", lookup)
    monkeypatch.setattr(facade, "routed_get", route)
    monkeypatch.setattr(facade.SyncSandboxInstance, "_plane", forbidden)
    monkeypatch.setattr(facade.SandboxInstance, "_wrap", forbidden)

    def delete():
        if asynchronous:
            asyncio.run(facade.SandboxInstance.delete("paused"))
        else:
            facade.SyncSandboxInstance.delete("paused")

    if outcome == "deleted":
        delete()
    else:
        with pytest.raises(SandboxAPIError) as error:
            delete()
        assert (error.value.status_code, error.value.code, error.value.body) == (
            404,
            "Sandbox not found",
            {"code": 404, "error": "Sandbox not found"},
        )
    assert calls == (["lookup"] if outcome == "missing" else ["lookup", "delete"])
