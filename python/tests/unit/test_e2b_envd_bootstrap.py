import hashlib
import io
import json
import os
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from arker import _guest_e2b_envd as helper

MANIFEST = json.loads(Path(helper.__file__).with_name(helper.MANIFEST_NAME).read_text())
TOKEN = "a" * 43


@pytest.fixture
def guest(tmp_path, monkeypatch):
    root = tmp_path / "state"
    monkeypatch.setattr(helper, "ROOT", root)
    monkeypatch.setattr(helper.os, "geteuid", lambda: 0)

    # The orchestration tests run without root or firewall privileges. Filesystem
    # ownership enforcement is covered separately against private_open.
    def directory(path):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)

    def private_open(path, flags=os.O_RDONLY):
        return os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)

    monkeypatch.setattr(helper, "private_directory", directory)
    monkeypatch.setattr(helper, "private_open", private_open)
    events = []
    state = SimpleNamespace(
        closed=False, auth=True, listener=True, init_status=204, release_fails=False, launches=[], initializations=[]
    )

    class Isolation:
        def block(self):
            state.closed = True
            events.append("block")

        def release_external(self, *, if_present=False):
            state.closed = False
            events.append("release")
            if state.release_fails:
                raise helper.BootstrapError("isolation_release_failed")

    class Process:
        pid = 91234
        returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            events.append("kill")
            self.returncode = -9

        def wait(self, timeout):
            assert timeout <= 5
            return self.returncode

    def launch(args, **kwargs):
        assert state.closed
        assert "-no-cgroups" in args
        assert TOKEN not in repr((args, kwargs))
        assert "ARKER_API_KEY" not in kwargs["env"]
        process = Process()
        state.launches.append(process)
        events.append("launch")
        return process

    def request(path, token=None, body=None, record=None):
        assert state.closed
        events.append(path)
        if path == "/init":
            state.initializations.append(body)
            assert body["accessToken"] == TOKEN
            return state.init_status, b""
        return 204, b""

    helper.prepare("vm-a")
    helper.save(root / helper.MANIFEST_NAME, MANIFEST)
    binary = root / "bin" / MANIFEST["artifacts"]["amd64"]["sha256"]
    monkeypatch.setattr(helper, "Isolation", Isolation)
    monkeypatch.setattr(helper, "install", lambda entry, candidate=None, **options: binary)
    monkeypatch.setattr(helper.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(helper, "ensure_user", lambda: None)
    monkeypatch.setattr(helper.subprocess, "Popen", launch)
    monkeypatch.setattr(helper, "fingerprint", lambda pid: {"pid": pid, "start": "100", "executable": str(binary)})
    monkeypatch.setattr(helper, "same_process", lambda record: True)
    monkeypatch.setattr(helper, "owns_listener", lambda record: state.listener)
    monkeypatch.setattr(helper, "process_cgroup", lambda pid: "/fixture")
    monkeypatch.setattr(helper, "request", request)
    monkeypatch.setattr(helper, "authenticated", lambda token, marker, record: state.auth)
    monkeypatch.setenv("ARKER_API_KEY", "must-never-enter-the-guest-daemon")

    def ensure(vm="vm-a", token=TOKEN, **options):
        helper.prepare(vm)
        path = root / "requests" / "input.json"
        helper.save(path, {"vm_id": vm, "token": token, **options})
        try:
            return helper.ensure(path)
        finally:
            assert not path.exists()

    return SimpleNamespace(root=root, state=state, events=events, ensure=ensure, binary=binary)


def test_fresh_start_is_guarded_secret_free_and_persistent(guest):
    result = guest.ensure()
    assert result["ready"] and not result["reused"]
    assert guest.events == ["block", "launch", "/health", "/init", "release"]
    assert TOKEN not in json.dumps(result)
    state_path = Path(result["state_path"])
    assert helper.load(state_path) == {"vm_id": "vm-a", "token": TOKEN}
    assert state_path.stat().st_mode & 0o777 == 0o600
    assert state_path.parent.stat().st_mode & 0o777 == 0o700


def test_reconnect_and_approved_fork_reuse_token_and_live_daemon(guest):
    first = guest.ensure()
    assert guest.ensure()["reused"]
    source = helper.prepare("vm-child")["existing_state_path"]
    assert source == first["state_path"]
    child = guest.ensure("vm-child")
    assert child["reused"] and len(guest.state.launches) == 1
    assert helper.load(Path(child["state_path"]))["token"] == TOKEN
    assert helper.load(guest.root / "active.json")["vm_id"] == "vm-child"
    assert guest.events.count("/init") == 1


@pytest.mark.parametrize("vm", ["vm-a", "vm-child"])
def test_live_token_mismatch_never_restarts_or_reinitializes(guest, vm):
    guest.ensure()
    with pytest.raises(helper.BootstrapError, match="token_mismatch"):
        guest.ensure(vm, "b" * 43)
    assert guest.state.closed
    assert len(guest.state.launches) == 1
    assert "kill" not in guest.events
    assert guest.events.count("/init") == 1


@pytest.mark.parametrize("failure", ["init", "authentication", "release"])
def test_failure_keeps_gate_closed_and_kills_only_new_process(guest, failure):
    if failure == "init":
        guest.state.init_status = 401
    elif failure == "authentication":
        guest.state.auth = False
    else:
        guest.state.release_fails = True
    with pytest.raises(helper.BootstrapError):
        guest.ensure()
    assert guest.state.closed
    assert guest.state.launches[0].returncode == -9
    assert guest.events[-2:] == ["block", "kill"]


def test_foreign_listener_never_receives_init_token(guest, monkeypatch):
    guest.state.listener = False
    monkeypatch.setattr(helper, "READINESS_SECONDS", 0)
    with pytest.raises(helper.BootstrapError, match="readiness_timeout"):
        guest.ensure()
    assert "/init" not in guest.events
    assert guest.state.closed and guest.state.launches[0].returncode == -9


def test_daemon_identity_mismatch_not_killed(guest):
    guest.ensure()
    active = helper.load(guest.root / "active.json")
    active["executable"] = "/unrelated/program"
    helper.save(guest.root / "active.json", active)
    with pytest.raises(helper.BootstrapError, match="daemon_identity_mismatch"):
        guest.ensure()
    assert guest.state.closed and "kill" not in guest.events


def test_ipv4_ipv6_and_local_owner_rules_preserve_unrelated_rules(monkeypatch):
    unrelated = ("-p", "tcp", "-j", "ACCEPT")
    installed = {(tool, chain): [unrelated] for tool in ("iptables", "ip6tables") for chain in ("INPUT", "OUTPUT")}
    commands = []

    def run(args):
        commands.append(args)
        tool, _, _, operation, chain, *rule = args
        rules = installed[tool, chain]
        if operation == "-I":
            rules.insert(int(rule[0]) - 1, tuple(rule[1:]))
        elif operation == "-D":
            if len(rule) == 1:
                rules.pop(int(rule[0]) - 1)
            else:
                rules.remove(tuple(rule))
        elif operation == "-S":
            return SimpleNamespace(
                returncode=0,
                stdout="\n".join(shlex.join(["-A", chain, *entry]) for entry in rules).encode(),
            )
        return SimpleNamespace(returncode=int(operation == "-C" and tuple(rule) not in rules))

    monkeypatch.setattr(helper, "run", run)
    isolation = helper.Isolation()
    isolation.block()
    # An unrelated earlier ACCEPT must not bypass a retained local guard.
    installed["iptables", "OUTPUT"].insert(0, unrelated)
    isolation.block()
    for tool in ("iptables", "ip6tables"):
        assert installed[tool, "INPUT"][0] == helper.Isolation.external[1:]
        assert installed[tool, "OUTPUT"][0] == helper.Isolation.local[1:]
        assert installed[tool, "OUTPUT"].count(helper.Isolation.local[1:]) == 1
    isolation.release_external()
    for tool in ("iptables", "ip6tables"):
        assert installed[tool, "INPUT"] == [unrelated]
        assert installed[tool, "OUTPUT"][0] == helper.Isolation.local[1:]
    assert installed["iptables", "OUTPUT"].count(unrelated) == 2
    assert all("-F" not in command and "-P" not in command for command in commands)


def test_ipv6_guard_failure_is_not_ignored(monkeypatch):
    monkeypatch.setattr(
        helper,
        "run",
        lambda args: SimpleNamespace(returncode=int(args[0] == "ip6tables"), stdout=b"-A INPUT -j DROP\n"),
    )
    with pytest.raises(helper.BootstrapError, match="isolation_unavailable"):
        helper.Isolation().block()


@pytest.mark.parametrize("machine,arch", [("x86_64", "amd64"), ("aarch64", "arm64")])
def test_literal_architecture_pins(monkeypatch, machine, arch):
    monkeypatch.setattr(helper.platform, "machine", lambda: machine)
    assert helper.artifact(MANIFEST) == (arch, MANIFEST["artifacts"][arch])
    assert len(MANIFEST["artifacts"][arch]["sha256"]) == 64


def test_corrupt_download_never_installs_and_cleans_temporary_file(guest, monkeypatch):
    monkeypatch.undo()
    root = guest.root
    monkeypatch.setattr(helper, "ROOT", root)
    monkeypatch.setattr(helper.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"corrupt"))
    monkeypatch.setattr(helper, "digest", lambda path: hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(helper.BootstrapError, match="artifact_hash_mismatch"):
        helper.install(MANIFEST["artifacts"]["amd64"])
    assert list((root / "bin").iterdir()) == []


@pytest.mark.skipif(os.geteuid() != 0, reason="Guest root-only filesystem contract")
def test_private_files_reject_symlinks_hardlinks_and_unsafe_modes(tmp_path):
    path = tmp_path / "state"
    path.write_bytes(b"preserve")
    path.chmod(0o644)
    with pytest.raises(helper.BootstrapError):
        helper.private_open(path, os.O_WRONLY | os.O_TRUNC)
    assert path.read_bytes() == b"preserve"
    path.chmod(0o600)
    with os.fdopen(helper.private_open(path), "rb") as stream:
        assert os.fstat(stream.fileno()).st_uid == 0
        assert stream.read() == b"preserve"
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(OSError):
        helper.private_open(alias)
    alias.unlink()
    os.link(path, alias)
    with pytest.raises(helper.BootstrapError):
        helper.private_open(alias)


def test_nonroot_cannot_prepare(monkeypatch):
    monkeypatch.setattr(helper.os, "geteuid", lambda: 1000)
    with pytest.raises(helper.BootstrapError, match="root_required"):
        helper.prepare("vm")


@pytest.mark.parametrize("foreign_listener", [False, True])
def test_listener_ownership_checks_both_address_families(tmp_path, monkeypatch, foreign_listener):
    proc = tmp_path / "proc"
    descriptors = proc / "42" / "fd"
    descriptors.mkdir(parents=True)
    (descriptors / "3").symlink_to("socket:[123]")
    (proc / "net").mkdir()
    for table, inode in (("tcp", "123"), ("tcp6", "456" if foreign_listener else "123")):
        (proc / "net" / table).write_text(
            "header\n" + f"0: 00000000:{helper.PORT:04X} 00000000:0000 0A 0 0 0 0 0 {inode}\n"
        )

    def path(value):
        value = Path(value)
        return proc / value.relative_to("/proc") if value.is_relative_to("/proc") else value

    monkeypatch.setattr(helper, "Path", path)
    monkeypatch.setattr(helper, "same_process", lambda record: True)
    assert helper.owns_listener({"pid": 42}) is not foreign_listener


@pytest.mark.parametrize("peer_owned", [True, False])
@pytest.mark.parametrize("self_join", [True, False])
def test_secret_requests_verify_connected_peer_before_sending(monkeypatch, peer_owned, self_join):
    events = []
    payload = b"0" if self_join else {"accessToken": TOKEN}
    options = {"body": payload, "record": {"pid": 42}}
    if self_join:
        options["content_type"] = "multipart/form-data; boundary=lifetime"

    class Response(io.BytesIO):
        status = 204

    class Connection:
        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ("127.0.0.1", helper.PORT, 1)

        def connect(self):
            events.append("connect")

        def request(self, method, path, body, headers):
            events.append("send")
            assert body == b"0" if self_join else json.loads(body) == {"accessToken": TOKEN}

        def getresponse(self):
            return Response(b"")

        def close(self):
            events.append("close")

    def verify(connection, record):
        events.append("verify")
        assert record == {"pid": 42}
        return peer_owned

    monkeypatch.setattr(helper.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(helper, "connected_peer_owned", verify)
    ticks = iter(range(10))
    monkeypatch.setattr(helper.time, "monotonic", lambda: next(ticks))
    if peer_owned:
        assert helper.request("/files" if self_join else "/init", **options) == (204, b"")
        assert events == ["connect", "verify", "send", "close"]
    else:
        with pytest.raises(helper.BootstrapError, match="connected_peer_identity_mismatch"):
            helper.request("/files" if self_join else "/init", **options)
        assert "send" not in events
        assert events[-1] == "close"


def test_invalid_token_is_consumed_without_starting_daemon(guest):
    with pytest.raises(helper.BootstrapError, match="invalid_token"):
        guest.ensure(token="too-short")
    assert not guest.state.launches


def test_preprovisioned_verified_binary_needs_no_guest_network(tmp_path, monkeypatch):
    payload = b"preprovisioned binary"
    sha = hashlib.sha256(payload).hexdigest()
    (tmp_path / "bin").mkdir()
    target = tmp_path / "bin" / sha
    target.write_bytes(payload)
    target.chmod(0o700)
    monkeypatch.setattr(helper, "ROOT", tmp_path)
    monkeypatch.setattr(helper, "digest", lambda path: hashlib.sha256(path.read_bytes()).hexdigest())

    def offline(*args, **kwargs):
        pytest.fail("Preprovisioned bootstrap attempted guest outbound access")

    monkeypatch.setattr(helper.urllib.request, "urlopen", offline)
    assert helper.install({"sha256": sha, "url": "https://example.com/envd"}) == target
    target.write_bytes(b"tampered")
    with pytest.raises(helper.BootstrapError, match="artifact_hash_mismatch"):
        helper.install({"sha256": sha, "url": "https://example.com/envd"})


@pytest.mark.parametrize("owned,peer", [(True, "0100007F"), (False, "0100007F"), (True, "0200007F")])
def test_connected_peer_requires_exact_loopback_connection_and_owned_inode(tmp_path, monkeypatch, owned, peer):
    proc = tmp_path / "proc"
    descriptors = proc / "42" / "fd"
    descriptors.mkdir(parents=True)
    (descriptors / "3").symlink_to("socket:[123]")
    (proc / "net").mkdir()
    inode = "123" if owned else "456"
    (proc / "net" / "tcp").write_text("header\n" + f"0: 0100007F:{helper.PORT:04X} {peer}:ABCD 01 0 0 0 0 0 {inode}\n")
    (proc / "net" / "tcp6").write_text("header\n")

    def path(value):
        value = Path(value)
        return proc / value.relative_to("/proc") if value.is_relative_to("/proc") else value

    monkeypatch.setattr(helper, "Path", path)
    monkeypatch.setattr(helper, "same_process", lambda record: True)
    connection = SimpleNamespace(sock=SimpleNamespace(getsockname=lambda: ("127.0.0.1", 0xABCD)))
    assert helper.connected_peer_owned(connection, {"pid": 42}) == (owned and peer == "0100007F")


@pytest.mark.parametrize("existing", [False, True])
def test_candidate_install_preserves_verified_existing_inode(tmp_path, monkeypatch, existing):
    payload = b"verified executable"
    sha = hashlib.sha256(payload).hexdigest()
    (tmp_path / "bin").mkdir()
    monkeypatch.setattr(helper, "ROOT", tmp_path)
    monkeypatch.setattr(helper, "digest", lambda path: hashlib.sha256(path.read_bytes()).hexdigest())
    target = tmp_path / "bin" / sha
    candidate = tmp_path / "bin" / (".candidate-" + "a" * 32)
    candidate.write_bytes(payload)
    if existing:
        target.write_bytes(payload)
        original_inode = target.stat().st_ino
    else:
        original_inode = candidate.stat().st_ino
    assert helper.install({"sha256": sha}, candidate) == target
    assert target.stat().st_ino == original_inode
    assert target.read_bytes() == payload
    assert candidate.exists() == existing  # ensure owns final cleanup.


@pytest.mark.parametrize("failure", [None, "init", "token"])
def test_ensure_consumes_candidate_on_success_and_failure(guest, failure):
    candidate = guest.root / "bin" / (".candidate-" + "a" * 32)
    candidate.write_bytes(b"fixture")
    request = guest.root / "requests" / "candidate.json"
    helper.save(
        request, {"vm_id": "vm-a", "token": "bad" if failure == "token" else TOKEN, "artifact_path": str(candidate)}
    )
    if failure == "init":
        guest.state.init_status = 401
    if failure:
        with pytest.raises(helper.BootstrapError):
            helper.ensure(request)
    else:
        assert helper.ensure(request)["ready"]
    assert not candidate.exists() and not request.exists()


@pytest.mark.parametrize("value", ["/tmp/.candidate-" + "a" * 32, "/root/.arker/e2b-envd/bin/../state.json", 42])
def test_candidate_path_cannot_target_unrelated_files(value):
    with pytest.raises(helper.BootstrapError, match="invalid_artifact_path"):
        helper.candidate_path(value)


def test_corrupt_candidate_cannot_replace_verified_binary(tmp_path, monkeypatch):
    payload = b"verified executable"
    sha = hashlib.sha256(payload).hexdigest()
    (tmp_path / "bin").mkdir()
    monkeypatch.setattr(helper, "ROOT", tmp_path)
    monkeypatch.setattr(helper, "digest", lambda path: hashlib.sha256(path.read_bytes()).hexdigest())
    target = tmp_path / "bin" / sha
    target.write_bytes(payload)
    inode = target.stat().st_ino
    candidate = tmp_path / "bin" / (".candidate-" + "a" * 32)
    candidate.write_bytes(b"corrupt")
    with pytest.raises(helper.BootstrapError, match="artifact_hash_mismatch"):
        helper.install({"sha256": sha}, candidate)
    assert target.stat().st_ino == inode and target.read_bytes() == payload


def test_create_envs_survive_reconnect_fork_and_daemon_restart(guest, monkeypatch):
    expected = {"CUSTOM": "private-value", "MULTILINE": "one\ntwo"}
    first = guest.ensure(envs=expected)
    assert guest.state.initializations[-1]["envVars"] == expected
    assert helper.load(Path(first["state_path"]))["envs"] == expected
    assert guest.ensure()["reused"]
    assert guest.ensure("vm-child")["reused"]
    assert len(guest.state.initializations) == 1
    monkeypatch.setattr(helper, "same_process", lambda record: False)
    restarted = guest.ensure("vm-child")
    assert not restarted["reused"]
    assert guest.state.initializations[-1]["envVars"] == expected
    assert helper.load(Path(restarted["state_path"]))["envs"] == expected


def test_explicit_empty_create_envs_clear_inherited_defaults(guest):
    guest.ensure(envs={"CUSTOM": "original"})
    child = guest.ensure("vm-child", envs={})
    assert child["reused"]
    assert guest.state.initializations[-1]["envVars"] == {}
    assert helper.load(Path(child["state_path"]))["envs"] == {}


@pytest.mark.parametrize("envs", [[], {"KEY": 1}])
def test_invalid_envs_never_reach_init(guest, envs):
    with pytest.raises(helper.BootstrapError, match="invalid_envs"):
        guest.ensure(envs=envs)
    assert guest.events == []


def test_verify_hashes_pinned_binary_and_offline_install_never_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "ROOT", tmp_path)
    monkeypatch.setattr(helper, "prepare", lambda vm: None)
    payload = b"pinned artifact"
    sha = hashlib.sha256(payload).hexdigest()
    entry = {"sha256": sha}
    monkeypatch.setattr(helper, "artifact", lambda manifest: ("amd64", entry))
    monkeypatch.setattr(helper, "load", lambda path: {})
    monkeypatch.setattr(helper, "private_open", lambda path, flags=os.O_RDONLY: os.open(path, flags | os.O_NOFOLLOW))
    monkeypatch.setattr(helper.urllib.request, "urlopen", lambda *a, **k: pytest.fail("must not download"))
    (tmp_path / "bin").mkdir()
    target = tmp_path / "bin" / sha
    assert helper.verify("vm-a") == {"verified": False, "arch": "amd64"}
    target.write_bytes(payload)
    inode = target.stat().st_ino
    assert helper.verify("vm-a")["verified"] is True
    assert helper.install(entry, offline=True) == target
    assert target.stat().st_ino == inode
    target.write_bytes(b"corrupt")
    assert helper.verify("vm-a")["verified"] is False
    target.unlink()
    with pytest.raises(helper.BootstrapError, match="artifact_missing"):
        helper.install(entry, offline=True)


@pytest.mark.parametrize(
    "uid,home,valid", [(2345, "/home/user", True), (0, "/home/user", False), (2345, "/root", False)]
)
def test_default_source_identity_requires_nonroot_user_and_expected_home(monkeypatch, uid, home, valid):
    monkeypatch.setattr(helper.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=uid, pw_dir=home))
    if valid:
        helper.ensure_user()
    else:
        with pytest.raises(helper.BootstrapError, match="unsupported_user_home"):
            helper.ensure_user()


