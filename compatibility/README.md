# Compatibility coverage

The migration target is unchanged vendor programs with only their import replaced.
The tools inventory installed SDKs, rather than a list of implemented shim methods.
Python versions are pinned in `requirements.txt`; TypeScript reference SDKs are
pinned in `typescript/package.json`.

The migration target is the core sandbox API. Apps and Functions remain native;
this exclusion does not exclude sandbox image inputs, secrets, drives, mounts,
processes, policies, or lifecycle behavior. These still have unverified gaps.

For Arker routes, set `ARKER_API_KEY`, `ARKER_PROVIDER`, `ARKER_REGION`, and
`ARKER_SOURCE_VM_ID` to select your prepared parent. An explicit `ARKER_BASE_URL`
replaces provider/region configuration. Named Blaxel creation additionally
requires `ARKER_CONTROL_BASE_URL` when using a custom endpoint; it must identify
the public global gateway that supports `/v1/named-fork`. Unsupported gateways
reject this dedicated endpoint before creation; no ordinary-fork fallback is
attempted. Keep native provider credentials for native routes.

E2B signed file URLs are unavailable on BYOC and self-hosted pool workers.
When URL preparation returns the specific unsupported-capability error, sandbox
creation may continue if bootstrap and native-client initialization succeed;
URL methods then raise that original error locally. Other preparation failures
still fail creation. Native E2B data-plane behavior on these pools has not been
verified live.

## Image parents and host policy secrets

The shared Arker adapter accepts `source_vm_id` (Python) or `sourceVmId`
(TypeScript), so an image-backed parent can be imported once and forked many
times without another registry import. Explicit per-call images take precedence
over the configured parent. Python's explicit `template_id` takes precedence
over a configured parent ID. Otherwise a parent ID takes precedence over a
source name.
Python retains its legacy named-source `ARKER_SOURCE_VM` interpretation; use
the explicit `source_vm_id` setting for a parent ID. Both languages also accept
`ARKER_SOURCE_VM_ID` as an unambiguous parent ID; an explicit configured parent
ID takes precedence over that environment variable. The older `ARKER_SOURCE_VM`
remains a name in Python and an ID in TypeScript for compatibility; new shared
configuration should use `ARKER_SOURCE_VM_ID`, not the ambiguous legacy variable.

Both adapters accept an Arker `policies` configuration passed to the fork API.
Named policy secrets are host-side values, not guest environment variables.
Provider network constraints add independent outbound guards that intersect
the existing policy. They preserve configured inbound/lifecycle rules and
secret rewrites; even an inherited allow cannot bypass a block-all guard.
An explicitly configured deprecated ordered policy list still replaces the
parent policy, while the new guard constrains that replacement. This requires
the matching backend guard API and is not yet live-certified or published.
Guard readback is additive: `policies.outbound_guards` contains the independent
floors, while the legacy `policies.rules` response union is unchanged. The
shared SDK preserves these floors when a policy read back is passed to update,
fork, run or the deprecated policy writer, translating them back to tagged
input rules. Explicit rule updates and null removals take precedence.
Registry credentials are configured as `registry_auth` in Python or
`registryAuth` in TypeScript, with `username` and `password`. They are sent only
for an image import, never for a child fork from an existing VM. Keep these
values in application configuration/environment, not the routing JSON or cache.
Native vendor Secret handles and native registry/image builder handles are not
translated by this configuration mechanism.

Live checks (not vendor-equivalence evidence):

```sh
cd python
uv run --no-sync pytest tests/e2e/test_compat_image_forks.py -q
cd ../typescript
npx -y tsx@4.20.6 tests/compat-image-forks-live.ts
```

In mixed routes containing Arker, E2B and Modal are not queried for identifiers
matching Arker's exact VM ID grammar: those vendors reject that syntax instead
of reporting absence. This also invalidates incompatible cache hints. Arker must
still authorize the lookup; recognizing syntax never establishes ownership.
Blaxel and Daytona remain eligible because their sandbox names may match that
grammar. Native-only configurations are unchanged. Both languages exercise the
same identifier fixtures in `router-identifiers.json`.

## Measurement

Ownership lookup uses one shared `existing.timeoutMs` client-wait budget
(default30000ms), including a cached provider probe and subsequent priority or
parallel probes. Local cache access consumes elapsed budget but depends on local
filesystem responsiveness: a hung cache filesystem is not covered by a hard
end-to-end deadline. JSON accepts a positive safe integer; TOML is not supported.
A timeout is a typed
`RouterLookupTimeoutError`, never absence and never permission to create on a
different provider. It does not cancel native I/O or change a sandbox lifetime;
an already-issued read may finish later, but cannot populate the routing cache.

