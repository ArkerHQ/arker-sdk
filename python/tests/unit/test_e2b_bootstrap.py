import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from arker import _compat_e2b_bootstrap as bootstrap

TOKEN = "a" * 43
ROOT = "/root/.arker/e2b-envd"


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("envs", [None, {}, {"CUSTOM": "private-value"}])
def test_bootstrap_uses_secret_sync_and_recovers_existing_token(monkeypatch, tmp_path, existing, envs):
    writes, commands = [], []
    prepared = {
        "root_path": ROOT,
        "request_directory": ROOT + "/requests",
        "state_path": ROOT + "/state.json",
        "existing_state_path": ROOT + "/state.json" if existing else None,
        "arch": "arm64",
    }
    ready = {"ready": True, "reused": existing, "port": 49983, "version": "0.10.2"}

    def run(command, **options):
        commands.append(command)
        assert TOKEN not in command
        if " prepare " in command:
            result = prepared
        elif " verify " in command:
            result = {"verified": existing, "arch": "arm64"}
        elif " ensure " in command:
            result = ready
        else:
            return SimpleNamespace(exit_code=1)
        assert options == {"timeout": 120, "end_after_all_subprocesses": False}
        return SimpleNamespace(exit_code=0, stdout=json.dumps(result))

    def write(entry):
        assert entry.is_secret
        writes.append((entry.path, base64.b64decode(entry.content)))
        return SimpleNamespace(complete=True, written=True)

    vm = SimpleNamespace(
        id="vm-a",
        run=run,
        _send_one_write=write,
        sync=lambda path: json.dumps({"vm_id": "vm-a", "token": TOKEN, "traffic_access_token": "b" * 43}).encode(),
        refresh=lambda: SimpleNamespace(hostname="vm-a.example.com"),
    )
    monkeypatch.setattr(bootstrap.secrets, "token_urlsafe", lambda count: TOKEN)

    def stage(target, root, arch, cache):
        assert not existing, "verified installed binary must not be transferred again"
        assert (target, root, arch, cache) == (vm, ROOT, "arm64", tmp_path)
        return ROOT + "/bin/.candidate-" + "b" * 32

    monkeypatch.setattr(bootstrap, "stage_envd_artifact", stage)
    descriptor, result = bootstrap.bootstrap_envd(vm, envs=envs, cache_directory=tmp_path)
    assert descriptor.endpoint == "https://vm-a-49983.example.com"
    assert descriptor.envd_access_token == TOKEN and TOKEN not in repr(descriptor)
    assert result == ready
    request = json.loads(writes[-1][1])
    assert request["traffic_access_token"] == ("b" * 43 if existing else None)
    assert request["token"] == TOKEN
    assert request["offline"] is True
    if existing:
        assert request["artifact_path"] is None
    else:
        assert "/.candidate-" in request["artifact_path"]
    assert ("envs" in request) is (envs is not None)
    if envs is not None:
        assert request["envs"] == envs
    assert all("private-value" not in command for command in commands)
    assert all("policy" not in command for command in commands)


def test_async_bootstrap_delegates_off_loop(monkeypatch):
    import threading

    main = threading.get_ident()

    def sync(vm, **options):
        assert threading.get_ident() != main
        return ("descriptor", {"ready": True})

    monkeypatch.setattr(bootstrap, "bootstrap_envd", sync)
    assert asyncio.run(bootstrap.bootstrap_envd_async(object())) == ("descriptor", {"ready": True})


def test_secret_sync_requires_completed_write():
    vm = SimpleNamespace(_send_one_write=lambda entry: SimpleNamespace(complete=False, written=False))
    with pytest.raises(RuntimeError, match="did not complete"):
        bootstrap.secret_sync(vm, "/root/input", b"secret")


@pytest.mark.parametrize("ports", [None, [49983], [[1, 65535]], [[50000, 49980]]])
@pytest.mark.parametrize("auth", ["open", {"token": "synthetic"}])
def test_bootstrap_refuses_effective_public_control_port_before_guest_io(ports, auth):
    calls = []
    rule = {
        "type": "inbound",
        "action": "allow",
        "auth": auth,
        "match": {"ports": ports},
        "expires_at": "2000-01-01T00:00:00Z",
    }
    private = {"type": "inbound", "priority": 1000000, "action": "allow", "auth": "arker"}
    vm = SimpleNamespace(
        refresh=lambda: SimpleNamespace(policies={"rules": {"public": rule, "private": private}}),
        _send_one_write=lambda *a: calls.append("write"),
        run=lambda *a: calls.append("run"),
    )
    with pytest.raises(RuntimeError, match="owner-authenticated ingress"):
        bootstrap.bootstrap_envd(vm)
    assert calls == []


@pytest.mark.parametrize(
    "rule",
    [
        {"type": "inbound", "action": "deny", "auth": "open"},
        {"type": "inbound", "action": "allow"},
        {"type": "inbound", "action": "allow", "auth": "arker"},
        {"type": "inbound", "action": "allow", "auth": "open", "match": {"ports": [[1, 49982], [49984, 65535]]}},
    ],
)
def test_private_control_port_and_public_app_ranges_are_accepted(rule):
    vm = SimpleNamespace(refresh=lambda: SimpleNamespace(policies={"rules": {"rule": rule}}))
    bootstrap._require_private_envd(vm)