def test_traffic_token_is_private_stable_and_not_inherited_by_child(guest):
    token = "b" * 43
    first = guest.ensure(traffic_access_token=token)
    assert token not in json.dumps(first)
    assert helper.load(Path(first["state_path"]))["traffic_access_token"] == token
    again = guest.ensure(traffic_access_token=token)
    assert again["reused"]
    assert Path(again["state_path"]).stat().st_mode & 0o777 == 0o600
    with pytest.raises(helper.BootstrapError, match="reconnect_traffic_token_mismatch"):
        guest.ensure(traffic_access_token="c" * 43)
    child = guest.ensure(vm="child", traffic_access_token="d" * 43)
    assert helper.load(Path(child["state_path"]))["traffic_access_token"] == "d" * 43
    assert helper.load(Path(first["state_path"]))["traffic_access_token"] == token
    public_child = guest.ensure(vm="public-child")
    assert "traffic_access_token" not in helper.load(Path(public_child["state_path"]))


@pytest.mark.parametrize("existing", [False, True])
def test_lifetime_bootstrap_keeps_existing_descriptor_blocked_until_monitor_publish(guest, existing):
    if existing:
        guest.ensure()
        assert not guest.state.closed
    guest.ensure(retain_activity=True)
    assert guest.state.closed
    assert guest.events[-1] != "release"


