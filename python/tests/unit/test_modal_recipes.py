import asyncio
from types import SimpleNamespace

import pytest

import arker.modal as shim
from arker import _modal_recipes as recipes
from arker._compat import UnsupportedCompatibilityOption


@pytest.fixture(autouse=True)
def recipe_mode(monkeypatch):
    monkeypatch.setenv("ARKER_API_KEY", "synthetic")


@pytest.fixture
def forks(monkeypatch):
    requests = []
    monkeypatch.setattr(shim, "routed_create", lambda name, config, creators: ("arker", creators["arker"]()))
    monkeypatch.setattr(
        "arker._compat.arker_client",
        lambda config: SimpleNamespace(fork=lambda **request: requests.append(request) or SimpleNamespace(id="owned")),
    )
    return requests


@pytest.mark.parametrize("asynchronous", [False, True])
def test_registry_recipe_routes_without_native_realization(forks, monkeypatch, asynchronous):
    def forbidden(*args, **kwargs):
        pytest.fail("Arker route must not realize a native image")

    monkeypatch.setattr(recipes.Image, "_native", forbidden)
    options = {"image": recipes.Image.from_registry("python:3.12-slim"), "arker": {"source": "unused"}}
    box = asyncio.run(shim.Sandbox.create.aio(**options)) if asynchronous else shim.Sandbox.create(**options)
    assert box.object_id == "owned"
    assert forks == [
        {
            "name": None,
            "image": "python:3.12-slim",
            "policies": {
                "rules": {"computesdk-modal-lifetime": {"type": "schedule", "after_created": 300, "action": "delete"}}
            },
        }
    ]


@pytest.mark.parametrize(
    "image",
    [
        object(),
        recipes.Image.debian_slim(),
        recipes.Image.from_registry("alpine", secret=object()),
        recipes.Image.from_registry("alpine", force_build=True),
        recipes.Image.from_registry("alpine", add_python="3.12"),
        recipes.Image.from_registry("alpine", setup_dockerfile_commands=["RUN touch /marker"]),
        recipes.Image.from_registry("alpine", unknown=True),
    ],
)
def test_unsupported_recipes_fail_before_fork(forks, image):
    with pytest.raises(UnsupportedCompatibilityOption):
        shim.Sandbox.create(image=image)
    assert forks == []


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_recipe_realization_preserves_options_and_error_identity(monkeypatch, asynchronous):
    calls = []
    native_image = object()
    failure = RuntimeError("native create failure")
    monkeypatch.setattr(
        "arker._modal_recipes.import_optional",
        lambda name: SimpleNamespace(
            Image=SimpleNamespace(from_registry=lambda **options: calls.append(options) or native_image)
        ),
    )

    def create(**options):
        assert options == {"image": native_image}
        raise failure

    async def create_async(**options):
        return create(**options)

    create.aio = create_async
    monkeypatch.setattr(shim.Sandbox, "_native_sandbox", staticmethod(lambda: SimpleNamespace(create=create)))
    monkeypatch.setattr(shim, "routed_create", lambda name, config, creators: ("modal", creators["modal"]()))
    secret = object()
    recipe = recipes.Image.from_registry("private/image", secret, add_python="3.12", force_build=True)
    assert calls == []
    with pytest.raises(RuntimeError) as caught:
        if asynchronous:
            asyncio.run(shim.Sandbox.create.aio(image=recipe))
        else:
            shim.Sandbox.create(image=recipe)
    assert caught.value is failure
    assert calls == [
        {
            "tag": "private/image",
            "secret": secret,
            "add_python": "3.12",
            "force_build": True,
            "setup_dockerfile_commands": [],
        }
    ]
    assert "private/image" not in repr(recipe)


def test_app_group_has_no_invented_native_id(forks):
    app = recipes.App.lookup("shared-workload", create_if_missing=True)
    shim.Sandbox.create(app=app, image=recipes.Image.from_registry("python:3.12-slim"))
    assert forks[0]["metadata"] == {"computesdk-modal-app": {"name": "shared-workload"}}
    assert "app_id" not in vars(app)
    assert app._resolved is None


