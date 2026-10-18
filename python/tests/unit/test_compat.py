from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from contextlib import contextmanager
from typing import Any

import httpx2 as httpx
import pytest

import arker.computer as sdk
from arker._compat import arker_create, arker_source, compat_network_policies
from arker.blaxel import SyncSandboxInstance as BlaxelSandbox
from arker.daytona import Daytona
from arker.e2b import Sandbox as E2BSandbox
from arker.modal import Sandbox as ModalSandbox


def test_blaxel_native_filesystem_retains_options_and_operation_identity():
    calls = []
    native_fs = types.SimpleNamespace(
        mkdir=lambda *args, **kwargs: calls.append((args, kwargs)),
        watch=object(),
    )
    sandbox = BlaxelSandbox(native=types.SimpleNamespace(id="native", fs=native_fs))
    assert sandbox.fs is native_fs
    sandbox.fs.mkdir("private", permissions="0700")
    assert calls == [(("private",), {"permissions": "0700"})]


def test_blaxel_custom_permissions_refuse_before_arker_io():
    from arker._compat import UnsupportedCompatibilityOption
    from arker.blaxel import FileSystem

    with pytest.raises(UnsupportedCompatibilityOption, match=r"permissions.*not supported"):
        FileSystem(vm=object()).mkdir("private", "0700")


@pytest.mark.parametrize("option", ["outbound_cidr_allowlist", "outbound_domain_allowlist"])
def test_modal_empty_allowlist_is_not_unrestricted(monkeypatch, option):
    captured = []
    monkeypatch.setattr("arker.modal.routed_create", lambda _vendor, _config, handlers: ("arker", handlers["arker"]()))
    monkeypatch.setattr(
        "arker.modal.arker_create",
        lambda _config, **options: captured.append(options) or types.SimpleNamespace(id="empty-allowlist"),
    )
    ModalSandbox.create(**{option: []})
    assert captured[0]["policies"] is not None
    assert list(captured[0]["policies"]["rules"].values()) == [{"type": "outbound_guard", "allow": []}]


@pytest.mark.parametrize("value", [{}, {"example.com": True}, False, 7, [7]])
def test_invalid_network_allowlists_never_mean_unrestricted(value):
    with pytest.raises(TypeError, match="allowlist"):
        compat_network_policies(domains=value)


def test_empty_allowlist_semantics_are_provider_specific():
    assert compat_network_policies(domains="", cidrs="") is None
    restricted = compat_network_policies(domains=(), empty_allowlist_blocks=True)
    assert list(restricted["rules"].values()) == [{"type": "outbound_guard", "allow": []}]
    with pytest.raises(ValueError, match="block-all"):
        compat_network_policies(block_all=True, cidrs=[], empty_allowlist_blocks=True)


@pytest.fixture(autouse=True)
def source_env(monkeypatch) -> None:
    monkeypatch.setenv("ARKER_SOURCE", "catalog-template")


class FakeTransport:
    """Scripts httpx responses by (method, url); drive it with ``use_transport``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.script: list[tuple[Any, int, bytes]] = []

    def add_json(self, predicate, status: int, body: dict[str, Any]) -> None:
        self.script.append((predicate, status, json.dumps(body).encode()))

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        url = str(request.url)
        self.calls.append(
            {
                "method": method,
                "url": url,
                "body": request.content or None,
                "headers": request.headers,
            }
        )

        for index, (predicate, status, payload) in enumerate(self.script):
            if predicate(method, url):
                self.script.pop(index)
                return httpx.Response(status, content=payload)

        raise AssertionError(f"no scripted response for {method} {url}")


@contextmanager
def use_transport(t: FakeTransport):
    """Route the SDK's shared client through ``t`` for the duration."""
    previous = sdk._http_client
    sdk._http_client = httpx.Client(transport=httpx.MockTransport(t.handler))
    try:
        yield
    finally:
        sdk._http_client.close()
        sdk._http_client = previous


