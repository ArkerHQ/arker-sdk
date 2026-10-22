from __future__ import annotations

import uuid
from contextlib import suppress
from typing import Any

from arker import Arker, ArkerError

E2B_AMD64_IMAGE = "e2bdev/base@sha256:8aebbf5e7d1209ebdc4bbc5999624556c2d9eebe91b9fded756ff2d688367065"
E2B_ARKER_PLATFORM = "icelake"
MARKER_PREFIX = "computesdk-e2b-baseline-"


def _run_checked(vm: Any, command: str, failure: str) -> None:
    result = vm.run(command)
    if result.exit_code != 0:
        raise RuntimeError(f"{failure}: {result.stderr.strip() or f'exit {result.exit_code}'}")


def create_e2b_baseline(
    client: Arker,
    *,
    marker: str | None = None,
    idempotency_key: str | None = None,
    attempts: int = 3,
) -> Any:
    """Create a private E2B-compatible parent; the caller owns its lifecycle."""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    marker = marker or MARKER_PREFIX + uuid.uuid4().hex
    if not marker.startswith(MARKER_PREFIX):
        raise ValueError("baseline marker must use the owned prefix")
    idempotency_key = idempotency_key or str(uuid.uuid4())
    vm = None
    last_error: BaseException | None = None
    for _ in range(attempts):
        try:
            vm = client.fork(
                image=E2B_AMD64_IMAGE,
                platforms=[E2B_ARKER_PLATFORM],
                name=marker,
                idempotency_key=idempotency_key,
            )
            break
        except ArkerError as error:
            last_error = error
            if error.status is not None and error.status < 500:
                raise
        except (ConnectionError, TimeoutError) as error:
            last_error = error
    if vm is None:
        raise RuntimeError(f"E2B baseline fork outcome is unknown; recovery key: {idempotency_key}") from last_error
    try:
        if vm.refresh().name != marker:
            raise RuntimeError("E2B baseline ownership marker mismatch")
        _run_checked(
            vm,
            'set -eu; test "$(uname -m)" = x86_64; command -v setpriv >/dev/null; '
            "! getent passwd 1000 >/dev/null; ! getent group 1000 >/dev/null; "
            "test \"$(stat -c '%u:%g:%a' /home/user)\" = 0:0:755; "
            'test -z "$(find /home/user -mindepth 1 -print -quit)"',
            "E2B baseline preflight conflict",
        )
        _run_checked(
            vm,
            "set -eu; rmdir /home/user; groupadd --gid 1000 user; "
            "useradd --uid 1000 --gid 1000 --create-home --home-dir /home/user --shell /bin/bash user; "
            "test \"$(getent passwd user)\" = 'user:x:1000:1000::/home/user:/bin/bash'; "
            "test \"$(getent group user)\" = 'user:x:1000:'; "
            "test \"$(stat -c '%u:%g:%a' /home/user)\" = 1000:1000:755",
            "E2B baseline provisioning failed",
        )
        return vm
    except BaseException:
        with suppress(BaseException):
            if vm.refresh().name == marker:
                vm.delete()
        raise


def delete_e2b_baseline(vm: Any, expected_marker: str) -> bool:
    if not expected_marker.startswith(MARKER_PREFIX) or vm.refresh().name != expected_marker:
        raise RuntimeError("refusing to delete an unproven E2B baseline")
    return bool(vm.delete().deleted)
