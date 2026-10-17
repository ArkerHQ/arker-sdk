"""Isolated HTTP origin shared by both languages' live compatibility scenarios."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(207)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Echo-Authorization", self.headers.get("Authorization", ""))
        self.send_header("X-Echo-Key", self.headers.get("x-api-key", ""))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.send_header("X-Head", "kept")
        self.end_headers()

    def do_GET(self):
        if self.path in ("/redirect", "/root-redirect"):
            self.send_response(302)
            self.send_header("Location", "chunked" if self.path == "/redirect" else "/chunked")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/quiet":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"1\r\nx\r\n")
            self.wfile.flush()
            return
        self.send_response(418)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.write(b"3\r\neof\r\n3\r\n\x00\xff\x01\r\n0\r\n\r\n")
        self.wfile.flush()


server = ThreadingHTTPServer(("0.0.0.0", 18082), Handler)
print("fetch-service-ready", flush=True)
server.serve_forever()
