# Compatibility coverage

The migration target is unchanged vendor programs with only their import replaced.
The tools inventory installed SDKs, rather than a list of implemented shim methods.
Python versions are pinned in `requirements.txt`; TypeScript reference SDKs are
pinned in `typescript/package.json`.

In mixed routes containing Arker, E2B and Modal are not queried for identifiers
matching Arker's exact VM ID grammar: those vendors reject that syntax instead
of reporting absence. This also invalidates incompatible cache hints. Arker must
still authorize the lookup; recognizing syntax never establishes ownership.
Blaxel and Daytona remain eligible because their sandbox names may match that
grammar. Native-only configurations are unchanged. Both languages exercise the
same identifier fixtures in `router-identifiers.json`.

## Measurement

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
ComputeSDK provider than the direct native reference. Tests of locally linked,
unreleased provider builds therefore do not certify the registry releases.

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

Configure provider/Arker credentials, plus `ARKER_SOURCE`, `ARKER_PROVIDER`, and
`ARKER_REGION`. Credentials are never included in evidence.

```sh
export COMPAT_COVERAGE_RUN="$(date -u +%Y%m%dT%H%M%SZ)"
cd python
uv pip install -r ../compatibility/requirements.txt
COMPAT_COVERAGE_EVIDENCE=/tmp/python-compat.jsonl \
  uv run pytest tests/e2e/test_e2b_command_import_only.py
COMPAT_COVERAGE_EVIDENCE=/tmp/python-compat.jsonl \
uv run pytest tests/e2e/test_blaxel_import_only.py
COMPAT_COVERAGE_EVIDENCE=/tmp/python-compat.jsonl \
  uv run pytest tests/e2e/test_blaxel_fetch_import_only.py
uv run python scripts/compat_coverage.py \
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

For the Node integration profile, run the TypeScript programs and reporter with
`npx -y tsx@4.20.6` instead of Bun. Mixing runtimes correctly invalidates evidence.
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
