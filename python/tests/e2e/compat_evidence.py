from __future__ import annotations

import importlib.metadata
import json
import os
import runpy
from pathlib import Path


def record(provider, style, route, scenario, apis, outcome):
    output = os.environ.get("COMPAT_COVERAGE_EVIDENCE")
    if not output:
        return
    script = Path(__file__).resolve().parents[2] / "scripts" / "compat_coverage.py"
    fingerprint = runpy.run_path(str(script))["implementation_fingerprint"]()
    event = {
        "provider": provider,
        "run": os.environ["COMPAT_COVERAGE_RUN"],
        "scenario": scenario,
        "route": route,
        "outcome": outcome,
        "vendor_version": importlib.metadata.version(provider),
        "implementation_fingerprint": fingerprint,
        "apis": [f"python:{provider}:{style}:{api}" for api in apis],
    }
    descriptor = os.open(output, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, (json.dumps(event) + "\n").encode())
    finally:
        os.close(descriptor)
