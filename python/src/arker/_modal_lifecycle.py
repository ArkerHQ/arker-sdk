"""Read owner-authorized lifecycle results without treating absence as termination."""

from urllib.parse import quote

from ._compat import UnsupportedCompatibilityOption, arker_client, import_optional
from ._modal_errors import _missing_vm
from .computer import VM


def missing(sandbox_id):
    native = import_optional("modal.exception")
    if native is not None:
        return native.NotFoundError(f"Sandbox {sandbox_id} not found")
    from ._compat import format_not_found

    return format_not_found("Sandbox", sandbox_id)


def state(vm):
    try:
        response = vm._client._request(
            "GET", f"/v1/vms/{quote(vm.id, safe='')}/lifecycle?include_terminated=true", base_url=vm.base_url
        )
    except Exception as error:
        if _missing_vm(error):
            raise missing(vm.id) from error
        raise
    if response.get("vm_id") != vm.id or response.get("state") not in ("running", "idle", "paused", "terminated"):
        raise ValueError("Invalid sandbox lifecycle response")
    if response["state"] != "terminated":
        return None
    reason = response.get("termination_reason")
    if reason not in ("deadline", "requested", "unknown") or not response.get("terminated_at"):
        raise ValueError("Invalid retained sandbox lifecycle response")
    return reason


def retained(sandbox_id, config):
    vm = VM(arker_client(config), sandbox_id)
    # A handle is only returned after this server-authorized lookup succeeds.
    vm._modal_terminal_reason = state(vm)
    return vm


def returncode(reason):
    if reason is None:
        return None
    if reason == "deadline":
        return 124
    if reason == "requested":
        return 137
    raise UnsupportedCompatibilityOption("The retained sandbox has no recorded terminal result")


def completed(reason, raise_on_termination):
    native = import_optional("modal.exception")
    returncode(reason)  # Reject unknown historical outcomes, never invent an exit code.
    if reason == "deadline" or (reason == "requested" and raise_on_termination):
        if native is None:
            raise UnsupportedCompatibilityOption("Native Modal terminal exceptions require the modal package")
        raise (native.SandboxTimeoutError if reason == "deadline" else native.SandboxTerminatedError)()
