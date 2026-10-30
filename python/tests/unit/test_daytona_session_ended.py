import asyncio
import contextlib
from types import SimpleNamespace

import pytest
from test_daytona_sessions import session as session_fixture

session = session_fixture


def test_shell_exit_preserves_unknown_exit_and_open_log_stream(session):
    owner, identifier = session
    result = owner.execute_session_command(identifier, SimpleNamespace(command="printf before; exit 7", run_async=True))
    chunks = []

    async def follow():
        task = asyncio.create_task(
            owner.get_session_command_logs_async(identifier, result.cmd_id, chunks.append, lambda _: None)
        )
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), 0.1)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(follow())
    assert "".join(chunks) == "before\n"
    assert owner.get_session_command(identifier, result.cmd_id).exit_code is None
    with pytest.raises(RuntimeError, match="Command input is closed"):
        owner.send_session_command_input(identifier, result.cmd_id, "after\n")
    with pytest.raises(RuntimeError, match="session process has exited"):
        owner.execute_session_command(identifier, SimpleNamespace(command="printf after", run_async=True))


def test_closed_input_wire_error_has_native_internal_server_identity():
    import json

    DaytonaInternalServerError = pytest.importorskip("daytona.common.errors").DaytonaInternalServerError

    from arker._daytona_sessions import SessionProcess
    from arker.computer import CompletedRunResult

    owner = SessionProcess()
    owner._session_control_id = "control"
    owner._session_source = lambda: "/helper.py"
    owner._vm = SimpleNamespace(
        run=lambda *args, **kwargs: CompletedRunResult(
            stdout=json.dumps({"error": "Command input is closed", "code": "input_closed"}),
            stderr="",
            stdout_bytes=b"",
            stderr_bytes=b"",
            exit_code=1,
        )
    )
    with pytest.raises(DaytonaInternalServerError) as caught:
        owner.send_session_command_input("existing", "existing", "after")
    assert caught.value.status_code == 500
