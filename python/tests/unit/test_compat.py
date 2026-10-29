from __future__ import annotations

import importlib
import json
import shlex
import subprocess
import sys
import types
from typing import Any

import pytest
from e2b_facade_helpers import delegation_response
from e2b_facade_helpers import mock_envd_bootstrap as mock_envd_bootstrap
from scripted_transport import FakeTransport, use_transport

from arker._compat import arker_create, arker_source, compat_network_policies
from arker.blaxel import SyncSandboxInstance as BlaxelSandbox
from arker.daytona import Daytona
from arker.e2b import Sandbox as E2BSandbox
from arker.modal import Sandbox as ModalSandbox

pytestmark = pytest.mark.usefixtures("mock_envd_bootstrap")


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
    assert list(captured[0]["policies"]["rules"].values()) == [
        {"type": "outbound_guard", "allow": []},
        {"type": "schedule", "after_created": 300, "action": "delete"},
    ]


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


@pytest.fixture
def arker_env(monkeypatch):
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")


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


def test_shared_fork_keeps_policy_secrets_and_uses_image_source_id(arker_env):
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


def test_shared_fork_preserves_legacy_named_source_environment(monkeypatch, arker_env):
    monkeypatch.delenv("ARKER_SOURCE", raising=False)
    monkeypatch.setenv("ARKER_SOURCE_VM", "legacy-source-name")
    t = FakeTransport()
    t.add_json(lambda method, url: method == "POST" and url.endswith("/v1/fork"), 200, vm_payload("vm_child"))
    with use_transport(t):
        arker_create()
    assert json.loads(t.calls[0]["body"])["source_vm_name"] == "legacy-source-name"


@pytest.mark.parametrize("configured_id", [None, "configured-parent-id"])
def test_shared_fork_accepts_unambiguous_parent_id_environment(monkeypatch, arker_env, configured_id):
    monkeypatch.setenv("ARKER_SOURCE_VM_ID", "environment-parent-id")
    monkeypatch.setenv("ARKER_SOURCE_VM", "legacy-source-name")
    transport = FakeTransport()
    transport.add_json(lambda method, url: method == "POST" and url.endswith("/v1/fork"), 200, vm_payload("vm_child"))
    with use_transport(transport):
        arker_create({"source_vm_id": configured_id} if configured_id else {})
    request = json.loads(transport.calls[0]["body"])
    assert request["source_vm_id"] == (configured_id or "environment-parent-id")
    assert "source_vm_name" not in request


@pytest.mark.parametrize("ordered", [False, True])
def test_native_network_floor_preserves_configured_policy_controls(arker_env, ordered):
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
def test_provider_block_network_options_translate_to_arker_policy(arker_env, create) -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "GET" and url.endswith("/v1/vms/catalog-template"),
        200,
        vm_payload("catalog-template"),
    )
    t.add_json(lambda method, url: method == "POST" and url.endswith("/v1/fork"), 200, vm_payload("vm_policy"))
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_policy/file-transfer-delegation"),
        200,
        delegation_response(),
    )

    with use_transport(t):
        create()

    body = json.loads(next(call["body"] for call in t.calls if call["method"] == "POST"))
    guards = [rule for rule in body["policies"]["rules"].values() if rule["type"] == "outbound_guard"]
    assert guards == [{"type": "outbound_guard", "allow": []}]


def test_allowlist_policies_intersect_inherited_rules(arker_env) -> None:
    t = FakeTransport()
    t.add_json(lambda method, url: method == "POST" and url.endswith("/v1/fork"), 200, vm_payload("vm_policy"))

    with use_transport(t):
        ModalSandbox.create(
            outbound_domain_allowlist=["api.example.com"],
            outbound_cidr_allowlist=["203.0.113.0/24"],
        )

    body = json.loads(t.calls[0]["body"])
    assert body["policies"]["rules"].pop("computesdk-modal-lifetime") == {
        "type": "schedule",
        "after_created": 300,
        "action": "delete",
    }
    assert len(body["policies"]["rules"]) == 1
    assert next(iter(body["policies"]["rules"].values())) == {
        "type": "outbound_guard",
        "allow": [
            {"hosts": ["api.example.com"], "ports": [443]},
            {"ips": ["203.0.113.0/24"]},
        ],
    }


