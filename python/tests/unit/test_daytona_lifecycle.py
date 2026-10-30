import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("daytona")
from daytona.common.errors import DaytonaError, DaytonaNotFoundError

from arker._compat import UnsupportedCompatibilityOption
from arker._compat_daytona_async import AsyncSandbox
from arker.computer import ArkerError
from arker.daytona import Sandbox


def box():
    client = SimpleNamespace(_request=Mock(), control_base_url="https://control.invalid")
    vm = SimpleNamespace(id="source", base_url="https://regional.invalid", _client=client, fork=Mock())
    return Sandbox(vm=vm), client


def test_pause_and_start_use_authoritative_public_operations():
    sandbox, client = box()
    client._request.return_value = {"vm_id": "source", "state": "paused", "mode": "full"}
    sandbox.pause(0)
    args, kwargs = client._request.call_args
    assert args == ("POST", "/v1/vms/source/pause", {"mode": "full"})
    assert kwargs["request_timeout"] is None
    assert kwargs["retry_network_failures"] is False
    client._request.return_value = {"vm_id": "source", "state": "running"}
    sandbox.start()
    assert client._request.call_args.args == ("POST", "/v1/vms/source/resume", {})
    assert client._request.call_count == 2


@pytest.mark.parametrize("method", ["stop", "archive"])
def test_shutdown_and_archive_refuse_before_mutation(method):
    sandbox, client = box()
    with pytest.raises(UnsupportedCompatibilityOption):
        getattr(sandbox, method)()
    client._request.assert_not_called()


def test_disk_fork_preserves_parent_and_resumes_only_child():
    sandbox, client = box()
    child = SimpleNamespace(id="child", base_url=sandbox._vm.base_url, _client=client)
    sandbox._vm.fork.return_value = child
    client._request.return_value = {"vm_id": "child", "state": "running"}
    result = sandbox.fork(timeout=0)
    assert result.id == "child"
    sandbox._vm.fork.assert_called_once_with(layers=["disk"], durable=False, request_timeout=None)
    assert client._request.call_args.args == ("POST", "/v1/vms/child/resume", {})


def test_failed_child_start_retains_native_error_and_recovery_id():
    sandbox, client = box()
    child = SimpleNamespace(id="child", base_url=sandbox._vm.base_url, _client=client)
    sandbox._vm.fork.return_value = child
    client._request.side_effect = ArkerError("not_found", "missing", 404)
    with pytest.raises(DaytonaNotFoundError) as error:
        sandbox.fork()
    assert error.value.sandbox_id == "child"
    assert client._request.call_count == 1


@pytest.mark.parametrize(
    "operation,args,kwargs",
    [
        ("pause", (0,), {}),
        ("start", (12,), {}),
        ("stop", (3,), {"force": True}),
        ("archive", (), {"request_timeout": 5}),
        ("fork", (), {"name": "child", "timeout": 7}),
    ],
)
def test_native_forwarding_and_error_identity(operation, args, kwargs):
    native = SimpleNamespace(id="native", process=object(), fs=object())
    method = Mock(return_value=object())
    setattr(native, operation, method)
    sandbox = Sandbox(native=native)
    result = getattr(sandbox, operation)(*args, **kwargs)
    assert result is method.return_value
    method.assert_called_once_with(*args, **kwargs)
    error = DaytonaNotFoundError("missing")
    method.side_effect = error
    with pytest.raises(DaytonaNotFoundError) as caught:
        getattr(sandbox, operation)(*args, **kwargs)
    assert caught.value is error


@pytest.mark.parametrize(
    "operation,args,kwargs",
    [
        ("pause", (0,), {}),
        ("start", (12,), {}),
        ("stop", (3,), {"force": True}),
        ("archive", (), {"request_timeout": 5}),
        ("fork", (), {"name": "child", "timeout": 7}),
    ],
)
def test_native_async_operations_are_awaited(operation, args, kwargs):
    native = SimpleNamespace(id="native", process=object(), fs=object())
    method = AsyncMock(return_value=object())
    setattr(native, operation, method)
    sandbox = AsyncSandbox(Sandbox(native=native))
    assert asyncio.run(getattr(sandbox, operation)(*args, **kwargs)) is method.return_value
    method.assert_awaited_once_with(*args, **kwargs)


def test_named_fork_refuses_custom_region_without_control_authority():
    sandbox, client = box()
    sandbox._lifecycle_config = {"base_url": sandbox._vm.base_url}
    with pytest.raises(ValueError, match="control_base_url"):
        sandbox.fork("child")
    client._request.assert_not_called()
    sandbox._vm.fork.assert_not_called()


@pytest.mark.parametrize("status", [401, 403, 404, 409])
def test_named_fork_errors_never_fallback_or_retry(status):
    sandbox, client = box()
    sandbox._lifecycle_config = {"control_base_url": client.control_base_url}
    client._request.side_effect = ArkerError("refused", "refused", status)
    with pytest.raises(DaytonaError):
        sandbox.fork("child")
    assert client._request.call_count == 1
    assert client._request.call_args.args[1] == "/v1/named-fork"
    assert client._request.call_args.kwargs["retry_network_failures"] is False
    sandbox._vm.fork.assert_not_called()


def test_async_arker_transitions_use_same_authoritative_calls():
    sandbox, client = box()
    client._request.side_effect = [
        {"vm_id": "source", "state": "paused", "mode": "full"},
        {"vm_id": "source", "state": "running"},
    ]

    async def run():
        wrapped = AsyncSandbox(sandbox)
        await wrapped.pause(0)
        await wrapped.start(0)

    asyncio.run(run())
    assert [call.args[1] for call in client._request.call_args_list] == [
        "/v1/vms/source/pause",
        "/v1/vms/source/resume",
    ]


