"""Native uploads preserve binary content, user selection, and metadata."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from e2b import FileType

from arker._compat_e2b_data_plane import EnvdDescriptor, create_data_plane


@pytest.fixture
def file_server():
    payload = bytes(range(256)) * 512
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

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
            requests.append((self.path, body, dict(self.headers)))
            result = json.dumps(
                [{"name": "value", "path": "/value", "type": "file", "metadata": {"kind": "binary"}}]
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(result)))
            self.end_headers()
            self.wfile.write(result)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield EnvdDescriptor(f"http://127.0.0.1:{server.server_port}", "0.10.2", "guest"), payload, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_upload_bytes_and_metadata_survive_native_transport(file_server, asynchronous):
    descriptor, payload, requests = file_server

    async def check():
        data = create_data_plane(descriptor, "platform", asynchronous=True)
        try:
            return await data.files.write(
                "/value", payload, user="root", metadata={"kind": "binary"}, use_octet_stream=True
            )
        finally:
            await data.aclose()

    if asynchronous:
        result = asyncio.run(check())
    else:
        data = create_data_plane(descriptor, "platform")
        try:
            result = data.files.write(
                "/value", payload, user="root", metadata={"kind": "binary"}, use_octet_stream=True
            )
        finally:
            data.close()
    assert result.path == "/value" and result.type is FileType.FILE and result.metadata == {"kind": "binary"}
    assert len(requests) == 1
    path, body, headers = requests[0]
    assert body == payload
    assert parse_qs(urlsplit(path).query)["username"] == ["root"]
    assert {key.lower(): value for key, value in headers.items()}["x-api-key"] == "platform"
