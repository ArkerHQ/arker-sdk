"""Lifecycle transport overrides stay scoped and never replay timed-out writes."""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
from e2b import TimeoutException
from test_computer import FakeTransport, client, use_transport

from arker._compat_e2b_lifecycle import AsyncLifecycle, Lifecycle, _options
from arker.computer import Arker, ArkerError


@pytest.mark.parametrize("timeout,expected", [(None, 60), (0, None), (0.25, 0.25)])
def test_timeout_normalization(timeout, expected):
    assert _options({"request_timeout": timeout}) == {"request_timeout": expected}


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan"), True, "1"])
def test_invalid_timeout_rejected_before_request(timeout):
    from e2b import InvalidArgumentException

    with pytest.raises(InvalidArgumentException):
        Lifecycle(vm=object()).set_timeout(5, request_timeout=timeout)


def test_timeout_budget_preserves_disabled_retry_and_other_client_defaults():
    transport = FakeTransport()
    transport.add_json(lambda *a: True, 503, {"error": {"code": "unavailable", "message": "busy"}})
    with use_transport(transport), pytest.raises(ArkerError):
        client()._request("GET", "/probe", request_timeout=1)
    assert len(transport.calls) == 1


@pytest.fixture
def delayed_server():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            calls.append(self.command)
            time.sleep(0.12)
            payload = json.dumps(
                {
                    "vm_id": "vm-a",
                    "state": "idle",
                    "mode": None,
                    "customer_paused": False,
                    "deadline_at": "2026-01-01T00:00:00Z",
                }
            ).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield (
            Arker(api_key="synthetic", base_url=f"http://127.0.0.1:{server.server_port}", retry=False).vm("vm-a"),
            calls,
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_http_timeout_and_disabled_timeout(delayed_server, asynchronous):
    vm, calls = delayed_server
    helper = (AsyncLifecycle if asynchronous else Lifecycle)(vm=vm)

    def invoke(method, *args, **kwargs):
        result = getattr(helper, method)(*args, **kwargs)
        return asyncio.run(result) if asynchronous else result

    with pytest.raises(TimeoutException):
        invoke("is_running", request_timeout=0.02)
    with pytest.raises(httpx.TimeoutException):
        invoke("set_timeout", 10, request_timeout=0.02)
    assert invoke("is_running", request_timeout=0) is True
    assert calls == ["GET", "POST", "GET"]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("method,args", [("pause", ()), ("beta_pause", ()), ("set_timeout", (20,))])
def test_static_lifecycle_routes_owner_without_bootstrap(monkeypatch, asynchronous, method, args):
    import arker.e2b as shim

    calls = []
    vm = SimpleNamespace(id="vm-a", base_url="https://example.invalid")

    def request(verb, path, body=None, **opts):
        calls.append((verb, path, opts))
        if path.endswith("/lifecycle"):
            return {"vm_id": vm.id, "state": "idle", "mode": None, "customer_paused": False}
        if path.endswith("/deadline"):
            return {"vm_id": vm.id, "deadline_at": "2026-01-01T00:00:00Z"}
        return {"vm_id": vm.id, "state": "paused", "mode": "full", "already_paused": False}

    vm._client = SimpleNamespace(_request=request)
    monkeypatch.setattr(shim.Sandbox, "_lookup", classmethod(lambda cls, identifier, opts: (("arker", vm), None)))
    monkeypatch.setattr(shim.Sandbox, "_prepare_data_plane", lambda *a, **k: pytest.fail("must not bootstrap"))
    value = getattr(shim.AsyncSandbox if asynchronous else shim.Sandbox, method)(vm.id, *args, request_timeout=0.5)
    if asynchronous:
        asyncio.run(value)
    assert calls and all(call[2]["request_timeout"] == 0.5 for call in calls)
    assert not any(path.endswith("/resume") for _, path, _ in calls)


def test_inventory_query_reaches_control_plane_with_exact_contract():
    from urllib.parse import parse_qs, urlsplit

    transport = FakeTransport()
    transport.add_json(lambda *args: True, 200, {"vms": [], "next_cursor": None})
    with use_transport(transport):
        client().list_vms(started_after="2026-01-01T00:00:00+00:00", started_at_order="desc")
    assert parse_qs(urlsplit(transport.calls[0]["url"]).query) == {
        "started_after": ["2026-01-01T00:00:00+00:00"],
        "started_at_order": ["desc"],
    }


@pytest.mark.parametrize(
    "method,args,operation",
    [("pause", (), {"mode": "filesystem"}), ("beta_pause", (), {"keep_memory": True}), ("set_timeout", (42,), {})],
)
def test_native_static_lifecycle_preserves_api_options_and_operation_arguments(monkeypatch, method, args, operation):
    import arker.e2b as shim

    calls = []
    api = {"api_key": "synthetic-vendor", "request_timeout": 0, "domain": "vendor.invalid"}
    native = SimpleNamespace(get_info=lambda identifier, **opts: calls.append(("probe", opts)) or object())
    setattr(native, method, lambda identifier, *values, **opts: calls.append((method, values, opts)) or True)
    monkeypatch.setattr(shim.Sandbox, "_native_sandbox", staticmethod(lambda: native))
    monkeypatch.setattr(shim, "routed_get", lambda identifier, provider, config, handlers: ("e2b", handlers["e2b"]()))
    assert getattr(shim.Sandbox, method)("native-id", *args, **api, **operation) is True
    assert calls[0] == ("probe", api)
    assert calls[1][1] == args
    assert {key: calls[1][2][key] for key in api | operation} == api | operation


def test_kill_validates_options_before_disposal_and_uses_only_delete():
    import arker.e2b as shim
    from arker._compat import UnsupportedCompatibilityOption

    calls = []
    vm = SimpleNamespace(id="vm-a", delete=lambda **opts: calls.append(opts) or SimpleNamespace(deleted=True))
    sandbox = shim.Sandbox(vm=vm)
    sandbox._data_plane = SimpleNamespace(close=lambda: calls.append("close"))
    with pytest.raises(UnsupportedCompatibilityOption):
        sandbox.kill(unknown=True)
    assert calls == []
    assert sandbox.kill(request_timeout=0)
    assert calls == ["close", {"request_timeout": None}]


@pytest.mark.parametrize("name", ["TimeoutException", "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout"])
@pytest.mark.parametrize("with_request", [False, True])
def test_control_preserves_native_timeout_subclass_and_unread_request(name, with_request):
    import httpx2

    from arker._compat_e2b_lifecycle import _control

    class UnreadStream(httpx2.SyncByteStream):
        def __iter__(self):
            pytest.fail("Timeout translation must not consume the request stream")

    original = (
        httpx2.Request(
            "POST",
            "https://example.invalid/v1/vms/vm-a/deadline",
            headers={"x-test": "context"},
            extensions={"timeout": {"read": 0.5}},
            stream=UnreadStream(),
        )
        if with_request
        else None
    )
    error = getattr(httpx2, name)("request timed out", request=original)

    def fail():
        raise error

    with pytest.raises(getattr(httpx, name)) as raised:
        _control(fail)
    translated = raised.value
    assert type(translated) is getattr(httpx, name)
    assert str(translated) == str(error)
    assert translated.__cause__ is error
    if original is None:
        with pytest.raises(RuntimeError):
            _ = translated.request
    else:
        assert isinstance(translated.request, httpx.Request)
        assert translated.request.method == original.method
        assert str(translated.request.url) == str(original.url)
        assert translated.request.headers.raw == original.headers.raw
        assert translated.request.extensions == original.extensions
        assert translated.request.stream is original.stream


@pytest.mark.parametrize("asynchronous", [False, True])
def test_repeated_kill_preserves_authoritative_backend_deleted_boolean(asynchronous):
    import arker.e2b as shim

    exists = True
    calls = []

    def delete(**opts):
        nonlocal exists
        calls.append("delete")
        deleted, exists = exists, False
        return SimpleNamespace(deleted=deleted)

    sandbox = shim.Sandbox(vm=SimpleNamespace(id="vm-a", delete=delete))
    if asynchronous:

        async def check():
            current = shim.AsyncSandbox(sandbox)
            assert await current.kill() is True
            assert await current.kill() is False

        asyncio.run(check())
    else:
        assert sandbox.kill() is True
        assert sandbox.kill() is False
    assert calls == ["delete", "delete"]
