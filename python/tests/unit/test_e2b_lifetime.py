import copy
import hashlib
import json
import os
import select
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from arker import _compat_e2b_bootstrap as bootstrap
from arker import _guest_e2b_envd as guest
from arker._compat_e2b_lifetime import retain_envd


def identity(vm="vm-a", session="owned"):
    return {"incarnation": {"vm_id": vm, "pid": 123, "start": "456", "boot_id": "boot"}, "session_id": session}


class VM:
    id = "vm-a"

    def __init__(self):
        self.current = identity()
        self.calls = []
        self.deleted = []
        self.state = "running"

    def run(self, command, **options):
        self.calls.append((command, options))
        return SimpleNamespace(type="background", run_id="shared-run")

    def get_run(self, run_id):
        return SimpleNamespace(
            vm_id=self.id,
            session_id=self.current["session_id"],
            state=self.state,
            stdout=json.dumps({"ready": True, "lifetime": self.current}) + "\n",
        )

    def create_session(self):
        return SimpleNamespace(session_id="candidate")

    def delete_session(self, value):
        self.deleted.append(value)


@pytest.fixture
def vm(monkeypatch):
    value = VM()
    monkeypatch.setattr(
        bootstrap,
        "guest_json",
        lambda vm, operation, argument: {"ready": True} if operation == "publish" else copy.deepcopy(value.current),
    )
    return value


def test_reconnect_replays_same_run_without_deadline_or_default_session(vm):
    assert retain_envd(vm) == retain_envd(vm) == "shared-run"
    assert vm.calls[0] == vm.calls[1]
    command, options = vm.calls[0]
    argument = '["vm-a",123,"456","boot","owned"]'
    assert argument in command
    assert options == {
        "session_id": "owned",
        "timeout": 0,
        "time_to_background": 0,
        "end_after_all_subprocesses": True,
        "idempotency_key": hashlib.sha256(argument.encode()).hexdigest(),
    }
    assert not vm.deleted


def test_concurrent_claim_loser_only_deletes_unselected_session(vm, monkeypatch):
    responses = iter([identity(session=None), identity(session="winner")])
    monkeypatch.setattr(
        bootstrap,
        "guest_json",
        lambda vm, operation, argument: {"ready": True} if operation == "publish" else next(responses),
    )
    vm.current = identity(session="winner")
    retain_envd(vm)
    assert vm.deleted == ["candidate"]


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled"])
def test_terminal_run_cannot_supply_stale_ready_ack(vm, state):
    vm.state = state
    with pytest.raises(RuntimeError, match="ended before readiness"):
        retain_envd(vm)
    assert not vm.deleted  # A reconnect does not own the shared run or session.


def test_cross_vm_run_ack_rejected(vm):
    original = vm.get_run

    def wrong(run_id):
        result = original(run_id)
        result.vm_id = "parent"
        return result

    vm.get_run = wrong
    with pytest.raises(RuntimeError, match="different VM"):
        retain_envd(vm)


def test_fork_identity_gets_distinct_host_key(vm):
    retain_envd(vm)
    vm.id = "child"
    vm.current = identity("child", "child-session")
    retain_envd(vm)
    assert vm.calls[0][1]["idempotency_key"] != vm.calls[1][1]["idempotency_key"]
    assert not vm.deleted


