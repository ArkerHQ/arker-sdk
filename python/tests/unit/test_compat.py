from __future__ import annotations

import json
import sys
import types
from contextlib import contextmanager
from typing import Any

import httpx2 as httpx
import pytest

import arker.computer as sdk
from arker._compat import arker_source
from arker.blaxel import SandboxInstance as BlaxelSandbox
from arker.daytona import Daytona
from arker.e2b import Sandbox as E2BSandbox
from arker.modal import Sandbox as ModalSandbox


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
    t.add_json(
        lambda method, url: method == "POST" and url == "https://test.invalid/api/v1/fork",
        200,
        vm_payload(vm_id),
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
        alias = sandbox.process.execute_command("echo 'Hello, World!'")
        assert response.result == "Hello, World!\n"
        assert alias.result == "Hello, World!\n"
        sandbox.delete()

    assert json.loads(t.calls[0]["body"])["source_vm_name"] == "catalog-template"
    first_run = json.loads(t.calls[1]["body"])
    assert first_run["command"] == "export FOO=bar; cd /workspace && echo 'Hello, World!'"
    assert first_run["timeout"] == 7


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


def test_e2b_official_surface(monkeypatch) -> None:
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")
    t = FakeTransport()
    add_create_run_delete(t, "vm_e2b", "Hello from E2B Sandbox!\n")

    with use_transport(t):
        sandbox = E2BSandbox.create()
        result = sandbox.commands.run(
            'echo "Hello from E2B Sandbox!"', cwd="/workspace", env={"FOO": "bar"}, timeout=8
        )
        assert result.stdout == "Hello from E2B Sandbox!\n"
        assert result.exit_code == 0
        sandbox.kill()

    run = json.loads(t.calls[1]["body"])
    assert run["command"] == 'export FOO=bar; cd /workspace && echo "Hello from E2B Sandbox!"'
    assert run["timeout"] == 8


def test_modal_official_surface(monkeypatch) -> None:
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
    assert run["command"] == "export FOO=bar; cd /workspace && echo hello"
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
        with pytest.raises(TypeError, match="only supports create"):
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
