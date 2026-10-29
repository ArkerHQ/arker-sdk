"""Facade ownership and native component behavior across both Python interfaces."""

import asyncio
from types import SimpleNamespace

import pytest
from test_e2b_data_plane import servers as servers

from arker import _compat_e2b_bootstrap as bootstrap
from arker import e2b
from arker._compat_e2b_data_plane import EnvdDescriptor


@pytest.fixture
def facade_vm(monkeypatch, servers):
    endpoint, _behavior, received, _redirected = servers
    calls = []
    from test_e2b_urls import delegation_response

    def request(method, path, body, **kwargs):
        assert (method, path, body) == ("POST", "/v1/vms/owned/file-transfer-delegation", {})
        calls.append("delegate")
        return delegation_response()

    vm = SimpleNamespace(
        id="owned",
        base_url="https://api.example.test",
        _client=SimpleNamespace(_api_key="platform-test-key", _request=request),
    )
    vm.refresh = lambda **options: vm
    vm.delete = lambda **options: calls.append("delete") or SimpleNamespace(deleted=True)
    descriptor = EnvdDescriptor(endpoint, "0.10.2", "guest-test-token")

    def prepare(current):
        assert current is vm
        calls.append("bootstrap")
        return descriptor, {"ready": True}

    async def prepare_async(current):
        return prepare(current)

    monkeypatch.setattr(bootstrap, "bootstrap_envd", prepare)
    monkeypatch.setattr(bootstrap, "bootstrap_envd_async", prepare_async)
    monkeypatch.setattr(e2b, "routed_create", lambda *a, **k: ("arker", vm))
    return vm, calls, received


@pytest.mark.parametrize("asynchronous", [False, True])
def test_create_binds_native_components_without_vendor_control_plane(facade_vm, monkeypatch, asynchronous):
    import e2b as native

    _vm, calls, received = facade_vm
    monkeypatch.setattr(native.Sandbox, "create", lambda *a, **k: pytest.fail("vendor control plane"))

    async def check():
        sandbox = await e2b.AsyncSandbox.create()
        try:
            assert sandbox._sandbox._native is None
            assert sandbox.commands is sandbox._data_plane.commands
            assert sandbox.files is sandbox._data_plane.files
            assert sandbox.pty is sandbox._data_plane.pty
            assert sandbox.git is sandbox._data_plane.git
            from test_e2b_urls import verify_url

            verify_url(sandbox.download_url("~/file"))
            verify_url(sandbox.upload_url(""))
            with pytest.raises(native.SandboxNotRunningException):
                await sandbox.commands.run("true")
            with pytest.raises(native.SandboxNotRunningException):
                await sandbox.files.read("/file")
        finally:
            await sandbox.kill()

    if asynchronous:
        asyncio.run(check())
    else:
        sandbox = e2b.Sandbox.create()
        try:
            assert sandbox._native is None
            assert sandbox.commands is sandbox._data_plane.commands
            assert sandbox.files is sandbox._data_plane.files
            assert sandbox.pty is sandbox._data_plane.pty
            assert sandbox.git is sandbox._data_plane.git
            from test_e2b_urls import verify_url

            verify_url(sandbox.download_url("~/file"))
            verify_url(sandbox.upload_url(""))
            with pytest.raises(native.SandboxNotRunningException):
                sandbox.commands.run("true")
            with pytest.raises(native.SandboxNotRunningException):
                sandbox.files.read("/file")
        finally:
            sandbox.kill()
    assert calls == ["bootstrap", "delegate", "delete"]
    assert len(received) == 2


@pytest.mark.parametrize("asynchronous", [False, True])
def test_bootstrap_failure_deletes_owned_created_vm(facade_vm, monkeypatch, asynchronous):
    _, calls, _ = facade_vm

    def fail(vm):
        raise RuntimeError("bootstrap failed")

    async def afail(vm):
        fail(vm)

    monkeypatch.setattr(bootstrap, "bootstrap_envd", fail)
    monkeypatch.setattr(bootstrap, "bootstrap_envd_async", afail)
    with pytest.raises(RuntimeError, match="bootstrap failed"):
        if asynchronous:
            asyncio.run(e2b.AsyncSandbox.create())
        else:
            e2b.Sandbox.create()
    assert calls == ["delete"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_delegation_failure_deletes_created_vm_before_client_allocation(facade_vm, monkeypatch, asynchronous):
    from arker import _compat_e2b_data_plane as data_plane

    vm, calls, _ = facade_vm

    def fail(*args, **kwargs):
        calls.append("delegate")
        raise RuntimeError("delegation unavailable")

    vm._client._request = fail
    monkeypatch.setattr(data_plane, "create_data_plane", lambda *a, **kw: pytest.fail("allocated before delegation"))
    with pytest.raises(RuntimeError, match="delegation unavailable"):
        if asynchronous:
            asyncio.run(e2b.AsyncSandbox.create())
        else:
            e2b.Sandbox.create()
    assert calls == ["bootstrap", "delegate", "delete"]