def test_daytona_official_surface(arker_env) -> None:
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
    for call, env, timeout in zip(t.calls[1:3], ({"FOO": "bar"}, {"ALIAS": "yes"}), (7, 9), strict=True):
        run = json.loads(call["body"])
        assert run["command"].startswith(
            '(\nsetpriv --reuid=daytona --regid="$(id -g daytona)" --init-groups -- python3 -I '
        )
        assert json.loads(shlex.split(run["command"])[-2]) == {
            "operation": "exec",
            "command": "echo 'Hello, World!'",
            "cwd": "/workspace",
            "env": env,
        }
        assert run["timeout"] == timeout


@pytest.mark.parametrize("keyword", [False, True])
def test_daytona_accepts_typed_create_params_and_gib_resources(arker_env, keyword):
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

    transport = FakeTransport()
    transport.add_json(lambda method, url: method == "POST" and url.endswith("/v1/fork"), 200, vm_payload("typed"))
    with use_transport(transport):
        if keyword:
            Daytona().create(params=Params())
        else:
            Daytona().create(Params())
    assert json.loads(transport.calls[0]["body"])["resources"] == {"vcpu": 4, "memory_mib": 8192, "disk_mib": 20480}


def test_daytona_does_not_drop_unsupported_resource_requests(arker_env):
    with use_transport(FakeTransport()), pytest.raises(TypeError, match="gpu"):
        Daytona().create(image="ubuntu:24.04", resources={"cpu": 4, "gpu": 1})


def test_compat_requires_source(monkeypatch) -> None:
    monkeypatch.delenv("ARKER_SOURCE", raising=False)
    monkeypatch.delenv("ARKER_SOURCE_VM", raising=False)

    with pytest.raises(ValueError, match="Arker source is required"):
        arker_source()


def test_arker_shim_config_customizes_source_without_forwarding_to_client(
    monkeypatch,
    arker_env,
) -> None:
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


def test_python_shims_translate_or_reject_arker_create_options(arker_env, monkeypatch) -> None:
    monkeypatch.setenv("ARKER_CONTROL_BASE_URL", "https://test.invalid/api")
    t = FakeTransport()
    monkeypatch.setattr("arker._compat_blaxel_bootstrap.bootstrap_blaxel", lambda *args: None)
    monkeypatch.setattr("arker._compat_blaxel_data_plane.validate_native", lambda: None)
    monkeypatch.setattr("arker._compat_blaxel_data_plane.BlaxelDataPlane", lambda *args: object())
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
    assert t.calls[1]["headers"]["Arker-VM-Name-Mode"] == "create"
    blaxel = json.loads(t.calls[1]["body"])
    assert blaxel == {
        "image": "ubuntu:24.04",
        "name": "blaxel",
        "resources": {"memory_mib": 4096},
    }


def test_e2b_control_plane_surface(arker_env) -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "GET" and url == "https://test.invalid/api/v1/vms/catalog-template",
        200,
        vm_payload("catalog-template"),
    )

    t.add_json(
        lambda method, url: (
            method == "POST" and url == "https://test.invalid/api/v1/vms/vm_e2b/file-transfer-delegation"
        ),
        200,
        delegation_response(),
    )
    add_create_run_delete(t, "vm_e2b", "")
    with use_transport(t):
        sandbox = E2BSandbox.create()
        assert sandbox.get_host(8080) == "vm_e2b.aws-us-west-2.arker.app"
        assert sandbox.getHost(3000) == "vm_e2b-3000.aws-us-west-2.arker.app"
        assert sandbox.kill()
    assert not any("/runs" in call["url"] for call in t.calls)


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


