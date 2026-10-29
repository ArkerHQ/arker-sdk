"""Lifecycle helpers for E2B adapters; routing remains the adapter's responsibility.

Arker's public ``idle`` state does not identify a paused sandbox. Explicit lifecycle
routes provide authoritative pause and resume results. Native lifecycle methods
are delegated without translating their values.

Pause and resume select only the reserved lifetime rule. The backend commits its
policy and pause marker together and fences scheduled deletion against that write.

Paused-operation semantics depend on backend admission rejecting the durable
pause marker with requirement explicit_resume. A client flag or GET-before-run
cannot enforce this across clients. Signed-file transport coverage and full
lifecycle parity still require backend and live differential validation.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from ._compat import UnsupportedCompatibilityOption
from .computer import ArkerError, _vm_path

LIFETIME_POLICY_ID = "computesdk-e2b-lifetime"
MAX_SESSION_SECONDS = 86_400


def _timeout(seconds: int, *, setting: bool = False) -> None:
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise TypeError("timeout must be an integer number of seconds")
    if seconds < 0 or (not setting and not 1 <= seconds <= MAX_SESSION_SECONDS):
        from e2b import SandboxException

        raise SandboxException("timeout is outside the supported range", status_code=400)


def _options(options: dict[str, Any], default=60) -> dict[str, Any]:
    unsupported = sorted(key for key, value in options.items() if key != "request_timeout" and value is not None)
    if unsupported:
        raise UnsupportedCompatibilityOption(f"Arker lifecycle does not support option(s): {', '.join(unsupported)}")
    timeout = options.get("request_timeout")
    if timeout is None:
        timeout = default
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
        from e2b import InvalidArgumentException

        raise InvalidArgumentException("request_timeout must be a finite non-negative number of seconds")
    return {"request_timeout": None if timeout == 0 else timeout}


def _control(call, *args, **kwargs):
    import httpx2

    try:
        return call(*args, **kwargs)
    except httpx2.TimeoutException as error:
        import httpx

        kinds = (
            (httpx2.ConnectTimeout, httpx.ConnectTimeout),
            (httpx2.ReadTimeout, httpx.ReadTimeout),
            (httpx2.WriteTimeout, httpx.WriteTimeout),
            (httpx2.PoolTimeout, httpx.PoolTimeout),
        )
        native_type = next((native for source, native in kinds if isinstance(error, source)), httpx.TimeoutException)
        try:
            original = error.request
        except RuntimeError:
            request = None
        else:
            request = httpx.Request(
                original.method,
                str(original.url),
                headers=original.headers.raw,
                extensions=dict(original.extensions),
                stream=original.stream,
            )
        raise native_type(str(error), request=request) from error


def configure_transport(client, request_timeout=None):
    """Install a private client's per-request default without changing its credentials."""
    from .computer import _UNSET

    normalized = _options({"request_timeout": request_timeout})["request_timeout"]
    client._e2b_request_timeout = 0 if normalized is None else normalized
    if hasattr(client, "_e2b_original_request"):
        return
    original = client._request
    client._e2b_original_request = original

    def request(*args, **kwargs):
        if kwargs.get("request_timeout", _UNSET) is _UNSET:
            kwargs["request_timeout"] = _options({}, client._e2b_request_timeout)["request_timeout"]
        return _control(original, *args, **kwargs)

    client._request = request
    original_download = client._download

    def download(url):
        return _control(original_download, url, **_options({}, client._e2b_request_timeout))

    client._download = download


def transport_timeout(vm):
    return getattr(getattr(vm, "_client", None), "_e2b_request_timeout", 60)


