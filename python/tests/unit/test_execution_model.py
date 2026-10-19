"""The execution-model contract: VM and session states, the policy shapes that
suspend, delete and gate on ``vm_state``, and the 409s a state refusal raises."""

from __future__ import annotations

import dataclasses
import json
from typing import Literal, get_args, get_type_hints

import pytest
from test_computer import FakeTransport, client, use_transport

import arker.computer as sdk
from arker.generated import api_models as models

SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl dev@laptop"

POLICY_RULES = {
    "dev-key": {"type": "inbound", "priority": 50, "match": {"via": ["ssh"], "ssh_keys": [SSH_KEY]}, "action": "allow"},
    "shell": {
        "type": "inbound",
        "priority": 40,
        "match": {"via": ["ssh", "pty"], "connection_state": ["new", "open"], "vm_state": "running"},
        "action": "allow",
    },
    "idle-gate": {"type": "inbound", "priority": 30, "match": {"ports": [8080], "vm_state": "idle"}, "action": "deny"},
    "web": {"type": "inbound", "priority": 20, "match": {"ports": [8080], "vm_state": "running"}, "action": "allow"},
    "llm": {
        "type": "outbound",
        "priority": 10,
        "match": {"hosts": ["api.example"]},
        "action": {"rewrite": {"headers": {"authorization": "${secret:KEY}"}}, "suspend": True},
    },
    "legacy": {"type": "outbound", "priority": 5, "action": {"scaling": {"suspend": True}}},
    "rest": {"type": "outbound", "priority": 0, "action": "suspend"},
    "nap": {"type": "schedule", "after_last_running_start": 600, "action": "suspend"},
    "ttl": {"type": "schedule", "after_created": 86400, "action": "delete"},
    "end": {"type": "schedule", "at": "2030-01-01T00:00:00Z", "action": "delete"},
    "now": {"type": "schedule", "at": "2020-01-01T00:00:00Z", "action": "suspend"},
}


def vm_payload(state: str, session_state: str = "idle") -> dict:
    return {
        "vm_id": "vm_1",
        "owner_org_id": "org",
        "created_at": "2026-10-16T00:00:00Z",
        "description": None,
        "public": False,
        "state": state,
        "sessions": [{"session_id": "s1", "state": session_state, "cwd": "/"}],
        "resources": {},
        "network": {},
        "policies": {"rules": POLICY_RULES},
    }


def error(code: str, operation: dict, details: dict) -> dict:
    return {
        "error": {
            "code": code,
            "message": "refused",
            "timestamp": "2026-10-16T00:00:00Z",
            "request_id": "req_1",
            "request": operation,
            "details": details,
        }
    }


NOT_RUNNING = {"resource": {"kind": "vm"}, "requirement": "running"}


def test_state_enums_are_exactly_the_contract() -> None:
    assert set(get_args(models.VmState)) == {"idle", "running"}
    assert set(get_args(models.SessionState)) == {"idle", "pending", "running"}


@pytest.mark.parametrize("state", ["idle", "running"])
def test_vm_and_pending_session_decode(state: str) -> None:
    t = FakeTransport()
    t.add_json(lambda m, u: u.endswith("/v1/vms/vm_1"), 200, vm_payload(state, "pending"))
    t.add_json(
        lambda m, u: "/sessions" in u,
        200,
        {"sessions": [{"session_id": "s1", "state": "pending", "cwd": "/"}]},
    )
    with use_transport(t):
        vm = client().vm("vm_1").refresh()
        sessions = vm.list_sessions(state="pending")
    assert vm.state == state
    assert vm.sessions[0].state == "pending"
    assert sessions.sessions[0].state == "pending"
    assert t.calls[1]["url"].endswith("/sessions?state=pending")


def test_policy_rules_decode_to_typed_models() -> None:
    t = FakeTransport()
    t.add_json(lambda *_: True, 200, vm_payload("idle"))
    with use_transport(t):
        rules = client().vm("vm_1").refresh().policies.rules

    assert rules["idle-gate"].match.vm_state == "idle"
    assert rules["shell"].match.via == ["ssh", "pty"]
    assert rules["shell"].match.connection_state == ["new", "open"]
    assert (rules["dev-key"].match.via, rules["dev-key"].match.ssh_keys) == (["ssh"], [SSH_KEY])
    assert (rules["now"].at, rules["now"].action) == ("2020-01-01T00:00:00Z", "suspend")
    assert rules["web"].match.vm_state == "running"
    assert rules["rest"].action == "suspend"
    llm = rules["llm"].action
    assert isinstance(llm, models.PolicyAction1)
    assert llm.suspend is True and llm.rewrite.headers == {"authorization": "${secret:KEY}"}
    assert rules["legacy"].action.scaling.suspend is True
    assert isinstance(rules["nap"], models.ScheduleRule)
    assert (rules["nap"].after_last_running_start, rules["nap"].action) == (600, "suspend")
    assert (rules["ttl"].after_created, rules["ttl"].action) == (86400, "delete")
    assert (rules["end"].at, rules["end"].action) == ("2030-01-01T00:00:00Z", "delete")