def vm_payload(vm_id: str) -> dict[str, Any]:
    return {
        "vm_id": vm_id,
        "owner_org_id": "owner",
        "created_at": "now",
        "description": None,
        "public": False,
        "state": "idle",
        "sessions": [],
        "network": {},
        "resources": {},
        "hostname": f"{vm_id}.aws-us-west-2.arker.app",
    }


def completed(stdout: str, stderr: str = "", exit_code: int = 0) -> dict[str, Any]:
    return {
        "stdout": stdout,
        "stdout_encoding": "utf-8",
        "stderr": stderr,
        "stderr_encoding": "utf-8",
        "exit_code": exit_code,
    }


def add_create_run_delete(t: FakeTransport, vm_id: str, stdout: str) -> None:
    if vm_id == "vm_e2b":
        t.add_json(
            lambda method, url: method == "GET" and url == f"https://test.invalid/api/v1/vms/{vm_id}",
            200,
            vm_payload(vm_id),
        )
    t.add_json(
        lambda method, url: method == "POST" and url == "https://test.invalid/api/v1/fork",
        200,
        vm_payload(vm_id),
    )
    if vm_id.startswith("vm_blaxel"):
        t.add_json(
            lambda method, url: method == "POST" and url == f"https://test.invalid/api/v1/vms/{vm_id}/runs",
            200,
            completed(""),
        )
    t.add_json(
        lambda method, url: method == "POST" and url == f"https://test.invalid/api/v1/vms/{vm_id}/runs",
        200,
        completed(stdout),
    )
    t.add_json(
        lambda method, url: method == "DELETE" and url == f"https://test.invalid/api/v1/vms/{vm_id}",
        200,
        {"deleted": True},
    )


def add_arker_not_found(t: FakeTransport, vm_id: str) -> None:
    t.add_json(
        lambda method, url: method == "GET" and url == f"https://test.invalid/api/v1/vms/{vm_id}",
        404,
        {
            "error": {
                "code": "not_found",
                "message": "not found",
                "timestamp": "2026-07-21T00:00:00Z",
            }
        },
    )


def test_shared_fork_keeps_policy_secrets_and_uses_image_source_id(monkeypatch):
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")
    config = {
        "source_vm_id": "vm_image_parent",
        "policies": {"secrets": {"TEST_TOKEN": "synthetic-policy-secret"}},
        "registry_auth": {"username": "synthetic", "password": "synthetic-registry-password"},
    }
    t = FakeTransport()
    for vm_id in ("vm_child", "vm_image"):
        t.add_json(lambda method, url: method == "POST" and url.endswith("/v1/fork"), 200, vm_payload(vm_id))
    with use_transport(t):
        arker_create(config, policies={"policies": [{"type": "outbound", "action": "deny"}]})
        arker_create(config, image="private.example/image@sha256:abc")
    child, image = (json.loads(call["body"]) for call in t.calls)
    assert child["source_vm_id"] == "vm_image_parent"
    assert "source_vm_name" not in child
    assert "registry_auth" not in child
    assert child["policies"] == {
        "secrets": {"TEST_TOKEN": "synthetic-policy-secret"},
        "policies": [{"type": "outbound", "action": "deny"}],
    }
    assert image["registry_auth"] == config["registry_auth"]
    assert "source_vm_id" not in image
    assert config["policies"] == {"secrets": {"TEST_TOKEN": "synthetic-policy-secret"}}


def test_shared_fork_preserves_legacy_named_source_environment(monkeypatch):
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")
    monkeypatch.delenv("ARKER_SOURCE", raising=False)
    monkeypatch.setenv("ARKER_SOURCE_VM", "legacy-source-name")
    t = FakeTransport()
    t.add_json(lambda method, url: method == "POST" and url.endswith("/v1/fork"), 200, vm_payload("vm_child"))
    with use_transport(t):
        arker_create()
    assert json.loads(t.calls[0]["body"])["source_vm_name"] == "legacy-source-name"


