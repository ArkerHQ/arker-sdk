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

- Blaxel named creation uses the public global `/v1/named-fork` endpoint for
  distributed uniqueness. Custom regional URLs require an explicit control URL
  with naming support. Unsupported gateways reject this dedicated route before
  creation; the shims never fall back to an ordinary fork.
  Strict duplicates and get-or-create still need fresh differential live validation;
  successful unit tests do not establish cross-region or response-loss parity.
  Strict named creation does not replay a failed transport request: the outcome
  may be unknown, and a matching name does not prove ownership of that creation.
- Blaxel control-plane refusals use native exception classes/body shapes while
  retaining Arker's authoritative status and reason. Backend-specific error text
  and authentication documentation are not copied from Blaxel. Network outcomes
  and timeouts remain explicit; they do not imply native creation cancellation.
- Blaxel private HTTP fetch, sessions, redirect/cookie/timeout behavior and
  network configuration need complete differential validation. Empty network
  settings and the native no-op `default` firewall ruleset preserve existing
  policies. Proxy filters, dedicated egress and subnet controls remain unsupported;
  they are not approximated with broader outbound rules. Async strict-create
  cancellation stops waiting without deleting an accepted or uncertain remote
  creation. Local clients are closed even when the originating event loop shuts
  down; cancellation also prevents a later busy-response retry. Fresh live
  comparisons of this implementation remain required.
- E2B on Arker retains a dedicated run for the verified native daemon lifetime.
  This intentionally keeps even an idle sandbox active and incurring compute
  charges; client disconnect does not release it. Reconnect reuses the same
  VM-bound run. Explicit pause and lifetime deletion remain the stopping
  mechanisms, and their interaction with this hold still requires live
  qualification across full-memory and filesystem resume. Local pidfd tests
  do not establish those guest lifecycle guarantees.
- E2B sandbox expiry, request timeouts, cancellation, streamed reads, process
  enumeration and native error identities have unverified or unsupported cases.
- Daytona custom users, image working-directory/shell selection, streaming,
  search/watch and timeout/error combinations remain incomplete. See the
  [filesystem contract](daytona-filesystem.md) for source prerequisites and
  [volume contract](daytona-volumes.md) for mount and deletion limits.
- Modal retained-terminal lookup, polling, waiting and termination are implemented
  but still lack passing live differential qualification.
- Modal native Image/Secret/Volume handles are forwarded on native routes but
  have local registry/secret recipe support for Arker creation; opaque handles
  and Volume recipes remain incomplete. Remote secrets, PTYs and broader
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
