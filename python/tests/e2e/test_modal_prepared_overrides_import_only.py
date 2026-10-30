"""Prepared startup overrides use identical workload arguments on every route."""

import asyncio
import os

import pytest
from compat_evidence import record_scenario

PROGRAM = r"""
import modal
import asyncio
import json
import time
import uuid

async def program(asynchronous, slow_build):
    async def call(method, *args, **options):
        if asynchronous:
            return await method.aio(*args, **options)
        return await asyncio.to_thread(method, *args, **options)
    app = await asyncio.to_thread(modal.App.lookup, "arker-compat-prepared", create_if_missing=True)
    startup = (
        "import os,time; "
        "assert os.getcwd() == '/tmp'; "
        "assert os.environ['PREPARED_SECRET'] == 'synthetic-only'; "
        "assert os.environ['PRECEDENCE'] == 'creation'; "
        "open('/tmp/prepared-started','w').write('startup-ok'); time.sleep(300)"
    )
    delay = 35 if slow_build else 0
    image = modal.Image.from_registry("python:3.12-slim-bookworm", setup_dockerfile_commands=[
        f"RUN sleep {delay} && touch /tmp/build-{uuid.uuid4().hex}",
        "ENTRYPOINT []", "CMD " + json.dumps(["python", "-c", startup]),
    ])
    secret = modal.Secret.from_dict({"PREPARED_SECRET": "synthetic-only", "PRECEDENCE": "secret"})
    started = time.monotonic()
    box = await call(modal.Sandbox.create, app=app, image=image, secrets=[secret],
                     env={"PRECEDENCE": "creation"}, workdir="/tmp", timeout=30)
    print(json.dumps({"event": "allocated", "id": box.object_id}), flush=True)
    try:
        if slow_build:
            assert time.monotonic() - started >= delay
        found = await call(modal.Sandbox.from_id, box.object_id)
        check = (
            "import os,pathlib,time; p=pathlib.Path('/tmp/prepared-started'); "
            "deadline=time.monotonic()+10\n"
            "while not p.exists() and time.monotonic()<deadline: time.sleep(.1)\n"
            "assert p.read_text() == 'startup-ok'; "
            "assert os.getcwd() == '/tmp'; "
            "assert os.environ['PREPARED_SECRET'] == 'synthetic-only'; "
            "assert os.environ['PRECEDENCE'] == 'exec'; time.sleep(5); print('prepared-ok')"
        )
        process = await call(found.exec, "python", "-c", check, env={"PRECEDENCE": "exec"})
        assert await call(process.stdout.read) == "prepared-ok\n"
        assert await call(process.wait) == 0
    finally:
        await call(box.terminate)
        print(json.dumps({"event": "cleanup_requested", "id": box.object_id}), flush=True)
"""


@pytest.mark.parametrize("provider,shim", [("modal", False), ("modal", True), ("arker", True)])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("slow_build", [False, True])
def test_modal_prepared_overrides_import_only(provider, shim, asynchronous, slow_build, live_router, monkeypatch):
    pytest.importorskip("modal")
    required = ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET") if provider == "modal" else ("ARKER_API_KEY",)
    if not all(os.environ.get(key) for key in required):
        pytest.skip("Live credentials required")
    if provider == "arker":
        monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
        monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    source = PROGRAM.replace("import modal\n", "from arker import modal\n", 1) if shim else PROGRAM
    namespace = {}
    exec(compile(source, "modal_prepared_overrides.py", "exec"), namespace)
    with record_scenario(
        "modal",
        "async" if asynchronous else "sync",
        provider,
        shim,
        "prepared-startup-overrides-slow" if slow_build else "prepared-startup-overrides",
        ["Sandbox.create", "Sandbox.from_id", "Sandbox.exec", "Sandbox.terminate"],
    ):
        asyncio.run(namespace["program"](asynchronous, slow_build))
