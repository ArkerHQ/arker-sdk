import asyncio
import threading
import types

import pytest

import arker.e2b as shim


@pytest.fixture(autouse=True)
def _valid_e2b_parent_identity(monkeypatch):
    monkeypatch.setattr(shim, "_validate_e2b_identity", lambda vm: None)


def test_native_creation_that_suppresses_cancellation_is_not_leaked(tmp_path, monkeypatch):
    native = pytest.importorskip("e2b")
    _router(tmp_path, monkeypatch, ["e2b"])

    async def scenario():
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
        pending = asyncio.create_task(shim.AsyncSandbox.create())
        await asyncio.wait_for(entered.wait(), 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await asyncio.wait_for(deleted.wait(), 1)

    asyncio.run(scenario())


def test_async_create_obeys_arker_only_routing_instead_of_bypassing_it(tmp_path, monkeypatch):
    native = pytest.importorskip("e2b")
    calls = []

    class Native:
        @classmethod
        async def create(cls, *args, **kwargs):
            calls.append("native")
            return types.SimpleNamespace(sandbox_id="native")

    config = tmp_path / "router.json"
    config.write_text(
        '{"version":1,"existing":{"strategy":"priority","order":["arker"]},"create":{"strategy":"priority","order":["arker"]}}'
    )
    monkeypatch.setenv("COMPUTESDK_ROUTER_CONFIG", str(config))
    monkeypatch.setenv("ARKER_API_KEY", "ark_test")
    monkeypatch.setattr(native, "AsyncSandbox", Native)
    monkeypatch.setattr(
        shim, "arker_create", lambda *args, **kwargs: calls.append("arker") or types.SimpleNamespace(id="arker")
    )
    result = asyncio.run(shim.AsyncSandbox.create())
    assert result.sandbox_id == "arker"
    assert calls == ["arker"]


def _router(tmp_path, monkeypatch, order):
    import json

    path = tmp_path / "router.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "priority", "order": order},
                "create": {"strategy": "priority", "order": order},
            }
        )
    )
    monkeypatch.setenv("COMPUTESDK_ROUTER_CONFIG", str(path))
    monkeypatch.setenv("ARKER_API_KEY", "ark_test")


def test_native_async_calls_stay_on_the_original_event_loop(tmp_path, monkeypatch):
    native = pytest.importorskip("e2b")
    _router(tmp_path, monkeypatch, ["e2b"])
    calls = []

    async def scenario():
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

    asyncio.run(scenario())
    assert calls == [("echo native", {"KEY": "value"}), "kill"]


def test_cancelling_native_creation_does_not_fall_through(tmp_path, monkeypatch):
    native = pytest.importorskip("e2b")
    _router(tmp_path, monkeypatch, ["e2b", "arker"])
    calls = []
    monkeypatch.setattr(shim, "arker_create", lambda *args, **kwargs: calls.append("duplicate"))

    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class Native:
            @classmethod
            async def create(cls, *args, **kwargs):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        monkeypatch.setattr(native, "AsyncSandbox", Native)
        creation = asyncio.create_task(shim.AsyncSandbox.create())
        await asyncio.wait_for(entered.wait(), 1)
        creation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await creation
        await asyncio.wait_for(cancelled.wait(), 1)

    asyncio.run(scenario())
    assert calls == []


