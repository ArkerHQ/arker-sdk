"""Native command handles, callbacks, and exit errors over the adapter transport."""

import asyncio
import base64
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from e2b import AsyncCommandHandle, CommandExitException, CommandHandle

from arker._compat_e2b_data_plane import EnvdDescriptor, create_data_plane


@pytest.fixture
def process_server():
    class Requests(list):
        def __init__(self):
            super().__init__()
            self.headers = []

    requests = Requests()

    def frame(value, flags=0):
        payload = json.dumps(value).encode()
        return struct.pack(">BI", flags, len(payload)) + payload

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.headers.get("Transfer-Encoding") == "chunked":
                chunks = []
                while size := int(self.rfile.readline().strip(), 16):
                    chunks.append(self.rfile.read(size))
                    self.rfile.read(2)
                self.rfile.readline()
                body = b"".join(chunks)
            else:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append((self.path, body))
            requests.headers.append(dict(self.headers))
            streaming = self.path.endswith(("/Start", "/Connect"))
            output = (
                b"".join(
                    [
                        frame({"event": {"start": {"pid": 123}}}),
                        frame({"event": {"data": {"stdout": base64.b64encode("λ".encode()).decode()}}}),
                        frame({"event": {"end": {"exitCode": 7, "exited": True, "status": "exited"}}}),
                        frame({}, 2),
                    ]
                )
                if streaming
                else b"{}"
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/connect+json" if streaming else "application/json")
            self.send_header("Content-Length", str(len(output)))
            self.end_headers()
            self.wfile.write(output)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield EnvdDescriptor(f"http://127.0.0.1:{server.server_port}", "0.10.2", "guest"), requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("user", ["root", "custom-account"])
def test_native_exit_handle_callback_and_stdin_contract(process_server, asynchronous, background, user):
    descriptor, requests = process_server
    output = []

    async def check():
        data = create_data_plane(descriptor, "platform", asynchronous=True)
        try:

            async def callback(chunk):
                await asyncio.sleep(0)
                output.append(chunk)

            with pytest.raises(CommandExitException) as failure:
                result = await data.commands.run(
                    "exit 7", background=background, stdin=True, envs={"VALUE": "kept"}, on_stdout=callback, user=user
                )
                assert isinstance(result, AsyncCommandHandle)
                await data.commands.send_stdin(result.pid, "hello")
                await data.commands.close_stdin(result.pid)
                await result.wait()
            assert failure.value.exit_code == 7 and failure.value.stdout == "λ"
        finally:
            await data.aclose()

    if asynchronous:
        asyncio.run(check())
    else:
        data = create_data_plane(descriptor, "platform")
        try:
            with pytest.raises(CommandExitException) as failure:
                result = data.commands.run(
                    "exit 7",
                    background=background,
                    stdin=True,
                    envs={"VALUE": "kept"},
                    on_stdout=output.append,
                    user=user,
                )
                assert isinstance(result, CommandHandle)
                data.commands.send_stdin(result.pid, "hello")
                data.commands.close_stdin(result.pid)
                result.wait()
            assert failure.value.exit_code == 7 and failure.value.stdout == "λ"
        finally:
            data.close()
    if not background:
        assert output == ["λ"]
    assert b'"VALUE":"kept"' in requests[0][1].replace(b" ", b"")
    if background:
        assert any(path.endswith("/SendInput") for path, _ in requests)
        assert any(path.endswith("/CloseStdin") for path, _ in requests)

    headers = {key.lower(): value for key, value in requests.headers[0].items()}
    assert headers["authorization"] == "Basic " + base64.b64encode(f"{user}:".encode()).decode()
