from __future__ import annotations

import uuid
from typing import Any

from arker import Arker, ArkerError

E2B_AMD64_IMAGE = "e2bdev/base@sha256:8aebbf5e7d1209ebdc4bbc5999624556c2d9eebe91b9fded756ff2d688367065"
MARKER_PREFIX = "computesdk-e2b-baseline-"


def _run_checked(vm: Any, command: str, failure: str) -> None:
    result = vm.run(command, timeout=300)
    if result.exit_code != 0:
        raise RuntimeError(f"{failure}: {result.stderr.strip() or f'exit {result.exit_code}'}")


def create_e2b_baseline(
    client: Arker,
    *,
    platform: str | None = None,
    marker: str | None = None,
    idempotency_key: str | None = None,
    attempts: int = 3,
) -> Any:
    """Prepare an owned pinned x86 image; omitted platform uses regional scheduling.

    Pass the returned ID as ARKER_SOURCE_VM_ID for comparisons and delete the
    parent with delete_e2b_baseline in finally. No existing source is modified.
    """
    if platform is not None and (not isinstance(platform, str) or not platform.strip()):
        raise ValueError("platform must be a nonempty string")
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
                **({"platforms": [platform]} if platform is not None else {}),
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
            'set -eu; test "$(id -u)" = 0; test "$(uname -m)" = x86_64; '
            "! getent passwd 1000 >/dev/null; ! getent group 1000 >/dev/null; "
            "test \"$(stat -c '%u:%g:%a' /home/user)\" = 0:0:755; "
            'test -z "$(find /home/user -mindepth 1 -print -quit)"',
            "E2B baseline preflight conflict",
        )
        _run_checked(
            vm,
            r"""set -eu
. /etc/os-release
case "$ID" in debian|ubuntu) ;; *) echo unsupported_baseline_distro >&2; exit 1 ;; esac
command -v apt-get >/dev/null
command -v dpkg >/dev/null
test "$(dpkg --print-architecture)" = amd64
missing=0
for tool in iptables ip6tables python3 setpriv useradd groupadd getent; do
    command -v "$tool" >/dev/null || missing=1
done
if test "$missing" = 1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        iptables python3 util-linux passwd libc-bin ca-certificates
fi
for tool in iptables ip6tables python3 setpriv useradd groupadd getent; do
    command -v "$tool" >/dev/null
done
""",
            "E2B baseline prerequisites unavailable",
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
        _run_checked(
            vm,
            r"""set -eu
python3 -c 'import ctypes, fcntl, hashlib, http.client, ipaddress, json, pwd, socket, ssl, subprocess, urllib.request'
for tool in iptables ip6tables; do
    chain="E2B_BASELINE_$$"
    "$tool" -w 2 -N "$chain"
    trap '"$tool" -w 2 -F "$chain"; "$tool" -w 2 -X "$chain"' EXIT
    "$tool" -w 2 -A "$chain" -o lo -p tcp --dport 49983 -m owner ! --uid-owner 0 \
        -m comment --comment e2b-baseline-check -j DROP
    "$tool" -w 2 -C "$chain" -o lo -p tcp --dport 49983 -m owner ! --uid-owner 0 \
        -m comment --comment e2b-baseline-check -j DROP
    "$tool" -w 2 -S INPUT >/dev/null
    "$tool" -w 2 -S OUTPUT >/dev/null
    "$tool" -w 2 -F "$chain"
    "$tool" -w 2 -X "$chain"
    trap - EXIT
done
""",
            "E2B baseline isolation capability unavailable",
        )
        from arker._compat_e2b_bootstrap import bootstrap_envd

        bootstrap_envd(vm)
        return vm
    except BaseException as preparation_error:
        try:
            delete_e2b_baseline(vm, marker)
        except BaseException as cleanup_error:
            raise cleanup_error from preparation_error
        raise


def delete_e2b_baseline(vm: Any, expected_marker: str) -> bool:
    if not expected_marker.startswith(MARKER_PREFIX) or vm.refresh().name != expected_marker:
        raise RuntimeError("refusing to delete an unproven E2B baseline")
    return bool(vm.delete().deleted)
