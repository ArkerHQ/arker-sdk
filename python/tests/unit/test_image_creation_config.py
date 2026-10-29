import json
from types import SimpleNamespace

import pytest

from arker import _compat
from arker import _compat_blaxel_create as creation


def test_shared_creation_forwards_image_config_without_losing_empty_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(_compat, "arker_client", lambda _: SimpleNamespace(fork=lambda **kw: calls.append(kw)))
    config = {"env": {"HOME": "/workspace"}, "entrypoint": [], "cmd": []}
    _compat.arker_create({"image_config": config}, image="example/image@sha256:abc")
    assert calls[0]["image_config"] == config
    assert config == {"env": {"HOME": "/workspace"}, "entrypoint": [], "cmd": []}


def test_shared_creation_rejects_image_config_on_vm_source_before_client(monkeypatch):
    monkeypatch.setattr(_compat, "arker_client", lambda _: pytest.fail("client created"))
    with pytest.raises(_compat.UnsupportedCompatibilityOption, match="image source"):
        _compat.arker_create({"source_vm_id": "source", "image_config": {"cmd": []}})


def test_blaxel_environment_merges_with_startup_overrides_without_mutation():
    overrides = {"env": {"BASE": "inherited", "HOME": "/old"}, "cmd": [], "entrypoint": []}
    values = {
        "image": "example/image",
        "envs": [{"name": "HOME", "value": "/new"}, {"name": "QUOTE", "value": "'$(not-a-command)"}],
    }
    result = creation.image_configuration(values, {"image_config": overrides})
    assert result == {
        "env": {"BASE": "inherited", "HOME": "/new", "QUOTE": "'$(not-a-command)"},
        "cmd": [],
        "entrypoint": [],
    }
    assert overrides["env"]["HOME"] == "/old"


def test_blaxel_environment_requires_image_source():
    with pytest.raises(_compat.UnsupportedCompatibilityOption, match="image source"):
        creation.image_configuration({"envs": [{"name": "HOME", "value": "/new"}]}, {})


@pytest.mark.parametrize("envs", [[{"name": "X"}], [{"name": "X", "value": 3}], [{"name": "BAD=KEY", "value": "v"}]])
def test_blaxel_invalid_environment_refused_before_fork(envs):
    with pytest.raises(ValueError, match="environment"):
        creation.image_configuration({"image": "example/image", "envs": envs}, {})


@pytest.fixture
def guest(monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path

    from arker import _guest_e2b_envd

    monkeypatch.setitem(sys.modules, "_guest_e2b_envd", _guest_e2b_envd)
    spec = importlib.util.spec_from_file_location("guest_blaxel", Path(_compat.__file__).with_name("_guest_blaxel.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_daemon_initial_environment_uses_resolved_image_values(guest, tmp_path):
    path = tmp_path / "image.json"
    path.write_text(json.dumps({"env": [["HOME", "/blaxel"], ["VALUE", "'$(literal)"], ["EMPTY", ""]]}))
    assert guest.initial_configuration(path) == {
        "envs": {"HOME": "/blaxel", "VALUE": "'$(literal)", "EMPTY": ""},
        "user": "",
    }


@pytest.mark.parametrize("raw", [b"[]", b'{"env": {"X":"secret"}}', b'{"env":[["X",1]]}', b"x" * (1024 * 1024 + 1)])
def test_daemon_invalid_image_environment_fails_closed(guest, tmp_path, raw):
    path = tmp_path / "image.json"
    path.write_bytes(raw)
    with pytest.raises(guest.security.BootstrapError, match=r"^invalid_image_environment$"):
        guest.initial_configuration(path)


def test_daemon_reconnect_uses_persisted_winner_not_new_image_environment(guest, monkeypatch):
    monkeypatch.setattr(guest, "initial_configuration", lambda: pytest.fail("image reread"))
    original = {"envs": {"VALUE": "winner"}, "user": ""}
    assert guest.startup_configuration(None, {"configuration": original}) is original


def test_non_image_daemon_keeps_default_environment(guest, tmp_path):
    assert guest.initial_configuration(tmp_path / "absent") == {"envs": {}, "user": ""}


def test_blaxel_facade_forwards_environment_before_fork(monkeypatch):
    from arker import _compat_blaxel_data_plane as plane
    from arker import blaxel

    requests = []
    client = SimpleNamespace(fork=lambda **request: requests.append(request) or SimpleNamespace(id="test", name="test"))
    monkeypatch.setattr(_compat, "arker_client", lambda _: client)
    monkeypatch.setattr(plane, "validate_native", lambda: None)
    monkeypatch.setattr(blaxel, "routed_create", lambda _name, _config, handlers: ("arker", handlers["arker"]()))
    monkeypatch.setattr("arker._compat_blaxel_bootstrap.bootstrap_blaxel", lambda *args: None)
    monkeypatch.setattr(plane, "BlaxelDataPlane", lambda *args: SimpleNamespace())
    blaxel.SyncSandboxInstance.create(
        {"image": "example/image", "envs": [{"name": "HOME", "value": "/workspace"}]},
        arker={"image_config": {"entrypoint": [], "cmd": []}},
    )
    assert requests[0]["image_config"] == {"entrypoint": [], "cmd": [], "env": {"HOME": "/workspace"}}


@pytest.mark.parametrize("envs", [False, 0, {}, ""])
def test_blaxel_wrong_environment_type_is_not_an_empty_environment(envs):
    with pytest.raises(ValueError, match="environment must be a list"):
        creation.image_configuration({"image": "example/image", "envs": envs}, {})


@pytest.mark.parametrize("envs", [None, []])
def test_blaxel_empty_environment_retains_config_defaults(envs):
    assert creation.image_configuration({"envs": envs}, {}) is None


def test_creation_environment_does_not_implicitly_clear_customer_startup():
    values = {"image": "example/image", "envs": [{"name": "X", "value": "value"}]}
    assert creation.image_configuration(values, {}) == {"env": {"X": "value"}}
    startup = {"entrypoint": ["/application"], "cmd": ["--serve"]}
    assert creation.image_configuration(values, {"image_config": startup}) == {**startup, "env": {"X": "value"}}


def test_initial_daemon_user_is_not_inferred_from_image_user(guest, tmp_path):
    path = tmp_path / "image.json"
    path.write_text(json.dumps({"env": [], "user": "1234:1234"}))
    # Custom image USER needs a separate native qualification; it is not a daemon flag.
    assert guest.initial_configuration(path)["user"] == ""