@pytest.mark.parametrize("ordered", [False, True])
def test_native_network_floor_preserves_configured_policy_controls(monkeypatch, ordered):
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")
    base = [{"type": "inbound", "action": "deny"}]
    controls = {"policies": base} if ordered else {"rules": {"private-ingress": base[0]}}
    controls["secrets"] = {"TOKEN": "synthetic-only"}
    original = json.loads(json.dumps(controls))
    t = FakeTransport()
    for vm_id in ("vm_one", "vm_two"):
        t.add_json(lambda method, url: method == "POST" and url.endswith("/v1/fork"), 200, vm_payload(vm_id))
    with use_transport(t):
        for _ in range(2):
            arker_create({"policies": controls}, policies=compat_network_policies(block_all=True))
    guard_ids = []
    for call in t.calls:
        written = json.loads(call["body"])["policies"]
        assert written["secrets"] == controls["secrets"]
        if ordered:
            assert written["policies"] == base
        else:
            assert written["rules"]["private-ingress"] == base[0]
        guards = {key: value for key, value in written["rules"].items() if value["type"] == "outbound_guard"}
        assert len(guards) == 1
        assert next(iter(guards.values())) == {"type": "outbound_guard", "allow": []}
        guard_ids.append(next(iter(guards)))
    assert guard_ids[0] != guard_ids[1]
    assert controls == original


@pytest.mark.parametrize(
    "create",
    [
        lambda: E2BSandbox.create(allow_internet_access=False),
        lambda: Daytona().create(network_block_all=True),
        lambda: ModalSandbox.create(block_network=True),
    ],
)
def test_provider_block_network_options_translate_to_arker_policy(monkeypatch, create) -> None:
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")
    t = FakeTransport()
    t.add_json(lambda method, url: method == "POST" and url.endswith("/v1/fork"), 200, vm_payload("vm_policy"))

    with use_transport(t):
        create()

    body = json.loads(t.calls[0]["body"])
    assert len(body["policies"]["rules"]) == 1
    assert next(iter(body["policies"]["rules"].values())) == {"type": "outbound_guard", "allow": []}


def test_allowlist_policies_intersect_inherited_rules(monkeypatch) -> None:
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")
    t = FakeTransport()
    t.add_json(lambda method, url: method == "POST" and url.endswith("/v1/fork"), 200, vm_payload("vm_policy"))

    with use_transport(t):
        ModalSandbox.create(
            outbound_domain_allowlist=["api.example.com"],
            outbound_cidr_allowlist=["203.0.113.0/24"],
        )

    body = json.loads(t.calls[0]["body"])
    assert len(body["policies"]["rules"]) == 1
    assert next(iter(body["policies"]["rules"].values())) == {
        "type": "outbound_guard",
        "allow": [
            {"hosts": ["api.example.com"], "ports": [443]},
            {"ips": ["203.0.113.0/24"]},
        ],
    }


def test_daytona_official_surface(monkeypatch) -> None:
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")
    t = FakeTransport()
    add_create_run_delete(t, "vm_daytona", "Hello, World!\n")
    t.add_json(
        lambda method, url: method == "POST" and url == "https://test.invalid/api/v1/vms/vm_daytona/runs",
        200,
        completed("Hello, World!\n"),
    )

    with use_transport(t):
        daytona = Daytona()
        sandbox = daytona.create()
        response = sandbox.process.exec("echo 'Hello, World!'", cwd="/workspace", env={"FOO": "bar"}, timeout=7)
        alias = sandbox.process.execute_command("echo 'Hello, World!'", "/workspace", {"ALIAS": "yes"}, 9)
        assert response.result == "Hello, World!\n"
        assert alias.result == "Hello, World!\n"
        sandbox.delete()

    assert json.loads(t.calls[0]["body"])["source_vm_name"] == "catalog-template"
    first_run = json.loads(t.calls[1]["body"])
    assert first_run["command"] == "(\nexport FOO=bar; cd /workspace && echo 'Hello, World!'\n)"
    assert first_run["timeout"] == 7
    alias_run = json.loads(t.calls[2]["body"])
    assert alias_run["command"] == "(\nexport ALIAS=yes; cd /workspace && echo 'Hello, World!'\n)"
    assert alias_run["timeout"] == 9


