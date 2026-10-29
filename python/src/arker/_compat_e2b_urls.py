"""Path-scoped GET and multipart POST file capabilities."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode, urlsplit

from ._compat_e2b_data_plane import EnvdDescriptor
from .computer import ArkerError
from .generated.api_models import MatchedRequestContext, UnsupportedOperation, UnsupportedOperationDetails


def _request(path, user, operation, expiry, descriptor):
    if not isinstance(path, str) or "\0" in path or (not path and operation == "read") or len(path.encode()) > 4096:
        raise ValueError("path must be a string without NUL; downloads require a path")
    if user is not None and (not isinstance(user, str) or ":" in user or "\0" in user or len(user.encode()) > 256):
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


def _decode(value, limit):
    if not isinstance(value, str) or not 0 < len(value) <= limit or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid file delegation encoding")
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if _encode(decoded) != value:
        raise ValueError("Invalid file delegation encoding")
    return decoded


def _encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class _FileDelegation:
    def __init__(self, response):
        url = response["redemption_url"]
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "\\" in url
            or "?" in url
            or "#" in url
            or any(ord(c) <= 32 for c in url)
        ):
            raise ValueError("Invalid file delegation redemption URL")
        self.url = url
        self.context = response["context"]
        _decode(self.context, 4096)
        self._key = _decode(response["signing_key"], 43)
        if len(self._key) != 32:
            raise ValueError("Invalid file delegation key")
        self.operations = response["operations"]
        if (
            not isinstance(self.operations, list)
            or not self.operations
            or any(op not in ("read", "write") for op in self.operations)
            or len(set(self.operations)) != len(self.operations)
        ):
            raise ValueError("Invalid file delegation operations")
        self.operations = tuple(self.operations)
        self.expires_at = response["expires_at"]
        if self.expires_at is not None and (type(self.expires_at) is not int or self.expires_at < 0):
            raise ValueError("Invalid file delegation expiry")

    def sign(self, body):
        if body["operation"] not in self.operations:
            raise ValueError("File operation is not delegated")
        expiry = body.get("expires_at")
        if self.expires_at is not None and (
            int(time.time()) > self.expires_at or expiry is None or expiry > self.expires_at
        ):
            raise ValueError("File URL expiry exceeds delegation lifetime")
        payload = _encode(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode())
        message = self.context + "." + payload
        tag = _encode(hmac.digest(self._key, b"arker:file-transfer:v1\0" + message.encode("ascii"), "sha256"))
        ticket = "ark_file_v1_" + message + "." + tag
        if len(ticket) > 16384:
            raise ValueError("File capability is too large")
        return self.url + "?" + urlencode({"ticket": ticket})


@dataclass(frozen=True)
class _UnsupportedFileURLs:
    error: ArkerError = field(repr=False)

    def sign(self, body):
        raise self.error.with_traceback(None)


def _load_delegation(vm):
    try:
        response = vm._client._request(
            "POST", f"/v1/vms/{quote(vm.id, safe='')}/file-transfer-delegation", {}, base_url=vm.base_url
        )
    except ArkerError as error:
        body = error.body
        if (
            error.status == 422
            and error.code == "unsupported_operation"
            and isinstance(body, UnsupportedOperation)
            and body.code == "unsupported_operation"
            and isinstance(body.details, UnsupportedOperationDetails)
            and body.details.reason == "feature_not_supported"
            and isinstance(body.request, MatchedRequestContext)
            and body.request.kind == "matched"
            and body.request.operation_id == "createFileTransferDelegation"
        ):
            return _UnsupportedFileURLs(error)
        raise
    return _FileDelegation(response)


def prepare_file_urls(vm):
    """Acquire client-only signing authority during sandbox create/connect."""
    vm._e2b_file_delegation = _load_delegation(vm)


async def prepare_file_urls_async(vm):
    """Install authority only if the caller is still awaiting preparation."""
    delegation = await asyncio.to_thread(_load_delegation, vm)
    vm._e2b_file_delegation = delegation


class E2BFileURLs:
    """Mint revocable GET/multipart POST URLs for native file services.

    Omitted expiry has no time limit; explicit expiry is 0..4294967295 seconds.
    Revoking the issuing API key invalidates subsequent redemptions.
    Unsupported file URL capabilities raise the original API error locally.
    """

    def __init__(self, vm=None, native=None, descriptor: EnvdDescriptor | None = None):
        self.vm, self.native, self.descriptor = vm, native, descriptor

    def _mint(self, operation, path, user, expiry):
        if self.native is not None:
            method = self.native.download_url if operation == "read" else self.native.upload_url
            return method(path, user=user, use_signature_expiration=expiry)
        body = _request(path, user, operation, expiry, self.descriptor)
        delegation = getattr(self.vm, "_e2b_file_delegation", None)
        if delegation is None:
            raise RuntimeError("File URLs require prepared signing authority; use create or connect")
        return delegation.sign(body)

    def download_url(self, path, user=None, use_signature_expiration=None):
        return self._mint("read", path, user, use_signature_expiration)

    def upload_url(self, path, user=None, use_signature_expiration=None):
        return self._mint("write", path, user, use_signature_expiration)
