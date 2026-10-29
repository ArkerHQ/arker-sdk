import builtins
import json
import subprocess

import pytest

from arker._modal_prepared import image_metadata


def metadata(**changes):
    return image_metadata(**{"env": {}, "user": None, "workdir": None, "entrypoint": None, "cmd": None, **changes})


def test_prepared_metadata_preserves_literal_argv_and_environment(tmp_path):
    marker = tmp_path / "must-not-exist"
    literal = f"'$(touch {marker}); spaced\nvalue"
    command = ["python3", "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", literal, ""]
    encoded, profile = metadata(
        env={"LITERAL": literal}, user="1000:1000", workdir=str(tmp_path), entrypoint=command[:1], cmd=command[1:]
    )
    config = json.loads(encoded)
    assert config["env"] == [["LITERAL", literal]]
    assert config["user"] == "1000:1000"
    assert config["workdir"] == str(tmp_path)
    result = subprocess.run(
        ["sh", "-c", config["entrypoint"] + " " + config["cmd"]], check=True, capture_output=True, text=True
    )
    assert json.loads(result.stdout) == [literal, ""]
    result = subprocess.run(
        ["sh", "-c", profile.decode() + '\nprintf "%s" "$LITERAL"'], check=True, capture_output=True, text=True
    )
    assert result.stdout == literal
    assert not marker.exists()


def test_prepared_metadata_explicit_clears():
    encoded, profile = metadata(entrypoint=[], cmd=[])
    assert json.loads(encoded) == {"env": [], "user": None, "workdir": None, "entrypoint": None, "cmd": None}
    assert b"export" not in profile and b"cd " not in profile


@pytest.mark.parametrize(
    "change",
    [
        {"env": {"BAD=NAME": "value"}},
        {"env": {"A": "\0"}},
        {"env": {"A": "x" * 32769}},
        {"env": {f"K{i}": "" for i in range(257)}},
        {"workdir": "relative"},
        {"user": ""},
        {"user": "1000:"},
        {"user": "a:b:c"},
        {"cmd": "echo unsafe"},
        {"entrypoint": ["\0"]},
        {"cmd": ["x" * 20000, "y" * 20000]},
        {"env": {f"K{i}": "x" * 32768 for i in range(33)}},
    ],
)
def test_prepared_metadata_rejects_invalid_values_without_echoing_them(change):
    with pytest.raises(ValueError) as caught:
        metadata(**change)
    assert len(str(caught.value)) < 100


class Builder:
    def __init__(self, name, calls, failure=None):
        self.id, self.calls, self.failure = name, calls, failure

    def sync(self, path, data):
        self.calls.append(("sync", self.id, path, data))

    def fork(self, **options):
        self.calls.append(("fork", self.id, options))
        if self.failure == self.id:
            raise RuntimeError("fork failed")
        return Builder("capture" if self.id == "builder" else "child", self.calls, self.failure)

    def delete(self):
        self.calls.append(("delete", self.id))


def test_prepared_capture_and_child_have_separate_deadlines_and_atomic_policies():
    from arker._modal_prepared import BUILD_POLICY, finish_prepared

    calls = []
    policies = {
        "rules": {
            "computesdk-modal-lifetime": {"type": "schedule", "after_created": 10, "action": "delete"},
            "customer-guard": {"type": "inbound", "auth": "arker"},
        }
    }
    child = finish_prepared(Builder("builder", calls), encoded_metadata=metadata(), final_policies=policies)
    assert child.id == "child"
    assert calls[0][2].endswith("99-arker-image.sh")
    assert calls[1][2].endswith("image-config.json")
    assert calls[2] == (
        "fork",
        "builder",
        {
            "immutable": True,
            "policies": {"rules": {BUILD_POLICY: {"type": "schedule", "after_created": 300, "action": "delete"}}},
        },
    )
    assert calls[3] == (
        "fork",
        "capture",
        {"layers": ["disk"], "start_image": True, "policies": {"rules": {**policies["rules"], BUILD_POLICY: None}}},
    )
    assert calls[4:] == [("delete", "capture"), ("delete", "builder")]
    assert BUILD_POLICY not in policies["rules"]


@pytest.mark.parametrize("failure,deleted", [("builder", ["builder"]), ("capture", ["capture", "builder"])])
def test_prepared_failures_reclaim_all_returned_resources(failure, deleted):
    from arker._modal_prepared import finish_prepared

    calls = []
    with pytest.raises(RuntimeError, match="fork failed"):
        finish_prepared(Builder("builder", calls, failure), encoded_metadata=metadata(), final_policies={})
    assert [call[1] for call in calls if call[0] == "delete"] == deleted


