"""App-only traffic credentials, installed atomically with a sandbox fork."""

from __future__ import annotations

import dataclasses
import re
import secrets

APP = "computesdk-e2b-apps"
CONTROL = "computesdk-e2b-envd"
SECRET = "computesdk-e2b-traffic"
PORTS = [[1, 49982], [49984, 65535]]
UNSET = object()


def traffic_token(vm):
    return getattr(vm, "_e2b_traffic_token", None)


def created_traffic_token(vm):
    return getattr(vm, "_e2b_traffic_token", UNSET)


def remember_traffic_token(vm, token):
    vm._e2b_traffic_token = token


def _dict(value):
    if dataclasses.is_dataclass(value):

        def clean(node):
            if isinstance(node, dict):
                return {k: clean(v) for k, v in node.items() if v is not None}
            if isinstance(node, list):
                return [clean(v) for v in node]
            return node

        return clean(dataclasses.asdict(value))
    return value.model_dump(by_alias=True, exclude_none=True) if hasattr(value, "model_dump") else value or {}


def _managed(name, rule):
    if not rule or rule.get("type") != "inbound" or rule.get("action") != "allow":
        return False
    match = rule.get("match", {})
    if set(rule) - {"type", "priority", "action", "auth", "match"} or set(match) - {"ports", "headers"}:
        return False
    headers = match.get("headers")
    if headers and (
        set(headers) != {"e2b-traffic-access-token"}
        or len(headers["e2b-traffic-access-token"]) != 1
        or any(v not in ("***", "${secret:" + SECRET + "}") for v in headers["e2b-traffic-access-token"])
    ):
        return False
    ports = match.get("ports")
    return (
        (name == APP or re.fullmatch(r"e2b-app-ingress-[0-9a-f-]{36}", name))
        and ports == PORTS
        and rule.get("auth") in ("arker", "open")
        and (not headers or rule.get("auth") == "open")
    ) or (
        (name == CONTROL or re.fullmatch(r"e2b-envd-ingress-[0-9a-f-]{36}", name))
        and ports == [49983]
        and rule.get("auth") == "arker"
        and not headers
    )


def traffic_policies(source, configured, public):
    source, configured = _dict(source), _dict(configured)
    existing, updates = source.get("rules", {}), configured.get("rules", {})
    if {APP, CONTROL}.intersection(updates) or SECRET in configured.get("secrets", {}):
        raise ValueError("E2B traffic policy names are reserved")
    if source.get("policies") or configured.get("policies"):
        raise ValueError("E2B traffic requires named ingress policies")
    custom = [r for k, r in {**existing, **updates}.items() if r and r.get("type") == "inbound" and not _managed(k, r)]
    if custom:
        if not public:
            raise ValueError(
                "Private traffic conflicts with custom inbound policies; automatic token configuration is unsupported"
            )
        if any(_managed(k, r) for k, r in existing.items()):
            raise ValueError("Custom and managed ingress cannot be combined automatically")
        return configured, None
    if any(r and not _managed(k, r) and r.get("priority") in (0, 1000000) for k, r in {**existing, **updates}.items()):
        raise ValueError("E2B ingress priorities are reserved")
    token = None if public else secrets.token_urlsafe(32)
    rules = {**updates, **{k: None for k, r in existing.items() if _managed(k, r)}}
    rules[APP] = {
        "type": "inbound",
        "priority": 0,
        "action": "allow",
        "auth": "open",
        "match": {
            "ports": PORTS,
            **({"headers": {"e2b-traffic-access-token": ["${secret:" + SECRET + "}"]}} if token else {}),
        },
    }
    rules[CONTROL] = {
        "type": "inbound",
        "priority": 1000000,
        "action": "allow",
        "auth": "arker",
        "match": {"ports": [49983]},
    }
    return {**configured, "rules": rules, "secrets": {**configured.get("secrets", {}), SECRET: token}}, token


def fork_with_traffic(client, request, public):
    source = request.get("source_vm_id") or request.get("source_vm_name")
    policies = client.get_vm(source).policies if source else None
    policies, token = traffic_policies(policies, request.get("policies"), public)
    vm = client.fork(**{**request, "policies": policies})
    remember_traffic_token(vm, token)
    return vm