@pytest.mark.parametrize("failure", ["admission", "terminal", "lost-cgroup"])
def test_failed_lifetime_admission_cannot_reopen_existing_client_ingress(guest, monkeypatch, failure):
    from arker import _compat_e2b_bootstrap as bootstrap
    from arker._compat_e2b_lifetime import retain_envd

    guest.ensure()  # An existing descriptor and token may already be in use.
    guest.ensure(retain_activity=True)
    assert guest.state.closed
    current = helper.lifetime(json.dumps({"vm_id": "vm-a", "session_id": "owned"}))
    helper.save(helper.vm_directory("vm-a") / "monitor.json", {"lifetime": current, "cgroup": "/fixture"})
    monkeypatch.setattr(helper, "process_cgroup", lambda pid: "/other")

    def invoke(vm, operation, argument):
        return helper.lifetime(argument) if operation == "lifetime" else helper.publish(argument)

    monkeypatch.setattr(bootstrap, "guest_json", invoke)

    def run(*args, **kwargs):
        if failure == "admission":
            raise RuntimeError("admission refused")
        return SimpleNamespace(type="background", run_id="held")

    vm = SimpleNamespace(
        id="vm-a",
        run=run,
        get_run=lambda _: SimpleNamespace(
            vm_id="vm-a",
            session_id="owned",
            state="completed" if failure == "terminal" else "running",
            stdout=json.dumps({"ready": True, "lifetime": current}) + "\n",
        ),
    )
    with pytest.raises((RuntimeError, helper.BootstrapError)):
        retain_envd(vm)
    assert guest.state.closed


