from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "compat_coverage", Path(__file__).resolve().parents[2] / "scripts" / "compat_coverage.py"
)
coverage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coverage)


def test_inventory_includes_public_context_and_iteration_protocols():
    class Native:
        def __enter__(self): ...
        def __exit__(self, *args): ...
        def __iter__(self): ...
        def __next__(self): ...
        def _private(self): ...

    for name in ("__enter__", "__exit__", "__iter__", "__next__", "_private"):
        getattr(Native, name).__module__ = "e2b.example"
    names = {item["api"] for item in coverage.surfaces("e2b", "sync", "Sandbox", Native, None)}
    assert names == {f"Sandbox.{name}" for name in ("__enter__", "__exit__", "__iter__", "__next__")}


def test_evidence_requires_reference_and_both_routes_for_current_code_and_version():
    def event(route, **options):
        return {
            "run": "run-1",
            "scenario": "stdin",
            "route": route,
            "outcome": "passed",
            "apis": ["process.exec"],
            "vendor_version": "1",
            "implementation_fingerprint": "current",
            **options,
        }

    native = [event("reference"), event("native")]
    assert coverage.verified_scenarios("process.exec", native, "1", "current") == []
    complete = [*native, event("arker")]
    assert coverage.verified_scenarios("process.exec", complete, "1", "current") == ["run-1:stdin"]
    assert coverage.verified_scenarios("process.exec", complete, "2", "current") == []
    assert coverage.verified_scenarios("process.exec", complete, "1", "different") == []
    assert (
        coverage.verified_scenarios("process.exec", [*complete, event("arker", outcome="failed")], "1", "current") == []
    )
    assert coverage.verified_scenarios("process.exec", [*native, event("arker", run="run-2")], "1", "current") == []
    assert coverage.verified_scenarios("process.kill", complete, "1", "current") == []
    assert (
        coverage.verified_scenarios(
            "process.exec", [*complete, event("arker", outcome="failed", apis=[])], "1", "current"
        )
        == []
    )


def test_evidence_cannot_combine_colliding_run_and_scenario_names():
    events = [
        {
            "run": run,
            "scenario": scenario,
            "route": route,
            "outcome": "passed",
            "apis": ["process.exec"],
            "vendor_version": "1",
            "implementation_fingerprint": "current",
        }
        for run, scenario, route in [
            ("a:b", "c", "reference"),
            ("a:b", "c", "native"),
            ("a", "b:c", "arker"),
        ]
    ]
    assert coverage.verified_scenarios("process.exec", events, "1", "current") == []


@pytest.mark.parametrize(
    "field,value",
    [("run", ""), ("scenario", None), ("route", "other"), ("outcome", "skipped"), ("apis", "process.exec")],
)
def test_malformed_current_evidence_fails_closed(field, value):
    event = {
        "run": "run-1",
        "scenario": "exec",
        "route": "arker",
        "outcome": "passed",
        "apis": ["process.exec"],
        "vendor_version": "1",
        "implementation_fingerprint": "current",
        field: value,
    }
    with pytest.raises(ValueError, match="Invalid compatibility evidence"):
        coverage.verified_scenarios("process.exec", [event], "1", "current")


@pytest.mark.parametrize(
    "name", ["compat_coverage.py", "compat_evidence.py", "test_blaxel_import_only.py", "http-service.py"]
)
def test_evidence_fingerprint_includes_measurement_and_scenario_code(monkeypatch, name):
    original = Path.read_bytes
    before = coverage.implementation_fingerprint()

    def changed(path):
        content = original(path)
        return content + b"\n# changed test harness\n" if path.name == name else content

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert coverage.implementation_fingerprint() != before


def test_evidence_fingerprint_distinguishes_runtime_profiles(monkeypatch):
    before = coverage.implementation_fingerprint()
    monkeypatch.setattr(coverage, "runtime_info", lambda: {"version": "different-runtime"})
    assert coverage.implementation_fingerprint() != before


@pytest.mark.parametrize("change", ["source", "vendor"])
def test_evidence_rejects_sources_changed_during_a_live_scenario(monkeypatch, tmp_path, change):
    import runpy

    monkeypatch.setattr(coverage.importlib.metadata, "version", lambda _: "1")
    path = Path(__file__).resolve().parents[1] / "e2e" / "compat_evidence.py"
    helper = runpy.run_path(str(path))
    output = tmp_path / "evidence.jsonl"
    monkeypatch.setenv("COMPAT_COVERAGE_EVIDENCE", str(output))
    monkeypatch.setenv("COMPAT_COVERAGE_RUN", "changed-while-running")
    if change == "source":
        monkeypatch.setattr(runpy, "run_path", lambda _: {"implementation_fingerprint": lambda: "changed"})
    else:
        monkeypatch.setattr(coverage.importlib.metadata, "version", lambda _: "2")
    with pytest.raises(RuntimeError, match="changed during"):
        helper["record"]("blaxel", "sync", "arker", "fetch", ["SandboxInstance.fetch"], "passed")
    event = json.loads(output.read_text())
    assert event["outcome"] == "failed"
    assert event["implementation_fingerprint"] != "changed"
    assert event["vendor_version"] == "1"


def test_empty_vendor_surfaces_cannot_pass_the_strict_gate(monkeypatch, capsys):
    monkeypatch.setattr(coverage, "surfaces", lambda *args: iter(()))
    monkeypatch.setattr(coverage, "resolve", lambda reference: object())
    monkeypatch.setattr(coverage.importlib.metadata, "version", lambda provider: "1")
    monkeypatch.setattr(sys, "argv", ["compat_coverage.py", "--fail-on-unverified"])
    with pytest.raises(SystemExit, match="1"):
        coverage.main()
    report = json.loads(capsys.readouterr().out)
    assert report["complete_inventory"] is False
    assert len(report["missing_roots"]) == sum(len(roots) for roots in coverage.ROOTS.values())
