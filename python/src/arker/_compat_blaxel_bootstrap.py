"""Prepare a pinned private daemon without changing VM policies or lifecycle."""

import json
import shlex
from contextlib import nullcontext
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from ._compat_blaxel_packages import staged_firewall_packages
from ._compat_daemon_activity import registered_bootstrap
from ._compat_e2b_artifact import stage_pinned_artifact
from ._compat_e2b_bootstrap import secret_sync
from ._compat_private_daemon import require_private_daemon

ROOT = "/root/.arker/blaxel"


def bootstrap_blaxel(vm, *, configuration=None, cache_directory=None):
    lifecycle = vm._client._request("GET", f"/v1/vms/{vm.id}/lifecycle", base_url=vm.base_url)
    if lifecycle.get("customer_paused") is not False:
        raise RuntimeError("Native daemon bootstrap requires an explicitly running VM")
    require_private_daemon(vm)
    package = files("arker")
    manifest = json.loads(package.joinpath("_guest_blaxel_manifest.json").read_text())
    for name in ["_guest_e2b_envd.py", "_guest_blaxel.py", "_guest_blaxel_packages.py", "_guest_blaxel_packages.json"]:
        secret_sync(vm, ROOT + "/" + name, package.joinpath(name).read_bytes())
    secret_sync(vm, ROOT + "/manifest.json", json.dumps(manifest).encode())
    secret_sync(vm, ROOT + "/LICENSE", manifest["license"].encode())

    def invoke(operation, argument=""):
        result = vm.run(
            f"python3 {ROOT}/_guest_blaxel.py {operation} {shlex.quote(argument)}",
            timeout=120,
            end_after_all_subprocesses=False,
        )
        if result.exit_code != 0:
            raise RuntimeError("Native daemon bootstrap failed: " + json.loads(result.stdout)["error"])
        return json.loads(result.stdout)

    prepared = invoke("prepare")
    cache_directory = cache_directory or Path.home() / ".cache/arker/blaxel"
    candidate = None
    request = ROOT + "/requests/" + uuid4().hex + ".json"
    failure = None
    try:
        if not prepared["verified"]:
            candidate = stage_pinned_artifact(
                vm,
                ROOT,
                manifest["artifacts"][prepared["arch"]],
                cache_directory,
            )
        packages = (
            staged_firewall_packages(vm, prepared["firewall_profile"], cache_directory)
            if prepared.get("firewall_verified") is False
            else nullcontext([])
        )
        with packages as candidates:
            secret_sync(
                vm,
                request,
                json.dumps(
                    {
                        "vm_id": vm.id,
                        "candidate": candidate,
                        "configuration": configuration,
                        "firewall_profile": prepared.get("firewall_profile"),
                        "package_candidates": candidates,
                    }
                ).encode(),
            )
            ready = registered_bootstrap(
                vm, f"python3 {ROOT}/_guest_blaxel.py ensure {request}", request, manifest["version"]
            )
            if ready.get("ready") is not True or ready.get("version") != manifest["version"]:
                raise RuntimeError("Native daemon did not establish readiness")
            return ready
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            cleanup = vm.run(
                "rm -f -- "
                + request
                + " "
                + request.rsplit(".", 1)[0]
                + ".registered"
                + (" " + candidate if candidate else ""),
                timeout=10,
            )
            if cleanup.exit_code != 0:
                raise RuntimeError("Native daemon staging cleanup failed")
        except Exception as error:
            if failure is None:
                raise
            failure.cleanup_error = error