def test_lifetime_publish_revalidates_identity_then_opens_ingress(guest):
    guest.ensure(retain_activity=True)
    current = helper.lifetime(json.dumps({"vm_id": "vm-a", "session_id": "owned"}))
    helper.save(helper.vm_directory("vm-a") / "monitor.json", {"lifetime": current, "cgroup": "/fixture"})
    inc = current["incarnation"]
    argument = json.dumps([inc["vm_id"], inc["pid"], inc["start"], inc["boot_id"], current["session_id"]])
    assert guest.state.closed
    assert helper.publish(argument) == {"ready": True}
    assert not guest.state.closed
    assert helper.publish(argument) == {"ready": True}
    active = helper.load(guest.root / "active.json")
    helper.save(guest.root / "active.json", {**active, "start": "replaced"})
    with pytest.raises(helper.BootstrapError, match="lifetime_identity_mismatch"):
        helper.publish(argument)
    assert guest.state.closed


def test_daemon_leaving_retained_cgroup_cannot_republish_stale_ready_record(guest, monkeypatch):
    guest.ensure(retain_activity=True)
    current = helper.lifetime(json.dumps({"vm_id": "vm-a", "session_id": "owned"}))
    helper.save(helper.vm_directory("vm-a") / "monitor.json", {"lifetime": current, "cgroup": "/fixture"})
    inc = current["incarnation"]
    argument = json.dumps([inc["vm_id"], inc["pid"], inc["start"], inc["boot_id"], current["session_id"]])
    assert helper.publish(argument) == {"ready": True}
    assert not guest.state.closed
    monkeypatch.setattr(helper, "process_cgroup", lambda pid: "/other")
    with pytest.raises(helper.BootstrapError, match="lifetime_identity_mismatch"):
        helper.publish(argument)
    assert guest.state.closed
    assert helper.lifetime(json.dumps({"vm_id": "vm-a"})) == current
    assert len(guest.state.launches) == 1


def test_legacy_monitor_claim_requires_new_session_before_publication(guest):
    guest.ensure(retain_activity=True)
    current = helper.lifetime(json.dumps({"vm_id": "vm-a", "session_id": "legacy"}))
    legacy = {key: value for key, value in current.items() if key != "tracking"}
    helper.save(helper.vm_directory("vm-a") / "lifetime.json", legacy)
    unclaimed = helper.lifetime(json.dumps({"vm_id": "vm-a"}))
    assert unclaimed == {**current, "session_id": None}
    selected = helper.lifetime(json.dumps({"vm_id": "vm-a", "session_id": "tracked"}))
    assert selected == {**current, "session_id": "tracked"}
    assert guest.state.closed