@pytest.mark.parametrize("app", [object(), recipes.App.lookup("existing")])
def test_unknown_app_ownership_is_not_assumed(forks, app):
    with pytest.raises(UnsupportedCompatibilityOption):
        shim.Sandbox.create(app=app)
    assert not forks


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_failed_default_install_cleans_up_and_preserves_error(monkeypatch, cleanup_fails):
    failure = RuntimeError("installation failed")
    deleted = []

    def delete():
        deleted.append(True)
        if cleanup_fails:
            raise OSError("delete failed")

    monkeypatch.setattr(shim, "routed_create", lambda name, config, creators: ("arker", creators["arker"]()))
    monkeypatch.setattr(shim, "arker_create", lambda *args, **kwargs: SimpleNamespace(id="owned", delete=delete))
    monkeypatch.setattr("arker._modal_defaults.write", lambda *args: (_ for _ in ()).throw(failure))
    with pytest.raises(RuntimeError) as caught:
        shim.Sandbox.create(secrets=[recipes.Secret.from_dict({"TOKEN": "synthetic"})])
    assert caught.value is failure and deleted == [True]


def test_creation_defaults_are_private_and_survive_new_handle(monkeypatch):
    from arker._compat import CommandResult
    from arker._modal_defaults import MARKER, PATH

    content, commands, requests = {}, [], []
    vm = SimpleNamespace(id="owned", metadata={MARKER: True})
    vm.sync = lambda path, data=None: content.__setitem__(path, data) if data is not None else content[path]
    monkeypatch.setattr(
        "arker._modal_defaults.run_command",
        lambda vm, command: commands.append(command) or CommandResult(stdout="", stderr="", exit_code=0),
    )
    monkeypatch.setattr(shim, "routed_create", lambda name, config, creators: ("arker", creators["arker"]()))
    monkeypatch.setattr(shim, "arker_create", lambda *args, **kwargs: requests.append(kwargs) or vm)
    secret = recipes.Secret.from_dict({"TOKEN": "synthetic-secret", "IGNORED": None, "ORDER": "secret"})
    shim.Sandbox.create(secrets=[secret], env={"ORDER": "explicit"}, workdir="/tmp")
    assert requests[0]["metadata"] == {MARKER: True}
    assert "synthetic-secret" not in repr(requests) + repr(commands) + repr(secret)
    from arker._modal_defaults import read

    reconnected = shim.Sandbox(vm=vm)
    assert read(reconnected._vm) == {"env": {"TOKEN": "synthetic-secret", "ORDER": "explicit"}, "workdir": "/tmp"}
    assert list(content) == [PATH]


@pytest.mark.parametrize("unsafe", [None, "mode", "symlink", "hardlink"])
def test_private_defaults_guard_checks_actual_filesystem(tmp_path, monkeypatch, unsafe):
    import os
    import sys

    from arker._modal_defaults import _GUARD

    if os.geteuid() != 0:
        pytest.skip("guest root ownership check requires root")
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    source = _GUARD.replace('root = "/root"', f"root = {str(root)!r}")
    monkeypatch.setattr(sys, "argv", ["guard", "prepare"])
    exec(compile(source, "guard", "exec"), {})
    path = root / ".arker-modal/defaults.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    if unsafe == "mode":
        path.chmod(0o644)
    elif unsafe == "hardlink":
        os.link(path, root / "alias")
    elif unsafe == "symlink":
        path.unlink()
        path.symlink_to(root / "missing")
    monkeypatch.setattr(sys, "argv", ["guard", "check"])
    if unsafe:
        with pytest.raises(RuntimeError):
            exec(compile(source, "guard", "exec"), {})
    else:
        exec(compile(source, "guard", "exec"), {})


