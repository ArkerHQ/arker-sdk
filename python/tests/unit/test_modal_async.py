import asyncio
import inspect
import threading
from types import SimpleNamespace

import pytest

import arker.modal as shim
from arker._compat_process import GuestProcesses


def dual(async_method):
    def sync(*args, **kwargs):
        pytest.fail("native synchronous method must not run on the async route")

    sync.aio = async_method
    return sync


def test_dual_entrypoints_keep_signatures_and_native_identity(monkeypatch):
    async def run():
        loop = asyncio.get_running_loop()
        process = object()
        failure = RuntimeError("native failure")
        calls = []

        async def execute(*args, **kwargs):
            assert asyncio.get_running_loop() is loop
            calls.append((args, kwargs))
            if args == ("fail",):
                raise failure
            return process

        async def terminate(**kwargs):
            assert kwargs == {"wait": True}
            return 137

        native = SimpleNamespace(object_id="sb-owned", exec=dual(execute), terminate=dual(terminate))

        async def create(*args, **kwargs):
            assert args == ("sleep", "5") and kwargs == {"env": {"VALUE": "native"}}
            return native

        async def from_id(identifier, **kwargs):
            assert identifier == "sb-owned" and kwargs == {"client": client}
            return native

        api = SimpleNamespace(create=dual(create), from_id=dual(from_id))
        client = object()
        monkeypatch.setattr(shim.Sandbox, "_native_sandbox", staticmethod(lambda: api))
        monkeypatch.setattr(shim, "routed_create", lambda name, config, creators: ("modal", creators["modal"]()))
        monkeypatch.setattr(shim, "routed_get", lambda identifier, name, config, getters: ("modal", getters["modal"]()))
        sandbox = await shim.Sandbox.create.aio("sleep", "5", env={"VALUE": "native"})
        assert type(sandbox) is shim.Sandbox and sandbox._native is native
        reconnected = await shim.Sandbox.from_id.aio("sb-owned", client)
        assert reconnected._native is native
        options = {"workdir": "/tmp", "env": {"VALUE": None}, "text": False}
        assert await sandbox.exec.aio("cat", **options) is process
        assert await shim.Sandbox.exec.aio(sandbox, "cat", **options) is process
        assert calls == [(("cat",), options), (("cat",), options)]
        with pytest.raises(RuntimeError) as caught:
            await sandbox.exec.aio("fail")
        assert caught.value is failure
        assert await sandbox.terminate.aio(wait=True) == 137

    for name in ("create", "from_id", "exec", "terminate"):
        method = getattr(shim.Sandbox, name)
        assert inspect.signature(method) == inspect.signature(method.aio)
        assert inspect.iscoroutinefunction(method.aio)
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["create", "from_id", "terminate"])
def test_arker_cancelled_control_operation_finishes_without_deleting_lookup(monkeypatch, operation):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    deletes = []
    vm = SimpleNamespace(id="owned")

    def delete():
        if operation == "terminate":
            started.set()
            assert release.wait(5)
        deletes.append(vm.id)
        finished.set()

    vm.delete = delete

    def resolve(*args, **kwargs):
        started.set()
        assert release.wait(5)
        if operation == "from_id":
            finished.set()
        return "arker", vm

    monkeypatch.setattr(shim, "routed_create", resolve)
    monkeypatch.setattr(shim, "routed_get", resolve)

    async def run():
        if operation == "create":
            pending = shim.Sandbox.create.aio()
        elif operation == "from_id":
            pending = shim.Sandbox.from_id.aio("owned")
        else:
            pending = shim.Sandbox(vm=vm).terminate.aio()
        task = asyncio.create_task(pending)
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert deletes == []
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 5)

    asyncio.run(run())
    assert deletes == ([] if operation == "from_id" else ["owned"])


def test_cancelled_arker_exec_kills_only_its_late_process(monkeypatch):
    started, release, killed = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def spawn(self, request):
        assert request["argv"] == ["sleep", "60"]
        started.set()
        assert release.wait(5)
        return SimpleNamespace(pid="owned-process")

    def kill(self, identifier):
        calls.append(identifier)
        killed.set()

    monkeypatch.setattr(GuestProcesses, "exec", spawn)
    monkeypatch.setattr(GuestProcesses, "kill", kill)
    sandbox = shim.Sandbox(vm=SimpleNamespace(id="vm", delete=lambda: pytest.fail("must not delete sandbox")))

    async def run():
        pending = asyncio.create_task(sandbox.exec.aio("sleep", "60"))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert calls == []
        finally:
            release.set()
        assert await asyncio.to_thread(killed.wait, 5)

    asyncio.run(run())
    assert calls == ["owned-process"]


def test_cancelled_native_create_reclaims_late_native_handle(monkeypatch):
    async def run():
        entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        async def terminate():
            calls.append("terminate")
            cleaned.set()

        native = SimpleNamespace(object_id="sb-owned", terminate=dual(terminate))

        async def create():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                return native

        monkeypatch.setattr(shim.Sandbox, "_native_sandbox", staticmethod(lambda: SimpleNamespace(create=dual(create))))
        monkeypatch.setattr(shim, "routed_create", lambda name, config, creators: ("modal", creators["modal"]()))
        task = asyncio.create_task(shim.Sandbox.create.aio())
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.wait_for(cleaned.wait(), 5)
        assert calls == ["terminate"]

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["exec", "terminate"])
def test_native_instance_cancellation_reaches_original_coroutine(operation):
    async def run():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def native_call(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        sandbox = shim.Sandbox(native=SimpleNamespace(object_id="sb-owned", **{operation: dual(native_call)}))
        method = getattr(sandbox, operation)
        task = asyncio.create_task(method.aio(*(("sleep", "60") if operation == "exec" else ())))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()

    asyncio.run(run())
