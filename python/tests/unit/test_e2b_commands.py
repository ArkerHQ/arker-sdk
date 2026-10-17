import types

import pytest

import arker.e2b as shim
from arker._compat import CommandResult


def test_native_command_environment_uses_the_vendor_envs_option():
    calls = []
    native = types.SimpleNamespace(run=lambda command, **options: calls.append((command, options)))
    shim.Commands(native=native).run("echo value", envs={"VALUE": "kept"})
    assert calls == [("echo value", {"envs": {"VALUE": "kept"}})]


def test_arker_command_accepts_vendor_environment_and_raises_vendor_exit(monkeypatch):
    calls = []

    def run(vm, command, **options):
        calls.append(command)
        return CommandResult(stdout="output", stderr="failure", exit_code=17)

    monkeypatch.setattr(shim, "run_command", run)
    with pytest.raises(shim.CommandExitException) as raised:
        shim.Commands(vm=object()).run("exit 17", envs={"VALUE": "kept"})
    assert raised.value.exit_code == 17
    assert raised.value.stdout == "output"
    assert raised.value.stderr == "failure"
    assert raised.value.error == "exit status 17"
    assert calls == ["export VALUE=kept; cd /home/user && exit 17"]
