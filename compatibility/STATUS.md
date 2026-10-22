# Recorded coverage baseline

## Additive policy readback preview

The companion backend now keeps the legacy policy response rule union unchanged
and returns independent floors in optional `outbound_guards` maps. SDK reads
preserve the map, and policy writes through update, fork, run and the deprecated
policy writer convert it back to tagged input rules without mutating callers.
Explicit edits and null removals override the corresponding readback floor.
Real Python and Node regressions failed before this change. The contract is
regenerated from app source `8bd63a20`; its exact current-main oasdiff gate passes
without severity changes or accepted-breaking-change exemptions.

Fresh production cohorts at source `9fdf196` complete all 36 Python and 31 Node
configurations: **32/36 Python and 26/31 Node pass**. Production still lacks
guard-schema/private-port support; all four core/expanded strict gates exit 1.
Core coverage remains **94/1,123 Python and 51/982 Node**, expanded **94/1,146
and 51/1,052**. Safe source-bound observations are committed as
`evidence/production-additive-20261022-*.jsonl`. This does not certify the
unpublished backend or full API equivalence. No deployment or policy weakening
is used.

Current local gates pass **529 vendor Python / 520 lean Python** (nine optional
skips), Ruff, TypeScript full typecheck/tests/build, five isolated packed
ESM+CJS consumers, and CLI plus PTY tests. Packed npm SHA-256:
`7bf89113067575bcbdf991e0f42fa8aa0dec8ada35b636c4e6def26935fcf3a9`.
The CLI test oracle now expects response-only `mitm_domains` to be omitted from
updates, matching the SDK writer; its prior CI failure is retained.

## Current provider SDK pins and earlier source-bound validation

Python fixtures now pin E2B **2.58.0**, Daytona **0.226.0**, Blaxel **0.4.19**
and unchanged Modal **1.6.2**. TypeScript pins E2B **2.58.0**, Daytona **0.226.0**,
Blaxel **0.3.31** and unchanged Modal **0.11.1**. Daytona's optional peer range
retains the previously tested 0.224 series alongside the current 0.226 series.
`provider-upgrade-20261022.json` records the exact core API delta: **eight Python
and four Node APIs added**, none removed or signature-changed. This includes
E2B's new command/PTY input-stream methods and returned input handles, including
Python async context-manager protocols. Inventory regressions failed before
the returned-handle roots were added. Core totals increase to **1,123 Python /
982 Node**; these new APIs remain unverified on Arker, not parity credit.

Current-pin gates pass **523 vendor Python / 514 lean Python** (nine optional
skips), Ruff, TypeScript typecheck/full tests/build/isolated packed ESM+CJS
consumers, 25 repository tooling tests and generated-contract checking. Packed
npm SHA-256:
`7c5a4ec978c0dd6c9465326f785b5bcebc4ac338130a74dec4a0766d4e2543b0`.
Fresh matching-version production cohorts at source `816cdc6` execute all
36 Python and 31 Node cases: **32 Python pass / four fail; 26 Node pass / five
fail**. Every failing case is a missing private-port route or rejected
outbound-guard rule, with no new native-provider-version mismatch in this
selection. All four core/expanded strict gates are actually invoked and fail
exit 1. Core coverage: **94/1,123 Python and 51/982 Node**; expanded:
**94/1,146 and 51/1,052**. Full API equivalence remains false. The 45 safe
records, including failures, are committed as
`evidence/production-current-20261022-*.jsonl`. All 11 GitHub checks pass at
the tested source. Owned fixtures are cleaned up; no feature deployment or
IAM changes are used. Earlier fingerprints below remain historical.

