from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import arker.blaxel as blaxel
from arker._compat import UnsupportedCompatibilityOption


@pytest.mark.parametrize("asynchronous", [False, True])
def test_atomic_creation_forwards_one_native_create_without_lookup(monkeypatch, asynchronous):
    calls = []
    native = SimpleNamespace(metadata=SimpleNamespace(name="named"))

    class Native:
        @staticmethod
        def create(sandbox, **kwargs):
            calls.append((sandbox, kwargs))
            return native

        @staticmethod
        def get(*args, **kwargs):
            pytest.fail("atomic creation must not perform a preliminary lookup")

    monkeypatch.setattr(blaxel.SyncSandboxInstance, "_native_class", staticmethod(lambda: Native))
    monkeypatch.setattr(blaxel, "routed_create", lambda vendor, config, callbacks: (vendor, callbacks[vendor]()))
    monkeypatch.setattr(blaxel, "routed_get", lambda *a, **k: pytest.fail("unexpected lookup"))
    request = {"name": "named", "memory": 2048}
    if asynchronous:

        async def wrap(cls, sandbox):
            return sandbox

        monkeypatch.setattr(blaxel.SandboxInstance, "_wrap", classmethod(wrap))
        result = asyncio.run(blaxel.SandboxInstance.create_if_not_exists(request, timeout=37))
    else:
        result = blaxel.SyncSandboxInstance.create_if_not_exists(request, timeout=37)
    assert result._native is native
    assert calls == [(request, {"create_if_not_exist": True, "timeout": 37})]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_arker_atomic_creation_refuses_before_lookup_or_fork(monkeypatch, asynchronous):
    monkeypatch.setattr(blaxel, "routed_create", lambda vendor, config, callbacks: ("arker", callbacks["arker"]()))
    monkeypatch.setattr(blaxel, "routed_get", lambda *a, **k: pytest.fail("unexpected lookup"))
    monkeypatch.setattr(blaxel, "arker_create", lambda *a, **k: pytest.fail("unexpected fork"))
    with pytest.raises(UnsupportedCompatibilityOption, match="create_if_not_exist"):
        if asynchronous:
            asyncio.run(blaxel.SandboxInstance.create_if_not_exists({"name": "named"}))
        else:
            blaxel.SyncSandboxInstance.create_if_not_exists({"name": "named"})


def test_native_atomic_creation_error_is_not_retried_as_plain_create(monkeypatch):
    refusal = RuntimeError("name cannot be used")
    calls = []

    class Native:
        @staticmethod
        def create(sandbox, **kwargs):
            calls.append(kwargs)
            raise refusal

    monkeypatch.setattr(blaxel.SyncSandboxInstance, "_native_class", staticmethod(lambda: Native))
    monkeypatch.setattr(blaxel, "routed_create", lambda vendor, config, callbacks: (vendor, callbacks[vendor]()))
    with pytest.raises(RuntimeError) as error:
        blaxel.SyncSandboxInstance.create_if_not_exists({"name": "named"})
    assert error.value is refusal
    assert calls == [{"create_if_not_exist": True}]
