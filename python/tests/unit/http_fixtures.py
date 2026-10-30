"""Local HTTP server lifetime and body framing for transport tests."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class HTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_body(self, payload, *, status=200, content_type="application/json", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def read_body(self):
        # Drain chunked RPC requests before replying to avoid connection resets.
        chunks = []
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            while size := int(self.rfile.readline().split(b";", 1)[0], 16):
                chunk = self.rfile.read(size)
                assert len(chunk) == size
                assert self.rfile.read(2) == b"\r\n"
                chunks.append(chunk)
            while self.rfile.readline() not in (b"\r\n", b""):
                pass
        else:
            size = int(self.headers.get("Content-Length", "0"))
            chunks.append(self.rfile.read(size))
            assert len(chunks[0]) == size
        return b"".join(chunks)


@pytest.fixture
def http_server():
    servers = []

    def start(handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return server

    yield start
    for server, thread in reversed(servers):
        server.shutdown()
        server.server_close()
        thread.join()