def test_e2b_native_git_runs_workflow_against_local_repository(tmp_path):
    from e2b.sandbox_sync.git import Git

    def run(command, **options):
        result = subprocess.run(command, shell=True, capture_output=True, text=True, cwd=options.get("cwd"), check=True)
        return types.SimpleNamespace(stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode)

    commands = types.SimpleNamespace(run=run)
    native = types.SimpleNamespace(sandbox_id="native", commands=commands, git=Git(commands))
    sandbox = E2BSandbox(native=native)
    remote, checkout = tmp_path / "remote.git", tmp_path / "checkout"
    sandbox.git.init(str(remote), bare=True, initial_branch="main", cwd=str(tmp_path))
    sandbox.git.clone(str(remote), str(checkout), cwd=str(tmp_path))
    sandbox.git.configure_user("Arker Test", "test@arker.invalid", scope="local", path=str(checkout), cwd=str(tmp_path))
    (checkout / "hello.txt").write_text("hello\n")
    sandbox.git.add(str(checkout), files=["hello.txt"], cwd=str(tmp_path))
    sandbox.git.commit(str(checkout), "initial", cwd=str(tmp_path))
    sandbox.git.create_branch(str(checkout), "feature", cwd=str(tmp_path))
    assert sandbox.git.status(str(checkout), cwd=str(tmp_path)).is_clean
    assert sandbox.git.branches(str(checkout), cwd=str(tmp_path)).current_branch == "feature"


def test_modal_official_surface(monkeypatch, arker_env) -> None:
    import base64

    monkeypatch.setattr("arker.modal.import_optional", lambda _: None)
    t = FakeTransport()
    t.add_json(lambda method, url: method == "POST" and url.endswith("/fork"), 200, vm_payload("vm_modal"))
    for output in ("", "", "hello\n", ""):
        t.add_json(lambda method, url: method == "POST" and url.endswith("/runs"), 200, completed(output))
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/sync"),
        200,
        {
            "ok": True,
            "op": "write",
            "results": [
                {
                    "path": "/private-control",
                    "size": 0,
                    "received_bytes": 0,
                    "ranges": [],
                    "complete": True,
                    "written": True,
                }
            ],
        },
    )
    t.add_json(lambda method, url: method == "DELETE", 200, {"deleted": True})
    with use_transport(t):
        sandbox = ModalSandbox.create()
        process = sandbox.exec("echo", "hello", workdir="/workspace", env={"FOO": "bar"}, timeout_ms=1_500)
        assert process.stdout.read() == "hello\n"
        assert process.wait() == 0
        sandbox.terminate()
    runs = [json.loads(call["body"]) for call in t.calls if call["url"].endswith("/runs")]
    assert all("export FOO=bar" not in run["command"] for run in runs)
    assert " exec exec-" in runs[2]["command"]
    assert runs[2]["timeout"] == 2
    write = next(json.loads(call["body"])["writes"][0] for call in t.calls if call["url"].endswith("/sync"))
    assert write["path"].startswith("/root/.arker-modal/exec-")
    assert json.loads(base64.b64decode(write["content"])) == {
        "argv": ["echo", "hello"],
        "env": {"FOO": "bar"},
        "workdir": "/workspace",
        "defaults": False,
    }


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


def test_blaxel_native_typed_requests_are_passed_through() -> None:
    request = object()
    native = types.SimpleNamespace(process=types.SimpleNamespace(exec=lambda value: value))
    sandbox = BlaxelSandbox(native=native)
    assert sandbox.process.exec(request) is request


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


def test_unsupported_calls_fail_fast(arker_env) -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "GET" and url.endswith("/v1/vms/catalog-template"),
        200,
        vm_payload("catalog-template"),
    )
    t.add_json(
        lambda method, url: method == "POST" and url == "https://test.invalid/api/v1/fork",
        200,
        vm_payload("vm_unsupported"),
    )
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_unsupported/file-transfer-delegation"),
        200,
        delegation_response(),
    )

    with use_transport(t):
        sandbox = E2BSandbox.create()
        assert sandbox.files is sandbox._data_plane.files
        calls_before = len(t.calls)
        with pytest.raises(TypeError, match="opaque native image handles are unsupported"):
            ModalSandbox.create(image="alpine")
        assert len(t.calls) == calls_before


def test_daytona_get_falls_back_to_native_sdk(monkeypatch, arker_env) -> None:

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


def test_daytona_delete_by_id_falls_back_to_native_sdk(monkeypatch, arker_env) -> None:
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


def test_e2b_connect_falls_back_to_native_sdk(monkeypatch, arker_env) -> None:

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


def test_modal_from_id_falls_back_to_native_sdk(monkeypatch, arker_env) -> None:

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
