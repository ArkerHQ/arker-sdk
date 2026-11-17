"""Live tests for listing, snapshots, forks, pausing and network updates
in the E2B compatibility layer.

Run with:

    ARKER_API_KEY=... python -m pytest tests/e2e/test_e2b_lifecycle_e2e.py
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from arker import Arker
from arker.daytona import Daytona
from arker.e2b import ALL_TRAFFIC, NotSupportedException, Sandbox, SandboxQuery

if not os.environ.get("ARKER_API_KEY"):
    pytest.skip("live Arker credentials are not configured", allow_module_level=True)

if not os.environ.get("ARKER_BASE_URL"):
    os.environ.setdefault("ARKER_PROVIDER", "aws")
    os.environ.setdefault("ARKER_REGION", "us-east-1")

RUN = uuid.uuid4().hex[:8]


@pytest.fixture(scope="module")
def sbx():
    with Sandbox.create(envs={"SANDBOX_VAR": "kept"}, metadata={"run": RUN, "role": "first"}, timeout=900) as sandbox:
        yield sandbox


def test_list_pages_through_this_layers_sandboxes(sbx: Sandbox) -> None:
    with Sandbox.create(metadata={"run": RUN, "role": "second"}, timeout=300) as second:
        paginator = Sandbox.list(query=SandboxQuery(metadata={"run": RUN}, state=["running"]), limit=1)
        pages = []
        while paginator.has_next:
            pages.append(paginator.next_items())
        assert [len(page) for page in pages] == [1, 1]
        assert [page[0].sandbox_id for page in pages] == [sbx.sandbox_id, second.sandbox_id]
        assert pages[0][0].metadata == {"run": RUN, "role": "first"}
        only_second = Sandbox.list(query=SandboxQuery(metadata={"run": RUN, "role": "second"})).next_items()
        assert [info.sandbox_id for info in only_second] == [second.sandbox_id]
        assert Sandbox.list(query=SandboxQuery(metadata={"run": RUN}, state=["paused"])).next_items() == []
        # A next_token resumes a new listing where the last page ended, in either order.
        first_page = Sandbox.list(query=SandboxQuery(metadata={"run": RUN}), limit=1)
        assert [info.sandbox_id for info in first_page.next_items()] == [sbx.sandbox_id]
        resumed = Sandbox.list(query=SandboxQuery(metadata={"run": RUN}), limit=1, next_token=first_page.next_token)
        assert [info.sandbox_id for info in resumed.next_items()] == [second.sandbox_id]
        assert (resumed.has_next, resumed.next_token) == (False, None)
        newest = Sandbox.list(query=SandboxQuery(metadata={"run": RUN}), limit=1, order="desc")
        assert [info.sandbox_id for info in newest.next_items()] == [second.sandbox_id]
        older = Sandbox.list(query=SandboxQuery(metadata={"run": RUN}), order="desc", next_token=newest.next_token)
        assert [info.sandbox_id for info in older.next_items()] == [sbx.sandbox_id]


def test_list_leaves_out_other_layers_sandboxes(sbx: Sandbox) -> None:
    other = Daytona().create()
    try:
        paginator = Sandbox.list()
        listed = []
        while paginator.has_next:
            listed += [info.sandbox_id for info in paginator.next_items()]
        assert sbx.sandbox_id in listed
        assert other.id not in listed
    finally:
        other.delete()


def test_snapshot_fork_and_create_from_snapshot(sbx: Sandbox) -> None:
    sbx.files.write("state.txt", "before")
    snapshot = sbx.create_snapshot()
    try:
        assert snapshot.snapshot_id in [info.snapshot_id for info in sbx.list_snapshots().next_items()]
        listed = Sandbox.list(query=SandboxQuery(metadata={"run": RUN, "role": "first"})).next_items()
        assert [info.sandbox_id for info in listed] == [sbx.sandbox_id], "a snapshot is not listed as a sandbox"
        assert "compat-timeout" not in Arker().get_vm(snapshot.snapshot_id).policies.rules
        sbx.files.write("state.txt", "after")
        with Sandbox.create(snapshot.snapshot_id, timeout=300) as restored:
            assert restored.files.read("state.txt") == "before"
            out = restored.commands.run("echo $SANDBOX_VAR $E2B_SANDBOX_ID").stdout
            assert out == f"kept {restored.sandbox_id}\n"
        [fork] = sbx.fork(timeout=300)
        with fork:
            assert fork.files.read("state.txt") == "after"
            assert fork.commands.run("echo $SANDBOX_VAR $E2B_SANDBOX_ID").stdout == f"kept {fork.sandbox_id}\n"
            assert Arker().get_vm(fork.sandbox_id).policies.rules["compat-timeout"].after_created == 300
    finally:
        assert Sandbox.delete_snapshot(snapshot.snapshot_id)
    assert not Sandbox.delete_snapshot(snapshot.snapshot_id)
    assert not Sandbox.delete_snapshot(sbx.sandbox_id), "a sandbox is not a snapshot"
    assert sbx.is_running()


def test_pause_suspends_and_connect_resumes(sbx: Sandbox) -> None:
    sbx.commands.run("sleep 120", background=True, timeout=0)
    assert sbx.arker.refresh().state == "running"
    assert Sandbox.pause(sbx.sandbox_id, keep_memory=True)
    deadline = time.monotonic() + 30
    while sbx.arker.refresh().state != "idle":
        assert time.monotonic() < deadline, "pause did not suspend the sandbox"
        time.sleep(1)
    assert "compat-timeout" not in sbx.arker.refresh().policies.rules
    resumed = Sandbox.connect(sbx.sandbox_id, timeout=900)
    assert resumed.commands.run("cat state.txt").stdout == "after"
    with pytest.raises(NotSupportedException, match="on_resume"):
        sbx.connect(on_resume="reboot")


def test_update_network_replaces_the_rules(sbx: Sandbox, reachable) -> None:
    def run(cmd: str) -> str:
        return sbx.commands.run(cmd).stdout

    sbx.update_network({"allow_out": ["1.1.1.1"], "deny_out": [ALL_TRAFFIC]})
    assert reachable(run, "https://1.1.1.1", "https://example.com") == {
        "https://1.1.1.1": True,
        "https://example.com": False,
    }
    Sandbox.update_network(sbx.sandbox_id, {"deny_out": ["1.1.1.1"]})
    assert reachable(run, "https://1.1.1.1", "https://example.com") == {
        "https://1.1.1.1": False,
        "https://example.com": True,
    }
    sbx.update_network({})
    assert reachable(run, "https://1.1.1.1", "https://example.com") == {
        "https://1.1.1.1": True,
        "https://example.com": True,
    }