`existing.strategy="race"` starts metadata lookups in parallel but waits for
their outcomes within that budget. Any non404 error fails the lookup even when
another provider has a hit; errors and duplicate hits follow configured provider
priority, not completion timing. This trades first-hit latency for fail-closed,
deterministic routing. A validated cached owner remains the fast path. Routing
lookups do not use weighted creation, and the losing E2B probe never connects or
extends a sandbox lease. Only the selected E2B owner is connected afterward.

Creation fallback is fail-closed. Only an explicit local preflight refusal
before a creation request (such as a missing Arker source) permits trying the
next provider. Remote errors, including opaque exceptions and HTTP 429, can
come from polling an already-created sandbox and do not permit fallback.
Priority and weighted initial selection still apply; generic remote-error
failover is not a supported guarantee.

Reports record native signatures, API names, providers, language/styles, matching
shim declarations, and differential test evidence. The inventory includes
sandbox/client roots, creation-related image/template/volume/secret roots, and
statically discoverable service objects to depth two. Returned handles, dynamic
attributes without discoverable types, and option combinations require more
scenarios. This bounded inventory does not enumerate every vendor behavior.

`scenario_verified` means runtime tracing observed a call and the same scenario
passed with the original SDK, the shim on the vendor, and the shim on Arker.
Evidence must match the SDK version, implementation fingerprint, scenario, and
test-run ID. The fingerprint includes implementation, dependencies, scenarios,
and measurement code; changing the test also requires new evidence. A failed
route invalidates that scenario. Different runs cannot be
combined to manufacture a passing comparison.

TypeScript fingerprints also include the installed ComputeSDK/provider runtime
artifacts. Native-route evidence refuses a different SDK version inside its
native adapter than the direct native reference. E2B and Blaxel adapters now live
in the shims and resolve the embedding application's vendor SDK, as do Modal
and Daytona. Tests of locally
linked, unreleased provider builds do not certify registry releases.

Fingerprints include the host runtime and architecture. A Node report does not
certify Bun, or another Node version. Live recorders snapshot source and vendor
versions before execution and invalidate evidence if either changes mid-run.
The HTTP origin shared by Python and TypeScript is fingerprinted too.

`unverified` includes missing methods, partial implementations, vendor-only
delegation, and methods without recorded differential scenarios. An exposed
method or explicit unsupported error does not grant behavioral coverage. One
passing scenario does not establish equivalence for every option, overload,
error, cancellation path, or timing behavior. Method counts alone never set the
report's `full_api_equivalence` field to true.

## Reproduce

Configure provider/Arker credentials, plus `ARKER_SOURCE_VM_ID`, `ARKER_PROVIDER`, and
`ARKER_REGION`. Credentials are never included in evidence.

```sh
export COMPAT_COVERAGE_RUN="$(date -u +%Y%m%dT%H%M%SZ)"
cd python
uv pip install -r ../compatibility/requirements.txt
COMPAT_COVERAGE_EVIDENCE=/tmp/python-compat.jsonl \
  uv run --no-sync pytest tests/e2e/test_e2b_command_import_only.py
COMPAT_COVERAGE_EVIDENCE=/tmp/python-compat.jsonl \
uv run --no-sync pytest tests/e2e/test_blaxel_import_only.py
COMPAT_COVERAGE_EVIDENCE=/tmp/python-compat.jsonl \
  uv run --no-sync pytest tests/e2e/test_blaxel_fetch_import_only.py
uv run --no-sync python scripts/compat_coverage.py \
  --evidence /tmp/python-compat.jsonl --output /tmp/python-coverage.json
cd ../typescript
bun install
COMPAT_COVERAGE_EVIDENCE=/tmp/typescript-compat.jsonl \
  bun tests/compat-e2b-command-live.ts
COMPAT_COVERAGE_EVIDENCE=/tmp/typescript-compat.jsonl \
bun tests/compat-blaxel-import-live.ts
COMPAT_COVERAGE_EVIDENCE=/tmp/typescript-compat.jsonl \
  bun tests/compat-blaxel-fetch-live.ts
bun scripts/compat-coverage.ts \
  --evidence /tmp/typescript-compat.jsonl --output /tmp/typescript-coverage.json
```

Both reporters accept `--fail-on-unverified` as a strict scenario-coverage gate.
It currently fails: full coverage has not been achieved. Existing tests without
instrumentation do not receive retrospective coverage.

Use `--scope core-sandbox --fail-on-unverified` for the migration target above.
The default `--scope expanded` retains the broader inventory. Both languages
read the same `api-scope.json` exclusions, report `selected_scope`, and list
each App/Function surface under `scope_exclusions` with its reason. A scoped
report retains images, secrets, volumes, drives, mounts, sandbox lifecycle,
and sandbox-related low-level client methods. Unknown APIs remain in scope.
Exclusions grant no behavioral coverage. The scope contract is fingerprinted;
changing it invalidates existing evidence and requires fresh live scenarios.

### Authenticated registry checks

