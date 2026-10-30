"""The same session and terminal programs on native and routed providers."""

import asyncio
import os

import pytest
from compat_evidence import record_scenario

SYNC = r"""
from daytona import Daytona
from daytona.common.process import SessionExecuteRequest
from daytona.common.pty import PtySize


def program():
    client = Daytona()
    sandbox = client.create()
    try:
        process = sandbox.process
        process.create_session("persistent")
        process.execute_session_command("persistent", SessionExecuteRequest(
            command="export RETAINED=yes; cd /tmp; retained() { printf function; }"))
        result = process.execute_session_command("persistent", SessionExecuteRequest(
            command='printf "%s:%s:" "$RETAINED" "$PWD"; retained; (exit 7)'))
        assert result.stdout == "yes:/tmp:function\n", repr(result.stdout)
        assert result.exit_code == 7
        assert len(process.get_session("persistent").commands) == 2
        process.delete_session("persistent")
        terminal = process.create_pty_session("terminal", pty_size=PtySize(rows=24, cols=80))
        terminal.resize(PtySize(rows=31, cols=93))
        terminal.send_input("stty size; test -t 0 && test -t 1 && test -t 2 && printf ALLTTY; exit 7\n")
        chunks = []
        result = terminal.wait(on_data=chunks.append, timeout=15)
        terminal.disconnect()
        output = b"".join(chunks)
        assert b"31 93\r\n" in output, repr(output)
        assert b"ALLTTY" in output
        assert result.exit_code == 7, result
        assert result.error == "non-zero exit", result
    finally:
        client.delete(sandbox)
"""

ASYNC = r"""
import asyncio
from daytona import AsyncDaytona
from daytona.common.process import SessionExecuteRequest
from daytona.common.pty import PtySize


async def program():
    async with AsyncDaytona() as client:
        sandbox = await client.create()
        try:
            process = sandbox.process
            await process.create_session("persistent")
            await process.execute_session_command("persistent", SessionExecuteRequest(
                command="export RETAINED=yes; cd /tmp; retained() { printf function; }"))
            result = await process.execute_session_command("persistent", SessionExecuteRequest(
                command='printf "%s:%s:" "$RETAINED" "$PWD"; retained; (exit 7)'))
            assert result.stdout == "yes:/tmp:function\n", repr(result.stdout)
            assert result.exit_code == 7
            command = await process.execute_session_command("persistent", SessionExecuteRequest(
                command='read value; printf "%s" "$value"; printf error >&2', run_async=True,
                suppress_input_echo=True))
            chunks, errors = [], []
            async def stdout(text):
                await asyncio.sleep(0)
                chunks.append(text)
            reader = asyncio.create_task(process.get_session_command_logs_async(
                "persistent", command.cmd_id, stdout, errors.append))
            await process.send_session_command_input("persistent", command.cmd_id, "hello\n")
            await asyncio.wait_for(reader, 15)
            assert "".join(chunks) == "hello\n", chunks
            assert "".join(errors) == "error\n", errors
            await process.delete_session("persistent")
            output = []
            terminal = await process.create_pty_session("terminal", on_data=output.append,
                pty_size=PtySize(rows=24, cols=80))
            await terminal.resize(PtySize(rows=31, cols=93))
            await terminal.send_input("stty size; test -t 0 && test -t 1 && test -t 2 && printf ALLTTY; exit 7\n")
            result = await asyncio.wait_for(terminal.wait(), 15)
            await terminal.disconnect()
            assert b"31 93\r\n" in b"".join(output), repr(output)
            assert result.exit_code == 7, result
            assert result.error == "non-zero exit", result
        finally:
            await client.delete(sandbox)
"""


@pytest.mark.parametrize("provider,shim", [("daytona", False), ("daytona", True), ("arker", True)])
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_daytona_process_import_only(provider, shim, mode, live_router, monkeypatch):
    pytest.importorskip("daytona")
    if not os.environ.get("DAYTONA_API_KEY" if provider == "daytona" else "ARKER_API_KEY"):
        pytest.skip("Live credentials required")
    if provider == "arker":
        monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    source = SYNC if mode == "sync" else ASYNC
    if shim:
        source = source.replace("from daytona import", "from arker.daytona import")
    namespace = {}
    exec(compile(source, "daytona_process_import_only.py", "exec"), namespace)
    with record_scenario(
        "daytona",
        mode,
        provider,
        shim,
        "persistent-session-and-real-terminal",
        [
            "Sandbox.process.create_session",
            "Sandbox.process.execute_session_command",
            "Sandbox.process.get_session",
            "Sandbox.process.delete_session",
            "Sandbox.process.create_pty_session",
        ],
    ):
        if mode == "sync":
            namespace["program"]()
        else:
            asyncio.run(namespace["program"]())