def test_update_sends_rules_verbatim() -> None:
    t = FakeTransport()
    t.add_json(lambda m, u: m == "PATCH", 200, vm_payload("idle"))
    update = {"rules": {**POLICY_RULES, "old": None}}
    with use_transport(t):
        client().vm("vm_1").update(policies=update)
    assert json.loads(t.calls[0]["body"]) == {"policies": update}


def test_typed_inbound_ssh_and_pty_rules_round_trip() -> None:
    shell = models.PolicyRule(
        type="inbound",
        priority=40,
        match=models.PolicyMatch(via=["ssh", "pty"], connection_state=["new", "open"], vm_state="running"),
        action="allow",
    )
    dev_key = models.PolicyRule(
        type="inbound",
        priority=50,
        match=models.PolicyMatch(via=["ssh"], ssh_keys=[SSH_KEY]),
        action="allow",
    )
    rules = {"shell": shell, "dev-key": dev_key}
    t = FakeTransport()
    t.add_json(lambda m, u: m == "PATCH", 200, vm_payload("idle"))
    with use_transport(t):
        vm = client().vm("vm_1").update(policies=models.PolicyUpdate(rules=rules))
    sent = {name: POLICY_RULES[name] for name in rules}
    assert json.loads(t.calls[0]["body"]) == {"policies": {"rules": sent}}
    assert {name: vm.policies.rules[name] for name in rules} == rules


def test_a_policy_read_back_updates_without_its_read_only_fields() -> None:
    payload = vm_payload("idle")
    payload["policies"] = {**payload["policies"], "secrets": {"KEY": "***"}, "mitm_domains": ["a"], "warnings": ["w"]}
    t = FakeTransport()
    t.add_json(lambda m, u: m == "GET", 200, payload)
    t.add_json(lambda m, u: m == "PATCH", 200, payload)
    with use_transport(t):
        vm = client().vm("vm_1").refresh()
        vm.update(policies=vm.policies)
    sent = json.loads(t.calls[1]["body"])["policies"]
    assert sent == {"rules": POLICY_RULES, "secrets": {"KEY": "***"}}


def test_typed_policy_surface_has_no_withdrawn_shapes() -> None:
    verbs = next(arg for arg in get_args(models.PolicyAction) if not dataclasses.is_dataclass(arg))
    assert set(get_args(verbs)) == {"allow", "deny", "delete", "suspend"}
    assert {f.name for f in dataclasses.fields(models.PolicyAction1)} == {"rewrite", "gate", "suspend", "scaling"}
    assert {f.name for f in dataclasses.fields(models.ScheduleRule)} == {
        "type",
        "action",
        "at",
        "after_created",
        "after_last_running_start",
    }
    _, rule = get_args(models.PolicyRules)
    assert set(get_args(rule)) == {models.PolicyRule, models.ScheduleRule, models.LifecycleRule}
    assert get_type_hints(models.PolicyRule)["type"] == Literal["outbound", "inbound"]
    for withdrawn in ("ResourcesRule", "WebhookRule", "Webhook"):
        assert not hasattr(models, withdrawn)


@pytest.mark.parametrize(
    "call, operation_id",
    [
        (lambda vm: vm.run("", signal="SIGINT"), "createRun"),
        (lambda vm: vm.fork(), "fork"),
    ],
    ids=["signal", "fork"],
)
def test_not_running_refusal_is_a_typed_invalid_state(call, operation_id: str) -> None:
    t = FakeTransport()
    t.add_json(
        lambda *_: True, 409, error("invalid_state", {"kind": "matched", "operation_id": operation_id}, NOT_RUNNING)
    )
    with use_transport(t), pytest.raises(sdk.ArkerError) as caught:
        call(client().vm("vm_1"))
    assert (caught.value.status, caught.value.code) == (409, "invalid_state")
    assert isinstance(caught.value.body, models.InvalidState)
    assert caught.value.body.details.requirement == "running"
    assert len(t.calls) == 1


def test_inbound_refusal_decodes_as_invalid_state() -> None:
    payload = error("invalid_state", {"kind": "unmatched", "method": "GET"}, NOT_RUNNING)
    refused = sdk._extract_error(payload, 409)
    assert isinstance(refused.body, models.InvalidState)
    assert refused.body.details.requirement == "running"


def test_run_on_a_pty_session_is_a_typed_resource_busy() -> None:
    t = FakeTransport()
    t.add_json(
        lambda *_: True,
        409,
        error("resource_busy", {"kind": "matched", "operation_id": "createRun"}, {"resource": "session"}),
    )
    with use_transport(t), pytest.raises(sdk.ArkerError) as caught:
        client().vm("vm_1").run("ls", session_id="s1")
    assert (caught.value.status, caught.value.code) == (409, "resource_busy")
    assert isinstance(caught.value.body, models.ResourceBusy)
    assert caught.value.body.details.resource == "session"


def test_202_background_answer_is_running() -> None:
    t = FakeTransport()
    t.add_json(lambda *_: True, 202, {"run_id": "run_1", "session_id": "s1", "state": "running"})
    with use_transport(t):
        result = client().vm("vm_1").run("sleep 60", time_to_background=0)
    assert isinstance(result, sdk.BackgroundRunResult)
    assert (result.run_id, result.state) == ("run_1", "running")
