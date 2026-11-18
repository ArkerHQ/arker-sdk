"""Live tests for signed URLs in the Daytona compatibility layer: file
download and upload URLs, and signed preview URLs.

Run with:

    ARKER_API_KEY=... python -m pytest tests/e2e/test_daytona_access_e2e.py
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
import uuid

import pytest

from arker.daytona import Daytona, DaytonaValidationError

if not os.environ.get("ARKER_API_KEY"):
    pytest.skip("live Arker credentials are not configured", allow_module_level=True)

if not os.environ.get("ARKER_BASE_URL"):
    os.environ.setdefault("ARKER_PROVIDER", "aws")
    os.environ.setdefault("ARKER_REGION", "us-east-1")


def fetch(url: str, *, data: bytes | None = None, content_type: str = "") -> tuple[int, bytes]:
    """The status and body, waiting out the moments a new rule or server takes to apply."""
    headers = {"Content-Type": content_type} if content_type else {}
    status, body = 0, b""
    for _ in range(20):
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
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
    return fetch(url)[0] in (401, 403)


@pytest.fixture(scope="module")
def sandbox():
    sandbox = Daytona().create()
    yield sandbox
    sandbox.delete()


def test_file_urls(sandbox) -> None:
    sandbox.fs.upload_file(b"daytona report", "/tmp/report.txt")
    download = sandbox.download_url("/tmp/report.txt")
    assert fetch(download) == (200, b"daytona report")
    assert fetch(download.replace("signature=", "signature=x"))[0] == 403
    assert fetch(download, data=b"overwrite")[0] == 403
    with pytest.raises(DaytonaValidationError):
        sandbox.download_url("report.txt")

    upload = sandbox.upload_url("/tmp/in/data.bin")
    boundary = uuid.uuid4().hex
    payload = bytes(range(256))
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="data.bin"\r\n\r\n'.encode()
        + payload
        + f"\r\n--{boundary}--\r\n".encode()
    )
    assert fetch(upload, data=body, content_type=f"multipart/form-data; boundary={boundary}")[0] == 200
    assert sandbox.fs.download_file("/tmp/in/data.bin") == payload

    short = sandbox.download_url("/tmp/in/data.bin", ttl_seconds=3)
    assert fetch(short) == (200, payload)
    assert refused_by(short, deadline=time.monotonic() + 60)

    forever = sandbox.download_url("/tmp/in/data.bin", ttl_seconds=0)
    assert fetch(forever) == (200, payload)
    sandbox.rotate_signing_key()
    assert fetch(download)[0] in (401, 403)
    assert fetch(forever)[0] in (401, 403)
    assert fetch(sandbox.download_url("/tmp/report.txt")) == (200, b"daytona report")


def test_signed_preview_urls(sandbox) -> None:
    sandbox.fs.upload_file(b"preview", "/tmp/www/index.html")
    sandbox.process.exec("cd /tmp/www && (nohup python3 -m http.server 3000 > /dev/null 2>&1 &)")
    sandbox.process.exec("for _ in $(seq 50); do curl -sf localhost:3000 > /dev/null && exit 0; sleep 0.2; done")
    assert sandbox.public is False
    signed = sandbox.create_signed_preview_url(3000, expires_in_seconds=600)
    assert (signed.sandbox_id, signed.port) == (sandbox.id, 3000)
    assert fetch(signed.url) == (200, b"preview")
    page = signed.url.replace("/?", "/index.html?")
    assert fetch(page) == (200, b"preview")
    assert fetch(signed.url.replace(signed.token, signed.token[:-1] + "x"))[0] in (401, 403)
    assert fetch(signed.url.split("?")[0])[0] in (401, 403)
    other = sandbox.create_signed_preview_url(3000)
    sandbox.expire_signed_preview_url(3000, signed.token)
    assert fetch(signed.url)[0] in (401, 403)
    assert fetch(other.url) == (200, b"preview")
    brief = sandbox.create_signed_preview_url(3000, expires_in_seconds=3)
    assert refused_by(brief.url, deadline=time.monotonic() + 60)
    # A fork does not inherit the signed URL, though it serves the port.
    fork = sandbox.fork()
    try:
        label, _, domain = fork.arker.refresh().hostname.partition(".")
        assert fetch(f"https://{label}-3000.{domain}/?arker_token={other.token}")[0] in (401, 403)
        assert fork.process.exec("curl -sf localhost:3000").result == "preview"
    finally:
        fork.delete()
