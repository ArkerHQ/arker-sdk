import asyncio
import types

import pytest
from async_lifecycle_helpers import cancel_creation, run_async
from async_lifecycle_helpers import configure_router as configure_router
from async_lifecycle_helpers import late_arker_creation as late_arker_creation

import arker.e2b as shim


@pytest.fixture
def guest_processes(monkeypatch):
    calls, killed = [], []
    current = types.SimpleNamespace(pid="42", status="running", stdout="", stderr="", exit_code=0)

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

        def kill(self, identifier):
            killed.append(identifier)

        def close_stdin(self, identifier):
            calls.append("eof")

    monkeypatch.setattr(shim, "GuestProcesses", Processes)

    return current, calls, killed


@pytest.fixture(autouse=True)
def _valid_e2b_parent_identity(monkeypatch):
    monkeypatch.setattr(shim, "_validate_e2b_identity", lambda vm: None)


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
    result = asyncio.run(shim.AsyncSandbox.create())
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
    asyncio.run(late_arker_creation(shim, boolean_delete=True)(shim.AsyncSandbox.create()))


@run_async
async def test_arker_async_handles_preserve_native_types_and_await_callbacks(guest_processes):
    native = pytest.importorskip("e2b")
    current, calls, _ = guest_processes
    current.status, current.stdout, current.stderr, current.exit_code = "completed", "λ", "failure", 17

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

    assert ("stdin", "hello") in calls
    assert "eof" in calls


def test_async_race_checks_metadata_miss_without_connecting(configure_router, monkeypatch):
    native = pytest.importorskip("e2b")
    configure_router(["e2b", "arker"], existing_strategy="race", create_order=["arker"])
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


@run_async
async def test_async_observation_timeout_and_disconnect_do_not_kill(guest_processes):
    native = pytest.importorskip("e2b")
    _, _, killed = guest_processes

    sandbox = shim.AsyncSandbox(shim.Sandbox(vm=types.SimpleNamespace(id="vm")))
    handle = await sandbox.commands.run("cat", background=True, timeout=0.01)
    with pytest.raises(native.TimeoutException):
        await handle.wait()
    detached = await sandbox.commands.run("cat", background=True, timeout=0)
    await detached.disconnect()

    assert killed == []
