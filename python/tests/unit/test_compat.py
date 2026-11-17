"""What the live compatibility tests cannot reach: the default source, and
handing ids Arker does not know to the original provider's package."""

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
from arker.daytona import Daytona, DaytonaNotFoundError
from arker.e2b import Sandbox as E2BSandbox
from arker.e2b import SandboxNotFoundException
from arker.modal import Sandbox as ModalSandbox
from arker.modal import Volume as ModalVolume


@pytest.fixture(autouse=True)
def arker_env(monkeypatch) -> None:
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_test")
    monkeypatch.setenv("ARKER_BASE_URL", "https://test.invalid/api")
    monkeypatch.delenv("ARKER_SOURCE", raising=False)
    monkeypatch.delenv("ARKER_SOURCE_VM", raising=False)
    for module in ("e2b", "modal", "daytona"):
        monkeypatch.setitem(sys.modules, module, None)


@contextmanager
def arker_api(*responses: tuple[str, str, int, dict[str, Any]]):
    """Serve ``(method, path, status, body)`` responses; record request bodies."""
    script = list(responses)
    bodies: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        for index, (method, path, status, body) in enumerate(script):
            if request.method == method and str(request.url) == f"https://test.invalid/api{path}":
                script.pop(index)
                bodies.append(json.loads(request.content) if request.content else None)
                return httpx.Response(status, content=json.dumps(body).encode())
        raise AssertionError(f"unexpected {request.method} {request.url}")

    previous = sdk._http_client
    sdk._http_client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        yield bodies
    finally:
        sdk._http_client.close()
        sdk._http_client = previous


def not_found(vm_id: str) -> tuple[str, str, int, dict[str, Any]]:
    error = {"error": {"code": "not_found", "message": "not found", "timestamp": "2026-01-01T00:00:00Z"}}
    return "GET", f"/v1/vms/{vm_id}", 404, error


def vm(vm_id: str) -> dict[str, Any]:
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


# A source VM with the rules this layer sets, which a new sandbox must not
# take, and one rule of its owner's own.
SOURCE = {
    **vm("ubuntu-coding"),
    "policies": {
        "rules": {
            "compat-port-8000": {
                "type": "inbound",
                "priority": 108000,
                "match": {"ports": [8000]},
                "action": "allow",
                "auth": "open",
            },
            "compat-suspend": {"type": "schedule", "at": "2026-01-01T00:00:00Z", "action": "suspend"},
            "mine": {"type": "outbound", "priority": 5, "action": "allow"},
        }
    },
}


def test_default_source_is_ubuntu_coding() -> None:
    assert arker_source() == "ubuntu-coding"
    assert arker_source({"source": "mine"}) == "mine"


def test_create_forks_the_default_source_with_a_timeout(monkeypatch) -> None:
    written: dict[str, Any] = {}
    monkeypatch.setattr(sdk.VM, "sync", lambda self, path, data=None: written.__setitem__(path, data))
    with arker_api(("GET", "/v1/vms/ubuntu-coding", 200, SOURCE), ("POST", "/v1/fork", 200, vm("vm_new"))) as bodies:
        sandbox = E2BSandbox.create()
    assert sandbox.sandbox_id == "vm_new"
    assert bodies[1]["source_vm_name"] == "ubuntu-coding"
    assert bodies[1]["policies"]["rules"] == {
        "compat-port-8000": None,
        "compat-suspend": None,
        "compat-timeout": {"type": "schedule", "after_created": 300, "action": "delete"},
    }
    assert "export E2B_SANDBOX_ID=vm_new\n" in written["/etc/arker-compat.env"]


def test_unknown_ids_go_to_the_installed_provider(monkeypatch) -> None:
    class Native:
        @classmethod
        def connect(cls, sandbox_id: str, **_: Any) -> str:
            return f"e2b:{sandbox_id}"

        @classmethod
        def from_id(cls, sandbox_id: str, **_: Any) -> str:
            return f"modal:{sandbox_id}"

    class NativeDaytona:
        def __init__(self, config: Any) -> None:
            pass

        def get(self, sandbox_id: str) -> str:
            return f"daytona:{sandbox_id}"

    monkeypatch.setitem(sys.modules, "e2b", types.SimpleNamespace(Sandbox=Native))
    monkeypatch.setitem(sys.modules, "modal", types.SimpleNamespace(Sandbox=Native))
    monkeypatch.setitem(sys.modules, "daytona", types.SimpleNamespace(Daytona=NativeDaytona))
    with arker_api(not_found("old"), not_found("old"), not_found("old")):
        assert E2BSandbox.connect("old") == "e2b:old"
        assert ModalSandbox.from_id("old") == "modal:old"
        assert Daytona().get("old") == "daytona:old"


