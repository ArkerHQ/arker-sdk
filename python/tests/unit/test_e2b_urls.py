import base64
import hashlib
import hmac
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from arker._compat_e2b_data_plane import EnvdDescriptor
from arker._compat_e2b_urls import E2BFileURLs, prepare_file_urls

TOKEN = "test-envd-token-not-a-platform-key"
DESCRIPTOR = EnvdDescriptor("https://sandbox.example.test", "0.10.2", TOKEN)


def fixture():
    calls = []

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return delegation_response()

    vm = SimpleNamespace(id="name /?", base_url="https://example.test/api", _client=SimpleNamespace(_request=request))
    prepare_file_urls(vm)
    calls.clear()
    return E2BFileURLs(vm, descriptor=DESCRIPTOR), calls


@pytest.mark.parametrize(
    "path", ["../a", "~/a", "/tmp/../a", "link/../a", "a?user=root&path=/secret", "", "~other/file"]
)
@pytest.mark.parametrize("user", [None, "root", "image-user"])
@pytest.mark.parametrize("expiry", [None, 0, 86401, 4294967295])
def test_signature_matches_installed_native_without_rewriting(path, user, expiry, monkeypatch):
    from e2b.connection_config import ConnectionConfig
    from e2b.sandbox.main import SandboxBase
    from packaging.version import Version

    monkeypatch.setattr("time.time", lambda: 1800000000)
    native = SandboxBase("test", Version("0.10.2"), TOKEN, "example.test", ConnectionConfig())
    expected = parse_qs(urlsplit(native.upload_url(path, user=user, use_signature_expiration=expiry)).query)
    urls, calls = fixture()
    result = urls.upload_url(path, user=user, use_signature_expiration=expiry)
    body = verify_url(result)
    assert body["path"] == path
    assert body["envd_signature"] == expected["signature"][0]
    assert body.get("user") == user
    assert body.get("expires_at") == (None if expiry is None else 1800000000 + expiry)
    assert TOKEN not in str(calls) and TOKEN not in result
    assert calls == []


@pytest.mark.parametrize("expiry", [-1, True, 1.2, float("inf"), 4294967296])
def test_invalid_expiry_never_mints(expiry):
    urls, calls = fixture()
    with pytest.raises(ValueError):
        urls.download_url("a", use_signature_expiration=expiry)
    assert not calls


@pytest.mark.parametrize("path,user", [("", None), ("a\0b", None), ("a", "bad:user")])
def test_invalid_read_scope_never_mints(path, user):
    urls, calls = fixture()
    with pytest.raises(ValueError):
        urls.download_url(path, user=user)
    assert not calls


def test_missing_bootstrap_fails_explicitly():
    with pytest.raises(RuntimeError, match="initialized"):
        E2BFileURLs().download_url("a")


def test_native_dispatch_and_python_required_path():
    native = SimpleNamespace(upload_url=lambda *a, **kw: "native")
    assert E2BFileURLs(native=native).upload_url("a", user="custom") == "native"
    with pytest.raises(TypeError):
        E2BFileURLs(native=native).upload_url()


def test_public_sync_and_async_url_methods_share_retained_descriptor():
    import inspect

    from arker._compat_e2b_async import AsyncSandbox
    from arker.e2b import Sandbox

    urls, calls = fixture()
    sandbox = Sandbox(vm=urls.vm)
    sandbox._envd_descriptor = DESCRIPTOR
    assert not inspect.iscoroutinefunction(AsyncSandbox.download_url)
    bodies = [
        verify_url(sandbox.download_url("~/file", user="image-user")),
        verify_url(AsyncSandbox(sandbox).upload_url("", user="image-user")),
    ]
    assert calls == []
    assert [(body["operation"], body["path"]) for body in bodies] == [("read", "~/file"), ("write", "")]
    assert all(body["user"] == "image-user" and "envd_signature" in body for body in bodies)


def delegation_response(**updates):
    return {
        "redemption_url": "https://regional.example.test/api/v1/vms/canonical/file-transfer",
        "context": "b3BhcXVl",
        "signing_key": base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("="),
        "operations": ["read", "write"],
        "expires_at": None,
        **updates,
    }


