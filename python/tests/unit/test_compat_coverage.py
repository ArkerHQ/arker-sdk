from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "compat_coverage", Path(__file__).resolve().parents[2] / "scripts" / "compat_coverage.py"
)
coverage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coverage)


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
