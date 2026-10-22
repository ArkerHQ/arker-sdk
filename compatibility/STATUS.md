# Compatibility status

These shims are experimental. Full core sandbox parity is not established, and
strict coverage gates remain failing. Apps and Functions are excluded; images,
secrets, filesystems, drives, mounts, policies and sandbox lifecycle remain in scope.

## Measurement

| Scope | Python APIs / vendor signatures | TypeScript APIs / vendor signatures |
| --- | ---: | ---: |
| Core sandbox | 1123 / 936 | 982 / 786 |
| Expanded | 1146 / 951 | 1052 / 850 |

Inventory, declarations, observed API calls and verified signatures are distinct.
Live API credit requires one unchanged scenario to pass on the reference vendor,
shim-native and shim-Arker routes, with matching runtime/vendor fingerprints.
Failed or stale evidence cannot earn credit. Signature credit additionally needs
explicit signature attestations; current live recorders emit API IDs only.
Neither method presence nor a passing unit test establishes full compatibility.

The inventory scripts expose strict gates for both scopes and languages. Run them
with fresh evidence using the commands in [README.md](README.md). Historical
cohorts are not current-source validation after runtime or scenario changes.

## Current validation

- Python combined unit suite: 650 passed after integrating current main.
  Live-derived E2B and Blaxel corrections also passed 75 focused regressions.
- TypeScript combined tests, build and packed ESM/CommonJS consumers passed for
  the base SDK and all four optional vendor packages. Dedicated filesystem and
  sandbox regression files are included in the standard handle-test command.
- Router tests cover reserved JSON keys, malformed cache entries, byte limits,
  priority/race lookup, invalidation and cross-language cache interoperability.
- Native live probes established that E2B `depth=None` uses one directory level,
  and Blaxel async grep returns relative paths and preserves context newlines.
  The pinned Blaxel Python sync filesystem has no grep API.
- At `6e4480e`, nine of eleven live suites pass using prepared source parents.
  E2B and Daytona filesystem and Modal process/filesystem comparisons pass in
  both languages. Python Blaxel grep/process/filesystem and TypeScript native
  Blaxel tests pass. Strict named creation and private fetch still fail on Arker.
- Current evidence contains 30 passing Python events and 18 passing/3 failing
  TypeScript events. The separate strict-name test also fails. Both evidence
  files are in `evidence/core-filesystem-20261022-{python,typescript}.jsonl`.
- Core scenario coverage is **105/1123 Python** and **41/982 TypeScript**; expanded
  coverage is 105/1146 and 41/1052. No events are rejected as stale. All four
  strict gates exit 1. Signature-tested/live counts remain zero because the
  recorders do not attest signatures. These counts are not full API equivalence.
- Prepared source cleanup was independently confirmed by a not-found response.
  Source preparation is not evidence of default-image or arbitrary-image parity.

## Remaining release requirements

- Blaxel strict duplicate-name creation and atomic get-or-create are not
  equivalent on Arker. A local lock cannot enforce distributed uniqueness.
- Blaxel private HTTP fetch, sessions, redirect/cookie/timeout behavior and
  network configuration need complete differential validation.
- E2B sandbox expiry, request timeouts, cancellation, streamed reads, process
  enumeration and native error identities have unverified or unsupported cases.
- Daytona custom users, image working-directory/shell selection, streaming,
  search/watch and timeout/error combinations remain incomplete. See the
  [filesystem contract](daytona-filesystem.md) for source prerequisites.
- Modal native Image/Secret/Volume handles are forwarded on native routes but
  are not fully translated for Arker creation. Remote secrets, PTYs and broader
  async/lifecycle behavior remain incomplete.
- Guest process helpers require Python; arbitrary Python-free images are not
  drop-in compatible. Preparing a source is an explicit prerequisite, not proof
  of default-image equivalence.
- Drives/mounts, registry/image builders and policy-secret behavior need additional
  unchanged-program comparisons. Host policy secrets and guest environment
  secrets are different capabilities and must not be silently substituted.
- Resolve policy/API compatibility failures and account-restricted comparisons;
  neither a skipped scenario nor a baseline failure counts as equivalence.
- Publish the required ComputeSDK provider fixes before releasing dependent shims.
