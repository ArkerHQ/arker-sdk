"""Reject ingress policies that can expose a private daemon port."""


def require_private_daemon(vm, port=49983):
    def value(item, key, default=None):
        return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)

    policies = value(vm.refresh(), "policies")
    entries = list((value(policies, "rules", {}) or {}).values()) + list(value(policies, "policies", []) or [])
    for rule in entries:
        if value(rule, "type") != "inbound" or value(rule, "action") == "deny" or value(rule, "auth") == "arker":
            continue
        ports = value(value(rule, "match"), "ports")
        if ports is None or any(
            item == port if isinstance(item, int) else min(item) <= port <= max(item) for item in ports
        ):
            raise RuntimeError(f"Native daemon requires owner-authenticated ingress on port {port}")
