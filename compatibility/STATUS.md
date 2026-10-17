# Recorded coverage baseline

## Current-main validation

On 2026-10-17 the branch was merged with SDK `main` at `2ec474e`.
Both strict reporters reject all previous live evidence because upstream SDK
implementation files changed: the current fingerprints have **0/944 Python**
and **0/928 TypeScript** scenario-verified entries. Both inventories are complete
within their declared bounded scope, and both strict gates return exit code 1.
The historical live results below must not be used as current-head certification.

Post-merge local validation passes: 301 Python unit tests and lint, plus the full
TypeScript test/build/typecheck pipeline. New evidence-validator regressions
first failed in both languages, demonstrating that colon-delimited run/scenario
keys could combine unrelated runs. Tuple keys now isolate those runs; malformed
current evidence also fails closed. These are harness tests, not live parity
credit.

Evidence fingerprints include the scenario and measurement sources, not just
production files and dependency manifests. Changing a test or its tracer also
invalidates old live evidence; regression tests exercise that requirement.

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

Full API equivalence has not been established. The three PRs remain drafts.

## Underlying Arker policy validation

An isolated feature environment passed 45 focused inbound cases and 47 HTTPS,
HTTP/2, and WebSocket cases. A broader selection executed 270 cases: 267 passed
and three failed. Main's recorded baseline also fails those three cases:
outbound GET to portquiz.net:8080 times out, a subdomain TCP:853 connection
fails, and applying an inline run policy permits an idle pause before the run.
The latter two reproduced separately. These failures remain open; baseline
classification does not turn them into passing behavior. TLS-only exclusions
from the broader HTTP run were addressed by the separate HTTPS selection.

The first ingress fix also exposed a repeated-header edge case: a later Arker
key among application header values reached the guest. Both live regression
cases failed before the additional worker hardening. After deployment of that
hardening, all 49 HTTPS, HTTP/2, and WebSocket cases passed, with zero skips and
exit code 0. This also verifies preservation of application Authorization when
the platform key is supplied in `x-api-key`. See arker-app #4260 for artifacts.
