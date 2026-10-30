from __future__ import annotations

import types

import pytest

import arker.modal as shim
from arker._compat import CommandResult, UnsupportedCompatibilityOption
from arker.modal import Sandbox


@pytest.fixture
def guest():
    return types.SimpleNamespace(id="vm-test")


@pytest.fixture
def private_guest(tmp_path, monkeypatch):
    import os
    from pathlib import Path

    from test_compat_process import LocalVM

    from arker import _modal_defaults

    root = tmp_path / "guest-root"
    root.mkdir(mode=0o700)
    # Map guest uid 0 to the local test owner; production guards stay unchanged.
    monkeypatch.setattr(
        _modal_defaults,
        "_GUARD",
        _modal_defaults._GUARD.replace('root = "/root"', f"root = {str(root)!r}").replace(
            "st_uid != 0", f"st_uid != {os.geteuid()}"
        ),
    )
    vm = LocalVM(tmp_path)
    vm.id = "local"
    sync = vm.sync

    def private_sync(path, content):
        if path.startswith("/root/.arker-modal/"):
            (root / ".arker-modal" / Path(path).name).write_text(content)
        else:
            if Path(path).name.startswith("arker-compat-process-"):
                content = content.replace("/root/.arker-modal", str(root / ".arker-modal"))
            sync(path, content)

    vm.sync = private_sync
    return vm


def test_arker_object_id_is_read_only(guest):
    sandbox = Sandbox(vm=guest)
    assert sandbox.object_id == "vm-test"
    with pytest.raises(AttributeError):
        sandbox.object_id = "another"


@pytest.mark.parametrize(
    "method, args, options, result",
    [
        (
            "exec",
            ("cat",),
            {"text": False, "bufsize": 1, "pty": True, "stdout": object(), "secrets": [object()]},
            object(),
        ),
        ("terminate", (), {"wait": False}, None),
        ("terminate", (), {"wait": True}, 137),
        ("create", ("sleep", "60"), None, object()),
    ],
    ids=["exec", "terminate-no-wait", "terminate-wait", "create-resources"],
)
def test_native_calls_preserve_arguments_and_results(monkeypatch, method, args, options, result):
    if method == "create":
        native = pytest.importorskip("modal")
        options = {
            "image": native.Image.debian_slim().env({"TEST": "value"}),
            "volumes": {"/data": native.Volume.from_name("test-volume")},
            "secrets": [native.Secret.from_dict({"TEST": "value"})],
        }

    def forward(*received_args, **received_options):
        assert received_args == args
        assert received_options == options
        assert all(received_options[key] is value for key, value in options.items())
        return result

    native = types.SimpleNamespace(**{method: forward})
    if method == "create":
        monkeypatch.setattr(Sandbox, "_native_sandbox", lambda: native)
        monkeypatch.setattr(shim, "routed_create", lambda provider, config, creators: ("modal", creators["modal"]()))
        assert Sandbox.create(*args, **options)._native is result
    else:
        actual = getattr(Sandbox(native=native), method)(*args, **options)
        if method == "exec":
            assert actual is result
        else:
            assert actual == result


def test_arker_exec_none_environment_value_omits_override(monkeypatch, private_guest) -> None:
    import subprocess

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
    process = Sandbox(vm=private_guest).exec(
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


def test_exec_local_secrets_precedence_and_native_defaults(tmp_path, monkeypatch, private_guest):
    import json

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
    vm = private_guest
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


@pytest.mark.parametrize(
    "case, error, match",
    [
        ("remote-secret", UnsupportedCompatibilityOption, "named or deployed"),
        ("pty_info", TypeError, "unsupported option"),
        ("_pty_info", TypeError, "unsupported option"),
    ],
)
def test_unsupported_options_rejected_before_guest_mutation(guest, case, error, match):
    def unexpected_mutation(*args, **kwargs):
        pytest.fail("unsupported options must not execute a process or delete the VM")

    guest.run = guest.delete = unexpected_mutation
    sandbox = Sandbox(vm=guest)
    if case == "remote-secret":
        native = pytest.importorskip("modal")
        options = {"secrets": [native.Secret.from_name("test-only")]}
    else:
        options = {case: True if case == "pty" else object()}
    with pytest.raises(error, match=match):
        sandbox.exec("true", **options)
