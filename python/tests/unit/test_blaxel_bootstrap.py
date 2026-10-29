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
