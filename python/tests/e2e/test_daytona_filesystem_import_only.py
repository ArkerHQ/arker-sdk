"""Daytona buffered filesystem differential scenario; requires live credentials."""

import asyncio
import inspect
import json
import os
import uuid

import pytest
from compat_evidence import record

APIS = [
    "Sandbox.fs.create_folder",
    "Sandbox.fs.upload_file",
    "Sandbox.fs.download_file",
    "Sandbox.fs.get_file_info",
    "Sandbox.fs.list_files",
    "Sandbox.fs.move_files",
    "Sandbox.fs.set_file_permissions",
    "Sandbox.fs.delete_file",
]


async def program(Client, progress=lambda stage: None):
    async def call(function, *args, **kwargs):
        result = function(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    progress("client-init")
    client = Client()
    sandbox = None
    primary = None
    try:
        progress("create")
        sandbox = await call(client.create)
        fs = sandbox.fs
        directory = "compat-files-" + uuid.uuid4().hex
        progress("create-folder")
        await call(fs.create_folder, directory, "700")
        path = directory + "/bytes"
        payload = bytes(range(256))
        progress("upload")
        await call(fs.upload_file, payload, path)
        progress("download")
        assert await call(fs.download_file, path) == payload
        progress("metadata")
        info = await call(fs.get_file_info, path)
        assert info.size == 256 and not info.is_dir
        # Compare filesystem ownership with the native/default process identity.
        progress("identity")
        identity = await call(sandbox.process.exec, "id -u")
        assert info.owner == identity.result.strip()
        progress("permissions")
        await call(fs.set_file_permissions, path, mode="640")
        assert (await call(fs.get_file_info, path)).permissions == "0640"
        progress("overwrite")
        await call(fs.upload_file, b"short", path)
        assert (await call(fs.get_file_info, path)).permissions == "0640"
        progress("move")
        await call(fs.move_files, path, directory + "/moved")
        progress("list")
        assert [v.name for v in await call(fs.list_files, directory)] == ["moved"]
        progress("delete-folder")
        await call(fs.delete_file, directory, recursive=True)
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup = None
        close = getattr(client, "close", None)
        actions = [("cleanup-delete", lambda: call(client.delete, sandbox))] if sandbox is not None else []
        if callable(close):
            actions.append(("cleanup-close", lambda: call(close)))
        for stage, action in actions:
            try:
                if primary is None and cleanup is None:
                    progress(stage)
                await action()
            except BaseException as error:
                if cleanup is None:
                    cleanup = error
        if primary is not None and cleanup is not None:
            primary.add_note("Owned-resource cleanup also failed; verify deletion independently.")
        if primary is None and cleanup is not None:
            raise cleanup


@pytest.mark.parametrize("style", ["sync", "async"])
@pytest.mark.parametrize("provider,shim", [("daytona", False), ("daytona", True), ("arker", True)])
def test_daytona_filesystem(style, provider, shim, tmp_path, monkeypatch):
    if not os.getenv("DAYTONA_API_KEY") or not os.getenv("ARKER_API_KEY"):
        pytest.skip("Live Daytona and Arker credentials required")
    import daytona as native

    from arker import daytona as translated

    config = tmp_path / "router.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "priority", "order": [provider]},
                "create": {"strategy": "priority", "order": [provider]},
            }
        )
    )
    monkeypatch.setenv("COMPUTESDK_ROUTER_CONFIG", str(config))
    if provider == "arker":
        monkeypatch.delenv("DAYTONA_API_KEY")
    client = getattr(translated if shim else native, "AsyncDaytona" if style == "async" else "Daytona")
    outcome = "failed"
    failure = None
    stage = "setup"

    def progress(value):
        nonlocal stage
        stage = value

    try:
        asyncio.run(program(client, progress))
        outcome = "passed"
    except BaseException as error:
        failure = error
    try:
        record(
            "daytona",
            style,
            "reference" if not shim else "arker" if provider == "arker" else "native",
            "buffered-filesystem",
            APIS,
            outcome,
        )
    except BaseException as error:
        if failure is None:
            failure = error
            stage = "evidence"
    if failure is not None:
        # Provider exceptions may carry request headers or credentials.
        notes = " Cleanup also failed." if getattr(failure, "__notes__", None) else ""
        raise AssertionError(
            f"Daytona filesystem {provider}/{style} failed at {stage}: {type(failure).__name__}.{notes}"
        ) from None
