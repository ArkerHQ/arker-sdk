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
    assert [path for path, _ in received] == ["/process.Process/Start", "/files?path=%2Ffile"]


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


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stage", ["bootstrap", "delegation", "components"])
@pytest.mark.parametrize("delete_fails", [False, True])
def test_unmounted_creation_failure_preserves_errors_and_owned_id(
    facade_vm, monkeypatch, asynchronous, stage, delete_fails
):
    from arker import _compat_e2b_data_plane as data_plane

    vm, calls, _ = facade_vm
    preparation_error = ValueError("preparation failed")
    cleanup_error = OSError("delete failed")

    def fail(*args, **kwargs):
        raise preparation_error

    if stage == "bootstrap":
        monkeypatch.setattr(bootstrap, "bootstrap_envd", fail)
    elif stage == "delegation":
        vm._client._request = fail
    else:
        monkeypatch.setattr(data_plane, "create_data_plane", fail)

    def delete(**options):
        calls.append("delete")
        if delete_fails:
            raise cleanup_error
        return SimpleNamespace(deleted=True)

    vm.delete = delete
    with pytest.raises(RuntimeError if delete_fails else ValueError) as caught:
        if asynchronous:
            asyncio.run(e2b.AsyncSandbox.create())
        else:
            e2b.Sandbox.create()
    assert calls.count("delete") == 1
    if delete_fails:
        assert str(caught.value) == "Sandbox creation and cleanup failed for VM owned"
        assert caught.value.__cause__ is cleanup_error
        assert cleanup_error.__context__ is preparation_error
    else:
        assert caught.value is preparation_error


@pytest.mark.parametrize("delete_fails", [False, True])
def test_creation_close_fault_still_deletes_and_preserves_all_errors(facade_vm, monkeypatch, delete_fails):
    from arker import _compat_e2b_data_plane as data_plane

    vm, calls, _ = facade_vm
    preparation_error = ValueError("mount failed")
    close_error = OSError("close failed")
    delete_error = OSError("delete failed")

    def close():
        calls.append("close")
        raise close_error

    def attach(*args):
        calls.append("attach")
        raise preparation_error

    def delete(**options):
        calls.append("delete")
        if delete_fails:
            raise delete_error
        return SimpleNamespace(deleted=True)

    plane = SimpleNamespace(commands=object(), files=object(), git=object(), close=close)
    monkeypatch.setattr(data_plane, "create_data_plane", lambda *a, **k: plane)
    monkeypatch.setattr(e2b, "mount_store", lambda *a: SimpleNamespace(attach=attach))
    vm.delete = delete
    with pytest.raises(RuntimeError, match=r"^Sandbox creation and cleanup failed for VM owned$") as caught:
        e2b.Sandbox.create(volume_mounts={"/workspace": "data"})
    assert calls == ["bootstrap", "delegate", "attach", "close", "delete"]
    assert caught.value.__cause__ is (delete_error if delete_fails else close_error)
    if delete_fails:
        assert delete_error.__context__ is close_error
    assert close_error.__context__ is preparation_error


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("delete_fails", [False, True])
def test_kill_deletes_after_native_pool_close_fault(facade_vm, monkeypatch, asynchronous, delete_fails):
    vm, calls, _ = facade_vm
    close_error = OSError("close failed")
    delete_error = OSError("delete failed")

    def delete(**options):
        calls.append("delete")
        if delete_fails:
            raise delete_error
        return SimpleNamespace(deleted=True)

    async def check_async():
        sandbox = await e2b.AsyncSandbox.create()
        original = sandbox._data_plane.aclose

        async def close():
            await original()
            calls.append("close")
            raise close_error

        monkeypatch.setattr(sandbox._data_plane, "aclose", close)
        await sandbox.kill()

    vm.delete = delete
    with pytest.raises(OSError) as caught:
        if asynchronous:
            asyncio.run(check_async())
        else:
            sandbox = e2b.Sandbox.create()
            original = sandbox._data_plane.close

            def close():
                original()
                calls.append("close")
                raise close_error

            monkeypatch.setattr(sandbox._data_plane, "close", close)
            sandbox.kill()
    assert calls == ["bootstrap", "delegate", "close", "delete"]
    assert caught.value is (delete_error if delete_fails else close_error)
    if delete_fails:
        assert delete_error.__context__ is close_error
