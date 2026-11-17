"""Live tests for who may reach an E2B-compatible sandbox from outside:
private traffic with a traffic access token, and signed file URLs.

Run with:

    ARKER_API_KEY=... python -m pytest tests/e2e/test_e2b_access_e2e.py
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import pytest

from arker.e2b import NotSupportedException, Sandbox

if not os.environ.get("ARKER_API_KEY"):
    pytest.skip("live Arker credentials are not configured", allow_module_level=True)

if not os.environ.get("ARKER_BASE_URL"):
    os.environ.setdefault("ARKER_PROVIDER", "aws")
    os.environ.setdefault("ARKER_REGION", "us-east-1")

TOKEN_HEADER = "e2b-traffic-access-token"


def fetch(
    url: str, *, headers: dict[str, str] | None = None, data: bytes | None = None, content_type: str = ""
) -> tuple[int, bytes]:
    """The status and body, waiting out the moments a new rule or server takes to apply."""
    request_headers = {**(headers or {}), **({"Content-Type": content_type} if content_type else {})}
    status, body = 0, b""
    for _ in range(20):
        request = urllib.request.Request(
            url, data=data, headers=request_headers, method="POST" if data is not None else "GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            status, body = error.code, error.read()
            if status not in (502, 503):
                return status, body
        except OSError:
            pass
        time.sleep(1)
    return status, body


def refused_by(url: str, deadline: float) -> bool:
    """Whether ``url`` is refused before ``deadline``: Arker applies a rule's expiry within seconds of it."""
    while fetch(url)[0] == 200 and time.monotonic() < deadline:
        time.sleep(1)
    return fetch(url)[0] == 403


def multipart(name: str, data: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/form-data; boundary={boundary}"


def serve(sandbox: Sandbox, port: int) -> str:
    sandbox.files.write("/tmp/www/index.html", "served")
    sandbox.commands.run(f"cd /tmp/www && python3 -m http.server {port}", background=True, timeout=0)
    url = f"https://{sandbox.get_host(port)}/index.html"
    sandbox.commands.run(
        f"for _ in $(seq 50); do curl -sf localhost:{port} > /dev/null && exit 0; sleep 0.2; done; exit 1"
    )
    return url


def test_private_traffic_needs_the_token() -> None:
    with Sandbox.create(network={"allow_public_traffic": False}) as sandbox:
        token = sandbox.traffic_access_token
        assert token
        url = serve(sandbox, 8000)
        assert fetch(url, headers={TOKEN_HEADER: token}) == (200, b"served")
        assert fetch(url)[0] == 403
        assert fetch(url, headers={TOKEN_HEADER: token[:-1] + "x"})[0] == 403
        assert Sandbox.connect(sandbox.sandbox_id).traffic_access_token == token
        with pytest.raises(NotSupportedException):
            sandbox.update_network({"allow_public_traffic": True})
        # A fork keeps the token; a snapshot's sandboxes get their own, and the old one stops working.
        [fork] = sandbox.fork()
        try:
            assert fork.traffic_access_token == token
            fork_url = f"https://{fork.get_host(8000)}/index.html"
            assert fetch(fork_url, headers={TOKEN_HEADER: token}) == (200, b"served")
        finally:
            fork.kill()
        snapshot = sandbox.create_snapshot()
        try:
            with Sandbox.create(snapshot.snapshot_id, network={"allow_public_traffic": False}) as child:
                assert child.traffic_access_token not in (None, token)
                # The snapshot's port rule, with the old token, is not inherited.
                label, _, domain = child.arker.refresh().hostname.partition(".")
                child_url = f"https://{label}-8000.{domain}/index.html"
                assert fetch(child_url, headers={TOKEN_HEADER: token})[0] in (401, 403)
                assert child.get_host(8000) == f"{label}-8000.{domain}"
                assert fetch(child_url, headers={TOKEN_HEADER: child.traffic_access_token}) == (200, b"served")
                assert fetch(child_url, headers={TOKEN_HEADER: token})[0] == 403
        finally:
            Sandbox.delete_snapshot(snapshot.snapshot_id)


def test_public_traffic_by_default() -> None:
    with Sandbox.create() as sandbox:
        assert sandbox.traffic_access_token is None
        assert fetch(serve(sandbox, 8001)) == (200, b"served")


def test_signed_file_urls() -> None:
    with Sandbox.create() as sandbox:
        sandbox.files.write("notes/report.txt", "secret report")
        download = sandbox.download_url("notes/report.txt")
        assert fetch(download) == (200, b"secret report")
        # The URL names one file, with one token, for reading only.
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(download).query)
        assert query["path"] == ["/home/user/notes/report.txt"]
        other = download.replace(
            urllib.parse.quote("/home/user/notes/report.txt", safe=""), urllib.parse.quote("/etc/hostname", safe="")
        )
        assert other != download
        assert fetch(other)[0] == 403
        assert fetch(download.replace(query["signature"][0], query["signature"][0][:-1] + "x"))[0] == 403
        assert fetch(download.split("&signature=")[0])[0] == 403
        assert fetch(download + "&path=%2Fetc%2Fhostname")[0] in (400, 403)
        assert fetch(download, data=b"overwrite")[0] == 403

        upload = sandbox.upload_url("/tmp/up/data.bin")
        payload = bytes(range(256)) * 4
        body, content_type = multipart("data.bin", payload)
        status, reply = fetch(upload, data=body, content_type=content_type)
        assert status == 200, reply
        assert sandbox.files.read("/tmp/up/data.bin", format="bytes") == payload
        assert fetch(upload)[0] == 403

        expiring = sandbox.download_url("/home/user/notes/report.txt", use_signature_expiration=5)
        assert fetch(expiring) == (200, b"secret report")
        assert refused_by(expiring, deadline=time.monotonic() + 60)

        # A fork does not inherit the URLs, though its file server is running.
        [fork] = sandbox.fork()
        try:
            label, _, domain = fork.arker.refresh().hostname.partition(".")
            on_fork = urllib.parse.urlsplit(download)._replace(netloc=f"{label}-49983.{domain}").geturl()
            assert fork.commands.run("curl -s -o /dev/null -w '%{http_code}' localhost:49983/files").stdout == "400"
            assert fetch(on_fork)[0] in (401, 403)
        finally:
            fork.kill()