@pytest.mark.parametrize("cancel_monitor", [False, True])
def test_pidfd_monitor_and_guest_session_claim(tmp_path, monkeypatch, cancel_monitor):
    if not hasattr(os, "pidfd_open") or os.geteuid() != 0:
        pytest.skip("root-owned daemon identity requires Linux and root")
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(guest, "ROOT", root)
    monkeypatch.setattr(guest, "owns_listener", lambda _: True)
    # Ownership checks have dedicated bootstrap tests; this test also runs non-root.
    monkeypatch.setattr(guest, "private_open", lambda path, flags=os.O_RDONLY: os.open(path, flags, 0o600))
    daemon = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    monitor = None
    try:
        active = {**guest.fingerprint(daemon.pid), "vm_id": "parent"}
        guest.vm_directory("parent").mkdir(parents=True)
        guest.save(root / "active.json", active)
        guest.save(guest.vm_directory("parent") / "state.json", {"token": "fixture"})
        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = list(
                pool.map(lambda n: guest.lifetime(json.dumps({"vm_id": "parent", "session_id": str(n)})), range(4))
            )
        assert len({item["session_id"] for item in claims}) == 1
        held = claims[0]
        # A fork updates only its own VM-bound descriptor, never selects the copied parent session.
        guest.vm_directory("child").mkdir()
        guest.save(root / "active.json", {**active, "vm_id": "child"})
        child = guest.lifetime('{"vm_id":"child"}')
        assert child["session_id"] is None
        with pytest.raises(guest.BootstrapError, match="daemon_identity_mismatch"):
            guest.lifetime('{"vm_id":"parent"}')
        guest.save(root / "active.json", active)
        inc = held["incarnation"]
        argument = json.dumps(["parent", inc["pid"], inc["start"], inc["boot_id"], held["session_id"]])
        # Run the actual monitor in a separate process; only listener discovery is substituted.
        program = (
            "from arker import _guest_e2b_envd as g; from pathlib import Path; import sys; "
            "g.ROOT=Path(sys.argv[1]); g.owns_listener=lambda _:True; "
            "g.bind_lifetime_cgroup=lambda active, token: '/fixture'; g.monitor(sys.argv[2])"
        )
        monitor = subprocess.Popen(
            [sys.executable, "-c", program, str(root), argument], stdout=subprocess.PIPE, text=True
        )
        assert select.select([monitor.stdout], [], [], 5)[0], "monitor failed to acknowledge"
        assert json.loads(monitor.stdout.readline()) == {"ready": True, "lifetime": held}
        os.kill(monitor.pid, signal.SIGSTOP)
        os.kill(monitor.pid, signal.SIGCONT)
        assert monitor.poll() is None
        if cancel_monitor:
            monitor.terminate()
            assert monitor.wait(timeout=5) == -signal.SIGTERM
            assert daemon.poll() is None  # Cancelling a hold must not kill envd or its processes.
        daemon.terminate()
        daemon.wait(timeout=5)
        if not cancel_monitor:
            assert monitor.wait(timeout=5) == 0
        with pytest.raises(guest.BootstrapError, match="daemon_identity_mismatch"):
            guest.lifetime('{"vm_id":"parent"}')
    finally:
        for process in (monitor, daemon):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_async_bootstrap_cancellation_drains_registration_without_cancelling_shared_hold(monkeypatch):
    import asyncio
    import threading

    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def prepare(vm, **options):
        started.set()
        assert release.wait(5)
        finished.set()
        return ("descriptor", {"ready": True})

    monkeypatch.setattr(bootstrap, "bootstrap_envd", prepare)

    async def check():
        task = asyncio.create_task(bootstrap.bootstrap_envd_async(object()))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(check())


def test_lost_run_response_reconnect_replays_without_cleanup_of_uncertain_owner(vm):
    original = vm.run

    def lost(command, **options):
        original(command, **options)
        raise ConnectionError("response lost")

    vm.run = lost
    with pytest.raises(ConnectionError, match="response lost"):
        retain_envd(vm)
    vm.run = original
    assert retain_envd(vm) == "shared-run"
    assert vm.calls[0] == vm.calls[1]
    assert not vm.deleted


def test_repeated_concurrent_connects_replay_only_the_selected_monitor(vm):
    with ThreadPoolExecutor(max_workers=8) as pool:
        runs = list(pool.map(lambda _: retain_envd(vm), range(32)))
    assert runs == ["shared-run"] * 32
    assert len(vm.calls) == 32
    assert all(call == vm.calls[0] for call in vm.calls)
    assert not vm.deleted


@pytest.mark.parametrize("moved", [False, True])
def test_cgroup_binding_verifies_membership_after_move(tmp_path, monkeypatch, moved):
    from pathlib import Path

    leaf = tmp_path / "session"
    leaf.mkdir()
    (leaf / "cgroup.procs").write_text(str(os.getpid()))
    monkeypatch.setattr(guest, "Path", lambda path: tmp_path if path == "/sys/fs/cgroup" else Path(path))
    monkeypatch.setattr(guest, "process_cgroup", lambda pid: "/session" if pid == os.getpid() or moved else "/other")
    requests = []

    def upload(path, **options):
        requests.append((path, options))
        assert options["record"] == {"pid": 123}
        assert options["body"].endswith(b"\r\n0\r\n--lifetime--\r\n")
        assert b"123" not in options["body"]
        return 200, b""

    monkeypatch.setattr(guest, "request", upload)
    if moved:
        assert guest.bind_lifetime_cgroup({"pid": 123}, "fixture") == "/session"
    else:
        with pytest.raises(guest.BootstrapError, match="lifetime_cgroup_mismatch"):
            guest.bind_lifetime_cgroup({"pid": 123}, "fixture")
    assert (leaf / "cgroup.procs").read_text() == str(os.getpid())
    assert len(requests) == 1
