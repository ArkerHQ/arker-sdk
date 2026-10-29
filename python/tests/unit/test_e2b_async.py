import asyncio
import types

import pytest
from async_lifecycle_helpers import cancel_creation, run_async
from async_lifecycle_helpers import configure_router as configure_router
from async_lifecycle_helpers import late_arker_creation as late_arker_creation
from e2b_facade_helpers import mock_envd_bootstrap as mock_envd_bootstrap

import arker.e2b as shim

pytestmark = pytest.mark.usefixtures("mock_envd_bootstrap")


@run_async
async def test_native_creation_that_suppresses_cancellation_is_not_leaked(configure_router, monkeypatch):
    native = pytest.importorskip("e2b")
    configure_router(["e2b"])

    entered, deleted = asyncio.Event(), asyncio.Event()

    async def kill():
        deleted.set()
        return True

    class Native:
        @classmethod
        async def create(cls, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return types.SimpleNamespace(sandbox_id="late-native", kill=kill)

    monkeypatch.setattr(native, "AsyncSandbox", Native)
    await cancel_creation(shim.AsyncSandbox.create(), entered, deleted)


def test_async_create_obeys_arker_only_routing_instead_of_bypassing_it(configure_router, monkeypatch):
    native = pytest.importorskip("e2b")
    calls = []

    class Native:
        @classmethod
        async def create(cls, *args, **kwargs):
            calls.append("native")
            return types.SimpleNamespace(sandbox_id="native")

    configure_router(["arker"])
    monkeypatch.setattr(native, "AsyncSandbox", Native)
    monkeypatch.setattr(
        shim, "arker_create", lambda *args, **kwargs: calls.append("arker") or types.SimpleNamespace(id="arker")
    )
    result = asyncio.run(shim.AsyncSandbox.create("ubuntu"))
    assert result.sandbox_id == "arker"
    assert calls == ["arker"]


@run_async
async def test_native_async_calls_stay_on_the_original_event_loop(configure_router, monkeypatch):
    native = pytest.importorskip("e2b")
    configure_router(["e2b"])
    calls = []

    loop = asyncio.get_running_loop()

    async def run(command, **kwargs):
        assert asyncio.get_running_loop() is loop
        calls.append((command, kwargs["envs"]))
        return types.SimpleNamespace(stdout="native")

    async def kill():
        assert asyncio.get_running_loop() is loop
        calls.append("kill")
        return True

    class Native:
        @classmethod
        async def create(cls, *args, **kwargs):
            assert asyncio.get_running_loop() is loop
            return types.SimpleNamespace(sandbox_id="native", commands=types.SimpleNamespace(run=run), kill=kill)

    monkeypatch.setattr(native, "AsyncSandbox", Native)
    async with await shim.AsyncSandbox.create() as sandbox:
        assert (await sandbox.commands.run("echo native", envs={"KEY": "value"})).stdout == "native"

    assert calls == [("echo native", {"KEY": "value"}), "kill"]


@pytest.mark.parametrize("cancellation", ["requested", "provider-raised"])
@run_async
async def test_cancelling_native_creation_does_not_fall_through(configure_router, monkeypatch, cancellation):
    native = pytest.importorskip("e2b")
    configure_router(["e2b", "arker"])
    calls = []
    monkeypatch.setattr(shim, "arker_create", lambda *args, **kwargs: calls.append("duplicate"))

    entered, cancelled = asyncio.Event(), asyncio.Event()

    class Native:
        @classmethod
        async def create(cls, *args, **kwargs):
            if cancellation == "provider-raised":
                raise asyncio.CancelledError
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    monkeypatch.setattr(native, "AsyncSandbox", Native)
    if cancellation == "requested":
        await cancel_creation(shim.AsyncSandbox.create(), entered, cancelled)
    else:
        with pytest.raises(asyncio.CancelledError):
            await shim.AsyncSandbox.create()

    assert calls == []


def test_cancelled_arker_creation_deletes_its_late_result(configure_router, late_arker_creation):
    configure_router(["arker"])
    asyncio.run(late_arker_creation(shim, boolean_delete=True)(shim.AsyncSandbox.create("ubuntu")))


def test_async_race_checks_metadata_miss_without_connecting(configure_router, monkeypatch):
    native = pytest.importorskip("e2b")
    configure_router(["e2b", "arker"], existing_strategy="race", create_order=["arker"])
    resumed = []

    def request(method, path, body, **kwargs):
        resumed.append((method, path, body))
        return {"vm_id": "arker", "state": "running", "mode": "full"}

    vm = types.SimpleNamespace(
        id="arker", base_url="https://worker.example", _client=types.SimpleNamespace(_request=request)
    )
    vm.refresh = lambda **options: vm
    monkeypatch.setattr(shim, "arker_get", lambda *args, **kwargs: vm)
    calls = []

    class Native:
        @classmethod
        async def get_info(cls, identifier):
            await asyncio.sleep(0)
            return

        @classmethod
        async def connect(cls, identifier):
            calls.append("connect")

    monkeypatch.setattr(native, "AsyncSandbox", Native)
    assert asyncio.run(shim.AsyncSandbox.connect("shared-name")).sandbox_id == "arker"
    assert calls == []
    assert resumed == [
        ("POST", "/v1/vms/arker/resume", {"deadline": {"policy_id": "computesdk-e2b-lifetime", "timeout_seconds": 300}})
    ]
