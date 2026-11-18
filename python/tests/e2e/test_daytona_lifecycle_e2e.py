"""Live tests for listing, names, start and stop, labels, network updates,
resizing, environment, snapshots and forks in the Daytona
compatibility layer.

Run with:

    ARKER_API_KEY=... python -m pytest tests/e2e/test_daytona_lifecycle_e2e.py
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from arker import Arker
from arker.daytona import (
    CreateSandboxFromSnapshotParams,
    CreateSnapshotParams,
    Daytona,
    DaytonaNotFoundError,
    DaytonaNotSupportedError,
    DaytonaValidationError,
    ListSandboxesQuery,
    Resources,
)

if not os.environ.get("ARKER_API_KEY"):
    pytest.skip("live Arker credentials are not configured", allow_module_level=True)

if not os.environ.get("ARKER_BASE_URL"):
    os.environ.setdefault("ARKER_PROVIDER", "aws")
    os.environ.setdefault("ARKER_REGION", "us-east-1")

RUN = uuid.uuid4().hex[:8]


@pytest.fixture(scope="module")
def daytona() -> Daytona:
    return Daytona()


@pytest.fixture(scope="module")
def sandbox(daytona: Daytona):
    sandbox = daytona.create(
        CreateSandboxFromSnapshotParams(
            name=f"daytona-e2e-{RUN}", labels={"run": RUN}, env_vars={"SANDBOX_VAR": "kept"}
        )
    )
    yield sandbox
    daytona.delete(sandbox)


def _ids(sandboxes) -> list[str]:
    return [sandbox.id for sandbox in sandboxes]


def test_list_labels_and_names(daytona: Daytona, sandbox) -> None:
    assert _ids(daytona.list(ListSandboxesQuery(labels={"run": RUN}))) == [sandbox.id]
    assert sandbox.set_labels({"run": RUN, "stage": "two"}) == {"run": RUN, "stage": "two"}
    assert _ids(daytona.list({"labels": {"stage": "two", "run": RUN}})) == [sandbox.id]
    assert _ids(daytona.list({"labels": {"run": RUN, "stage": "three"}})) == []
    assert _ids(daytona.list(ListSandboxesQuery(name=f"DAYTONA-E2E-{RUN}"))) == [sandbox.id]
    assert _ids(daytona.list(ListSandboxesQuery(name=f"no-such-{RUN}"))) == []
    named = daytona.get(f"daytona-e2e-{RUN}")
    assert named.id == sandbox.id
    assert named.labels == {"run": RUN, "stage": "two"}
    assert named.env == {"SANDBOX_VAR": "kept"}
    assert _ids(daytona.list({"labels": {"run": RUN}, "states": ["started"]})) == [sandbox.id]
    assert _ids(daytona.list({"labels": {"run": RUN}, "states": ["archived"]})) == []
    assert _ids(daytona.list({"labels": {"run": RUN}, "is_public": False})) == [sandbox.id]
    assert _ids(daytona.list({"labels": {"run": RUN}, "is_public": True})) == []
    created = Arker().get_vm(sandbox.id).created_at
    after = datetime.fromisoformat(created.replace("Z", "+00:00"))
    assert _ids(daytona.list({"labels": {"run": RUN}, "created_at_after": after - timedelta(minutes=1)})) == [
        sandbox.id
    ]
    assert _ids(daytona.list({"labels": {"run": RUN}, "created_at_before": after - timedelta(minutes=1)})) == []
    assert _ids(daytona.list({"labels": {"run": RUN}, "min_cpu": 1, "max_cpu": 64})) == [sandbox.id]
    assert _ids(daytona.list({"labels": {"run": RUN}, "min_memory_gib": 4096})) == []
    with pytest.raises(DaytonaNotSupportedError, match="auto_destroy_at_after"):
        list(daytona.list({"auto_destroy_at_after": after}))


def _assert_suspends(sandbox, action) -> None:
    session = sandbox.arker.create_session()
    sandbox.arker.run("sleep 120", session_id=session.session_id, time_to_background=0)
    assert sandbox.arker.refresh().state == "running"
    action()
    deadline = time.monotonic() + 30
    while sandbox.arker.refresh().state != "idle":
        assert time.monotonic() < deadline, f"{action.__name__} did not suspend the sandbox"
        time.sleep(1)


def test_stop_pause_and_archive_suspend(daytona: Daytona, sandbox) -> None:
    _assert_suspends(sandbox, lambda: daytona.stop(sandbox))
    assert sandbox.state == "stopped"
    sandbox.wait_for_sandbox_stop()
    daytona.start(sandbox)
    sandbox.wait_for_sandbox_start()
    assert sandbox.state == "started"
    assert sandbox.process.exec("echo $SANDBOX_VAR").result == "kept\n"
    _assert_suspends(sandbox, sandbox.pause)
    assert sandbox.state == "paused"
    _assert_suspends(sandbox, sandbox.archive)
    assert sandbox.state == "archived"
    sandbox.recover()
    sandbox.refresh_activity()
    assert sandbox.state == "started"
    assert sandbox.process.exec("echo $SANDBOX_VAR").result == "kept\n"


def test_env_and_directories(daytona: Daytona, sandbox) -> None:
    sandbox.update_env({"NEW_VAR": "it's new"}, unset=["SANDBOX_VAR"])
    assert sandbox.process.exec('echo "$NEW_VAR|$SANDBOX_VAR"').result == "it's new|\n"
    sandbox.refresh_data()
    assert sandbox.env == {"NEW_VAR": "it's new"}
    sandbox.update_env({"SANDBOX_VAR": "kept"}, unset=["NEW_VAR"])
    assert sandbox.get_user_home_dir() == "/root"
    assert sandbox.get_user_root_dir() == "/root"
    assert sandbox.get_work_dir().startswith("/")


def test_set_public_opens_existing_ports(daytona: Daytona, sandbox) -> None:
    sandbox.fs.upload_file(b"served", "/tmp/www/index.html")
    sandbox.process.exec("cd /tmp/www && (nohup python3 -m http.server 3000 >/dev/null 2>&1 &)")
    url = sandbox.get_preview_link(3000).url + "/index.html"
    assert _status(url) == 401
    sandbox.set_public(True)
    assert sandbox.public
    assert daytona.get(sandbox.id).public
    deadline = time.monotonic() + 30
    while _status(url) != 200:
        assert time.monotonic() < deadline, f"{url} never served without credentials"
        time.sleep(1)


def _status(url: str) -> int | None:
    try:
        return urllib.request.urlopen(url, timeout=10).status
    except urllib.error.HTTPError as error:
        return error.code
    except OSError:
        return None


def test_update_network_settings(sandbox, reachable) -> None:
    def run(cmd: str) -> str:
        return sandbox.process.exec(cmd).result

    urls = ("https://1.1.1.1", "https://example.com", "https://github.com")
    other = Daytona().get(sandbox.id)
    sandbox.update_network_settings(network_block_all=True)
    assert sandbox.network_block_all is True
    assert other.network_block_all is False
    other.refresh_data()
    assert other.network_block_all is True
    assert reachable(run, *urls) == dict.fromkeys(urls, False)
    sandbox.update_network_settings(network_allow_list="1.1.1.1/32")
    assert sandbox.network_block_all is False
    assert reachable(run, *urls) == {"https://1.1.1.1": True, "https://example.com": False, "https://github.com": False}
    sandbox.update_network_settings(domain_allow_list="example.com")
    assert reachable(run, *urls) == {"https://1.1.1.1": False, "https://example.com": True, "https://github.com": False}
    sandbox.update_network_settings(network_block_all=False)
    assert reachable(run, *urls) == {"https://1.1.1.1": True, "https://example.com": True, "https://github.com": True}
    with pytest.raises(DaytonaValidationError):
        sandbox.update_network_settings()


def test_list_sorts_and_filters_by_snapshot(daytona: Daytona, sandbox) -> None:
    name = f"daytona-sorted-{RUN}"
    sandbox.create_snapshot(name)
    try:
        second = daytona.create(CreateSandboxFromSnapshotParams(snapshot=name, labels={"run": RUN}))
        try:
            assert _ids(daytona.list({"labels": {"run": RUN}, "snapshots": [name]})) == [second.id]
            ordered = [sandbox.id, second.id]
            assert _ids(daytona.list({"labels": {"run": RUN}, "sort": "createdAt", "order": "asc"})) == ordered
            assert _ids(daytona.list({"labels": {"run": RUN}, "sort": "createdAt", "order": "desc"})) == ordered[::-1]
            with pytest.raises(DaytonaValidationError):
                list(daytona.list({"sort": "colour"}))
        finally:
            second.delete()
    finally:
        daytona.snapshot.delete(name)


def test_ttl_and_idle_intervals(daytona: Daytona) -> None:
    sandbox = daytona.create()
    try:
        sandbox.set_ttl(30)
        rule = Arker().get_vm(sandbox.id).policies.rules["compat-timeout"]
        assert rule.action == "delete"
        remaining = datetime.fromisoformat(rule.at.replace("Z", "+00:00")) - datetime.now(timezone.utc)
        assert timedelta(minutes=29) < remaining <= timedelta(minutes=30)
        sandbox.set_ttl(0)
        assert "compat-timeout" not in Arker().get_vm(sandbox.id).policies.rules
        with pytest.raises(DaytonaValidationError):
            sandbox.set_ttl(-1)
        for method in ("set_autostop_interval", "set_auto_pause_interval", "set_auto_archive_interval"):
            with pytest.warns(UserWarning, match=f"Sandbox.{method}"):
                getattr(sandbox, method)(15)
        with pytest.warns(UserWarning, match="set_auto_delete_interval"):
            sandbox.set_auto_delete_interval(-1)
        sandbox.wait_for_resize_complete()
    finally:
        sandbox.delete()


def test_resize(sandbox) -> None:
    sandbox.resize(Resources(cpu=2, memory=2))
    resources = Arker().get_vm(sandbox.id).resources
    assert (resources.vcpu, resources.memory_mib) == (2, 2048)
    with pytest.raises(DaytonaNotSupportedError, match="gpu"):
        sandbox.resize(Resources(gpu=1))


def test_snapshot_and_fork(daytona: Daytona, sandbox) -> None:
    sandbox.fs.upload_file(b"before", "/tmp/state.txt")
    name = f"daytona-snapshot-{RUN}"
    sandbox.create_snapshot(name)
    try:
        assert name not in [listed.name for listed in daytona.list()]
        assert "compat-timeout" not in Arker().get_vm(name).policies.rules
        sandbox.fs.upload_file(b"after", "/tmp/state.txt")
        restored = daytona.create(CreateSandboxFromSnapshotParams(snapshot=name))
        try:
            assert restored.fs.download_file("/tmp/state.txt") == b"before"
            assert restored.process.exec("echo $SANDBOX_VAR").result == "kept\n"
        finally:
            restored.delete()
        fork = sandbox.fork(name=f"daytona-fork-{RUN}")
        try:
            assert fork.id != sandbox.id and fork.name == f"daytona-fork-{RUN}"
            assert fork.fs.download_file("/tmp/state.txt") == b"after"
            assert fork.labels == sandbox.labels
            assert fork.process.exec("echo $SANDBOX_VAR").result == "kept\n"
        finally:
            fork.delete()
    finally:
        Arker().get_vm(name).delete()


def test_snapshot_service(daytona: Daytona, sandbox) -> None:
    name = f"daytona-image-{RUN}"
    logs: list[str] = []
    params = CreateSnapshotParams(name=name, image="node:22-slim", resources=Resources(cpu=2))
    snapshot = daytona.snapshot.create(params, on_logs=logs.append)
    try:
        assert (snapshot.name, snapshot.image_name, snapshot.state, snapshot.cpu) == (name, "node:22-slim", "active", 2)
        assert logs
        assert daytona.snapshot.get(name).id == snapshot.id
        assert daytona.snapshot.activate(name).name == name
        assert name in [listed.name for listed in daytona.snapshot.list(limit=1000).items]
        assert name not in [listed.name for listed in daytona.list()]
        of_sandbox = f"daytona-of-sandbox-{RUN}"
        sandbox.create_snapshot(of_sandbox)
        try:
            page = daytona.snapshot.list(source_sandbox_id=sandbox.id, limit=1)
            assert ([listed.name for listed in page.items], page.total, page.total_pages) == ([of_sandbox], 1, 1)
            assert daytona.snapshot.get(of_sandbox).source_sandbox_id == sandbox.id
        finally:
            daytona.snapshot.delete(of_sandbox)
        started = daytona.create(CreateSandboxFromSnapshotParams(snapshot=name))
        try:
            assert started.process.exec("node --version").result.startswith("v22.")
        finally:
            started.delete()
    finally:
        daytona.snapshot.delete(snapshot)
    with pytest.raises(DaytonaNotFoundError):
        daytona.snapshot.get(name)
    with pytest.raises(DaytonaNotFoundError):
        daytona.snapshot.get(sandbox.id)
    with pytest.raises(DaytonaValidationError):
        daytona.snapshot.list(page=0)


def _port_url(sandbox_id: str, port: int) -> str:
    label, _, domain = Arker().get_vm(sandbox_id).hostname.partition(".")
    return f"https://{label}-{port}.{domain}/index.html"


def test_a_sandbox_from_a_snapshot_takes_none_of_its_ports_or_network_rules(daytona: Daytona, reachable) -> None:
    parent = daytona.create(CreateSandboxFromSnapshotParams(public=True, network_allow_list="1.1.1.1/32"))
    name = f"daytona-ports-{RUN}"
    try:
        parent.fs.upload_file(b"parent's page", "/tmp/www/index.html")
        parent.process.exec("cd /tmp/www && (nohup python3 -m http.server 3000 >/dev/null 2>&1 &)")
        url = parent.get_preview_link(3000).url + "/index.html"
        deadline = time.monotonic() + 30
        while _status(url) != 200:
            assert time.monotonic() < deadline, f"{url} never served without credentials"
            time.sleep(1)
        parent.create_snapshot(name)
        child = daytona.create(CreateSandboxFromSnapshotParams(snapshot=name))
        try:
            # The child runs the parent's server, but is not public and opened no port.
            assert child.process.exec("curl -s localhost:3000/index.html").result == "parent's page"
            assert _status(_port_url(child.id, 3000)) != 200
            assert not [rule for rule in Arker().get_vm(child.id).policies.rules if rule.startswith("compat-")]
            assert reachable(lambda cmd: child.process.exec(cmd).result, "https://example.com") == {
                "https://example.com": True
            }
        finally:
            child.delete()
    finally:
        parent.delete()
        Arker().get_vm(name).delete()


def test_multi_line_envs_survive_reconnecting_and_snapshots(daytona: Daytona) -> None:
    pem = '-----BEGIN KEY-----\nab\'c "d" $e\n-----END KEY-----\n'
    sandbox = daytona.create(CreateSandboxFromSnapshotParams(env_vars={"PEM": pem, "PLAIN": "x"}))
    name = f"daytona-pem-{RUN}"
    try:
        assert sandbox.process.exec('printf %s "$PEM"').result == pem
        assert daytona.get(sandbox.id).env == {"PEM": pem, "PLAIN": "x"}
        sandbox.create_snapshot(name)
        child = daytona.create(CreateSandboxFromSnapshotParams(snapshot=name, env_vars={"PLAIN": "y"}))
        try:
            assert daytona.get(child.id).env == {"PEM": pem, "PLAIN": "y"}
            assert child.process.exec('printf %s "$PEM"').result == pem
        finally:
            child.delete()
    finally:
        sandbox.delete()
        Arker().get_vm(name).delete()
