from types import SimpleNamespace

import pytest

from arker import _compat_blaxel_bootstrap as bootstrap
from arker import _compat_blaxel_data_plane as adapter
from arker import blaxel


@pytest.mark.parametrize("failure", ["bootstrap", "plane"])
def test_creation_failure_deletes_only_new_vm(monkeypatch, failure):
    deleted = []
    vm = SimpleNamespace(id="vm-test", name="test", delete=lambda: deleted.append(True))
    monkeypatch.setattr(blaxel, "routed_create", lambda *args: ("arker", vm))

    def fail(*args, **kwargs):
        raise RuntimeError("initialization failed")

    monkeypatch.setattr(bootstrap, "bootstrap_blaxel", fail if failure == "bootstrap" else lambda *args: None)
    if failure == "plane":
        monkeypatch.setattr(adapter, "BlaxelDataPlane", fail)
    with pytest.raises(RuntimeError, match="initialization failed"):
        blaxel.SyncSandboxInstance.create()
    assert deleted == [True]


def test_native_preflight_precedes_fork(monkeypatch):
    def fail():
        raise RuntimeError("native source changed")

    monkeypatch.setattr(adapter, "validate_native", fail)
    monkeypatch.setattr(blaxel, "routed_create", lambda _name, _config, handlers: handlers["arker"]())
    monkeypatch.setattr(blaxel, "arker_create", lambda *args, **kwargs: pytest.fail("fork attempted"))
    with pytest.raises(RuntimeError, match="native source changed"):
        blaxel.SyncSandboxInstance.create()


def test_reconnect_failure_never_deletes_existing_vm(monkeypatch):
    vm = SimpleNamespace(id="vm-test", name="test", delete=lambda: pytest.fail("existing VM deleted"))
    monkeypatch.setattr(blaxel, "routed_get", lambda *args: ("arker", vm))

    def fail(*args):
        raise RuntimeError("paused")

    monkeypatch.setattr(bootstrap, "bootstrap_blaxel", fail)
    with pytest.raises(RuntimeError, match="paused"):
        blaxel.SyncSandboxInstance.get(vm.id)


def test_native_components_and_delete_cleanup(monkeypatch):
    closed = []
    plane = SimpleNamespace(fs=object(), process=object(), close=lambda: closed.append(True))
    monkeypatch.setattr(bootstrap, "bootstrap_blaxel", lambda *args: None)
    monkeypatch.setattr(adapter, "BlaxelDataPlane", lambda *args: plane)
    vm = SimpleNamespace(id="vm-test", name="test", delete=lambda: None)
    sandbox = blaxel.SyncSandboxInstance(vm=vm)
    assert sandbox.fs is plane.fs and sandbox.process is plane.process
    sandbox.delete()
    assert closed == [True]


def test_async_cancelled_creation_drains_and_deletes_owned_result(monkeypatch):
    import asyncio
    import threading

    entered, release = threading.Event(), threading.Event()
    deleted = []

    def create(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(delete=lambda: deleted.append(True))

    monkeypatch.setattr(blaxel.SyncSandboxInstance, "create", create)

    async def run():
        task = asyncio.create_task(blaxel.SandboxInstance.create())
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert deleted == [True]

    try:
        asyncio.run(run())
    finally:
        release.set()


@pytest.mark.parametrize("method", ["create", "create_if_not_exists", "get"])
def test_async_wrap_failure_preserves_possibly_existing_sandbox(monkeypatch, method):
    import asyncio

    existing = SimpleNamespace(delete=lambda: pytest.fail("existing sandbox deleted"))
    monkeypatch.setattr(blaxel.SyncSandboxInstance, "create", lambda *args, **kwargs: existing)
    monkeypatch.setattr(blaxel.SyncSandboxInstance, "get", lambda *args, **kwargs: existing)

    async def fail(cls, sandbox):
        raise RuntimeError("wrapper failed")

    monkeypatch.setattr(blaxel.SandboxInstance, "_wrap", classmethod(fail))
    kwargs = {"create_if_not_exist": True} if method == "create" else {}
    with pytest.raises(RuntimeError, match="wrapper failed"):
        asyncio.run(getattr(blaxel.SandboxInstance, method)("existing", **kwargs))


def test_async_wrap_failure_deletes_owned_creation(monkeypatch):
    import asyncio

    deleted = []
    monkeypatch.setattr(
        blaxel.SyncSandboxInstance,
        "create",
        lambda *args, **kwargs: SimpleNamespace(delete=lambda: deleted.append(True)),
    )

    async def fail(cls, sandbox):
        raise RuntimeError("wrapper failed")

    monkeypatch.setattr(blaxel.SandboxInstance, "_wrap", classmethod(fail))
    with pytest.raises(RuntimeError, match="wrapper failed"):
        asyncio.run(blaxel.SandboxInstance.create())
    assert deleted == [True]


def test_async_cancelled_atomic_creation_preserves_unproven_ownership(monkeypatch):
    import asyncio
    import threading

    entered, release = threading.Event(), threading.Event()

    def create(*args, **kwargs):
        assert kwargs["create_if_not_exist"] is True
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(delete=lambda: pytest.fail("possibly existing sandbox deleted"))

    monkeypatch.setattr(blaxel.SyncSandboxInstance, "create", create)

    async def run():
        task = asyncio.create_task(blaxel.SandboxInstance.create_if_not_exists("existing"))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(run())
    finally:
        release.set()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("staging_cleanup_failed", [False, True])
def test_failed_creation_preserves_cleanup_error_and_resource_id(monkeypatch, asynchronous, staging_cleanup_failed):
    import asyncio

    primary, cleanup = RuntimeError("initialization failed"), RuntimeError("delete failed")
    staging_error = RuntimeError("staging cleanup failed")
    if staging_cleanup_failed:
        primary.cleanup_error = staging_error

    def fail_delete():
        raise cleanup

    vm = SimpleNamespace(id="vm-test", name="test", delete=fail_delete)
    monkeypatch.setattr(blaxel, "routed_create", lambda *args: ("arker", vm))

    def fail(*args, **kwargs):
        raise primary

    if asynchronous:
        created = SimpleNamespace(id=vm.id, delete=fail_delete)
        monkeypatch.setattr(blaxel.SyncSandboxInstance, "create", lambda *args, **kwargs: created)
        monkeypatch.setattr(blaxel.SandboxInstance, "_wrap", fail)
        invoke = lambda: asyncio.run(blaxel.SandboxInstance.create())
    else:
        monkeypatch.setattr(bootstrap, "bootstrap_blaxel", fail)
        invoke = blaxel.SyncSandboxInstance.create
    with pytest.raises(RuntimeError) as caught:
        invoke()
    assert caught.value is primary
    assert caught.value.cleanup_error is cleanup
    assert caught.value.cleanup_errors == ((staging_error, cleanup) if staging_cleanup_failed else (cleanup,))
    assert caught.value.sandbox_id == "vm-test"
