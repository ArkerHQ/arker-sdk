"""Native uploads preserve binary content, user selection, and metadata."""

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from e2b import FileType
from http_fixtures import HTTPHandler

from arker._compat_e2b_data_plane import EnvdDescriptor, create_data_plane


@pytest.fixture
def file_server(http_server):
    payload = bytes(range(256)) * 512
    requests = []

    class Handler(HTTPHandler):
        def do_GET(self):
            self.send_body(payload, content_type="application/octet-stream")

        def do_POST(self):
            body = self.read_body()
            requests.append((self.path, body, dict(self.headers)))
            result = json.dumps(
                [{"name": "value", "path": "/value", "type": "file", "metadata": {"kind": "binary"}}]
            ).encode()
            self.send_body(result)

    server = http_server(Handler)
    yield EnvdDescriptor(f"http://127.0.0.1:{server.server_port}", "0.10.2", "guest"), payload, requests


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
