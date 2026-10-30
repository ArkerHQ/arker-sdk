# Daytona lifecycle

The Python sync/async and TypeScript adapters forward native lifecycle calls
without replacing native results or exceptions. On Arker, `pause` confirms a
full memory-preserving pause, `start` explicitly resumes the persisted mode,
and `fork` creates an independent, non-durable disk-only child and confirms its
startup. These calls do not rewrite the source's policies or resume the source.
A named fork requires the global API's dedicated `/v1/named-fork` endpoint;
a custom regional URL also requires an explicit control URL. Errors never
fall back to ordinary creation.

Timeouts bound the client wait, not the server operation. Zero disables the
client timeout. A failed child startup preserves its known ID as `sandbox_id`
in Python or `sandboxId` in TypeScript on the exception. A lost creation
response may leave an unknown outcome; neither retry nor cancellation deletes
an unconfirmed resource. A caller-supplied name supports later lookup.

Arker `stop` and `archive` remain unsupported and fail before mutation. Native
`stop` promises SIGTERM shutdown (SIGKILL with `force`), and `archive` requires
a stopped sandbox before filesystem offload. Filesystem pause does not supply
those guarantees. Native routing retains both operations. The adapter does not
synthesize native lifecycle state fields from ambiguous idle VM state.

Lifecycle transport and forwarding regressions use the pinned Daytona 0.227.0
SDKs. Live Arker lifecycle equivalence has not yet been qualified.
