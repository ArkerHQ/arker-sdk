"""Register a verified daemon while its bootstrap run remains active."""

import json
import time
from uuid import UUID

from ._compat_e2b_bootstrap import secret_sync


def registered_bootstrap(vm, command, request, version):
    run = vm.run(command, timeout=120, time_to_background=0, end_after_all_subprocesses=False)
    if run.type != "background":
        raise RuntimeError("Daemon bootstrap did not retain its activity guard")
    deadline = time.monotonic() + 120
    acknowledgement = request.rsplit(".", 1)[0] + ".registered"
    acknowledged = False
    failure = None
    try:
        while time.monotonic() < deadline:
            current = vm.get_run(run.run_id)
            output = current.stdout or ""
            if "\n" in output:
                if current.state != "running":
                    raise RuntimeError("Daemon bootstrap released its activity guard before registration")
                ready = json.loads(output.split("\n", 1)[0])
                if ready.get("ready") is not True or ready.get("version") != version:
                    raise RuntimeError("Native daemon did not establish readiness")
                identity = ready.get("activity", {})
                if (
                    type(identity.get("pid")) is not int
                    or identity["pid"] < 2
                    or type(identity.get("start_ticks")) is not int
                    or not 0 <= identity["start_ticks"] < 2**63
                    or not isinstance(identity.get("boot_id"), str)
                    or str(UUID(identity["boot_id"])) != identity["boot_id"]
                ):
                    raise RuntimeError("Invalid daemon activity identity")
                vm._client._request(
                    "POST",
                    f"/v1/vms/{vm.id}/daemon-activity",
                    {**{key: identity[key] for key in ("pid", "start_ticks", "boot_id")}, "intent_version": 1},
                    base_url=vm.base_url,
                    request_timeout=60,
                )
                secret_sync(vm, acknowledgement, b'{"registered":true}')
                acknowledged = True
                break
            if current.state not in ("running", "pending"):
                raise RuntimeError("Daemon bootstrap ended before registration")
            time.sleep(0.05)
        else:
            raise TimeoutError("Daemon bootstrap identity timed out")
        while time.monotonic() < deadline:
            current = vm.get_run(run.run_id)
            if current.state not in ("running", "pending"):
                if current.state != "completed" or current.exit_code != 0:
                    raise RuntimeError("Daemon bootstrap failed after registration")
                return ready
            time.sleep(0.05)
        raise TimeoutError("Daemon bootstrap acknowledgement timed out")
    except BaseException as error:
        failure = error
        raise
    finally:
        if not acknowledged:
            try:
                secret_sync(vm, acknowledgement, b'{"registered":false}')
            except Exception as error:
                if failure is None:
                    raise
                failure.cleanup_error = error
