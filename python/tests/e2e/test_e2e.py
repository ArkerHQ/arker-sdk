"""Live Python SDK end-to-end suite.

Every test costs real VMs, so the module-scoped ``vm`` is forked once and shared;
tests that need their own VM fork from it and delete in ``finally``.

    ARKER_API_KEY=ark_live_... \\
    ARKER_PROVIDER=aws \\
    ARKER_REGION=us-west-2 \\
    ARKER_SOURCE_VM=<source-name> \\
    uv run pytest tests/e2e/test_e2e.py -q

Or run the file directly, which hands itself to pytest:

    python tests/e2e/test_e2e.py
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sys
from collections.abc import Iterator

import pytest

from arker import VM, Arker

API_KEY = os.environ.get("ARKER_API_KEY")
BASE_URL = os.environ.get("ARKER_BASE_URL")
REGION = os.environ.get("ARKER_REGION")
PROVIDER = os.environ.get("ARKER_PROVIDER")
SOURCE_VM = os.environ.get("ARKER_SOURCE_VM")

PAYLOAD = b"hello-from-python-sdk\n"
REMOTE_PATH = "/home/user/python-sdk-e2e.txt"


@pytest.fixture(scope="module")
def arker() -> Arker:
    return Arker(api_key=API_KEY, base_url=BASE_URL, provider=PROVIDER, region=REGION)


@pytest.fixture(scope="module")
def vm(arker: Arker) -> Iterator[VM]:
    """One fork of the source VM for the whole module; deleted even on failure."""
    forked = arker.vm(SOURCE_VM).fork(name="python-sdk-e2e")
    try:
        yield forked
    finally:
        forked.delete()


def test_fork_produces_a_running_vm(vm: VM) -> None:
    assert vm.id
    assert vm.run("printf 'ready\\n'").stdout == "ready\n"


def test_fork_inherits_the_parent_filesystem(vm: VM) -> None:
    vm.sync(REMOTE_PATH, PAYLOAD)

    child = vm.fork(name="python-sdk-e2e-inherit")
    try:
        assert child.id != vm.id
        assert child.sync(REMOTE_PATH) == PAYLOAD
    finally:
        child.delete()


def test_fork_isolates_child_writes_from_the_parent(vm: VM) -> None:
    child_only = "/home/user/child-only.txt"

    child = vm.fork(name="python-sdk-e2e-isolation")
    try:
        child.sync(child_only, b"child\n")
        assert child.run(f"test -f {child_only}").exit_code == 0
        assert vm.run(f"test -f {child_only}").exit_code != 0
    finally:
        child.delete()


def test_nested_forks_inherit_the_whole_chain(vm: VM) -> None:
    """Fork a fork a fork: each level must carry every ancestor's writes."""
    vm.sync("/home/user/depth-0.txt", b"0\n")

    chain: list[VM] = []
    try:
        parent = vm
        for depth in range(1, 4):
            child = parent.fork(name=f"python-sdk-e2e-depth-{depth}")
            chain.append(child)
            child.sync(f"/home/user/depth-{depth}.txt", str(depth).encode() + b"\n")
            parent = child

        deepest = chain[-1]
        for depth in range(4):
            assert deepest.sync(f"/home/user/depth-{depth}.txt") == str(depth).encode() + b"\n"
    finally:
        for child in reversed(chain):
            child.delete()


def test_run_reports_a_failing_command(vm: VM) -> None:
    run = vm.run("printf 'to-stderr\\n' >&2; exit 3")

    assert run.exit_code == 3
    assert "to-stderr" in run.stderr


def test_a_prompt_ends_a_run_without_an_exit_code(vm: VM) -> None:
    # Its own session: the command is still running when the prompt ends the run.
    run = vm.run("echo READY; sleep 30", end_symbol="READY", session_idx=9)
    assert (run.state, run.exit_code) == ("completed", None)
    assert "READY" in run.stdout


def test_sync_round_trips_bytes(vm: VM) -> None:
    vm.sync(REMOTE_PATH, PAYLOAD)

    assert vm.sync(REMOTE_PATH) == PAYLOAD


def test_sync_streams_a_large_payload(vm: VM) -> None:
    """5MB round-trip with a digest check on both the read-back and the guest's own view."""
    payload = secrets.token_bytes(5 * 1024 * 1024)
    digest = hashlib.sha256(payload).hexdigest()
    path = "/home/user/large.bin"

    vm.sync(path, payload)

    assert hashlib.sha256(vm.sync(path)).hexdigest() == digest
    assert vm.run(f"sha256sum {path}").stdout.split()[0] == digest


def test_control_plane_works_without_placement(monkeypatch) -> None:
    """No placement still reaches the control plane, and says so for compute."""
    for name in ("ARKER_BASE_URL", "ARKER_PROVIDER", "ARKER_REGION"):
        monkeypatch.delenv(name, raising=False)
    control_only = Arker(api_key=API_KEY)

    assert control_only.whoami().org_id
    assert control_only.list_regions().regions

    with pytest.raises(ValueError, match="No placement configured"):
        control_only.vm("vm_01")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
