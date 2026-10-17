"""Native E2B command semantics with only the Sandbox import changed."""

import json
import os

import pytest
from compat_evidence import record

PROGRAM = """
from e2b import Sandbox, CommandResult, CommandExitException

sandbox = Sandbox.create()
try:
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
finally:
    sandbox.kill()
"""


@pytest.mark.parametrize("provider,shim", [("e2b", False), ("e2b", True), ("arker", True)])
def test_command_import_only(provider, shim, tmp_path, monkeypatch):
    pytest.importorskip("e2b")
    if not os.environ.get("E2B_API_KEY") or (provider == "arker" and not os.environ.get("ARKER_API_KEY")):
        pytest.skip("live credentials required")
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
            ["Sandbox.create", "Sandbox.commands.run", "Sandbox.kill"],
            outcome,
        )