def test_prepared_cancellation_after_child_fork_reclaims_late_child():
    import asyncio
    import threading

    from arker._modal_prepared import finish_prepared

    cancelled = threading.Event()
    calls = []

    class CancellingBuilder(Builder):
        def fork(self, **options):
            result = super().fork(**options)
            if self.id == "capture":
                cancelled.set()
            return CancellingBuilder(result.id, calls)

    with pytest.raises(asyncio.CancelledError):
        finish_prepared(
            CancellingBuilder("builder", calls), encoded_metadata=metadata(), final_policies={}, cancelled=cancelled
        )
    assert [call[1] for call in calls if call[0] == "delete"] == ["child", "capture", "builder"]


def test_prepared_cleanup_failure_preserves_primary_and_owned_id():
    from arker._modal_prepared import finish_prepared

    primary = RuntimeError("build capture failed")
    cleanup = RuntimeError("delete failed")
    calls = []

    class FailedBuilder(Builder):
        def fork(self, **options):
            raise primary

        def delete(self):
            raise cleanup

    with pytest.raises(builtins.ExceptionGroup) as caught:
        finish_prepared(FailedBuilder("builder", calls), encoded_metadata=metadata(), final_policies={})
    assert caught.value.exceptions == (primary, cleanup)
    assert cleanup.vm_id == "builder"


def test_prepared_dispatch_failure_reclaims_recovery_child(monkeypatch):
    from arker._modal_prepared import finish_prepared
    from arker.computer import VM, _server_error

    error = _server_error(
        {
            "code": "unavailable",
            "message": "startup failed",
            "timestamp": "2026-01-01T00:00:00Z",
            "request_id": "prepared",
            "request": {"kind": "matched", "operation_id": "fork"},
            "recovery": {"work": "unknown", "context": {"vm_id": "vm-prepared-recovery-test"}},
        },
        503,
    )
    assert error.body is not None
    calls = []
    client = object()

    class FailingFork(Builder):
        _client = client
        base_url = "https://example.invalid"

        def fork(self, **options):
            raise error

    monkeypatch.setattr(VM, "delete", lambda vm: calls.append(("delete", vm.id)))
    with pytest.raises(type(error)) as caught:
        finish_prepared(FailingFork("builder", calls), encoded_metadata=metadata(), final_policies={})
    assert caught.value is error
    assert [call[1] for call in calls if call[0] == "delete"] == [
        "vm-prepared-recovery-test",
        "builder",
    ]


class RecipeBuilder(Builder):
    def sync(self, path, data=None):
        if data is None:
            return json.dumps({"env": [["BASE", "kept"]], "user": "1000:1000", "workdir": "/work"}).encode()
        return super().sync(path, data)

    def create_session(self):
        from types import SimpleNamespace

        self.calls.append(("session",))
        return SimpleNamespace(session_id="build-session")

    def run(self, command, **options):
        from types import SimpleNamespace

        self.calls.append(("run", command, options))
        return SimpleNamespace(exit_code=0)

    def delete_session(self, session_id):
        self.calls.append(("delete_session", session_id))


@pytest.mark.parametrize("instruction", ["CMD", "ENTRYPOINT"])
@pytest.mark.parametrize("shell_form", [True, False])
def test_recipe_startup_preserves_shell_and_exec_forms(tmp_path, instruction, shell_form):
    import os
    import shlex
    from types import SimpleNamespace

    from arker._modal_prepared import create_prepared, recipe_steps

    output = tmp_path / "output"
    value = "$HOME > literal"
    command = f'printf "%s" "$HOME" > {shlex.quote(str(output))}' if shell_form else json.dumps(["printf", "%s", value])
    other = "ENTRYPOINT" if instruction == "CMD" else "CMD"
    calls = []
    client = SimpleNamespace(fork=lambda **kwargs: RecipeBuilder("builder", calls))
    create_prepared(client, {"image": "fixture"}, recipe_steps([f"{other} []", f"{instruction} {command}"]))
    config = json.loads(next(call[3] for call in calls if call[0] == "sync" and call[2].endswith("image-config.json")))
    startup = " ".join(value for value in [config["entrypoint"], config["cmd"]] if value)
    result = subprocess.run(
        ["sh", "-c", startup], env={**os.environ, "HOME": str(tmp_path)}, check=True, capture_output=True, text=True
    )
    if shell_form:
        assert result.stdout == ""
        assert output.read_text() == str(tmp_path)
    else:
        assert result.stdout == value
        assert not output.exists()


