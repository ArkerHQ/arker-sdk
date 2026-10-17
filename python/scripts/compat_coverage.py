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
        ("sync", "Template", "e2b:Template", "arker.e2b:Template"),
        ("sync", "Volume", "e2b:Volume", "arker.e2b:Volume"),
        ("sync", "Secret", "e2b:Secret", "arker.e2b:Secret"),
    ],
    "daytona": [
        ("sync", "Daytona", "daytona:Daytona", "arker.daytona:Daytona"),
        ("sync", "Sandbox", "daytona:Sandbox", "arker.daytona:Sandbox"),
        ("async", "Daytona", "daytona:AsyncDaytona", "arker.daytona:AsyncDaytona"),
        ("async", "Sandbox", "daytona:AsyncSandbox", "arker.daytona:AsyncSandbox"),
        ("sync", "Image", "daytona:Image", "arker.daytona:Image"),
    ],
    "modal": [
        ("sync", "Sandbox", "modal:Sandbox", "arker.modal:Sandbox"),
        ("async", "Sandbox", "modal:Sandbox", None),
        ("sync", "Image", "modal:Image", "arker.modal:Image"),
        ("sync", "App", "modal:App", "arker.modal:App"),
        ("sync", "Volume", "modal:Volume", "arker.modal:Volume"),
        ("sync", "Secret", "modal:Secret", "arker.modal:Secret"),
    ],
    "blaxel": [
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
        root / "src" / "arker" / "_linux_signals.json",
        root / "scripts" / "compat_coverage.py",
        *root.joinpath("tests", "e2e").glob("*.py"),
    ]
    digest = hashlib.sha256()
    digest.update(json.dumps(runtime_info(), sort_keys=True).encode() + b"\0")
    for path in sorted(paths):
        digest.update(str(path.relative_to(root.parent)).encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


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


def verified_scenarios(api, evidence, version, fingerprint):
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
        ):
            raise ValueError("Invalid compatibility evidence")
        group = (item["run"], item["scenario"])
        if item.get("outcome") != "passed":
            failures.add(group)
        elif api in item.get("apis", []):
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
        implemented = shim is not None and inspect.getattr_static(shim, name, None) is not None
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
            "declared_by_shim": implemented,
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
    for row in rows:
        provider_evidence = [item for item in evidence if item.get("provider") == row["provider"]]
        row["verified_scenarios"] = verified_scenarios(
            row["id"], provider_evidence, versions[row["provider"]], fingerprint
        )
        row["status"] = "scenario_verified" if row["verified_scenarios"] else "unverified"
    report = {
        "schema_version": 1,
        "language": "python",
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
        "full_api_equivalence": False,
        "apis": rows,
    }
    output = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(output)
    else:
        print(output, end="")
    if args.fail_on_unverified and (
        not report["complete_inventory"] or any(row["status"] == "unverified" for row in rows)
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
