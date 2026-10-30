from __future__ import annotations

import ast
import asyncio
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "e2e"))
    return importlib.import_module("compat_program"), importlib.import_module("compat_evidence")


@pytest.mark.parametrize("signatures", [None, {"Sandbox.create": ["(template=None)"]}])
@pytest.mark.parametrize("failure", [None, RuntimeError("program"), asyncio.CancelledError()])
@pytest.mark.parametrize("route,shim", [("blaxel", False), ("blaxel", True), ("arker", True)])
def test_scenario_records_completion_and_failure_identity(harness, monkeypatch, failure, route, shim, signatures):
    _, evidence = harness
    records, apis = [], set()

    def capture(*args, **kwargs):
        assert kwargs == {"signatures": signatures}
        records.append(args)

    monkeypatch.setattr(evidence, "record", capture)

    def run():
        with evidence.record_scenario("blaxel", "sync", route, shim, "example", apis, signatures=signatures):
            apis.add("Sandbox.create")
            if failure is not None:
                raise failure

    if failure is None:
        run()
    else:
        with pytest.raises(type(failure)) as caught:
            run()
        assert caught.value is failure
    assert records == [
        (
            "blaxel",
            "sync",
            "reference" if not shim else route if route == "arker" else "native",
            "example",
            ["Sandbox.create"],
            "passed" if failure is None else "failed",
        )
    ]


def test_cleanup_failure_cannot_record_a_passing_scenario(harness, monkeypatch):
    _, evidence = harness
    records = []
    monkeypatch.setattr(evidence, "record", lambda *args, **kwargs: records.append(args))
    with (
        pytest.raises(RuntimeError, match="cleanup"),
        evidence.record_scenario("modal", "sync", "arker", True, "cleanup", []),
    ):
        try:
            pass
        finally:
            raise RuntimeError("cleanup")
    assert records[0][-1] == "failed"


def test_async_derivation_awaits_only_named_targets_and_keeps_literals(harness):
    programs, _ = harness
    calls = []

    class AsyncSandbox:
        @classmethod
        async def create(cls):
            calls.append("create")
            return cls()

        async def read(self):
            calls.append("read")
            return "Sandbox"

        async def kill(self):
            calls.append("kill")

    source = """sandbox = Sandbox.create()
try:
    assert str(sandbox.read()).upper() == "SANDBOX"
finally:
    sandbox.kill()
"""
    derived = programs.async_program(source, rename={"Sandbox": "AsyncSandbox"}, targets={"AsyncSandbox", "sandbox"})
    namespace = {"AsyncSandbox": AsyncSandbox}
    exec(compile(derived, "derived-test", "exec"), namespace)
    asyncio.run(namespace["program"]())
    assert calls == ["create", "read", "kill"]
    assert sum(isinstance(node, ast.Await) for node in ast.walk(ast.parse(derived))) == 3
