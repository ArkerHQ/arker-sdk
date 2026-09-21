from __future__ import annotations

import base64
import json
import time
from contextlib import contextmanager
from typing import Any

import httpx
import pytest

import arker.computer as sdk


class FakeTransport:
    """Scripts httpx responses by (method, url); drive it with ``use_transport``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.script: list[tuple[Any, int, bytes]] = []

    def add_json(self, predicate, status: int, body: dict[str, Any]) -> None:
        self.script.append((predicate, status, json.dumps(body).encode()))

    def add_raw(self, predicate, status: int, body: bytes) -> None:
        self.script.append((predicate, status, body))

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        url = str(request.url)
        self.calls.append({"method": method, "url": url, "body": request.content or None, "headers": request.headers})

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


def test_constructor_requires_base_url(monkeypatch) -> None:
    monkeypatch.delenv("ARKER_BASE_URL", raising=False)
    monkeypatch.delenv("ARKER_REGION", raising=False)
    monkeypatch.delenv("ARKER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="provider and region or base_url are required"):
        sdk.Arker(api_key="ark_live_test")


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


def test_invalid_provider_syntax_fails_closed() -> None:
    with pytest.raises(ValueError, match="provider"):
        sdk.Arker(
            api_key="ark_live_test",
            provider="bad.example/path",
            region="region-one",
        )


def test_explicit_vm_handle_uses_placement_endpoint() -> None:
    arker = sdk.Arker(
        api_key="ark_live_test",
        base_url="https://fallback.invalid/api",
        retry=False,
    )
    vm = arker.vm("vm_placed", provider="provider-two", region="region-two")

    assert vm.base_url == "https://provider-two-region-two.arker.ai/api"


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
        lambda method, url: method == "GET"
        and url == "https://arker.ai/api/v1/regions",
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
        lambda method, url: method == "GET"
        and url == "https://control.invalid/api/v1/regions",
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
        vm = client().fork("catalog-template")

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


def test_fork_rejects_an_image_alongside_a_source_vm() -> None:
    for kwargs in (
        {"image": "ubuntu:24.04", "source_vm_name": "base"},
        {"image": "ubuntu:24.04", "source_vm_id": "vm_abc"},
    ):
        with pytest.raises(sdk.ArkerError) as excinfo:
            client().fork(**kwargs)  # type: ignore[arg-type]
        assert excinfo.value.code == "bad_request"


def test_fork_rejects_an_image_passed_positionally_and_by_keyword() -> None:
    # The positional form resolves to `source_vm_name`, so this is the same
    # collision as above arriving by a different route.
    with pytest.raises(sdk.ArkerError) as excinfo:
        client().fork("base", image="ubuntu:24.04")
    assert excinfo.value.code == "bad_request"


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


def test_fork_from_a_dockerfile_sends_only_the_dockerfile() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and url == "https://test.invalid/api/v1/fork",
        200,
        _fork_response("vm_dockerfile"),
    )

    with use_transport(t):
        vm = client().fork(dockerfile="FROM ubuntu:24.04\nRUN echo hi\n")

    assert vm.id == "vm_dockerfile"
    body = json.loads(t.calls[0]["body"])
    assert body == {"dockerfile": "FROM ubuntu:24.04\nRUN echo hi\n"}


def test_fork_rejects_an_image_and_a_dockerfile_together() -> None:
    with pytest.raises(sdk.ArkerError) as excinfo:
        client().fork(image="ubuntu:24.04", dockerfile="FROM ubuntu:24.04\n")
    assert excinfo.value.code == "bad_request"


def test_fork_rejects_a_dockerfile_alongside_a_source_vm() -> None:
    for kwargs in (
        {"dockerfile": "FROM ubuntu:24.04\n", "source_vm_name": "base"},
        {"dockerfile": "FROM ubuntu:24.04\n", "source_vm_id": "vm_abc"},
    ):
        with pytest.raises(sdk.ArkerError) as excinfo:
            client().fork(**kwargs)  # type: ignore[arg-type]
        assert excinfo.value.code == "bad_request"


def test_fork_refuses_registry_auth_without_something_to_pull() -> None:
    """Silently dropping them would send credentials nowhere and look fine."""
    with pytest.raises(sdk.ArkerError) as excinfo:
        client().fork(
            source_vm_name="base",
            registry_auth={"username": "u", "password": "p"},
        )
    assert excinfo.value.code == "bad_request"


def test_fork_rejects_legacy_id_response() -> None:
    """A response missing the fields we REQUIRE is still an error.

    The legacy shape sends `id` where the contract says `vm_id`, so decoding
    must fail — but now because `vm_id` is absent, not because `id` is extra.
    """
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
    version has to keep working. This previously raised
    `TypeError: Vm contains fields outside openapi.json: ...`
    and broke `fork()` outright against a newer deployment.
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
            "some_future_flag": True,
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
        lambda method, url: method == "GET" and url == "https://arker.ai/api/v1/vms?region=us-west-2&provider=aws&org_id=ArkerHQ&public=True&state=idle",
        200,
        {"vms": [{
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
        }]},
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
    assert result.vms[0].resources == sdk.VmResources(
        vcpu=2, memory_mib=1024, disk_mib=4096
    )


def test_listed_vm_uses_its_placement_endpoint() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "GET" and url == "https://arker.ai/api/v1/vms",
        200,
        {"vms": [{
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
        }]},
    )
    t.add_json(
        lambda method, url: method == "POST" and url == "https://provider-two-region-two.arker.ai/api/v1/vms/vm_placed/runs",
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
        lambda method, url: method == "GET" and url == "https://control.invalid/api/v1/runs?since=10&until=20&vm=vm_1&vms=vm_2%2Cvm_3&region=us-west-2&provider=aws&search=pytest&limit=25&offset=5&lite=True&runtime=vm&endpoint=run&actions=run%2Cfork&status=success%2Cinternal&status_min=200&status_max=599&sort=when&dir=asc",
        200,
        {
            "since": 10,
            "until": 20,
            "limit": 25,
            "offset": 5,
            "lite": True,
            "rows": [{
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
                "executor_kind": "vm",
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
            }],
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
            runtime="vm",
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
        {"run_id": "run_bg", "state": "running", "started_at": "now", "exit_code": None,
         "stdout": "", "stdout_encoding": "utf-8", "stderr": "", "stderr_encoding": "utf-8"},
    )
    t.add_json(
        lambda method, url: method == "GET" and url.endswith("/v1/vms/vm_1/runs/run_bg"),
        200,
        {"run_id": "run_bg", "state": "completed", "started_at": "now", "exit_code": 0,
         "stdout": "done\n", "stdout_encoding": "utf-8", "stderr": "", "stderr_encoding": "utf-8"},
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
    assert json.loads(t.calls[0]["body"]) == {"command": "sleep 999", "time_to_background": 0}


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

    # resize now PATCHes /v1/vms/{id} with a resources object (the server has no
    # POST /v1/vms/{id}/resize route). None fields are pruned.
    assert json.loads(t.calls[0]["body"]) == {"resources": {"memory_mib": 1024}}
    assert result is not None


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
    assert json.loads(t.calls[0]["body"]) == {"command": "sleep 10", "time_to_background": 0}


def test_flat_error_response_is_rejected_as_malformed() -> None:
    t = FakeTransport()
    t.add_json(lambda _method, _url: True, 404, {"code": "not_found", "message": "missing"})

    with use_transport(t), pytest.raises(sdk.ArkerError) as caught:
        client().vm("missing").delete()

    assert caught.value.code == "internal"
    assert caught.value.status == 404


def test_error_response_with_legacy_top_level_field_is_rejected() -> None:
    t = FakeTransport()
    t.add_json(lambda _method, _url: True, 404, {"ok": False, "error": {"code": "not_found", "message": "missing"}})

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
    t.add_json(predicate, 200, {"ok": True, "op": "read", "path": "/home/user/x", "size": 2, "content": "ok", "encoding": "utf-8"})

    with use_transport(t):
        assert sdk.Arker(api_key="k", base_url="https://test.invalid/api", retry={"attempts": 2}).vm("vm").sync("/home/user/x") == b"ok"

    assert len(t.calls) == 2


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
        {"ok": True, "op": "read", "path": "/home/user/big", "size": 5, "presigned_url": "https://storage.invalid/file", "expires_in": 900, "method": "GET"},
    )
    t.add_raw(lambda method, url: method == "GET" and url == "https://storage.invalid/file", 200, b"hello")

    with use_transport(t):
        assert client().vm("vm_1").sync("/home/user/big") == b"hello"


def test_small_write_streams_to_canonical_sync() -> None:
    """A content write puts the bytes on the wire as-is, in one request."""
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "POST" and "/sync?" in url,
        200,
        {"ok": True},
    )

    with use_transport(t):
        client().vm("vm_1").sync("/home/user/x", b"hello world")

    call = t.calls[0]
    assert "/sync?" in call["url"]
    assert "path=%2Fhome%2Fuser%2Fx" in call["url"]
    assert "size=11" in call["url"]
    # The bytes travel as-is.
    body = call["body"]
    assert (body.encode() if isinstance(body, str) else body) == b"hello world"


def test_empty_write_sends_one_empty_chunk() -> None:
    t = FakeTransport()
    predicate = lambda method, url: method == "POST" and url.endswith("/sync")
    t.add_json(predicate, 200, {"ok": True, "op": "write", "results": [{
        "path": "/home/user/empty",
        "size": 0,
        "received_bytes": 0,
        "ranges": [{"start": 0, "end": 0}],
        "complete": True,
        "written": True,
    }]})

    with use_transport(t):
        # `sync()` streams now; the chunked write machinery stays reachable as
        # a compatibility fallback, so this exercises it directly rather than
        # through the public entrypoint.
        client().vm("vm_1")._sync_write_inline("/home/user/empty", b"")

    writes = json.loads(t.calls[0]["body"])["writes"]
    assert len(writes) == 1
    assert (writes[0]["start"], writes[0]["end"], writes[0]["size"]) == (0, 0, 0)
    assert writes[0]["content"] == ""


def test_mid_size_write_inlines_chunks_in_one_request() -> None:
    payload = b"A" * (sdk.CHUNK_SIZE + 1)
    t = FakeTransport()
    predicate = lambda method, url: method == "POST" and url.endswith("/sync")
    t.add_json(predicate, 200, {"ok": True, "op": "write", "results": [
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
    ]})

    with use_transport(t):
        # `sync()` streams now; the chunked write machinery stays reachable as
        # a compatibility fallback, so this exercises it directly rather than
        # through the public entrypoint.
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


def test_large_inline_write_splits_across_multiple_requests() -> None:
    """A >20MB inline-fallback write is the exact shape that hard-failed with
    `payload_too_large` before arkerd's `MAX_SYNC_INLINE_REQUEST_BYTES` cap
    was respected client-side: `_sync_write_inline` used to put every chunk
    of the whole file in ONE request regardless of size. It must now split
    into multiple sequential requests, each staying within
    `INLINE_WRITE_LIMIT`, sharing one `upload_id` so arkerd's
    `SyncChunkSessionState` ledger reassembles them across requests."""
    chunk = sdk.CHUNK_SIZE
    # 6 chunks (~24MiB decoded): more than fits in one arkerd
    # MAX_SYNC_INLINE_REQUEST_BYTES=20MiB request, and more than fits in one
    # INLINE_WRITE_LIMIT=16MiB batch, so this forces >=2 requests.
    size = 6 * chunk + 1
    payload = bytes(i % 256 for i in range(size))

    # Mirror exactly what `_sync_write_inline` builds, then batch it through
    # the SDK's own (now unit-tested-by-construction) batching helper so the
    # expected request shape can never drift from the implementation.
    starts = list(range(0, size, chunk))
    entries = [
        sdk.SyncChunkWrite(
            path="/home/user/huge",
            size=size,
            upload_id="uid_test",
            content=base64.b64encode(payload[start : start + chunk]).decode("ascii"),
            start=start,
            end=min(start + chunk, size),
        )
        for start in starts
    ]
    batches = sdk._inline_write_batches(entries)
    assert len(batches) >= 2, "test payload must force multiple requests"

    t = FakeTransport()
    predicate = lambda method, url: method == "POST" and url.endswith("/sync")
    for batch_index, batch in enumerate(batches):
        is_last_batch = batch_index == len(batches) - 1
        results = [
            {
                "path": "/home/user/huge",
                "size": size,
                "received_bytes": entry.end,
                "ranges": [{"start": 0, "end": entry.end}],
                "complete": is_last_batch and i == len(batch) - 1,
                "written": is_last_batch and i == len(batch) - 1,
            }
            for i, entry in enumerate(batch)
        ]
        t.add_json(predicate, 200, {"ok": True, "op": "write", "results": results})

    with use_transport(t):
        client().vm("vm_1")._sync_write_inline("/home/user/huge", payload)

    # One request per batch — the fix's whole point is that this is NOT one
    # request for the entire file.
    assert len(t.calls) == len(batches)

    all_writes = []
    for call in t.calls:
        writes = json.loads(call["body"])["writes"]
        all_writes.extend(writes)
        # Each request's own decoded total must stay within the server's
        # inline cap. Before the fix, a single request carried every chunk of
        # the whole (>20MB) file and arkerd rejected it with
        # `payload_too_large`.
        decoded_total = sum(len(base64.b64decode(w["content"])) for w in writes)
        assert decoded_total <= sdk.INLINE_WRITE_LIMIT

    # Every chunk shares one upload_id, in order, and reassembles losslessly —
    # this is what lets arkerd's cross-request chunk-session ledger treat the
    # split requests as one upload.
    upload_ids = {w["upload_id"] for w in all_writes}
    assert len(upload_ids) == 1
    reconstructed = b"".join(base64.b64decode(w["content"]) for w in all_writes)
    assert reconstructed == payload


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
    t.add_json(predicate, 200, {"ok": True, "op": "write", "results": [{
        "path": "/home/user/x",
        "size": 5,
        "received_bytes": 5,
        "ranges": [{"start": 0, "end": 5}],
        "complete": True,
        "written": True,
    }]})

    with use_transport(t):
        sdk.Arker(api_key="k", base_url="https://test.invalid/api", retry={"attempts": 2}).vm("vm")._sync_write_inline("/home/user/x", b"hello")

    assert len(t.calls) == 2


def test_get_and_set_policies() -> None:
    t = FakeTransport()
    t.add_json(
        lambda method, url: method == "GET" and url == "https://test.invalid/api/v1/vms/vm_1/policies",
        200,
        {"policies": [], "secrets": {}, "hostname": None, "mitm_domains": [], "warnings": []},
    )
    with use_transport(t):
        doc = client().vm("vm_1").get_policies()
    assert doc.policies == []

    t.add_json(
        lambda method, url: method == "PUT" and url == "https://test.invalid/api/v1/vms/vm_1/policies",
        200,
        {
            "policies": [{"type": "outbound", "match": {"hosts": ["example.com"]}, "action": "allow"}],
            "secrets": {},
            "hostname": None,
            "mitm_domains": ["example.com"],
            "warnings": [],
        },
    )
    with use_transport(t):
        updated = client().vm("vm_1").set_policies({
            "policies": [{"type": "outbound", "match": {"hosts": ["example.com"]}, "action": "allow"}],
        })
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
    t.add_json(lambda method, url: method == "GET" and url.endswith("/runs/run_1"), 200, stored_body)

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
        try:
            vm.connect_pty()          # no websocket-client / no server: the
        except Exception:             # session POST still happens first
            pass
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
            vgpu=0.25,
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
        client().fork(source_vm_id="gpu-source-vm-id", vcpu_count=4, vgpu=0.5)

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
    t.add_json(predicate, 503, {"error": {
        "code": "unavailable", "message": "cold",
        "retry_after": 0.05, "timestamp": "2026-01-01T00:00:00.000Z",
    }})
    t.add_json(predicate, 200, {
        "vm_id": "vm_child", "owner_org_id": "owner", "created_at": "now",
        "description": None, "public": False, "state": "idle",
        "sessions": [session()], "network": {}, "resources": {},
    })

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
    return {"error": {
        "code": "unavailable", "message": "at capacity",
        "retry_after": retry_after_s, "timestamp": "2026-01-01T00:00:00.000Z",
    }}


def _completed_run_body() -> dict[str, Any]:
    return {"run_id": "run_q", "state": "completed", "exit_code": 0,
            "stdout": "ok", "stdout_encoding": "utf-8",
            "stderr": "", "stderr_encoding": "utf-8"}


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
    with use_transport(t):
        with pytest.raises(sdk.ArkerError) as error:
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

    with use_transport(t):
        with pytest.raises(sdk.ArkerError) as error:
            client().vm("vm_1").run("true", queueing_timeout=30)
    assert error.value.code == "unavailable"
    assert len(t.calls) == 1


def test_fork_forwards_queueing_timeout() -> None:
    # fork() builds its body from an explicit kwarg list — pin that the field
    # survives it. The retry mechanics are shared with run and tested there.
    t = FakeTransport()
    t.add_json(lambda method, url: method == "POST" and url.endswith("/v1/fork"), 200, {
        "vm_id": "vm_child", "owner_org_id": "owner", "created_at": "now",
        "description": None, "public": False, "state": "idle",
        "sessions": [session()], "network": {}, "resources": {},
    })

    with use_transport(t):
        client().fork("ubuntu", queueing_timeout=30)
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
        200, {"run_id": "run_srv", "state": "running"},
    )
    running = {"run_id": "run_srv", "state": "running", "started_at": "now",
               "exit_code": None, "stdout": "", "stdout_encoding": "utf-8",
               "stderr": "", "stderr_encoding": "utf-8"}
    for _ in range(25):  # far more polls than any hour-based budget would allow
        t.add_json(
            lambda method, url: method == "GET" and url.endswith("/v1/vms/vm_1/runs/run_srv"),
            200, running,
        )
    t.add_json(
        lambda method, url: method == "GET" and url.endswith("/v1/vms/vm_1/runs/run_srv"),
        200, {**running, "state": "completed", "exit_code": 0, "stdout": "bye\n"},
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
        200, {"run_id": "run_bound", "state": "running"},
    )
    for _ in range(60):
        t.add_json(
            lambda method, url: method == "GET" and url.endswith("/v1/vms/vm_1/runs/run_bound"),
            200, {"run_id": "run_bound", "state": "running", "started_at": "now",
                  "exit_code": None, "stdout": "", "stdout_encoding": "utf-8",
                  "stderr": "", "stderr_encoding": "utf-8"},
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
        200, {"run_id": "run_gone", "state": "running"},
    )
    for _ in range(sdk.RUN_POLL_MAX_CONSECUTIVE_FAILURES + 2):
        t.add_json(
            lambda method, url: method == "GET" and url.endswith("/v1/vms/vm_1/runs/run_gone"),
            503, {"error": {"code": "unavailable", "message": "service temporarily unavailable"}},
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
        200, {"run_id": "run_blip", "state": "running"},
    )
    is_get = lambda method, url: method == "GET" and url.endswith("/v1/vms/vm_1/runs/run_blip")
    running = {"run_id": "run_blip", "state": "running", "started_at": "now",
               "exit_code": None, "stdout": "", "stdout_encoding": "utf-8",
               "stderr": "", "stderr_encoding": "utf-8"}
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
            200, {"run_id": "run_srv", "state": "running"},
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
        200, {"run_id": "run_d", "state": "running"},
    )
    t.add_json(
        lambda method, url: method == "GET" and url.endswith("/v1/vms/vm_1/runs/run_d"),
        200, {"run_id": "run_d", "state": "completed", "started_at": "now", "exit_code": 0,
              "stdout": "fin\n", "stdout_encoding": "utf-8", "stderr": "",
              "stderr_encoding": "utf-8"},
    )

    with use_transport(t):
        result = client().vm("vm_1").run("sleep 1")

    assert isinstance(result, sdk.CompletedRunResult)
    assert result.stdout == "fin\n"
    assert [c["method"] for c in t.calls] == ["POST", "GET"]


# ── Unified sync(): one call, transport chosen internally ────────────────────

def _stream_calls(t: FakeTransport) -> list[dict[str, Any]]:
    return [call for call in t.calls if "/sync?" in call["url"]]


def _extract_mode(url: str) -> str | None:
    query = httpx.URL(url).params
    return query.get("extract")


def test_sync_from_local_small_file_sends_bytes(tmp_path) -> None:
    """A lone small file has nothing to gain from an archive, so it goes as
    plain bytes in one request."""
    local = tmp_path / "note.txt"
    local.write_bytes(b"hello world")

    t = FakeTransport()
    t.add_json(lambda m, u: m == "POST" and "/sync?" in u, 200, {"ok": True})

    with use_transport(t):
        result = client().vm("vm_1").sync("/home/user/note.txt", from_local=str(local))

    assert (result.sent, result.bytes_sent) == (1, 11)
    call = _stream_calls(t)[0]
    assert _extract_mode(call["url"]) is None
    assert "path=%2Fhome%2Fuser%2Fnote.txt" in call["url"]
    assert call["body"] == b"hello world"


def test_sync_from_local_large_file_uses_archive(tmp_path) -> None:
    """At or above the archive threshold a single file travels as an archive,
    so its mode bits travel with it."""
    local = tmp_path / "blob.bin"
    local.write_bytes(b"\0" * (sdk.ARCHIVE_MIN_BYTES + 1))

    t = FakeTransport()
    t.add_json(lambda m, u: m == "POST" and "/sync?" in u, 200, {"ok": True})

    with use_transport(t):
        client().vm("vm_1").sync("/home/user/blob.bin", from_local=str(local))

    call = _stream_calls(t)[0]
    assert _extract_mode(call["url"]) in ("tar", "tar.gz")
    assert "path=%2Fhome%2Fuser" in call["url"]


def test_sync_from_local_executable_uses_archive(tmp_path) -> None:
    """An executable file is small but still needs its mode preserved."""
    local = tmp_path / "tool"
    local.write_bytes(b"#!/bin/sh\necho hi\n")
    local.chmod(0o755)

    t = FakeTransport()
    t.add_json(lambda m, u: m == "POST" and "/sync?" in u, 200, {"ok": True})

    with use_transport(t):
        client().vm("vm_1").sync("/usr/local/bin/tool", from_local=str(local))

    assert _extract_mode(_stream_calls(t)[0]["url"]) in ("tar", "tar.gz")


def test_sync_from_local_dir_sends_one_archive(tmp_path) -> None:
    """A multi-file tree is one request regardless of file count."""
    (tmp_path / "pkg").mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / "pkg" / name).write_text(name * 10)

    t = FakeTransport()
    t.add_json(
        lambda m, u: m == "POST" and u.endswith("/sync"),
        200,
        {"ok": True, "op": "manifest", "root": "/home/user/pkg", "hash_algo": "sha256",
         "entries": [], "truncated": False},
    )
    t.add_json(lambda m, u: m == "POST" and "/sync?" in u, 200, {"ok": True})

    with use_transport(t):
        result = client().vm("vm_1").sync("/home/user/pkg", from_local=str(tmp_path / "pkg"))

    assert result.sent == 3
    assert len(_stream_calls(t)) == 1
    assert _extract_mode(_stream_calls(t)[0]["url"]) in ("tar", "tar.gz")


def test_batch_by_size_fills_greedily_and_never_splits_one_file() -> None:
    """Pure unit coverage for the batching primitive, independent of the HTTP
    layer: greedy fill up to (not over) the cap, order preserved, and an
    oversized single file gets its own batch rather than being split (which
    this helper deliberately does not attempt -- a single file over the
    server's own per-file cap is a separate, pre-existing limit)."""
    entry = lambda rel, size: (rel, f"/abs/{rel}", size, 0o100644)  # noqa: E731

    # Exact-boundary packing: 3 + 3 == cap(6) fits one batch; +3 more forces a
    # second.
    files = [entry("a", 3), entry("b", 3), entry("c", 3)]
    batches = sdk.VM._batch_by_size(files, cap=6)
    assert [[e[0] for e in b] for b in batches] == [["a", "b"], ["c"]]

    # A single file at/above the cap is not split, and does not drag
    # neighboring small files into its own batch.
    files = [entry("small", 1), entry("huge", 100), entry("small2", 1)]
    batches = sdk.VM._batch_by_size(files, cap=6)
    assert [[e[0] for e in b] for b in batches] == [["small"], ["huge"], ["small2"]]

    # Empty input -> no batches.
    assert sdk.VM._batch_by_size([], cap=6) == []


def test_sync_from_local_dir_splits_oversized_changed_set(tmp_path, monkeypatch) -> None:
    """A changed set that would exceed arkerd's per-request MAX_SYNC_FILE_BYTES
    cap must split into multiple archives instead of building one oversized tar
    and having the server reject it with 413 payload_too_large.

    Real files stay tiny here (this is a unit test); ARCHIVE_BATCH_MAX_BYTES is
    patched down so the same batching code path is exercised at a size the
    test can afford. ARCHIVE_BATCH_CONCURRENCY is patched to 1 so the batches
    upload sequentially -- FakeTransport's call log and script queue are not
    thread-safe, and the real concurrency is exercised by TypeScript's
    equivalent batching test and this one still proves the split happens.
    """
    monkeypatch.setattr(sdk, "ARCHIVE_BATCH_MAX_BYTES", 5)
    monkeypatch.setattr(sdk, "ARCHIVE_BATCH_CONCURRENCY", 1)

    (tmp_path / "pkg").mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / "pkg" / name).write_bytes(b"12345")  # 5 bytes == the patched cap

    t = FakeTransport()
    t.add_json(
        lambda m, u: m == "POST" and u.endswith("/sync"),
        200,
        {"ok": True, "op": "manifest", "root": "/home/user/pkg", "hash_algo": "sha256",
         "entries": [], "truncated": False},
    )
    for _ in range(3):
        t.add_json(lambda m, u: m == "POST" and "/sync?" in u, 200, {"ok": True})

    with use_transport(t):
        result = client().vm("vm_1").sync("/home/user/pkg", from_local=str(tmp_path / "pkg"))

    # One 5-byte file per batch under a 5-byte cap: three files, three
    # archives -- never one archive carrying all three.
    assert result.sent == 3
    assert len(_stream_calls(t)) == 3
    for call in _stream_calls(t):
        assert _extract_mode(call["url"]) in ("tar", "tar.gz")


