# Recorded differential evidence

Source cd7e0eb adds filesystem scenarios, which changes fingerprints even without
a runtime edit. The completed103/60 checkpoint below remains source-bound to
the prior scenario set until fresh enlarged cohorts execute; no relabeling.

Current `production-hardening-20261022-{python,node}.jsonl` records matching-source
core hardening at e46af69 (unit-only fixes through80f492a). Python32/36 and Node26/31
selections pass, plus six of six Git differential selections. Core coverage is
103/1123 Python and60/982 TypeScript; expanded103/1146 and60/1052. All four strict
gates exit1. Nine production selections still fail on private-port404 or guard-
schema400. No backend deployment, full parity or untested option coverage is claimed.
Fingerprints are Python `306a60eea7d3c4ca014dc207f804c2ac66748d3506347b90c313bd4fc7afe1fc`
and Node `e5a249aabcb1f058f59e147d5fc74c43a80cf7b01d227452a0870398082bf6fd`.
Earlier observations below are historical, not current-source credit.

These are observations, not a claim of full compatibility. They contain API
names, outcome, route, run ID, provider SDK version and implementation
fingerprints; no credentials or returned file contents.

`production-secret-sync-20261022-node.jsonl` records source `56c7175` after
adding secret-aware sync writes. All31 Node cases execute:26 pass/five fail,
the same production private-port404/guard-schema400 blockers. Core51/982 and
expanded51/1052 remain strict-red, exit1 each. Fingerprint:
`ff2472a96fc8bab8a62a269e0f664fcc8da38906233636038f17dd4d22b3adef`.
This supersedes Node observations below for current-source credit; matching
Python additive-source evidence remains unchanged. No backend deployment or full
equivalence claim.

`production-additive-20261022-{python,node}.jsonl` record source `9fdf196`
after additive guard readback normalization. All 36 Python and 31 Node cases
execute: Python 32 pass/four fail, Node 26 pass/five fail. Missing production
private-port routes and outbound-guard support remain explicit; all four strict
gates fail exit 1. Core coverage is 94/1,123 Python and 51/982 Node, expanded
94/1,146 and 51/1,052. These observations do not certify unpublished backend
changes or full parity. Fingerprints:

- Python: `cc005c65831c75308c32d4a604602cbf07982590d953e169c101ec460e6407d3`.
- Node: `4b446d7f26dcbfa9a6d927922d9598d380a8492b01f33c8383715d4e510ee77a`.

`production-current-20261022-{python,node}.jsonl` record source `816cdc6`
against current provider pins (see `../provider-upgrade-20261022.json`). All
36 Python and31 Node configurations execute: Python32 pass/four fail;
Node26 pass/five fail. Failures remain missing production private-port routes
and outbound-guard support. All four core/expanded strict gates fail exit1.
Core coverage is94/1123 Python and51/982 Node; expanded94/1146 and51/1052.
No new native-version mismatch is seen in these selections; this does not
certify all APIs, option combinations, lifecycle or untested input streams.
The45 records include failed observations. Current fingerprints:

- Python: `09c95b7cb492211532b0021ff91d66f4f5f3e866e63be5778a802c52c780e864`.
- Node: `4dadbae44b4d046135aa0338bd0883d820995f5d7444bc5348838e2f894181ae`.

`production-main-20261022-{python,node}.jsonl` supersede that day's earlier
cohort at source `8cb23cf`, now including SDK main `4f1b1d9`. All 36 Python and
31 Node configurations execute: Python 32 pass/four fail; Node 26 pass/five fail.
The same missing production private-port route and outbound-guard schema cause
the failures; no policy relaxation is used. All four strict gates fail exit 1.
Core coverage is 94/1,115 Python and 51/978 Node; expanded is 94/1,138 and
51/1,048. The 45 traces retain failed private-fetch outcomes too. Fingerprints:

- Python: `9ddf67ea7c3c5ca29f0fff89b5f603280351948956428638daa63499415ef4b7`.
- Node: `0c7a409da8d47d1ca637f2b2d946329c7420912ee25087b48a04345b31fa110f`.

These remain tied to the prior pinned vendor versions, not subsequent upgrades.

`production-20261022-python.jsonl` and `production-20261022-node.jsonl` record
source `8361b4a` against the existing us-west-2 production API using the supplied
key, without a feature deployment. Python executes 36 cases: 32 pass and four
fail. Node executes 24 cases: 22 pass and two fail; seven further configurations
are not reached after their program's first failure. Failures expose missing
production private-port routes and unsupported outbound-guard rules, not waived
policy behavior. The two expanded strict gates fail exit 1; coverage is 94/1,138
Python and 51/1,048 Node. The 39 records include failed observations. Fingerprints:

- Python: `5842e1d8a6a1c145b92149c19219f421ab24608f67c74631da251da3235efa67`.
- Node: `21a2a34e88eb9c94e9c4e1c958f98f1762a08ad768d09e6ceab337104424e78f`.

These observations do not certify subsequent source changes or unpublished
backend changes. They cannot be replayed for coverage credit after merging main
without fresh matching-source live evidence.

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