def test_unknown_ids_without_the_provider_are_not_found() -> None:
    with arker_api(not_found("old"), not_found("old")):
        with pytest.raises(SandboxNotFoundException):
            E2BSandbox.connect("old")
        with pytest.raises(DaytonaNotFoundError):
            Daytona().get("old")


def test_what_is_missing_is_named_with_a_way_forward() -> None:
    with pytest.raises(AttributeError) as missing:
        E2BSandbox.get_mcp_token()
    message = str(missing.value)
    assert message.startswith("Sandbox.get_mcp_token is not supported by Arker's E2B compatibility layer yet.")
    assert "`.arker`" in message and "/issues" in message
    with pytest.raises(ImportError, match="AsyncTemplate is not supported"):
        from arker.e2b import AsyncTemplate  # noqa: F401
    assert not hasattr(E2BSandbox, "beta_create")


def test_options_are_refused_before_creating_or_warned_about() -> None:
    from arker.daytona import CreateSandboxFromSnapshotParams, DaytonaNotSupportedError
    from arker.e2b import NotSupportedException

    with pytest.raises(NotSupportedException, match=r"Sandbox\(mcp=\.\.\.\) is not supported"):
        E2BSandbox.create(mcp={})
    with pytest.raises(DaytonaNotSupportedError, match=r"Daytona\.create\(auto_pause_interval=\.\.\.\)"):
        Daytona().create(CreateSandboxFromSnapshotParams(auto_pause_interval=5))
    with (
        pytest.warns(UserWarning, match=r"auto_stop_interval=\.\.\.\) has no effect"),
        arker_api(
            (
                "GET",
                "/v1/vms/ubuntu-coding",
                500,
                {"error": {"code": "internal", "message": "stop here", "timestamp": "now"}},
            )
        ),
        pytest.raises(Exception, match="stop here"),
    ):
        Daytona().create(CreateSandboxFromSnapshotParams(auto_stop_interval=5))


def test_listing_skips_vms_seen_on_an_earlier_page(monkeypatch) -> None:
    monkeypatch.setenv("ARKER_CONTROL_BASE_URL", "https://test.invalid/api")
    own = {**vm("vm1"), "created_at": "2026-01-01T00:00:00Z", "description": '{"layer": "e2b"}'}
    other = {**vm("vm2"), "description": '{"layer": "modal"}'}
    with arker_api(
        ("GET", "/v1/vms?limit=1000", 200, {"vms": [own, other], "next_cursor": "c2"}),
        ("GET", "/v1/vms?cursor=c2&limit=1000", 200, {"vms": [own], "next_cursor": None}),
    ):
        assert [info.sandbox_id for info in E2BSandbox.list().next_items()] == ["vm1"]


def test_sandbox_envs_written_by_either_sdk_read_back_whole(monkeypatch) -> None:
    from arker._compat import CompatSandbox
    from arker.e2b import ERRORS, SandboxException

    pem = '-----BEGIN KEY-----\nab\'c "d" $e\n-----END KEY-----\n'
    # As the TypeScript SDK writes them, with '\'' for a quote.
    file = {
        "text": "export PEM='-----BEGIN KEY-----\nab'\\''c \"d\" $e\n-----END KEY-----\n'\n"
        "export EMPTY=''\nexport PLAIN=x\n"
    }

    def sync(self, path: str, data: Any = None) -> Any:
        if data is not None:
            file["text"] = data
            return None
        return file["text"].encode()

    monkeypatch.setattr(sdk.VM, "sync", sync)
    sandbox = CompatSandbox(sdk.VM(None, "vm", "https://test.invalid/api", None), ERRORS)
    assert sandbox.env() == {"PEM": pem, "EMPTY": "", "PLAIN": "x"}
    sandbox.update_env({"QUOTED": "it's"})
    assert sandbox.env() == {"PEM": pem, "EMPTY": "", "PLAIN": "x", "QUOTED": "it's"}
    file["text"] = "export BROKEN='unclosed\n"
    with pytest.raises(SandboxException):
        sandbox.env()


def test_a_volume_is_found_past_the_first_page_of_its_prefix(monkeypatch) -> None:
    monkeypatch.setenv("ARKER_CONTROL_BASE_URL", "https://test.invalid/api")

    def volume(name: str) -> dict[str, Any]:
        return {"filesystem_id": f"fs-{name}", "name": name, "owner_org_id": "owner", "created_at": "now"}

    with arker_api(
        ("GET", "/v1/filesystems?name_prefix=data", 200, {"filesystems": [volume("data-old")], "next_cursor": "c2"}),
        ("GET", "/v1/filesystems?cursor=c2&name_prefix=data", 200, {"filesystems": [volume("data")]}),
    ):
        assert ModalVolume.from_name("data")._id(None) == "fs-data"