def test_cancelled_creation_waits_for_secret_install_then_deletes(monkeypatch):
    import threading

    started, release, deleted = threading.Event(), threading.Event(), threading.Event()
    vm = SimpleNamespace(id="owned", delete=deleted.set)
    monkeypatch.setattr(shim, "routed_create", lambda name, config, creators: ("arker", creators["arker"]()))
    monkeypatch.setattr(shim, "arker_create", lambda *args, **kwargs: vm)

    def install(*args):
        started.set()
        assert release.wait(5)

    monkeypatch.setattr("arker._modal_defaults.write", install)

    async def scenario():
        task = asyncio.create_task(shim.Sandbox.create.aio(secrets=[recipes.Secret.from_dict({"TOKEN": "synthetic"})]))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not deleted.is_set()
        finally:
            release.set()
        assert await asyncio.to_thread(deleted.wait, 5)

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [None, [], {"VALUE": 1}, {1: "value"}])
def test_secret_input_errors_match_native(value):
    native = pytest.importorskip("modal")
    with pytest.raises(Exception) as expected:
        native.Secret.from_dict(value)
    with pytest.raises(type(expected.value)) as actual:
        recipes.Secret.from_dict(value)
    assert str(actual.value) == str(expected.value)


def test_recipe_factories_need_no_native_installation(monkeypatch, forks):
    monkeypatch.setattr("arker._modal_recipes.import_optional", lambda name: None)
    monkeypatch.setattr(shim, "import_optional", lambda name: None)
    box = shim.Sandbox.create(
        app=shim.App.lookup("local", create_if_missing=True), image=shim.Image.from_registry("python:3.12-slim")
    )
    assert box.object_id == "owned"
    assert forks[0]["image"] == "python:3.12-slim"
    assert shim.Secret.from_dict({"VALUE": "local"})._values == {"VALUE": "local"}


def test_minimal_sdk_none_env_keeps_creation_default(monkeypatch):
    from arker._compat import CommandResult

    commands = []
    monkeypatch.setattr(shim, "import_optional", lambda name: None)
    monkeypatch.setattr(
        shim,
        "run_command",
        lambda vm, command, **kwargs: commands.append(command) or CommandResult(stdout="", stderr="", exit_code=0),
    )
    import json

    contents = {}
    monkeypatch.setattr(
        "arker._modal_defaults.run_command", lambda *a, **kw: CommandResult(stdout="", stderr="", exit_code=0)
    )
    sandbox = shim.Sandbox(
        vm=SimpleNamespace(
            id="owned",
            metadata={"computesdk-modal-defaults": True},
            sync=lambda path, data: contents.__setitem__(path, data),
        )
    )
    sandbox.exec("true", env={"VALUE": None})
    control = json.loads(next(iter(contents.values())))
    assert control["env"] == {}
    assert control["defaults"] is True
    assert all("creation" not in command for command in commands)


def test_empty_exec_workdir_is_not_replaced_by_creation_default():
    native = pytest.importorskip("modal")
    sandbox = shim.Sandbox(vm=SimpleNamespace(id="owned"))
    with pytest.raises(native.exception.InvalidError, match="workdir must be an absolute path"):
        sandbox.exec("true", workdir="")


@pytest.mark.parametrize("minimal", [False, True])
def test_exec_never_serializes_private_environment(monkeypatch, minimal):
    import base64
    import json
    import shlex

    from arker._compat import CommandResult
    from arker._modal_defaults import MARKER, PATH

    commands, contents = [], {PATH: json.dumps({"env": {"TOKEN": "creation-secret"}, "workdir": "/tmp"})}
    stopped = RuntimeError("stop after capturing wire command")

    def run(command, **options):
        commands.append(command)
        raise stopped

    vm = SimpleNamespace(
        id="owned", metadata={MARKER: True}, run=run, create_session=lambda: SimpleNamespace(session_id="session")
    )
    vm.sync = lambda path, data=None: contents.__setitem__(path, data) if data is not None else contents[path]
    monkeypatch.setattr(
        "arker._modal_defaults.run_command", lambda *a, **kw: CommandResult(stdout="", stderr="", exit_code=0)
    )
    if minimal:
        monkeypatch.setattr(shim, "import_optional", lambda name: None)
    try:
        shim.Sandbox(vm=vm).exec("true", env={"EXEC_TOKEN": "exec-secret"})
    except RuntimeError as error:
        assert error is stopped
    assert commands
    for command in commands:
        assert "creation-secret" not in command and "exec-secret" not in command
        if " supervise " in command:
            payload = base64.b64decode(shlex.split(command)[-1]).decode()
            assert "creation-secret" not in payload and "exec-secret" not in payload
    assert any("exec-secret" in str(value) for value in contents.values())


