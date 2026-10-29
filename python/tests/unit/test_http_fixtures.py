import io
from types import SimpleNamespace

import pytest
from http_fixtures import HTTPHandler


@pytest.mark.parametrize(
    "headers,wire,body",
    [
        ({"Content-Length": "3"}, b"a\x00b", b"a\x00b"),
        ({"Transfer-Encoding": "CHUNKED"}, b"1;ext=yes\r\na\r\n2\r\n\x00b\r\n0\r\nX-Trailer: kept\r\n\r\n", b"a\x00b"),
        ({"Transfer-Encoding": "chunked"}, b"0\r\n\r\n", b""),
    ],
)
def test_body_reader_consumes_framing_without_eating_next_request(headers, wire, body):
    handler = SimpleNamespace(headers=headers, rfile=io.BytesIO(wire + b"NEXT"))
    assert HTTPHandler.read_body(handler) == body
    assert handler.rfile.read() == b"NEXT"


@pytest.mark.parametrize("wire", [b"3\r\nx", b"1\r\nx!!0\r\n\r\n"])
def test_chunked_body_reader_rejects_truncation_and_bad_terminators(wire):
    handler = SimpleNamespace(headers={"Transfer-Encoding": "chunked"}, rfile=io.BytesIO(wire))
    with pytest.raises(AssertionError):
        HTTPHandler.read_body(handler)
