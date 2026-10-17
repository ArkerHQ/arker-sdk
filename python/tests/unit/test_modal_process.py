from __future__ import annotations

import asyncio
import base64
import types

import pytest


@pytest.mark.parametrize("text", [False, True])
@pytest.mark.parametrize("async_reads", [False, True])
def test_modal_process_uses_native_streams_and_buffered_stdin(text, async_reads):
    pytest.importorskip("modal")
    from modal.container_process import ContainerProcess
    from modal.exception import InvalidError

    from arker._modal_process import modal_process

    class Processes:
        def __init__(self):
            self.input = bytearray()
            self.closed = False

        def get(self, identifier, **kwargs):
            assert identifier == "123"
            assert kwargs == {"raw_exit_code": True}
            return types.SimpleNamespace(status="completed" if self.closed else "running", exit_code=17)

        def _call(self, action, **kwargs):
            assert action == "readStream"
            data = bytes(self.input) if kwargs["stream"] == "stdout" else b"error"
            offset = kwargs["offset"]
            chunk = data[offset : offset + 1003]
            return {
                "data": base64.b64encode(chunk).decode(),
                "nextOffset": offset + len(chunk),
                "eof": self.closed and offset + len(chunk) >= len(data),
            }

        def write_stdin(self, identifier, data):
            self.input.extend(data)

        def close_stdin(self, identifier):
            self.closed = True

    processes = Processes()
    process = modal_process(processes, "123", text=text)
    assert isinstance(process, ContainerProcess)
    assert process.poll() is None
    with pytest.raises(InvalidError):
        _ = process.returncode
    data = ("αβγ\n" * 40000).encode() if text else bytes(range(256)) * 1024
    process.stdin.write(data)
    assert not processes.input
    process.stdin.write_eof()

    async def read_async():
        await process.stdin.drain.aio()
        return await process.stdout.read.aio(), await process.stderr.read.aio(), await process.wait.aio()

    if async_reads:
        output, error, code = asyncio.run(read_async())
    else:
        process.stdin.drain()
        output, error, code = process.stdout.read(), process.stderr.read(), process.wait()
    assert output == (data.decode() if text else data)
    assert error == ("error" if text else b"error")
    assert code == 17
    assert process.returncode == 17
    assert process.poll() == 17
    assert process.stdout.read() == ("" if text else b"")
