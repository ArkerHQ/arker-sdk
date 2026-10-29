"""Inventory installed vendor APIs; combine with differential test evidence.

An exposed method is not counted as behaviorally verified. Evidence must include
the same scenario passing against the vendor SDK and both shim routes.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import importlib.metadata
import inspect
import json
import platform
import re
import sys
import textwrap
import typing
from collections import Counter
from pathlib import Path
from typing import Any

ROOTS = {
    "e2b": [
        ("sync", "Sandbox", "e2b:Sandbox", "arker.e2b:Sandbox"),
        ("sync", "CommandHandle", "e2b:CommandHandle", "arker.e2b:CommandHandle"),
        ("async", "Sandbox", "e2b:AsyncSandbox", "arker.e2b:AsyncSandbox"),
        ("async", "CommandHandle", "e2b:AsyncCommandHandle", "arker.e2b:AsyncCommandHandle"),
        ("async", "CommandInputHandle", "e2b:AsyncCommandInputHandle", "arker.e2b:AsyncCommandInputHandle"),
        (
            "sync",
            "Sandbox.commands",
            "e2b.sandbox_sync.commands.command:Commands",
            "e2b.sandbox_sync.commands.command:Commands",
        ),
        (
            "sync",
            "Sandbox.files",
            "e2b.sandbox_sync.filesystem.filesystem:Filesystem",
            "e2b.sandbox_sync.filesystem.filesystem:Filesystem",
        ),
        ("sync", "Sandbox.pty", "e2b.sandbox_sync.commands.pty:Pty", "e2b.sandbox_sync.commands.pty:Pty"),
        ("sync", "Sandbox.git", "e2b.sandbox_sync.git:Git", "e2b.sandbox_sync.git:Git"),
        (
            "async",
            "Sandbox.commands",
            "e2b.sandbox_async.commands.command:Commands",
            "e2b.sandbox_async.commands.command:Commands",
        ),
        (
            "async",
            "Sandbox.files",
            "e2b.sandbox_async.filesystem.filesystem:Filesystem",
            "e2b.sandbox_async.filesystem.filesystem:Filesystem",
        ),
        ("async", "Sandbox.pty", "e2b.sandbox_async.commands.pty:Pty", "e2b.sandbox_async.commands.pty:Pty"),
        ("async", "Sandbox.git", "e2b.sandbox_async.git:Git", "e2b.sandbox_async.git:Git"),
        (
            "sync",
            "SandboxPaginator",
            "e2b.sandbox_sync.paginator:SandboxPaginator",
            "arker._compat_e2b_inventory:InventoryPaginator",
        ),
        (
            "async",
            "SandboxPaginator",
            "e2b.sandbox_async.paginator:AsyncSandboxPaginator",
            "arker._compat_e2b_inventory:AsyncInventoryPaginator",
        ),
        ("sync", "Template", "e2b:Template", "arker.e2b:Template"),
        ("sync", "Volume", "e2b:Volume", "arker.e2b:Volume"),
        ("async", "Volume", "e2b:AsyncVolume", "arker.e2b:AsyncVolume"),
        ("sync", "Secret", "e2b:Secret", "arker.e2b:Secret"),
    ],
    "daytona": [
        ("sync", "Daytona.volume", "daytona._sync.volume:VolumeService", "arker._daytona_volumes:VolumeService"),
        (
            "async",
            "Daytona.volume",
            "daytona._async.volume:AsyncVolumeService",
            "arker._daytona_volumes:AsyncVolumeService",
        ),
        ("sync", "Daytona", "daytona:Daytona", "arker.daytona:Daytona"),
        ("sync", "Sandbox", "daytona:Sandbox", "arker.daytona:Sandbox"),
        ("async", "Daytona", "daytona:AsyncDaytona", "arker.daytona:AsyncDaytona"),
        ("async", "Sandbox", "daytona:AsyncSandbox", "arker.daytona:AsyncSandbox"),
        ("sync", "Image", "daytona:Image", "arker.daytona:Image"),
        ("sync", "Sandbox.fs", "daytona._sync.filesystem:FileSystem", "arker._daytona_filesystem:FileSystem"),
        (
            "async",
            "Sandbox.fs",
            "daytona._async.filesystem:AsyncFileSystem",
            "arker._daytona_filesystem:AsyncFileSystem",
        ),
    ],
    "modal": [
        ("sync", "Sandbox", "modal:Sandbox", "arker.modal:Sandbox"),
        ("async", "Sandbox", "modal:Sandbox", None),
        ("sync", "ContainerProcess", "modal.container_process:ContainerProcess", "arker.modal:Process"),
        ("async", "ContainerProcess", "modal.container_process:ContainerProcess", None),
        ("sync", "ContainerProcess.stdout", "modal.io_streams:StreamReader", "arker.modal:_Stream"),
        ("async", "ContainerProcess.stdout", "modal.io_streams:StreamReader", None),
        ("sync", "ContainerProcess.stderr", "modal.io_streams:StreamReader", "arker.modal:_Stream"),
        ("async", "ContainerProcess.stderr", "modal.io_streams:StreamReader", None),
        ("sync", "ContainerProcess.stdin", "modal.io_streams:StreamWriter", None),
        ("async", "ContainerProcess.stdin", "modal.io_streams:StreamWriter", None),
        ("sync", "Image", "modal:Image", "arker.modal:Image"),
        ("sync", "App", "modal:App", "arker.modal:App"),
        ("sync", "Volume", "modal:Volume", "arker.modal:Volume"),
        ("sync", "Secret", "modal:Secret", "arker.modal:Secret"),
    ],
    "blaxel": [
        ("sync", "DriveInstance", "blaxel.core:SyncDriveInstance", "arker.blaxel:SyncDriveInstance"),
        ("async", "DriveInstance", "blaxel.core:DriveInstance", "arker.blaxel:DriveInstance"),
        ("sync", "VolumeInstance", "blaxel.core:SyncVolumeInstance", "arker.blaxel:SyncVolumeInstance"),
        ("async", "VolumeInstance", "blaxel.core:VolumeInstance", "arker.blaxel:VolumeInstance"),
        ("sync", "SecretInstance", "blaxel.core:SyncSecretInstance", "arker.blaxel:SyncSecretInstance"),
        ("async", "SecretInstance", "blaxel.core:SecretInstance", "arker.blaxel:SecretInstance"),
        ("sync", "Snapshot", "blaxel.core:SyncSnapshot", "arker.blaxel:SyncSnapshot"),
        ("async", "Snapshot", "blaxel.core:Snapshot", "arker.blaxel:Snapshot"),
        ("sync", "ImageInstance", "blaxel.core:ImageInstance", "arker.blaxel:ImageInstance"),
        ("sync", "SandboxInstance", "blaxel.core.sandbox:SyncSandboxInstance", "arker.blaxel:SyncSandboxInstance"),
        ("async", "SandboxInstance", "blaxel.core.sandbox:SandboxInstance", "arker.blaxel:SandboxInstance"),
        (
            "sync",
            "SandboxInstance.process",
            "blaxel.core.sandbox.sync.process:SyncSandboxProcess",
            "arker.blaxel:Process",
        ),
        ("async", "SandboxInstance.process", "blaxel.core.sandbox.default.process:SandboxProcess", None),
        (
            "sync",
            "SandboxInstance.fs",
            "blaxel.core.sandbox.sync.filesystem:SyncSandboxFileSystem",
            "arker.blaxel:FileSystem",
        ),
        ("async", "SandboxInstance.fs", "blaxel.core.sandbox.default.filesystem:SandboxFileSystem", None),
    ],
}


def runtime_info():
    return {
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": sys.platform,
        "architecture": platform.machine(),
        "packages": sorted(
            (distribution.metadata.get("Name", "").lower().replace("_", "-"), distribution.version)
            for distribution in importlib.metadata.distributions()
        ),
    }


def implementation_fingerprint():
    root = Path(__file__).resolve().parents[1]
    paths = [
        *root.joinpath("src", "arker").rglob("*.py"),
        root / "pyproject.toml",
        root / "uv.lock",
        root.parent / "compatibility" / "requirements.txt",
        root.parent / "compatibility" / "http-service.py",
        root.parent / "compatibility" / "api-scope.json",
        root / "src" / "arker" / "_linux_signals.json",
        root / "src" / "arker" / "_guest_e2b_envd_manifest.json",
        root / "scripts" / "compat_coverage.py",
        *root.joinpath("tests", "e2e").glob("*.py"),
    ]
    digest = hashlib.sha256()
    digest.update(json.dumps(runtime_info(), sort_keys=True).encode() + b"\0")
    for path in sorted(paths):
        digest.update(str(path.relative_to(root.parent)).encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def select_scope(rows, scope):
    if scope not in ("expanded", "core-sandbox"):
        raise ValueError("unknown compatibility scope")
    contract = json.loads((Path(__file__).resolve().parents[2] / "compatibility" / "api-scope.json").read_text())
    if type(contract["version"]) is not int or contract["version"] != 1:
        raise ValueError("unsupported API scope contract")
    selected, excluded = [], []
    for row in rows:
        match = next(
            (
                rule
                for rule in contract["exclusions"]
                if row["provider"] == rule["provider"] and re.search(rule["pattern"], row["api"])
            ),
            None,
        )
        if match:
            excluded.append({**row, "exclusion_reason": match["reason"]})
        if scope == "expanded" or not match:
            selected.append(row)
    return selected, excluded


def resolve(reference: str | None) -> Any:
    if reference is None:
        return None
    module, name = reference.split(":")
    return getattr(importlib.import_module(module), name)


def signature(member: Any) -> str | None:
    try:
        return str(inspect.signature(member))
    except (TypeError, ValueError):
        return None


def signatures(member):
    return [value for method in (typing.get_overloads(member) or [member]) if (value := signature(method))]


def verified_scenarios(api, evidence, version, fingerprint, required_signature=None):
    groups = {}
    failures = set()
    for item in evidence:
        if item.get("vendor_version") != version or item.get("implementation_fingerprint") != fingerprint:
            continue
        if (
            not all(isinstance(item.get(key), str) and item[key].strip() for key in ("run", "scenario"))
            or item.get("route") not in ("reference", "native", "arker")
            or item.get("outcome") not in ("passed", "failed")
            or not isinstance(item.get("apis"), list)
            or not all(isinstance(api, str) and api.strip() for api in item["apis"])
            or not isinstance(item.get("signatures", {}), dict)
            or not all(
                key in item["apis"]
                and isinstance(values, list)
                and all(isinstance(value, str) and value.strip() for value in values)
                for key, values in item.get("signatures", {}).items()
            )
        ):
            raise ValueError("Invalid compatibility evidence")
        group = (item["run"], item["scenario"])
        if item.get("outcome") != "passed":
            failures.add(group)
        elif api in item.get("apis", []) and (
            required_signature is None or required_signature in item.get("signatures", {}).get(api, [])
        ):
            groups.setdefault(group, set()).add(item["route"])
    return sorted(
        f"{group[0]}:{group[1]}"
        for group, routes in groups.items()
        if {"reference", "native", "arker"} <= routes and group not in failures
    )


def surfaces(provider: str, style: str, prefix: str, native: type, shim: type | None, depth: int = 0):
    protocols = {"__enter__", "__exit__", "__aenter__", "__aexit__", "__iter__", "__next__", "__aiter__", "__anext__"}
    for name in sorted(dir(native)):
        if name.startswith("_") and name not in protocols:
            continue
        raw = inspect.getattr_static(native, name)
        if isinstance(raw, property):
            member = raw.fget
            kind = "property"
        else:
            try:
                member = getattr(native, name)
            except (AttributeError, TypeError):
                member = raw
            kind = "method" if callable(member) else "descriptor"
        module = getattr(member, "__module__", getattr(type(raw), "__module__", ""))
        if not module.startswith(("e2b", "daytona", "modal", "blaxel")):
            continue
        shim_member = inspect.getattr_static(shim, name, None) if shim is not None else None
        implemented = shim_member is not None
        if isinstance(shim_member, (property, classmethod, staticmethod)):
            shim_member = shim_member.fget if isinstance(shim_member, property) else shim_member.__func__
        if provider == "modal" and style == "async":
            if not hasattr(member, "aio"):
                continue
            member = member.aio
        yield {
            "id": f"python:{provider}:{style}:{prefix}.{name}",
            "provider": provider,
            "style": style,
            "api": f"{prefix}.{name}",
            "kind": kind,
            "signature": signature(member),
            "signatures": signatures(member) if kind == "method" else [],
            "declared_by_shim": implemented,
            "shim_signatures": signatures(shim_member) if kind == "method" and implemented else [],
        }
        if kind == "property" and depth < 2:
            try:
                returned = typing.get_type_hints(member).get("return")
            except (NameError, TypeError):
                returned = None
            if inspect.isclass(returned) and returned.__module__.startswith(("e2b", "daytona", "blaxel", "modal")):
                # The inventory follows vendor properties without constructing a
                # sandbox or triggering an authenticated property getter.
                yield from surfaces(provider, style, f"{prefix}.{name}", returned, None, depth + 1)
    if depth < 2:
        initializer = getattr(native, "__init__", None)
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(initializer)))
        except (OSError, TypeError, IndentationError):
            return
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            constructor = node.value.func
            if not isinstance(constructor, ast.Name):
                continue
            returned = getattr(initializer, "__globals__", {}).get(constructor.id)
            if not inspect.isclass(returned) or not returned.__module__.startswith(
                ("blaxel", "daytona", "e2b", "modal")
            ):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and not target.attr.startswith("_")
                ):
                    yield from surfaces(provider, style, f"{prefix}.{target.attr}", returned, None, depth + 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fail-on-unverified", action="store_true")
    parser.add_argument("--scope", choices=("expanded", "core-sandbox"), default="expanded")
    args = parser.parse_args()
    evidence = []
    for path in args.evidence:
        evidence.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
    fingerprint = implementation_fingerprint()
    rows = []
    versions = {}
    missing = []
    for provider, roots in ROOTS.items():
        try:
            versions[provider] = importlib.metadata.version(provider)
            for style, prefix, reference, shim_reference in roots:
                try:
                    discovered = list(surfaces(provider, style, prefix, resolve(reference), resolve(shim_reference)))
                    if not discovered:
                        missing.append({"root": reference, "error": "no public APIs discovered"})
                    rows.extend(discovered)
                except (ImportError, AttributeError) as error:
                    missing.append({"root": reference, "error": str(error)})
        except importlib.metadata.PackageNotFoundError:
            missing.append({"provider": provider, "error": "vendor SDK is not installed"})
    rows = list({row["id"]: row for row in rows}.values())
    rejected_evidence = sum(
        item.get("implementation_fingerprint") != fingerprint
        or item.get("vendor_version") != versions.get(item.get("provider"))
        for item in evidence
    )
    for row in rows:
        provider_evidence = [item for item in evidence if item.get("provider") == row["provider"]]
        row["verified_scenarios"] = verified_scenarios(
            row["id"], provider_evidence, versions[row["provider"]], fingerprint
        )
        row["status"] = "scenario_verified" if row["verified_scenarios"] else "unverified"
        vendor_signatures = row["signatures"]
        row["tested_signatures"] = [
            sig
            for sig in vendor_signatures
            if any(
                item.get("implementation_fingerprint") == fingerprint
                and item.get("vendor_version") == versions[row["provider"]]
                and item.get("outcome") == "passed"
                and row["id"] in item["apis"]
                and sig in item.get("signatures", {}).get(row["id"], [])
                for item in provider_evidence
            )
        ]
        row["live_signatures"] = [
            sig
            for sig in vendor_signatures
            if verified_scenarios(row["id"], provider_evidence, versions[row["provider"]], fingerprint, sig)
        ]
    rows, exclusions = select_scope(rows, args.scope)
    report = {
        "schema_version": 1,
        "language": "python",
        "selected_scope": args.scope,
        "scope_exclusions": exclusions,
        "runtime": runtime_info(),
        "scope": (
            "Public methods and properties of sandbox/client roots and statically discoverable service objects, "
            "depth 2; signatures are recorded separately from behavioral evidence."
        ),
        "vendor_versions": versions,
        "implementation_fingerprint": fingerprint,
        "complete_inventory": bool(rows) and not missing,
        "missing_roots": missing,
        "counts": dict(Counter(row["status"] for row in rows)),
        "rejected_evidence": rejected_evidence,
        "signature_counts": {
            "vendor": sum(len(row["signatures"]) for row in rows),
            "shim_declared": sum(len(row["shim_signatures"]) for row in rows),
            "tested": sum(len(row["tested_signatures"]) for row in rows),
            "live": sum(len(row["live_signatures"]) for row in rows),
            "unknown": sum(row["kind"] == "method" and not row["signatures"] for row in rows),
        },
        "full_api_equivalence": False,
        "apis": rows,
    }
    output = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(output)
    else:
        print(output, end="")
    if args.fail_on_unverified and (
        not report["complete_inventory"]
        or rejected_evidence
        or report["signature_counts"]["unknown"]
        or any(row["status"] == "unverified" or row["signatures"] != row["live_signatures"] for row in rows)
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
