from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from arker._compat_e2b_data_plane import EnvdDescriptor
from arker._compat_e2b_urls import E2BFileURLs

TOKEN = "test-envd-token-not-a-platform-key"
DESCRIPTOR = EnvdDescriptor("https://sandbox.example.test", "0.10.2", TOKEN)


def fixture():
    calls = []

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return {"vm_id": "canonical", "ticket": "ark_file_v1_scoped.signature", "expires_at": None}

    vm = SimpleNamespace(id="name /?", base_url="https://example.test/api", _client=SimpleNamespace(_request=request))
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
    body = calls[0][0][2]
    assert body["path"] == path
    assert body["envd_signature"] == expected["signature"][0]
    assert body.get("user") == user
    assert body.get("expires_at") == (None if expiry is None else 1800000000 + expiry)
    assert TOKEN not in str(calls) and TOKEN not in result
    assert parse_qs(urlsplit(result).query) == {"ticket": ["ark_file_v1_scoped.signature"]}
    assert calls[0][0][:2] == ("POST", "/v1/vms/name%20%2F%3F/file-transfer-urls")


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
    assert isinstance(sandbox.download_url("~/file", user="image-user"), str)
    assert isinstance(AsyncSandbox(sandbox).upload_url("", user="image-user"), str)
    bodies = [call[0][2] for call in calls]
    assert [(body["operation"], body["path"]) for body in bodies] == [("read", "~/file"), ("write", "")]
    assert all(body["user"] == "image-user" and "envd_signature" in body for body in bodies)