@pytest.mark.parametrize("entry_shell", [False, True])
@pytest.mark.parametrize("cmd_shell", [False, True])
@pytest.mark.parametrize("explicit_shell", [False, True])
def test_recipe_combined_startup_preserves_oci_argv(tmp_path, entry_shell, cmd_shell, explicit_shell):
    import shlex
    from types import SimpleNamespace

    from arker._modal_prepared import create_prepared, recipe_steps

    marker = tmp_path / "must-not-exist"
    raw = f'echo "$HOME" > {shlex.quote(str(marker))}'
    args = ["extra", "$HOME", ">", str(marker)]
    entrypoint = 'printf "%s" entry' if entry_shell else json.dumps(["printf", "%s\\n"])
    cmd = raw if cmd_shell else json.dumps(args)
    commands = [f"ENTRYPOINT {entrypoint}"]
    if explicit_shell:
        commands.append('SHELL ["/bin/sh", "-ec"]')
    commands.append(f"CMD {cmd}")
    calls = []
    client = SimpleNamespace(fork=lambda **kwargs: RecipeBuilder("builder", calls))
    create_prepared(client, {"image": "fixture"}, recipe_steps(commands))
    config = json.loads(next(call[3] for call in calls if call[0] == "sync" and call[2].endswith("image-config.json")))
    result = subprocess.run(
        ["sh", "-c", config["entrypoint"] + " " + config["cmd"]], check=True, capture_output=True, text=True
    )
    cmd_argv = ["/bin/sh", "-ec" if explicit_shell else "-c", raw] if cmd_shell else args
    assert result.stdout == ("entry" if entry_shell else "".join(value + "\n" for value in cmd_argv))
    assert not marker.exists()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_public_create_prepares_recipe_and_applies_lifetime_only_to_child(monkeypatch, asynchronous):
    import asyncio
    from types import SimpleNamespace

    import arker.modal as shim
    from arker._modal_prepared import BUILD_POLICY
    from arker._modal_recipes import Image

    calls = []

    def fork(**request):
        calls.append(("builder_request", request))
        return RecipeBuilder("builder", calls)

    monkeypatch.setattr(shim, "routed_create", lambda name, config, creators: ("arker", creators["arker"]()))
    monkeypatch.setattr("arker._compat.arker_client", lambda config: SimpleNamespace(fork=fork))
    image = Image.from_registry(
        "example/image",
        setup_dockerfile_commands=[
            "RUN printf built > /artifact",
            'ENTRYPOINT ["/bin/sh", "-c"]',
            'CMD ["exec sleep 100"]',
        ],
    )
    guard = {"rules": {"customer-egress": {"type": "outbound_guard", "allow": []}}}
    box = (
        asyncio.run(shim.Sandbox.create.aio(image=image, timeout=10, arker={"policies": guard}))
        if asynchronous
        else shim.Sandbox.create(image=image, timeout=10, arker={"policies": guard})
    )
    assert box.object_id == "child"
    request = calls[0][1]
    assert request["image_config"] == {"cmd": [], "entrypoint": []}
    assert "computesdk-modal-lifetime" not in request["policies"]["rules"]
    assert request["policies"]["rules"][BUILD_POLICY]["after_created"] == 900
    assert request["policies"]["rules"]["computesdk-modal-build-ingress"]["action"] == "deny"
    assert "secrets" not in request["policies"]
    assert request["policies"]["rules"]["customer-egress"] == guard["rules"]["customer-egress"]
    run = next(call for call in calls if call[0] == "run")
    assert run[2]["timeout"] > 10
    child = next(call[2] for call in calls if call[:2] == ("fork", "capture"))
    assert child["policies"]["rules"]["computesdk-modal-lifetime"]["after_created"] == 10
    assert child["policies"]["rules"][BUILD_POLICY] is None
    assert child["policies"]["rules"]["customer-egress"] == guard["rules"]["customer-egress"]
    assert child["policies"]["rules"]["computesdk-modal-build-ingress"] is None
    assert child["policies"]["rules"]["computesdk-modal-prepared-ingress"] == {
        "type": "inbound",
        "priority": 0,
        "action": "allow",
        "auth": "arker",
    }
    config = json.loads(next(call[3] for call in calls if call[0] == "sync" and call[2].endswith("image-config.json")))
    assert config["env"] == [["BASE", "kept"]]
    assert config["user"] == "1000:1000" and config["workdir"] == "/work"
    assert calls[-2:] == [("delete", "capture"), ("delete", "builder")]


