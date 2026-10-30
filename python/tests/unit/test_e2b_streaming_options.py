"""Native streaming options across the instance-scoped HTTP transport."""

import asyncio
import base64
import json
import threading

import pytest
from http_fixtures import HTTPHandler

from arker._compat_e2b_data_plane import EnvdDescriptor, create_data_plane


def frame(value, flags=0):
    payload = json.dumps(value).encode()
    return bytes([flags]) + len(payload).to_bytes(4, "big") + payload


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("retain", [False, True])
@pytest.mark.parametrize("operation", ["run", "background", "connect"])
def test_output_retention_keeps_callbacks(http_server, asynchronous, retain, operation):
    seen = []

    class Handler(HTTPHandler):
        def do_POST(self):
            self.read_body()
            seen.append((self.path, self.headers["X-API-Key"], self.headers["X-Access-Token"]))
            events = [{"start": {"pid": 123}}]
            events.extend(
                {"data": {name: base64.b64encode(value).decode()}}
                for name, value in [("stdout", b"hello "), ("stdout", "世界".encode()), ("stderr", b"warning")]
            )
            events.append({"end": {"exitCode": 0, "exited": True, "status": "exited"}})
            self.send_body(
                b"".join(frame({"event": event}) for event in events) + frame({}, 2),
                content_type="application/connect+json",
            )

    server = http_server(Handler)
    descriptor = EnvdDescriptor(f"http://127.0.0.1:{server.server_port}", "0.10.2", "guest")
    stdout, stderr = [], []
    options = {"retain_output": retain, "on_stdout": stdout.append, "on_stderr": stderr.append}

    async def run():
        data = create_data_plane(descriptor, "platform", asynchronous=True)
        try:
            if operation == "run":
                return await data.commands.run("printf output", **options)
            handle = (
                await data.commands.run("printf output", background=True, **options)
                if operation == "background"
                else await data.commands.connect(123, **options)
            )
            return await handle.wait()
        finally:
            await data.aclose()

    if asynchronous:
        result = asyncio.run(run())
    else:
        data = create_data_plane(descriptor, "platform")
        try:
            if operation == "run":
                result = data.commands.run("printf output", **options)
            else:
                handle = (
                    data.commands.run("printf output", background=True, retain_output=retain)
                    if operation == "background"
                    else data.commands.connect(123, retain_output=retain)
                )
                result = handle.wait(on_stdout=stdout.append, on_stderr=stderr.append)
        finally:
            data.close()
    assert "".join(stdout) == "hello 世界"
    assert "".join(stderr) == "warning"
    assert result.stdout == ("hello 世界" if retain else None)
    assert result.stderr == ("warning" if retain else None)
    assert result.exit_code == 0
    assert len(seen) == 1
    assert seen[0][1:] == ("platform", "guest")


@pytest.mark.parametrize("asynchronous", [False, True])
def test_iterable_upload_is_pulled_after_request_arrives(http_server, asynchronous):
    arrived = threading.Event()
    received = []

    class Handler(HTTPHandler):
        def do_POST(self):
            arrived.set()
            received.append(
                (
                    self.path,
                    self.headers["Content-Type"],
                    self.read_body(),
                    self.headers["X-API-Key"],
                    self.headers["X-Access-Token"],
                )
            )
            self.send_body(b'[{"name":"file","path":"/tmp/file","type":"file"}]')

    server = http_server(Handler)
    descriptor = EnvdDescriptor(f"http://127.0.0.1:{server.server_port}", "0.10.2", "guest")
    chunks = [b"first\x00", b"", "世界".encode(), b"last"]

    def source():
        yield chunks[0]
        assert arrived.wait(5), "upload was buffered before request dispatch"
        yield from chunks[1:]

    async def asource():
        yield chunks[0]
        assert await asyncio.to_thread(arrived.wait, 5), "upload was buffered before request dispatch"
        for chunk in chunks[1:]:
            yield chunk

    async def run():
        data = create_data_plane(descriptor, "platform", asynchronous=True)
        try:
            return await data.files.write("/tmp/file", asource())
        finally:
            await data.aclose()

    if asynchronous:
        result = asyncio.run(run())
    else:
        data = create_data_plane(descriptor, "platform")
        try:
            result = data.files.write("/tmp/file", source())
        finally:
            data.close()
    assert result.path == "/tmp/file"
    assert len(received) == 1
    assert received[0][1:] == ("application/octet-stream", b"".join(chunks), "platform", "guest")


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("invalid", ["wrong-kind", "multipart", "old-envd"])
def test_iterable_rejection_preserves_native_errors_without_io(http_server, asynchronous, invalid):
    from e2b import InvalidArgumentException, TemplateException

    requests = []

    class Handler(HTTPHandler):
        def do_POST(self):
            requests.append(self.path)
            self.send_body(b"[]")

    server = http_server(Handler)
    descriptor = EnvdDescriptor(
        f"http://127.0.0.1:{server.server_port}", "0.5.6" if invalid == "old-envd" else "0.10.2", "guest"
    )
    consumed = []

    def source():
        consumed.append(True)
        yield b"content"

    async def asource():
        consumed.append(True)
        yield b"content"

    options = {"use_octet_stream": False} if invalid == "multipart" else {}
    expected = TemplateException if invalid == "old-envd" else InvalidArgumentException

    async def run():
        data = create_data_plane(descriptor, "platform", asynchronous=True)
        try:
            with pytest.raises(expected):
                await data.files.write("/tmp/file", source() if invalid == "wrong-kind" else asource(), **options)
        finally:
            await data.aclose()

    if asynchronous:
        asyncio.run(run())
    else:
        data = create_data_plane(descriptor, "platform")
        try:
            with pytest.raises(expected):
                data.files.write("/tmp/file", asource() if invalid == "wrong-kind" else source(), **options)
        finally:
            data.close()
    assert requests == []
    assert consumed == []


def test_async_upload_cancellation_closes_producer(http_server):
    arrived = threading.Event()
    disconnected = threading.Event()

    class Handler(HTTPHandler):
        def do_POST(self):
            arrived.set()
            while self.rfile.read(1):
                pass
            disconnected.set()

    server = http_server(Handler)
    descriptor = EnvdDescriptor(f"http://127.0.0.1:{server.server_port}", "0.10.2", "guest")

    async def run():
        finalized = asyncio.Event()
        producing = asyncio.Event()

        async def source():
            try:
                producing.set()
                yield b"first"
                await asyncio.Event().wait()
            finally:
                finalized.set()

        data = create_data_plane(descriptor, "platform", asynchronous=True)
        upload = asyncio.create_task(data.files.write("/tmp/file", source()))
        try:
            assert await asyncio.to_thread(arrived.wait, 5)
            await asyncio.wait_for(producing.wait(), 5)
            upload.cancel()
            with pytest.raises(asyncio.CancelledError):
                await upload
            await asyncio.wait_for(finalized.wait(), 2)
            assert await asyncio.to_thread(disconnected.wait, 2)
        finally:
            await data.aclose()

    asyncio.run(run())