def test_native_image_realizes_nested_registry_secret(monkeypatch):
    native_secret = object()
    calls = []
    monkeypatch.setattr(
        recipes,
        "import_optional",
        lambda name: SimpleNamespace(
            Secret=SimpleNamespace(from_dict=lambda values: native_secret),
            Image=SimpleNamespace(from_registry=lambda **options: calls.append(options) or object()),
        ),
    )
    recipes.Image.from_registry("private/image", secret=recipes.Secret.from_dict({"TOKEN": "fixture"}))._native()
    assert calls[0]["secret"] is native_secret


def test_creation_cleanup_preserves_both_failures_and_vm_id(monkeypatch):
    primary, cleanup = RuntimeError("setup failed"), OSError("delete failed")
    monkeypatch.setattr(shim, "routed_create", lambda name, config, creators: ("arker", creators["arker"]()))
    monkeypatch.setattr(
        shim,
        "arker_create",
        lambda *a, **kw: SimpleNamespace(id="owned-failed-vm", delete=lambda: (_ for _ in ()).throw(cleanup)),
    )
    monkeypatch.setattr("arker._modal_defaults.write", lambda *a: (_ for _ in ()).throw(primary))
    with pytest.raises(RuntimeError) as caught:
        shim.Sandbox.create(env={"TOKEN": "fixture"})
    assert caught.value is primary
    assert caught.value.__cause__ is cleanup
    assert caught.value.vm_id == "owned-failed-vm"