The first fresh packed-consumer install failed because `dockerfile-ast`'s
floating dependency resolved to `vscode-languageserver-types@3.18.5`, whose
[registry-advertised tarball](https://registry.npmjs.org/vscode-languageserver-types/-/vscode-languageserver-types-3.18.5.tgz)
returned HTTP 404. An explicit compatible 3.18.0 dependency constraint restores
actual fresh installs; this is not a test-only npm override. The failing run is
retained. Production-only npm audit reports **zero advisories**; the development
and fixture graph still reports **12 advisories (11 high / one low)**, including
the old ComputeSDK Daytona fixture's transitive dependency chain. Those are not
waived or represented as a clean full dependency audit.

## Current SDK main integration

Source `8cb23cf` includes SDK main `4f1b1d9` and its 1.3.0 SDK release,
API-key contracts and subprocess-lifetime controls. Generated contracts match;
522 Python units, Ruff, TypeScript typecheck/full tests/build/packed consumers,
25 tooling tests, CLI checks and Go vet/build/unit tests pass. All 11 GitHub
checks pass. Initial merged-unit validation found the new upstream exact policy
union assertion omitted this branch's intentional `OutboundGuardRule`; the
corrected exact union preserves the remaining withdrawn-shape checks.

Fresh production cohorts at this source execute **all 36 Python and all 31
Node configurations**, with independent Node cases continuing after failure.
Results: **32 Python pass / four fail; 26 Node pass / five fail**. Failures are
the missing private-port route and rejected outbound-guard schema described
below, now exercised in all HTTP transport/allowlist profiles. All four core
and expanded strict gates are invoked and remain red, exit 1. Core coverage:
**94/1,115 Python and 51/978 Node**; expanded: **94/1,138 and 51/1,048**.
The 45 credential-free records, including failed cases, are retained under
`evidence/production-main-20261022-*.jsonl`. This is not full API parity or
certification of unpublished backend changes. Python wheel SHA-256:
`42f0136cd3035355ab76dcef9ede2ac41703cffb32708ec3b42dd4ba29b9ded5`.
Packed npm SHA-256:
`4e7776a24ecb4a4924fb4ffe25aee7c2b5e561fe097bfc2f3cb655d55f652a5d`.

## Native Blaxel watcher workspace isolation

The TypeScript native adapter now gives synchronous filesystem watchers their
own credential epochs for request construction, stream-header acquisition and
subsequent `withContent` reads. It does not hold the global epoch while waiting
for watch events, so another workspace can make progress while a stream stays
open. Closing a watcher before its queued start prevents the request.

A deterministic regression first reproduced all three operations incorrectly
using the second workspace. The corrected regression and a real native SDK
HTTP-stream test verify workspace headers and delayed content reads. TypeScript
typecheck and the full test/build/isolated-consumer suite pass. Fresh checks on
2026-10-22 also pass the unchanged Blaxel TypeScript program through all three
routes (native, shim-native and shim-Arker), using the supplied production key
against the existing us-west-2 API. Owned fixtures were deleted. The first run
omitted the required Arker endpoint and failed before any Arker VM creation;
the corrected explicit-endpoint run passes. This does not certify unpublished
backend changes, Arker watcher support or full API parity. Strict inventories
remain red; the preceding source fingerprints are historical.

The complete production selection at source `8361b4a` finished with **32/36
Python cases passing**. Two private-fetch cases fail because production lacks
the authenticated private-port route (typed HTTP 404); two empty-Modal-allowlist
cases fail because production rejects the unpublished `outbound_guard` schema
(HTTP 400). Node executes 24 cases: **22 pass / two fail** for the same missing
backend primitives. Its private-fetch and empty-allowlist programs stop at their
first failing case, so seven later configurations were not executed; they are
not passes. Both expanded strict gates were actually invoked and fail exit 1.
Expanded scenario coverage is **94/1,138 Python and 51/1,048 Node**; full API
equivalence remains false. The credential-free 39 records, including failures,
are retained as `evidence/production-20261022-*.jsonl`. These source-bound
observations precede the subsequent merge of SDK main and must not be presented
as certification of that merge. No policy weakening, public-ingress fallback,
deployment or IAM changes were performed.

## Blaxel absolute filesystem paths

Fresh native-versus-Arker observations reproduced incorrect root names,
trailing-slash directory names/child paths and repeated-slash display paths.
Both shims now distinguish the formatted display path from the cleaned I/O
target, matching the observed absolute-path behavior without changing generic
provider path helpers. Three Python regressions and the TypeScript regression
failed before correction. Unit gates pass **492 vendor / 483 lean Python**
tests (nine optional skips), Ruff check/format, TypeScript typecheck and the
full test/build/isolated-consumer suite. Packed npm SHA-256:
`6fbff950daef6efbb16f5e5d9d64e50181614640080e685f6a99c5e2e483ed44`.
Updated import-only programs cover root, trailing and repeated absolute paths.
Live checks against backend `d21cfae` pass **all six Python sync/async cases
and all three TypeScript routes**, each running the unchanged native program
through reference, shim-native and shim-Arker configurations. Owned fixtures
were deleted. This targeted result does not replace fresh complete cohorts.

The native TypeScript SDK itself exhibits operation-specific relative-path
encoding: a nested relative text write was observed at `/relative-check/file`
while a bare relative directory remained under `/blaxel`. The shims do not yet
reproduce that behavior. Relative paths, complete metadata and custom Arker
permissions remain explicit gaps; this absolute-path fix is not full parity.
Both fingerprints changed again; previous cohorts below are historical.

## Network preflight safety and empty Modal allowlists

Both languages preserve Modal's distinction between an omitted allowlist
and an explicitly empty allowlist. Empty lists now attach a deny-all egress
guard instead of silently creating an unrestricted VM. Daytona's empty-string
semantics remain unchanged. Malformed allowlist values are rejected before
I/O rather than treated as absent. The shared TypeScript adapter also rejects
four untranslated policy/lifecycle controls it previously accepted and dropped.
The Modal contract is documented at
[Networking and security](https://modal.com/docs/guide/sandbox-networking).

Regressions reproduced the fail-open behavior before correction in both
languages. All **489 vendor / 480 lean Python tests** pass (nine optional
skips), as do Ruff, TypeScript typecheck and the full test/build/isolated
consumer suite. Packed npm SHA-256:
`6eae1a340223fcc4df10b4044c11aa54b78c7d4e5ddf5d5bb7eaecd264f17485`.
Both fingerprints changed again. Live traffic against feature backend
`d21cfae` reproduced **two empty-allowlist failures and one positive-control
pass** with the preceding immutable `7f42d07` wheel. The corrected source
passes **all nine cases**: native Modal, shim-native and shim-Arker, each with
unrestricted, empty-CIDR and empty-domain configurations. Every unrestricted
control connects; every empty allowlist blocks. Owned parents/children were
deleted. Native app/image setup differs from Arker's image-parent binding, so
these checks grant no additional vendor API coverage credit. Fresh complete
cohorts remain pending the backend's broader live gates. These observations
do not certify native DNS behavior, private-network exceptions or full policy
parity.

The equivalent TypeScript traffic program also passes **all nine cases** on
the same feature release through the real published ComputeSDK factory, with
unrestricted positive controls and both empty-list restrictions. It creates
and deletes its own parent/children, restores the router environment and
closes native clients. Both language suites intentionally grant no extra
vendor API coverage for this differently configured creation setup.

## Blaxel filesystem option safety

Sync Python now retains the native Blaxel filesystem handle, preserving its
method surface, operation identity and options. TypeScript no longer silently
ignores custom `mkdir` permissions on Arker; both languages reject unsupported
permissions before I/O. Native routes retain native permission support.
Custom Arker permissions remain a parity gap, not a verified capability.

Both regressions failed before correction. Corrected full gates pass:
**481 vendor-enabled / 472 lean Python tests** (nine optional skips), Ruff,
TypeScript typecheck and full test/build/isolated-consumer suite. An older
native test double omitted the SDK's permission parameter; its initial full
run failed one case, and the corrected signature/forwarding assertions pass.
Packed npm SHA-256:
`829274725062e83deb209fef1d4aedb8c0d9aa74c96a2db537a683f452da9a8e`.
Both fingerprints changed. The completed `4951ec9` cohorts below are
historical, not certification of this filesystem change. Fresh live gates
are pending the isolated backend rollout.

## Modal native resource services

The TypeScript Modal client preserves native secrets, volumes, cloud-bucket
mounts, sandbox snapshots, proxies and environments through its existing lazy
native client. The missing-secret-service regression failed before correction.
Focused tests verify service identity and genuine native inline Secret and
CloudBucketMount handles without creating remote resources. Typecheck and the
full test/build/isolated-consumer suite pass. Packed npm SHA-256:
`c003abdc39e6e85517eeb67591a8f1d05d19176bd73cc518e379dc2c37d047d0`.

This is native-route preservation, not Arker resource translation or parity
credit. Named secrets and volume semantics remain in the strict core inventory.
Fresh matching-source cohorts at `4951ec9` against feature backend `51c0ce8`
pass **27 Python / 22 Node** cases. Both expanded and both core strict gates
were invoked and remain **RED, exit 1**. Core scope still verifies
**100/1,115 Python and 53/978 Node**; expanded scope remains 100/1,138 and
53/1,048. Native service exposure grants no behavioral parity credit.
Python fingerprint remains
`6181f3a9f9a0df051caff60f3eddcae81de233550dc4652097a68109d7fdf77a`;
current Node fingerprint is
`f3a3134188a3eea30b1cdddd82f81f6d7e4be822280b076f7c2841255a043e90`.
Credential-free traces are retained as `evidence/feature51-resources-*.jsonl`.
All 11 latest SDK GitHub checks pass. This does not certify the newer backend
release currently being built for the isolated feature environment.

## Blaxel directory paths

TypeScript directory listings now include the directory name/path and every
entry's path. A regression failed before correction. Typecheck and the full
TypeScript test/build/isolated-consumer suite pass; the unchanged Blaxel program
passes on native Blaxel, the shim's native route, and Arker feature backend
`51c0ce8`. All owned sandboxes were deleted by the program's cleanup.
Packed npm SHA-256:
`23bade1ded0f7011b85f338712bc92cfa0e3341657654f2d3e55f51b97cc56b6`.

This changes the Node fingerprint. Earlier full-cohort counts below are
historical until refreshed; the targeted three-route run is not full parity.
File owner/group, permissions, timestamp and symlink behavior remain gaps.

## E2B binary filesystem update

At `63e9003`, buffered binary reads preserve all byte values as native
`bytearray` (Python sync/async) or `Uint8Array` (TypeScript). TypeScript accepts
the native `ArrayBuffer` write input without text conversion. Unit regressions
failed before correction. Streaming formats are rejected before I/O, not
implemented as fake buffered streams. Arker's per-call filesystem identity,
timeouts, streaming, Blob uploads and write metadata remain gaps.

All **479 Python vendor / 470 lean tests** pass (nine optional-vendor skips),
with TypeScript typecheck and the full test/build/isolated consumer gates.
Packed npm SHA-256:
`447b274b658172a7ab0d62f437ac4e038bdda8475e4978b730b0bae28437e163`.
The import-only differential programs now assert binary write/read, exact
native return types and default UTF-8 text behavior. Fresh matching-source
cohorts against backend `51c0ce8` pass **27 Python / 22 Node** cases. Both
expanded and both core strict gates were invoked and remain **RED, exit 1**.
Core scope verifies **100/1,115 Python and 53/978 Node**; expanded scope is
**100/1,138 and 53/1,048**. Complete declared inventories still report
`full_api_equivalence=false`. Credential-free traces are retained under
[`evidence/`](evidence/README.md).
Python fingerprint:
`6181f3a9f9a0df051caff60f3eddcae81de233550dc4652097a68109d7fdf77a`.
Node fingerprint:
`13f75b540c5405a779577598a2dfcf82a06959b72fee943f74e41abad01aaf04`.

## Native E2B filesystem preservation

At `637bc09`, sync/async Python and TypeScript retain the native E2B filesystem
handle rather than translating it through a smaller wrapper. Regressions first
failed in both languages; binary options, operation identity and write metadata
are preserved on native routes. Arker's TypeScript filesystem rejects extra
options before I/O instead of silently dropping them. This is not proof of
Arker binary-format, stream, identity or metadata parity.

All **476 Python vendor / 467 lean tests** pass (nine optional-vendor skips),
as do TypeScript typecheck and the full test/build/isolated packed-consumer
gates. Packed npm SHA-256:
`c4dfade935999ee9791e6c068316cf745fe8e531a1cba538c79a85c846589a96`.
Both runtime fingerprints changed; fresh differential evidence is pending.
The preceding cohorts below are historical, not current certification.

## Route-cache recovery update

At `48cbf9c`, six new Python regressions first reproduced crashes on valid
non-object JSON cache roots and acceptance of boolean `true` as version `1`.
Explicit object/integer validation fixes both. The same six recovery cases
pass in TypeScript. All **474 Python vendor tests**, TypeScript typecheck,
and the full test/build/isolated packed-consumer gates pass. The npm artifact
hash is unchanged because the runtime correction is Python-only.

The Python implementation fingerprint has changed. The live cohorts and
fingerprints below certify the preceding source, not this change; fresh
matching-source differential evidence is pending. No additional vendor API
coverage or full-parity credit is claimed by these cache tests.

## Core-sandbox measurement update

Both reporters now accept `--scope core-sandbox`, using the same fingerprinted
scope contract and listing every excluded App/Function API with its reason.
The expanded inventory remains the default. Storage, mounts, secrets, images,
sandbox methods and unknown APIs remain in the scoped inventory. Regressions
failed before the change. Python gates pass **468 vendor / 459 lean tests**
(nine optional-vendor skips); TypeScript typecheck and full test/build/packed
consumer gates pass. Fresh matching-source cohorts against backend `45def92`
pass **27 Python / 22 Node** cases. Both expanded strict gates and both
core-sandbox strict gates were invoked and remain **RED, exit 1**.
Core-sandbox scope verifies **100/1,115 Python** APIs and **51/978 TypeScript**
APIs. The expanded counts remain **100/1,138** and **51/1,048**; exclusions
account for 23 and 70 App/Function entries, without adding verified behavior.
Python fingerprint:
`8349d5e51facc05749a9314b165df804a630ddfebb540ec2a8a7e92e09b40846`.
Node fingerprint:
`d4c81255009b6fcbd32ce4447c36a54c0ca4ff61d335b75f0bf7c77dd4b88de9`.
Both inventories are complete for their declared roots;
`full_api_equivalence` remains false. The older fingerprints below are
historical. The packed npm hash is unchanged after the full build.

## Previous runtime checkpoint

The `e43dcaa` process checkpoint translates native Blaxel execution timeout
seconds to internal milliseconds and translates ComputeSDK milliseconds back
to native seconds. Live differential tests then exposed signal exit code
137 versus native -1; the shared runner now supports Blaxel's convention without
changing explicit positive exit codes or other providers' conventions. Unit
regressions failed before both fixes. The first live checkpoint at `1c7ab21`
had **25 Python passes / two failures**; that evidence is retained.

Current gates pass **466 Python vendor tests / 457 lean tests** (nine optional
vendor skips), full TypeScript typecheck/test/build/isolated-consumer gates,
and all SDK CI/CodeQL checks. Fresh matching-source cohorts against isolated backend
`45def92` pass **27 Python / 22 Node** cases (25/21 differential, 2/1 Arker-only),
including the new timeout assertions. Fingerprints: Python
`ace0a190770e60ba63a1a2c0c82754da414dd882cc1cfc26b852c09bfa6bc492`, Node
`ae63b8ee881af515873f0947acbff2ee6277668f49b5c7c7825df0a7b3b968b7`.
Both strict gates were invoked and remain **RED, exit 1: 100/1,138 Python;
51/1,048 Node verified**. Full API equivalence remains false.

Current npm artifact SHA-256:
`535b80a9e3d3328183ab8f076b112c46440cf5bf0b76576efcc2d1a7d237864c`.
It passes live ESM and CommonJS probes in the existing E2B-only consumer:
image-parent inheritance, create/default cwd, NUL-containing filesystem data,
fresh shells, native command-error identity and deletion. All owned parents
and children were removed. This is supplemental Arker-only evidence, not
additional native API coverage. Immutable Python wheel SHA-256:
`549a5320197461be5f95fd4489bfb9e2d8f92cff0e69eb52fe1487c4655cea46`;
it passes **645 harness unit tests** in the updated harness.

Backend `45def92` passes **12/12 live outbound-guard tests**, zero skips/errors,
after the preceding five-pass/seven-failure run found guards dropped during
encrypted storage. Current transport rerun remains **RED: 100 passed / one
fixture error**, a file-sync response that never arrived before the 300-second
test timeout. A fresh isolated ingress suite passes **30/30**, but the root
cause is unresolved. An earlier unanswered POST fork also remains unresolved;
idempotency-key reconciliation owns its cleanup.
The router did not restart; 800 bounded read-only health requests across both
HTTP clients/reuse modes passed without explaining the failure. The retained
protocol-201 guest passes Python and Node binary Unix transport with boot ID,
agent PID/start time and binary hash unchanged. No worker/agent restart or
retry waiver is credited as a fix.

The same release passes **43/43 shared-filesystem, sync-contract and filesystem
operation tests**, including mount statuses and independent-VM visibility,
with zero skips/errors. These are Arker behavior checks, not native
Drive/Volume equivalence. The broader network-policy gate remains red at
347 passes, six failures and one environment exclusion; follow-up diagnostic
and test corrections still require Linux and live validation.

## Previous creation-routing checkpoint

The creation-routing checkpoint replaces error-name/status heuristics with an
explicit local-preflight refusal type. Opaque exceptions, wrapped opaque
failures, and HTTP 429 reproduced duplicate fallback in both languages before
the fix; those regressions and safe-preflight fallback now pass. Python profiles
pass **464 vendor tests / 455 lean tests** (nine optional-vendor skips). Full
TypeScript typecheck/test/build/isolated-consumer gates pass; packed SHA-256:
`e0240b98086297795fafa6ad09f8799fff4829cae0f198fedc81538af57e6606`.
Fresh matching-source cohorts at `54ed65c` pass **27 Python / 22 Node**
cases (25/21 differential and 2/1 Arker-only). Fingerprints: Python
`967e077fedcf67d57fba20b686aea4ba8236451665290229a6815ac4445e13f3`, Node
`6068cf0fe57494ea5d9bb7dd6c5220cb2981ad7ab3ac6d6989c465816ecba6a1`.
Both strict gates remain **RED, exit 1: 100/1,138 Python; 51/1,048 Node**
verified. These pre-deployment cohorts do not certify the new guard backend.

The exact packed npm artifact also passes supplemental live ESM and CommonJS
checks from a clean E2B-only consumer: image-parent inheritance, E2B create,
default cwd, NUL-containing filesystem data, fresh shells, native command-error
identity and deletion. All owned parents/children were removed. The first
probe incorrectly supplied an unsupported `arker` create option and failed
before VM creation; that evidence is retained separately. These probes are
Arker-only, not additional native differential coverage. Neither generic
remote-error failover nor full API parity is established.

## Previous outbound-guard SDK checkpoint

The outbound-guard checkpoint replaces destructive native-network translation
with independent egress permission floors in both languages. Configured keyed
rules are merged, ordered replacements retain their replacement semantics,
and policy secrets are preserved. New regressions reproduced rule loss before
the fix and now pass for both configured policy forms. This requires the
matching backend guard API; it is not yet deployed or live-certified.

Python unit profiles pass **460 vendor tests / 451 lean tests** (nine optional
vendor skips). TypeScript typecheck, unit/build and isolated packed consumers
pass; packed SHA-256:
`c1ba328eaeb2c5e1cac7c05ec486bc088a617b8ffb6d50621f5fe0b5393ffe60`.
An additional ordered-policy preservation assertion and TypeScript typecheck
also pass. Matching-source cohorts at `ab59123` pass **27 Python / 22 Node**
cases (25/21 differential and 2/1 Arker-only). Fingerprints: Python
`1ddfc1316bab8d7cf2327e3f007896dbb93fc075d0bf89aded5b6791f66de991`, Node
`f8256fdf93c3dfd918324bcec952fecc1be5c721af418cd8282676e241aa365c`.
Both strict gates were invoked and remain **RED, exit 1**:
**100/1,138 Python; 51/1,048 Node** verified. These scenarios run against
feature release `2a5419e`, not the new guard backend, and do not certify guard
enforcement. Previous fingerprints below are historical, not current evidence.

## Previous Blaxel fetch checkpoint

The `3272a84` checkpoint fixes Blaxel fetch transport selection: regionless
creation uses Fetch semantics, while H2 eligibility is captured at creation
and still honors a later disable flag. A local regression failed before the
fix and now covers four profile transitions. The full TypeScript
typecheck/test/build/isolated-consumer pipeline passes. Packed SHA-256:
`f173c572cea47832590ab04899108f3ce726fa3753639432e4f4e04ba4ba8cd3`.
Python is unchanged from its **458 vendor-unit / 449 lean-unit** checkpoint
(nine optional-vendor skips in the lean profile).

Matching-source live cohorts pass **27 Python/22 Node cases**, comprising
**25/21 differential plus 2/1 Arker-only cases**. Node explicitly verifies
native H2 selection and separately tests disabled-H2 and regionless Fetch.
The first attempt incorrectly assumed enabling H2 selected it without a
region; the native SDK's actual fallback exposed the corresponding shim bug.
Failed evidence remains separate. Current fingerprints are Python
`b82c4d90dc85fbda5f6f5d938b79263bae777609fa01894d4921d8d803f6b6ee`
and Node `0811e6990cec9bd170df3bf75f09211d015684dca3ee04be3c37a57b2fab53e2`.
Both strict gates remain **RED, exit 1**: **100/1,138 Python; 51/1,048 Node**
verified. Neither inventory establishes full core API equivalence.

The permanent Python and Node authenticated-registry tests now pass against
feature release `compat-private-port-g2a5419e5b3f9-235399e4`, including cached
image bad-auth rejection, required typed recovery metadata, credential redaction
and three isolated child forks. Supplemental forks also pass after deleting
the registry fixture. All registry/parent/child test VMs were cleaned up.
These checks are Arker-only, not native API differential coverage.

That release passes **101/101 declared inbound/private transport cases** with
zero skips/errors. A preceding run passed 100 and failed one policy PUT on a
connection closing before a response. The failed fixture was deleted, neither
worker nor router restarted, a fresh isolated assertion passed, then the full
101-case rerun passed. The transport disconnect remains unexplained, not waived.
Backend Linux Rust/Clippy pass at the deployed checkpoint; subsequent generic
outbound-guard storage work is not deployed or enforcement-certified. Policy
composition, native resource handles, identity and other gaps below remain.

## Previous required-recovery checkpoint

The `2c687cd` checkpoint makes `OperationFailed.recovery` required in both
generated language types, matching the existing wire schema. A real private
registry negative test found that the backend omitted this required field on
image authentication failures. The backend fix supplies truthful `unknown`
recovery when no more specific execution state is available; its feature
deployment and full Linux gate are still running. The permanent Python and
Node registry tests must pass against that release before this issue is closed.
The authenticated import and three independent forks after deleting the
registry fixture have already passed in supplemental probes; these are not
native-provider differential coverage.

At this checkpoint, Python vendor units pass **458 tests** and the lean profile
passes **449 with nine optional-vendor skips**. The full TypeScript
typecheck/test/build/isolated-consumer pipeline passes, as do the current SDK
GitHub checks. Packed SHA-256:
`143115622e269c5b45ca5f1f7f3605d37db454672bcfc4a49089582873e34ebc`.

The preceding `6168dbe` source cohort passed **27 Python/19 Node cases**
(25/18 differential plus 2/1 Arker-only). Its strict reports remain
**100/1,138 Python** and **51/1,048 Node** verified, not full parity. That
evidence predates the generated-type change and is not current-source evidence.

The deployed Unix transport checkpoint passed **101 declared inbound/private
transport cases**, with no failures or skips. Python and Node also passed TCP
and Unix byte-stream probes against the retained protocol-201 guest without
restarting its original agent. This certifies those transports, not a native
Blaxel daemon integration.

An additional unresolved policy-composition gap is explicit: translating a
provider network restriction can replace configured rules, dropping inbound,
schedule/lifecycle controls or secret-header rewrites. Preserving the named
secret map alone does not establish policy equivalence. A safe composition
must intersect egress permissions rather than append a bypassing allow rule.

## Previous Unix-socket SDK checkpoint

The private Unix-socket SDK checkpoint adds Python `VM.connect_unix` and Node
`VM.connectUnix`, sharing the existing owner-authenticated binary/EOF stream
transport. Its new unit cases check exact control-character path encoding,
UTF-8 byte limits, authentication, framing and rejection before network access.
Python's vendor unit profile passes **457 tests**; the full TypeScript
typecheck/test/build/isolated-consumer gate and OpenAPI artifact check pass.
Packed SHA-256:
`672abdc49389ae3b2a921712c8fc507fe4f9b4a2574979a6f6e972188f5f00c5`.
Backend Linux Clippy passes; full Rust and live transport validation are in progress.
This does not install a native daemon or establish additional provider parity.

Matching-source live cohorts at `6124bf2` pass **25 Python/18 Node differential
cases**, plus **two Python/one Node Arker-only image/policy cases**. Both strict
inventories remain red: **100/1,138 Python** and **51/1,048 Node** verified.
Fingerprints are Python
`aa29c8c2bcc3faf0f7e549a0dfc29155373eb7199b58df226934a72e247470ba`
and Node `f131d3b402ce4307d395aeb7848ea6f9a98fa452369786658ee9eb3956ddac76`.
These run against the preceding feature release and do not certify the new
Unix endpoint. A first attempt omitted the required evidence run ID and failed
recording (24 Python failures, three passes; Node stopped at the first case).
New regressions failed before adding import-time preflight checks in both
languages, preventing paid calls under incomplete evidence configuration.
The corrected cohorts above use an explicit run ID and preserve source identity.

## Previous literal-path checkpoint

Literal filesystem paths now survive both shared TypeScript shell operations
and Python Blaxel listings. Three leading-dash mkdir/list/remove regressions
failed before adding option terminators. Python's NUL-delimited listing also
needed a pipe: GNU find replaces control characters with `?` on a terminal.
The new real-PTY regression failed before correction, and a success marker
detects failed/truncated producer output despite the pipe. Listings retain
native lexical order. This still requires GNU find on the Python guest path.

An expanded differential program initially failed six cases: native Python
Blaxel rejects control characters in file-write URLs, while Arker accepted
them but sanitized their listing. The listing scenario now creates these files
through process execution, which is supported by every route; the write-URL
validation mismatch remains a separate gap, not a waived equivalence claim.

Fresh matching-source cohorts pass **25 Python/18 Node differential cases**
plus **two Python/one Node Arker-only image/policy cases**. Python vendor units
pass **449**, lean **440 with nine optional-vendor skips**. Ruff lint/format
and the full TypeScript typecheck/test/build/isolated-consumer gate pass.
Packed SHA-256:
`ae375cda155348067afe6b7ef04b5ec64a7a45ab5858cb19b76b7561d9d34ae8`.
Current Python fingerprint:
`f5039f5f13e1aa3fa67845b8c71d02d548388131a735baf7011df7b72110b422`;
Node: `0dc397cdd142853713fed578fa3be023614165b3ac4f3affcaf2a535f7ea5068`.
Strict reports remain **100/1,138 Python** and **51/1,048 Node**, both exit 1,
full equivalence false. These changes have not removed the remaining gaps.

The same random-write/append/rename/mmap/SQLite program passes on a native
Blaxel persistent volume and on an Arker FUSE mount. Native volume cleanup first
returned 400 after sandbox deletion; the exact probe sandbox was subsequently
TERMINATED, and explicit cleanup confirmed both sandbox and volume absent.
An additional two-VM Arker mount probe observed an immediate remote overwrite
after close. Neither result certifies all mount semantics or shim adapters.

## Previous image/secret checkpoint

The requested target is now core sandbox parity; Apps and Functions stay
native. Drives/mounts, secrets and image inputs remain in scope, not waived.

The shared fork path now supports Python source VM IDs (including image-backed
parents), configured host policy secrets in both languages, and registry
credentials restricted to image-import requests. Provider network constraints
replace configured network rules without discarding named secrets. These are
explicit Arker settings, not translations of native Secret/Volume/Image handles.
Private-registry authentication has request-shape tests, not a live private
registry fixture.

New feature-environment checks pass in Python and Node: one Ubuntu registry
import followed by three distinct child VM forks, inherited parent files,
isolated child writes, masked inherited policy secrets, no corresponding guest
environment variable, and egress denial with a successful pre-policy connection
control. Python additionally passes an allowed HTTPS header rewrite containing
a named synthetic secret and a denied HTTPS destination with its own positive
control. TLS-classification denial resets the connection before HTTP; a bare
TCP connect is not an L7 enforcement check.

The TypeScript shared filesystem adapter also fixes newline-delimited filename
corruption, omitted dangling symlinks and leading-dash stat arguments. Both a
real-shell regression and the live Node API check cover these names.

Fresh full differential cohorts pass **25 Python and 18 Node cases**, plus
**two Python and one Node Arker-only image/policy cases**. All **443 Python
unit tests**, Ruff and the complete TypeScript typecheck/test/build/isolated
consumer pipeline pass. Packed SHA-256:
`80f6b059ca5b2b05e8e54ac384252ace88e2c5576b346bb7ff99232fd8cab6a9`.

Blaxel's Python import forwarding now preserves core Drive, Volume, Secret,
Snapshot and Image helpers, rather than only the sandbox module's exports.
These helpers remain native, not Arker implementations. The expanded inventory
records them in both languages: Python **100/1,138** scenario-verified (1,038
unverified), Node **51/1,048** (997 unverified). Both inventories complete their
declared roots and both strict gates exit 1; full equivalence remains false.
Current fingerprints are
`3ee7fb112ecc413cb1bb9c29323143ec07ec3a13db74a696a5b855577d8f021f`
and `be0bf4fe257a7dd774e3e78945676b894519f27845be0356af258257edb89637`.
The earlier differential fingerprints below are historical. Additional
Arker-only tests do not grant native-equivalence coverage.

The lean Python profile passes **434 tests with nine optional-vendor skips**.
GitHub's formatter caught two new test files before correction; the formatted
head passes all CI/CodeQL checks and the repeated Python live cohort passes
27 cases with the new fingerprint above.

An additional isolated Arker filesystem/mount probe passes random writes,
append, rename, writable mmap and SQLite on the live FUSE mount. This does not
establish multi-VM cache coherence or native mount equivalence. Native Blaxel
Drive creation and read-only listing both return HTTP 403: "Drives feature is
not enabled for this workspace". No drive was created. Drive parity remains
unverified; the workspace setting was not changed.

The latest Modal descendant-lifetime fix keeps the managed Arker run alive
until a Linux subreaper has reaped detached descendants, while the native
process wait/poll still reports the foreground exit immediately. Before this
fix, the VM went idle after foreground completion and a delayed child did not
make wall-clock progress until guest execution resumed. Native Modal completed
the child's delayed output/file write during that interval. The new three-route
programs in both languages now agree, including the delayed output and file.
Local regressions cover ordinary backgrounding and double-fork/setsid adoption.
This supervision path requires Python's ctypes and Linux PR_SET_CHILD_SUBREAPER;
it is not arbitrary-image process support.

Fresh source-matched cohorts again pass **25 Python and 18 Node live tests**.
Python reports **100/1,013** verified (913 unverified), Node **51/967** (916
unverified), both strict exit 1. Their current fingerprints are
`506a4478bd847417383196328118ea09b4c3d90ee35f67c246565b99d2683476`
and `efe7224bbd37f550434a380d88ff1893d2f56dfa4d86dab96c9f77e329faba25`.
All **439 vendor Python unit tests** pass; lean passes 431 with eight optional
vendor skips. Ruff and the complete TypeScript typecheck/test/build/consumer
pipeline pass. Latest packed SHA-256:
`43957de1d76750cd72e2253407457d4448f74be96c71e74b0098159acf99de5c`.

The latest shared-supervisor change executes Modal argv directly in both
languages instead of through an extra shell. Three regressions failed before
the fix: missing executable, missing cwd, and literal argv/signal identity.
Startup failures now return terminal native process handles with exit 128;
the OS diagnostics still differ from Modal's runtime-specific stderr wording.
The live checks assert exit status, handle type, empty stdout and the relevant
diagnostic path, not exact diagnostic equality.

Fresh matching-source cohorts pass **25 Python and 18 Node live tests**, using
all four native credentials and the isolated Arker environment. Python verifies
**100/1,013** entries (913 unverified), Node **51/967** (916 unverified); both
strict gates exit 1. Fingerprints are
`f747a04f507ab724c5436c2702e2f86754a92e7ed034be35714ae977e2e3d9fe`
and `b5b0dd1ce42dee214d87a89e0dc3264182e473a5cc5fd9c76cadcae24269278a`.
All **437 vendor Python unit tests** pass; the lean environment passes 429
with eight optional-vendor skips. Ruff, exact TypeScript CI typecheck, full
test/build and isolated packed-consumer gates pass. Packed SHA-256:
`20b37d5a3f43d37dd561c9a6551719beb3033e09029b2d1d881b761d748b3f68`.
The retained protocol-201 VM also passes Python binary/half-close and three
Node echo probes again. None of this establishes full API equivalence.

The latest Python Modal native-route regression preserves the original process
object, all native exec keyword arguments and terminate(wait=True)'s exit
status. Four regression cases failed before the fix. An initial env=None fix
incorrectly assumed that Modal unsets variables. Pinned native source and a
real three-route comparison established that None omits the override instead:
inherited HOME stays intact. Both the implementation and regression expectation
are corrected; the earlier Arker-only unset probe was not equivalence evidence.
A real Modal sandbox passed binary
stdin/stdout/stderr, EOF, wait and termination. This native-only test does not
grant Arker-route coverage.
An initial manual probe omitted Modal stdin.drain()
and blocked; the checked-in program uses the documented flush operation.
The frozen reports below predate this source change and are not current-source
parity certification. The Python inventory now explicitly includes returned
Modal ContainerProcess and stdin/stdout/stderr sync/async surfaces, which the
Sandbox.exec annotation does not expose. Its denominator is **1,013**, up by
38; a regression prevents removing these roots. A fresh frozen-source fc72ec7
cohort passed all **22 live tests**, using the supplied native credentials and
the isolated feature environment for Arker. The locked-runtime report verifies
**86/1,013** entries with **927 unverified** and exits 1 as required. The added
native-only Modal case grants no Arker parity credit. All 423 current Python
vendor unit tests pass. This expansion does not retroactively grant coverage
to earlier evidence. All SDK GitHub checks passed at fc72ec7.

The new Python Modal Arker process bridge returns the vendor's actual
ContainerProcess and stream classes over the shared guest supervisor. Its
three-route import-only program passes 256 KiB binary stdin/output, EOF,
poll/wait/returncode, repeated output reads, literal shell argv, environment
isolation and None-override omission. Arker runs with native keys removed and
inbound/outbound denied. Fixtures create sandboxes separately; this does not
certify native app/image-builder creation. Unit tests also cover native .aio
stream/wait methods and incremental Unicode decoding. Frozen source f8717d3
passes **25 live tests** and **428 vendor unit tests**. The lean environment
passes 420 tests with eight optional-vendor skips. Its fresh full Python report
verifies **100/1,013**, leaving **913 unverified**, strict exit 1; earlier Python
fingerprints are not reused. All GitHub SDK checks pass at f8717d3.
Sandbox.create.aio/exec.aio, PTY, secrets, timeout equivalence, descendants'
late-output EOF, stdin-offset idempotence and arbitrary Python-free images
remain unverified or unsupported.

A preceding real Modal startup-error comparison was RED. Native Modal returns
a process with exit 128 for both a missing executable and missing working
directory. Arker returns exit 127 for the executable and raises GuestProcessError
for the directory, with different diagnostics. Both probe sandboxes were cleaned.
The initial 25-case stream cohort did not certify these error paths. The latest
argv change fixes their process/exit-status behavior, not exact diagnostics.

The branch includes SDK main at 2ec474e. Frozen commit e2dc4cc runtime-scoped differential
reports verify **86/975 Python** entries on CPython 3.12.14/Linux/aarch64 and
**51/967 TypeScript** entries on Node 22.23.3/Linux/arm64. The latest Node profile
uses a clean install of published dependencies, with no local provider links.
All four native bridges are in the shims, built with ComputeSDK's provider
factory, and use the embedding SDK's vendor versions. The shared factory is a
declared dependency, not an undeclared transitive import.
Both inventories are complete in their declared scope.
The strict gates still exit 1: **889 Python and 916 TypeScript** entries remain
unverified. Neither method counts nor these scenarios establish full equivalence.

The unchanged files/process programs and new private HTTP programs passed all
twelve Python cases. Three additional E2B cases in each language preserve native
environment option names, success results and typed nonzero command errors,
including stdout, stderr, exit code and error text. They additionally cover
background PID identity, stdin/EOF, disconnect/reconnect, typed wait and kill
errors, and authoritative missing-PID behavior. The Arker E2B route succeeds
without an E2B key. Returned command handles are now included in the inventory;
the expanded denominator is intentional. Python context/iteration protocols are
also inventoried. Static and instance sandbox deletion, repeated deletion, and
Python context cleanup/error propagation pass the same unchanged programs.
Ownership probes use metadata, not E2B connect, so a losing race does not resume
a paused sandbox. The TypeScript native bridge registers the embedding vendor
instance with ComputeSDK, preserving class identity across package installations.
Node passed the three files/process routes and six HTTP
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

Local validation passes 417 Python unit tests and lint, and the full TypeScript
test/build/typecheck pipeline. Source/vendor snapshots now reject changes during
a scenario; runtime profiles are included in fingerprints. Existing evidence
grouping tests also reject collisions and malformed events. These measurement
tests do not grant vendor parity credit.

The pinned-vendor Python CI lane installs all four SDKs from the lockfile and
passes all 417 tests without skips. A separate lean environment passes 413 tests
and skips four optional-Daytona checks. Both were verified in clean temporary
virtualenvs; the initial CI failure from assuming an optional SDK was installed
was fixed, not suppressed in the vendor lane. All GitHub checks pass at d7af319.
The current 21-case Python live cohort also passes in that locked vendor
environment. Evidence now fingerprints installed transitive dependency versions
in addition to source, vendor roots and interpreter, preventing evidence from
different Python environments from being combined.

Modal's TypeScript adapter now uses the embedding SDK and returns its native
ContainerProcess class rather than waiting for completion and fabricating a
small result wrapper. All three from-ID routes pass literal argv (including
shell positional arguments), cwd/environment isolation, 256 KiB binary stdin
and output, EOF, exit status 17 and stream cancellation. The Arker route has no
Modal credentials and denies inbound/outbound traffic. Fixtures create the
sandboxes separately; this does not certify native image-builder creation on
Arker. PTY, secrets, entrypoint-wait and other streaming cases remain gaps.

A large-input regression reproduced guest shell argument overflow in Python
and TypeScript. Both clients now chunk stdin into bounded control frames and
serialize each complete write before EOF. The shared guest protocol provides
bounded binary output reads at byte offsets; tests cover all byte values and
reject invalid stream paths and bounds. Python-free images remain unsupported.

The packed-consumer gate now installs the SDK with each vendor separately, plus
standard Node type-check tooling, and also checks a base-only installation.
All five profiles import successfully in ESM/CommonJS without unrelated vendor
SDKs or ComputeSDK vendor adapters. Native controls and import-changed examples
pass strict typechecks in both modes, without skipLibCheck. This exposed missing
vendor declaration dependencies and a real CommonJS private-class mismatch.
The SDK declares required type dependencies and selects matching .d.ts/.d.cts
exports. Runtime-only import checks had not caught the mismatch. These consumer
calls are mocked; the separate paid cohorts provide cloud evidence.

Opaque native conflict/validation classes, gRPC mutation failures and HTTP
timeouts/5xx responses now fail closed instead of creating elsewhere. The
regressions failed before the guards. Such failures cannot establish that
creation did not happen; native error identity is preserved.

Python's async E2B entry point now uses the same router and ownership cache as
the sync entry point instead of returning the native class and bypassing routing.
Native async calls stay on their original event loop. Cancellation does not
trigger cross-provider fallback, losing ownership probes are cancelled, and a
late successful Arker creation is deleted after caller cancellation. These
cancellation paths have unit coverage, not complete live interleaving coverage.
The refreshed cohort passes all 21 Python and 18 Node programs. Async E2B adds
typed native handles, awaited callbacks, stdin/EOF, reconnect, context cleanup,
and static/instance deletion on all three routes. The inventory now includes
async command handles. This does not verify user identity or all creation defaults.

The TypeScript test gate also packs the SDK and installs it in an isolated npm
consumer with only E2B, Daytona and Blaxel vendor dependencies. ESM and CommonJS imports,
native error identity, and creation inputs pass mocked checks without separate
ComputeSDK vendor packages. ComputeSDK is a declared runtime dependency. This
packaging check is credential-free and does not count as live cloud evidence.

A fresh Node HTTP rerun exposed a reset/abort race after an earlier passing
cohort: the body could expose ECONNRESET instead of AbortError. A deterministic
regression fails without preserving the caller's abort reason and passes with
the fix on Node and Bun. All nine Node live programs passed again afterward.
This is bounded evidence, not proof that every cancellation interleaving works.

The separate framed relay helper in arker-app #4260 now passes real Python and
Node half-close probes against a retained protocol-2.1 guest. Its boot ID, old
agent build and PID 110 stayed unchanged, and helper permissions are 0700.
The refreshed 58-case ingress/private-port selection passed with zero skips.
Bun 1.4.3 and Node preserve chunked WebSocket-upgrade refusals, including split
Unicode error bytes. Bun's shim supplies raw chunk frames; bounded transfer
decoding preserves the original typed API error. Bun 1.3.8 lacks the required
upgrade-response behavior; development and CI now pin 1.4.3. Runtime profiles
remain separate and are not certified by Node's live report.

A clean published-dependency run first failed version attestation: published
ComputeSDK's E2B provider embeds 2.35.0, while the reference uses 2.56.0. The
new in-shim bridge uses the embedding SDK directly and still participates in
ComputeSDK routing. Attestation checks the dependency actually used by that
bridge; the mismatch was not waived. The refreshed 12-program Node cohort
passed with all four provider packages restored to published installs.

A subsequent Daytona image regression failed because the TypeScript shim
discarded native Image builders. The shim now forwards the original object to
the native adapter. Arker rejects builders before making a VM request, rather
than silently booting a configured fallback image. The shared Arker adapter
also rejects non-string or empty image values. Regression and full local tests
pass. The refreshed Node profile includes the in-shim Daytona adapter, native
endpoint/target configuration, unchanged native execution responses, and
separate ComputeSDK command normalization with preserved stdout/stderr and status.

Python AsyncDaytona also had a native-class re-export that bypassed routing.
It now shares the sync router/cache and the common async bridge with E2B.
Both async wrappers cancel losing probes and native requests without falling
through to another provider; if native creation suppresses cancellation and
returns a sandbox, the bridge deletes that late result. The new cancellation
regression failed before this fix. The current three-route Daytona programs
verify cwd/environment isolation, reconnect, nonzero exit, response artifacts,
and cleanup. Python returns native typed execution results. Python uses
process.exec; TypeScript uses process.executeCommand. Python's shell artifacts
include charts=[], while TypeScript omits charts; the live harness checks each
actual vendor shape rather than assuming they match across languages.

A real E2B identity probe is RED: the default native sandbox runs as uid/gid
1000 (user), with HOME=/home/user, USER=user and LOGNAME=user; Arker runs the
command as root with root environment variables. Both report /home/user as cwd.
Matching directories does not establish matching permissions or process
identity. The probe deleted both disposable sandboxes. This is a deployment
blocker, not a cosmetic output difference.

The expanded four-provider identity probe is RED for every default source.
Native Daytona runs as uid/gid 1001 (daytona), with HOME=/home/daytona;
Arker runs as root with HOME=/root despite the matching working directory.
Native Blaxel runs as root but sets HOME=/blaxel, unlike Arker's HOME=/root.
Native Modal prints empty USER/LOGNAME, while Arker sets both to root.
Unset versus empty variables still needs a separate check. All disposable
sandboxes were cleaned up. These results compare the native defaults with
Arker's configured ubuntu source, not every possible custom image.

A named Blaxel regression then failed on the published-provider route: it
discarded the requested name and returned a UUID. The in-shim adapter preserves
name, environment, network, strict/get-or-create, safety and creation timeout.
The strengthened named program now passes all three Node routes. Its Arker
metadata also preserves the configured sandbox name rather than the VM ID.
Credential epochs preserve global HTTP transport settings; the live harness
now asserts that disabled-H2 cases remain disabled before and after operations.

The separate strict-create lifecycle regression is still RED on Arker: it
creates a second same-name VM instead of returning a conflict. Both native
routes preserve Blaxel's SANDBOX_ALREADY_EXISTS code. ComputeSDK's redundant
outer creation fallback had wrapped that structured error; disabling it leaves
fallback decisions in the shim router. Both routers now refuse fallback after
409/structured conflicts, with regressions that failed before the fix. Own
duplicate test resources were deleted. A local mutex cannot provide the missing
distributed name guarantee, and the failing live test has not been weakened.

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
