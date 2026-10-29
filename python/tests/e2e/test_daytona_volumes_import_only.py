"""Shared namespace and cross-sandbox filesystem differential workload."""

import asyncio
import inspect
import os
import time
import uuid

import pytest
from compat_evidence import record

APIS = [
    "Daytona.volume.create",
    "Daytona.volume.get",
    "Daytona.volume.list",
    "Daytona.create",
    "Daytona.delete",
    "Sandbox.fs.upload_file",
    "Sandbox.fs.download_file",
    "Sandbox.fs.move_files",
    "Sandbox.fs.delete_file",
]


async def program(Client, progress=lambda stage: None, *, cleanup_volume=None, scenario="shared-volume-filesystem"):
    from daytona import CreateSandboxFromSnapshotParams, VolumeMount
    from daytona.common.errors import DaytonaNotFoundError

    async def call(method, *args, **kwargs):
        result = method(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    client, volume, sandboxes = Client(), None, []
    name = "compat-volume-" + uuid.uuid4().hex
    mount = "/mnt/compat-volume"
    primary = None
    try:
        progress("create-volume")
        if scenario == "get-create-concurrency":

            async def concurrent_get():
                result = await asyncio.to_thread(client.volume.get, name, True)
                return await result if inspect.isawaitable(result) else result

            outcomes = await asyncio.gather(concurrent_get(), concurrent_get(), return_exceptions=True)
            successes = [result for result in outcomes if not isinstance(result, BaseException)]
            assert successes
            volume = successes[0]
            assert all(result.id == volume.id for result in successes)
            from daytona.common.errors import DaytonaBadRequestError

            for result in outcomes:
                if isinstance(result, BaseException):
                    assert isinstance(result, DaytonaBadRequestError) and result.status_code == 400
            with pytest.raises(DaytonaBadRequestError) as duplicate:
                await call(client.volume.create, name)
            assert duplicate.value.status_code == 400
            assert (await call(client.volume.get, name)).id == volume.id
            return
        volume = await call(client.volume.create, name)
        deadline = time.monotonic() + 60
        while volume.state.value != "ready":
            assert volume.error_reason is None
            if time.monotonic() >= deadline:
                raise AssertionError("Volume did not become ready")
            await asyncio.sleep(0.25)
            volume = await call(client.volume.get, name)
        progress("get-list")
        assert (await call(client.volume.get, name, create=True)).id == volume.id
        assert any(item.id == volume.id for item in await call(client.volume.list))
        for index in range(1 if scenario == "delete-mounted" else 2):
            progress("create-mounted-sandbox-" + str(index))
            sandbox = await call(
                client.create,
                CreateSandboxFromSnapshotParams(volumes=[VolumeMount(volume_id=volume.id, mount_path=mount)]),
            )
            sandboxes.append(sandbox)
        if scenario == "delete-mounted":
            from daytona.common.errors import DaytonaConflictError

            progress("delete-mounted")
            await call(sandboxes[0].fs.upload_file, b"retained", mount + "/marker")
            with pytest.raises(DaytonaConflictError) as caught:
                await call(client.volume.delete, volume)
            assert caught.value.status_code == 409
            assert (await call(client.volume.get, name)).state.value == "ready"
            assert await call(sandboxes[0].fs.download_file, mount + "/marker") == b"retained"
            return
        first, second = sandboxes
        payload = bytes(range(256)) * 4 + b"\0\xff"
        source, target = mount + "/payload", mount + "/renamed"

        async def visible(fs, path, expected):
            deadline = time.monotonic() + 30
            while True:
                try:
                    assert await call(fs.download_file, path) == expected
                    return
                except DaytonaNotFoundError:
                    if time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(0.25)

        async def absent(fs, path):
            deadline = time.monotonic() + 30
            while True:
                try:
                    await call(fs.download_file, path)
                except DaytonaNotFoundError:
                    return
                if time.monotonic() >= deadline:
                    raise AssertionError("Deleted path remains visible")
                await asyncio.sleep(0.25)

        progress("cross-vm-binary-write-read")
        await call(first.fs.upload_file, payload, source)
        await visible(second.fs, source, payload)
        progress("cross-vm-rename")
        await call(second.fs.move_files, source, target)
        await visible(first.fs, target, payload)
        await absent(first.fs, source)
        progress("cross-vm-unlink")
        await call(first.fs.delete_file, target)
        await absent(second.fs, target)
        progress("missing-volume-error")
        with pytest.raises(DaytonaNotFoundError):
            await call(client.volume.get, "missing-" + uuid.uuid4().hex)
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup_errors = []
        for sandbox in reversed(sandboxes):
            try:
                await call(client.delete, sandbox)
            except BaseException as error:
                cleanup_errors.append(error)
        if volume is not None:
            try:
                await call(cleanup_volume or client.volume.delete, volume)
            except BaseException as error:
                cleanup_errors.append(error)
        if cleanup_errors:
            raise RuntimeError("Owned volume or sandbox cleanup failed") from (primary or cleanup_errors[0])


@pytest.mark.parametrize("scenario", ["shared-volume-filesystem", "get-create-concurrency", "delete-mounted"])
@pytest.mark.parametrize("style", ["sync", "async"])
@pytest.mark.parametrize("provider,shim", [("daytona", False), ("daytona", True), ("arker", True)])
def test_daytona_volumes(scenario, style, provider, shim, live_router):
    if not os.getenv("DAYTONA_API_KEY") or not os.getenv("ARKER_API_KEY"):
        pytest.skip("Live Daytona and Arker credentials required")
    import daytona as native

    from arker import daytona as translated

    Client = getattr(translated if shim else native, "AsyncDaytona" if style == "async" else "Daytona")
    outcome, failure, stage = "failed", None, "setup"

    def progress(value):
        nonlocal stage
        stage = value

    try:
        cleanup_volume = None
        if provider == "arker":
            from arker._compat import arker_client

            client = arker_client({})
            # Teardown uses the explicit storage API after deleting owned VMs;
            # it supplies no evidence for Daytona's conditional deletion API.
            cleanup_volume = lambda volume: client.delete_filesystem(volume.id)
        asyncio.run(program(Client, progress, cleanup_volume=cleanup_volume, scenario=scenario))
        outcome = "passed"
    except BaseException as error:
        failure = error
    apis = APIS
    if scenario == "get-create-concurrency":
        apis = ["Daytona.volume.create", "Daytona.volume.get"]
    elif scenario == "delete-mounted":
        apis = [api for api in APIS if api not in {"Sandbox.fs.move_files", "Sandbox.fs.delete_file"}]
        apis = [*apis, "Daytona.volume.delete"]
    record(
        "daytona",
        style,
        "reference" if not shim else "arker" if provider == "arker" else "native",
        scenario,
        apis,
        outcome,
    )
    if failure is not None:
        raise AssertionError(f"Daytona volume {provider}/{style} failed at {stage}: {type(failure).__name__}") from None
