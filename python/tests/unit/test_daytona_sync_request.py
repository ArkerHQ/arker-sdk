import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpcore2._backends.sync
import httpx2 as httpx
import pytest
import urllib3.util.connection

from arker import _daytona_sync_request as transport
from arker import computer
from arker._daytona_request import request_timeout


@pytest.fixture
def server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            if self.path == "/slow":
                time.sleep(0.65)
            body = b"done"
            try:
                self.send_response(302 if self.path.endswith("/redirect") else 200)
                self.send_header("Location", "/must-not-follow")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_POST = do_GET

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=http.serve_forever)
    worker.start()
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{http.server_port}", requests=requests)
    finally:
        http.shutdown()
        http.server_close()
        worker.join()


@request_timeout
def request(url, method="GET", request_timeout=None):
    return computer._http(method, url, {"authorization": "Bearer synthetic"}, None)


def test_connect_elapsed_is_subtracted_from_read_budget(server, monkeypatch):
    errors = pytest.importorskip("daytona.common.errors")
    original = urllib3.util.connection.create_connection

    def slow(*args, **kwargs):
        time.sleep(0.65)
        return original(*args, **kwargs)

    monkeypatch.setattr(urllib3.util.connection, "create_connection", slow)
    with pytest.raises(errors.DaytonaConnectionTimeoutError):
        request(server.url + "/slow", request_timeout=1)
    assert len(server.requests) == 1


def test_phase_timeout_would_not_enforce_native_remaining_budget(server, monkeypatch):
    original = httpcore2._backends.sync.SyncBackend.connect_tcp

    def slow(*args, **kwargs):
        time.sleep(0.65)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpcore2._backends.sync.SyncBackend, "connect_tcp", slow)
    with httpx.Client(timeout=1) as client:
        assert client.get(server.url + "/slow").content == b"done"


def test_proxy_credentials_and_redirect_refusal(server, monkeypatch):
    proxy = server.url.replace("http://", "http://user:password@")
    monkeypatch.setattr(transport, "getproxies", lambda: {"http": proxy})
    monkeypatch.setattr(transport, "proxy_bypass", lambda host: False)
    status, _ = request("http://synthetic.invalid/redirect", request_timeout=1)
    assert status == 302
    assert len(server.requests) == 1
    path, headers = server.requests[0]
    assert path == "http://synthetic.invalid/redirect"
    assert headers["authorization"] == "Bearer synthetic"
    assert headers["proxy-authorization"] == "Basic dXNlcjpwYXNzd29yZA=="


def test_no_proxy_bypasses_proxy_and_tls_verification_remains_required(server, monkeypatch):
    contexts = []
    original = httpx.create_ssl_context

    def context(**options):
        result = original(**options)
        contexts.append((options, result))
        return result

    monkeypatch.setattr(transport.httpx, "create_ssl_context", context)
    monkeypatch.setattr(transport, "getproxies", lambda: {"http": "http://unreachable.invalid:1"})
    monkeypatch.setattr(transport, "proxy_bypass", lambda host: True)
    assert request(server.url, request_timeout=1)[0] == 200
    assert contexts[0][0] == {"verify": True, "trust_env": True}
    assert contexts[0][1].verify_mode == ssl.CERT_REQUIRED


def test_mutation_timeout_is_not_replayed(server, monkeypatch):
    errors = pytest.importorskip("daytona.common.errors")
    original = urllib3.util.connection.create_connection

    def slow(*args, **kwargs):
        time.sleep(0.65)
        return original(*args, **kwargs)

    monkeypatch.setattr(urllib3.util.connection, "create_connection", slow)
    with pytest.raises(errors.DaytonaConnectionTimeoutError):
        request(server.url + "/slow", "POST", request_timeout=1)
    assert len(server.requests) == 1


def test_untrusted_tls_rejected_and_custom_ca_honored(tmp_path, monkeypatch):
    import shutil
    import subprocess

    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is required to create the local TLS fixture")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"verified")

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    http.socket = context.wrap_socket(http.socket, server_side=True)
    worker = threading.Thread(target=http.serve_forever)
    worker.start()
    monkeypatch.setattr(transport, "proxy_bypass", lambda host: True)
    try:
        url = f"https://127.0.0.1:{http.server_port}"
        with pytest.raises(httpx.RequestError, match="HTTP transport failed"):
            request(url, request_timeout=1)
        monkeypatch.setenv("SSL_CERT_FILE", str(cert))
        assert request(url, request_timeout=1) == (200, b"verified")
    finally:
        http.shutdown()
        http.server_close()
        worker.join()
