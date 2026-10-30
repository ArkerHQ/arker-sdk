"""Keyed creation and outbound policy translation shared by E2B facades."""

import hashlib
import ipaddress
import json
import re
from types import SimpleNamespace

from ._compat import UnsupportedCompatibilityOption


def creation_key(value):
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9-]{1,255}", value) is None:
        from e2b import InvalidArgumentException

        raise InvalidArgumentException("Idempotency key must contain 1-255 lowercase letters, digits or hyphens")
    return value


def intent_hash(template, timeout, internet, options):
    intent = {
        "template": template,
        "timeout": timeout,
        "internet": internet,
        "envs": options.get("envs") or {},
        "metadata": options.get("metadata") or {},
        "network": options.get("network") or {},
    }
    mounts = options.get("volume_mounts")
    if mounts:
        intent["volume_mounts"] = {p: v if isinstance(v, str) else v.volume_id for p, v in mounts.items()}
    return hashlib.sha256(
        json.dumps(intent, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def network_options(network):
    if network is None:
        return {}, None
    if not isinstance(network, dict):
        raise TypeError("network must be an object")
    if set(network) - {"allow_public_traffic", "allow_out", "deny_out", "dns_servers"}:
        raise UnsupportedCompatibilityOption("Unsupported E2B network option")
    result = dict(network)
    for name in ("allow_out", "deny_out"):
        value = result.get(name)
        if callable(value):
            value = value(SimpleNamespace(all_traffic="0.0.0.0/0", rules={}))
            result[name] = value
        if value is not None and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
            raise TypeError(f"network.{name} must be a list of strings")
    dns = result.get("dns_servers", ["8.8.8.8"])
    if dns is None:
        dns = ["8.8.8.8"]
    if not isinstance(dns, list) or len(dns) > 10:
        raise ValueError("dns_servers must contain at most ten IPv4 addresses")
    for address in dns:
        if not isinstance(address, str) or ipaddress.ip_address(address).version != 4:
            raise ValueError("dns_servers entries must be IPv4 addresses")
    deny = result.get("deny_out") or []
    if deny and deny != ["0.0.0.0/0"]:
        raise UnsupportedCompatibilityOption("Only deny-all with explicit outbound exceptions is supported")
    allowed = result.get("allow_out")
    if allowed is None and not deny:
        return result, None  # Native DNS options have no effect without domain selectors.
    hosts, ips = [], []
    for entry in allowed or []:
        try:
            ipaddress.ip_network(entry, strict=False)
            ips.append(entry)
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9*](?:[A-Za-z0-9*.-]*[A-Za-z0-9])?", entry):
                raise ValueError("Invalid outbound host selector") from None
            hosts.append(entry)
    if hosts and not deny:
        from e2b import SandboxException

        raise SandboxException("Domain allowlists require deny_out to include 0.0.0.0/0", status_code=400)
    if not deny:
        return result, None
    if hosts:
        ips.extend(dns)
    allow = ([{"hosts": hosts}] if hosts else []) + ([{"ips": ips}] if ips else [])
    rule = {"type": "outbound_guard", "allow": allow}
    policies = outbound_guard(rule)
    if hosts:
        # Host guards carry an implicit resolver exception. An independent
        # port/IP floor limits DNS to the explicitly allowed addresses.
        dns_rule = {
            "type": "outbound_guard",
            "allow": [{"ports": [[1, 52], [54, 65535]]}] + ([{"ips": ips}] if ips else []),
        }
        policies["rules"].update(outbound_guard(dns_rule)["rules"])
    return result, policies


def outbound_guard(rule):
    # Policy IDs have a 64-character wire limit, including the prefix.
    digest = hashlib.sha256(json.dumps(rule, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"rules": {"e2b-egress-" + digest[:53]: rule}}