@pytest.mark.parametrize("override", [None, "/"])
def test_guest_launcher_reads_defaults_and_consumes_control(tmp_path, override):
    import json
    import os
    import subprocess
    import sys

    from arker._modal_defaults import _GUARD

    if os.geteuid() != 0:
        pytest.skip("actual guest root ownership checks require root")
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    directory = root / ".arker-modal"
    directory.mkdir(mode=0o700)
    defaults = directory / "defaults.json"
    defaults.write_text(
        json.dumps({"env": {"TOKEN": "creation-secret", "ORDER": "creation"}, "workdir": str(tmp_path)})
    )
    defaults.chmod(0o600)
    name = "exec-" + "a" * 32 + ".json"
    control = directory / name
    script = "import os,json; print(json.dumps([os.environ['TOKEN'], os.environ['ORDER'], os.getcwd()]))"
    control.write_text(
        json.dumps(
            {
                "argv": [sys.executable, "-c", script],
                "defaults": True,
                "env": {"ORDER": "exec-secret"},
                "workdir": override,
            }
        )
    )
    control.chmod(0o600)
    source = _GUARD.replace('root = "/root"', f"root = {str(root)!r}")
    command = [sys.executable, "-c", source, "exec", name]
    assert "creation-secret" not in repr(command) and "exec-secret" not in repr(command)
    result = subprocess.run(command, text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == ["creation-secret", "exec-secret", override or str(tmp_path)]
    assert not control.exists()
    assert defaults.exists()


def test_native_registry_secret_is_a_real_hydration_dependency():
    pytest.importorskip("modal")
    from modal._utils.async_utils import synchronizer
    from modal.secret import _Secret

    image = recipes.Image.from_registry(
        "private/image", recipes.Secret.from_dict({"REGISTRY_USERNAME": "fixture", "REGISTRY_PASSWORD": "synthetic"})
    )._native()
    dependencies = synchronizer._translate_in(image)._deps_()
    assert any(isinstance(dependency, _Secret) for dependency in dependencies)
    assert all(hasattr(dependency, "_load") for dependency in dependencies)


def test_control_staging_cleanup_retains_primary_cleanup_and_vm(monkeypatch):
    from arker._modal_defaults import prepare_exec

    primary, cleanup = RuntimeError("sync failed"), OSError("remove failed")
    operations = []

    def guard(vm, operation):
        operations.append(operation.split()[0])
        if operation.startswith("remove"):
            raise cleanup

    monkeypatch.setattr("arker._modal_defaults.guard", guard)
    vm = SimpleNamespace(id="owned-control", sync=lambda *a: (_ for _ in ()).throw(primary))
    with pytest.raises(RuntimeError) as caught:
        prepare_exec(vm, ["true"], {"TOKEN": "synthetic"}, None)
    assert caught.value is primary
    assert caught.value.__cause__ is cleanup
    assert caught.value.vm_id == "owned-control"
    assert operations == ["prepare", "remove"]


def test_worker_cleans_control_when_launcher_never_starts(tmp_path, monkeypatch):
    import os
    from pathlib import Path

    import arker._guest_process as guest

    directory = tmp_path / "process"
    directory.mkdir()
    control = tmp_path / ("exec-" + "a" * 32 + ".json")
    control.write_text("synthetic private control")
    real_path = Path
    monkeypatch.setattr(
        guest, "Path", lambda value: tmp_path if str(value) == "/root/.arker-modal" else real_path(value)
    )
    monkeypatch.setattr(guest.subprocess, "Popen", lambda *a, **kw: (_ for _ in ()).throw(OSError("spawn failed")))
    read_fd, write_fd = os.pipe()
    try:
        guest.worker(
            {
                "_directory": str(directory),
                "command": "true",
                "name": "owned",
                "workingDir": str(tmp_path),
                "_modalControl": control.name,
            },
            write_fd,
        )
    finally:
        os.close(read_fd)
    assert not control.exists()
    assert (directory / "worker-complete").exists()


def test_registry_credentials_only_enter_image_pull(forks):
    secret = recipes.Secret.from_dict(
        {"REGISTRY_USERNAME": "fixture-user", "REGISTRY_PASSWORD": "fixture-password", "OTHER": "not-forwarded"}
    )
    config = {"registry_auth": {"username": "old", "password": "old"}}
    shim.Sandbox.create(image=recipes.Image.from_registry("private/image", secret=secret), arker=config)
    assert forks[0]["registry_auth"] == {"username": "fixture-user", "password": "fixture-password"}
    assert "metadata" not in forks[0]
    assert "env" not in forks[0]
    assert config == {"registry_auth": {"username": "old", "password": "old"}}
    assert "fixture-password" not in repr(secret)


@pytest.mark.parametrize(
    "values", [{}, {"REGISTRY_USERNAME": "u"}, {"REGISTRY_USERNAME": "u", "REGISTRY_PASSWORD": ""}]
)
def test_invalid_registry_secret_fails_before_fork(forks, values):
    with pytest.raises(ValueError, match="Registry secret requires"):
        shim.Sandbox.create(image=recipes.Image.from_registry("private/image", secret=recipes.Secret.from_dict(values)))
    assert forks == []


@pytest.mark.parametrize("asynchronous", [False, True])
def test_public_recipes_route_with_per_call_key_and_configured_route(forks, monkeypatch, tmp_path, asynchronous):
    import json

    route = tmp_path / "router.json"
    route.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "priority", "order": ["arker"]},
                "create": {"strategy": "priority", "order": ["arker"]},
            }
        )
    )
    monkeypatch.setenv("COMPUTESDK_ROUTER_CONFIG", str(route))
    native = pytest.importorskip("modal")
    monkeypatch.delenv("ARKER_API_KEY", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    image = shim.Image.from_registry(
        "private/image", secret=shim.Secret.from_dict({"REGISTRY_USERNAME": "user", "REGISTRY_PASSWORD": "password"})
    )
    assert isinstance(image, native.Image)
    app = shim.App.lookup("same-app", create_if_missing=True)
    options = {"app": app, "image": image, "arker": {"api_key": "synthetic"}}
    box = asyncio.run(shim.Sandbox.create.aio(**options)) if asynchronous else shim.Sandbox.create(**options)
    assert box.object_id == "owned"
    assert forks[0]["registry_auth"] == {"username": "user", "password": "password"}


def test_public_local_handles_preserve_native_identity_on_native_route(monkeypatch):
    native = pytest.importorskip("modal")
    monkeypatch.delenv("ARKER_API_KEY", raising=False)
    image = shim.Image.from_registry("public/image")
    secret = shim.Secret.from_dict({"VALUE": "fixture"})
    assert isinstance(image, native.Image)
    assert isinstance(secret, native.Secret)
    sentinel = object()

    def create(**options):
        assert options["image"] is image
        assert options["secrets"][0] is secret
        return sentinel

    monkeypatch.setattr(shim.Sandbox, "_native_sandbox", staticmethod(lambda: SimpleNamespace(create=create)))
    monkeypatch.setattr(shim, "routed_create", lambda name, config, creators: ("modal", creators["modal"]()))
    result = shim.Sandbox.create(image=image, secrets=[secret])
    assert result._native is sentinel


def test_lazy_app_forwards_native_operations_once(monkeypatch):
    calls = []
    native = SimpleNamespace(app_id="native-app", operation=lambda: "native-result")
    monkeypatch.setattr(
        recipes,
        "import_optional",
        lambda name: SimpleNamespace(
            App=SimpleNamespace(lookup=lambda *args, **kwargs: calls.append((args, kwargs)) or native)
        ),
    )
    app = recipes.App.lookup("lazy", create_if_missing=True)
    assert calls == []
    assert app.operation() == "native-result"
    assert app.app_id == "native-app"
    assert len(calls) == 1
    assert app._native() is native


@pytest.mark.parametrize("strategy", ["priority", "weighted"])
def test_native_only_route_preserves_native_classes_even_with_arker_key(monkeypatch, tmp_path, strategy):
    import json

    native = pytest.importorskip("modal")
    route = tmp_path / "native-router.json"
    create = {"strategy": strategy, **({"order": ["modal"]} if strategy == "priority" else {"weights": {"modal": 1}})}
    route.write_text(
        json.dumps({"version": 1, "existing": {"strategy": "priority", "order": ["modal"]}, "create": create})
    )
    monkeypatch.setenv("ARKER_API_KEY", "synthetic-key-must-not-select-factories")
    monkeypatch.setenv("COMPUTESDK_ROUTER_CONFIG", str(route))
    for name in ("App", "Image", "Secret"):
        assert getattr(shim, name) is getattr(native, name)


@pytest.mark.parametrize(
    "create",
    [
        {"strategy": "priority", "order": ["modal", "arker"]},
        {"strategy": "weighted", "weights": {"arker": 1}},
        {"strategy": "weighted", "weights": {"modal": 1}, "fallback": ["arker"]},
    ],
)
def test_arker_capable_routes_track_public_factories_without_global_key(monkeypatch, tmp_path, create):
    import json

    native = pytest.importorskip("modal")
    route = tmp_path / "mixed-router.json"
    route.write_text(
        json.dumps({"version": 1, "existing": {"strategy": "priority", "order": ["modal"]}, "create": create})
    )
    monkeypatch.setenv("COMPUTESDK_ROUTER_CONFIG", str(route))
    monkeypatch.delenv("ARKER_API_KEY", raising=False)
    image = shim.Image.from_registry("public/image")
    assert isinstance(image, native.Image)
    assert recipes.image_recipe(image)._registry() == "public/image"
    # A native derived handle has additional semantics; the original recipe is insufficient.
    changed = image.env({"BUILD_VALUE": "literal"})
    assert changed is not image
    assert recipes.image_recipe(changed) is None
    assert recipes.image_recipe(image)._registry() == "public/image"


def test_scoped_factory_forwards_unknown_native_constructor_without_tracking(monkeypatch):
    native = pytest.importorskip("modal")
    sentinel = object()
    calls = []
    monkeypatch.setattr(
        native.Image, "from_name", staticmethod(lambda *args, **kwargs: calls.append((args, kwargs)) or sentinel)
    )
    factory = recipes.RecipeFactory("Image")
    assert factory.from_name("native-image", namespace="workspace") is sentinel
    assert calls == [(("native-image",), {"namespace": "workspace"})]
    assert recipes.image_recipe(sentinel) is None
