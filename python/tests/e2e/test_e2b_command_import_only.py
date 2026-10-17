"""Native E2B command semantics with only the Sandbox import changed."""

import json
import os

import pytest
from compat_evidence import record

PROGRAM = """
from e2b import Sandbox, CommandResult, CommandExitException, CommandHandle, NotFoundException

sandbox = Sandbox.create()
try:
    connected_sandbox = Sandbox.connect(sandbox.sandbox_id)
    assert connected_sandbox.sandbox_id == sandbox.sandbox_id
    assert connected_sandbox.commands.run("printf connected").stdout == "connected"
    result = sandbox.commands.run('printf "%s" "$COMPAT_VALUE"', envs={"COMPAT_VALUE": "preserved"})
    assert isinstance(result, CommandResult)
    assert result.stdout == "preserved"
    assert result.stderr == ""
    assert result.exit_code == 0
    assert result.error is None
    try:
        sandbox.commands.run("printf output; printf failure >&2; exit 17")
    except CommandExitException as error:
        assert error.exit_code == 17
        assert error.stdout == "output"
        assert error.stderr == "failure"
        assert error.error == "exit status 17", repr(error.error)
    else:
        raise AssertionError("Nonzero command did not raise CommandExitException")
    handle = sandbox.commands.run("printf '%s\\n' $$; cat; printf diagnostic >&2; exit 17", background=True, stdin=True)
    assert isinstance(handle, CommandHandle)
    sandbox.commands.send_stdin(handle.pid, "hello\\n")
    sandbox.commands.close_stdin(handle.pid)
    try:
        handle.wait()
    except CommandExitException as error:
        assert error.stdout == str(handle.pid) + "\\nhello\\n", repr(error.stdout)
        assert (error.stderr, error.exit_code, error.error) == ("diagnostic", 17, "exit status 17")
    else:
        raise AssertionError("Background nonzero wait did not raise")
    handle = sandbox.commands.run("cat; printf reconnected", background=True, stdin=True)
    handle.disconnect()
    connected = sandbox.commands.connect(handle.pid)
    assert connected.pid == handle.pid
    connected.send_stdin("shared\\n")
    connected.close_stdin()
    assert connected.wait().stdout == "shared\\nreconnected"
    killed = sandbox.commands.run("cat", background=True, stdin=True)
    assert killed.kill() is True
    try:
        killed.wait()
    except CommandExitException as error:
        assert (error.exit_code, error.error) == (-1, "signal: killed")
    else:
        raise AssertionError("Killed process did not raise")
    assert sandbox.commands.kill(killed.pid) is False
    assert sandbox.commands.kill(2147483647) is False
    try:
        sandbox.commands.connect(2147483647)
    except NotFoundException:
        pass
    else:
        raise AssertionError("Missing process connect did not raise NotFoundException")
finally:
    try:
        with sandbox as entered:
            assert entered is sandbox
            assert Sandbox.kill(sandbox.sandbox_id) is True
            raise RuntimeError("context sentinel")
    except RuntimeError as error:
        assert str(error) == "context sentinel"
    else:
        raise AssertionError("Context manager suppressed application error")
    assert sandbox.kill() is False
    assert Sandbox.kill(sandbox_id=sandbox.sandbox_id) is False
"""


@pytest.mark.parametrize("provider,shim", [("e2b", False), ("e2b", True), ("arker", True)])
def test_command_import_only(provider, shim, tmp_path, monkeypatch):
    pytest.importorskip("e2b")
    if (provider == "e2b" and not os.environ.get("E2B_API_KEY")) or (
        provider == "arker" and not os.environ.get("ARKER_API_KEY")
    ):
        pytest.skip("live credentials required")
    if provider == "arker":
        monkeypatch.delenv("E2B_API_KEY", raising=False)
    config = tmp_path / "router.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "priority", "order": [provider]},
                "create": {"strategy": "priority", "order": [provider]},
            }
        )
    )
    monkeypatch.setenv("COMPUTESDK_ROUTER_CONFIG", str(config))
    source = (
        PROGRAM.replace(
            "from e2b import Sandbox, CommandResult, CommandExitException",
            "from arker.e2b import Sandbox\nfrom e2b import CommandResult, CommandExitException",
        )
        if shim
        else PROGRAM
    )
    outcome = "failed"
    try:
        exec(compile(source, "e2b_command_import_only.py", "exec"), {})
        outcome = "passed"
    finally:
        record(
            "e2b",
            "sync",
            "reference" if not shim else "arker" if provider == "arker" else "native",
            "import-only-command-environment-and-exit",
            [
                "Sandbox.create",
                "Sandbox.connect",
                "Sandbox.__enter__",
                "Sandbox.__exit__",
                "Sandbox.commands.run",
                "Sandbox.commands.connect",
                "Sandbox.commands.send_stdin",
                "Sandbox.commands.close_stdin",
                "Sandbox.commands.kill",
                "Sandbox.kill",
                "CommandHandle.pid",
                "CommandHandle.wait",
                "CommandHandle.send_stdin",
                "CommandHandle.close_stdin",
                "CommandHandle.disconnect",
                "CommandHandle.kill",
            ],
            outcome,
        )