@pytest.mark.parametrize("keyword", [False, True])
def test_daytona_accepts_typed_create_params_and_gib_resources(monkeypatch, keyword):
    from dataclasses import dataclass

    @dataclass
    class Resources:
        cpu: int = 4
        memory: int = 8
        disk: int = 20
        gpu: None = None

    class Params:
        def model_dump(self, *, exclude_none):
            assert exclude_none is True
            return {"image": "ubuntu:24.04", "name": "typed", "resources": Resources()}

    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")
    transport = FakeTransport()
    transport.add_json(lambda method, url: method == "POST" and url.endswith("/v1/fork"), 200, vm_payload("typed"))
    with use_transport(transport):
        if keyword:
            Daytona().create(params=Params())
        else:
            Daytona().create(Params())
    assert json.loads(transport.calls[0]["body"])["resources"] == {"vcpu": 4, "memory_mib": 8192, "disk_mib": 20480}


def test_daytona_does_not_drop_unsupported_resource_requests(monkeypatch):
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")
    with use_transport(FakeTransport()), pytest.raises(TypeError, match="gpu"):
        Daytona().create(image="ubuntu:24.04", resources={"cpu": 4, "gpu": 1})


def test_compat_requires_source(monkeypatch) -> None:
    monkeypatch.delenv("ARKER_SOURCE", raising=False)
    monkeypatch.delenv("ARKER_SOURCE_VM", raising=False)

    with pytest.raises(ValueError, match="Arker source is required"):
        arker_source()


def test_arker_shim_config_customizes_source_without_forwarding_to_client(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url == "https://test.invalid/api/v1/fork",
        200,
        vm_payload("vm_custom"),
    )

    with use_transport(t):
        sandbox = Daytona(arker={"source": "custom-source", "name": "custom-name"}).create()

    body = json.loads(t.calls[0]["body"])
    assert sandbox.id == "vm_custom"
    assert body["source_vm_name"] == "custom-source"
    assert body["name"] == "custom-name"


def test_python_shims_translate_or_reject_arker_create_options(monkeypatch) -> None:
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")
    t = FakeTransport()
    for vm_id in ("vm_daytona_image", "vm_blaxel_image"):
        t.add_json(
            lambda method, url: method == "POST" and url == "https://test.invalid/api/v1/fork",
            200,
            vm_payload(vm_id),
        )
    t.add_json(
        lambda method, url: method == "POST" and url == "https://test.invalid/api/v1/vms/vm_blaxel_image/runs",
        200,
        completed(""),
    )

    with use_transport(t):
        Daytona().create(image="ubuntu:24.04", name="daytona", resources={"cpu": 4, "memory": 8})
        BlaxelSandbox.create({"image": "ubuntu:24.04", "name": "blaxel", "memory": 4096})
        with pytest.raises(TypeError, match="envVars"):
            Daytona().create(envVars={"A": "b"})
        with pytest.raises(TypeError, match="ttl"):
            BlaxelSandbox.create({"ttl": "5m"})

    daytona = json.loads(t.calls[0]["body"])
    assert daytona == {
        "image": "ubuntu:24.04",
        "name": "daytona",
        "resources": {"vcpu": 4, "memory_mib": 8192},
    }
    blaxel = json.loads(t.calls[1]["body"])
    assert blaxel == {
        "image": "ubuntu:24.04",
        "name": "blaxel",
        "resources": {"memory_mib": 4096},
    }


def test_e2b_official_surface(monkeypatch) -> None:
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")
    t = FakeTransport()
    add_create_run_delete(t, "vm_e2b", "Hello from E2B Sandbox!\n")

    with use_transport(t):
        sandbox = E2BSandbox.create()
        result = sandbox.commands.run('echo "Hello from E2B Sandbox!"', cwd="/workspace", env={"FOO": "bar"}, timeout=8)
        assert result.stdout == "Hello from E2B Sandbox!\n"
        assert result.exit_code == 0
        assert sandbox.get_host(8080) == "vm_e2b.aws-us-west-2.arker.app"
        assert sandbox.getHost(3000) == "vm_e2b-3000.aws-us-west-2.arker.app"
        sandbox.kill()

    run = json.loads(t.calls[1]["body"])
    assert run["command"] == '(\nexport FOO=bar; cd /workspace && echo "Hello from E2B Sandbox!"\n)'
    assert run["timeout"] == 8


