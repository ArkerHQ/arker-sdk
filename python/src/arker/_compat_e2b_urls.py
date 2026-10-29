"""Path-scoped GET and multipart POST file capabilities."""

from __future__ import annotations

import base64
import hashlib
import time
from urllib.parse import quote, urlencode, urlsplit

from ._compat_e2b_data_plane import EnvdDescriptor


def _request(path, user, operation, expiry, descriptor):
    if not isinstance(path, str) or "\0" in path or (not path and operation == "read"):
        raise ValueError("path must be a string without NUL; downloads require a path")
    if user is not None and (not isinstance(user, str) or ":" in user or "\0" in user):
        raise ValueError("user must be an account name without colon or NUL")
    if expiry is not None and (type(expiry) is not int or not 0 <= expiry <= 4294967295):
        raise ValueError("use_signature_expiration must be an unsigned 32-bit integer in seconds")
    if descriptor is None or not descriptor.envd_access_token:
        raise RuntimeError("File URLs require initialized sandbox file services")
    expires_at = None if expiry is None else int(time.time()) + expiry
    # Native signing binds the literal path and default-user marker. Resolution
    # belongs to the guest; normalizing here would change the signed authority.
    signed = f"{path}:{operation}:{user or ''}:{descriptor.envd_access_token}"
    if expires_at is not None:
        signed += f":{expires_at}"
    signature = "v1_" + base64.b64encode(hashlib.sha256(signed.encode()).digest()).decode().rstrip("=")
    body = {"path": path, "operation": operation, "envd_signature": signature}
    if user:
        body["user"] = user
    if expires_at is not None:
        body["expires_at"] = expires_at
    return body


def _url(base_url, response):
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Invalid file transfer API base URL")
    vm_id, ticket = response["vm_id"], response["ticket"]
    if not isinstance(vm_id, str) or not vm_id or not isinstance(ticket, str) or not ticket.startswith("ark_file_v1_"):
        raise ValueError("Invalid file transfer capability response")
    return f"{base_url.rstrip('/')}/v1/vms/{quote(vm_id, safe='')}/file-transfer?{urlencode({'ticket': ticket})}"


class E2BFileURLs:
    """Mint revocable GET/multipart POST URLs for native file services.

    Omitted expiry has no time limit; explicit expiry is 0..4294967295 seconds.
    Revoking the issuing API key invalidates subsequent redemptions.
    """

    def __init__(self, vm=None, native=None, descriptor: EnvdDescriptor | None = None):
        self.vm, self.native, self.descriptor = vm, native, descriptor

    def _mint(self, operation, path, user, expiry):
        if self.native is not None:
            method = self.native.download_url if operation == "read" else self.native.upload_url
            return method(path, user=user, use_signature_expiration=expiry)
        body = _request(path, user, operation, expiry, self.descriptor)
        response = self.vm._client._request(
            "POST",
            f"/v1/vms/{quote(self.vm.id, safe='')}/file-transfer-urls",
            body,
            base_url=self.vm.base_url,
        )
        return _url(self.vm.base_url, response)

    def download_url(self, path, user=None, use_signature_expiration=None):
        return self._mint("read", path, user, use_signature_expiration)

    def upload_url(self, path, user=None, use_signature_expiration=None):
        return self._mint("write", path, user, use_signature_expiration)
