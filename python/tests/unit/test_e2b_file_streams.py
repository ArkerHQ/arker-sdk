"""Native binary reads and explicitly closed streams share the scoped transport."""

import asyncio

import pytest
from test_e2b_staging import file_server as file_server

from arker._compat_e2b_data_plane import create_data_plane


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_native_binary_and_stream_reads_release_responses(file_server, asynchronous, partial):
    descriptor, payload, _ = file_server

    async def check():
        data = create_data_plane(descriptor, "platform", asynchronous=True)
        try:
            binary = await data.files.read("/value", format="bytes")
            assert type(binary) is bytearray and binary == payload
            reader = await data.files.read("/value", format="stream")
            async with reader:
                if partial:
                    assert await anext(reader)
                else:
                    assert b"".join([chunk async for chunk in reader]) == payload
        finally:
            await data.aclose()
        assert not data._pool.pending and not data._pool.responses

    if asynchronous:
        asyncio.run(check())
    else:
        data = create_data_plane(descriptor, "platform")
        try:
            binary = data.files.read("/value", format="bytes")
            assert type(binary) is bytearray and binary == payload
            with data.files.read("/value", format="stream") as reader:
                if partial:
                    assert next(reader)
                else:
                    assert b"".join(reader) == payload
        finally:
            data.close()
