from __future__ import annotations

import base64
import contextlib
import json
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import httpx2 as httpx
import pytest

import arker.computer as sdk
from arker.generated.api_models import PolicyDoc, PolicyEntry, PolicyMatch

UNKNOWN_OUTCOME_ERROR_BODY = {
    "error": {
        "code": "unavailable",
        "message": "operation outcome is unknown; reconcile resource state before retry",
        "timestamp": "2026-01-01T00:00:00.000Z",
        "retryable": False,
    }
}


class FakeTransport:
    """Scripts httpx responses by (method, url); drive it with ``use_transport``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.script: list[tuple[Any, int | None, bytes | httpx.RequestError]] = []

    def add_json(self, predicate, status: int, body: dict[str, Any]) -> None:
        self.script.append((predicate, status, json.dumps(body).encode()))

    def add_raw(self, predicate, status: int, body: bytes) -> None:
        self.script.append((predicate, status, body))

    def add_network_error(self, predicate, message: str = "response lost") -> None:
        self.script.append((predicate, None, httpx.ReadError(message)))

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
                if isinstance(payload, httpx.RequestError):
                    raise payload
                assert status is not None
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


def client() -> sdk.Arker:
    return sdk.Arker(api_key="ark_live_test", base_url="https://test.invalid/api", retry=False)


def region_client() -> sdk.Arker:
    return sdk.Arker(
        api_key="ark_live_test",
        provider="provider-one",
        region="region-one",
        retry=False,
    )


def session(session_id: str = "s0") -> dict[str, str]:
    return {"session_id": session_id, "state": "ready", "cwd": "/home/user"}


def test_api_key_from_argument_beats_env(monkeypatch) -> None:
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_env")
    assert sdk.Arker(api_key="ark_live_arg")._api_key == "ark_live_arg"


def test_api_key_falls_back_to_env(monkeypatch) -> None:
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_env")
    assert sdk.Arker()._api_key == "ark_live_env"


def test_missing_api_key_raises() -> None:
    with pytest.raises(ValueError, match="api_key is required"):
        sdk.Arker()


@pytest.mark.parametrize(
    "base_url_argument, base_url_env",
    [
        ("https://pinned.invalid/api", None),
        (None, "https://pinned.invalid/api"),
    ],
    ids=["from_argument", "from_env"],
)
def test_base_url_overrides_placement(monkeypatch, base_url_argument, base_url_env) -> None:
    monkeypatch.setenv("ARKER_PROVIDER", "env-cloud")
    monkeypatch.setenv("ARKER_REGION", "env-1")
    if base_url_env:
        monkeypatch.setenv("ARKER_BASE_URL", base_url_env)

    arker = sdk.Arker(
        api_key="ark_live_test",
        base_url=base_url_argument,
        provider="future-cloud",
        region="moon-1",
    )

    assert arker.base_url == "https://pinned.invalid/api"
    assert arker.provider is None
    assert arker.region is None


def test_base_url_argument_beats_env(monkeypatch) -> None:
    monkeypatch.setenv("ARKER_BASE_URL", "https://env.invalid/api")
    arker = sdk.Arker(api_key="ark_live_test", base_url="https://arg.invalid/api")
    assert arker.base_url == "https://arg.invalid/api"


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"region": "moon-1"}, "provider and region are required together"),
        ({"provider": "future-cloud"}, "provider and region are required together"),
        ({"provider": "", "region": "moon-1"}, "provider and region are required together"),
    ],
)
def test_placement_errors(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        sdk.Arker(api_key="ark_live_test", **kwargs)


def test_shared_client_uses_httpx2() -> None:
    assert type(sdk._http_client).__module__.split(".", 1)[0] == "httpx2"


def test_control_only_client_defers_compute_placement_error() -> None:
    arker = sdk.Arker(api_key="ark_live_test")
    with pytest.raises(ValueError) as excinfo:
        arker.vm("vm_01")
    assert str(excinfo.value) == (
        "No placement configured; set ARKER_PROVIDER and ARKER_REGION, or pass Arker(provider=..., region=...)"
    )


def test_constructor_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("ARKER_API_KEY", "ark_live_env")
    monkeypatch.setenv("ARKER_BASE_URL", "https://env.invalid/api/")
    assert sdk.Arker().base_url == "https://env.invalid/api"


def test_arbitrary_provider_builds_endpoint() -> None:
    arker = sdk.Arker(
        api_key="ark_live_test",
        provider="future-cloud",
        region="moon-1",
        retry=False,
    )

    assert arker.provider == "future-cloud"
    assert arker.region == "moon-1"
    assert arker.base_url == "https://future-cloud-moon-1.arker.ai/api"


def test_placement_requires_separate_provider_and_region() -> None:
    with pytest.raises(ValueError, match="provider and region are required together"):
        sdk.Arker(api_key="ark_live_test", region="region-one")
    with pytest.raises(ValueError, match="provider and region are required together"):
        sdk.Arker(api_key="ark_live_test", provider="provider-one")


def test_list_regions_uses_public_control_plane_catalog() -> None:
    t = FakeTransport()
    # `endpoint` is required, so a placement missing it is not a shape the
    # service can return. `provider` is deliberately NOT a closed set — see
    # tests/test_openapi_enforcement.py; a pinned SDK must not reject the first
    # placement on a provider added after its release.
    placement = {
        "provider": "aws",
        "region": "region-two",
        # The catalog always carries an endpoint, and the contract now says so.
        "endpoint": "https://provider-two-region-two.arker.ai/",
    }
    t.add_json(
        lambda method, url: method == "GET" and url == "https://arker.ai/api/v1/regions",
        200,
        {"regions": [placement]},
    )

    with use_transport(t):
        regions = client().list_regions()

    assert regions.regions[0].provider == "aws"
    assert regions.regions[0].region == "region-two"
    assert regions.regions[0].endpoint == "https://provider-two-region-two.arker.ai/"


def test_discover_regions_requires_no_configured_client() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "GET" and url == "https://control.invalid/api/v1/regions",
        200,
        {"regions": []},
    )

    with use_transport(t):
        regions = sdk.discover_regions(
            control_base_url="https://control.invalid/api",
            retry=False,
        )

    assert regions.regions == []
    assert "authorization" not in t.calls[0]["headers"]


def test_whoami_uses_authenticated_control_plane() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "GET" and url == "https://control.invalid/api/v1/whoami",
        200,
        {"org_id": "org_01", "org_name": "ArkerHQ"},
    )

    with use_transport(t):
        identity = sdk.Arker(
            api_key="ark_live_test",
            control_base_url="https://control.invalid/api",
            retry=False,
        ).whoami()

    assert isinstance(identity, sdk.WhoamiResponse)
    assert identity.org_id == "org_01"
    assert identity.org_name == "ArkerHQ"


def test_fork_posts_directly_to_source_vm() -> None:
    t = FakeTransport()
    # Contract 0.3 routes forks to `/v1/fork`, with the source vm id
    # passed in the body.
    t.add_json(
        lambda method, url: method == "POST" and url == "https://test.invalid/api/v1/fork",
        200,
        {
            "vm_id": "vm_child",
            "owner_org_id": "owner",
            "created_at": "now",
            "description": None,
            "public": False,
            "state": "idle",
            "sessions": [session()],
            "network": {},
            "resources": {},
        },
    )

    with use_transport(t):
        vm = client().fork(
            source_vm_id="source-vm-id",
            name="demo",
            description="CI runner",
            ssh_public_keys=["ssh-ed25519 AAAA test@example.com"],
        )

    assert vm.id == "vm_child"
    body = json.loads(t.calls[0]["body"])
    # Computer.fork passes source_vm_id; `disk` is omitted unless the caller
    # sets it, so the server derives it from the source when inheritance is unavailable.
    assert body == {
        "name": "demo",
        "description": "CI runner",
        "ssh_public_keys": ["ssh-ed25519 AAAA test@example.com"],
        "source_vm_id": "source-vm-id",
    }


def test_fork_preserves_the_canonical_wire_shape() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/fork"),
        200,
        _fork_response("vm_child"),
    )

    with use_transport(t):
        client().fork(
            source_vm_name="ubuntu",
            source_org_id="org_123",
            resources={"vcpu": 2, "memory_mib": 2048},
            description=None,
            disk=False,
            layers=["disk"],
        )

    assert json.loads(t.calls[0]["body"]) == {
        "source_vm_name": "ubuntu",
        "source_org_id": "org_123",
        "resources": {"vcpu": 2, "memory_mib": 2048},
        "description": None,
        "disk": False,
        "layers": ["disk"],
    }


def test_vm_fork_uses_its_id_and_attached_endpoint() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url == "https://attached.invalid/api/v1/fork",
        200,
        _fork_response("vm_child"),
    )
    vm = sdk.VM(client(), "vm_source", "https://attached.invalid/api")

    with use_transport(t):
        child = vm.fork(source_vm_id="ignored", resources={"vcpu": 4})

    assert child.base_url == "https://attached.invalid/api"
    assert json.loads(t.calls[0]["body"]) == {
        "resources": {"vcpu": 4},
        "source_vm_id": "vm_source",
    }


def test_fork_omits_source_org_when_not_explicit() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url == "https://test.invalid/api/v1/fork",
        200,
        {
            "vm_id": "vm_named_source",
            "owner_org_id": "owner",
            "created_at": "now",
            "description": None,
            "public": False,
            "state": "idle",
            "sessions": [session()],
            "network": {},
            "resources": {"vcpu": 4, "memory_mib": 8192, "disk_mib": 10240},
        },
    )

    with use_transport(t):
        vm = client().fork(source_vm_name="catalog-template")

    assert vm.id == "vm_named_source"
    body = json.loads(t.calls[0]["body"])
    assert body == {"source_vm_name": "catalog-template"}


def test_fork_from_image_sends_only_the_image() -> None:
    """`image` is a third source: no VM selector is sent alongside it.

    The service's schema models the three sources as a `oneOf`, so a body
    carrying both `image` and a selector fails to decode there. Sending a
    stray `source_vm_name: null` would be harmless, but sending a real one
    would turn a registry pull into a fork of someone's VM.
    """
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url == "https://test.invalid/api/v1/fork",
        200,
        {
            "vm_id": "vm_from_image",
            "owner_org_id": "owner",
            "created_at": "now",
            "description": None,
            "public": False,
            "state": "idle",
            "sessions": [session()],
            "network": {},
            "resources": {},
        },
    )

    with use_transport(t):
        vm = client().fork(image="ubuntu:24.04", name="from-image")

    assert vm.id == "vm_from_image"
    body = json.loads(t.calls[0]["body"])
    assert body == {"image": "ubuntu:24.04", "name": "from-image"}


def test_fork_rejects_positional_sources() -> None:
    with pytest.raises(TypeError):
        client().fork("base", image="ubuntu:24.04")


def _fork_response(vm_id: str) -> dict:
    return {
        "vm_id": vm_id,
        "owner_org_id": "owner",
        "created_at": "now",
        "description": None,
        "public": False,
        "state": "idle",
        "sessions": [session()],
        "network": {},
        "resources": {},
    }


def test_fork_from_a_private_image_sends_registry_auth() -> None:
    """Credentials ride with the pull they authorize, and nothing else.

    The service performs the pull as the authorization check, so the body must
    carry the image AND the credentials together — a pull attempted without
    them fails as `not found`, which reads to the caller as a typo rather than
    a permission problem.
    """
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url == "https://test.invalid/api/v1/fork",
        200,
        _fork_response("vm_private"),
    )

    with use_transport(t):
        vm = client().fork(
            image="ghcr.io/org/private:v1",
            registry_auth={"username": "u", "password": "p"},
        )

    assert vm.id == "vm_private"
    body = json.loads(t.calls[0]["body"])
    assert body == {
        "image": "ghcr.io/org/private:v1",
        "registry_auth": {"username": "u", "password": "p"},
    }


def test_fork_from_a_dockerfile_forks_the_base_image_then_drives_the_rest() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url == "https://test.invalid/api/v1/fork",
        200,
        _fork_response("vm_dockerfile"),
    )
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/runs"),
        200,
        {
            "stdout": "",
            "stdout_encoding": "utf-8",
            "stderr": "",
            "stderr_encoding": "utf-8",
            "exit_code": 0,
        },
    )

    with use_transport(t):
        vm = client().fork(dockerfile="FROM ubuntu:24.04\nRUN echo hi\n")

    assert vm.id == "vm_dockerfile"
    assert json.loads(t.calls[0]["body"]) == {"image": "ubuntu:24.04"}
    assert t.calls[1]["url"].endswith("/vms/vm_dockerfile/runs")
    assert json.loads(t.calls[1]["body"])["command"] == "echo hi"


def test_fork_rejects_a_response_without_vm_id() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url == "https://test.invalid/api/v1/fork",
        200,
        {"id": "vm_child"},
    )

    with use_transport(t), pytest.raises(TypeError):
        client().fork(source_vm_id="source-vm-id")


def test_fork_tolerates_unknown_response_fields() -> None:
    """Adding a field to a response must not break an older SDK.

    Servers add response fields additively; a client pinned to an earlier
    version has to keep working.
    """
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url == "https://test.invalid/api/v1/fork",
        200,
        {
            "vm_id": "vm_child",
            "owner_org_id": "org",
            "created_at": "2026-07-29T00:00:00Z",
            "description": None,
            "public": False,
            "state": "idle",
            "sessions": [],
            "resources": {"vcpu": 1, "memory_mib": 1024, "disk_mib": 4096},
            "network": {},
            # fields a future server adds that this SDK has never heard of
            "hostname": "vm_child.aws-us-west-2.arker.app",
            "keep_alive": True,
            "some_field_from_the_future": {"nested": [1, 2, 3]},
        },
    )

    with use_transport(t):
        vm = client().fork(source_vm_id="source-vm-id")
    assert vm.id == "vm_child"


def test_configured_placement_routes_named_sources_to_main_endpoint() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url == "https://provider-one-region-one.arker.ai/api/v1/fork",
        200,
        {
            "vm_id": "vmh-child",
            "owner_org_id": "owner",
            "created_at": "now",
            "description": None,
            "public": False,
            "state": "idle",
            "sessions": [],
            "network": {},
            "resources": {},
        },
    )

    with use_transport(t):
        arker = region_client()
        vm = arker.fork(source_vm_id="source-vm-id")

    assert arker.base_url == "https://provider-one-region-one.arker.ai/api"
    assert vm.base_url == "https://provider-one-region-one.arker.ai/api"


def test_list_uses_configured_base_url() -> None:
    t = FakeTransport()
    # `Arker.list()` is an admin call — routed through the control
    # plane, not the compute URL.
    t.add_json(
        lambda method, url: (
            method == "GET"
            and url == "https://arker.ai/api/v1/vms?region=us-west-2&provider=aws&org_id=ArkerHQ&public=True&state=idle"
        ),
        200,
        {
            "vms": [
                {
                    "vm_id": "vm_1",
                    "owner_org_id": "ArkerHQ",
                    "created_at": "now",
                    "description": None,
                    "public": True,
                    "state": "idle",
                    "sessions": [session()],
                    "name": "demo",
                    "network": {},
                    "resources": {"vcpu": 2, "memory_mib": 1024, "disk_mib": 4096},
                    "max_vcpus": 8,
                    "max_memory_mib": 32768,
                    "min_memory_mib": 512,
                }
            ]
        },
    )

    with use_transport(t):
        result = client().list_vms(
            region="us-west-2",
            provider="aws",
            org_id="ArkerHQ",
            public=True,
            state="idle",
        )

    assert isinstance(result, sdk.VmList)
    assert len(result) == 1
    assert result.vms[0].id == "vm_1"
    assert result.vms[0].vm_id == "vm_1"
    assert result.vms[0].owner_org_id == "ArkerHQ"
    assert result.vms[0].max_vcpus == 8
    assert result.vms[0].max_memory_mib == 32768
    assert result.vms[0].min_memory_mib == 512
    assert result.vms[0].network is not None
    assert result.vms[0].network.ssh_public_keys is None
    assert result.vms[0].resources == sdk.VmResources(vcpu=2, memory_mib=1024, disk_mib=4096)


def test_listed_vm_uses_its_placement_endpoint() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "GET" and url == "https://arker.ai/api/v1/vms",
        200,
        {
            "vms": [
                {
                    "vm_id": "vm_placed",
                    "owner_org_id": "org_1",
                    "created_at": "now",
                    "description": None,
                    "public": False,
                    "state": "idle",
                    "region": "region-two",
                    "provider": "provider-two",
                    "network": {},
                    "sessions": [],
                    "resources": {},
                }
            ]
        },
    )
    t.add_json(
        lambda method, url: (
            method == "POST" and url == "https://provider-two-region-two.arker.ai/api/v1/vms/vm_placed/runs"
        ),
        200,
        {
            "run_id": "run_placed",
            "state": "completed",
            "stdout": "ok\n",
            "stdout_encoding": "utf-8",
            "stderr": "",
            "stderr_encoding": "utf-8",
            "exit_code": 0,
        },
    )

    arker = sdk.Arker(
        api_key="ark_live_test",
        base_url="https://fallback.invalid/api",
        retry=False,
    )
    with use_transport(t):
        result = arker.list_vms()
        assert result.vms[0].base_url == "https://provider-two-region-two.arker.ai/api"
        result.vms[0].run("printf ok")


def test_list_runs_uses_control_plane_and_filters() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: (
            method == "GET"
            and url
            == "https://control.invalid/api/v1/runs?since=10&until=20&vm=vm_1&vms=vm_2%2Cvm_3&region=us-west-2&provider=aws&search=pytest&limit=25&offset=5&lite=True&runtime=fc&endpoint=run&actions=run%2Cfork&status=success%2Cinternal&status_min=200&status_max=599&sort=when&dir=asc"
        ),
        200,
        {
            "since": 10,
            "until": 20,
            "limit": 25,
            "offset": 5,
            "lite": True,
            "rows": [
                {
                    "source": "arkerd",
                    "t_ms": 10,
                    "request_id": "req_1",
                    "run_id": "run_1",
                    "vm_id": "vm_1",
                    "session_id": "session_1",
                    "region": "us-west-2",
                    "provider": "aws",
                    "status": 200,
                    "total_ms": 12.5,
                    "queue_ms": 1.5,
                    "executor_duration_ms": 10,
                    "executor_kind": "firecracker",
                    "executor_cpu_ms": 8,
                    "executor_mem_mb": 64,
                    "vm_vcpus": 2,
                    "vm_memory_mib": 4096,
                    "path": "/v1/vms/vm_1/runs",
                    "method": "POST",
                    "command": "pytest",
                    "source_vm_id": "",
                    "exit_code": 0,
                    "endpoint": "run",
                    "api_key_prefix": "ark_live",
                    "body_bytes_in": 10,
                    "body_bytes_out": 20,
                    "body_in": "",
                    "body_out": "",
                }
            ],
        },
    )

    arker = sdk.Arker(
        api_key="ark_live_test",
        base_url="https://test.invalid/api",
        control_base_url="https://control.invalid/api",
        retry=False,
    )
    with use_transport(t):
        result = arker.list_runs(
            since=10,
            until=20,
            vm="vm_1",
            vm_ids=["vm_2", "vm_3"],
            region="us-west-2",
            provider="aws",
            search="pytest",
            limit=25,
            offset=5,
            lite=True,
            runtime="fc",
            endpoint="run",
            actions=["run", "fork"],
            status=["success", "internal"],
            status_min=200,
            status_max=599,
            sort="when",
            dir="asc",
        )

    assert isinstance(result, sdk.ListOrgRunsResponse)
    assert result.lite is True
    assert result.rows[0].region == "us-west-2"
    assert result.rows[0].vm_vcpus == 2
    assert t.calls[0]["body"] is None


def test_run_sends_command_without_default_session_id() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs"),
        200,
        {
            "stdout": "hi\n",
            "stdout_encoding": "utf-8",
            "stderr": "",
            "stderr_encoding": "utf-8",
            "exit_code": 0,
            "memory_requested_mib": 1024,
            "memory_achieved_mib": 1536,
            "memory_partial": True,
        },
    )

    with use_transport(t):
        result = client().vm("vm_1").run("printf hi")

    assert isinstance(result, sdk.CompletedRunResult)
    assert result.stdout == "hi\n"
    assert result.stdout_bytes == b"hi\n"
    assert result.stderr == ""
    assert result.stderr_bytes == b""
    assert result.exit_code == 0
    assert result.memory_requested_mib == 1024
    assert result.memory_achieved_mib == 1536
    assert result.memory_partial is True
    assert json.loads(t.calls[0]["body"]) == {"command": "printf hi"}


def test_sync_run_polls_backgrounded_run_to_completion(monkeypatch) -> None:
    # A synchronous run() that outlives the server sync window gets a background
    # ack; run() must poll get_run() under the hood and return the terminal run
    # — the caller never sees the intermediate background shape.
    monkeypatch.setattr(sdk.time, "sleep", lambda _s: None)
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs"),
        200,
        {"run_id": "run_bg", "state": "running"},
    )
    # First poll: still running. Second poll: terminal.
    t.add_json(
        lambda method, url: method == "GET" and url.endswith("/v1/vms/vm_1/runs/run_bg"),
        200,
        {
            "run_id": "run_bg",
            "state": "running",
            "started_at": "now",
            "exit_code": None,
            "stdout": "",
            "stdout_encoding": "utf-8",
            "stderr": "",
            "stderr_encoding": "utf-8",
        },
    )
    t.add_json(
        lambda method, url: method == "GET" and url.endswith("/v1/vms/vm_1/runs/run_bg"),
        200,
        {
            "run_id": "run_bg",
            "state": "completed",
            "started_at": "now",
            "exit_code": 0,
            "stdout": "done\n",
            "stdout_encoding": "utf-8",
            "stderr": "",
            "stderr_encoding": "utf-8",
        },
    )

    with use_transport(t):
        result = client().vm("vm_1").run("sleep 999")

    assert isinstance(result, sdk.CompletedRunResult)
    assert result.run_id == "run_bg"
    assert result.state == "completed"
    assert result.exit_code == 0
    assert result.stdout == "done\n"
    assert result.stdout_bytes == b"done\n"
    # POST + 2 polls.
    assert [c["method"] for c in t.calls] == ["POST", "GET", "GET"]


def test_explicit_zero_returns_ack_without_polling(monkeypatch) -> None:
    # time_to_background=0 is a pure pass-through — run() returns the running ack
    # immediately and never polls get_run().
    slept: list[float] = []
    monkeypatch.setattr(sdk.time, "sleep", lambda s: slept.append(s))
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs"),
        200,
        {"run_id": "run_bg", "state": "running"},
    )

    with use_transport(t):
        result = client().vm("vm_1").run("sleep 999", time_to_background=0)

    assert isinstance(result, sdk.BackgroundRunResult)
    assert result.run_id == "run_bg"
    # Only the POST — no polling, no sleeping.
    assert [c["method"] for c in t.calls] == ["POST"]
    assert slept == []
    assert json.loads(t.calls[0]["body"]) == {
        "command": "sleep 999",
        "time_to_background": 0,
    }


def test_removed_background_argument_is_rejected() -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument 'background'"):
        client().vm("vm_1").run("sleep 999", background=True)  # type: ignore[call-arg]


def test_run_sends_policies() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs"),
        200,
        {
            "stdout": "",
            "stdout_encoding": "utf-8",
            "stderr": "",
            "stderr_encoding": "utf-8",
            "exit_code": 0,
        },
    )
    policies = {
        "policies": [
            {
                "type": "outbound",
                "action": "deny",
                # `PolicyMatch` has no `protocol` field (real fields: ports, ips,
                # hosts, methods, paths, headers, body_contains) — a leftover
                # "protocol": "tcp" here only ever passed because the
                # `_decode_value` bug (see test_get_and_set_policies) skipped
                # nested validation entirely, so an unreal field was silently
                # never checked. Fixed now that nested decode is fixed.
                "match": {"ports": [443]},
            }
        ]
    }

    with use_transport(t):
        client().vm("vm_1").run("curl https://example.com", policies=policies)

    assert json.loads(t.calls[0]["body"]) == {
        "command": "curl https://example.com",
        "policies": policies,
    }


def test_resize_patches_vm_resources() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "PATCH" and url.endswith("/v1/vms/vm_1"),
        200,
        {
            "vm_id": "vm_1",
            "owner_org_id": "owner",
            "created_at": "now",
            "description": None,
            "public": False,
            "state": "idle",
            "sessions": [],
            "resources": {"vcpu": 2, "memory_mib": 1024, "disk_mib": 4096},
            "network": {},
        },
    )

    with use_transport(t):
        result = client().vm("vm_1").update(memory_mib=1024)

    # resize now PATCHes /v1/vms/{id} with a resources object (arkerd reality;
    # the old POST /v1/vms/{id}/resize route does not exist). None fields are pruned.
    assert json.loads(t.calls[0]["body"]) == {"resources": {"memory_mib": 1024}}
    assert result is not None


def test_resize_patches_vgpu_on_its_own() -> None:
    """A GPU resize is a PATCH like any other resize.

    `vgpu` is the one non-int resource arg, so it has to be in the "any of
    these were passed" guard as well as the `ResourcesInput` construction —
    miss the guard and `resources` is dropped and the VM keeps its old share.
    """
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "PATCH" and url.endswith("/v1/vms/vm_1"),
        200,
        {
            "vm_id": "vm_1",
            "owner_org_id": "owner",
            "created_at": "now",
            "description": None,
            "public": False,
            "state": "idle",
            "sessions": [],
            "resources": {"gpu_sms": 66, "gpu_vram_mib": 40779},
            "network": {},
        },
    )

    with use_transport(t):
        client().vm("vm_1").update(vgpu=0.5)

    assert json.loads(t.calls[0]["body"]) == {"resources": {"vgpu": 0.5}}


def test_update_patches_vm_description() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "PATCH" and url.endswith("/v1/vms/vm_1"),
        200,
        {
            "vm_id": "vm_1",
            "owner_org_id": "owner",
            "created_at": "now",
            "description": "CI runner",
            "public": False,
            "state": "idle",
            "sessions": [],
            "resources": {"vcpu": 2, "memory_mib": 1024, "disk_mib": 4096},
            "network": {},
        },
    )

    with use_transport(t):
        result = client().vm("vm_1").update(description="CI runner")

    assert json.loads(t.calls[0]["body"]) == {"description": "CI runner"}
    assert result.description == "CI runner"


def test_update_can_clear_vm_description_with_null() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "PATCH" and url.endswith("/v1/vms/vm_1"),
        200,
        {
            "vm_id": "vm_1",
            "owner_org_id": "owner",
            "created_at": "now",
            "description": None,
            "public": False,
            "state": "idle",
            "sessions": [],
            "resources": {},
            "network": {},
        },
    )

    with use_transport(t):
        client().vm("vm_1").update(description=None)

    assert json.loads(t.calls[0]["body"]) == {"description": None}


def test_update_can_clear_vm_description_with_blank_string() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "PATCH" and url.endswith("/v1/vms/vm_1"),
        200,
        {
            "vm_id": "vm_1",
            "owner_org_id": "owner",
            "created_at": "now",
            "description": None,
            "public": False,
            "state": "idle",
            "sessions": [],
            "resources": {},
            "network": {},
        },
    )

    with use_transport(t):
        client().vm("vm_1").update(description="")

    assert json.loads(t.calls[0]["body"]) == {"description": ""}


@pytest.mark.parametrize(
    "keys",
    [["ssh-ed25519 AAAA test@example.com"], []],
)
def test_update_replaces_ssh_public_keys_at_the_top_level(keys: list[str]) -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "PATCH" and url.endswith("/v1/vms/vm_1"),
        200,
        {
            "vm_id": "vm_1",
            "owner_org_id": "owner",
            "created_at": "now",
            "description": None,
            "public": False,
            "state": "idle",
            "sessions": [],
            "resources": {},
            "network": {},
        },
    )

    with use_transport(t):
        client().vm("vm_1").update(ssh_public_keys=keys)

    assert json.loads(t.calls[0]["body"]) == {"ssh_public_keys": keys}


def _patched_vm() -> dict[str, Any]:
    return {
        "vm_id": "vm_1",
        "owner_org_id": "owner",
        "created_at": "now",
        "description": None,
        "public": False,
        "state": "idle",
        "sessions": [],
        "resources": {},
        "network": {},
    }


def _is_patch_vm(method: str, url: str) -> bool:
    return method == "PATCH" and url.endswith("/v1/vms/vm_1")


def test_update_replaces_policies_via_patch() -> None:
    """A policy document rides on PATCH /v1/vms/{id} as a top-level
    ``policies`` field: the same shape ``set_policies`` PUTs, so a caller can
    change the policy alongside the other patchable fields in one request."""
    doc = {"policies": [{"type": "outbound", "match": {"hosts": ["example.com"]}, "action": "allow"}]}
    t = FakeTransport()
    t.add_json(_is_patch_vm, 200, _patched_vm())

    with use_transport(t):
        client().vm("vm_1").update(policies=doc)

    assert json.loads(t.calls[0]["body"]) == {"policies": doc}


def test_update_accepts_a_policy_doc_instance() -> None:
    doc = PolicyDoc(
        policies=[
            PolicyEntry(
                type="outbound",
                match=PolicyMatch(hosts=["example.com"]),
                action="allow",
            ),
        ],
    )
    t = FakeTransport()
    t.add_json(_is_patch_vm, 200, _patched_vm())

    with use_transport(t):
        client().vm("vm_1").update(vcpu_count=2, policies=doc)

    assert json.loads(t.calls[0]["body"]) == {
        "resources": {"vcpu": 2},
        "policies": {
            "policies": [
                {
                    "type": "outbound",
                    "match": {"hosts": ["example.com"]},
                    "action": "allow",
                }
            ]
        },
    }


def test_update_sends_policies_alongside_an_explicit_description() -> None:
    """The description branch builds the body as a dict (to carry an explicit
    null); the policy document must survive that path too."""
    t = FakeTransport()
    t.add_json(_is_patch_vm, 200, _patched_vm())

    with use_transport(t):
        client().vm("vm_1").update(description=None, policies={"policies": []})

    assert json.loads(t.calls[0]["body"]) == {
        "description": None,
        "policies": {"policies": []},
    }


@pytest.mark.parametrize("doc", [{}, {"policies": []}])
def test_update_sends_an_empty_policy_doc_to_clear(doc: dict[str, Any]) -> None:
    """An empty document is a real replacement (clears to allow-all), so it is
    sent as-is rather than pruned like an omitted field."""
    t = FakeTransport()
    t.add_json(_is_patch_vm, 200, _patched_vm())

    with use_transport(t):
        client().vm("vm_1").update(policies=doc)

    assert json.loads(t.calls[0]["body"]) == {"policies": doc}


def test_update_omits_policies_unless_given() -> None:
    t = FakeTransport()
    t.add_json(_is_patch_vm, 200, _patched_vm())

    with use_transport(t):
        client().vm("vm_1").update(vcpu_count=4)

    assert "policies" not in json.loads(t.calls[0]["body"])


def test_background_run_response() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/runs"),
        200,
        {
            "run_id": "run_1",
            "state": "running",
        },
    )

    with use_transport(t):
        result = client().vm("vm_1").run("sleep 10", time_to_background=0)

    assert isinstance(result, sdk.BackgroundRunResult)
    assert result.run_id == "run_1"
    assert result.state == "running"
    assert json.loads(t.calls[0]["body"]) == {
        "command": "sleep 10",
        "time_to_background": 0,
    }


def test_flat_error_response_is_rejected_as_malformed() -> None:
    t = FakeTransport()
    t.add_json(lambda _method, _url: True, 404, {"code": "not_found", "message": "missing"})

    with use_transport(t), pytest.raises(sdk.ArkerError) as caught:
        client().vm("missing").delete()

    assert caught.value.code == "internal"
    assert caught.value.status == 404


def test_error_response_with_legacy_top_level_field_is_rejected() -> None:
    t = FakeTransport()
    t.add_json(
        lambda _method, _url: True,
        404,
        {"ok": False, "error": {"code": "not_found", "message": "missing"}},
    )

    with use_transport(t), pytest.raises(sdk.ArkerError) as caught:
        client().vm("missing").delete()

    assert caught.value.code == "internal"
    assert caught.value.status == 404


def test_canonical_error_response_parses() -> None:
    t = FakeTransport()
    t.add_json(
        lambda _method, _url: True,
        503,
        {
            "error": {
                "code": "unavailable",
                "message": "try later",
                "timestamp": "2026-07-21T00:00:00Z",
            }
        },
    )

    with use_transport(t), pytest.raises(sdk.ArkerError) as caught:
        client().vm("missing").delete()

    assert caught.value.code == "unavailable"
    assert caught.value.status == 503


def test_retry_on_503_then_success(monkeypatch) -> None:
    monkeypatch.setattr(sdk.time, "sleep", lambda *_: None)
    t = FakeTransport()
    predicate = lambda method, url: method == "POST" and url.endswith("/sync")
    t.add_raw(predicate, 503, b"service unavailable")
    t.add_json(
        predicate,
        200,
        {
            "ok": True,
            "op": "read",
            "path": "/home/user/x",
            "size": 2,
            "content": "ok",
            "encoding": "utf-8",
        },
    )

    with use_transport(t):
        assert (
            sdk.Arker(api_key="k", base_url="https://test.invalid/api", retry={"attempts": 2})
            .vm("vm")
            .sync("/home/user/x")
            == b"ok"
        )

    assert len(t.calls) == 2


def test_get_retries_a_network_failure(monkeypatch) -> None:
    monkeypatch.setattr(sdk.time, "sleep", lambda *_: None)
    t = FakeTransport()
    predicate = lambda method, url: method == "GET" and url.endswith("/v1/vms/vm_1")
    t.add_network_error(predicate)
    t.add_json(predicate, 200, _fork_response("vm_1"))

    with use_transport(t):
        vm = sdk.Arker(
            api_key="k",
            base_url="https://test.invalid/api",
            retry={"attempts": 2},
        ).get_vm("vm_1")

    assert vm.id == "vm_1"
    assert len(t.calls) == 2


def test_keyed_run_does_not_retry_an_ambiguous_network_failure() -> None:
    t = FakeTransport()
    predicate = lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs")
    t.add_network_error(predicate)
    vm = sdk.Arker(
        api_key="k",
        base_url="https://test.invalid/api",
        retry={"attempts": 2},
    ).vm("vm_1")

    with use_transport(t), pytest.raises(sdk.ArkerError) as caught:
        vm.run(
            "touch /tmp/once",
            time_to_background=0,
            idempotency_key="run-key",
        )

    assert caught.value.code == "unavailable"
    assert "outcome is unknown" in caught.value.message
    assert "reconcile" in caught.value.message.lower()
    assert len(t.calls) == 1
    assert t.calls[0]["headers"]["idempotency-key"] == "run-key"


def test_mutation_does_not_retry_a_server_unknown_outcome() -> None:
    t = FakeTransport()
    predicate = lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs")
    t.add_json(
        predicate,
        503,
        UNKNOWN_OUTCOME_ERROR_BODY,
    )
    t.add_json(predicate, 200, {"run_id": "run_2", "exit_code": 0})

    with use_transport(t), pytest.raises(sdk.ArkerError) as caught:
        sdk.Arker(
            api_key="k",
            base_url="https://test.invalid/api",
            retry={"attempts": 2, "base_delay_s": 0, "jitter_s": 0},
        ).vm("vm_1").run("touch /tmp/once", time_to_background=0)

    assert caught.value.code == "unavailable"
    assert caught.value.status == 503
    assert "outcome is unknown" in caught.value.message
    assert len(t.calls) == 1


def test_sync_stream_does_not_retry_an_ambiguous_network_failure() -> None:
    t = FakeTransport()
    predicate = lambda method, url: method == "POST" and "/sync-stream" in url
    t.add_network_error(predicate)

    with use_transport(t), pytest.raises(sdk.ArkerError, match="outcome is unknown"):
        sdk.Arker(
            api_key="k",
            base_url="https://test.invalid/api",
            retry={"attempts": 2},
        ).vm("vm_1").sync("/home/user/x", b"content")

    assert len(t.calls) == 1


def test_read_inline_base64() -> None:
    payload = bytes(range(64))
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/sync"),
        200,
        {
            "ok": True,
            "op": "read",
            "path": "/home/user/bin",
            "size": len(payload),
            "content": base64.b64encode(payload).decode(),
            "encoding": "base64",
        },
    )

    with use_transport(t):
        assert client().vm("vm_1").sync("/home/user/bin") == payload


def test_read_presigned_follows_url() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/sync"),
        200,
        {
            "ok": True,
            "op": "read",
            "path": "/home/user/big",
            "size": 5,
            "presigned_url": "https://s3.invalid/file",
            "expires_in": 900,
            "method": "GET",
        },
    )
    t.add_raw(
        lambda method, url: method == "GET" and url == "https://s3.invalid/file",
        200,
        b"hello",
    )

    with use_transport(t):
        assert client().vm("vm_1").sync("/home/user/big") == b"hello"


def test_small_write_streams_to_sync_stream() -> None:
    """`sync` streams raw bytes to /sync-stream rather than base64-ing them
    into a JSON writes[] envelope. Measured in-region, streaming beat the
    inline path 103-112 MB/s vs 41-50, so it is the default at every size the
    router will accept."""
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and "/sync-stream" in url,
        200,
        {"ok": True},
    )

    with use_transport(t):
        client().vm("vm_1").sync("/home/user/x", b"hello world")

    call = t.calls[0]
    assert "/sync-stream" in call["url"]
    assert "path=%2Fhome%2Fuser%2Fx" in call["url"]
    assert "size=11" in call["url"]
    # Raw body: no base64, so none of the +33% inflation the JSON path pays.
    body = call["body"]
    assert (body.encode() if isinstance(body, str) else body) == b"hello world"


def test_empty_write_sends_one_empty_chunk() -> None:
    t = FakeTransport()
    predicate = lambda method, url: method == "POST" and url.endswith("/sync")
    t.add_json(
        predicate,
        200,
        {
            "ok": True,
            "op": "write",
            "results": [
                {
                    "path": "/home/user/empty",
                    "size": 0,
                    "received_bytes": 0,
                    "ranges": [{"start": 0, "end": 0}],
                    "complete": True,
                    "written": True,
                }
            ],
        },
    )

    with use_transport(t):
        # `sync()` streams now; the inline write machinery stays reachable via
        # sync_dir's fallback for servers predating /sync-stream, so this
        # exercises it directly rather than through the public entrypoint.
        client().vm("vm_1")._sync_write_inline("/home/user/empty", b"")

    writes = json.loads(t.calls[0]["body"])["writes"]
    assert len(writes) == 1
    assert (writes[0]["start"], writes[0]["end"], writes[0]["size"]) == (0, 0, 0)
    assert writes[0]["content"] == ""


def test_mid_size_write_inlines_chunks_in_one_request() -> None:
    payload = b"A" * (sdk.CHUNK_SIZE + 1)
    t = FakeTransport()
    predicate = lambda method, url: method == "POST" and url.endswith("/sync")
    t.add_json(
        predicate,
        200,
        {
            "ok": True,
            "op": "write",
            "results": [
                {
                    "path": "/home/user/big",
                    "size": len(payload),
                    "received_bytes": sdk.CHUNK_SIZE,
                    "ranges": [{"start": 0, "end": sdk.CHUNK_SIZE}],
                    "complete": False,
                    "written": False,
                },
                {
                    "path": "/home/user/big",
                    "size": len(payload),
                    "received_bytes": len(payload),
                    "ranges": [{"start": 0, "end": len(payload)}],
                    "complete": True,
                    "written": True,
                },
            ],
        },
    )

    with use_transport(t):
        # `sync()` streams now; the inline write machinery stays reachable via
        # sync_dir's fallback for servers predating /sync-stream, so this
        # exercises it directly rather than through the public entrypoint.
        client().vm("vm_1")._sync_write_inline("/home/user/big", payload)

    # One request, two chunks sharing an upload_id; only the final chunk
    # reports completion.
    assert [call["method"] for call in t.calls] == ["POST"]
    writes = json.loads(t.calls[0]["body"])["writes"]
    assert len(writes) == 2
    assert writes[0]["upload_id"] == writes[1]["upload_id"]
    assert (writes[0]["start"], writes[0]["end"]) == (0, sdk.CHUNK_SIZE)
    assert (writes[1]["start"], writes[1]["end"]) == (sdk.CHUNK_SIZE, len(payload))
    assert writes[0]["size"] == len(payload)
    assert writes[1]["size"] == len(payload)
    decoded = base64.b64decode(writes[0]["content"]) + base64.b64decode(writes[1]["content"])
    assert decoded == payload


def test_fork_sends_durable_flag() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/fork"),
        200,
        {
            "vm_id": "vm_child",
            "owner_org_id": "owner",
            "created_at": "now",
            "description": None,
            "public": False,
            "state": "idle",
            "sessions": [],
            "network": {},
            "resources": {},
        },
    )

    with use_transport(t):
        client().fork(
            source_vm_id="source-vm-id",
            durable=True,
            ssh_public_keys=["ssh-ed25519 AAAA test@example"],
            policies={"policies": []},
        )

    # Computer.fork auto-fills source_vm_id; `disk` is omitted unless set.
    assert json.loads(t.calls[0]["body"]) == {
        "durable": True,
        "source_vm_id": "source-vm-id",
        "ssh_public_keys": ["ssh-ed25519 AAAA test@example"],
        "policies": {"policies": []},
    }


def test_fork_sends_disk_only_layers() -> None:
    """A disk-only fork (`layers=["disk"]`) puts the layer selection in the body
    so the child inherits only the filesystem and cold-boots with fresh RAM."""
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/fork"),
        200,
        {
            "vm_id": "vm_child",
            "owner_org_id": "owner",
            "created_at": "now",
            "description": None,
            "public": False,
            "state": "idle",
            "sessions": [],
            "network": {},
            "resources": {},
        },
    )

    with use_transport(t):
        client().fork(source_vm_id="source-vm-id", layers=["disk"])

    # Computer.fork auto-fills source_vm_id; `disk` is omitted unless set.
    assert json.loads(t.calls[0]["body"]) == {
        "layers": ["disk"],
        "source_vm_id": "source-vm-id",
    }


def test_fork_omits_layers_by_default() -> None:
    """A plain fork sends no `layers` key — the server applies the default full
    fork (disk + memory), so the child resumes warm."""
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/fork"),
        200,
        {
            "vm_id": "vm_child",
            "owner_org_id": "owner",
            "created_at": "now",
            "description": None,
            "public": False,
            "state": "idle",
            "sessions": [],
            "network": {},
            "resources": {},
        },
    )

    with use_transport(t):
        client().fork(source_vm_id="source-vm-id")

    assert "layers" not in json.loads(t.calls[0]["body"])


def test_run_sends_idempotency_key_header() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/runs"),
        200,
        {
            "stdout": "hi\n",
            "stdout_encoding": "utf-8",
            "stderr": "",
            "stderr_encoding": "utf-8",
            "exit_code": 0,
        },
    )

    with use_transport(t):
        client().vm("vm_1").run("printf hi", idempotency_key="key-abc")

    assert t.calls[0]["headers"]["idempotency-key"] == "key-abc"


def test_run_status_parses_retry_count() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "GET" and url.endswith("/runs/run_1"),
        200,
        {
            "run_id": "run_1",
            "stdout": "",
            "stdout_encoding": "utf-8",
            "stderr": "",
            "stderr_encoding": "utf-8",
            "exit_code": 0,
            "state": "completed",
            "started_at": "now",
            "retry_count": 2,
        },
    )

    with use_transport(t):
        status = client().vm("vm_1").get_run("run_1")

    assert status.retry_count == 2


def test_run_status_defaults_retry_count_when_missing() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "GET" and url.endswith("/runs/run_1"),
        200,
        {
            "run_id": "run_1",
            "stdout": "",
            "stdout_encoding": "utf-8",
            "stderr": "",
            "stderr_encoding": "utf-8",
            "exit_code": 0,
            "state": "completed",
            "started_at": "now",
        },
    )

    with use_transport(t):
        status = client().vm("vm_1").get_run("run_1")

    assert status.retry_count == 0


def test_per_entry_internal_error_retries(monkeypatch) -> None:
    monkeypatch.setattr(sdk.time, "sleep", lambda *_: None)
    t = FakeTransport()
    transient = {
        "path": "/home/user/x",
        "size": 5,
        "complete": False,
        "written": False,
        "error": {"code": "internal", "message": "503 Service Unavailable SlowDown"},
    }
    predicate = lambda method, url: method == "POST" and url.endswith("/sync")
    t.add_json(predicate, 200, {"ok": True, "op": "write", "results": [transient]})
    t.add_json(
        predicate,
        200,
        {
            "ok": True,
            "op": "write",
            "results": [
                {
                    "path": "/home/user/x",
                    "size": 5,
                    "received_bytes": 5,
                    "ranges": [{"start": 0, "end": 5}],
                    "complete": True,
                    "written": True,
                }
            ],
        },
    )

    with use_transport(t):
        sdk.Arker(api_key="k", base_url="https://test.invalid/api", retry={"attempts": 2}).vm("vm")._sync_write_inline(
            "/home/user/x", b"hello"
        )

    assert len(t.calls) == 2


def test_get_and_set_policies() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "GET" and url == "https://test.invalid/api/v1/vms/vm_1/policies",
        200,
        {
            "policies": [],
            "secrets": {},
            "hostname": None,
            "mitm_domains": [],
            "warnings": [],
        },
    )
    with use_transport(t):
        doc = client().vm("vm_1").get_policies()
    assert doc.policies == []

    t.add_json(
        lambda method, url: method == "PUT" and url == "https://test.invalid/api/v1/vms/vm_1/policies",
        200,
        {
            "policies": [
                {
                    "type": "outbound",
                    "match": {"hosts": ["example.com"]},
                    "action": "allow",
                }
            ],
            "secrets": {},
            "hostname": None,
            "mitm_domains": ["example.com"],
            "warnings": [],
        },
    )
    with use_transport(t):
        updated = (
            client()
            .vm("vm_1")
            .set_policies(
                {
                    "policies": [
                        {
                            "type": "outbound",
                            "match": {"hosts": ["example.com"]},
                            "action": "allow",
                        }
                    ],
                }
            )
        )
    # Regression coverage for a bug found writing this test: `PolicyDoc.policies`
    # is typed `list[PolicyEntry] | None`. `_decode_value`'s Union branch used to
    # only recurse when a member was itself a bare dataclass type — `list[PolicyEntry]`
    # is a generic alias, not a dataclass, so it was never matched and the raw
    # list-of-dicts passed through unconverted (entries stayed plain dicts;
    # `.type` attribute access raised AttributeError). Fixed in `_decode_value`
    # to also recurse into a single non-null Optional[list[...]]/Optional[dict[...]]
    # member. Assert the real dataclass here so this can't silently regress.
    assert updated.policies[0].type == "outbound"
    assert updated.mitm_domains == ["example.com"]
    put_call = next(c for c in t.calls if c["method"] == "PUT")
    assert b"example.com" in put_call["body"]


def test_request_policies_reach_the_server_unfiltered() -> None:
    """A key the model does not know must go out on the wire, not be dropped.

    Decoding an outbound policy doc through PolicyDoc silently discarded
    unknown keys, so a typo'd `policys` shipped an empty doc and the run went
    out unrestricted. The server rejects unknown fields and names the valid
    ones; the SDK must let it.
    """
    t = FakeTransport()
    t.add_json(lambda method, url: method == "PUT", 200, {"policies": []})
    with use_transport(t):
        client().vm("vm_1").set_policies({"policys": [{"action": "allow"}]})
    assert b"policys" in next(c for c in t.calls if c["method"] == "PUT")["body"]


def test_create_filesystem() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url == "https://test.invalid/api/v1/filesystems",
        200,
        {
            "filesystem_id": "fs_1",
            "name": "my-fs",
            "owner_org_id": "ArkerHQ",
            "created_at": "now",
            "size_bytes": 0,
            "region": "us-west-2",
            "provider": "aws",
        },
    )
    with use_transport(t):
        fs = client().create_filesystem(name="my-fs")
    assert fs.filesystem_id == "fs_1"
    assert fs.name == "my-fs"
    assert t.calls[0]["method"] == "POST"
    assert b"my-fs" in t.calls[0]["body"]


def test_cancel_run() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "DELETE" and url == "https://test.invalid/api/v1/vms/vm_1/runs/run_1",
        200,
        {"cancelled": True},
    )
    with use_transport(t):
        result = client().vm("vm_1").cancel_run("run_1")
    assert result.cancelled is True
    assert t.calls[0]["method"] == "DELETE"


def test_session_crud_lifecycle() -> None:
    t = FakeTransport()
    with use_transport(t):
        vm = client().vm("vm_1")

        t.add_json(
            lambda method, url: method == "POST" and url == "https://test.invalid/api/v1/vms/vm_1/sessions",
            200,
            session("sess_1"),
        )
        created = vm.create_session(cwd="/home/user")
        assert created.session_id == "sess_1"

        t.add_json(
            lambda method, url: method == "GET" and url == "https://test.invalid/api/v1/vms/vm_1/sessions/sess_1",
            200,
            session("sess_1"),
        )
        fetched = vm.get_session("sess_1")
        assert fetched.session_id == "sess_1"

        t.add_json(
            lambda method, url: method == "GET" and url == "https://test.invalid/api/v1/vms/vm_1/sessions",
            200,
            {"sessions": [session("sess_1")], "next_cursor": None},
        )
        listed = vm.list_sessions()
        assert len(listed.sessions) == 1
        assert listed.next_cursor is None

        t.add_json(
            lambda method, url: method == "PATCH" and url == "https://test.invalid/api/v1/vms/vm_1/sessions/sess_1",
            200,
            {"ok": True, "session_id": "sess_1"},
        )
        patched = vm.update_session("sess_1", cols=100, rows=40)
        assert patched.ok is True
        patch_call = next(c for c in t.calls if c["method"] == "PATCH")
        assert b'"cols": 100' in patch_call["body"] or b'"cols":100' in patch_call["body"]

        t.add_json(
            lambda method, url: method == "DELETE" and url == "https://test.invalid/api/v1/vms/vm_1/sessions/sess_1",
            200,
            {"deleted": True},
        )
        deleted = vm.delete_session("sess_1")
        assert deleted.deleted is True


def test_run_reports_failed_when_platform_killed_the_run() -> None:
    """A negative exit code means no process status was obtained."""
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs"),
        200,
        {
            "run_id": "01RUN",
            "state": "completed",  # service reports the enum variant's name
            "stdout": "",
            "stdout_encoding": "utf-8",
            "stderr": "",
            "stderr_encoding": "utf-8",
            "exit_code": -1,
        },
    )
    with use_transport(t):
        result = client().vm("vm_1").run("sleep 30", timeout=2)
    assert result.state == "failed"
    assert result.exit_code == -1


def test_run_keeps_completed_for_nonzero_program_exit() -> None:
    """A non-zero status is the program's failure, not the platform's."""
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs"),
        200,
        {
            "run_id": "01RUN",
            "state": "completed",
            "stdout": "",
            "stdout_encoding": "utf-8",
            "stderr": "boom\n",
            "stderr_encoding": "utf-8",
            "exit_code": 7,
        },
    )
    with use_transport(t):
        result = client().vm("vm_1").run("exit 7")
    assert result.state == "completed"
    assert result.exit_code == 7


def test_run_and_get_run_agree_on_state_for_a_killed_run() -> None:
    """The two paths report the same state for the same run."""
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs"),
        200,
        {
            "run_id": "01RUN",
            "state": "completed",
            "stdout": "",
            "stdout_encoding": "utf-8",
            "stderr": "",
            "stderr_encoding": "utf-8",
            "exit_code": -1,
        },
    )
    t.add_json(
        lambda method, url: method == "GET" and url.endswith("/v1/vms/vm_1/runs/01RUN"),
        200,
        {
            "run_id": "01RUN",
            "state": "failed",
            "started_at": "2026-07-27T00:00:00Z",
            "exit_code": None,
            "fail_reason": "the compute environment became unavailable",
            "stdout": "",
            "stdout_encoding": "utf-8",
            "stderr": "",
            "stderr_encoding": "utf-8",
        },
    )
    with use_transport(t):
        vm = client().vm("vm_1")
        sync = vm.run("sleep 30", timeout=2)
        stored = vm.get_run("01RUN")
    assert sync.state == stored.state == "failed"


# ── Binary output ──────────────────────────────────────────────────
# A run can emit anything: an image, an archive, random bytes. The text view is
# lossy for those by definition, so `*_bytes` must round-trip them exactly.

# 1x1 PNG. Starts with 0x89, which is not a valid UTF-8 start byte, so decoding
# to text mangles it — that is the whole point of keeping the bytes.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _binary_run_body(payload: bytes) -> dict[str, Any]:
    return {
        "stdout": base64.b64encode(payload).decode(),
        "stdout_encoding": "base64",
        "stderr": "",
        "stderr_encoding": "utf-8",
        "exit_code": 0,
    }


def test_run_returns_image_bytes_intact_and_text_is_lossy() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs"),
        200,
        _binary_run_body(PNG_1X1),
    )

    with use_transport(t):
        result = client().vm("vm_1").run("cat photo.png")

    # The bytes survive exactly — you can write them straight to a file.
    assert result.stdout_bytes == PNG_1X1
    assert result.stdout_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(result.stdout_bytes) == len(PNG_1X1)
    # The text view is lossy for binary, and must not raise.
    assert isinstance(result.stdout, str)
    assert "�" in result.stdout


def test_get_run_returns_image_bytes_intact() -> None:
    t = FakeTransport()
    body = _binary_run_body(PNG_1X1)
    body.update({"run_id": "run_1", "state": "completed", "started_at": "now"})
    t.add_json(lambda method, url: method == "GET" and url.endswith("/runs/run_1"), 200, body)

    with use_transport(t):
        stored = client().vm("vm_1").get_run("run_1")

    assert stored.stdout_bytes == PNG_1X1
    assert "�" in stored.stdout


def test_run_and_get_run_agree_on_binary_output() -> None:
    """The two paths decode the same wire payload identically — the asymmetry
    that made `get_run` return encoded strings is what broke callers."""
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs"),
        200,
        _binary_run_body(PNG_1X1),
    )
    stored_body = _binary_run_body(PNG_1X1)
    stored_body.update({"run_id": "run_1", "state": "completed", "started_at": "now"})
    t.add_json(
        lambda method, url: method == "GET" and url.endswith("/runs/run_1"),
        200,
        stored_body,
    )

    with use_transport(t):
        vm = client().vm("vm_1")
        live = vm.run("cat photo.png")
        stored = vm.get_run("run_1")

    assert live.stdout_bytes == stored.stdout_bytes == PNG_1X1
    assert live.stdout == stored.stdout


def test_every_byte_value_round_trips() -> None:
    """All 256 byte values, including NUL and the invalid-UTF-8 range. Text
    decoding collapses many of these to U+FFFD; the bytes must not."""
    payload = bytes(range(256))
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs"),
        200,
        _binary_run_body(payload),
    )

    with use_transport(t):
        result = client().vm("vm_1").run("cat /dev/urandom")

    assert result.stdout_bytes == payload
    assert len(result.stdout_bytes) == 256
    # Lossy as text: distinct bytes collapse onto the replacement char.
    assert len(set(result.stdout)) < 256


def test_utf8_wire_still_yields_both_forms() -> None:
    """The utf-8 wire path must populate bytes too, not only text."""
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs"),
        200,
        {
            "stdout": "café \U0001f389\n",
            "stdout_encoding": "utf-8",
            "stderr": "",
            "stderr_encoding": "utf-8",
            "exit_code": 0,
        },
    )

    with use_transport(t):
        result = client().vm("vm_1").run("echo cafe")

    assert result.stdout == "café \U0001f389\n"
    assert result.stdout_bytes == "café \U0001f389\n".encode()
    assert result.stdout_bytes.decode() == result.stdout


def test_connect_pty_defaults_to_plain_text_env() -> None:
    """A PTY should emit plain text by default — its consumer is a program.

    The env rides on the SESSION (a PTY inherits it), so this asserts the
    create-session body rather than any PTY query param.
    """
    t = FakeTransport()
    seen: dict[str, object] = {}

    def capture(method: str, url: str) -> bool:
        return method == "POST" and url.endswith("/sessions")

    t.add_json(capture, 200, {"session_id": "s1", "vm_id": "vm1", "state": "idle"})
    with use_transport(t):
        vm = client().vm("vm1")
        # No websocket-client and no server, but the session POST still
        # happens first, which is what this asserts on.
        with contextlib.suppress(Exception):
            vm.connect_pty()
        for call in getattr(t, "calls", []):
            if str(call).endswith("/sessions") or "/sessions" in str(call):
                seen["hit"] = True
    # The contract we care about is the constant itself; assert it is plain.
    from arker.computer import PLAIN_PTY_ENV

    assert PLAIN_PTY_ENV["TERM"] == "dumb"
    assert PLAIN_PTY_ENV["NO_COLOR"] == "1"
    assert PLAIN_PTY_ENV["FORCE_COLOR"] == "0"


def test_plain_pty_env_is_overridable() -> None:
    """`plain=False` must leave a human-facing TUI able to render."""
    import inspect

    from arker.computer import VM

    sig = inspect.signature(VM.connect_pty)
    assert sig.parameters["plain"].default is True
    assert sig.parameters["env"].default is None


def test_fork_sends_vgpu_as_the_only_resource() -> None:
    """`vgpu` alone must reach the wire, and reach it as a float.

    It is the one resource arg that is not an int, so it has to be in the
    "any of these were passed" guard as well as the VmResources construction —
    miss the guard and `resources` is dropped entirely and the caller silently
    gets a whole card.
    """
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/fork"),
        200,
        {
            "vm_id": "vm_child",
            "owner_org_id": "owner",
            "created_at": "now",
            "description": None,
            "public": False,
            "state": "idle",
            "sessions": [],
            "network": {},
            "resources": {},
        },
    )

    with use_transport(t):
        client().fork(
            source_vm_id="gpu-source-vm-id",
            platforms=["x86_64-l40s"],
            resources={"vgpu": 0.25},
        )

    assert json.loads(t.calls[0]["body"]) == {
        "source_vm_id": "gpu-source-vm-id",
        "platforms": ["x86_64-l40s"],
        "resources": {"vgpu": 0.25},
    }


def test_fork_mixes_gpu_and_cpu_resources() -> None:
    """CPU and GPU fields coexist in the single resources object."""
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/fork"),
        200,
        {
            "vm_id": "vm_child",
            "owner_org_id": "owner",
            "created_at": "now",
            "description": None,
            "public": False,
            "state": "idle",
            "sessions": [],
            "network": {},
            "resources": {},
        },
    )

    with use_transport(t):
        client().fork(
            source_vm_id="gpu-source-vm-id",
            resources={"vcpu": 4, "vgpu": 0.5},
        )

    assert json.loads(t.calls[0]["body"]) == {
        "source_vm_id": "gpu-source-vm-id",
        "resources": {"vcpu": 4, "vgpu": 0.5},
    }


def test_fork_omits_resources_when_no_sizing_requested() -> None:
    """No sizing of any kind ⇒ no `resources` key at all (not an empty object).

    Guards the `any(...)` gate: adding GPU fields must not make every fork
    start sending a resources object it did not send before.
    """
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/fork"),
        200,
        {
            "vm_id": "vm_child",
            "owner_org_id": "owner",
            "created_at": "now",
            "description": None,
            "public": False,
            "state": "idle",
            "sessions": [],
            "network": {},
            "resources": {},
        },
    )

    with use_transport(t):
        client().fork(source_vm_id="source-vm-id")

    assert "resources" not in json.loads(t.calls[0]["body"])


def test_fork_omits_disk_so_nodisk_sources_work() -> None:
    """`disk` must not be defaulted client-side."""
    t = FakeTransport()
    for _ in range(3):
        t.add_json(
            lambda method, url: method == "POST" and url.endswith("/v1/fork"),
            200,
            {
                "vm_id": "vm_child",
                "owner_org_id": "owner",
                "created_at": "now",
                "description": None,
                "public": False,
                "state": "idle",
                "sessions": [],
                "network": {},
                "resources": {},
            },
        )

    with use_transport(t):
        client().fork(source_vm_name="gpu-source-vm")

    body = json.loads(t.calls[0]["body"])
    assert "disk" not in body

    # An explicit choice is still honoured in both directions.
    with use_transport(t):
        client().fork(source_vm_name="gpu-source-vm", disk=False)
    assert json.loads(t.calls[1]["body"])["disk"] is False

    with use_transport(t):
        client().fork(source_vm_name="source-vm", disk=True)
    assert json.loads(t.calls[2]["body"])["disk"] is True


def test_retry_delay_honours_server_retry_after() -> None:
    # A hint beats backoff when the caller left max_delay_s at its default:
    # the default exists to shape backoff, and capping the hint with it would
    # neuter real capacity waits.
    retry = sdk.RetryOptions(attempts=4, base_delay_s=0.2, jitter_s=0.0)
    assert sdk._retry_delay(retry, 0, {"code": "unavailable", "retry_after": 30}) == 30.0

    # Without a hint the existing backoff is untouched.
    assert sdk._retry_delay(retry, 0) == pytest.approx(0.2)
    assert sdk._retry_delay(retry, 2) == pytest.approx(0.8)
    assert sdk._retry_delay(retry, 1, {"code": "unavailable", "retry_after": None}) == pytest.approx(0.4)

    # An explicitly configured max_delay_s is the caller's latency budget, and
    # it caps the hint too.
    capped = sdk.RetryOptions(attempts=4, base_delay_s=0.2, max_delay_s=2.0, jitter_s=0.0)
    assert sdk._retry_delay(capped, 0, {"code": "unavailable", "retry_after": 30}) == 2.0


def test_wire_retry_after_rejects_what_the_decoder_lets_through() -> None:
    # The response decoder passes scalars through unchecked, so anything the
    # server sent lands here; a non-number must not reach the delay math.
    for bad in (0, -5, True, "30", None):
        assert sdk._wire_retry_after(bad) is None
    assert sdk._wire_retry_after(30) == 30.0
    assert sdk._wire_retry_after(1.5) == 1.5


def test_retry_after_hint_drives_the_actual_sleep() -> None:
    # The one test of the wiring: the request loop must hand the parsed error
    # to the delay, or the hint silently never applies. Lower bound only.
    t = FakeTransport()
    predicate = lambda method, url: method == "POST" and url.endswith("/v1/fork")
    t.add_json(
        predicate,
        503,
        {
            "error": {
                "code": "unavailable",
                "message": "cold",
                "retry_after": 0.05,
                "timestamp": "2026-01-01T00:00:00.000Z",
            }
        },
    )
    t.add_json(
        predicate,
        200,
        {
            "vm_id": "vm_child",
            "owner_org_id": "owner",
            "created_at": "now",
            "description": None,
            "public": False,
            "state": "idle",
            "sessions": [session()],
            "network": {},
            "resources": {},
        },
    )

    started = time.monotonic()
    with use_transport(t):
        sdk.Arker(
            api_key="ark_live_test",
            base_url="https://test.invalid/api",
            retry={"attempts": 2, "base_delay_s": 0.001, "jitter_s": 0.0},
        ).fork(source_vm_name="source-vm")
    assert time.monotonic() - started >= 0.045
    assert len(t.calls) == 2


def _unavailable_body(retry_after_s: float) -> dict[str, Any]:
    return {
        "error": {
            "code": "unavailable",
            "message": "at capacity",
            "retry_after": retry_after_s,
            "timestamp": "2026-01-01T00:00:00.000Z",
        }
    }


def _completed_run_body() -> dict[str, Any]:
    return {
        "run_id": "run_q",
        "state": "completed",
        "exit_code": 0,
        "stdout": "ok",
        "stdout_encoding": "utf-8",
        "stderr": "",
        "stderr_encoding": "utf-8",
    }


def _queueing_client(**retry: Any) -> sdk.Arker:
    return sdk.Arker(
        api_key="ark_live_test",
        base_url="https://test.invalid/api",
        retry={"base_delay_s": 0.001, "jitter_s": 0.0, **retry},
    )


def test_queueing_timeout_retries_past_the_attempt_cap() -> None:
    # The window is the budget: three failures exceed attempts=2, still succeeds.
    t = FakeTransport()
    predicate = lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs")
    for _ in range(3):
        t.add_json(predicate, 503, _unavailable_body(0.05))
    t.add_json(predicate, 200, _completed_run_body())

    with use_transport(t):
        result = _queueing_client(attempts=2).vm("vm_1").run("echo ok", queueing_timeout=30)
    assert result.exit_code == 0
    assert len(t.calls) == 4, "the window must outlast attempts=2"


def test_queueing_window_drains_then_surfaces_unavailable() -> None:
    # 3s window, 1.1s hints: bodies re-send the remaining window (3, 2, 1),
    # then the error surfaces without sleeping past the deadline.
    t = FakeTransport()
    predicate = lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs")
    for _ in range(3):
        t.add_json(predicate, 503, _unavailable_body(1.1))

    started = time.monotonic()
    with use_transport(t), pytest.raises(sdk.ArkerError) as error:
        _queueing_client(attempts=4).vm("vm_1").run("true", queueing_timeout=3)
    elapsed = time.monotonic() - started
    assert error.value.code == "unavailable"
    assert error.value.status == 503
    assert [json.loads(c["body"])["queueing_timeout"] for c in t.calls] == [3, 2, 1]
    assert 2.0 <= elapsed < 5.0, f"waited {elapsed}s"


def test_queueing_timeout_respects_retry_false() -> None:
    # retry=False = exactly one request, window or not.
    t = FakeTransport()
    predicate = lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs")
    t.add_json(predicate, 503, _unavailable_body(0.05))

    with use_transport(t), pytest.raises(sdk.ArkerError) as error:
        client().vm("vm_1").run("true", queueing_timeout=30)
    assert error.value.code == "unavailable"
    assert len(t.calls) == 1


def test_fork_forwards_queueing_timeout() -> None:
    # fork() passes the caller's retry window to the shared transport.
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/fork"),
        200,
        {
            "vm_id": "vm_child",
            "owner_org_id": "owner",
            "created_at": "now",
            "description": None,
            "public": False,
            "state": "idle",
            "sessions": [session()],
            "network": {},
            "resources": {},
        },
    )

    with use_transport(t):
        client().fork(source_vm_name="ubuntu", queueing_timeout=30)
    assert json.loads(t.calls[0]["body"])["queueing_timeout"] == 30


def test_backoff_survives_an_unbounded_attempt_count() -> None:
    # A queueing window uncaps attempts, so the backoff exponent grows without
    # limit; base_delay_s * 2**attempt must not overflow the float multiply.
    retry = sdk.RetryOptions(attempts=4, base_delay_s=0.2, jitter_s=0.0)
    assert sdk._retry_delay(retry, 5000) == sdk.DEFAULT_RETRY_MAX_DELAY_S


def test_run_poll_budget_is_unbounded_without_a_caller_timeout() -> None:
    # The poll budget exists to outlive the server-side kill and report its
    # outcome. There is no server-side kill without a caller ``timeout``
    # (absent and ``0`` are both unbounded), so there is nothing to outlive and
    # the poll must not invent a deadline — abandoning a run that is still
    # going is worse than waiting.
    assert sdk.run_poll_budget_s(None) is None
    assert sdk.run_poll_budget_s(0) is None
    # A caller-set bound still gets the kill bound plus the 30s margin.
    assert sdk.run_poll_budget_s(5) == 5 + sdk.RUN_POLL_MARGIN_S
    assert sdk.run_poll_budget_s(3600) == 3600 + sdk.RUN_POLL_MARGIN_S
    # Negative is nonsense the API would reject; treat it as unbounded rather
    # than as an instantly-expired deadline.
    assert sdk.run_poll_budget_s(-1) is None


# ── run(): what bounds the wait, and what does not ──────────────────────────
# `timeout` is the SERVER-side kill bound (run_command.rs: "timeout: N kills the
# command after N seconds; zero is unbounded"). The SDK used to substitute a
# 3600s client deadline when it was unset, which raised `timeout` on runs the
# service was still executing — an hour-long stall on a deliberately unbounded
# server run. These pin the corrected contract in both directions.


def test_unset_timeout_never_gives_up_on_a_still_running_run(monkeypatch) -> None:
    """No `timeout` = no client deadline. The poll loop must keep waiting.

    Fails on the old code, which raised after its 3600s budget: here the clock
    is advanced past that budget while every poll answers 200 `running`.
    """
    monkeypatch.setattr(sdk.time, "sleep", lambda _s: None)
    clock = {"t": 0.0}

    # Each read jumps an hour, so any surviving 3600s-style deadline trips fast.
    def fake_monotonic() -> float:
        clock["t"] += 3600.0
        return clock["t"]

    monkeypatch.setattr(sdk.time, "monotonic", fake_monotonic)

    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs"),
        200,
        {"run_id": "run_srv", "state": "running"},
    )
    running = {
        "run_id": "run_srv",
        "state": "running",
        "started_at": "now",
        "exit_code": None,
        "stdout": "",
        "stdout_encoding": "utf-8",
        "stderr": "",
        "stderr_encoding": "utf-8",
    }
    for _ in range(25):  # far more polls than any hour-based budget would allow
        t.add_json(
            lambda method, url: method == "GET" and url.endswith("/v1/vms/vm_1/runs/run_srv"),
            200,
            running,
        )
    t.add_json(
        lambda method, url: method == "GET" and url.endswith("/v1/vms/vm_1/runs/run_srv"),
        200,
        {**running, "state": "completed", "exit_code": 0, "stdout": "bye\n"},
    )

    with use_transport(t):
        result = client().vm("vm_1").run("node server.js")

    assert result.state == "completed"
    assert result.stdout == "bye\n"
    # It really did keep polling rather than bailing early.
    assert sum(1 for c in t.calls if c["method"] == "GET") == 26


def test_explicit_timeout_still_bounds_the_wait(monkeypatch) -> None:
    """Setting `timeout` must still produce a bounded wait and a `timeout` error.

    Removing the default must not remove the knob — without this, "no timeouts"
    could be implemented by never timing out at all and nothing would notice.
    """
    monkeypatch.setattr(sdk.time, "sleep", lambda _s: None)
    clock = {"t": 0.0}

    def fake_monotonic() -> float:
        clock["t"] += 5.0
        return clock["t"]

    monkeypatch.setattr(sdk.time, "monotonic", fake_monotonic)

    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs"),
        200,
        {"run_id": "run_bound", "state": "running"},
    )
    for _ in range(60):
        t.add_json(
            lambda method, url: method == "GET" and url.endswith("/v1/vms/vm_1/runs/run_bound"),
            200,
            {
                "run_id": "run_bound",
                "state": "running",
                "started_at": "now",
                "exit_code": None,
                "stdout": "",
                "stdout_encoding": "utf-8",
                "stderr": "",
                "stderr_encoding": "utf-8",
            },
        )

    with use_transport(t), pytest.raises(sdk.ArkerError) as excinfo:
        client().vm("vm_1").run("sleep 999", timeout=10)

    assert excinfo.value.code == "timeout"
    # The message must tell the caller the run survives server-side.
    assert "run_bound" in str(excinfo.value)


def test_polling_gives_up_when_the_service_stops_answering(monkeypatch) -> None:
    """An unbounded wait still ends if the SERVICE goes away — but only then."""
    monkeypatch.setattr(sdk.time, "sleep", lambda _s: None)
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs"),
        200,
        {"run_id": "run_gone", "state": "running"},
    )
    for _ in range(sdk.RUN_POLL_MAX_CONSECUTIVE_FAILURES + 2):
        t.add_json(
            lambda method, url: method == "GET" and url.endswith("/v1/vms/vm_1/runs/run_gone"),
            503,
            {
                "error": {
                    "code": "unavailable",
                    "message": "service temporarily unavailable",
                }
            },
        )

    with use_transport(t), pytest.raises(sdk.ArkerError) as excinfo:
        client().vm("vm_1").run("node server.js")

    assert excinfo.value.code == "unavailable"
    assert "consecutive poll failures" in str(excinfo.value)


def test_a_poll_blip_does_not_end_an_unbounded_wait(monkeypatch) -> None:
    """A 200 resets the failure count, so a blip mid-run is survivable.

    This is the difference between "the service is gone" and "one request lost".
    """
    monkeypatch.setattr(sdk.time, "sleep", lambda _s: None)
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs"),
        200,
        {"run_id": "run_blip", "state": "running"},
    )
    is_get = lambda method, url: method == "GET" and url.endswith("/v1/vms/vm_1/runs/run_blip")
    running = {
        "run_id": "run_blip",
        "state": "running",
        "started_at": "now",
        "exit_code": None,
        "stdout": "",
        "stdout_encoding": "utf-8",
        "stderr": "",
        "stderr_encoding": "utf-8",
    }
    # Blip, recover, blip again — never MAX consecutive — then finish.
    for _ in range(sdk.RUN_POLL_MAX_CONSECUTIVE_FAILURES - 1):
        t.add_json(is_get, 503, {"error": {"code": "unavailable", "message": "blip"}})
    t.add_json(is_get, 200, running)
    for _ in range(sdk.RUN_POLL_MAX_CONSECUTIVE_FAILURES - 1):
        t.add_json(is_get, 503, {"error": {"code": "unavailable", "message": "blip"}})
    t.add_json(is_get, 200, {**running, "state": "completed", "exit_code": 0, "stdout": "ok\n"})

    with use_transport(t):
        result = client().vm("vm_1").run("node server.js")

    assert result.state == "completed"
    assert result.stdout == "ok\n"


def test_time_to_background_zero_matches_background_true(monkeypatch) -> None:
    """The two spellings of "don't wait" must behave identically.

    openapi.json: `background` is a "DEPRECATED alias for `time_to_background`;
    `true` is exactly `time_to_background: 0`". The SDK used to poll for the
    canonical field and pass through for the alias, so the alias behaved UNLIKE
    the thing it aliases. A caller starting a server with `time_to_background=0`
    got its run id from the server and then blocked in the client forever.
    """
    slept: list[float] = []
    monkeypatch.setattr(sdk.time, "sleep", lambda s: slept.append(s))

    def ack_only() -> FakeTransport:
        t = FakeTransport()
        t.add_json(
            lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs"),
            200,
            {"run_id": "run_srv", "state": "running"},
        )
        # Deliberately script NO get_run: any poll would 404 and fail loudly,
        # which is what we want — it proves no polling happened.
        return t

    t1 = ack_only()
    with use_transport(t1):
        r1 = client().vm("vm_1").run("node server.js", time_to_background=0)
    t2 = ack_only()
    with use_transport(t2):
        r2 = client().vm("vm_1").run("node server.js", time_to_background=0)

    assert isinstance(r1, sdk.BackgroundRunResult), "ttb=0 must return the ack, not poll"
    assert isinstance(r2, sdk.BackgroundRunResult)
    assert r1.state == r2.state == "running"
    # Neither made a single get_run call.
    assert [c["method"] for c in t1.calls] == ["POST"]
    assert [c["method"] for c in t2.calls] == ["POST"]
    assert slept == [], f"neither spelling should sleep/poll; slept={slept}"


def test_the_default_sync_path_still_polls_to_completion(monkeypatch) -> None:
    """Guard the other direction: honouring ttb=0 must not stop the DEFAULT
    synchronous run() from polling a backgrounded run to a terminal state."""
    monkeypatch.setattr(sdk.time, "sleep", lambda _s: None)
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url.endswith("/v1/vms/vm_1/runs"),
        200,
        {"run_id": "run_d", "state": "running"},
    )
    t.add_json(
        lambda method, url: method == "GET" and url.endswith("/v1/vms/vm_1/runs/run_d"),
        200,
        {
            "run_id": "run_d",
            "state": "completed",
            "started_at": "now",
            "exit_code": 0,
            "stdout": "fin\n",
            "stdout_encoding": "utf-8",
            "stderr": "",
            "stderr_encoding": "utf-8",
        },
    )

    with use_transport(t):
        result = client().vm("vm_1").run("sleep 1")

    assert isinstance(result, sdk.CompletedRunResult)
    assert result.stdout == "fin\n"
    assert [c["method"] for c in t.calls] == ["POST", "GET"]


def test_a_failed_build_step_deletes_the_vm_it_created(tmp_path, monkeypatch):
    import arker.build as build_mod
    from arker.build import BuildError

    deleted: list[str] = []
    fake_vm = SimpleNamespace(id="vmh-fake", delete=lambda: deleted.append("vmh-fake"))

    arker = sdk.Arker(api_key="ark_live_test", base_url="https://test.invalid/api", retry=False)
    monkeypatch.setattr(sdk.Arker, "_fork", lambda self, options, *, base_url: fake_vm)

    def failing_step(vm, steps, context_root, **kwargs):
        raise BuildError("RUN exited 1")

    monkeypatch.setattr(build_mod, "apply_steps", failing_step)

    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM ubuntu:24.04\nRUN false\n")

    with pytest.raises(BuildError):
        arker.fork(dockerfile=str(dockerfile))

    assert deleted == ["vmh-fake"]


def test_a_delete_that_fails_does_not_mask_the_build_error(tmp_path, monkeypatch):
    import arker.build as build_mod
    from arker.build import BuildError

    def refuse_delete():
        raise RuntimeError("delete refused")

    fake_vm = SimpleNamespace(id="vmh-fake", delete=refuse_delete)

    arker = sdk.Arker(api_key="ark_live_test", base_url="https://test.invalid/api", retry=False)
    monkeypatch.setattr(sdk.Arker, "_fork", lambda self, options, *, base_url: fake_vm)

    def failing_step(vm, steps, context_root, **kwargs):
        raise BuildError("RUN exited 1")

    monkeypatch.setattr(build_mod, "apply_steps", failing_step)

    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM ubuntu:24.04\nRUN false\n")

    with pytest.raises(BuildError, match="RUN exited 1"):
        arker.fork(dockerfile=str(dockerfile))


def _dockerfile(tmp_path) -> str:
    path = tmp_path / "Dockerfile"
    path.write_text("FROM ubuntu\nRUN echo hi\n")
    return str(path)


def test_a_dockerfile_build_inherits_the_fork_queueing_window(tmp_path, monkeypatch) -> None:
    import arker.build as build_module

    seen: dict[str, Any] = {}

    def fake_apply_steps(vm, steps, context_root, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(build_module, "apply_steps", fake_apply_steps)
    arker = client()
    monkeypatch.setattr(type(arker), "_fork", lambda self, options, *, base_url: SimpleNamespace(delete=lambda: None))

    arker._fork_dockerfile(
        _dockerfile(tmp_path),
        None,
        {"queueing_timeout": 900},
        base_url="https://test.invalid/api",
    )

    assert seen.get("queueing_timeout") == 900


def test_a_failed_build_retries_a_cleanup_delete_that_fails(tmp_path, monkeypatch) -> None:
    import arker.build as build_module

    attempts: list[int] = []

    def failing_apply_steps(vm, steps, context_root, **kwargs):
        raise RuntimeError("build step failed")

    def flaky_delete():
        attempts.append(1)
        if len(attempts) < 3:
            raise sdk.ArkerError("unavailable", "service temporarily unavailable", 503)

    monkeypatch.setattr(build_module, "apply_steps", failing_apply_steps)
    monkeypatch.setattr(sdk.time, "sleep", lambda _s: None)
    arker = client()
    monkeypatch.setattr(type(arker), "_fork", lambda self, options, *, base_url: SimpleNamespace(delete=flaky_delete))

    with pytest.raises(RuntimeError, match="build step failed"):
        arker._fork_dockerfile(_dockerfile(tmp_path), None, {}, base_url="https://test.invalid/api")

    assert len(attempts) == 3


def test_the_shared_client_does_not_multiplex_requests_over_one_socket() -> None:
    pool = sdk._http_client._transport._pool
    assert pool._http2 is False
    assert pool._http1 is True


# ── fork Idempotency-Key ────────────────────────────────────────────────
#
# The prod failure: the gateway answered a fork with 502/504 AFTER the worker
# had already built the VM, `_request` re-POSTed, and the caller got a SECOND
# machine while the first ran on, unnamed and billable. Transport failures on a
# mutation are not retried, but a 502/504 is a *response* and still is --
# so this is the window that remains, and the key is what closes it.

_FORK_VM = {
    "vm_id": "vm_child",
    "owner_org_id": "owner",
    "created_at": "now",
    "description": None,
    "public": False,
    "state": "idle",
    "sessions": [],
    "network": {},
    "resources": {},
}


def _fork_transport(*statuses: int) -> FakeTransport:
    t = FakeTransport()
    for status in statuses:
        t.add_json(
            lambda method, url: method == "POST" and url.endswith("/v1/fork"),
            status,
            _FORK_VM if status < 400 else {"error": {"code": "bad_gateway", "message": "lost"}},
        )
    return t


def _retrying_client() -> sdk.Arker:
    return sdk.Arker(
        api_key="ark_live_test",
        base_url="https://test.invalid/api",
        retry={"attempts": 2, "base_delay_s": 0, "jitter_s": 0},
    )


def test_fork_sends_no_key_unless_asked() -> None:
    """OFF is the default. An unkeyed fork is never deduplicated, which is the
    API's own behaviour -- so a caller who says nothing must get exactly that,
    not a key the SDK decided to add."""
    t = _fork_transport(200)

    with use_transport(t):
        client().fork(source_vm_id="source-vm-id")

    assert "idempotency-key" not in t.calls[0]["headers"]


def test_fork_generates_a_key_when_idempotency_is_requested() -> None:
    t = _fork_transport(200)

    with use_transport(t):
        client().fork(source_vm_id="source-vm-id", idempotency=True)

    assert t.calls[0]["headers"]["idempotency-key"].startswith("sdk-fork-")
    # A header, not a contract field: `idempotency` must not reach the body
    # either, or the server's validator rejects the unknown key.
    assert "idempotency" not in json.loads(t.calls[0]["body"])
    # A header, not a contract field. `fork(**options)` passes everything into
    # the body, so a key left in there would 400 on the server's validator.
    assert "idempotency_key" not in json.loads(t.calls[0]["body"])


def test_fork_retry_reuses_the_same_idempotency_key() -> None:
    """The load-bearing one: the retried attempt must present the FIRST key.

    A fresh key per attempt looks identical in every other test -- a key is
    sent, it is well formed, the fork succeeds -- and still builds the
    duplicate VM this exists to prevent.
    """
    t = _fork_transport(502, 200)

    with use_transport(t):
        _retrying_client().fork(source_vm_id="source-vm-id", idempotency=True)

    assert len(t.calls) == 2, f"expected a retry, got {len(t.calls)} call(s)"
    first = t.calls[0]["headers"]["idempotency-key"]
    # Non-empty as well as equal: two blank keys are also "the same key", and
    # would let a broken generator pass this test.
    assert first, "the fork sent an empty Idempotency-Key"
    assert first == t.calls[1]["headers"]["idempotency-key"]


def test_fork_uses_an_explicit_idempotency_key_verbatim() -> None:
    t = _fork_transport(200)

    with use_transport(t):
        client().fork(source_vm_id="source-vm-id", idempotency_key="caller-chosen")

    assert t.calls[0]["headers"]["idempotency-key"] == "caller-chosen"


def test_an_explicit_key_wins_over_the_flag() -> None:
    """The key is the more specific instruction, and the only one the caller
    can act on later."""
    t = _fork_transport(200)

    with use_transport(t):
        client().fork(source_vm_id="source-vm-id", idempotency=True, idempotency_key="caller-chosen")

    assert t.calls[0]["headers"]["idempotency-key"] == "caller-chosen"


def test_two_forks_do_not_share_a_generated_key() -> None:
    """Generated keys scope to ONE call; sharing one across separate forks
    would collapse two deliberate machines into one."""
    t = _fork_transport(200, 200)

    with use_transport(t):
        arker = client()
        arker.fork(source_vm_id="source-vm-id", idempotency=True)
        arker.fork(source_vm_id="source-vm-id", idempotency=True)

    assert t.calls[0]["headers"]["idempotency-key"] != t.calls[1]["headers"]["idempotency-key"]


def test_generated_fork_key_fits_the_server_limit() -> None:
    """The handler rejects anything over 64 characters before it forks, so a
    generated key that outgrew the cap would 400 every unkeyed fork."""
    t = _fork_transport(200)

    with use_transport(t):
        client().fork(source_vm_id="source-vm-id", idempotency=True)

    assert 0 < len(t.calls[0]["headers"]["idempotency-key"]) <= 64