def test_e2b_nested_native_surface_delegates_provider_extensions() -> None:
    native_commands = types.SimpleNamespace(list=lambda: ["command-1"])
    native_files = types.SimpleNamespace(watch=lambda path: f"watch:{path}")
    native = types.SimpleNamespace(
        sandbox_id="native-e2b",
        commands=native_commands,
        files=native_files,
        get_host=lambda port: f"native-{port}.example",
    )
    sandbox = E2BSandbox(native=native)

    assert sandbox.commands.list() == ["command-1"]
    assert sandbox.files.watch("/tmp") == "watch:/tmp"
    assert sandbox.get_host(3000) == "native-3000.example"


def test_modal_official_surface(monkeypatch) -> None:
    monkeypatch.setattr("arker.modal.import_optional", lambda _: None)
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")
    t = FakeTransport()
    add_create_run_delete(t, "vm_modal", "hello\n")

    with use_transport(t):
        sandbox = ModalSandbox.create()
        process = sandbox.exec("echo", "hello", workdir="/workspace", env={"FOO": "bar"}, timeout_ms=1_500)
        assert process.stdout.read() == "hello\n"
        assert process.wait() == 0
        sandbox.terminate()

    run = json.loads(t.calls[1]["body"])
    assert run["command"] == "(\nexport FOO=bar; cd /workspace && echo hello\n)"
    assert run["timeout"] == 2


def test_blaxel_official_surface(monkeypatch) -> None:
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")
    t = FakeTransport()
    add_create_run_delete(t, "vm_blaxel", "hello from blaxel\n")

    with use_transport(t):
        sandbox = BlaxelSandbox.create()
        result = sandbox.process.exec("echo 'hello from blaxel'")
        assert result.stdout == "hello from blaxel\n"
        assert result.exit_code == 0
        sandbox.delete()


@pytest.mark.parametrize("transport_failure", [False, True])
def test_blaxel_initialization_failure_deletes_created_vm(monkeypatch, transport_failure) -> None:
    module = importlib.import_module("arker.blaxel")
    deleted = []
    vm = types.SimpleNamespace(delete=lambda: deleted.append(True))
    monkeypatch.setattr(module, "routed_create", lambda *args: ("arker", vm))

    def initialize(*args):
        if transport_failure:
            raise RuntimeError("initialization failed")
        return module.CommandResult(stdout="", stderr="initialization failed", exit_code=1)

    monkeypatch.setattr(module, "run_command", initialize)
    with pytest.raises(RuntimeError, match="initialization failed"):
        module.SyncSandboxInstance.create()
    assert deleted == [True]


