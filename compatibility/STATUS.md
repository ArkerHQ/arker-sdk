# Recorded coverage baseline

## Current validation

The private Unix-socket SDK checkpoint adds Python `VM.connect_unix` and Node
`VM.connectUnix`, sharing the existing owner-authenticated binary/EOF stream
transport. Its new unit cases check exact control-character path encoding,
UTF-8 byte limits, authentication, framing and rejection before network access.
Python's vendor unit profile passes **456 tests**; the full TypeScript
typecheck/test/build/isolated-consumer gate and OpenAPI artifact check pass.
Packed SHA-256:
`672abdc49389ae3b2a921712c8fc507fe4f9b4a2574979a6f6e972188f5f00c5`.
Backend Linux compilation and live transport validation are still in progress.
This does not install a native daemon or establish additional provider parity.
The source change invalidates prior differential fingerprints; the cohorts
below certify their recorded checkpoints, not this new SDK source.

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
