"""The file server behind signed file URLs, run inside a sandbox by the compatibility layers.

Arker's inbound rules decide who reaches it: each signed URL is a rule that
lets in one method, on /files, with one path and one token, until it expires.
This server only reads and writes the file, and refuses a query that names a
parameter twice, so the path it serves is the one the rule matched. GET
returns the file; POST writes the body, or the first file of a
multipart/form-data body, to it. Keep it standalone: it runs on the sandbox's
Python, which may be older than this SDK's. The TypeScript SDK carries a copy.
"""

import json
import os
import shutil
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def multipart_file(content_type, body):
    """The first file in a multipart/form-data body, else its first part."""
    params = dict(param.strip().split("=", 1) for param in content_type.split(";")[1:] if "=" in param)
    boundary = params.get("boundary", "").strip('"').encode()
    if not boundary:
        return None
    parts = []
    for part in body.split(b"--" + boundary)[1:-1]:
        head, _, data = part.partition(b"\r\n\r\n")
        parts.append((head, data[:-2] if data.endswith(b"\r\n") else data))
    files = [data for head, data in parts if b"filename=" in head]
    return (files or [data for _, data in parts] or [None])[0]


class Handler(BaseHTTPRequestHandler):
    def target(self):
        url = urllib.parse.urlsplit(self.path)
        pairs = urllib.parse.parse_qsl(url.query, keep_blank_values=True)
        names = [name for name, _ in pairs]
        if url.path != "/files" or len(names) != len(set(names)) or "path" not in names:
            self.reply(400, {"message": "Expected /files with each query parameter once, path among them."})
            return None
        return dict(pairs)["path"]

    def reply(self, status, body, content_type="application/json"):
        data = json.dumps(body).encode() if content_type == "application/json" else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.target()
        if path is None:
            return
        try:
            handle = open(path, "rb")  # noqa: SIM115 - closed below
        except OSError as error:
            self.reply(404, {"message": f"Cannot read {path}: {error.strerror}"})
            return
        with handle:
            name = urllib.parse.quote(os.path.basename(path))
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(os.fstat(handle.fileno()).st_size))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{name}")
            self.end_headers()
            shutil.copyfileobj(handle, self.wfile)

    def do_POST(self):
        path = self.target()
        if path is None:
            return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        content_type = self.headers.get("Content-Type") or ""
        if content_type.startswith("multipart/form-data"):
            body = multipart_file(content_type, body)
            if body is None:
                self.reply(400, {"message": "The multipart body has no file."})
                return
        try:
            os.makedirs(os.path.dirname(path) or "/", exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(body)
        except OSError as error:
            self.reply(400, {"message": f"Cannot write {path}: {error.strerror}"})
            return
        self.reply(200, [{"name": os.path.basename(path), "type": "file", "path": path}])

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1])), Handler).serve_forever()
