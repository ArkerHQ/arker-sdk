"""Shared routing and cancellation contracts for async compatibility tests."""

import asyncio
import json
import threading
import types
from functools import wraps

import pytest


def run_async(test):
    """Give each async test a fresh loop while preserving pytest fixture discovery."""

    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return run


@pytest.fixture
def configure_router(tmp_path, monkeypatch):
    def configure(order, *, existing_strategy="priority", create_order=None, create=None, cache_path=None):
        path = tmp_path / "router.json"
        config = {
            "version": 1,
            "existing": {"strategy": existing_strategy, "order": order},
            "create": create if create is not None else {"strategy": "priority", "order": create_order or order},
        }
        if cache_path is not None:
            config["cache"] = {"path": str(cache_path)}
        path.write_text(json.dumps(config))
        monkeypatch.setenv("COMPUTESDK_ROUTER_CONFIG", str(path))
        monkeypatch.setenv("ARKER_API_KEY", "ark_test")
        return cache_path

    return configure


async def cancel_creation(creation, entered, finished):
    pending = asyncio.create_task(creation)
    await asyncio.wait_for(entered.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await asyncio.wait_for(finished.wait(), 1)


@pytest.fixture
def late_arker_creation(monkeypatch):
    def install(shim, *, boolean_delete=False):
        entered, release, deleted = threading.Event(), threading.Event(), threading.Event()

        def delete(**options):
            deleted.set()
            if boolean_delete:
                return types.SimpleNamespace(deleted=True)
            return None

        def create(*args, **kwargs):
            entered.set()
            assert release.wait(2)
            return types.SimpleNamespace(id="late", refresh=lambda **options: None, delete=delete)

        monkeypatch.setattr(shim, "arker_create", create)

        async def check(creation):
            pending = asyncio.create_task(creation)
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            finally:
                release.set()
            assert await asyncio.to_thread(deleted.wait, 1)

        return check

    return install