@pytest.mark.parametrize("options", [{"env": {"TOKEN": "synthetic"}}, {"workdir": "/other"}])
def test_prepared_create_rejects_child_startup_overrides_before_fork(monkeypatch, options):
    import arker.modal as shim
    from arker._compat import UnsupportedCompatibilityOption
    from arker._modal_recipes import Image

    monkeypatch.setattr(shim, "routed_create", lambda name, config, creators: ("arker", creators["arker"]()))
    monkeypatch.setattr("arker._compat.arker_client", lambda config: pytest.fail("must reject before allocation"))
    image = Image.from_registry("example/image", setup_dockerfile_commands=["ENTRYPOINT []", 'CMD ["true"]'])
    with pytest.raises(UnsupportedCompatibilityOption, match="per-child"):
        shim.Sandbox.create(image=image, **options)


def test_cancelled_public_prepared_create_disposes_late_child(monkeypatch):
    import asyncio
    import threading
    from types import SimpleNamespace

    import arker.modal as shim
    from arker._modal_recipes import Image

    calls = []
    release = threading.Event()

    async def scenario():
        loop = asyncio.get_running_loop()
        entered, disposed = asyncio.Event(), asyncio.Event()

        class DelayedBuilder(RecipeBuilder):
            def run(self, command, **options):
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(3), "test must release the blocked build"
                return super().run(command, **options)

            def fork(self, **options):
                result = super().fork(**options)
                return DelayedBuilder(result.id, calls)

            def delete(self):
                super().delete()
                if self.id == "child":
                    loop.call_soon_threadsafe(disposed.set)

        monkeypatch.setattr(shim, "routed_create", lambda name, config, creators: ("arker", creators["arker"]()))
        monkeypatch.setattr(
            "arker._compat.arker_client",
            lambda config: SimpleNamespace(fork=lambda **request: DelayedBuilder("builder", calls)),
        )
        image = Image.from_registry(
            "example/image", setup_dockerfile_commands=["RUN true", "ENTRYPOINT []", "CMD true"]
        )
        task = asyncio.create_task(shim.Sandbox.create.aio(image=image))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        await asyncio.wait_for(disposed.wait(), 3)
        assert [call[1] for call in calls if call[0] == "delete"] == ["capture", "builder", "child"]

    asyncio.run(scenario())


@pytest.mark.parametrize("explicit", [None, "/explicit"])
def test_reconnected_prepared_exec_uses_image_workdir_unless_overridden(monkeypatch, explicit):
    pytest.importorskip("modal")
    from types import SimpleNamespace

    from arker._compat_process import GuestProcesses
    from arker._modal_prepared import IMAGE_WORKDIR
    from arker.modal import Sandbox

    received = []
    monkeypatch.setattr(
        GuestProcesses, "exec", lambda self, options: received.append(options) or SimpleNamespace(pid="process")
    )
    result = object()
    monkeypatch.setattr("arker._modal_process.modal_process", lambda *args, **kwargs: result)
    vm = SimpleNamespace(id="owned", metadata={IMAGE_WORKDIR: "/image-workdir"})
    box = Sandbox(vm=vm)
    options = {"workdir": explicit} if explicit is not None else {}
    assert box.exec("pwd", **options) is result
    assert received[0]["workingDir"] == (explicit or "/image-workdir")
    assert received[0]["env"] == {}


def test_prepared_startup_secret_is_rejected_before_builder_allocation(monkeypatch):
    import arker.modal as shim
    from arker._compat import UnsupportedCompatibilityOption
    from arker._modal_recipes import Image, Secret

    monkeypatch.setattr(shim, "routed_create", lambda name, config, creators: ("arker", creators["arker"]()))
    monkeypatch.setattr("arker._compat.arker_client", lambda config: pytest.fail("must not allocate"))
    image = Image.from_registry("example/image", setup_dockerfile_commands=["CMD true", "ENTRYPOINT []"])
    with pytest.raises(UnsupportedCompatibilityOption, match="per-child"):
        shim.Sandbox.create(image=image, secrets=[Secret.from_dict({"TOKEN": "synthetic-secret"})])
