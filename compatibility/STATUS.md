# Compatibility status

These shims are experimental. Full core sandbox parity is not established, and
strict coverage gates remain failing. Apps and Functions are excluded; images,
secrets, filesystems, drives, mounts, policies and sandbox lifecycle remain in scope.

## Validation

Run the unit, packed-consumer and differential suites described in
[README.md](README.md). Save reports and evidence outside the checkout. Historical
runs do not certify the current source or dependencies.

Live API credit requires one unchanged scenario to pass on the reference vendor,
shim-native and shim-Arker routes with matching runtime/vendor fingerprints.
Signature credit additionally requires explicit signature attestations; the live
recorders currently emit API IDs only. Method presence and unit tests do not
establish full compatibility. Both languages expose strict coverage gates, which
must pass with fresh evidence before claiming parity.

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
