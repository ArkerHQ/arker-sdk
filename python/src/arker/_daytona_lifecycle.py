"""Daytona lifecycle operations backed by authoritative public VM operations."""

from __future__ import annotations

import os
import signal
import threading
import time
from urllib.parse import quote

from ._compat import UnsupportedCompatibilityOption
from ._daytona_volumes import _error as translate_error
from .computer import VM, _vm_info


def _error(function, *args, **kwargs):
    import httpx
    from daytona.common.errors import DaytonaConnectionTimeoutError

    try:
        return translate_error(function, *args, **kwargs)
    except httpx.TimeoutException as error:
        raise DaytonaConnectionTimeoutError(str(error)) from error


class Budget:
    def __init__(self, timeout, *, round_main_thread=True):
        from daytona._utils.timeout import http_timeout

        seconds = http_timeout(timeout)
        # Native sync start/fork use SIGALRM on the Unix main thread; their
        # HTTP budget rounds subsecond values to match that timer.
        if not (
            round_main_thread and hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()
        ):
            seconds = None if seconds is None else float(timeout)
        self.deadline = None if seconds is None else time.monotonic() + seconds

    def remaining(self, identifier):
        from daytona.common.errors import DaytonaTimeoutError

        if self.deadline is None:
            return None
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise DaytonaTimeoutError(
                f"Lifecycle request timed out for sandbox {identifier}; its outcome may be pending"
            )
        return left


def transition(vm, operation, budget):
    body = {"mode": "full"} if operation == "pause" else {}
    response = _error(
        vm._client._request,
        "POST",
        f"/v1/vms/{quote(vm.id, safe='')}/{operation}",
        body,
        base_url=vm.base_url,
        request_timeout=budget.remaining(vm.id),
        retry_network_failures=False,
    )
    if response.get("vm_id") != vm.id or response.get("state") != ("paused" if operation == "pause" else "running"):
        raise RuntimeError("Invalid lifecycle response")
    if operation == "pause" and response.get("mode") != "full":
        raise RuntimeError("Pause did not confirm memory preservation")


def pause(vm, timeout=60):
    if timeout is None:
        from daytona.common.errors import DaytonaError

        raise DaytonaError("Failed to pause sandbox'<' not supported between instances of 'NoneType' and 'int'")
    transition(vm, "pause", Budget(timeout, round_main_thread=False))


def start(vm, timeout=60):
    transition(vm, "resume", Budget(timeout))


def unsupported_stop():
    raise UnsupportedCompatibilityOption(
        "Daytona stop requires guest SIGTERM/SIGKILL shutdown and a durable stopped state; "
        "Arker pause preserves execution and cannot substitute for shutdown"
    )


def unsupported_archive():
    raise UnsupportedCompatibilityOption(
        "Daytona archive requires atomic stopped-state validation and filesystem offload; "
        "Arker pause does not provide that archive contract"
    )


def fork(vm, name=None, timeout=60, *, config=None):
    budget = Budget(timeout)
    request = {"layers": ["disk"], "durable": False}
    if name is None:
        child = _error(vm.fork, **request, request_timeout=budget.remaining(vm.id))
    else:
        if not isinstance(name, str):
            raise TypeError("name must be a string")
        config = config or {}
        if (config.get("base_url") or os.environ.get("ARKER_BASE_URL")) and not (
            config.get("control_base_url") or os.environ.get("ARKER_CONTROL_BASE_URL")
        ):
            raise ValueError("Named forks with a custom base_url require an explicit control_base_url")
        headers = {"Arker-VM-Name": name, "Arker-VM-Name-Mode": "create"}
        for key in ("region", "provider"):
            value = config.get(key) or getattr(vm._client, key, None)
            if value:
                headers[f"x-arker-vm-{key}"] = value
        wire = _error(
            vm._client._request,
            "POST",
            "/v1/named-fork",
            {**request, "source_vm_id": vm.id, "name": name},
            base_url=vm._client.control_base_url,
            extra_headers=headers,
            retry_network_failures=False,
            request_timeout=budget.remaining(vm.id),
        )
        info = _vm_info(wire)
        child = VM(vm._client, info.vm_id, vm._client.control_base_url, info)
    # No source resume, process replay, policy patch, or implicit child deletion.
    try:
        transition(child, "resume", budget)
    except Exception as error:
        error.sandbox_id = child.id
        if hasattr(error, "add_note"):
            error.add_note(f"Forked sandbox {child.id} did not confirm startup; retain its ID for recovery")
        raise
    return child
