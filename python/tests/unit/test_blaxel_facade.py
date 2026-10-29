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


@pytest.mark.parametrize("atomic", [False, True])
def test_async_cancelled_creation_returns_before_completion_and_disposes_without_delete(monkeypatch, atomic):
    import asyncio
    import threading

    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def create(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(
            _data_plane=SimpleNamespace(close=closed.set),
            delete=lambda: pytest.fail("cancelled creation deleted remote sandbox"),
        )

    monkeypatch.setattr(blaxel.SyncSandboxInstance, "create", create)

    async def run():
        method = blaxel.SandboxInstance.create_if_not_exists if atomic else blaxel.SandboxInstance.create
        task = asyncio.create_task(method())
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert not release.is_set()
        finally:
            release.set()
        assert await asyncio.to_thread(closed.wait, 5)

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


def test_async_cancel_during_wrap_preserves_resource_and_closes_sync_pool(monkeypatch):
    import asyncio

    closed = []
    created = SimpleNamespace(
        _data_plane=SimpleNamespace(close=lambda: closed.append(True)),
        delete=lambda: pytest.fail("cancelled wrapping deleted remote sandbox"),
    )
    monkeypatch.setattr(blaxel.SyncSandboxInstance, "create", lambda *args, **kwargs: created)

    async def wrap(cls, sandbox):
        raise asyncio.CancelledError

    monkeypatch.setattr(blaxel.SandboxInstance, "_wrap", classmethod(wrap))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(blaxel.SandboxInstance.create())
    assert closed == [True]


def test_async_native_create_uses_native_coroutine_and_preserves_result(monkeypatch):
    import asyncio

    calls = []

    class Native:
        def __init__(self):
            self.metadata = SimpleNamespace(name="test")
            self.process, self.fs, self.network = object(), object(), object()

        @classmethod
        async def create(cls, config, **kwargs):
            calls.append((config, kwargs, asyncio.get_running_loop()))
            return cls()

        @classmethod
        async def get(cls, name):
            pytest.fail("returned native instance should be reused")

    monkeypatch.setattr(blaxel, "import_optional", lambda name: SimpleNamespace(SandboxInstance=Native))
    monkeypatch.setattr(blaxel, "routed_create", lambda name, config, handlers: ("blaxel", handlers["blaxel"]()))

    async def run():
        config = {"name": "test"}
        result = await blaxel.SandboxInstance.create(config)
        assert calls == [(config, {}, asyncio.get_running_loop())]
        assert isinstance(result._native, Native)
        assert result.fs is result._native.fs

    asyncio.run(run())


def test_cancelled_creation_preserves_remote_after_late_bootstrap_failure(monkeypatch):
    import asyncio
    import threading

    entered, release, failed = threading.Event(), threading.Event(), threading.Event()
    deleted = []
    vm = SimpleNamespace(id="vm-test", name="test", delete=lambda: deleted.append(True))
    monkeypatch.setattr(blaxel, "routed_create", lambda *args: ("arker", vm))

    def fail(*args):
        entered.set()
        assert release.wait(5)
        failed.set()
        raise RuntimeError("late bootstrap failure")

    monkeypatch.setattr(bootstrap, "bootstrap_blaxel", fail)

    async def run():
        task = asyncio.create_task(blaxel.SandboxInstance.create())
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.to_thread(failed.wait, 5)

    try:
        asyncio.run(run())
    finally:
        release.set()
    assert deleted == []


def test_cancel_before_provider_dispatch_does_not_start_creation(monkeypatch):
    import asyncio
    import threading

    entered, release, dispatched = threading.Event(), threading.Event(), threading.Event()
    forks = []

    def routed(name, config, handlers):
        entered.set()
        assert release.wait(5)
        try:
            return "arker", handlers["arker"]()
        finally:
            dispatched.set()

    monkeypatch.setattr(blaxel, "routed_create", routed)
    monkeypatch.setattr(adapter, "validate_native", lambda: None)
    monkeypatch.setattr(blaxel, "arker_create", lambda *args, **kwargs: forks.append(True))

    async def run():
        task = asyncio.create_task(blaxel.SandboxInstance.create())
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.to_thread(dispatched.wait, 5)

    try:
        asyncio.run(run())
    finally:
        release.set()
    assert forks == []


@pytest.mark.parametrize("wrapping_fails", [False, True])
def test_async_native_result_cleanup_does_not_reuse_expired_creation_bridge(monkeypatch, wrapping_fails):
    import asyncio

    deleted = []
    failure = RuntimeError("wrapper failed")

    class Native:
        def __init__(self):
            self.metadata = SimpleNamespace(name="test")
            self.process, self.fs, self.network = object(), object(), object()

        @classmethod
        async def create(cls, config, **kwargs):
            return cls()

        async def delete(self):
            deleted.append(asyncio.get_running_loop())

    monkeypatch.setattr(blaxel, "import_optional", lambda name: SimpleNamespace(SandboxInstance=Native))
    monkeypatch.setattr(blaxel, "routed_create", lambda name, config, handlers: ("blaxel", handlers["blaxel"]()))
    if wrapping_fails:

        async def fail(cls, created):
            raise failure

        monkeypatch.setattr(blaxel.SandboxInstance, "_wrap", classmethod(fail))

    async def run():
        if wrapping_fails:
            with pytest.raises(RuntimeError) as error:
                await blaxel.SandboxInstance.create({"name": "test"})
            assert error.value is failure
            assert not hasattr(error.value, "cleanup_error")
        else:
            box = await blaxel.SandboxInstance.create({"name": "test"})
            await box.delete()
        assert deleted == [asyncio.get_running_loop()]

    asyncio.run(run())


def test_cancelled_creation_closes_pool_after_origin_loop_shutdown(monkeypatch):
    import asyncio
    import threading

    entered, release, finished, closed = (threading.Event() for _ in range(4))

    def create(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        finished.set()
        return SimpleNamespace(_data_plane=SimpleNamespace(close=closed.set))

    monkeypatch.setattr(blaxel.SyncSandboxInstance, "create", create)
    loop = asyncio.new_event_loop()

    async def cancel():
        task = asyncio.create_task(blaxel.SandboxInstance.create())
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Match loop teardown: cancel the shielded to_thread task too.
        remaining = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)

    try:
        loop.run_until_complete(cancel())
    finally:
        loop.close()
        release.set()
    assert finished.wait(5)
    assert closed.wait(2)
