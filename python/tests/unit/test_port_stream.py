from __future__ import annotations

import json
import socket

import pytest
import websocket

from arker import Arker, ArkerError, PortStream


class Socket:
    def __init__(self):
        self.frames = iter([(2, b"eof"), (2, b"response"), (1, b"eof")])
        self.sent = []
        self.closed = False

    def recv_data(self):
        return next(self.frames)

    def send_binary(self, data):
        self.sent.append((2, bytes(data)))

    def send(self, text):
        self.sent.append((1, text))

    def close(self):
        self.closed = True


def test_private_port_headers_frames_and_half_close(monkeypatch):
    sock = Socket()
    calls = []

    def connect(url, **options):
        calls.append((url, options))
        return sock

    monkeypatch.setattr(websocket, "create_connection", connect)
    vm = Arker(api_key="ark_test", base_url="https://example.invalid/api").vm("vm_1")
    with vm.connect_port(8080) as port:
        payload = bytes(range(256)) * 1024
        port.sendall(payload)
        assert all(len(data) <= 65536 for kind, data in sock.sent if kind == 2)
        assert b"".join(data for kind, data in sock.sent if kind == 2) == payload
        assert port.recv(2) == b"eo"
        assert port.recv(2) == b"f"
        port.shutdown(socket.SHUT_WR)
        assert port.recv(20) == b"response"
        assert port.recv(20) == b""
        with pytest.raises(BrokenPipeError):
            port.sendall(b"late")
    assert sock.closed
    assert calls[0][0] == "wss://example.invalid/api/v1/vms/vm_1/ports/8080"
    assert calls[0][1]["header"] == ["Authorization: Bearer ark_test"]
    assert calls[0][1]["subprotocols"] == ["vm-port.v1"]
    assert calls[0][1]["redirect_limit"] == 0


@pytest.mark.parametrize("port", [0, -1, 65536, True, 1.5])
def test_invalid_port_never_opens_a_connection(monkeypatch, port):
    def unexpected(*args, **kwargs):
        raise AssertionError("invalid port reached the network")

    monkeypatch.setattr(websocket, "create_connection", unexpected)
    vm = Arker(api_key="ark_test", base_url="https://example.invalid/api").vm("vm_1")
    with pytest.raises(ValueError, match="port"):
        vm.connect_port(port)


@pytest.mark.parametrize("frame", [(1, b"invalid"), (2, b"x" * 65537), (8, b"")])
def test_malformed_frames_fail_and_context_closes_socket(frame):
    sock = Socket()
    sock.frames = iter([frame])
    with pytest.raises(OSError), PortStream(sock) as stream:
        stream.recv(65536)
    assert sock.closed


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_invalid_timeout_never_opens_a_connection(monkeypatch, timeout):
    def unexpected(*args, **kwargs):
        raise AssertionError("invalid timeout reached the network")

    monkeypatch.setattr(websocket, "create_connection", unexpected)
    vm = Arker(api_key="ark_test", base_url="https://example.invalid").vm("test")
    with pytest.raises(ValueError, match="timeout"):
        vm.connect_port(8080, timeout=timeout)


def test_upgrade_refusal_preserves_api_error_without_retry(monkeypatch):
    calls = []

    def refused(*args, **kwargs):
        calls.append(args)
        raise websocket.WebSocketBadStatusException(
            "refused",
            status_code=404,
            resp_body=json.dumps(
                {
                    "error": {
                        "code": "not_found",
                        "message": "missing",
                        "timestamp": "2026-10-17T06:00:00Z",
                        "request_id": "req-port-test",
                        "request": {"kind": "matched", "operation_id": "openVmPort"},
                        "details": {"resource": "vm"},
                    }
                }
            ),
        )

    monkeypatch.setattr(websocket, "create_connection", refused)
    vm = Arker(api_key="ark_test", base_url="https://example.invalid").vm("test")
    with pytest.raises(ArkerError) as caught:
        vm.connect_port(8080)
    assert caught.value.code == "not_found"
    assert caught.value.status == 404
    assert caught.value.body.request.operation_id == "openVmPort"
    assert len(calls) == 1
