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
        assert kwargs == {"signatures": signatures, "api_evidence": "assertion"}
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


def test_runtime_trace_skips_unexecuted_calls_and_preserves_objects(harness):
    programs, _ = harness
    called, getters = set(), []
    marker = object()

    class Box:
        @property
        def name(self):
            getters.append("name")
            return marker

        def read(self, value):
            assert self is box
            return value

        def skipped(self):
            pytest.fail("unexecuted branch")

    box = Box()
    namespace = {"box": box, "marker": marker}
    source = """
assert box.name is marker
assert box.read(marker) is marker
assert box.read == box.read
if False:
    box.skipped()
"""
    exec(programs.trace_program(source, "observed", namespace, {"box": "Sandbox"}, called), namespace)
    assert called == {"Sandbox.name", "Sandbox.read"}
    assert getters == ["name"]
    assert namespace["box"] is box


def test_runtime_trace_does_not_credit_argument_or_getter_failure(harness):
    programs, _ = harness
    called = set()
    failure = RuntimeError("same failure")

    class Box:
        @property
        def name(self):
            raise failure

        def read(self, value):
            pytest.fail("argument failed before invocation")

    def broken():
        raise failure

    for source in ("box.name", "box.read(broken())"):
        namespace = {"box": Box(), "broken": broken}
        with pytest.raises(RuntimeError) as caught:
            exec(programs.trace_program(source, "observed", namespace, {"box": "Sandbox"}, called), namespace)
        assert caught.value is failure
    assert called == set()


def test_runtime_trace_credits_async_only_when_awaited_and_keeps_error_identity(harness):
    programs, _ = harness
    called = set()
    failure = RuntimeError("same failure")

    class Box:
        async def read(self):
            raise failure

    namespace = {"box": Box()}
    source = "unawaited = box.read()\nasync def run():\n    await box.read()"
    exec(programs.trace_program(source, "observed", namespace, {"box": "Sandbox"}, called), namespace)
    assert called == set()
    namespace["unawaited"].close()
    assert called == set()
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(namespace["run"]())
    assert caught.value is failure
    assert called == {"Sandbox.read"}


@pytest.mark.parametrize("case", ["stream-read", "stream-cancel"])
def test_core_program_observations_exclude_other_case_apis(harness, monkeypatch, case):
    import sys
    from types import SimpleNamespace

    programs, _ = harness
    scenario = importlib.import_module("test_e2b_core_extended_import_only")
    called, deleted = set(), []
    data = {}

    class Files:
        def write(self, path, content):
            data[path] = content

        def read(self, path, format=None):
            content = data[path]
            if format == "stream":
                return iter_chunks(content)
            return content

    def iter_chunks(content):
        yield content[:3]
        yield content[3:]

    box = SimpleNamespace(
        files=Files(),
        commands=SimpleNamespace(run=lambda command: SimpleNamespace(stdout="alive")),
        kill=lambda: deleted.append(True),
    )
    monkeypatch.setitem(
        sys.modules,
        "e2b",
        SimpleNamespace(
            Sandbox=SimpleNamespace(create=lambda: box),
            CommandExitException=RuntimeError,
            PtySize=object,
        ),
    )
    namespace = {"case": case}
    code = programs.trace_program(
        scenario.PROGRAM, "core-program", namespace, {"Sandbox": "Sandbox", "sandbox": "Sandbox"}, called
    )
    exec(code, namespace)
    assert called == {
        "Sandbox.create",
        "Sandbox.kill",
        "Sandbox.files.write",
        "Sandbox.files.read",
        "Sandbox.commands.run",
    }
    assert deleted == [True]
    assert namespace["sandbox"] is box


@pytest.mark.parametrize("kind", ["assertion", "runtime"])
def test_record_labels_api_attribution_without_attesting_signatures(harness, monkeypatch, tmp_path, kind):
    import json

    _, evidence = harness
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("COMPAT_COVERAGE_EVIDENCE", str(path))
    monkeypatch.setenv("COMPAT_COVERAGE_RUN", "local-observation-test")
    evidence.record("daytona", "sync", "reference", "example", ["Sandbox.id"], "passed", api_evidence=kind)
    event = json.loads(path.read_text())
    assert event["api_evidence"] == kind
    assert event["apis"] == ["python:daytona:sync:Sandbox.id"]
    assert event["signatures"] == {}
    with pytest.raises(ValueError, match="Unknown API evidence kind"):
        evidence.record("daytona", "sync", "reference", "example", [], "passed", api_evidence="inferred")
    assert len(path.read_text().splitlines()) == 1