def verify_url(url):
    assert urlsplit(url).netloc == "regional.example.test"
    ticket = parse_qs(urlsplit(url).query)["ticket"][0]
    context, payload, tag = ticket.removeprefix("ark_file_v1_").split(".")
    assert context == "b3BhcXVl"
    message = b"arker:file-transfer:v1\0" + f"{context}.{payload}".encode()
    expected = hmac.new(bytes(range(32)), message, hashlib.sha256).digest()
    assert base64.urlsafe_b64decode(tag + "=" * (-len(tag) % 4)) == expected
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def test_preparation_is_only_network_request_and_missing_preparation_never_fetches():
    urls, calls = fixture()
    del urls.vm._e2b_file_delegation
    with pytest.raises(RuntimeError, match="prepared"):
        urls.download_url("a")
    assert calls == []
    prepare_file_urls(urls.vm)
    assert len(calls) == 1
    assert calls[0][0] == ("POST", "/v1/vms/name%20%2F%3F/file-transfer-delegation", {})
    calls.clear()
    verify_url(urls.download_url("a"))
    verify_url(urls.upload_url("b"))
    assert calls == []
    assert delegation_response()["signing_key"] not in repr(urls.vm)


@pytest.mark.parametrize(
    "updates",
    [
        {"redemption_url": "http://regional.example.test/files"},
        {"redemption_url": "https://user:password@regional.example.test/files"},
        {"redemption_url": "https://regional.example.test/files?"},
        {"redemption_url": "https://regional.example.test/files#"},
        {"context": "abc="},
        {"signing_key": "a"},
        {"operations": []},
        {"operations": ["read", "read"]},
        {"expires_at": True},
    ],
)
def test_invalid_delegation_response_is_not_installed(updates):
    vm = SimpleNamespace(
        id="vm",
        base_url="https://example.test",
        _client=SimpleNamespace(_request=lambda *a, **kw: delegation_response(**updates)),
    )
    with pytest.raises((ValueError, TypeError)):
        prepare_file_urls(vm)
    assert not hasattr(vm, "_e2b_file_delegation")


def test_action_and_finite_cap_checked_locally(monkeypatch):
    monkeypatch.setattr("time.time", lambda: 100)
    urls, calls = fixture()
    urls.vm._client._request = lambda *a, **kw: delegation_response(operations=["read"], expires_at=105)
    prepare_file_urls(urls.vm)
    assert verify_url(urls.download_url("a", use_signature_expiration=5))["expires_at"] == 105
    for expiry in [None, 6]:
        with pytest.raises(ValueError, match="lifetime"):
            urls.download_url("a", use_signature_expiration=expiry)
    with pytest.raises(ValueError, match="operation"):
        urls.upload_url("a", use_signature_expiration=1)
    monkeypatch.setattr("time.time", lambda: 106)
    with pytest.raises(ValueError, match="lifetime"):
        urls.download_url("a", use_signature_expiration=0)
    assert calls == []


def test_cancelled_async_preparation_does_not_install_late_authority():
    import asyncio
    import threading

    from arker._compat_e2b_urls import prepare_file_urls_async

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def request(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        finished.set()
        return delegation_response()

    vm = SimpleNamespace(id="vm", base_url="https://example.test", _client=SimpleNamespace(_request=request))

    async def check():
        task = asyncio.create_task(prepare_file_urls_async(vm))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 5)

    asyncio.run(check())
    assert not hasattr(vm, "_e2b_file_delegation")


def test_shared_wire_vector_matches_rust_verifier_fixture(monkeypatch):
    from pathlib import Path

    vector = json.loads(
        (Path(__file__).resolve().parents[3] / "compatibility/e2b-file-delegation-vector.json").read_text()
    )
    monkeypatch.setattr("time.time", lambda: vector["now"])
    vm = SimpleNamespace(
        id="vm",
        base_url="https://api.example.test",
        _client=SimpleNamespace(_request=lambda *a, **kw: vector["response"]),
    )
    prepare_file_urls(vm)
    descriptor = EnvdDescriptor("https://guest.example.test", "0.10.2", vector["envd_token"])
    url = E2BFileURLs(vm, descriptor=descriptor).download_url(
        vector["path"], user=vector["user"], use_signature_expiration=vector["expiry"]
    )
    assert parse_qs(urlsplit(url).query)["ticket"][0] == vector["ticket"]
