import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from arker import computer
from arker._daytona_request import request_timeout


@pytest.fixture
def endpoint():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.command, self.path))
            if self.path.endswith("/fail") or (self.path.endswith("/recover") and len(calls) < 3):
                self.close_connection = True
                return
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body) + (10 if self.path.endswith("/body") else 0)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@request_timeout
def fetch(base_url, path, method="GET", request_timeout=1):
    return computer._request_json(
        method, path, base_url=base_url, retry=computer.RetryOptions(attempts=5, base_delay_s=0, jitter_s=0)
    )


def test_get_recovers_before_headers(endpoint):
    url, calls = endpoint
    assert fetch(url, "/recover") == {"ok": True}
    assert len(calls) == 3


def test_get_exhaustion_does_not_multiply_outer_retries(endpoint):
    errors = pytest.importorskip("daytona.common.errors")
    url, calls = endpoint
    with pytest.raises(errors.DaytonaError) as caught:
        fetch(url, "/fail")
    assert type(caught.value) is errors.DaytonaError
    assert len(calls) == 4


def test_received_body_is_not_replayed(endpoint):
    errors = pytest.importorskip("daytona.common.errors")
    url, calls = endpoint
    with pytest.raises(errors.DaytonaConnectionError):
        fetch(url, "/body")
    assert len(calls) == 1


def test_post_backed_metadata_remains_unknown_outcome_not_replayed(endpoint):
    url, calls = endpoint
    with pytest.raises(Exception) as caught:
        fetch(url, "/fail", method="POST")
    assert "unknown" in str(caught.value).lower()
    assert calls == [("POST", "/fail")]


@pytest.mark.parametrize(
    "path, error_name, attempts",
    [
        ("fail", "DaytonaError", 4),
        ("body", "DaytonaConnectionError", 1),
    ],
)
def test_pinned_native_retry_and_body_error_boundary(endpoint, path, error_name, attempts):
    native = pytest.importorskip("daytona_toolbox_api_client")
    import httpx2
    from daytona._sync.process import Process
    from daytona.common import errors

    url, calls = endpoint
    with native.ApiClient(native.Configuration(host=url)) as api, httpx2.Client() as http:
        process = Process("python", native.ProcessApi(api), http)
        with pytest.raises(getattr(errors, error_name)) as caught:
            process.get_session(path, request_timeout=1)
        assert type(caught.value) is getattr(errors, error_name)
    assert len(calls) == attempts
