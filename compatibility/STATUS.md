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
Signature credit additionally requires explicit signature attestations from the
scenario. Both live recorders accept them; existing API-only events do not gain
retroactive signature credit. Method presence and unit tests do not
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
  mechanisms. Nine live scenarios across Python sync/async and TypeScript passed
  on reference, shim-native and shim-Arker routes for unattended work, reconnect,
  full-memory/filesystem pause, fork and expiry. The daemon joins the tracked run
  through its authenticated filesystem connection. Separate comparisons against
  E2B 2.58 and 2.62 passed all nine scenarios each, including additional monitor
  termination checks. Host-loss recovery remains unqualified.
- E2B keyed creation uses stable routing with unchanged configuration and credentials;
  keyed retries never fall back to another provider. Outbound deny-all policies
  support explicit host/IP exceptions and DNS address restrictions. Live Arker
  qualification of these options remains pending. See [creation options](e2b-creation.md).
- E2B sandbox expiry, request timeouts, cancellation, streamed reads, process
  enumeration and native error identities have unverified or unsupported cases.
- Daytona PTY/session comparisons against 0.227 pass nine Python sync/async and
  TypeScript cases across all three routes, covering persistent shell state,
  streams, resizing, tagged log lines and termination signals. Bounded file
  streams are implemented; downloads stage the guest file before iteration and
  unknown-length uploads spool locally. Filename glob and literal content search
  are implemented. Python file transfers honor native connect/pool and read/write
  phase timeouts, including zero for an unbounded transfer phase; cleanup uses
  independent request limits. Custom users, image working-directory selection,
  metadata deadlines and TypeScript transfer timeout combinations remain incomplete.
  The pinned native filesystem exposes neither watch nor signed file URLs. See the
  [filesystem contract](daytona-filesystem.md) for source prerequisites and
  [volume contract](daytona-volumes.md) for mount and deletion limits.
- Modal retained-terminal lookup, polling, waiting and termination are implemented.
  The latest terminal comparison passed 15 of 18 cases; three native-backed
  requested-termination cases failed. All 12 Arker cases passed, but this does
  not establish complete differential equivalence.
- Modal native Image/Secret/Volume handles are forwarded on native routes but
  have local registry/secret recipe support for Arker creation; opaque handles
  and Volume recipes remain incomplete. Exec PTY passed nine live cases across
  Python sync/async and TypeScript on all three routes. Creation-time PTY,
  remote secrets and broader async/lifecycle behavior remain incomplete.
- Blaxel Python 0.4.21 and TypeScript 0.3.32 watch/stream comparisons pass 18
  cases on the corrected cancellation implementation. Recursive directory reads
  and named creation also have bounded comparisons. Qualification uses a private
  prepared derivative; public bootstrap and archive remain incomplete. Writable
  drive mount/list/unmount bindings are implemented in Python and TypeScript,
  including authoritative readback after a lost mount response. TypeScript also
  supports writable mountDrives setup. Read-only mounts, UID/GID mapping and
  full DriveInstance routing remain unsupported. Native drive comparisons are
  authorization-blocked; live sharing/persistence is not yet qualified. Python
  cancellation stops subsequent drive operations but cannot interrupt an already
  in-flight synchronous HTTP request.
  Python Docker registry config supports explicit registry basic-auth entries for
  image forks. Credential helpers and token-based registry entries remain unsupported;
  private registry pulls still need live qualification. Registry config is not a Dockerfile.
- Guest process helpers require Python; arbitrary Python-free images are not
  drop-in compatible. Preparing a source is an explicit prerequisite, not proof
  of default-image equivalence.
- Drives/mounts, registry/image builders and policy-secret behavior need additional
  unchanged-program comparisons. Host policy secrets and guest environment
  secrets are different capabilities and must not be silently substituted.
- Resolve policy/API compatibility failures and account-restricted comparisons;
  neither a skipped scenario nor a baseline failure counts as equivalence.
- Publish the required ComputeSDK provider fixes before releasing dependent shims.
