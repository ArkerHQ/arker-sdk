from __future__ import annotations

import importlib.metadata
import json
import os
import runpy
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "compat_coverage.py"
INITIAL_FINGERPRINT = runpy.run_path(str(SCRIPT))["implementation_fingerprint"]()


def vendor_version(provider):
    try:
        return importlib.metadata.version(provider)
    except importlib.metadata.PackageNotFoundError:
        return None


INITIAL_VERSIONS = {provider: vendor_version(provider) for provider in ("blaxel", "e2b", "modal", "daytona")}


def record(provider, style, route, scenario, apis, outcome):
    output = os.environ.get("COMPAT_COVERAGE_EVIDENCE")
    if not output:
        return
    fingerprint = runpy.run_path(str(SCRIPT))["implementation_fingerprint"]()
    version = vendor_version(provider)
    changed = fingerprint != INITIAL_FINGERPRINT or version != INITIAL_VERSIONS[provider]
    event = {
        "provider": provider,
        "run": os.environ["COMPAT_COVERAGE_RUN"],
        "scenario": scenario,
        "route": route,
        "outcome": "failed" if changed else outcome,
        "vendor_version": INITIAL_VERSIONS[provider],
        "implementation_fingerprint": INITIAL_FINGERPRINT,
        "apis": [f"python:{provider}:{style}:{api}" for api in apis],
    }
    descriptor = os.open(output, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, (json.dumps(event) + "\n").encode())
    finally:
        os.close(descriptor)
    if changed:
        raise RuntimeError("Implementation or vendor version changed during the live compatibility scenario")
