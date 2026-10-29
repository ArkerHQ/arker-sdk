"""Client-side pinned artifact staging for guests with denied egress."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import tempfile
import time
import urllib.request
from contextlib import suppress
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import uuid4

_MAX_BYTES = 128 * 1024 * 1024


def envd_version() -> str:
    return json.loads(files("arker").joinpath("_guest_e2b_envd_manifest.json").read_text())["version"]


def stage_envd_artifact(vm: Any, root_path: str, arch: str, cache_directory: Path) -> str:
    """Verify the local cache, then use the SDK's bounded sync writer.

    Call after guest prepare and before ensure. The guest independently verifies
    this same hash and installs the candidate under its root lock. The returned
    path belongs in the ensure request as ``artifact_path``; it never names the running executable.
    No platform credential is used for the artifact download.
    """
    manifest = json.loads(files("arker").joinpath("_guest_e2b_envd_manifest.json").read_text())
    entry = manifest["artifacts"][arch]
    if root_path != "/root/.arker/e2b-envd":
        raise ValueError("Unexpected envd installation directory")
    digest = entry["sha256"]
    cache_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    cached = cache_directory / digest
    if cached.exists():
        with cached.open("rb") as source:
            content = source.read(_MAX_BYTES + 1)
        if len(content) > _MAX_BYTES or hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("Cached envd artifact checksum mismatch")
    else:
        deadline = time.monotonic() + 60
        content = bytearray()
        with urllib.request.urlopen(entry["url"], timeout=10) as response:
            while chunk := response.read(512 * 1024):
                content.extend(chunk)
                if len(content) > _MAX_BYTES or time.monotonic() > deadline:
                    raise ValueError("Envd artifact download limit exceeded")
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("Downloaded envd artifact checksum mismatch")
        fd, temporary = tempfile.mkstemp(dir=cache_directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(content)
            os.replace(temporary, cached)
        finally:
            Path(temporary).unlink(missing_ok=True)
    destination = f"{root_path}/bin/.candidate-{uuid4().hex}"
    try:
        vm.sync(destination, bytes(content))
        result = vm.run(f"chmod 0700 -- {shlex.quote(destination)}")
        if result.exit_code != 0:
            raise RuntimeError("Cannot set envd artifact permissions")
    except BaseException:
        with suppress(Exception):
            vm.run(f"rm -f -- {shlex.quote(destination)}")
        raise
    return destination
