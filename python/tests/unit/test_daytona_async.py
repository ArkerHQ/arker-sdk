import asyncio
import types

import pytest
from async_lifecycle_helpers import cancel_creation, run_async
from async_lifecycle_helpers import configure_router as configure_router
from async_lifecycle_helpers import late_arker_creation as late_arker_creation

import arker.daytona as shim


@run_async
async def test_async_daytona_arker_route_does_not_initialize_native_client(configure_router, monkeypatch):
    native = pytest.importorskip("daytona")
    configure_router(["arker"])
    calls = []

    class Native:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Native client must not initialize for Arker-only routing")

    monkeypatch.setattr(native, "AsyncDaytona", Native)
    monkeypatch.setattr(
        shim,
        "arker_create",
        lambda *args, **kwargs: types.SimpleNamespace(id="arker", delete=lambda: calls.append("delete")),
    )
    monkeypatch.setattr(
        shim, "run_command", lambda *args, **kwargs: types.SimpleNamespace(stdout="async", stderr="", exit_code=0)
    )

    async with shim.AsyncDaytona() as client:
        sandbox = await client.create()
        assert sandbox.id == "arker"
        assert (await sandbox.process.exec("printf async")).result == "async"
        await client.delete(sandbox)

    assert calls == ["delete"]


@run_async
async def test_async_daytona_native_objects_stay_on_original_loop(configure_router, monkeypatch):
    native = pytest.importorskip("daytona")
    configure_router(["daytona"])
    calls = []

    loop = asyncio.get_running_loop()

    async def execute(command):
        assert asyncio.get_running_loop() is loop
        return types.SimpleNamespace(result=command)

    async def delete(*args, **kwargs):
        assert asyncio.get_running_loop() is loop
        calls.append((args, kwargs))

    box = types.SimpleNamespace(id="native", process=types.SimpleNamespace(exec=execute), delete=delete)

    class Native:
        def __init__(self):
            assert asyncio.get_running_loop() is loop
            calls.append("initialize")

        async def create(self, *args, **kwargs):
            assert asyncio.get_running_loop() is loop
            return box

        async def get(self, identifier):
            assert asyncio.get_running_loop() is loop
            assert identifier == "native"
            return box

        async def close(self):
            assert asyncio.get_running_loop() is loop
            calls.append("close")

    monkeypatch.setattr(native, "AsyncDaytona", Native)
    async with shim.AsyncDaytona() as client:
        sandbox = await client.create()
        assert sandbox.process is box.process
        assert (await sandbox.process.exec("unchanged")).result == "unchanged"
        connected = await client.get("native")
        await client.delete(connected, timeout=10, wait=True)

    assert calls == ["initialize", ((10,), {"wait": True}), "close"]


@run_async
async def test_cancelled_daytona_creation_cleans_up_its_late_arker_result(configure_router, late_arker_creation):
    configure_router(["arker"])
    check = late_arker_creation(shim)

    async with shim.AsyncDaytona() as client:
        await check(client.create())


@run_async
async def test_cancelled_daytona_native_creation_does_not_fall_through(configure_router, monkeypatch):
    native = pytest.importorskip("daytona")
    configure_router(["daytona", "arker"])
    calls = []
    monkeypatch.setattr(shim, "arker_create", lambda *args, **kwargs: calls.append("duplicate"))

    entered, cancelled = asyncio.Event(), asyncio.Event()

    class Native:
        async def create(self):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def close(self):
            calls.append("close")

    monkeypatch.setattr(native, "AsyncDaytona", Native)
    async with shim.AsyncDaytona() as client:
        await cancel_creation(client.create(), entered, cancelled)

    assert calls == ["close"]


@run_async
async def test_closed_daytona_client_cannot_reinitialize_a_native_client():
    client = shim.AsyncDaytona()
    await client.close()
    await client.close()
    with pytest.raises(RuntimeError, match="closed"):
        await client.create()


def test_arker_daytona_execution_returns_native_result_type_and_artifacts(monkeypatch):
    ExecuteResponse = pytest.importorskip("daytona.common.process").ExecuteResponse

    monkeypatch.setattr(
        shim, "run_command", lambda *args, **kwargs: types.SimpleNamespace(stdout="output", stderr="", exit_code=17)
    )
    result = shim.Process(object()).exec("printf output; exit 17")
    assert isinstance(result, ExecuteResponse)
    assert result.artifacts.stdout == "output"
    assert result.artifacts.charts == []