def test_cancelled_arker_creation_deletes_its_late_result(tmp_path, monkeypatch):
    _router(tmp_path, monkeypatch, ["arker"])
    entered, release, deleted = threading.Event(), threading.Event(), threading.Event()
    vm = types.SimpleNamespace(
        id="late", refresh=lambda: None, delete=lambda: deleted.set() or types.SimpleNamespace(deleted=True)
    )

    def create(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return vm

    monkeypatch.setattr(shim, "arker_create", create)

    async def scenario():
        creation = asyncio.create_task(shim.AsyncSandbox.create())
        assert await asyncio.to_thread(entered.wait, 1)
        creation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await creation
        release.set()
        assert await asyncio.to_thread(deleted.wait, 1)

    asyncio.run(scenario())


def test_native_cancellation_is_not_reclassified_as_a_provider_error(tmp_path, monkeypatch):
    native = pytest.importorskip("e2b")
    _router(tmp_path, monkeypatch, ["e2b", "arker"])
    calls = []

    class Native:
        @classmethod
        async def create(cls, *args, **kwargs):
            raise asyncio.CancelledError

    monkeypatch.setattr(native, "AsyncSandbox", Native)
    monkeypatch.setattr(shim, "arker_create", lambda *args, **kwargs: calls.append("duplicate"))

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await shim.AsyncSandbox.create()

    asyncio.run(scenario())
    assert calls == []


def test_arker_async_handles_preserve_native_types_and_await_callbacks(monkeypatch):
    native = pytest.importorskip("e2b")
    calls = []
    current = types.SimpleNamespace(pid="42", status="completed", stdout="λ", stderr="failure", exit_code=17)

    class Processes:
        def __init__(self, vm):
            pass

        def exec(self, request):
            calls.append(request)
            return current

        def get(self, identifier, **kwargs):
            return current

        def write_stdin(self, identifier, data):
            calls.append(("stdin", data))

        def close_stdin(self, identifier):
            calls.append("eof")

    monkeypatch.setattr(shim, "GuestProcesses", Processes)

    async def scenario():
        sandbox = shim.AsyncSandbox(shim.Sandbox(vm=types.SimpleNamespace(id="vm")))
        output = []

        async def on_stdout(chunk):
            await asyncio.sleep(0)
            output.append(chunk)

        handle = await sandbox.commands.run("cat; exit 17", background=True, stdin=True, on_stdout=on_stdout)
        assert isinstance(handle, native.AsyncCommandHandle)
        await handle.send_stdin("hello")
        await handle.close_stdin()
        with pytest.raises(native.CommandExitException) as raised:
            await handle.wait()
        assert (raised.value.stdout, raised.value.stderr, raised.value.exit_code) == ("λ", "failure", 17)
        assert output == ["λ"]

    asyncio.run(scenario())
    assert ("stdin", "hello") in calls
    assert "eof" in calls


def test_async_race_checks_metadata_miss_without_connecting(tmp_path, monkeypatch):
    native = pytest.importorskip("e2b")
    path = tmp_path / "race.json"
    path.write_text(
        '{"version":1,"existing":{"strategy":"race","order":["e2b","arker"]},"create":{"strategy":"priority","order":["arker"]}}'
    )
    monkeypatch.setenv("COMPUTESDK_ROUTER_CONFIG", str(path))
    monkeypatch.setenv("ARKER_API_KEY", "ark_test")
    monkeypatch.setattr(shim, "arker_get", lambda *args, **kwargs: types.SimpleNamespace(id="arker"))
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


def test_async_observation_timeout_and_disconnect_do_not_kill(monkeypatch):
    native = pytest.importorskip("e2b")
    killed = []
    current = types.SimpleNamespace(pid="42", status="running", stdout="", stderr="", exit_code=0)

    class Processes:
        def __init__(self, vm):
            pass

        def exec(self, request):
            return current

        def get(self, identifier, **kwargs):
            return current

        def kill(self, identifier):
            killed.append(identifier)

        def write_stdin(self, identifier, data):
            pass

        def close_stdin(self, identifier):
            pass

    monkeypatch.setattr(shim, "GuestProcesses", Processes)

    async def scenario():
        sandbox = shim.AsyncSandbox(shim.Sandbox(vm=types.SimpleNamespace(id="vm")))
        handle = await sandbox.commands.run("cat", background=True, timeout=0.01)
        with pytest.raises(native.TimeoutException):
            await handle.wait()
        detached = await sandbox.commands.run("cat", background=True, timeout=0)
        await detached.disconnect()

    asyncio.run(scenario())
    assert killed == []
