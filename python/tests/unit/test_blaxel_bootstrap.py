from types import SimpleNamespace

import pytest

from arker._compat_blaxel_bootstrap import bootstrap_blaxel
from arker._compat_private_daemon import require_private_daemon


@pytest.mark.parametrize("auth", [None, "open", "token"])
def test_public_daemon_policy_refused_even_with_private_rule(auth):
    rule = {"type": "inbound", "action": "allow", "match": {"ports": [[1, 65535]]}}
    if auth is not None:
        rule["auth"] = auth
    vm = SimpleNamespace(
        refresh=lambda: SimpleNamespace(
            policies={
                "rules": {
                    "public": rule,
                    "private": {**rule, "auth": "arker", "priority": 100},
                }
            }
        )
    )
    with pytest.raises(RuntimeError, match="owner-authenticated"):
        require_private_daemon(vm)


def test_paused_bootstrap_performs_no_guest_io():
    calls = []

    def request(*args, **kwargs):
        calls.append(args)
        return {"customer_paused": True}

    vm = SimpleNamespace(id="vm-test", base_url="https://custom.example/api", _client=SimpleNamespace(_request=request))
    with pytest.raises(RuntimeError, match="explicitly running"):
        bootstrap_blaxel(vm)
    assert calls == [("GET", "/v1/vms/vm-test/lifecycle")]


def test_bootstrap_keeps_primary_and_staging_cleanup_errors(monkeypatch):
    from arker import _compat_blaxel_bootstrap as module

    primary = RuntimeError("ensure failed")
    cleanup = RuntimeError("cleanup failed")
    monkeypatch.setattr(module, "require_private_daemon", lambda vm: None)
    monkeypatch.setattr(module, "secret_sync", lambda *args: None)

    def run(command, **kwargs):
        if "prepare" in command:
            return SimpleNamespace(exit_code=0, stdout='{"verified":true,"arch":"amd64"}')
        raise cleanup if command.startswith("rm ") else primary

    vm = SimpleNamespace(
        id="test",
        base_url="https://custom.example/api",
        run=run,
        _client=SimpleNamespace(_request=lambda *args, **kwargs: {"customer_paused": False}),
    )
    with pytest.raises(RuntimeError) as error:
        bootstrap_blaxel(vm)
    assert error.value is primary
    assert error.value.cleanup_error is cleanup


def test_qualified_packages_are_scoped_through_ensure(monkeypatch):
    import json
    from contextlib import contextmanager

    from arker import _compat_blaxel_bootstrap as module

    events, writes = [], {}
    image = "qualified-test-image"
    monkeypatch.setattr(module, "require_private_daemon", lambda vm: events.append("policy"))
    monkeypatch.setattr(module, "secret_sync", lambda vm, path, data: writes.update({path: data}))
    manifest = json.loads(module.files("arker").joinpath("_guest_blaxel_manifest.json").read_text())

    @contextmanager
    def staging(vm, observed, cache):
        assert observed == image
        events.append("stage")
        try:
            yield ["/root/.arker/blaxel/bin/.candidate-test.apk"]
        finally:
            events.append("package-cleanup")

    monkeypatch.setattr(module, "staged_firewall_packages", staging)

    def run(command, **kwargs):
        if " prepare " in command:
            return SimpleNamespace(
                exit_code=0,
                stdout=json.dumps(
                    {
                        "arch": "amd64",
                        "verified": True,
                        "firewall_profile": image,
                        "firewall_verified": False,
                    }
                ),
            )
        if " ensure " in command:
            request = json.loads(writes[command.split()[-1]])
            assert request["firewall_profile"] == image
            assert request["package_candidates"] == ["/root/.arker/blaxel/bin/.candidate-test.apk"]
            events.append("ensure")
            return SimpleNamespace(exit_code=0, stdout=json.dumps({"ready": True, "version": manifest["version"]}))
        events.append("request-cleanup")
        return SimpleNamespace(exit_code=0, stdout="")

    vm = SimpleNamespace(
        id="test",
        base_url="https://custom.example/api",
        run=run,
        _client=SimpleNamespace(_request=lambda *a, **kw: {"customer_paused": False}),
    )
    monkeypatch.setattr(
        module, "registered_bootstrap", lambda vm, command, request, version: json.loads(run(command).stdout)
    )
    module.bootstrap_blaxel(vm)
    assert events == ["policy", "stage", "ensure", "package-cleanup", "request-cleanup"]
