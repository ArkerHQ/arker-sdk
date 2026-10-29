"""Persist Modal creation defaults in a private guest directory for reconnects."""

from __future__ import annotations

import json
import shlex
from importlib.resources import files

from ._compat import run_command

PATH = "/root/.arker-modal/defaults.json"
MARKER = "computesdk-modal-defaults"

# Sync carries the secret body; the command contains only filesystem checks.
_GUARD = files("arker").joinpath("_guest_modal_defaults.py").read_text()


def guard(vm, operation):
    result = run_command(vm, f"python3 -c {shlex.quote(_GUARD)} {operation}")
    if result.exit_code:
        raise RuntimeError("Cannot secure Modal creation defaults")


def write(vm, defaults):
    guard(vm, "prepare")
    vm.sync(PATH, json.dumps(defaults))
    guard(vm, "seal")


def read(vm):
    if not (getattr(vm, "metadata", None) or {}).get(MARKER):
        return {}
    guard(vm, "check")
    value = json.loads(vm.sync(PATH))
    if not isinstance(value, dict) or set(value) - {"env", "workdir"}:
        raise ValueError("Malformed Modal creation defaults")
    environment = value.get("env", {})
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in environment.items()
    ):
        raise ValueError("Malformed Modal creation environment")
    workdir = value.get("workdir", "/")
    if not isinstance(workdir, str) or not workdir.startswith("/"):
        raise ValueError("Malformed Modal creation working directory")
    return value


def prepare_exec(vm, argv, environment, workdir):
    import uuid

    if not environment and not (getattr(vm, "metadata", None) or {}).get(MARKER):
        return list(argv), None
    name = "exec-" + uuid.uuid4().hex + ".json"
    control = {
        "argv": list(argv),
        "env": environment or {},
        "workdir": workdir,
        "defaults": bool((getattr(vm, "metadata", None) or {}).get(MARKER)),
    }
    try:
        guard(vm, "prepare " + name)
        vm.sync("/root/.arker-modal/" + name, json.dumps(control))
        guard(vm, "seal " + name)
    except BaseException as error:
        discard_exec(vm, name, error)
        raise
    return ["python3", "-c", _GUARD, "exec", name], name


def discard_exec(vm, name, primary=None):
    if name is None:
        return
    try:
        guard(vm, "remove " + name)
    except BaseException as cleanup:
        if primary is not None:
            primary.vm_id = vm.id
            raise primary from cleanup
        raise
