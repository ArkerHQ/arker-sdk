from __future__ import annotations

import pytest

import arker.modal as shim
from arker._compat import CommandResult, UnsupportedCompatibilityOption
from arker.modal import Sandbox


def test_arker_object_id_is_read_only():
    class Guest:
        id = "vm-test"

    sandbox = Sandbox(vm=Guest())
    assert sandbox.object_id == "vm-test"
    with pytest.raises(AttributeError):
        sandbox.object_id = "another"


def test_native_exec_preserves_process_identity_and_options() -> None:
    process = object()
    options = {"text": False, "bufsize": 1, "pty": True, "stdout": object(), "secrets": [object()]}

    class Native:
        def exec(self, *args, **kwargs):
            assert args == ("cat",)
            assert kwargs == options
            return process

    assert Sandbox(native=Native()).exec("cat", **options) is process


@pytest.mark.parametrize("wait, result", [(False, None), (True, 137)])
def test_native_terminate_preserves_wait_and_return_value(wait, result) -> None:
    class Native:
        def terminate(self, **kwargs):
            assert kwargs == {"wait": wait}
            return result

    assert Sandbox(native=Native()).terminate(wait=wait) == result


def test_arker_terminate_wait_rejected_before_delete() -> None:
    class Guest:
        id = "vm-test"

        def delete(self):
            pytest.fail("unsupported wait must not delete the VM")

    with pytest.raises(UnsupportedCompatibilityOption):
        Sandbox(vm=Guest()).terminate(wait=True)


def test_arker_exec_none_environment_value_omits_override(monkeypatch) -> None:
    import subprocess

    class Guest:
        id = "vm-test"

    def run(vm, command, **kwargs):
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            env={"DROP_ME": "inherited", "KEEP_ME": "old"},
        )
        return CommandResult(stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode)

    monkeypatch.setattr(shim, "run_command", run)
    monkeypatch.setattr(shim, "import_optional", lambda _: None)
    process = Sandbox(vm=Guest()).exec(
        "sh",
        "-c",
        'printf "%s|%s" "$DROP_ME" "$KEEP_ME"',
        env={"DROP_ME": None, "KEEP_ME": "new value"},
    )
    assert process.stdout.read() == "inherited|new value"
    assert process.wait() == 0