Configure an isolated private image with `COMPAT_PRIVATE_REGISTRY_IMAGE`,
`COMPAT_REGISTRY_USERNAME` and `COMPAT_REGISTRY_PASSWORD`, plus the Arker API
configuration above. Keep these values in a protected environment, not source
or command arguments. Run:

```sh
cd python
uv run --no-sync pytest tests/e2e/test_compat_private_registry.py
cd ../typescript
npx -y tsx@4.20.6 tests/compat-private-registry-live.ts
```

These Arker-only checks import once per language, require cached-image requests
with incorrect credentials to fail, then fork three isolated children. They
inspect the shared fork arguments to ensure credentials appear only on image
imports, and check that passwords do not appear in child environments. Each
test deletes its own VMs; it does not modify or delete the supplied registry.
They are supplementary checks, not native-provider API equivalence evidence.

For the Node integration profile, run the TypeScript programs and reporter with
`npx -y tsx@4.20.6` instead of Bun. Mixing runtimes correctly invalidates evidence.
Set `BL_REGION` to an available Blaxel region (for example `us-pdx-1`) for the
Node private-fetch scenario. Enabling H2 alone does not select it: the native
SDK uses regular Fetch when no region is supplied. The scenario verifies the
actual native H2 domain before testing each transport profile; a fallback must
not be reported as H2 coverage.
Python private HTTP requires the native Blaxel SDK's HTTP dependencies, or
`pip install 'arker[blaxel]'` for Arker-only HTTP use.
Python E2B command handles require `arker[e2b]` and Python 3.11 or later, matching
the current native SDK's Python requirement. They use the pinned native handle
and typed result classes. TypeScript's adapter is checked against every public
member of the vendor handle type; its constructor is not a public runtime export.

The guest process protocol is authored in `python/src/arker/_guest_process.py`;
the TypeScript build generates an embedded copy. State stays on the VM, allowing
reconnect without replaying a command. TypeScript installs it through ComputeSDK;
both languages use Arker's session API for isolated, serialized control requests
and managed background supervisors. Application sessions are not reused for
control traffic. It supports named/background processes,
status/list, wait, logs, stdin/EOF, stop, kill, and execution timeout.

The current guest protocol requires Python 3. Image-independent operation remains
a release requirement before claiming arbitrary-image compatibility.

## Blaxel image creation environment

Creation `envs` are applied before boot when creating from an explicit OCI image.
Python uses native name/value entries; TypeScript uses its native environment
object. The Arker configuration also accepts `image_config` (Python) or
`imageConfig` (TypeScript) with `env`, `entrypoint` and `cmd`. Omitted command
arrays inherit the image; empty arrays clear them. This requires a server with
the image-configuration API. Captured VM sources reject these overrides.

A source whose entrypoint launches a public daemon must be prepared with explicit
`entrypoint: []` and `cmd: []` before private bootstrap. The shim does not silently
replace a customer's startup command. Custom-image user identity, native default
image resolution and unattended daemon lifecycle are not fully qualified.

## Modal creation recipes

Arker supports plain registry-image recipes, local secret values, creation
`env`/`workdir`, and sandbox grouping by app name with `create_if_missing=True`
(Python) or `createIfMissing: true` (TypeScript). Creation defaults are stored in
a private guest directory and restored on reconnect. Explicit exec environment
values override exec secrets, which override creation defaults.

Python uses scoped recipe factories when the configured creation route includes
Arker. Configure `COMPUTESDK_ROUTER_CONFIG` (or `computesdk_router.json`) before
accessing `App`, `Image`, and `Secret`; credentials may then be supplied at
`Sandbox.create`. Native-only routes retain the exact native classes, even when
an Arker credential is present. With no explicit routing file, the normal
credential-based routing defaults still apply.

Recipe image and secret constructors preserve real native handles when Modal is
installed and record only their public constructor inputs. App lookup is
resolved when the native route or a native app operation is selected. Native
opaque handles created before configuring an Arker-capable route cannot be
mapped retroactively; per-call-only routing with such handles is unsupported.
TypeScript records local image/secret recipes without inspecting native builder
closures.

An Arker app group is VM metadata, not a deployed Modal App: it has no fabricated
native ID, and lookup without `create_if_missing=True` / `createIfMissing: true`
is unsupported.
Named Volume recipes are not mapped to Arker storage. Python `debian_slim`, image
build steps and unknown native image handles are also
unsupported on Arker; those requests fail before creating a VM. Native routes
continue to use native resources. The creation differential scenarios use the
same workload arguments on all routes; native-only success does not certify the
Arker route.

Static private-registry credentials may be supplied through a local
`Secret.from_dict` / `secrets.fromObject` recipe passed to the registry image
constructor. Both `REGISTRY_USERNAME` and `REGISTRY_PASSWORD` must be nonempty.
Those two values are sent only in the image-pull request, not as sandbox
environment variables. Named/remote secrets and cloud-registry credential
exchange are unsupported. A per-image secret overrides configured registry
credentials for that creation without mutating the client's defaults.
