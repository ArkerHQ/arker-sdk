"""Bounded, explicit opt-in live probe for Arker mount primitives."""

from __future__ import annotations

import time
import uuid

from arker import ArkerError


def replayable_failure(error):
    return isinstance(error, (TypeError, TimeoutError)) or (isinstance(error, ArkerError) and error.status >= 500)


def exact_filesystem(client, name):
    return [item for item in client.list_filesystems(name_prefix=name, limit=100).filesystems if item.name == name]


def run_probe(client, *, source="ubuntu-full", timeout=180, nonce=None, sleep=time.sleep):
    nonce = nonce or uuid.uuid4().hex
    filesystem_name = f"primitive-probe-{nonce}"
    fork_key = f"primitive-probe-{nonce}"
    mount_path = f"/mnt/{filesystem_name}"
    marker = f"arker-filesystem-probe:{nonce}\n"
    filesystem_id = None
    vm = None
    mount_id = None
    cleanup = {}
    result = {
        "scope": "Arker filesystem and mount primitives only",
        "filesystem_name": filesystem_name,
        "fork_replay_key": fork_key,
    }
    try:
        try:
            filesystem = client.create_filesystem(name=filesystem_name)
        except Exception as create_error:
            # This POST has no idempotency key. Never retry it: recover only a
            # single exact nonce name belonging to this caller and placement.
            matches = exact_filesystem(client, filesystem_name)
            if len(matches) != 1:
                result["unknown_filesystem_create"] = True
                raise RuntimeError(
                    f"Filesystem create outcome is unknown; exact-name matches={len(matches)}"
                ) from create_error
            filesystem = matches[0]
            result["filesystem_create_recovered"] = True
        filesystem_id = filesystem.filesystem_id
        result["filesystem_id"] = filesystem_id

        fork_request = {
            "source_vm_name": source,
            "source_org_name": "ArkerHQ",
            "name": fork_key,
            "description": "Bounded filesystem primitive probe",
            "layers": ["disk"],
            "resources": {"vcpu": 2, "memory_mib": 2048},
            "idempotency_key": fork_key,
        }
        for attempt in range(3):
            try:
                vm = client.fork(**fork_request)
                break
            except Exception as error:
                if not replayable_failure(error) or attempt == 2:
                    raise
        if vm is None or not vm.id.startswith("vmh-"):
            raise RuntimeError("Fork did not return a path-safe VM ID")
        result["vm_id"] = vm.id

        # The endpoint is idempotent for the same filesystem and path.
        for attempt in range(3):
            try:
                mount = vm.create_mount(filesystem_id=filesystem_id, path=mount_path)
                break
            except Exception as error:
                if not replayable_failure(error) or attempt == 2:
                    raise
        else:
            raise RuntimeError("Mount creation did not return a result")
        mount_id = mount.mount_id
        result["mount_id"] = mount_id

        reconcile = vm.run("true", timeout=120, idempotency_key=f"primitive-run-{nonce}")
        if reconcile.type != "completed" or reconcile.exit_code != 0:
            raise RuntimeError("Mount reconciliation run did not complete successfully")
        result["reconcile_run"] = "completed"

        deadline = time.monotonic() + timeout
        states = []
        while True:
            matches = [
                item
                for item in vm.list_mounts(filesystem_id=filesystem_id, limit=100).mounts
                if item.mount_id == mount_id and item.path == mount_path
            ]
            if len(matches) != 1:
                raise RuntimeError("Owned mount could not be resolved uniquely")
            mount = matches[0]
            states.append(mount.state)
            result["mount_states"] = states
            if mount.state is None:
                raise RuntimeError("Mount response omitted its state")
            if mount.state != "attaching":
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("Mount did not become ready within the bounded timeout")
            sleep(2)
        if mount.state != "mounted":
            raise RuntimeError(f"Mount failed: {mount.state}: {mount.state_detail or ''}")

        marker_path = f"{mount_path}/marker.txt"
        vm.sync(marker_path, marker)
        if bytes(vm.sync(marker_path)).decode() != marker:
            raise RuntimeError("Mounted filesystem content did not round-trip")
        result.update(mounted=True, content_round_trip=True, success=True)
    except Exception as error:
        result["success"] = False
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        if vm is not None and mount_id is not None:
            try:
                cleanup["mount"] = "deleted" if vm.delete_mount(mount_id).deleted else "not-deleted"
            except Exception as error:
                cleanup["mount"] = f"failed:{type(error).__name__}"
        if vm is not None:
            try:
                vm.delete()
                cleanup["vm"] = "deleted"
            except Exception as error:
                cleanup["vm"] = f"failed:{type(error).__name__}"
        if filesystem_id is not None:
            try:
                deleted = client.delete_filesystem(filesystem_id)
                cleanup["filesystem"] = "deleted" if deleted.deleted else "not-deleted"
            except Exception as error:
                cleanup["filesystem"] = f"failed:{type(error).__name__}"
        result["cleanup"] = cleanup
    return result
