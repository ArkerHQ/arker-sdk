import copy
import json
from pathlib import Path

import pytest
from test_computer import FakeTransport, client, use_transport

import arker.computer as sdk

SPEC = json.loads((Path(__file__).resolve().parents[3] / "openapi.json").read_text())


def example(code):
    return copy.deepcopy(
        next(
            item["value"]
            for response in SPEC["components"]["responses"].values()
            for item in response.get("content", {}).get("application/json", {}).get("examples", {}).values()
            if item.get("value", {}).get("error", {}).get("code") == code
        )
    )


def test_http_error_preserves_typed_resource_and_request():
    payload = {
        "error": {
            "code": "not_found",
            "message": "missing",
            "timestamp": "2026-09-16T00:00:00Z",
            "request_id": "req-test",
            "request": {"kind": "matched", "operation_id": "deleteVm"},
            "details": {"resource": "vm"},
        }
    }
    transport = FakeTransport()
    transport.add_json(lambda *_: True, 404, payload)
    with use_transport(transport), pytest.raises(sdk.ArkerError) as caught:
        client().vm("missing").delete()
    assert caught.value.body.code == "not_found"
    assert caught.value.body.details.resource == "vm"
    assert caught.value.body.request_id == "req-test"


def test_unknown_error_preserves_payload():
    payload = {"error": {"code": "future_code", "message": "new", "timestamp": "2026-09-16T00:00:00Z", "new_field": 42}}
    transport = FakeTransport()
    transport.add_json(lambda *_: True, 409, payload)
    with use_transport(transport), pytest.raises(sdk.ArkerError) as caught:
        client().vm("missing").delete()
    assert caught.value.code == "future_code"
    assert caught.value.body is None
    assert caught.value.raw == payload["error"]


@pytest.mark.parametrize("work", ["unknown", "continuing", "stopped"])
def test_partial_work_is_not_replayed(work):
    payload = example("unavailable")
    payload["error"]["recovery"] = {"work": work, "context": {"vm_id": "vm-existing"}}
    payload["error"]["retry_after_seconds"] = 0
    transport = FakeTransport()
    transport.add_json(lambda *_: True, 503, payload)
    with use_transport(transport), pytest.raises(sdk.ArkerError) as caught:
        sdk.Arker(api_key="k", base_url="https://test.invalid", retry={"attempts": 3}).vm("vm").delete()
    assert len(transport.calls) == 1
    assert caught.value.body.recovery.context.vm_id == "vm-existing"


def test_every_contract_example_decodes_by_code():
    for response in SPEC["components"]["responses"].values():
        for item in response.get("content", {}).get("application/json", {}).get("examples", {}).values():
            payload = item.get("value", {})
            if "error" not in payload:
                continue
            error = sdk._extract_error(payload, 400)
            assert error.body is not None
            assert error.body.code == payload["error"]["code"]


def test_sync_file_error_preserves_resource():
    transport = FakeTransport()
    transport.add_json(
        lambda *_: True,
        200,
        {
            "op": "write",
            "results": [
                {
                    "error": {
                        "code": "not_found",
                        "message": "missing file",
                        "details": {"resource": "file"},
                        "recovery": {"work": "stopped", "context": {"vm_id": "vm"}},
                    }
                }
            ],
        },
    )
    with use_transport(transport), pytest.raises(sdk.ArkerError) as caught:
        client().vm("vm").sync("/file", b"hello")
    assert caught.value.body.code == "not_found"
    assert caught.value.body.details.resource == "file"
    assert caught.value.body.recovery.context.vm_id == "vm"


def test_invalid_sync_resource_is_not_exposed_as_a_typed_error():
    raw = {"code": "not_found", "message": "invalid resource", "details": {"resource": "session"}}
    error = sdk._server_error(raw, 200, file=True)
    assert error.body is None
    assert error.raw == raw


@pytest.mark.parametrize(
    "changes",
    [
        {"retry_after_seconds": 4294967296},
        {"stage": "image_configuration", "recovery": None},
        {"stage": "dockerfile_build", "recovery": {"work": "continuing", "context": {"vm_id": "vm"}}},
    ],
)
def test_invalid_contract_metadata_is_not_exposed_as_typed(changes):
    raw = example("internal")["error"]
    raw.update(changes)
    if raw.get("recovery") is None:
        raw.pop("recovery", None)
    transport = FakeTransport()
    transport.add_json(lambda *_: True, 500, {"error": raw})
    with use_transport(transport), pytest.raises(sdk.ArkerError) as caught:
        client().vm("vm").delete()
    error = caught.value
    assert error.body is None
    assert error.raw == raw
