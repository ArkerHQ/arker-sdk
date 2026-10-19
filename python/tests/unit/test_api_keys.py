"""API keys: create, list and revoke on the control plane, with scopes as
``{"vm_id"}`` or ``{"pool_id"}`` objects."""

from __future__ import annotations

import json

import pytest
from test_computer import FakeTransport, region_client, use_transport

import arker
import arker.computer as sdk

POOL_ID = "0b6c1d2e-3f40-4a51-8b62-7c83d94ea5f6"
CREATED_AT = "2026-10-17T00:00:00.000Z"
# What the API still returns for SDKs released before `scopes` took objects.
DEPRECATED = {"role": "admin", "vm_id": None, "pool_id": None, "action": None}


def created(scopes: list[dict]) -> dict:
    key = {"api_key_id": "agents_abc", "scopes": scopes, "created_at": CREATED_AT, "key": "ark_live_secret"}
    return {**key, **DEPRECATED}


def body(call: dict) -> dict:
    return json.loads(call["body"])


@pytest.mark.parametrize(
    ("kwargs", "sent"),
    [
        ({"name": "agents", "scopes": [{"pool_id": POOL_ID}]}, {"name": "agents", "scopes": [{"pool_id": POOL_ID}]}),
        (
            {"name": "agent", "scopes": [{"vm_id": "vm_a"}, arker.ApiKeyVmScope(vm_id="vm_b")]},
            {"name": "agent", "scopes": [{"vm_id": "vm_a"}, {"vm_id": "vm_b"}]},
        ),
        ({"scopes": []}, {"scopes": []}),
        ({}, {}),
    ],
)
def test_create_sends_the_request_to_the_control_plane(kwargs: dict, sent: dict) -> None:
    t = FakeTransport()
    t.add_json(lambda m, u: m == "POST", 201, created(sent.get("scopes", [])))
    with use_transport(t):
        region_client().create_api_key(**kwargs)
    (call,) = t.calls
    assert call["url"] == "https://arker.ai/api/v1/api-keys"
    assert body(call) == sent


def test_create_decodes_the_key_and_typed_scopes() -> None:
    t = FakeTransport()
    t.add_json(lambda m, u: True, 201, created([{"pool_id": POOL_ID}]))
    with use_transport(t):
        key = region_client().create_api_key(name="agents", scopes=[{"pool_id": POOL_ID}])
    assert isinstance(key, arker.CreatedApiKey)
    assert key.key == "ark_live_secret"
    assert key.api_key_id == "agents_abc"
    assert key.created_at == CREATED_AT
    assert key.scopes == [arker.ApiKeyPoolScope(pool_id=POOL_ID)]


def test_list_decodes_each_key() -> None:
    t = FakeTransport()
    t.add_json(
        lambda m, u: m == "GET",
        200,
        {
            "api_keys": [
                {"api_key_id": "org_1", "scopes": [], "created_at": CREATED_AT, **DEPRECATED},
                {
                    "api_key_id": "vms_1",
                    "scopes": [{"vm_id": "vm_a"}, {"vm_id": "vm_b"}],
                    "created_at": CREATED_AT,
                    **DEPRECATED,
                },
            ]
        },
    )
    with use_transport(t):
        listed = region_client().list_api_keys()
    assert t.calls[0]["url"] == "https://arker.ai/api/v1/api-keys"
    assert [key.api_key_id for key in listed.api_keys] == ["org_1", "vms_1"]
    assert listed.api_keys[0].scopes == []
    assert listed.api_keys[1].scopes == [arker.ApiKeyVmScope(vm_id="vm_a"), arker.ApiKeyVmScope(vm_id="vm_b")]


def test_revoke_deletes_the_escaped_id_and_returns_none() -> None:
    t = FakeTransport()
    t.add_raw(lambda m, u: m == "DELETE", 204, b"")
    with use_transport(t):
        assert region_client().revoke_api_key("agents/abc") is None
    assert t.calls[0]["url"] == "https://arker.ai/api/v1/api-keys/agents%2Fabc"


def refusal(code: str, details: dict | None = None) -> dict:
    error = {
        "code": code,
        "message": "refused",
        "timestamp": "2026-10-17T00:00:00Z",
        "request_id": "req_1",
        "request": {"kind": "matched", "operation_id": "createApiKey"},
    }
    if details is not None:
        error["details"] = details
    return {"error": error}


SCOPES_SHAPE = 'scopes must be 1..16 {"vm_id"} objects or one {"pool_id"}'


def test_bad_scopes_raise_a_typed_bad_request() -> None:
    t = FakeTransport()
    t.add_json(
        lambda m, u: True,
        400,
        refusal("bad_request", {"violations": [{"field": "request", "message": SCOPES_SHAPE}]}),
    )
    with use_transport(t), pytest.raises(sdk.ArkerError) as caught:
        region_client().create_api_key(scopes=[{"pool_id": POOL_ID}, {"vm_id": "vm_a"}])
    assert caught.value.status == 400
    assert caught.value.code == "bad_request"
    assert caught.value.body.details.violations[0].message == SCOPES_SHAPE
    assert len(t.calls) == 1


@pytest.mark.parametrize(("status", "code"), [(403, "forbidden"), (404, "not_found")])
def test_refusals_raise_with_their_code(status: int, code: str) -> None:
    t = FakeTransport()
    details = {"resource": "pool"} if code == "not_found" else None
    t.add_json(lambda m, u: True, status, refusal(code, details))
    with use_transport(t), pytest.raises(sdk.ArkerError) as caught:
        region_client().create_api_key(name="agents", scopes=[{"pool_id": POOL_ID}])
    assert (caught.value.status, caught.value.code) == (status, code)
