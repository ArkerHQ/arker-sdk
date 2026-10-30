"""Exercise original async E2B code with only its Sandbox import changed."""

import asyncio
import os

import pytest
from compat_evidence import Cleanup, record_scenario

PROGRAM = r"""
from e2b import AsyncSandbox, AsyncCommandHandle, CommandExitException, CommandResult, NotFoundException

async def program(cleanup):
    sandbox = await AsyncSandbox.create()
    cleanup.allocated(sandbox.sandbox_id)
    try:
        connected = await AsyncSandbox.connect(sandbox.sandbox_id)
        assert connected.sandbox_id == sandbox.sandbox_id
        assert sandbox.sandbox_id.lower() in sandbox.get_host(8080).lower()
        result = await connected.commands.run('printf "%s" "$VALUE"', envs={"VALUE": "async"})
        assert isinstance(result, CommandResult)
        assert (result.stdout, result.stderr, result.exit_code, result.error) == ("async", "", 0, None)
        await sandbox.files.write("/tmp/compat-async-e2b", "async λ")
        assert await sandbox.files.read("/tmp/compat-async-e2b") == "async λ"
        stdout = []
        async def on_stdout(chunk):
            await asyncio.sleep(0)
            stdout.append(chunk)
        handle = await sandbox.commands.run("printf '%s\n' $$; cat; printf diagnostic >&2; exit 17",
            background=True, stdin=True, on_stdout=on_stdout)
        assert isinstance(handle, AsyncCommandHandle)
        await sandbox.commands.send_stdin(handle.pid, "hello λ\n")
        await sandbox.commands.close_stdin(handle.pid)
        try:
            await handle.wait()
        except CommandExitException as error:
            expected = str(handle.pid) + "\nhello λ\n"
            assert error.stdout == expected, repr(error.stdout)
            assert "".join(stdout) == expected
            assert (error.stderr, error.exit_code, error.error) == ("diagnostic", 17, "exit status 17")
        else:
            raise AssertionError("Expected typed asynchronous command exit")
        detached = await sandbox.commands.run("cat; printf reconnected", background=True, stdin=True)
        await detached.disconnect()
        reconnected = await sandbox.commands.connect(detached.pid)
        await reconnected.send_stdin("shared\n")
        await reconnected.close_stdin()
        assert (await reconnected.wait()).stdout == "shared\nreconnected"
        killed = await sandbox.commands.run("cat", background=True, stdin=True)
        assert await killed.kill() is True
        try:
            await killed.wait()
        except CommandExitException as error:
            assert (error.exit_code, error.error) == (-1, "signal: killed")
        else:
            raise AssertionError("Killed async process did not raise")
        assert await sandbox.commands.kill(killed.pid) is False
        assert await sandbox.commands.kill(2147483647) is False
        try:
            await sandbox.commands.connect(2147483647)
        except NotFoundException:
            pass
        else:
            raise AssertionError("Missing async process did not raise")
    finally:
        try:
            async with sandbox as entered:
                assert entered is sandbox
                assert await AsyncSandbox.kill(sandbox.sandbox_id) is True
                raise RuntimeError("context sentinel")
        except RuntimeError as error:
            assert str(error) == "context sentinel"
        else:
            raise AssertionError("Async context swallowed application error")
        assert await sandbox.kill() is False
        assert await AsyncSandbox.kill(sandbox_id=sandbox.sandbox_id) is False
        await cleanup.observe_absent_async(sandbox.sandbox_id, lambda identifier: AsyncSandbox.get_info(identifier))
"""


@pytest.mark.parametrize("provider,shim", [("e2b", False), ("e2b", True), ("arker", True)])
def test_async_import_only(provider, shim, live_router, monkeypatch):
    pytest.importorskip("e2b")
    if not os.environ.get("E2B_API_KEY" if provider == "e2b" else "ARKER_API_KEY"):
        pytest.skip("Live credentials required")
    if provider == "arker":
        monkeypatch.delenv("E2B_API_KEY", raising=False)
    source = (
        PROGRAM.replace("from e2b import AsyncSandbox,", "from arker.e2b import AsyncSandbox\nfrom e2b import")
        if shim
        else PROGRAM
    )
    namespace = {"asyncio": asyncio}
    exec(compile(source, "e2b_async_import_only.py", "exec"), namespace)
    cleanup = Cleanup("e2b")
    with record_scenario(
        "e2b",
        "async",
        provider,
        shim,
        "import-only-async-command-and-lifecycle",
        [
            "Sandbox.create",
            "Sandbox.connect",
            "Sandbox.get_host",
            "Sandbox.kill",
            "Sandbox.__aenter__",
            "Sandbox.__aexit__",
            "Sandbox.files.write",
            "Sandbox.files.read",
            "Sandbox.commands.run",
            "Sandbox.commands.connect",
            "Sandbox.commands.send_stdin",
            "Sandbox.commands.close_stdin",
            "Sandbox.commands.kill",
            "CommandHandle.pid",
            "CommandHandle.wait",
            "CommandHandle.send_stdin",
            "CommandHandle.close_stdin",
            "CommandHandle.disconnect",
            "CommandHandle.kill",
        ],
        cleanup=cleanup,
    ):
        asyncio.run(namespace["program"](cleanup))