def test_blaxel_native_surface_delegates_without_losing_provider_features() -> None:
    class NativeFileSystem:
        def find(self, path: str, options: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            return path, options

    class NativeProcess:
        def list(self) -> list[str]:
            return ["pid-1"]

    native = types.SimpleNamespace(
        id="native-box",
        metadata={"name": "native-box"},
        fs=NativeFileSystem(),
        process=NativeProcess(),
        drives=types.SimpleNamespace(list=lambda: ["drive-1"]),
        snapshot=lambda name=None: f"snapshot:{name}",
    )
    sandbox = BlaxelSandbox(native=native)

    assert sandbox.fs.find("/app", {"patterns": ["*.md"]}) == ("/app", {"patterns": ["*.md"]})
    assert sandbox.process.list() == ["pid-1"]
    assert sandbox.drives.list() == ["drive-1"]
    assert sandbox.snapshot("checkpoint") == "snapshot:checkpoint"


def test_blaxel_typed_request_and_directory_attributes(monkeypatch) -> None:
    from arker._compat_process import GuestProcesses

    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")

    class TypedRequest:
        def to_dict(self):
            return {"command": "printf typed", "workingDir": "/tmp", "waitForCompletion": True, "timeout": 1500}

    captured = []

    def execute(self, request):
        captured.append(request)
        return types.SimpleNamespace(stdout="typed")

    monkeypatch.setattr(GuestProcesses, "exec", execute)
    t = FakeTransport()
    add_create_run_delete(t, "vm_blaxel_typed", "f\0probe.txt\0d\0nested\0complete\0")
    with use_transport(t):
        sandbox = BlaxelSandbox.create()
        assert sandbox.process.exec(TypedRequest()).stdout == "typed"
        listing = sandbox.fs.ls("/blaxel")
        assert listing.path == "/blaxel"
        assert listing.files[0].name == "probe.txt"
        assert listing.files[0].path == "/blaxel/probe.txt"
        assert listing.subdirectories[0].name == "nested"
        sandbox.process.exec({"command": "sleep 10", "waitForCompletion": False})
        sandbox.process.exec({"command": "true", "timeout": 0})
        sandbox.delete()
    assert captured[0] == {
        "command": "printf typed",
        "workingDir": "/tmp",
        "waitForCompletion": True,
        "timeout": 1500000,
        "signalExitCode": -1,
    }
    assert captured[1]["waitForCompletion"] is False
    assert "timeout" not in captured[1]
    assert captured[2]["timeout"] == 0


@pytest.mark.skipif(sys.platform != "linux", reason="Exercises the Linux guest directory command")
@pytest.mark.parametrize("terminal", [False, True])
def test_blaxel_listing_preserves_control_characters_in_names(monkeypatch, tmp_path, terminal):
    import errno
    import os
    import pty
    import subprocess
    import tty

    from arker._compat import CommandResult
    from arker.blaxel import FileSystem

    names = ["line\nbreak", "tab\tfile", "-leading", ".hidden"]
    for name in names:
        (tmp_path / name).write_text("content")
    (tmp_path / "directory\nname").mkdir()
    (tmp_path / "dangling").symlink_to("missing")

    def execute(_vm, command):
        if not terminal:
            return CommandResult(subprocess.check_output(["/bin/sh", "-c", command], text=True), "", 0)
        master, slave = pty.openpty()
        tty.setraw(slave)
        output = bytearray()
        try:
            with subprocess.Popen(["/bin/sh", "-c", command], stdout=slave) as process:
                os.close(slave)
                while True:
                    try:
                        block = os.read(master, 65536)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        break
                    if not block:
                        break
                    output.extend(block)
            return CommandResult(output.decode(), "", process.returncode)
        finally:
            os.close(master)

    monkeypatch.setattr("arker.blaxel.run_command", execute)
    listing = FileSystem(vm=object()).ls(str(tmp_path))
    assert sorted(entry.name for entry in listing.files) == sorted([*names, "dangling"])
    assert [entry.name for entry in listing.subdirectories] == ["directory\nname"]


@pytest.mark.parametrize("stdout", ["", "f\0name\0", "f\0complete\0", "f\0name\0complete\0truncated"])
def test_blaxel_listing_rejects_incomplete_producer_output(monkeypatch, stdout):
    from arker._compat import CommandResult
    from arker.blaxel import FileSystem

    monkeypatch.setattr("arker.blaxel.run_command", lambda *_: CommandResult(stdout, "", 0))
    with pytest.raises(RuntimeError, match="Incomplete directory listing"):
        FileSystem(vm=object()).ls("/missing")


def test_blaxel_native_typed_requests_are_passed_through() -> None:
    request = object()
    native = types.SimpleNamespace(process=types.SimpleNamespace(exec=lambda value: value))
    sandbox = BlaxelSandbox(native=native)
    assert sandbox.process.exec(request) is request


def test_blaxel_binary_files_preserve_every_byte_and_local_path_semantics(tmp_path) -> None:
    from arker.blaxel import FileSystem

    stored = {}

    def sync(path, content=None):
        if content is None:
            return stored[path]
        stored[path] = content
        return None

    fs = FileSystem(vm=types.SimpleNamespace(sync=sync))
    payload = bytes(range(256))
    fs.write_binary("binary", payload)
    assert fs.read_binary("binary") == payload
    source = tmp_path / "input"
    source.write_bytes(payload[::-1])
    fs.write_binary("binary", str(source))
    assert fs.read_binary("binary") == payload[::-1]
    destination = tmp_path / "output"
    fs.download("binary", str(destination), mode=0o600)
    assert destination.read_bytes() == payload[::-1]
    assert destination.stat().st_mode & 0o777 == 0o600
    fs.write_binary("empty", bytearray())
    assert fs.read_binary("empty") == b""


def test_blaxel_mkdir_returns_native_success_on_both_routes(monkeypatch) -> None:
    from arker._compat import CommandResult
    from arker.blaxel import FileSystem

    native_response = object()
    calls = []

    def native_mkdir(path, permissions="0755"):
        calls.append((path, permissions))
        return native_response

    native = types.SimpleNamespace(mkdir=native_mkdir)
    assert FileSystem(native=native).mkdir("relative") is native_response
    assert FileSystem(native=native).mkdir("private", "0700") is native_response
    assert calls == [("relative", "0755"), ("private", "0700")]
    monkeypatch.setattr("arker.blaxel.run_command", lambda *_: CommandResult(stdout="", stderr="", exit_code=0))
    assert FileSystem(vm=object()).mkdir("relative").to_dict() == {
        "message": "Directory created successfully",
        "path": "/blaxel/relative",
    }


def test_blaxel_success_serialization_without_optional_sdk(monkeypatch) -> None:
    from arker._compat_process import success

    monkeypatch.setattr("arker._compat_process.import_optional", lambda _: None)
    assert success({"message": "done", "path": "/blaxel/test"}).to_dict() == {
        "message": "done",
        "path": "/blaxel/test",
    }
    assert success({"message": "done"}).to_dict() == {"message": "done"}


def test_blaxel_remove_is_nonrecursive_unless_requested(monkeypatch) -> None:
    from arker._compat import CommandResult
    from arker.blaxel import FileSystem

    calls = []

    def command(vm, script):
        calls.append(script)
        return CommandResult(stdout="file", stderr="", exit_code=0)

    monkeypatch.setattr("arker.blaxel.run_command", command)
    fs = FileSystem(vm=object())
    fs.rm("/blaxel/directory")
    assert "rm -rf" not in calls[-1]
    assert "rmdir --" in calls[-1]
    fs.rm("/blaxel/directory", recursive=True)
    assert "rm -r --" in calls[-1]


def test_blaxel_async_surface_uses_shared_router(monkeypatch) -> None:
    from arker._compat_process import GuestProcesses
    from arker.blaxel import SandboxInstance

    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")
    t = FakeTransport()
    add_create_run_delete(t, "vm_blaxel_async", "async")
    monkeypatch.setattr(GuestProcesses, "exec", lambda self, request: types.SimpleNamespace(stdout="async"))

    async def program():
        sandbox = await SandboxInstance.create()
        try:
            assert (
                await sandbox.process.exec({"command": "printf async", "wait_for_completion": False})
            ).stdout == "async"
            assert sandbox.metadata.name == "vm_blaxel_async"
        finally:
            await sandbox.delete()

    with use_transport(t):
        asyncio.run(program())


@pytest.mark.parametrize(
    "shim_name,native_name",
    [
        ("arker.e2b", "e2b"),
        ("arker.daytona", "daytona"),
        ("arker.modal", "modal"),
        ("arker.blaxel", "blaxel.core.sandbox"),
    ],
)
def test_compat_imports_preserve_vendor_builders(monkeypatch, shim_name, native_name):
    shim = importlib.import_module(shim_name)
    builder = object()
    monkeypatch.setitem(sys.modules, native_name, types.SimpleNamespace(CreationBuilder=builder))
    assert shim.CreationBuilder is builder


def test_unsupported_calls_fail_fast(monkeypatch) -> None:
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url == "https://test.invalid/api/v1/fork",
        200,
        vm_payload("vm_unsupported"),
    )

    with use_transport(t):
        sandbox = E2BSandbox.create()
        with pytest.raises(
            AttributeError,
            match="not supported by the Arker compatibility layer for e2b",
        ):
            sandbox.files.rename  # noqa: B018
        with pytest.raises(TypeError, match="not supported by Arker: image"):
            ModalSandbox.create(image="alpine")