def test_timeout_preserves_unknown_outcome_without_retry():
    import httpx
    from daytona.common.errors import DaytonaConnectionTimeoutError

    sandbox, client = box()
    error = httpx.ReadTimeout("response lost")
    client._request.side_effect = error
    with pytest.raises(DaytonaConnectionTimeoutError) as caught:
        sandbox.pause()
    assert caught.value.__cause__ is error
    assert client._request.call_count == 1


def test_named_fork_uses_global_authority_and_keeps_name_on_response_loss():
    sandbox, client = box()
    client._request.side_effect = ConnectionError("response lost")
    sandbox._lifecycle_config = {"control_base_url": client.control_base_url}
    with pytest.raises(ConnectionError):
        sandbox.fork("recoverable-name")
    args, kwargs = client._request.call_args
    assert args == (
        "POST",
        "/v1/named-fork",
        {
            "source_vm_id": "source",
            "layers": ["disk"],
            "durable": False,
            "name": "recoverable-name",
        },
    )
    assert kwargs["extra_headers"] == {"Arker-VM-Name": "recoverable-name", "Arker-VM-Name-Mode": "create"}
    assert kwargs["base_url"] == client.control_base_url
    assert client._request.call_count == 1


def test_rejected_timeout_and_unconfirmed_memory_pause():
    from daytona.common.errors import DaytonaValidationError

    sandbox, client = box()
    with pytest.raises(DaytonaValidationError):
        sandbox.pause(-1)
    client._request.assert_not_called()
    client._request.return_value = {"vm_id": "source", "state": "paused", "mode": "filesystem"}
    with pytest.raises(RuntimeError, match="memory preservation"):
        sandbox.pause()


def test_named_fork_returns_authoritative_child_and_uses_control_resume():
    sandbox, client = box()
    sandbox._lifecycle_config = {"control_base_url": client.control_base_url}
    client._request.side_effect = [
        {
            "vm_id": "child",
            "state": "idle",
            "sessions": [],
            "network": {},
            "resources": {},
            "owner_org_id": "org",
            "created_at": "2026-10-30T00:00:00Z",
            "description": "",
            "public": False,
        },
        {"vm_id": "child", "state": "running"},
    ]
    child = sandbox.fork("child-name", timeout=0)
    assert child.id == "child"
    assert child._lifecycle_config == sandbox._lifecycle_config
    assert client._request.call_args.args[1] == "/v1/vms/child/resume"
    assert client._request.call_args.kwargs["base_url"] == client.control_base_url


def test_fork_uses_remaining_budget_and_keeps_id_if_budget_expired(monkeypatch):
    from daytona.common.errors import DaytonaTimeoutError

    from arker import _daytona_lifecycle

    sandbox, client = box()
    sandbox._vm.fork.return_value = SimpleNamespace(id="child", base_url=sandbox._vm.base_url, _client=client)
    times = iter([100.0, 100.25, 102.0])
    monkeypatch.setattr(_daytona_lifecycle.time, "monotonic", lambda: next(times))
    with pytest.raises(DaytonaTimeoutError) as error:
        sandbox.fork(timeout=1)
    assert error.value.sandbox_id == "child"
    assert sandbox._vm.fork.call_args.kwargs["request_timeout"] == 0.75
    client._request.assert_not_called()


def test_async_cancelled_accepted_fork_never_deletes_child():
    import threading

    sandbox, client = box()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    child = SimpleNamespace(id="child", base_url=sandbox._vm.base_url, _client=client, delete=Mock())

    def fork(**kwargs):
        entered.set()
        assert release.wait(5)
        return child

    def resume(*args, **kwargs):
        finished.set()
        return {"vm_id": "child", "state": "running"}

    sandbox._vm.fork.side_effect = fork
    client._request.side_effect = resume

    async def run():
        pending = asyncio.create_task(AsyncSandbox(sandbox).fork())
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 5)

    asyncio.run(run())
    child.delete.assert_not_called()
    assert client._request.call_count == 1


@pytest.mark.parametrize("asynchronous", [False, True])
def test_pause_none_refuses_before_dispatch(asynchronous):
    sandbox, client = box()
    with pytest.raises(DaytonaError) as caught:
        if asynchronous:
            asyncio.run(AsyncSandbox(sandbox).pause(None))
        else:
            sandbox.pause(None)
    assert type(caught.value) is DaytonaError
    client._request.assert_not_called()


@pytest.mark.parametrize("operation", ["start", "fork", "pause"])
@pytest.mark.parametrize("context", ["main", "thread", "async"])
def test_fractional_budget_respects_native_execution_context(operation, context):
    import signal
    from concurrent.futures import ThreadPoolExecutor

    sandbox, client = box()
    client._request.return_value = {
        "vm_id": "source",
        "state": "paused" if operation == "pause" else "running",
        "mode": "full",
    }
    sandbox._vm.fork.return_value = sandbox._vm
    invoke = lambda: getattr(sandbox, operation)(timeout=0.2)
    if context == "main":
        invoke()
    elif context == "thread":
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(invoke).result()
    else:
        asyncio.run(getattr(AsyncSandbox(sandbox), operation)(timeout=0.2))
    request = sandbox._vm.fork if operation == "fork" else client._request
    budget = request.call_args.kwargs["request_timeout"]
    expected = 1 if context == "main" and hasattr(signal, "SIGALRM") and operation != "pause" else 0.2
    assert expected - 0.1 < budget <= expected
