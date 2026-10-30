"""Capture an immutable checkpoint once, fan out, then prepare each child.

A server-side deletion deadline bounds checkpoint retention after client death.
This is not a durable snapshot namespace or a server-side fan-out transaction.
"""

from __future__ import annotations

import asyncio
import builtins
import math
import threading
import time
from contextlib import suppress
from copy import deepcopy

from ._compat_e2b_errors import translate_error
from ._compat_e2b_lifecycle import LIFETIME_POLICY_ID, _control, _options, _timeout, transport_timeout
from ._compat_e2b_network import _dict, _managed, remember_traffic_token, traffic_policies
from .computer import ArkerError


def fork_error(error):
    translated = translate_error(error)
    if translated is not error or not isinstance(error, ArkerError):
        return translated
    from e2b import InvalidArgumentException, RateLimitException, SandboxException, SandboxNotFoundException

    kind = {400: InvalidArgumentException, 404: SandboxNotFoundException, 429: RateLimitException}.get(
        error.status, SandboxException
    )
    result = kind(str(error))
    result.status_code = error.status
    return result


def dispose(sandbox):
    failures = []
    plane = getattr(sandbox, "_data_plane", None)
    if plane is not None:
        try:
            plane.close()
        except Exception as error:
            failures.append(error)
    try:
        sandbox._vm.delete()
    except Exception as error:
        failures.append(error)
    if failures:
        raise builtins.ExceptionGroup("Fork client cleanup failed", failures)


def fork_arker(vm, prepare, *, timeout=None, count=None, cancelled=None, **opts):
    from e2b.sandbox.sandbox_api import validate_fork_count

    validate_fork_count(count)
    seconds = 300 if timeout is None else timeout
    _timeout(seconds)
    transport = _options(opts, transport_timeout(vm))
    budget = transport["request_timeout"]
    if budget is None:
        from ._compat import UnsupportedCompatibilityOption

        raise UnsupportedCompatibilityOption("Fork requires a finite request_timeout for checkpoint reclamation")
    deadline = time.monotonic() + budget

    def control(call, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            import httpx

            raise httpx.ReadTimeout("Fork request deadline exceeded")
        return _control(call, request_timeout=remaining, **kwargs)

    source = control(vm.refresh)
    source_metadata = deepcopy(source.metadata)
    source_policies = _dict(source.policies)
    ingress = {
        name: rule for name, rule in source_policies.get("rules", {}).items() if rule and rule.get("type") == "inbound"
    }
    if source_policies.get("policies") or any(not _managed(name, rule) for name, rule in ingress.items()):
        from ._compat import UnsupportedCompatibilityOption

        raise UnsupportedCompatibilityOption("Fork cannot reconstruct custom inbound policies from redacted responses")
    public = not any(
        "e2b-traffic-access-token" in rule.get("match", {}).get("headers", {}) for rule in ingress.values()
    )
    results = []
    pending = []
    checkpoint = None

    def check_cancelled():
        if cancelled is not None and cancelled.is_set():
            raise asyncio.CancelledError

    try:
        check_cancelled()
        # Registration installs immutability and reclamation together, before visibility.
        checkpoint = control(
            vm.fork,
            immutable=True,
            metadata={"arker_e2b_checkpoint": {"version": 1, "source_vm_id": vm.id}},
            policies={
                "rules": {
                    **dict.fromkeys(ingress),
                    LIFETIME_POLICY_ID: {
                        "type": "schedule",
                        "after_created": math.ceil(deadline - time.monotonic()),
                        "action": "delete",
                    },
                }
            },
        )
        for _ in range(1 if count is None else count):
            check_cancelled()
            child = None
            try:
                policies, token = traffic_policies(
                    source_policies,
                    {
                        "rules": {
                            LIFETIME_POLICY_ID: {
                                "type": "schedule",
                                "after_created": seconds,
                                "action": "delete",
                            }
                        }
                    },
                    public,
                )
                child = control(checkpoint.fork, policies=policies, metadata=deepcopy(source_metadata))
                remember_traffic_token(child, token)
                check_cancelled()
                pending.append(child)
            except BaseException as error:
                if child is not None:
                    try:
                        child.delete()
                    except Exception as cleanup:
                        error = builtins.BaseExceptionGroup(
                            "Fork initialization and child cleanup failed", [error, cleanup]
                        )
                if not isinstance(error, Exception):
                    raise error
                pending.append(fork_error(error))
        check_cancelled()
        checkpoint.delete()
        checkpoint = None
        # No child readiness work can delay checkpoint reclamation or change the capture.
        while pending:
            child = pending.pop(0)
            if isinstance(child, Exception):
                results.append(child)
                continue
            try:
                check_cancelled()
                results.append(prepare(child))
            except BaseException as error:
                try:
                    child.delete()
                except Exception as cleanup:
                    error = builtins.BaseExceptionGroup(
                        "Fork initialization and child cleanup failed", [error, cleanup]
                    )
                if not isinstance(error, Exception):
                    raise error
                results.append(fork_error(error))
        check_cancelled()
        return results
    except BaseException as error:
        failures = [error]
        for result in results:
            if not isinstance(result, Exception):
                try:
                    dispose(result)
                except Exception as cleanup:
                    failures.append(cleanup)
        for child in pending:
            if not isinstance(child, Exception):
                try:
                    child.delete()
                except Exception as cleanup:
                    failures.append(cleanup)
        if checkpoint is not None:
            try:
                checkpoint.delete()
            except Exception as cleanup:
                failures.append(cleanup)
        if len(failures) > 1:
            raise builtins.BaseExceptionGroup("Fork failed and cleanup was incomplete", failures) from None
        raise fork_error(error) from None


async def _join_cleanup(task):
    # Once cancellation starts cleanup, repeated cancellation must not abandon it.
    while not task.done():
        with suppress(asyncio.CancelledError):
            await asyncio.shield(task)
    return task.result()


async def fork_arker_async(vm, prepare, **opts):
    cancelled = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(fork_arker, vm, prepare, cancelled=cancelled, **opts))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        results = await _join_cleanup(task)

        def cleanup():
            failures = []
            for result in results:
                if not isinstance(result, Exception):
                    try:
                        dispose(result)
                    except Exception as error:
                        failures.append(error)
            if failures:
                raise builtins.ExceptionGroup("Cancelled fork cleanup failed", failures)

        await _join_cleanup(asyncio.create_task(asyncio.to_thread(cleanup)))
        raise
