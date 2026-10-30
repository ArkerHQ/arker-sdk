"""Keep native daemon work runnable until explicit pause, expiry, or daemon exit.

This deliberately keeps an otherwise idle sandbox active. The monitor owns a
host run, not the daemon: failed reconnects must not cancel a shared lifetime.
"""

import hashlib
import json
import shlex
import time


def retain_envd(vm):
    from ._compat_e2b_bootstrap import GUEST_SCRIPT, guest_json

    def inspect(**values):
        return guest_json(vm, "lifetime", json.dumps({"vm_id": vm.id, **values}))

    current = inspect()
    if current["session_id"] is None:
        candidate = vm.create_session().session_id
        # If this response is lost, the candidate might have won. Keep it for
        # reconciliation rather than deleting a possibly shared session.
        current = inspect(incarnation=current["incarnation"], session_id=candidate)
        if current["session_id"] != candidate:
            vm.delete_session(candidate)
    incarnation = current["incarnation"]
    if incarnation["vm_id"] != vm.id or not current["session_id"]:
        raise RuntimeError("Invalid envd lifetime identity")
    argument = json.dumps(
        [vm.id, incarnation["pid"], incarnation["start"], incarnation["boot_id"], current["session_id"]],
        separators=(",", ":"),
    )
    command = "python3 " + shlex.quote(GUEST_SCRIPT) + " monitor " + shlex.quote(argument)
    run = vm.run(
        command,
        session_id=current["session_id"],
        timeout=0,
        time_to_background=0,
        end_after_all_subprocesses=True,
        idempotency_key=hashlib.sha256(argument.encode()).hexdigest(),
    )
    if run.type != "background":
        raise RuntimeError("Envd lifetime run is not active")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        status = vm.get_run(run.run_id)
        if status.vm_id != vm.id or status.session_id != current["session_id"]:
            raise RuntimeError("Envd lifetime run belongs to a different VM or session")
        if status.state not in ("pending", "running"):
            raise RuntimeError("Envd lifetime run ended before readiness")
        output = status.stdout or ""
        if "\n" in output:
            if status.state != "running" or json.loads(output.split("\n", 1)[0]) != {
                "ready": True,
                "lifetime": current,
            }:
                raise RuntimeError("Envd lifetime acknowledgement does not match")
            if guest_json(vm, "publish", argument) != {"ready": True}:
                raise RuntimeError("Envd lifetime ingress was not released")
            return run.run_id
        time.sleep(0.05)
    raise TimeoutError("Envd lifetime readiness timed out")
