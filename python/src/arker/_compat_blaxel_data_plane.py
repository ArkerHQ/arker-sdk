"""Scoped native Blaxel clients over the authenticated VM port transport."""

from __future__ import annotations

import hashlib
import importlib
import inspect
from importlib.metadata import version
from threading import RLock
from types import FunctionType, MappingProxyType, SimpleNamespace

import httpx

from ._compat_network import _AsyncTransport, _origin, _Transport

# These native implementations reference module-level HTTP factories and headers.
# Bind their unchanged code into subclasses; never mutate the vendor modules.
_SOURCES = {
    "sync.filesystem": "4f38db9e57de154c0fb89dcea616cdf3d41aaa2f853ec62258ca4e4d8364edc2",
    "sync.process": "ea7dc5859a19b45fdd8a27459d6bc121b949e2878c2f395375eb5f9a6f07d545",
    "default.filesystem": "c05a4cd504cf9b5c946f5de0d403c7293711cb863ef624e244ffa4f674be009b",
    "default.process": "35c20b97eed7fa54360c99c2f158444de494620a4a06c5a2ea6c8c1f759f2a17",
}


class BlaxelCleanupError(RuntimeError):
    def __init__(self, errors):
        super().__init__("Native Blaxel client cleanup failed")
        self.errors = tuple(errors)


class _Settings:
    headers = MappingProxyType({})

    def __init__(self, native):
        self.native = native

    def __getattr__(self, name):
        return getattr(self.native, name)


def _subclass(module, native, plane):
    namespace = {**vars(module), "settings": _Settings(module.settings)}
    namespace["httpx"] = SimpleNamespace(**{**vars(httpx), "Client": plane.client, "AsyncClient": plane.async_client})
    methods = {}
    for name, method in vars(native).items():
        if inspect.isfunction(method):
            bound = FunctionType(method.__code__, namespace, method.__name__, method.__defaults__, method.__closure__)
            bound.__kwdefaults__ = method.__kwdefaults__
            bound.__annotations__ = method.__annotations__
            methods[name] = bound
    # The native generated client has a separate factory from its direct HTTP calls.
    methods["get_api_client"] = lambda self: plane.api_client()
    methods["get_client"] = lambda self: plane.get_client()
    return type(f"Private{native.__name__}", (native,), methods)


def validate_native():
    if version("blaxel") != "0.4.21":
        raise RuntimeError("Native Blaxel data plane requires blaxel==0.4.21")
    modules = {name: importlib.import_module("blaxel.core.sandbox." + name) for name in _SOURCES}
    for name, module in modules.items():
        if hashlib.sha256(inspect.getsource(module).encode()).hexdigest() != _SOURCES[name]:
            raise RuntimeError("Native Blaxel transport implementation changed")
    return modules


class _DaemonTransport(httpx.BaseTransport, httpx.AsyncBaseTransport):
    def __init__(self, vm, transport):
        self.origin = _origin(vm)
        self.transport = transport(vm)

    def _request(self, request):
        url = request.url
        if (url.scheme, url.host, url.port) != (self.origin.scheme, self.origin.host, self.origin.port):
            raise httpx.UnsupportedProtocol("Request outside the private daemon origin is unsupported")
        # Select the transport port only after the native URL has been normalized.
        return httpx.Request(
            request.method,
            url.copy_with(raw_path=b"/port/49983" + url.raw_path),
            headers=request.headers,
            stream=request.stream,
            extensions=request.extensions,
        )

    def handle_request(self, request):
        return self.transport.handle_request(self._request(request))

    async def handle_async_request(self, request):
        return await self.transport.handle_async_request(self._request(request))

    def close(self):
        self.transport.close()

    async def aclose(self):
        await self.transport.aclose()


class BlaxelDataPlane:
    def __init__(self, vm, *, asynchronous=False):
        from blaxel.core.client.models import Sandbox
        from blaxel.core.sandbox.types import SandboxConfiguration

        modules = validate_native()
        self.vm, self.asynchronous = vm, asynchronous
        self.base_url = str(_origin(vm))
        self.clients, self.async_clients = set(), set()
        self._lock = RLock()
        self.closed = False
        self._persistent_async = None
        config = SandboxConfiguration(
            Sandbox.from_dict({"metadata": {"name": vm.id}, "spec": {}}), force_url=self.base_url
        )
        prefix, stem = ("default", "Sandbox") if asynchronous else ("sync", "SyncSandbox")
        process_module, fs_module = modules[prefix + ".process"], modules[prefix + ".filesystem"]
        self.process = _subclass(process_module, getattr(process_module, stem + "Process"), self)(config)
        self.fs = _subclass(fs_module, getattr(fs_module, stem + "FileSystem"), self)(config, self.process)

    def _options(self, kwargs, transport):
        if self.closed:
            raise RuntimeError("Native Blaxel data plane is closed")
        return {
            **kwargs,
            "base_url": self.base_url,
            "transport": _DaemonTransport(self.vm, transport),
            "trust_env": False,
            "follow_redirects": False,
            "headers": {
                key: value
                for key, value in httpx.Headers(kwargs.get("headers", {})).items()
                if key.lower() not in {"authorization", "x-blaxel-authorization", "x-blaxel-workspace", "x-api-key"}
            },
        }

    def client(self, **kwargs):
        owner = self

        class OwnedClient(httpx.Client):
            def __exit__(self, *args):
                try:
                    return super().__exit__(*args)
                finally:
                    owner.clients.discard(self)

            def close(self):
                try:
                    super().close()
                finally:
                    owner.clients.discard(self)

        with self._lock:
            client = OwnedClient(**self._options(kwargs, _Transport))
            self.clients.add(client)
        return client

    def async_client(self, **kwargs):
        owner = self

        class OwnedClient(httpx.AsyncClient):
            async def __aexit__(self, *args):
                try:
                    return await super().__aexit__(*args)
                finally:
                    owner.async_clients.discard(self)

            async def aclose(self):
                try:
                    await super().aclose()
                finally:
                    owner.async_clients.discard(self)

        with self._lock:
            client = OwnedClient(**self._options(kwargs, _AsyncTransport))
            self.async_clients.add(client)
        return client

    def get_client(self):
        if not self.asynchronous:
            return self.client()
        if self._persistent_async is None:
            self._persistent_async = self.async_client(timeout=httpx.Timeout(300.0, connect=10.0))
        if self.closed:
            raise RuntimeError("Native Blaxel data plane is closed")
        return self._persistent_async

    def api_client(self):
        from blaxel.core.sandbox.client.client import Client

        if self.closed:
            raise RuntimeError("Native Blaxel data plane is closed")
        plane = self

        class PrivateClient(Client):
            def get_async_httpx_client(self):
                if self._async_client is None:
                    self._async_client = plane.async_client()
                return self._async_client

            def get_httpx_client(self):
                if self._client is None:
                    self._client = plane.client()
                return self._client

        return PrivateClient(base_url=self.base_url, raise_on_unexpected_status=False)

    def _close_sync(self):
        with self._lock:
            self.closed = True
            clients = list(self.clients)
        errors = []
        for client in clients:
            try:
                client.close()
            except Exception as error:
                errors.append(error)
        return errors

    def close(self):
        errors = self._close_sync()
        if errors:
            raise BlaxelCleanupError(errors) from errors[0]

    async def aclose(self):
        errors = self._close_sync()
        for client in list(self.async_clients):
            try:
                await client.aclose()
            except Exception as error:
                errors.append(error)
        if errors:
            raise BlaxelCleanupError(errors) from errors[0]
