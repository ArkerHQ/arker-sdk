from __future__ import annotations

import importlib.util
import inspect
import json
import runpy
import sys
import types
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "compat_coverage", Path(__file__).resolve().parents[2] / "scripts" / "compat_coverage.py"
)
coverage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coverage)


def test_core_scope_excludes_only_app_and_function_surfaces():
    names = [
        "App.lookup",
        "ModalClient.apps.fromName",
        "ModalClient.functions.fromName",
        "ModalClient.cls.fromName",
        "ModalClient.cpClient.appGetInfo",
        "ModalClient.cpClient.functionGet",
        "ModalClient.cpClient.classGet",
    ]
    retained = [
        "Sandbox.create",
        "Image.run_function",
        "Volume.commit",
        "Secret.from_name",
        "ModalClient.cpClient.sandboxCreate",
        "ModalClient.cpClient.secretGet",
        "ModalClient.cloudBucketMounts.create",
        "DriveInstance.create",
    ]
    rows = [{"provider": "modal", "api": name} for name in names + retained]
    selected, excluded = coverage.select_scope(rows, "core-sandbox")
    assert [row["api"] for row in selected] == retained
    assert [row["api"] for row in excluded] == names
    assert all(row["exclusion_reason"] for row in excluded)
    assert coverage.select_scope(rows, "expanded")[0] == rows
    with pytest.raises(ValueError):
        coverage.select_scope(rows, "unknown")


