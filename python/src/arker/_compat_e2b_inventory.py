"""Map observed Arker inventory to native E2B models without guest activation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ._compat import UnsupportedCompatibilityOption


def field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise UnsupportedCompatibilityOption(f"Arker does not expose {name}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise UnsupportedCompatibilityOption(f"Arker {name} has no timezone")
    return parsed


def _network_info(vm):
    from ._compat_e2b_network import APP, PORTS, SECRET
    from .computer import _drop_none

    rules = field(field(vm, "policies"), "rules") or {}
    inbound = {name: rule for name, rule in rules.items() if field(rule, "type") == "inbound"}
    if len(inbound) != 2 or any(
        "expires_at" in rule if isinstance(rule, dict) else field(rule, "expires_at") is not None
        for rule in inbound.values()
    ):
        return None
    normalized = {name: _drop_none(rule) for name, rule in inbound.items()}
    if any(set(rule) - {"type", "priority", "action", "auth", "match"} for rule in normalized.values()):
        return None
    control = [
        rule
        for rule in normalized.values()
        if rule.get("action") == "allow" and rule.get("auth") == "arker" and rule.get("match") == {"ports": [49983]}
    ]
    if len(control) != 1:
        return None
    for name, rule in normalized.items():
        if rule.get("action") != "allow":
            continue
        if rule.get("match") == {"ports": PORTS} and rule.get("auth") in ("open", "arker"):
            return {"allow_public_traffic": rule["auth"] == "open"}
        if (
            name == APP
            and rule.get("auth") == "open"
            and any(
                rule.get("match") == {"ports": PORTS, "headers": {"e2b-traffic-access-token": [value]}}
                for value in ("***", "${secret:" + SECRET + "}")
            )
        ):
            return {"allow_public_traffic": False}
    return None


def sandbox_info(vm: Any, lifecycle: dict[str, Any], *, include_network: bool = True) -> Any:
    from e2b import SandboxInfo, SandboxState

    descriptor = field(field(vm, "metadata"), "arker_e2b")
    if not isinstance(descriptor, dict) or type(descriptor.get("version")) is not int or descriptor["version"] != 1:
        raise UnsupportedCompatibilityOption("VM has no supported E2B inventory descriptor")
    metadata = descriptor.get("metadata")
    source = descriptor.get("source")
    if not isinstance(metadata, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()
    ):
        raise UnsupportedCompatibilityOption("E2B inventory metadata is not a string map")
    if (
        not isinstance(source, dict)
        or source.get("kind") not in {"vm_id", "vm_name", "image"}
        or not isinstance(source.get("value"), str)
    ):
        raise UnsupportedCompatibilityOption("E2B inventory has no recorded source reference")
    version = descriptor.get("envd_version")
    if not isinstance(version, str) or not version:
        raise UnsupportedCompatibilityOption("E2B inventory has no recorded envd version")
    resources = field(vm, "resources")
    cpu, memory = field(resources, "vcpu"), field(resources, "memory_mib")
    if type(cpu) is not int or cpu <= 0 or type(memory) is not int or memory <= 0:
        raise UnsupportedCompatibilityOption("Arker does not expose concrete CPU and memory sizes for this VM")
    from ._compat_e2b_lifecycle import LIFETIME_POLICY_ID

    state = "paused" if lifecycle["customer_paused"] else "running"
    started = timestamp(lifecycle.get("started_at"), "sandbox lifecycle start timestamp")
    if state == "paused":
        end = timestamp(lifecycle.get("paused_at"), "sandbox lifecycle pause timestamp")
    else:
        rule = field(field(field(vm, "policies"), "rules"), LIFETIME_POLICY_ID)
        if field(rule, "type") != "schedule" or field(rule, "action") != "delete":
            raise UnsupportedCompatibilityOption("VM has no E2B lifetime delete schedule")
        if field(rule, "at") is not None:
            end = timestamp(field(rule, "at"), "sandbox expiration timestamp")
        elif type(field(rule, "after_created")) is int:
            end = timestamp(field(vm, "created_at"), "VM creation timestamp") + timedelta(
                seconds=field(rule, "after_created")
            )
        else:
            raise UnsupportedCompatibilityOption("E2B lifetime schedule uses an unsupported clock")
    return SandboxInfo(
        sandbox_id=vm.id,
        sandbox_domain=None,
        template_id=source["value"],
        name=source["value"] if source["kind"] == "vm_name" else None,
        metadata=dict(metadata),
        started_at=started,
        end_at=end,
        state=SandboxState(state),
        cpu_count=cpu,
        memory_mb=memory,
        envd_version=version,
        network=_network_info(vm) if include_network else None,
    )


class InventoryPaginator:
    """Use the server cursor; state checks never activate the listed VMs."""

    def __init__(self, client: Any, *, query=None, limit=None, next_token=None, order=None, **opts):
        from e2b import InvalidArgumentException

        if order is not None and order not in {"asc", "desc"}:
            raise InvalidArgumentException("order must be asc or desc")
        self.order = order or "desc"
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise InvalidArgumentException("limit must be a positive integer")
        started_after = field(query, "started_after")
        if started_after is not None and not isinstance(started_after, datetime):
            raise InvalidArgumentException("started_after must be a datetime")
        self.started_after = started_after.astimezone(timezone.utc).isoformat() if started_after is not None else None
        metadata = field(query, "metadata") or {}
        if not isinstance(metadata, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()
        ):
            raise InvalidArgumentException("metadata must be a string map")
        states = field(query, "state")
        if states and any(state not in {"running", "paused"} for state in states):
            raise InvalidArgumentException("state must contain running or paused")
        from ._compat_e2b_lifecycle import _options

        _options(opts)
        self.options = opts
        self.client, self.query, self.limit = client, query, limit
        self.states = set(states) if states else {"running", "paused"}
        self.metadata_filter = {"arker_e2b": {"version": 1, "metadata": metadata}}
        template = field(query, "template")
        if template is not None:
            if not isinstance(template, str):
                raise InvalidArgumentException("template must be a string")
            self.metadata_filter["arker_e2b"]["source"] = {"value": template}
        self._next_token, self._has_next = next_token, True

    @property
    def has_next(self):
        return self._has_next

    @property
    def next_token(self):
        return self._next_token

    def next_items(self, **opts):
        from ._compat_e2b_lifecycle import Lifecycle, _control, _options
        from .computer import ArkerError

        request = _options({**self.options, **{key: value for key, value in opts.items() if value is not None}})
        if not self.has_next:
            raise Exception("No more items to fetch")
        page = _control(
            self.client.list_vms,
            cursor=self.next_token,
            limit=self.limit,
            metadata_filter=self.metadata_filter,
            started_after=self.started_after,
            started_at_order=self.order,
            **request,
        )
        result = []
        for vm in page.vms:
            try:
                lifecycle = Lifecycle(vm=vm)
                observed = lifecycle._read_lifecycle(**request)
                state = "paused" if observed["customer_paused"] else "running"
                if state in self.states:
                    result.append(sandbox_info(vm, observed, include_network=False))
            except ArkerError as error:
                if error.status != 404:
                    raise
        # Conversion failures leave the cursor unchanged so a caller can retry.
        self._next_token, self._has_next = page.next_cursor, bool(page.next_cursor)
        return result


class AsyncInventoryPaginator:
    def __init__(self, paginator):
        self._paginator = paginator

    @property
    def has_next(self):
        return self._paginator.has_next

    @property
    def next_token(self):
        return self._paginator.next_token

    async def next_items(self, **opts):
        import asyncio

        return await asyncio.to_thread(self._paginator.next_items, **opts)


def list_sandboxes(native, config, *, asynchronous=False, **options):
    from ._compat import arker_client, router_config

    providers = router_config(config, "e2b").existing.order
    if providers == ("e2b",):
        if native is None:
            raise ImportError("The native E2B SDK is required")
        return native.list(**options)
    if providers != ("arker",):
        raise UnsupportedCompatibilityOption(
            "Sandbox.list requires a single inventory provider; cross-provider pagination is unavailable"
        )
    known = {"query", "limit", "next_token", "order", "request_timeout"}
    unsupported = set(options) - known
    if unsupported:
        raise UnsupportedCompatibilityOption("Unsupported inventory options: " + ", ".join(sorted(unsupported)))
    paginator = InventoryPaginator(arker_client(config), **options)
    return AsyncInventoryPaginator(paginator) if asynchronous else paginator
