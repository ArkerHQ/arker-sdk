"""Native filesystem RPC options and result semantics through the scoped transport."""

import asyncio
import base64
import json

import pytest
from e2b import InvalidArgumentException
from http_fixtures import HTTPHandler

from arker._compat_e2b_data_plane import EnvdDescriptor, create_data_plane


@pytest.fixture
def filesystem_server(http_server):
    calls = []
    response = {"status": 200, "body": {}}

    class Handler(HTTPHandler):
        def do_POST(self):
            body = self.read_body()
            calls.append((self.path, json.loads(body), dict(self.headers)))
            payload = json.dumps(response["body"]).encode()
            self.send_body(payload, status=response["status"])

    server = http_server(Handler)
    yield EnvdDescriptor(f"http://127.0.0.1:{server.server_port}", "0.10.2", "guest"), calls, response


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("depth", [None, 1, 2])
@pytest.mark.parametrize("user", ["root", "custom-account"])
def test_list_preserves_depth_identity_and_request_timeout(filesystem_server, asynchronous, depth, user):
    descriptor, calls, response = filesystem_server
    response["body"] = {"entries": []}

    async def check():
        data = create_data_plane(descriptor, "platform", asynchronous=True)
        try:
            assert await data.files.list("/directory", depth=depth, user=user, request_timeout=7) == []
            with pytest.raises(InvalidArgumentException):
                await data.files.list("/directory", depth=0)
        finally:
            await data.aclose()

    if asynchronous:
        asyncio.run(check())
    else:
        data = create_data_plane(descriptor, "platform")
        try:
            assert data.files.list("/directory", depth=depth, user=user, request_timeout=7) == []
            with pytest.raises(InvalidArgumentException):
                data.files.list("/directory", depth=0)
        finally:
            data.close()
    assert len(calls) == 1
    path, body, headers = calls[0]
    assert path.endswith("/ListDir")
    assert body["path"] == "/directory" and body.get("depth", 0) == (depth or 0)
    headers = {key.lower(): value for key, value in headers.items()}
    assert headers["authorization"] == "Basic " + base64.b64encode(f"{user}:".encode()).decode()
    assert headers["connect-timeout-ms"] == "7000"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("exists", [False, True])
def test_make_dir_preserves_native_existing_directory_boolean(filesystem_server, asynchronous, exists):
    descriptor, calls, response = filesystem_server
    if exists:
        response.update(status=409, body={"code": "already_exists", "message": "exists"})

    async def check():
        data = create_data_plane(descriptor, "platform", asynchronous=True)
        try:
            assert await data.files.make_dir("/directory") is (not exists)
        finally:
            await data.aclose()

    if asynchronous:
        asyncio.run(check())
    else:
        data = create_data_plane(descriptor, "platform")
        try:
            assert data.files.make_dir("/directory") is (not exists)
        finally:
            data.close()
    assert len(calls) == 1 and calls[0][1]["path"] == "/directory"
