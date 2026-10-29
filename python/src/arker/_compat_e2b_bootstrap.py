"""Bootstrap native envd through authenticated sync, retaining private ingress."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import shlex
from contextlib import suppress
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from ._compat_e2b_artifact import envd_version, stage_envd_artifact
from ._compat_e2b_data_plane import EnvdDescriptor
from .generated.api_models import SyncChunkWrite

GUEST_SCRIPT = "/root/.arker-envd-bootstrap.py"
CACHE = Path.home() / ".cache" / "arker" / "envd"


def secret_sync(vm, path, data):
    """Small private control files only; binary staging uses bounded vm.sync."""
    if len(data) > 64 * 1024:
        raise ValueError("Bootstrap control file exceeds limit")
    result = vm._send_one_write(
        SyncChunkWrite(
            path=path,
            size=len(data),
            upload_id=uuid4().hex,
            content=base64.b64encode(data).decode(),
            start=0,
            end=len(data),
            is_secret=True,
        )
    )
    if not result.complete or not result.written:
        raise RuntimeError("Bootstrap secret sync did not complete")


def guest_json(vm, operation, argument):
    command = "python3 " + shlex.quote(GUEST_SCRIPT) + " " + operation + " " + shlex.quote(argument)
    result = vm.run(command, timeout=120, end_after_all_subprocesses=False)
    # Never forward daemon logs, request contents or arbitrary guest output.
    if result.exit_code != 0:
        raise RuntimeError("Guest bootstrap failed; no descriptor was produced")
    return json.loads(result.stdout)


def _value(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _require_private_envd(vm):
    policies = _value(vm.refresh(), "policies")
    rules = _value(policies, "rules", {}) or {}
    # Exposure unions rules before ordered request filtering; a private rule
    # cannot shadow another rule that exposes the control port.
    entries = list(rules.values()) + list(_value(policies, "policies", []) or [])
    for rule in entries:
        if (
            _value(rule, "type") != "inbound"
            or _value(rule, "action") == "deny"
            or _value(rule, "auth") in (None, "arker")
        ):
            continue
        ports = _value(_value(rule, "match"), "ports")
        covers = ports is None or any(
            port == 49983 if isinstance(port, int) else min(port) <= 49983 <= max(port) for port in ports
        )
        if covers:
            raise RuntimeError("Native envd requires owner-authenticated ingress on port 49983; incompatible VM policy")


def bootstrap_envd(vm, *, token=None, envs=None, cache_directory=CACHE):
    """Return (EnvdDescriptor, readiness) without changing network policies.

    Use only approved envd-free sources or same-trust reconnect/forks. The token
    is recovered through authenticated sync, never embedded in command text.
    Platform API credentials remain entirely in the caller's Arker client.
    """
    if envs is not None and (
        not isinstance(envs, dict)
        or not all(isinstance(key, str) and isinstance(value, str) for key, value in envs.items())
    ):
        raise TypeError("envs must be a mapping of strings to strings")
    _require_private_envd(vm)
    secret_sync(vm, GUEST_SCRIPT, files("arker").joinpath("_guest_e2b_envd.py").read_bytes())
    prepared = guest_json(vm, "prepare", vm.id)
    if token is None:
        source = prepared["existing_state_path"]
        if source is not None:
            token = json.loads(vm.sync(source))["token"]
        else:
            # A failed fresh start may have saved state before launching.
            exists = vm.run("test -f " + shlex.quote(prepared["state_path"]), timeout=10)
            if exists.exit_code not in (0, 1):
                raise RuntimeError("Cannot inspect envd state")
            token = (
                json.loads(vm.sync(prepared["state_path"]))["token"]
                if exists.exit_code == 0
                else secrets.token_urlsafe(32)
            )
    root = prepared["root_path"]
    secret_sync(
        vm,
        root + "/_guest_e2b_envd_manifest.json",
        files("arker").joinpath("_guest_e2b_envd_manifest.json").read_bytes(),
    )
    verified = guest_json(vm, "verify", vm.id)
    if type(verified.get("verified")) is not bool or verified.get("arch") != prepared["arch"]:
        raise RuntimeError("Guest artifact verification returned an invalid response")
    candidate = None if verified["verified"] else stage_envd_artifact(vm, root, prepared["arch"], cache_directory)
    request = prepared["request_directory"] + "/" + uuid4().hex + ".json"
    secret_sync(
        vm,
        request,
        json.dumps(
            {
                "vm_id": vm.id,
                "token": token,
                "artifact_path": candidate,
                "offline": True,
                **({"envs": envs} if envs is not None else {}),
            }
        ).encode(),
    )
    ready = guest_json(vm, "ensure", request)
    if ready.get("ready") is not True or ready.get("port") != 49983 or ready.get("version") != envd_version():
        raise RuntimeError("Guest bootstrap did not establish readiness")
    current = vm.refresh()
    first, rest = current.hostname.split(".", 1)
    descriptor = EnvdDescriptor(
        endpoint=f"https://{first}-49983.{rest}",
        envd_version=ready["version"],
        envd_access_token=token,
    )
    return descriptor, ready


async def bootstrap_envd_async(vm, *, token=None, envs=None, cache_directory=CACHE):
    """Run the synchronous VM bootstrap off-loop; drain it before cancellation."""
    task = asyncio.create_task(
        asyncio.to_thread(bootstrap_envd, vm, token=token, envs=envs, cache_directory=cache_directory)
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(Exception):
            await task
        raise
