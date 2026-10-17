import asyncio
import json
import threading
import types

import pytest

import arker.daytona as shim


def router(tmp_path, monkeypatch, provider):
    path = tmp_path / "router.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "priority", "order": [provider]},
                "create": {"strategy": "priority", "order": [provider]},
            }
        )
    )
    monkeypatch.setenv("COMPUTESDK_ROUTER_CONFIG", str(path))
    monkeypatch.setenv("ARKER_API_KEY", "ark_test")


def test_async_daytona_arker_route_does_not_initialize_native_client(tmp_path, monkeypatch):
    native = pytest.importorskip("daytona")
    router(tmp_path, monkeypatch, "arker")
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

    async def scenario():
        async with shim.AsyncDaytona() as client:
            sandbox = await client.create()
            assert sandbox.id == "arker"
            assert (await sandbox.process.exec("printf async")).result == "async"
            await client.delete(sandbox)

    asyncio.run(scenario())
    assert calls == ["delete"]


def test_async_daytona_native_objects_stay_on_original_loop(tmp_path, monkeypatch):
    native = pytest.importorskip("daytona")
    router(tmp_path, monkeypatch, "daytona")
    calls = []

    async def scenario():
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

    asyncio.run(scenario())
    assert calls == ["initialize", ((10,), {"wait": True}), "close"]


def test_cancelled_daytona_creation_cleans_up_its_late_arker_result(tmp_path, monkeypatch):
    router(tmp_path, monkeypatch, "arker")
    entered, release, deleted = threading.Event(), threading.Event(), threading.Event()

    def create(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return types.SimpleNamespace(id="late", delete=deleted.set)

    monkeypatch.setattr(shim, "arker_create", create)

    async def scenario():
        async with shim.AsyncDaytona() as client:
            pending = asyncio.create_task(client.create())
            assert await asyncio.to_thread(entered.wait, 1)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            release.set()
            assert await asyncio.to_thread(deleted.wait, 1)

    asyncio.run(scenario())


def test_cancelled_daytona_native_creation_does_not_fall_through(tmp_path, monkeypatch):
    native = pytest.importorskip("daytona")
    path = tmp_path / "router.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "priority", "order": ["daytona", "arker"]},
                "create": {"strategy": "priority", "order": ["daytona", "arker"]},
            }
        )
    )
    monkeypatch.setenv("COMPUTESDK_ROUTER_CONFIG", str(path))
    monkeypatch.setenv("ARKER_API_KEY", "ark_test")
    calls = []
    monkeypatch.setattr(shim, "arker_create", lambda *args, **kwargs: calls.append("duplicate"))

    async def scenario():
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
            pending = asyncio.create_task(client.create())
            await asyncio.wait_for(entered.wait(), 1)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            await asyncio.wait_for(cancelled.wait(), 1)

    asyncio.run(scenario())
    assert calls == ["close"]


def test_closed_daytona_client_cannot_reinitialize_a_native_client():
    async def scenario():
        client = shim.AsyncDaytona()
        await client.close()
        await client.close()
        with pytest.raises(RuntimeError, match="closed"):
            await client.create()

    asyncio.run(scenario())


def test_arker_daytona_execution_returns_native_result_type_and_artifacts(monkeypatch):
    from daytona.common.process import ExecuteResponse

    monkeypatch.setattr(
        shim, "run_command", lambda *args, **kwargs: types.SimpleNamespace(stdout="output", stderr="", exit_code=17)
    )
    result = shim.Process(object()).exec("printf output; exit 17")
    assert isinstance(result, ExecuteResponse)
    assert result.artifacts.stdout == "output"
    assert result.artifacts.charts == []
