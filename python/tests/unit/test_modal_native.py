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


@pytest.mark.parametrize("positional", [False, True])
def test_from_id_forwards_native_client(monkeypatch, positional):
    client = object()
    native = object()

    class Native:
        @staticmethod
        def from_id(sandbox_id, **kwargs):
            assert sandbox_id == "sb-test"
            assert kwargs == {"client": client}
            return native

    monkeypatch.setattr(Sandbox, "_native_sandbox", lambda: Native)
    monkeypatch.setattr(shim, "routed_get", lambda identifier, provider, config, getters: ("modal", getters["modal"]()))
    sandbox = Sandbox.from_id("sb-test", client) if positional else Sandbox.from_id("sb-test", client=client)
    assert sandbox._native is native


@pytest.mark.parametrize("name", ["Image", "Volume", "Secret", "CloudBucketMount"])
def test_native_resource_classes_preserve_identity(name):
    native = pytest.importorskip("modal")
    assert getattr(shim, name) is getattr(native, name)


def test_mount_export_matches_pinned_native_sdk():
    native = pytest.importorskip("modal")
    assert not hasattr(native, "Mount")
    assert not hasattr(shim, "Mount")


def test_exec_local_secrets_precedence_and_native_defaults(tmp_path, monkeypatch):
    import json

    from test_compat_process import LocalVM

    native = pytest.importorskip("modal")
    from modal._utils.async_utils import synchronizer
    from modal.secret import _secret_sources

    monkeypatch.setenv("MODAL_TEST_LOCAL", "local-test-value")
    dotenv = tmp_path / ".env"
    dotenv.write_text("MODAL_TEST_DOTENV=dotenv-test-value\n")
    secrets = [
        native.Secret.from_dict({"ORDER": "first", "OMITTED": None}),
        native.Secret.from_dict({"ORDER": "last", "KEEP": "kept"}),
        native.Secret.from_local_environ(["MODAL_TEST_LOCAL"]),
        native.Secret.from_dotenv(path=str(tmp_path)),
    ]
    env = {"ORDER": "explicit", "KEEP": None}
    expected = {}
    for source in _secret_sources([synchronizer._translate_in(secret) for secret in secrets], {"ORDER": "explicit"}):
        expected.update(source.env.contents)
    vm = LocalVM(tmp_path)
    vm.id = "local"
    sandbox = Sandbox(vm=vm)
    process = sandbox.exec(
        "python3",
        "-c",
        "import json, os; print(json.dumps({k: os.environ[k] for k in " + repr(list(expected)) + "}))",
        secrets=secrets,
        env=env,
        pty=False,
        pty_info=None,
        _pty_info=None,
    )
    try:
        assert json.loads(process.stdout.read()) == expected
        assert process.wait() == 0
    finally:
        for running, _, _ in vm.runs.values():
            running.wait(timeout=5)


def test_exec_remote_secret_rejected_before_guest_execution():
    native = pytest.importorskip("modal")

    class Guest:
        id = "local"

        def run(self, *args, **kwargs):
            pytest.fail("remote secrets must be rejected before executing anything")

    with pytest.raises(UnsupportedCompatibilityOption, match="named or deployed"):
        Sandbox(vm=Guest()).exec("true", secrets=[native.Secret.from_name("test-only")])


def test_create_preserves_native_resource_options(monkeypatch, tmp_path):
    native = pytest.importorskip("modal")
    options = {
        "image": native.Image.debian_slim().env({"TEST": "value"}),
        "volumes": {"/data": native.Volume.from_name("test-volume")},
        "secrets": [native.Secret.from_dict({"TEST": "value"})],
    }
    result = object()

    class Native:
        @staticmethod
        def create(*args, **kwargs):
            assert args == ("sleep", "60")
            assert kwargs == options
            assert all(kwargs[key] is value for key, value in options.items())
            return result

    monkeypatch.setattr(Sandbox, "_native_sandbox", lambda: Native)
    monkeypatch.setattr(shim, "routed_create", lambda provider, config, creators: ("modal", creators["modal"]()))
    assert Sandbox.create("sleep", "60", **options)._native is result


@pytest.mark.parametrize("options", [{"pty": True}, {"pty_info": object()}, {"_pty_info": object()}])
def test_exec_nondefault_pty_still_rejected_before_execution(options):
    class Guest:
        id = "local"

        def run(self, *args, **kwargs):
            pytest.fail("unsupported PTY must not start a process")

    with pytest.raises(TypeError, match="unsupported option"):
        Sandbox(vm=Guest()).exec("true", **options)
