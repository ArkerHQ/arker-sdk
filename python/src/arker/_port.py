from __future__ import annotations

import io
import socket
import threading
from contextlib import suppress

from typing_extensions import Self


class PortStream:
    """A private VM byte stream with independent read/write half-close."""

    def __init__(self, websocket) -> None:
        self._websocket = websocket
        self._pending = bytearray()
        self._read_eof = False
        self._write_eof = False
        self._closed = False
        self._read_lock = threading.Lock()
        self._write_lock = threading.Lock()

    def sendall(self, data: bytes | bytearray | memoryview) -> None:
        with self._write_lock:
            if self._closed or self._write_eof:
                raise BrokenPipeError("Private port write side is closed")
            view = memoryview(data)
            for start in range(0, len(view), 65536):
                self._websocket.send_binary(view[start : start + 65536].tobytes())

    def recv(self, size: int) -> bytes:
        if size < 0:
            raise ValueError("receive size must be nonnegative")
        if size == 0:
            return b""
        with self._read_lock:
            if self._closed:
                raise OSError("Private port is closed")
            while not self._pending and not self._read_eof:
                opcode, data = self._websocket.recv_data()
                if opcode == 2:
                    if len(data) > 65536:
                        raise OSError("Private port frame exceeds 65536 bytes")
                    self._pending.extend(data)
                elif opcode == 1 and data in ("eof", b"eof"):
                    self._read_eof = True
                else:
                    raise ConnectionResetError("Unexpected private port frame or close")
            data = bytes(self._pending[:size])
            del self._pending[:size]
            return data

    def shutdown(self, how: int = socket.SHUT_WR) -> None:
        if how == socket.SHUT_RDWR:
            self.close()
            return
        if how != socket.SHUT_WR:
            raise ValueError("Private port supports write half-close or full close")
        with self._write_lock:
            if self._closed:
                raise OSError("Private port is closed")
            if not self._write_eof:
                self._websocket.send("eof")
                self._write_eof = True

    def makefile(self, mode: str = "rb", buffering: int = -1):
        if mode != "rb":
            raise ValueError("Private port makefile supports binary reads only")
        reader = _PortReader(self)
        return (
            reader
            if buffering == 0
            else io.BufferedReader(reader, io.DEFAULT_BUFFER_SIZE if buffering < 0 else buffering)
        )

    def settimeout(self, timeout: float | None) -> None:
        self._websocket.settimeout(timeout)

    def gettimeout(self) -> float | None:
        return self._websocket.gettimeout()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._websocket.close()

    def abort(self) -> None:
        """Close immediately and wake concurrent reads, without a close handshake."""
        if not self._closed:
            self._closed = True
            with suppress(OSError):
                try:
                    self._websocket.abort()
                finally:
                    self._websocket.shutdown()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class _PortReader(io.RawIOBase):
    def __init__(self, stream: PortStream):
        self._stream = stream

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        data = self._stream.recv(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            super().close()
