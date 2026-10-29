"""Named creation through the public, organization-wide fork coordinator."""

from __future__ import annotations

import os
import re
import time

from ._compat_async import RoutingCancelled
from ._compat_blaxel_errors import creation_error
from .computer import REQUEST_TIMEOUT_S, VM, ArkerError, _drop_none, _policy_update, _vm_info


def validate_name(name):
    if name is None or name == "":
        return
    if not isinstance(name, str):
        message = "Failed to parse sandbox body"
    elif len(name) > 49:
        message = "Invalid sandbox configuration: metadata.name is too long (maximum 49 characters)"
    elif re.fullmatch(r"[a-z0-9-]+", name) is None:
        message = (
            "Invalid sandbox configuration: metadata.name must contain only "
            "lower case alphanumeric characters and hyphens"
        )
    else:
        return
    raise creation_error(message, status=400, code="VALIDATION_FAILED")


def fork_named(client, request, *, config, get_or_create=False, cancelled=lambda: False):
    name = request.get("name")
    if cancelled():
        raise RoutingCancelled("Async routing was cancelled")
    validate_name(name)
    if not name:
        return client.fork(**request)
    # A regional URL is not a global coordinator. Never silently send
    # that caller's source or credentials to the default production gateway.
    if (config.get("base_url") or os.environ.get("ARKER_BASE_URL")) and not (
        config.get("control_base_url") or os.environ.get("ARKER_CONTROL_BASE_URL")
    ):
        raise ValueError(
            "Named Blaxel creation with a custom base_url requires control_base_url for its public global gateway"
        )
    headers = {"Arker-VM-Name": name, "Arker-VM-Name-Mode": "get_or_create" if get_or_create else "create"}
    for key in ("region", "provider"):
        value = config.get(key) or getattr(client, key, None)
        if value:
            headers[f"x-arker-vm-{key}"] = value
    if request.get("policies") is not None:
        request = {**request, "policies": _drop_none(_policy_update(request["policies"]))}
    deadline = time.monotonic() + REQUEST_TIMEOUT_S
    while True:
        if cancelled():
            raise RoutingCancelled("Async routing was cancelled")
        try:
            wire = client._request(
                "POST",
                "/v1/named-fork",
                request,
                base_url=client._control_base_url,
                extra_headers=headers,
                preserve_nulls=True,
                retry_network_failures=get_or_create,
                request_timeout=max(0, deadline - time.monotonic()),
            )
            info = _vm_info(wire)
            return VM(client, info.vm_id, client._control_base_url, info)
        except ArkerError as error:
            raw = error.raw if isinstance(error.raw, dict) else {}
            details = raw.get("details")
            if error.status == 409 and error.code == "resource_busy":
                delay = raw.get("retry_after_seconds", 1)
                if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay <= 0:
                    delay = 1
                if time.monotonic() + delay < deadline:
                    time.sleep(delay)
                    continue
            if (
                error.status == 409
                and error.code == "conflict"
                and isinstance(details, dict)
                and details.get("resource") == "vm"
            ):
                raise creation_error(error.message, 409, "SANDBOX_ALREADY_EXISTS") from error
            raise