def test_missing_evidence_run_id_fails_before_starting_live_programs(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPAT_COVERAGE_EVIDENCE", str(tmp_path / "evidence.jsonl"))
    monkeypatch.delenv("COMPAT_COVERAGE_RUN", raising=False)
    with pytest.raises(RuntimeError, match="COMPAT_COVERAGE_RUN"):
        runpy.run_path(str(Path(__file__).resolve().parents[1] / "e2e" / "compat_evidence.py"))


def test_inventory_includes_blaxel_sandbox_storage_secrets_and_images():
    roots = {(style, prefix) for style, prefix, _, _ in coverage.ROOTS["blaxel"]}
    for style in ("sync", "async"):
        for prefix in ("DriveInstance", "VolumeInstance", "SecretInstance", "Snapshot"):
            assert (style, prefix) in roots
    assert ("sync", "ImageInstance") in roots


def test_blaxel_import_shim_keeps_core_resource_exports():
    native = pytest.importorskip("blaxel.core")
    from arker import blaxel

    for name in (
        "DriveInstance",
        "SyncDriveInstance",
        "VolumeInstance",
        "SyncVolumeInstance",
        "SecretInstance",
        "ImageInstance",
    ):
        assert getattr(blaxel, name) is getattr(native, name)


def test_inventory_includes_modal_returned_process_and_stream_surfaces():
    roots = {(style, prefix, native) for style, prefix, native, _ in coverage.ROOTS["modal"]}
    for style in ("sync", "async"):
        assert (style, "ContainerProcess", "modal.container_process:ContainerProcess") in roots
        assert (style, "ContainerProcess.stdout", "modal.io_streams:StreamReader") in roots
        assert (style, "ContainerProcess.stderr", "modal.io_streams:StreamReader") in roots
        assert (style, "ContainerProcess.stdin", "modal.io_streams:StreamWriter") in roots


def test_inventory_includes_e2b_returned_stream_input_handle():
    roots = {(style, prefix, native) for style, prefix, native, _ in coverage.ROOTS["e2b"]}
    assert ("async", "CommandInputHandle", "e2b:AsyncCommandInputHandle") in roots


@pytest.mark.parametrize("style", ["sync", "async"])
def test_e2b_explicit_pty_roots_override_property_walk(monkeypatch, capsys, style):
    native = pytest.importorskip(f"e2b.sandbox_{style}.commands.pty").Pty
    root = (style, "Sandbox.pty", f"e2b.sandbox_{style}.commands.pty:Pty", f"e2b.sandbox_{style}.commands.pty:Pty")
    assert root in coverage.ROOTS["e2b"]
    roots = [entry for entry in coverage.ROOTS["e2b"] if entry[0] == style and entry[1] in ("Sandbox", "Sandbox.pty")]
    assert roots[-1] == root
    sandbox_root = roots[0]
    walked = [
        row
        for row in coverage.surfaces(
            "e2b", style, "Sandbox", coverage.resolve(sandbox_root[2]), coverage.resolve(sandbox_root[3])
        )
        if row["api"].startswith("Sandbox.pty.")
    ]
    assert walked and all(not row["declared_by_shim"] for row in walked)
    monkeypatch.setattr(coverage, "ROOTS", {"e2b": roots})
    monkeypatch.setattr(coverage, "implementation_fingerprint", lambda: "unit-test")
    monkeypatch.setattr(sys, "argv", ["compat_coverage.py"])
    coverage.main()
    report = json.loads(capsys.readouterr().out)
    assert report["complete_inventory"] is True
    rows = [row for row in report["apis"] if row["api"].startswith("Sandbox.pty.")]
    methods = {name for name, member in inspect.getmembers(native, callable) if not name.startswith("_")}
    assert methods == {"create", "connect", "resize", "send_stdin", "kill", "stream_input"}
    expected_ids = {f"python:e2b:{style}:Sandbox.pty.{name}" for name in methods}
    assert {row["id"] for row in walked} == expected_ids
    assert {row["id"] for row in rows} == expected_ids
    assert len(rows) == len(expected_ids)
    shim = coverage.resolve(root[3])
    for row in rows:
        method = row["api"].removeprefix("Sandbox.pty.")
        assert row["declared_by_shim"] is True
        assert row["signatures"] == coverage.signatures(getattr(native, method))
        assert row["shim_signatures"] == coverage.signatures(getattr(shim, method))
        assert row["status"] == "unverified"


@pytest.mark.parametrize("api", ["Volume.create", "Volume.destroy", "AsyncVolume.create", "Volume.nonexistent"])
def test_e2b_async_volume_evidence_matches_only_real_vendor_names(monkeypatch, tmp_path, capsys, api):
    native = pytest.importorskip("e2b")
    root = ("async", "Volume", "e2b:AsyncVolume", "arker.e2b:AsyncVolume")
    assert root in coverage.ROOTS["e2b"]
    assert coverage.resolve(root[2]) is native.AsyncVolume
    monkeypatch.setattr(coverage, "ROOTS", {"e2b": [root]})
    monkeypatch.setattr(coverage, "implementation_fingerprint", lambda: "unit-test")
    version = coverage.importlib.metadata.version("e2b")
    evidence = tmp_path / "evidence.jsonl"
    evidence.write_text(
        "\n".join(
            json.dumps(
                {
                    "provider": "e2b",
                    "run": "unit-test",
                    "scenario": "volume-mount",
                    "route": route,
                    "outcome": "passed",
                    "apis": [f"python:e2b:async:{api}"],
                    "vendor_version": version,
                    "implementation_fingerprint": "unit-test",
                }
            )
            for route in ("reference", "native", "arker")
        )
    )
    monkeypatch.setattr(sys, "argv", ["compat_coverage.py", "--evidence", str(evidence)])
    coverage.main()
    report = json.loads(capsys.readouterr().out)
    assert report["complete_inventory"] is True
    rows = {row["id"]: row for row in report["apis"]}
    for method in ("create", "destroy"):
        assert inspect.iscoroutinefunction(getattr(native.AsyncVolume, method))
        assert f"python:e2b:async:Volume.{method}" in rows
    assert all(hasattr(native.AsyncVolume, row["api"].removeprefix("Volume.")) for row in rows.values())
    verified = {name for name, row in rows.items() if row["status"] == "scenario_verified"}
    expected = {f"python:e2b:async:{api}"} if api in ("Volume.create", "Volume.destroy") else set()
    assert verified == expected
    assert "python:e2b:async:AsyncVolume.create" not in rows
    assert "python:e2b:async:Volume.nonexistent" not in rows


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
    "name",
    [
        "compat_coverage.py",
        "compat_evidence.py",
        "test_blaxel_import_only.py",
        "http-service.py",
        "api-scope.json",
        "_guest_e2b_envd_manifest.json",
    ],
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


def test_evidence_fingerprint_includes_installed_transitive_dependencies(monkeypatch):
    dependency = types.SimpleNamespace(metadata={"Name": "httpx"}, version="0.28.1")
    monkeypatch.setattr(coverage.importlib.metadata, "distributions", lambda: [dependency])
    before = coverage.implementation_fingerprint()
    dependency.version = "0.29.0"
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


@pytest.mark.parametrize("style", ["sync", "async"])
def test_e2b_inventory_paginator_roots_are_real_vendor_apis(style):
    roots = [root for root in coverage.ROOTS["e2b"] if root[0] == style and root[1] == "SandboxPaginator"]
    assert len(roots) == 1
    _, prefix, reference, shim_reference = roots[0]
    native, shim = coverage.resolve(reference), coverage.resolve(shim_reference)
    rows = list(coverage.surfaces("e2b", style, prefix, native, shim))
    assert {row["api"] for row in rows} >= {
        "SandboxPaginator.next_items",
        "SandboxPaginator.has_next",
        "SandboxPaginator.next_token",
    }
    assert all(row["declared_by_shim"] for row in rows)
    assert all(hasattr(native, row["api"].split(".")[-1]) for row in rows)
