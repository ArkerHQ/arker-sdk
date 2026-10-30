"""Guest prerequisite checks do not require vendor SDKs or network access."""

import gzip
import hashlib
import importlib.util
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[2] / "src/arker/_guest_blaxel_packages.py"


def isolate_process_table(bootstrap, tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    for table in ("tcp", "tcp6"):
        (proc / "net" / table).write_text("header\n")
    paths = {"/proc": proc, "/proc/net": proc / "net"}
    monkeypatch.setattr(bootstrap, "Path", lambda value: paths.get(value, Path(value)))


@pytest.fixture
def guest(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("blaxel_packages", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    module._real_directories = module._directories
    monkeypatch.setattr(module, "_directories", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_file", lambda path: path.read_bytes())
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(module, "CACHE", tmp_path / "cache")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:") as archive:
        item = tarfile.TarInfo("usr/sbin/tool")
        item.size = 4
        archive.addfile(item, io.BytesIO(b"tool"))
        link = tarfile.TarInfo("usr/sbin/iptables")
        link.type, link.linkname = tarfile.SYMTYPE, "tool"
        archive.addfile(link)
    payload = gzip.compress(buffer.getvalue())
    key = b"trusted test key"
    digest = lambda value: hashlib.sha256(value).hexdigest()
    manifest = {
        "profile": "alpine-test",
        "dependencies": {},
        "conflicts": [],
        "architecture": "x86_64",
        "alpine_release": "3.23.7",
        "key": {"name": "test.pub", "sha256": digest(key)},
        "packages": [
            {
                "name": "iptables",
                "version": "1-r0",
                "bytes": len(payload),
                "sha256": digest(payload),
                "payload_sha256": digest(
                    json.dumps(
                        sorted([("usr/sbin/tool", "sha256", digest(b"tool")), ("usr/sbin/iptables", "link", "tool")]),
                        separators=(",", ":"),
                    ).encode()
                ),
            }
        ],
    }
    monkeypatch.setattr(module, "MANIFEST", manifest)
    for name, content in {
        "etc/alpine-release": b"3.23.7\n",
        "etc/apk/keys/test.pub": key,
        "lib/apk/db/installed": b"",
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    candidate = tmp_path / "candidate.apk"
    candidate.write_bytes(payload)
    calls = []

    def complete():
        (tmp_path / "lib/apk/db/installed").write_text("P:iptables\nV:1-r0\n\n")
        (tmp_path / "usr/sbin").mkdir(parents=True, exist_ok=True)
        (tmp_path / "usr/sbin/tool").write_bytes(b"tool")
        (tmp_path / "usr/sbin/iptables").symlink_to("tool")
        module.CACHE.mkdir(exist_ok=True)
        (module.CACHE / (digest(payload) + ".apk")).write_bytes(payload)

    def run(command, **kwargs):
        assert "--keys-dir" in command
        keys = Path(command[command.index("--keys-dir") + 1])
        assert [entry.name for entry in keys.iterdir()] == ["test.pub"]
        assert (keys / "test.pub").read_bytes() == key
        calls.append((command, kwargs))
        if "add" in command:
            complete()

    monkeypatch.setattr(module.subprocess, "run", run)
    return module, manifest["profile"], candidate, calls, complete


def test_offline_install_then_idempotent_verification(guest):
    module, image, candidate, calls, _ = guest
    module.install(image, [str(candidate)])
    assert module.verify(image)
    assert len(calls) == 2
    assert "verify" in calls[0][0]
    assert {"--no-scripts", "--no-commit-hooks", "add"} <= set(calls[1][0])
    for command, options in calls:
        assert {"--no-network", "--no-cache", "--repositories-file", "/dev/null"} <= set(command)
        assert "--allow-untrusted" not in command
        assert options["check"] is True
        assert set(options["env"]) == {"PATH", "HOME"}
    candidate.unlink()
    module.install(image, [])
    assert len(calls) == 2


@pytest.mark.parametrize("damage", ["archive", "key", "image", "release", "version", "missing"])
def test_invalid_inputs_fail_before_package_execution(guest, damage):
    module, image, candidate, calls, _ = guest
    if damage == "archive":
        candidate.write_bytes(b"invalid payload")
    elif damage == "key":
        (module.ROOT / "etc/apk/keys/test.pub").write_bytes(b"untrusted")
    elif damage == "image":
        image = "blaxel/base-image:latest"
    elif damage == "release":
        (module.ROOT / "etc/alpine-release").write_text("3.24.0")
    elif damage == "version":
        (module.ROOT / "lib/apk/db/installed").write_text("P:iptables\nV:2-r0\n")
    with pytest.raises(RuntimeError):
        module.install(image, [] if damage == "missing" else [str(candidate)])
    assert calls == []


def test_signature_failure_never_installs(guest, monkeypatch):
    module, image, candidate, calls, _ = guest

    def fail(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(module.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        module.install(image, [str(candidate)])
    assert len(calls) == 1 and "verify" in calls[0] and "add" not in calls[0]
    assert not module.verify(image)


@pytest.mark.parametrize("damage", ["file", "link"])
def test_installed_tamper_is_not_repaired_or_admitted(guest, damage):
    module, image, _, calls, complete = guest
    complete()
    if damage == "file":
        (module.ROOT / "usr/sbin/tool").write_bytes(b"tampered")
    else:
        path = module.ROOT / "usr/sbin/iptables"
        path.unlink()
        path.symlink_to("other")
    with pytest.raises(RuntimeError):
        module.install(image, [])
    assert calls == []


def test_manifest_has_only_pinned_public_artifacts():
    manifest = json.loads(SOURCE.with_suffix(".json").read_text())
    assert len(manifest["packages"]) == 4
    assert sum(package["bytes"] for package in manifest["packages"]) == 418336
    for package in manifest["packages"]:
        assert package["url"] == (
            f"https://dl-cdn.alpinelinux.org/alpine/v3.23/main/x86_64/{package['name']}-{package['version']}.apk"
        )
        assert len(bytes.fromhex(package["sha256"])) == 32
        assert len(bytes.fromhex(package["payload_sha256"])) == 32


@pytest.mark.parametrize("mode,uid", [(0o100666, 0), (0o120777, 0), (0o100644, 1000)])
def test_file_verification_rejects_mutable_or_unowned_input(mode, uid):
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location("blaxel_package_security", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._directories = lambda *args, **kwargs: None
    path = SimpleNamespace(parent=None, lstat=lambda: SimpleNamespace(st_mode=mode, st_uid=uid))
    with pytest.raises(RuntimeError):
        module._file(path)


def test_unsuffixed_stage_candidate_is_rejected_before_apk(guest):
    module, image, candidate, calls, _ = guest
    stage = candidate.with_suffix("")
    candidate.rename(stage)
    with pytest.raises(RuntimeError):
        module.install(image, [str(stage)])
    assert calls == []


def test_failed_install_does_not_report_readiness(guest, monkeypatch):
    module, image, candidate, calls, _ = guest

    def fail_install(command, **kwargs):
        calls.append(command)
        if "add" in command:
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(module.subprocess, "run", fail_install)
    with pytest.raises(subprocess.CalledProcessError):
        module.install(image, [str(candidate)])
    assert len(calls) == 2
    assert not module.verify(image)


@pytest.mark.parametrize("signature_ok", [False, True])
def test_guest_ensure_requires_packages_before_isolation_or_launch(guest, monkeypatch, tmp_path, signature_ok):
    import os
    import sys
    from types import SimpleNamespace

    packages, image, candidate, _, _ = guest
    root = tmp_path / "bootstrap"
    (root / "requests").mkdir(parents=True)
    request = root / "requests/request.json"
    request.write_text("{}")
    events = []
    stop = RuntimeError("stop before daemon")

    def install(*args):
        events.append("packages")
        if not signature_ok:
            raise subprocess.CalledProcessError(1, ["apk", "verify"])

    def block():
        events.append("isolation")
        raise stop

    security = SimpleNamespace(
        load=lambda path: {"firewall_profile": image, "package_candidates": [str(candidate)]},
        private_open=lambda path, flags: os.open(path, flags, 0o600),
        private_directory=lambda path: None,
        Isolation=lambda: SimpleNamespace(block=block),
    )
    monkeypatch.setitem(sys.modules, "_guest_e2b_envd", security)
    monkeypatch.setitem(sys.modules, "_guest_blaxel_packages", packages)
    monkeypatch.setattr(packages, "install", install)
    spec = importlib.util.spec_from_file_location("guest_bootstrap_packages", SOURCE.with_name("_guest_blaxel.py"))
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    isolate_process_table(bootstrap, tmp_path, monkeypatch)
    monkeypatch.setattr(bootstrap, "ROOT", root)
    monkeypatch.setattr(bootstrap, "STATE", root / "state.json")
    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(bootstrap.subprocess, "Popen", lambda *a, **kw: pytest.fail("daemon must not launch"))
    with pytest.raises(RuntimeError if signature_ok else subprocess.CalledProcessError):
        bootstrap.ensure(request)
    assert events == (["packages", "isolation"] if signature_ok else ["packages"])


def test_cached_archive_tamper_rejected_before_payload_check(guest):
    module, image, candidate, calls, complete = guest
    complete()
    next(module.CACHE.iterdir()).write_bytes(b"tampered archive")
    with pytest.raises(RuntimeError, match="Cached firewall package checksum"):
        module.install(image, [str(candidate)])
    assert calls == []


@pytest.mark.parametrize("damage", ["parent-link", "cache-link", "writable-parent", "root-link"])
def test_directory_chain_refuses_links_and_writable_ancestors(guest, monkeypatch, tmp_path, damage):
    import os
    from types import SimpleNamespace

    module, _, _, _, _ = guest
    parent = tmp_path / "private"
    parent.mkdir()
    cache = parent / "packages"
    cache.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    original = Path.lstat

    def stat_fixture(path):
        info = original(path)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=0 if info.st_uid == os.geteuid() else info.st_uid)

    monkeypatch.setattr(Path, "lstat", stat_fixture)
    module._real_directories(cache)
    if damage == "writable-parent":
        parent.chmod(0o777)
    elif damage == "cache-link":
        cache.rmdir()
        cache.symlink_to(outside, target_is_directory=True)
    elif damage == "parent-link":
        cache.rmdir()
        parent.rmdir()
        parent.symlink_to(outside, target_is_directory=True)
    else:
        linked = tmp_path / "linked-root"
        linked.symlink_to(parent, target_is_directory=True)
        module.ROOT = linked
        cache = linked / "packages"
    with pytest.raises(RuntimeError, match="Unsafe firewall package directory"):
        module._real_directories(cache, allow_missing=True)


def test_running_daemon_prevents_reinstall_on_reconnect(guest, monkeypatch, tmp_path):
    import os
    import sys
    from types import SimpleNamespace

    packages, image, candidate, _, _ = guest
    root = tmp_path / "bootstrap"
    (root / "requests").mkdir(parents=True)
    request = root / "requests/request.json"
    request.write_text("{}")
    (root / "state.json").write_text("{}")
    security = SimpleNamespace(
        BootstrapError=RuntimeError,
        load=lambda path: (
            {"package_candidates": [str(candidate)]} if path == request else {"pid": 123, "firewall_profile": image}
        ),
        private_directory=lambda path: None,
        private_open=lambda path, flags: os.open(path, flags, 0o600),
        same_process=lambda record: True,
        Isolation=lambda: pytest.fail("must not manipulate guards while prerequisites are invalid"),
    )
    monkeypatch.setitem(sys.modules, "_guest_e2b_envd", security)
    monkeypatch.setitem(sys.modules, "_guest_blaxel_packages", packages)
    monkeypatch.setattr(packages, "install", lambda *args: pytest.fail("must not install under a running daemon"))
    spec = importlib.util.spec_from_file_location("guest_reconnect_packages", SOURCE.with_name("_guest_blaxel.py"))
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    isolate_process_table(bootstrap, tmp_path, monkeypatch)
    monkeypatch.setattr(bootstrap, "ROOT", root)
    monkeypatch.setattr(bootstrap, "STATE", root / "state.json")
    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 0)
    with pytest.raises(RuntimeError, match="firewall_change_requires_daemon_stop"):
        bootstrap.ensure(request)


@pytest.mark.parametrize("mismatch", ["architecture", "release", "missing-release"])
def test_unknown_runtime_profile_does_not_qualify_for_install(guest, monkeypatch, mismatch):
    packages, _, _, calls, _ = guest
    if mismatch == "architecture":
        monkeypatch.setattr(packages.platform, "machine", lambda: "aarch64")
    elif mismatch == "release":
        (packages.ROOT / "etc/alpine-release").write_text("3.24.0")
    else:
        (packages.ROOT / "etc/alpine-release").unlink()
    assert packages.profile() is None
    assert calls == []


@pytest.mark.parametrize("winner_dies", [False, True])
def test_loser_first_and_winner_crash_need_no_creation_receipt(guest, monkeypatch, tmp_path, winner_dies):
    import os
    import sys
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace

    packages, profile, candidate, calls, _ = guest
    root = tmp_path / "bootstrap"
    (root / "requests").mkdir(parents=True)
    resumed = Event()
    held = Event()
    admitted = RuntimeError("verified prerequisites reached isolation")

    def block():
        raise admitted

    security = SimpleNamespace(
        BootstrapError=RuntimeError,
        load=lambda path: json.loads(path.read_text()),
        private_open=lambda path, flags: os.open(path, flags, 0o600),
        private_directory=lambda path: None,
        Isolation=lambda: SimpleNamespace(block=block),
    )
    monkeypatch.setitem(sys.modules, "_guest_e2b_envd", security)
    monkeypatch.setitem(sys.modules, "_guest_blaxel_packages", packages)
    spec = importlib.util.spec_from_file_location("guest_profile_race", SOURCE.with_name("_guest_blaxel.py"))
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    isolate_process_table(bootstrap, tmp_path, monkeypatch)
    monkeypatch.setattr(bootstrap, "ROOT", root)
    monkeypatch.setattr(bootstrap, "STATE", root / "state.json")
    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(bootstrap.subprocess, "Popen", lambda *a, **kw: pytest.fail("test stops at isolation"))

    def initialize(name, candidates):
        request = root / "requests" / (name + ".json")
        # Every caller derives the profile from the runtime, with no metadata or receipt.
        request.write_text(json.dumps({"firewall_profile": packages.profile(), "package_candidates": candidates}))
        with pytest.raises(RuntimeError) as error:
            bootstrap.ensure(request)
        assert error.value is admitted

    def winner():
        held.set()
        assert resumed.wait(5)
        if not winner_dies:
            initialize("winner", [])

    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(winner)
        try:
            assert held.wait(5)
            initialize("loser", [str(candidate)])
            assert packages.verify(profile)
        finally:
            resumed.set()
            task.result()
    initialize("recovery", [])
    assert len(calls) == 2, "Only one signature verification/install pair may run"


def test_partial_install_recovers_without_replacing_installed_package(guest, monkeypatch):
    packages, profile, candidate, calls, _ = guest
    original = packages.subprocess.run

    def interrupted(command, **options):
        original(command, **options)
        if "add" in command:
            for archive in packages.CACHE.iterdir():
                archive.unlink()
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(packages.subprocess, "run", interrupted)
    with pytest.raises(subprocess.CalledProcessError):
        packages.install(profile, [str(candidate)])
    assert not packages.verify(profile)
    monkeypatch.setattr(packages.subprocess, "run", original)
    packages.install(profile, [str(candidate)])
    assert packages.verify(profile)
    assert sum("add" in command for command, _ in calls) == 1


@pytest.mark.parametrize("conflict", ["dependency", "replacement", "unowned-file"])
def test_profile_conflicts_never_execute_apk(guest, conflict):
    packages, profile, candidate, calls, _ = guest
    if conflict == "dependency":
        packages.MANIFEST["dependencies"] = {"musl": "expected"}
    elif conflict == "replacement":
        packages.MANIFEST["conflicts"] = ["ip6tables"]
        (packages.ROOT / "lib/apk/db/installed").write_text("P:ip6tables\nV:1-r0\n")
    else:
        target = packages.ROOT / "usr/sbin/tool"
        target.parent.mkdir(parents=True)
        target.write_text("customer tool")
    with pytest.raises(RuntimeError):
        packages.install(profile, [str(candidate)])
    assert calls == []


def test_control_listener_prevents_package_installation(monkeypatch):
    import sys
    from types import SimpleNamespace

    monkeypatch.setitem(sys.modules, "_guest_e2b_envd", SimpleNamespace(BootstrapError=RuntimeError))
    spec = importlib.util.spec_from_file_location("guest_listener", SOURCE.with_name("_guest_blaxel.py"))
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)

    class Proc:
        def iterdir(self):
            return []

        def __truediv__(self, table):
            return self

        def read_text(self):
            return "header\n0: 00000000:C33F 00000000:0000 0A\n"

    monkeypatch.setattr(bootstrap, "Path", lambda value: Proc())
    with pytest.raises(RuntimeError, match="firewall_change_requires_daemon_stop"):
        bootstrap.require_stopped_daemon(None)


def test_concurrent_ensure_serializes_installation(guest, monkeypatch, tmp_path):
    import fcntl
    import os
    import sys
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace

    packages, profile, candidate, calls, _ = guest
    root = tmp_path / "bootstrap"
    (root / "requests").mkdir(parents=True)
    installing, contender, release = Event(), Event(), Event()
    original_run, original_flock = packages.subprocess.run, fcntl.flock
    reached = RuntimeError("isolation reached")

    def block():
        raise reached

    security = SimpleNamespace(
        BootstrapError=RuntimeError,
        load=lambda path: json.loads(path.read_text()),
        private_open=lambda path, flags: os.open(path, flags, 0o600),
        private_directory=lambda path: None,
        Isolation=lambda: SimpleNamespace(block=block),
    )
    monkeypatch.setitem(sys.modules, "_guest_e2b_envd", security)
    monkeypatch.setitem(sys.modules, "_guest_blaxel_packages", packages)
    spec = importlib.util.spec_from_file_location("guest_profile_contention", SOURCE.with_name("_guest_blaxel.py"))
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    isolate_process_table(bootstrap, tmp_path, monkeypatch)
    monkeypatch.setattr(bootstrap, "ROOT", root)
    monkeypatch.setattr(bootstrap, "STATE", root / "state.json")
    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 0)

    def flock(fd, operation):
        if operation == fcntl.LOCK_EX and installing.is_set():
            contender.set()
        return original_flock(fd, operation)

    def run(command, **options):
        if "add" in command:
            installing.set()
            assert release.wait(5)
        return original_run(command, **options)

    monkeypatch.setattr(fcntl, "flock", flock)
    monkeypatch.setattr(packages.subprocess, "run", run)

    def ensure(name):
        path = root / "requests" / (name + ".json")
        path.write_text(json.dumps({"firewall_profile": profile, "package_candidates": [str(candidate)]}))
        with pytest.raises(RuntimeError) as result:
            bootstrap.ensure(path)
        assert result.value is reached

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ensure, "first")
        try:
            assert installing.wait(5)
            second = pool.submit(ensure, "second")
            assert contender.wait(5), "second ensure must contend on the same flock"
        finally:
            release.set()
        first.result()
        second.result()
    assert sum("add" in command for command, _ in calls) == 1
    assert packages.verify(profile)