def test_daytona_get_falls_back_to_native_sdk(monkeypatch) -> None:
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")

    class NativeProcess:
        def exec(self, command: str) -> Any:
            return types.SimpleNamespace(result=f"native:{command}")

    class NativeSandbox:
        id = "native_daytona"
        process = NativeProcess()

        def delete(self) -> None:
            self.deleted = True

    class NativeDaytona:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def get(self, sandbox_id: str) -> NativeSandbox:
            assert sandbox_id == "native_daytona"
            return NativeSandbox()

    monkeypatch.setitem(sys.modules, "daytona", types.SimpleNamespace(Daytona=NativeDaytona))
    t = FakeTransport()
    add_arker_not_found(t, "native_daytona")

    with use_transport(t):
        sandbox = Daytona(api_key="native_key").get("native_daytona")
        assert sandbox.id == "native_daytona"
        assert sandbox.process.exec("echo native").result == "native:echo native"


def test_daytona_delete_by_id_falls_back_to_native_sdk(monkeypatch) -> None:
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")
    deleted: list[str] = []

    class NativeDaytona:
        def delete(self, sandbox_id: str) -> None:
            deleted.append(sandbox_id)

    monkeypatch.setitem(sys.modules, "daytona", types.SimpleNamespace(Daytona=NativeDaytona))
    t = FakeTransport()
    add_arker_not_found(t, "native_daytona")

    with use_transport(t):
        Daytona().delete("native_daytona")

    assert deleted == ["native_daytona"]


