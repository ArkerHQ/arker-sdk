"""Owned native/facade fork proof, including process handles and local URLs."""

import asyncio
import importlib
import os
import time
import uuid
from unittest.mock import patch

import httpx
import pytest
from compat_evidence import record_scenario
from e2b import SandboxQuery


class ForkFailures(RuntimeError):
    def __init__(self, errors):
        self.errors = tuple(errors)
        super().__init__(f"Fork differential/cleanup failed: {len(errors)} errors")


async def program(Sandbox, asynchronous):
    async def call(method, *args, **kwargs):
        if asynchronous:
            return await method(*args, **kwargs)
        return await asyncio.to_thread(method, *args, **kwargs)

    metadata = {"fork_case": uuid.uuid4().hex}
    owned, errors, samples = [], [], []
    pending = None
    path = "/home/user/fork-payload"
    ready = "/home/user/fork-memory"

    async def inventory():
        paginator = Sandbox.list(query=SandboxQuery(metadata=metadata))
        rows = []
        while paginator.has_next:
            rows.extend(await call(paginator.next_items))
        return {row.sandbox_id for row in rows}

    try:
        parent = await call(Sandbox.create, timeout=300, metadata=metadata, network={"allow_public_traffic": False})
        owned.append(parent)
        before = await call(parent.get_info)
        parent_token = parent.traffic_access_token
        assert isinstance(parent_token, str) and parent_token
        await call(parent.files.write, path, "before")
        process = await call(
            parent.commands.run,
            f'value=$(cat /proc/sys/kernel/random/uuid); printf %s "$value" > {ready}; '
            'read release; printf "%s:%s" "$value" "$release"',
            background=True,
            stdin=True,
            timeout=240,
        )
        deadline = time.monotonic() + 30
        while not await call(parent.files.exists, ready):
            assert time.monotonic() < deadline, "Background process did not publish its memory value"
            await asyncio.sleep(0.05)
        memory = await call(parent.files.read, ready)
        await call(process.disconnect)
        server = await call(
            parent.commands.run, "python3 -m http.server 8080 --directory /home/user", background=True, timeout=240
        )
        await call(server.disconnect)
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            host = "https://" + parent.get_host(8080) + "/fork-payload"
            deadline = time.monotonic() + 60
            while True:
                response = await client.get(host, headers={"e2b-traffic-access-token": parent_token})
                if response.status_code == 200:
                    assert response.text == "before"
                    break
                assert time.monotonic() < deadline, "Parent application did not become ready"
                await asyncio.sleep(0.1)
            pending = asyncio.create_task(call(parent.fork, count=2, timeout=180, request_timeout=120))
            while not pending.done():
                samples.append(await inventory())
                await asyncio.sleep(0.05)
            values = await pending
            owned.extend(value for value in values if not isinstance(value, Exception))
            pending = None
            assert len(values) == 2
            for value in values:
                if isinstance(value, Exception):
                    raise value
            expected = {sandbox.sandbox_id for sandbox in owned}
            assert samples, "Inventory was not observed during fork"
            assert all(ids <= expected for ids in samples), "Temporary checkpoint leaked into inventory"
            deadline = time.monotonic() + 30
            while await inventory() != expected:
                assert time.monotonic() < deadline, "Inventory did not expose exactly parent and children"
                await asyncio.sleep(0.1)
            tokens = [sandbox.traffic_access_token for sandbox in owned]
            assert all(isinstance(token, str) and token for token in tokens)
            assert len(set(tokens)) == 3, "Fork must rotate each child's private traffic token"
            await call(parent.files.write, path, "parent-after")
            for index, child in enumerate(values):
                info = await call(child.get_info)
                assert info.metadata == metadata
                assert await call(child.files.read, path) == "before"
                handle = await call(child.commands.connect, process.pid, timeout=30)
                await call(child.commands.send_stdin, process.pid, "child\n")
                result = await call(handle.wait)
                assert result.stdout == memory + ":child"
                assert result.exit_code == 0
                payload = f"child-{index}"
                await call(child.files.write, path, payload)
                # URL construction must not contact either control or guest APIs.
                with (
                    patch.object(httpx.Client, "send", side_effect=AssertionError("URL mint performed I/O")),
                    patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("URL mint performed I/O")),
                ):
                    url = child.download_url(path)
                    assert isinstance(url, str)
                response = await client.get(url)
                assert response.status_code == 200
                assert response.text == payload
                child_host = "https://" + child.get_host(8080) + "/fork-payload"
                for token in tokens:
                    response = await client.get(child_host, headers={"e2b-traffic-access-token": token})
                    assert response.status_code == (200 if token == child.traffic_access_token else 403)
                    if token == child.traffic_access_token:
                        assert response.text == payload
            assert await call(parent.files.read, path) == "parent-after"
            parent_handle = await call(parent.commands.connect, process.pid, timeout=30)
            await call(parent.commands.send_stdin, process.pid, "parent\n")
            assert (await call(parent_handle.wait)).stdout == memory + ":parent"
            after = await call(parent.get_info)
            assert (after.started_at, after.end_at) == (before.started_at, before.end_at)
            assert parent.traffic_access_token == parent_token
    except BaseException as error:
        errors.append(error)
    finally:
        # Inventory/assertion failure must still join fanout and own every result.
        if pending is not None:
            try:
                values = await pending
                owned.extend(value for value in values if not isinstance(value, Exception))
                errors.extend(value for value in values if isinstance(value, Exception))
            except BaseException as error:
                errors.append(error)
        for sandbox in reversed(owned):
            try:
                assert await call(sandbox.kill) is True
            except BaseException as error:
                errors.append(error)
    if errors:
        raise ForkFailures(errors) from errors[0]


@pytest.mark.parametrize("style", ["sync", "async"])
@pytest.mark.parametrize(
    "provider,shim", [("e2b", False), ("e2b", True), ("arker", True)], ids=["reference", "native", "arker"]
)
def test_fork_differential(style, provider, shim, live_router, monkeypatch):
    apis = [
        "Sandbox.create",
        "Sandbox.fork",
        "Sandbox.list",
        "Sandbox.get_info",
        "Sandbox.download_url",
        "Sandbox.kill",
        "Sandbox.commands.run",
        "Sandbox.commands.connect",
        "Sandbox.commands.send_stdin",
        "Sandbox.files.read",
        "Sandbox.files.write",
    ]
    with record_scenario("e2b", style, provider, shim, "import-only-atomic-fork", apis):
        for name in ("E2B_API_KEY", "ARKER_API_KEY", "COMPAT_COVERAGE_EVIDENCE", "COMPAT_COVERAGE_RUN"):
            assert os.environ.get(name, "").strip(), f"Missing live prerequisite: {name}"
        assert any(os.environ.get(name) for name in ("ARKER_SOURCE", "ARKER_SOURCE_VM", "ARKER_SOURCE_VM_ID"))
        if provider == "arker":
            monkeypatch.delenv("E2B_API_KEY")
        module = importlib.import_module("arker.e2b" if shim else "e2b")
        Sandbox = getattr(module, "AsyncSandbox" if style == "async" else "Sandbox")
        asyncio.run(program(Sandbox, style == "async"))
