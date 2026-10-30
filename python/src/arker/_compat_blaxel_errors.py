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


def _transport_error(error):
    import httpx2

    if not isinstance(error, httpx2.RequestError):
        return None
    try:
        import httpx
    except ImportError:
        return None

    # Only matching built-in transport classes have the native constructor contract.
    names = (
        "RequestError",
        "TransportError",
        "TimeoutException",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "NetworkError",
        "ConnectError",
        "ReadError",
        "WriteError",
        "CloseError",
        "ProtocolError",
        "LocalProtocolError",
        "RemoteProtocolError",
        "ProxyError",
        "UnsupportedProtocol",
        "DecodingError",
        "TooManyRedirects",
    )
    for name in names:
        if type(error) is getattr(httpx2, name):
            native_type = getattr(httpx, name, None)
            break
    else:
        return None
    if not isinstance(native_type, type) or not issubclass(native_type, httpx.RequestError):
        return None
    try:
        try:
            request = error.request
        except RuntimeError:
            return native_type(str(error))
        # Diagnostic metadata must not duplicate credentials or replayable bodies.
        url = request.url.copy_with(username="", password="", query=None, fragment=None)
        diagnostic = httpx.Request(request.method, str(url))
        return native_type(str(error), request=diagnostic)
    except (TypeError, ValueError, AttributeError):
        return None


def translate_error(error, operation):
    cause = error.__cause__ if isinstance(error, ArkerError) and error.status == 0 else error
    translated = _transport_error(cause)
    if translated is not None:
        return translated
    if not isinstance(error, ArkerError):
        return error
    if operation == "fork" and error.status > 0:
        import json

        from blaxel.core.client import errors
        from blaxel.core.client.models import Error
        from blaxel.core.sandbox import SandboxAPIError

        body = {"code": error.status, "error": error.message}
        if error.status in (400, 404, 409, 500):
            return SandboxAPIError(
                error.message, status_code=error.status, code=error.message, error=Error.from_dict(body)
            )
        return errors.from_response(error.status, json.dumps(body).encode())
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
    import httpx2

    try:
        return call()
    except (ArkerError, httpx2.RequestError) as error:
        translated = translate_error(error, operation)
        if translated is error:
            raise
        raise translated from error