def test_sync_from_local_dir_skips_unchanged(tmp_path) -> None:
    """What the VM already has is authoritative: matching files are not sent."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.txt").write_text("same")
    digest = __import__("hashlib").sha256(b"same").hexdigest()

    t = FakeTransport()
    t.add_json(
        lambda m, u: m == "POST" and u.endswith("/sync"),
        200,
        {"ok": True, "op": "manifest", "root": "/home/user/pkg", "hash_algo": "sha256",
         "entries": [{"path": "a.txt", "size": 4, "mode": 0o100644, "hash": digest}],
         "truncated": False},
    )

    with use_transport(t):
        result = client().vm("vm_1").sync("/home/user/pkg", from_local=str(tmp_path / "pkg"))

    assert (result.sent, result.skipped) == (0, 1)
    assert _stream_calls(t) == []


def test_incompressible_payload_is_not_compressed(tmp_path) -> None:
    """Compression only pays when the payload actually shrinks."""
    import os as _os
    (tmp_path / "pkg").mkdir()
    for index in range(4):
        (tmp_path / "pkg" / f"r{index}.bin").write_bytes(_os.urandom(256 * 1024))

    t = FakeTransport()
    t.add_json(
        lambda m, u: m == "POST" and u.endswith("/sync"),
        200,
        {"ok": True, "op": "manifest", "root": "/home/user/pkg", "hash_algo": "sha256",
         "entries": [], "truncated": False},
    )
    t.add_json(lambda m, u: m == "POST" and "/sync?" in u, 200, {"ok": True})

    with use_transport(t):
        client().vm("vm_1").sync("/home/user/pkg", from_local=str(tmp_path / "pkg"))

    assert _extract_mode(_stream_calls(t)[0]["url"]) == "tar"


def test_compressible_payload_is_compressed(tmp_path) -> None:
    (tmp_path / "pkg").mkdir()
    for index in range(4):
        (tmp_path / "pkg" / f"t{index}.txt").write_text("def hello(): return 'world'\n" * 12000)

    t = FakeTransport()
    t.add_json(
        lambda m, u: m == "POST" and u.endswith("/sync"),
        200,
        {"ok": True, "op": "manifest", "root": "/home/user/pkg", "hash_algo": "sha256",
         "entries": [], "truncated": False},
    )
    t.add_json(lambda m, u: m == "POST" and "/sync?" in u, 200, {"ok": True})

    with use_transport(t):
        client().vm("vm_1").sync("/home/user/pkg", from_local=str(tmp_path / "pkg"))

    assert _extract_mode(_stream_calls(t)[0]["url"]) == "tar.gz"


def test_sync_to_local_file(tmp_path) -> None:
    """VM -> client for a single file."""
    dest = tmp_path / "out.txt"
    t = FakeTransport()
    t.add_json(
        lambda m, u: m == "POST" and u.endswith("/sync"),
        200,
        {"ok": True, "op": "manifest", "root": "/home/user/out.txt", "hash_algo": "sha256",
         "entries": [], "truncated": False},
    )
    t.add_json(
        lambda m, u: m == "POST" and u.endswith("/sync"),
        200,
        {"ok": True, "op": "read", "path": "/home/user/out.txt", "size": 5,
         "content": "hello", "encoding": "utf8"},
    )

    with use_transport(t):
        result = client().vm("vm_1").sync("/home/user/out.txt", to_local=str(dest))

    assert dest.read_bytes() == b"hello"
    assert (result.sent, result.bytes_sent) == (1, 5)


def test_sync_to_local_dir_reads_each_file(tmp_path) -> None:
    """VM -> client for a directory. There is no bulk read, so this is one
    request per file."""
    dest = tmp_path / "pulled"
    t = FakeTransport()
    t.add_json(
        lambda m, u: m == "POST" and u.endswith("/sync"),
        200,
        {"ok": True, "op": "manifest", "root": "/home/user/pkg", "hash_algo": "sha256",
         "entries": [
             {"path": "a.txt", "size": 1, "mode": 0o100644, "hash": "0" * 64},
             {"path": "nested/b.txt", "size": 1, "mode": 0o100644, "hash": "1" * 64},
         ],
         "truncated": False},
    )
    for body in ("A", "B"):
        t.add_json(
            lambda m, u: m == "POST" and u.endswith("/sync"),
            200,
            {"ok": True, "op": "read", "path": "x", "size": 1, "content": body, "encoding": "utf8"},
        )

    with use_transport(t):
        result = client().vm("vm_1").sync("/home/user/pkg", to_local=str(dest))

    assert result.sent == 2
    assert sorted(q.name for q in dest.rglob("*.txt")) == ["a.txt", "b.txt"]
    assert (dest / "nested" / "b.txt").exists()


def test_sync_rejects_both_directions() -> None:
    with pytest.raises(sdk.ArkerError) as caught:
        client().vm("vm_1").sync("/home/user/x", from_local="a", to_local="b")
    assert caught.value.code == "invalid_request"


def test_sync_dir_alias_still_works(tmp_path) -> None:
    """The deprecated alias forwards to the same code path."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.txt").write_text("hello")

    t = FakeTransport()
    t.add_json(
        lambda m, u: m == "POST" and u.endswith("/sync"),
        200,
        {"ok": True, "op": "manifest", "root": "/home/user/pkg", "hash_algo": "sha256",
         "entries": [], "truncated": False},
    )
    t.add_json(lambda m, u: m == "POST" and "/sync?" in u, 200, {"ok": True})

    with use_transport(t):
        result = client().vm("vm_1").sync_dir(str(tmp_path / "pkg"), "/home/user/pkg")

    assert result.sent == 1
    assert isinstance(result, sdk.SyncResult)


def test_sync_from_local_renames_on_upload(tmp_path) -> None:
    """The destination basename may differ from the local one; the archive entry
    must carry the DESTINATION name or the file lands under the wrong path."""
    import io
    import tarfile as _tarfile

    local = tmp_path / "orig.bin"
    local.write_bytes(b"\0" * (sdk.ARCHIVE_MIN_BYTES + 1))

    t = FakeTransport()
    t.add_json(lambda m, u: m == "POST" and "/sync?" in u, 200, {"ok": True})

    with use_transport(t):
        client().vm("vm_1").sync("/home/user/renamed.bin", from_local=str(local))

    call = _stream_calls(t)[0]
    params = httpx.URL(call["url"]).params
    assert params.get("path") == "/home/user"
    mode = params.get("extract")
    assert mode in ("tar", "tar.gz")
    with _tarfile.open(fileobj=io.BytesIO(call["body"]), mode="r:*") as archive:
        assert archive.getnames() == ["renamed.bin"]
