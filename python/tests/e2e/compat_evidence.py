from __future__ import annotations

import importlib.metadata
import json
import os
import runpy
from contextlib import contextmanager
from pathlib import Path

if os.environ.get("COMPAT_COVERAGE_EVIDENCE") and not os.environ.get("COMPAT_COVERAGE_RUN", "").strip():
    raise RuntimeError("COMPAT_COVERAGE_RUN is required when recording evidence")

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "compat_coverage.py"
INITIAL_FINGERPRINT = runpy.run_path(str(SCRIPT))["implementation_fingerprint"]()


def vendor_version(provider):
    try:
        return importlib.metadata.version(provider)
    except importlib.metadata.PackageNotFoundError:
        return None


INITIAL_VERSIONS = {provider: vendor_version(provider) for provider in ("blaxel", "e2b", "modal", "daytona")}


def record(provider, style, route, scenario, apis, outcome, *, signatures=None, api_evidence="assertion", cleanup=None):
    output = os.environ.get("COMPAT_COVERAGE_EVIDENCE")
    if not output:
        return
    if api_evidence not in {"assertion", "runtime"}:
        raise ValueError("Unknown API evidence kind")
    signatures = {} if signatures is None else signatures
    if not isinstance(signatures, dict) or not all(
        api in apis
        and isinstance(values, list)
        and values
        and all(isinstance(value, str) and value.strip() for value in values)
        for api, values in signatures.items()
    ):
        raise ValueError("Signature attestations must name exercised APIs and nonempty signatures")
    fingerprint = runpy.run_path(str(SCRIPT))["implementation_fingerprint"]()
    version = vendor_version(provider)
    changed = fingerprint != INITIAL_FINGERPRINT or version != INITIAL_VERSIONS[provider]
    event = {
        "api_evidence": api_evidence,
        "cleanup": cleanup,
        "provider": provider,
        "run": os.environ["COMPAT_COVERAGE_RUN"],
        "scenario": scenario,
        "route": route,
        "outcome": "failed" if changed else outcome,
        "vendor_version": INITIAL_VERSIONS[provider],
        "implementation_fingerprint": INITIAL_FINGERPRINT,
        "apis": [f"python:{provider}:{style}:{api}" for api in apis],
        "signatures": {f"python:{provider}:{style}:{api}": values for api, values in signatures.items()},
    }
    descriptor = os.open(output, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, (json.dumps(event) + "\n").encode())
    finally:
        os.close(descriptor)
    if changed:
        raise RuntimeError("Implementation or vendor version changed during the live compatibility scenario")


@contextmanager
def record_scenario(
    vendor, style, provider, shim, scenario, apis, *, signatures=None, api_evidence="assertion", cleanup=None
):
    """Record program outcome; strict credit additionally requires cleanup readbacks."""
    outcome = "failed"
    try:
        yield
        outcome = "passed"
    finally:
        route = "reference" if not shim else "arker" if provider == "arker" else "native"
        record(
            vendor,
            style,
            route,
            scenario,
            sorted(apis),
            outcome,
            signatures=signatures,
            api_evidence=api_evidence,
            **({"cleanup": cleanup.receipt()} if cleanup is not None else {}),
        )


class ModalCleanup:
    """Enroll each allocation immediately; read retained terminal state after cleanup.

    A termination acknowledgement alone is insufficient. The harness must enroll
    every owned allocation; this is an auditable observation, not cryptographic proof.
    """

    def __init__(self):
        self.resources = []
        self.observations = {}

    def allocated(self, identifier):
        if not isinstance(identifier, str) or not identifier or identifier in self.resources:
            raise ValueError("Expected a new sandbox identifier")
        self.resources.append(identifier)

    async def observe(self, identifier, lookup, poll):
        if identifier not in self.resources:
            raise ValueError("Sandbox was not enrolled")
        self.observations.pop(identifier, None)
        sandbox = await lookup(identifier)
        if sandbox.object_id != identifier:
            raise RuntimeError("Cleanup readback returned a different sandbox")
        returncode = await poll(sandbox)
        if type(returncode) is not int:
            raise RuntimeError("Sandbox termination has not been observed")
        self.observations[identifier] = {
            "id": identifier,
            "operation": "Sandbox.from_id.poll",
            "returncode": returncode,
        }

    def receipt(self):
        return {
            "protocol": "modal-terminal-v1",
            "resources": list(self.resources),
            "observations": [self.observations[x] for x in self.resources if x in self.observations],
        }
