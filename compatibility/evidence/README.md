# Recorded differential evidence

These are observations, not a claim of full compatibility. They contain API
names, outcome, route, run ID, provider SDK version and implementation
fingerprints; no credentials or returned file contents.

The newer `feature51-resources-python.jsonl` and
`feature51-resources-node.jsonl` cohort runs at shim source `4951ec9`, against
the same isolated backend `51c0ce8`. It includes Blaxel directory/entry path
assertions and follows the native Modal resource-service preservation fix.
It passes 27 Python / 22 Node cases; all four strict gates still fail, with
the same core counts of 100/1,115 Python and 53/978 Node. Python's fingerprint
is unchanged; Node's is
`f3a3134188a3eea30b1cdddd82f81f6d7e4be822280b076f7c2841255a043e90`.
Use the corresponding `feature51-resources-*` filename in the commands below
for this source. No newer backend release is certified by these observations.

The original cohort below is historical:

The cohort ran against SDK runtime `63e9003` (status head `c103247`) and
isolated Arker release `compat-private-port-g51c0ce8f5416-19559a34`.
It passed 27 Python and 22 Node cases, including binary E2B read/write and
exact native byte-array return types. Some cases are Arker-only and grant
no vendor-equivalence credit.

- Python fingerprint: `6181f3a9f9a0df051caff60f3eddcae81de233550dc4652097a68109d7fdf77a`.
- Node fingerprint: `13f75b540c5405a779577598a2dfcf82a06959b72fee943f74e41abad01aaf04`.
- Core scope: 100/1,115 Python and 53/978 Node scenario-verified APIs.
- Expanded scope: 100/1,138 Python and 53/1,048 Node.
- Both expanded and both core strict gates were invoked and failed (exit 1).
  `full_api_equivalence` is false.

Reporters reject mismatched source, dependency, scenario, SDK-version or
host-runtime fingerprints. Changing code requires new live evidence; copying
these files does not certify a different checkout or runtime.

To inspect this evidence with the matching Python environment:

```sh
cd python
python scripts/compat_coverage.py --scope core-sandbox \
  --evidence ../compatibility/evidence/feature51-python.jsonl --fail-on-unverified
```

For Node, use the matching Node environment, not Bun (its runtime fingerprint
differs):

```sh
cd typescript
npx -y tsx@4.20.6 scripts/compat-coverage.ts --scope core-sandbox \
  --evidence ../compatibility/evidence/feature51-node.jsonl --fail-on-unverified
```

An exit code of 1 is the recorded strict result, not a successful coverage gate.
The complete declared inventory is bounded by the roots/depth documented in
the parent README; it does not enumerate all option combinations or behaviors.
