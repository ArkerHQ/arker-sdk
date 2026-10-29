"""Blaxel control-plane error shapes without hiding uncertain outcomes."""

from .computer import ArkerError


def creation_error(message, status, code):
    from blaxel.core.sandbox import SandboxAPIError

    body = {"code": code, "message": message, "status_code": status}
    return SandboxAPIError(message, status_code=status, code=code, body=body)


def not_found():
    from blaxel.core.sandbox import SandboxAPIError

    body = {"code": 404, "error": "Sandbox not found"}
    return SandboxAPIError(body["error"], status_code=404, code=body["error"], body=body)


def translate_error(error, operation):
    if not isinstance(error, ArkerError):
        return error
    if operation in ("get", "delete") and error.status == 404 and error.code == "not_found":
        return not_found()
    # A transport failure or timeout does not prove the creation was cancelled.
    if error.status not in (401, 403):
        return error
    from blaxel.core.sandbox import SandboxAPIError

    if operation == "create":
        return creation_error(error.message, error.status, error.code)
    body = {"code": error.status, "error": error.message}
    code = error.message
    return SandboxAPIError(error.message, status_code=error.status, code=code, body=body)


def control_call(operation, call):
    try:
        return call()
    except ArkerError as error:
        translated = translate_error(error, operation)
        if translated is error:
            raise
        raise translated from error