def test_e2b_connect_falls_back_to_native_sdk(monkeypatch) -> None:
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")

    class NativeCommands:
        def run(self, command: str) -> Any:
            return types.SimpleNamespace(stdout=f"native:{command}")

    class NativeSandbox:
        sandbox_id = "native_e2b"
        commands = NativeCommands()

        @classmethod
        def get_info(cls, sandbox_id: str) -> Any:
            assert sandbox_id == "native_e2b"
            return types.SimpleNamespace(sandbox_id=sandbox_id)

        @classmethod
        def connect(cls, sandbox_id: str) -> NativeSandbox:
            assert sandbox_id == "native_e2b"
            return cls()

    monkeypatch.setitem(sys.modules, "e2b", types.SimpleNamespace(Sandbox=NativeSandbox))
    t = FakeTransport()
    add_arker_not_found(t, "native_e2b")

    with use_transport(t):
        sandbox = E2BSandbox.connect("native_e2b")
        assert sandbox.sandbox_id == "native_e2b"
        assert sandbox.commands.run("echo native").stdout == "native:echo native"


def test_modal_from_id_falls_back_to_native_sdk(monkeypatch) -> None:
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")

    class NativeProcess:
        stdout = types.SimpleNamespace(read=lambda: "native stdout")
        stderr = types.SimpleNamespace(read=lambda: "")

        def wait(self) -> int:
            return 0

    class NativeSandbox:
        sandbox_id = "native_modal"

        @classmethod
        def from_id(cls, sandbox_id: str) -> NativeSandbox:
            assert sandbox_id == "native_modal"
            return cls()

        def exec(self, *command: Any) -> NativeProcess:
            assert command == ("echo", "native")
            return NativeProcess()

    monkeypatch.setitem(sys.modules, "modal", types.SimpleNamespace(Sandbox=NativeSandbox))
    t = FakeTransport()
    add_arker_not_found(t, "native_modal")

    with use_transport(t):
        sandbox = ModalSandbox.from_id("native_modal")
        process = sandbox.exec("echo", "native")
        assert sandbox.sandbox_id == "native_modal"
        assert process.stdout.read() == "native stdout"
        assert process.wait() == 0
