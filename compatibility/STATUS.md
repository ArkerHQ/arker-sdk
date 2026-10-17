# Recorded coverage baseline

## Current validation

The branch includes SDK main at 2ec474e. Fresh runtime-scoped differential
reports verify **57/950 Python** entries on CPython 3.12.14/Linux/aarch64 and
**34/938 TypeScript** entries on Node 22.23.3/Linux/arm64. The Node profile uses
the locally linked draft ComputeSDK providers from computesdk #6, not published
provider releases. Both inventories are complete in their declared scope.
The strict gates still exit 1: **893 Python and 904 TypeScript** entries remain
unverified. Neither method counts nor these scenarios establish full equivalence.

The unchanged files/process programs and new private HTTP programs passed all
twelve Python cases. Three additional E2B cases in each language preserve native
environment option names, success results and typed nonzero command errors,
including stdout, stderr, exit code and error text. They additionally cover
background PID identity, stdin/EOF, disconnect/reconnect, typed wait and kill
errors, and authoritative missing-PID behavior. The Arker E2B route succeeds
without an E2B key. Returned command handles are now included in the inventory;
the expanded denominator is intentional. The TypeScript native route supplies
its vendor constructors to ComputeSDK, preserving class identity across package
installations. Node passed the three files/process routes and six HTTP
cases: original Blaxel, shim on Blaxel, and shim on Arker, with both H2 and
explicitly disabled-H2 profiles. HTTP coverage includes every byte value,
both application auth headers, repeated-header normalization, chunked and HEAD
responses, manual redirects, streaming abort, and cleanup. Python additionally
preserves the native async client's cookies, 300-second default request timeout
and 10-second connect timeout. Default cookie paths isolate different service
ports; idle connections do not retain VM activity leases. Arker's HTTP cases
run with inbound and outbound denied. They require the draft backend private
port API, not a public ingress exception. Fresh feature tests passed all 58
private-port/HTTPS/HTTP2/WebSocket cases with zero skips.

Local validation passes 375 Python unit tests and lint, and the full TypeScript
test/build/typecheck pipeline. Source/vendor snapshots now reject changes during
a scenario; runtime profiles are included in fingerprints. Existing evidence
grouping tests also reject collisions and malformed events. These measurement
tests do not grant vendor parity credit.

A fresh Node HTTP rerun exposed a reset/abort race after an earlier passing
cohort: the body could expose ECONNRESET instead of AbortError. A deterministic
regression fails without preserving the caller's abort reason and passes with
the fix on Node and Bun. All nine Node live programs passed again afterward.
This is bounded evidence, not proof that every cancellation interleaving works.

The separate framed relay helper in arker-app #4260 now passes real Python and
Node half-close probes against a retained protocol-2.1 guest. Its boot ID, old
agent build and PID 110 stayed unchanged, and helper permissions are 0700.
The refreshed 58-case ingress/private-port selection passed with zero skips.
Bun's chunked WebSocket-upgrade refusal decoding remains unverified. Node's
chunked-refusal tests pass. Published dependencies and other runtimes are
separate profiles and are not certified by Node's report.

An immutable SDK wheel before the resource-alias fix exposed 15 real smoke
failures: public resource attributes and the vcpu_count resize argument had been
removed on SDK main. Restoring those aliases preserves the nested wire contract.
The fixed immutable wheel passed all 353 executed smoke cases, zero failures;
194 other cases were excluded by the feature environment. This certifies that
wheel, not subsequent edits or the published SDK. An interrupted earlier live
cohort failed during router-roll cleanup and remains red; a stable fresh cohort
subsequently passed all 15 Python and 12 Node programs.

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

A separate unattended-background regression failed on Arker while both native
routes completed: without further client calls, the detached command lost its
runtime activity and did not produce its output. Python and TypeScript now
launch a dedicated asynchronous Arker run whose supervisor remains until the
child reaches a terminal state. The idle daemon is not kept active forever.
After the fix, all nine strengthened live routes passed, including an eight-
second period with no VM calls before reading the child's completed output.
The new unit regressions failed before the fix; full local gates pass.

Python discarded the native `mkdir` result even on its native route, and both
Arker filesystem adapters omitted the structured success response. New unit
regressions failed in both languages; the strengthened unchanged programs now
assert the exact response and all nine live routes pass. TypeScript forwards
Blaxel network configuration to the native adapter and distinguishes strict
creation from native get-or-create. The draft ComputeSDK adapter honors the
explicit flag; Arker still rejects atomic reconciliation and network settings
instead of silently weakening their behavior.

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

- E2B handles have bounded live coverage, not complete parity. Process listing,
  request-timeout options, all callback/error combinations, foreground signal
  termination, sandbox kill return values and context-manager protocols need
  further work. Default cwd checks do not establish user/UID/HOME equivalence.
  Python's current E2B handle bridge requires SDK 2.56 and Python 3.11 or later;
  both languages still need Python 3 in the guest process image.

- Translate native Modal app/image handles on Arker. TypeScript now preserves
  them through ComputeSDK on native Modal routes, rather than silently using a
  default image. A live native-versus-shim test passed with Alpine 3.22 and exit
  code 17. Native apps/images are delegated; this does not establish Arker image
  parity or grant instrumented coverage.

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
- Complete private HTTP option/session coverage, including native async
  persistent cookies and timeout defaults. Requests currently open separate
  HTTP clients. External redirects and fetch integrity checks are not yet
  implemented. Native root-relative redirect behavior also needs coverage.
- Translate and differentially validate Blaxel's current `network` configuration.
  The pinned SDK includes proxy domain filtering and optional enforced firewall
  rulesets; absence of translation is a shim gap, not an absent vendor feature.
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