class Lifecycle:
    """Instance operations after provider ownership has been resolved.

    ``connect`` returns a VM or the native connection result. The caller wraps the
    VM in its Sandbox type. Deadline arithmetic and policy validation belong to the
    atomic backend lifecycle operation.
    """

    def __init__(self, vm: Any = None, native: Any = None) -> None:
        if (vm is None) == (native is None):
            raise ValueError("Supply exactly one of vm or native")
        self.vm = vm
        self.native = native

    def set_timeout(self, timeout: int, **opts: Any) -> Any:
        if self.native is not None:
            return self.native.set_timeout(timeout, **opts)
        request = _options(opts, transport_timeout(self.vm))
        _timeout(timeout, setting=True)
        from e2b import SandboxNotFoundException

        from ._compat_e2b_errors import translate_error
        from ._compat_e2b_inventory import timestamp

        try:
            response = self._request(
                "POST",
                "deadline",
                {
                    "policy_id": LIFETIME_POLICY_ID,
                    "timeout_seconds": timeout,
                    "max_session_duration_seconds": MAX_SESSION_SECONDS,
                },
                **request,
            )
            if response.get("vm_id") != self.vm.id:
                raise RuntimeError("Invalid Arker deadline response")
            timestamp(response.get("deadline_at"), "deadline response timestamp")
        except ArkerError as error:
            if error.status == 404 or translate_error(error) is not error:
                raise SandboxNotFoundException("Sandbox was not found or is paused") from error
            raise
        return None

    def connect(self, timeout: int | None = None, *, on_resume: str = "restore", **opts: Any) -> Any:
        if self.native is not None:
            return self.native.connect(timeout=timeout, on_resume=on_resume, **opts)
        request = _options(opts, transport_timeout(self.vm))
        if timeout is not None:
            _timeout(timeout)
        if on_resume not in ("restore", "reboot"):
            from e2b import InvalidArgumentException

            raise InvalidArgumentException("on_resume must be 'restore' or 'reboot'")
        body: dict[str, Any] = {
            "deadline": {"policy_id": LIFETIME_POLICY_ID, "timeout_seconds": 300 if timeout is None else timeout}
        }
        if on_resume == "reboot":
            body["mode"] = "filesystem"
        response = self._request("POST", "resume", body, **request)
        self._check_response(response, "running")
        if on_resume == "reboot" and response["mode"] != "filesystem":
            raise RuntimeError("Invalid Arker resume mode")
        return _control(self.vm.refresh, **request)

    def _request(self, method: str, operation: str, body: Any = None, **request) -> dict[str, Any]:
        return _control(
            self.vm._client._request,
            method,
            f"{_vm_path(self.vm.id)}/{operation}",
            body,
            base_url=self.vm.base_url,
            **request,
        )

    def _check_response(self, response: dict[str, Any], state: str) -> None:
        if (
            response.get("vm_id") != self.vm.id
            or response.get("state") != state
            or response.get("mode") not in ("full", "filesystem")
        ):
            raise RuntimeError("Invalid Arker lifecycle response")

    def pause(self, keep_memory: bool | None = None, *, mode: str | None = None, **opts: Any) -> Any:
        if self.native is not None:
            return self.native.pause(keep_memory=keep_memory, mode=mode, **opts)
        from e2b import InvalidArgumentException

        request = _options(opts, transport_timeout(self.vm))
        if mode is not None and keep_memory is not None:
            raise InvalidArgumentException("Pass either mode or keep_memory, not both")
        if mode is not None and mode not in ("full", "filesystem"):
            raise InvalidArgumentException("mode must be 'full' or 'filesystem'")
        if keep_memory is not None and not isinstance(keep_memory, bool):
            raise InvalidArgumentException("keep_memory must be a boolean")
        if self._state(**request) == "paused":
            return False
        selected = mode or ("filesystem" if keep_memory is False else "full")
        response = self._request(
            "POST", "pause", {"mode": selected, "deadline": {"policy_id": LIFETIME_POLICY_ID}}, **request
        )
        self._check_response(response, "paused")
        if type(response.get("already_paused")) is not bool or (
            not response["already_paused"] and response["mode"] != selected
        ):
            raise RuntimeError("Invalid Arker pause response")
        return not response["already_paused"]

    def beta_pause(self, keep_memory: bool | None = None, *, mode: str | None = None, **opts: Any) -> Any:
        if self.native is not None:
            return self.native.beta_pause(keep_memory=keep_memory, mode=mode, **opts)
        return self.pause(keep_memory, mode=mode, **opts)

    def is_running(self, request_timeout: float | None = None) -> bool:
        if self.native is not None:
            return self.native.is_running(request_timeout=request_timeout)
        import httpx
        from e2b.exceptions import format_request_timeout_error

        request = _options({"request_timeout": request_timeout}, transport_timeout(self.vm))
        try:
            return self._state(**request) == "running"
        except httpx.TimeoutException as error:
            raise format_request_timeout_error() from error
        except ArkerError as error:
            if error.status == 404:
                return False
            raise

    def _state(self, **request) -> str:
        return "paused" if self._read_lifecycle(**request)["customer_paused"] else "running"

    def _read_lifecycle(self, **request) -> dict[str, Any]:
        response = self._request("GET", "lifecycle", **request)
        if (
            response.get("vm_id") != self.vm.id
            or "mode" not in response
            or response["mode"] not in (None, "full", "filesystem")
        ):
            raise RuntimeError("Invalid Arker lifecycle response")
        if response.get("state") == "paused" and response.get("customer_paused") is True:
            self._check_response(response, "paused")
            return response
        if response.get("state") in ("running", "idle") and response.get("customer_paused") is False:
            return response
        raise RuntimeError("Inconsistent Arker lifecycle state")

    def get_info(self, **opts: Any) -> Any:
        if self.native is not None:
            return self.native.get_info(**opts)
        from e2b import SandboxNotFoundException

        from ._compat_e2b_inventory import sandbox_info

        request = _options(opts, transport_timeout(self.vm))
        try:
            current = _control(self.vm.refresh, **request)
            observed = Lifecycle(vm=current)._read_lifecycle(**request)
            return sandbox_info(current, observed)
        except ArkerError as error:
            if error.status == 404:
                raise SandboxNotFoundException("Sandbox was not found") from error
            raise


class AsyncLifecycle:
    """Await native methods directly; run synchronous Arker requests off-loop."""

    def __init__(self, vm: Any = None, native: Any = None) -> None:
        self._sync = Lifecycle(vm=vm, native=native)

    async def _invoke(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self._sync.native is not None:
            return await getattr(self._sync.native, method)(*args, **kwargs)
        return await asyncio.to_thread(getattr(self._sync, method), *args, **kwargs)

    async def set_timeout(self, timeout: int, **opts: Any) -> Any:
        return await self._invoke("set_timeout", timeout, **opts)

    async def connect(self, timeout: int | None = None, *, on_resume: str = "restore", **opts: Any) -> Any:
        return await self._invoke("connect", timeout=timeout, on_resume=on_resume, **opts)

    async def pause(self, keep_memory: bool | None = None, *, mode: str | None = None, **opts: Any) -> Any:
        return await self._invoke("pause", keep_memory=keep_memory, mode=mode, **opts)

    async def beta_pause(self, keep_memory: bool | None = None, *, mode: str | None = None, **opts: Any) -> Any:
        return await self._invoke("beta_pause", keep_memory=keep_memory, mode=mode, **opts)

    async def is_running(self, request_timeout: float | None = None) -> bool:
        return await self._invoke("is_running", request_timeout=request_timeout)

    async def get_info(self, **opts: Any) -> Any:
        return await self._invoke("get_info", **opts)
