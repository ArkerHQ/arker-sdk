# Recorded coverage baseline

## Current-main validation

On 2026-10-17 the branch was merged with SDK `main` at `2ec474e`.
Fresh differential runs on current implementation sources establish **38/944
Python** scenario-verified entries. TypeScript establishes **19/928** only when
using the locally linked draft ComputeSDK provider builds from computesdk #6.
After restoring the committed published dependencies, those integration
fingerprints correctly become stale: **0/928 TypeScript** entries are verified
for the published dependency profile. Both inventories are complete within
their declared bounded scope, and both strict gates return exit code 1.
Neither the draft integration profile nor historical results certify the
published packages or complete API equivalence.

Post-merge local validation passes: 307 Python unit tests and lint, plus the full
TypeScript test/build/typecheck pipeline. New evidence-validator regressions
first failed in both languages, demonstrating that colon-delimited run/scenario
keys could combine unrelated runs. Tuple keys now isolate those runs; malformed
current evidence also fails closed. These are harness tests, not live parity
credit.

Evidence fingerprints include the scenario and measurement sources, not just
production files and dependency manifests. Changing a test or its tracer also
invalidates old live evidence; regression tests exercise that requirement.
Empty discovered vendor roots are inventory errors, not zero-gap success.
TypeScript fingerprints also include installed ComputeSDK/provider runtime
artifacts. Native evidence rejects a vendor version mismatch between the
reference SDK and the provider's actual resolved dependency.

All four providers passed the current-main common execution comparison using
the draft provider builds. Cross-language process control, daemon/inbound
behavior, and unchanged Blaxel programs (six Python cases, three TypeScript
cases) passed. The unchanged programs now assert exact stdout, stderr, status,
and exit code for a command exiting 17. These passing comparisons do not grant
inventory credit unless recorded by the differential measurement harness.

The strengthened unchanged Blaxel programs also passed binary reads/writes of
every byte value, empty files, local-file uploads, downloads and permissions,
structured mutation responses, and preservation of a nonempty directory after
nonrecursive deletion fails. Both languages previously lacked binary methods
and always deleted recursively; the new unit regressions failed before fixing
those behaviors. Fresh live evidence: six Python and three TypeScript routes,
zero failures. Draft provider builds remain a separate integration profile.

Current Daytona regressions additionally verify GiB-to-MiB conversion only for
Arker creation, unchanged native resource objects, typed Python creation models,
and rejection of unsupported resource fields. These request-level tests do not
grant live equivalence credit.

## Historical live baseline

Measured against the pinned vendor SDKs on 2026-10-16. Counts below are public
entries within the bounded inventory described in `README.md`, not claims that
each entry's option combinations or error paths are equivalent.

| Provider | Python discovered | Python scenario-tested | TypeScript discovered | TypeScript scenario-tested |
| --- | ---: | ---: | ---: | ---: |
| Blaxel | 278 | 30 | 154 | 15 |
| Daytona | 258 | 0 | 234 | 0 |
| E2B | 201 | 0 | 118 | 0 |
| Modal | 207 | 0 | 422 | 0 |
| Total | 944 | 30 | 928 | 15 |

Zero means no recorded differential evidence in the new harness. It does not
erase previous testing or assert that every remaining entry is unimplemented.
Both `--fail-on-unverified` gates return exit code 1.

The passing Blaxel program runs unchanged against the original SDK, the shim on
Blaxel, and the shim on Arker, in Python sync, Python async, and TypeScript. It
checks foreground output, cwd/environment isolation, file overwrite/listing,
reconnect, named background commands, stdin/EOF, status/logs/list, timeout of
observation without killing the command, and process kill. Local protocol tests
also cover execution timeout, duplicate-name rejection, nonzero exit, callbacks,
and cancellation. These local tests do not grant differential coverage.

The program lists `/blaxel` immediately after creation, before running any
command. This exposed a missing Python initialization step; both languages now
initialize that directory at creation and delete a newly created VM if
initialization fails.

A separate live cross-language program passed on native Blaxel, the shim on
Blaxel, and the shim on Arker. It verifies that Python and TypeScript can control
the same process in both directions, keep the same PID, and share the persisted
provider-ownership cache. It is additional validation, not an increase in the
instrumented API counts above.

## Open release requirements

- Remove the guest process agent's Python 3 dependency or provide an
  image-independent runtime. A real Arker `ubuntu:24.04` image probe confirmed
  that Python 3 is absent. The current background process implementation was
  validated on `ubuntu-coding`, not arbitrary images.
- Instrument and extend the existing E2B, Modal, and Daytona comparisons.
- Cover returned handles and option/error/cancellation combinations, not merely
  a method's presence or a single successful call.
- Close the remaining Arker-side APIs exposed as missing/unverified by inventory,
  including provider-specific services, lifecycle options, and streaming APIs.
- Establish atomic named get-or-create semantics. Python currently performs a
  lookup followed by creation; concurrent callers can race. TypeScript's
  Blaxel `createIfNotExists` forwards `createIfNotExist`, which the Arker
  provider currently rejects. Arker permits multiple VM rows under one name,
  so a local shim lock is not a distributed correctness fix.
- Release the ComputeSDK provider fixes before the migration SDK release.
- Implement Arker-backed Blaxel fetch. Supporting both application auth headers
  simultaneously also needs a non-conflicting platform-authentication channel.
- Establish native Daytona allowlist equivalence with an account that permits
  per-sandbox overrides. The supplied workspace rejects them. The current live
  policy matrix passes its other nine supported cases but deliberately exits 1
  for this unverified comparison; it does not treat account restrictions as a
  passing skip. Changing the workspace-wide policy requires separate approval.

Full API equivalence has not been established. The three PRs remain drafts.

## Underlying Arker policy validation

The current-main isolated HTTPS policy selection executed 321 cases: 312
passed, nine failed, and one additional selected case was skipped for a missing
golden. Triage classified all nine failures as known on main. Two stale suspend
request cases and one preview-resume race were fixed in the test setup; all five
focused fork/preview/WebSocket cases then passed. The six other broad-run
failures remain unresolved. Baseline classification does not turn them green.
See arker-app #4260 for the exact failures and immutable artifacts.

The first ingress fix also exposed a repeated-header edge case: a later Arker
key among application header values reached the guest. Both live regression
cases failed before the additional worker hardening. After deployment of that
hardening, all 49 HTTPS, HTTP/2, and WebSocket cases passed, with zero skips and
exit code 0. Further live regressions found lower- and mixed-case bearer keys
also leaked when another header authenticated the request (two failed, two
canonical controls passed). After the case-insensitive worker fix was deployed
to the isolated feature environment, all **51 HTTPS/HTTP2/WebSocket cases
passed**, zero skips/errors, exit 0, on release
`compat-parity-main-gdf3f0561b747-cf6cfba4`. Artifact:
`s3://arker-ci/arkt-runs/artifacts/20261017T021137617064Z-compat-parity-main/`.
This is isolated validation, not a production deployment or full policy parity.
