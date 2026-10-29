"""PTY operations use native handles and the shared authenticated transport."""

import asyncio
import base64

import pytest
from e2b import AsyncCommandHandle, CommandExitException, CommandHandle, PtySize
from test_e2b_commands import process_server as process_server

from arker._compat_e2b_data_plane import create_data_plane


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("user", ["root", "custom-account"])
def test_native_pty_resize_binary_input_and_exit(process_server, asynchronous, user):
    descriptor, requests = process_server

    async def check():
        data = create_data_plane(descriptor, "platform", asynchronous=True)
        try:
            handle = await data.pty.create(size=PtySize(rows=24, cols=80), on_data=lambda chunk: None, user=user)
            assert isinstance(handle, AsyncCommandHandle)
            await data.pty.resize(handle.pid, PtySize(rows=30, cols=100))
            await data.pty.send_stdin(handle.pid, b"\x00\xff")
            with pytest.raises(CommandExitException) as failure:
                await handle.wait()
            assert failure.value.exit_code == 7
        finally:
            await data.aclose()

    if asynchronous:
        asyncio.run(check())
    else:
        data = create_data_plane(descriptor, "platform")
        try:
            handle = data.pty.create(PtySize(rows=24, cols=80), user=user)
            assert isinstance(handle, CommandHandle)
            data.pty.resize(handle.pid, PtySize(rows=30, cols=100))
            data.pty.send_stdin(handle.pid, b"\x00\xff")
            with pytest.raises(CommandExitException) as failure:
                handle.wait()
            assert failure.value.exit_code == 7
        finally:
            data.close()
    assert any(path.endswith("/Update") for path, _ in requests)
    assert any(path.endswith("/SendInput") for path, _ in requests)

    headers = {key.lower(): value for key, value in requests.headers[0].items()}
    assert headers["authorization"] == "Basic " + base64.b64encode(f"{user}:".encode()).decode()
