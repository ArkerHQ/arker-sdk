# Arker Go SDK

Go client for the Arker VM API. The surface mirrors the [Python](../python) and
[TypeScript](../typescript) SDKs: a `Client` for org-wide calls, and a `VM`
handle for everything scoped to one VM.

```bash
go get github.com/ArkerHQ/arker-sdk/go
```

There is no registry upload: `proxy.golang.org` fetches straight from the repo,
and a `go/vX.Y.Z` git tag is what makes a version exist. That prefix is required:
Go resolves a module in a subdirectory only from `<subdir>/vX.Y.Z`. Releasing is
deliberately manual: bump `go/VERSION`, then run the `Publish Go` workflow and
type that version to confirm. A Go tag is immutable once the proxy fetches it.

```go
client, err := arker.New(arker.Options{})       // ARKER_API_KEY, ARKER_PROVIDER, ARKER_REGION
vm, err := client.Fork(ctx, arker.ForkRequest{SourceVMName: "ubuntu-base"})
defer vm.Delete(ctx)

out, err := vm.Run(ctx, arker.RunRequest{Command: "echo hello"})
fmt.Print(out.Stdout)
```

## Configuration

`New` reads `ARKER_API_KEY`, `ARKER_BASE_URL`, `ARKER_CONTROL_BASE_URL`,
`ARKER_PROVIDER` and `ARKER_REGION`, and explicit `Options` win over all of them.

Placement works exactly as in the other SDKs. `BaseURL` fully determines
routing, so `Provider` and `Region` are ignored when it is set; supply either
`BaseURL` or *both* `Provider` and `Region`. Org-wide calls — `ListVMs`,
`ListRuns`, `ListRegions`, `Whoami` and `ListPools` — reach the control plane
and work with no placement at all; everything else needs a regional endpoint.

## Surface

| Client | VM |
| --- | --- |
| `VM` `Fork` `GetVM` `ListVMs` | `Refresh` `Fork` `Update` `Delete` |
| `ListRuns` `ListRegions` `Whoami` | `Run` `ListRuns` `GetRun` `CancelRun` |
| `ListFilesystems` `CreateFilesystem` | `ListSessions` `CreateSession` `GetSession` |
| `GetFilesystem` `DeleteFilesystem` | `UpdateSession` `DeleteSession` |
| `ListPools` `GetPool` `ListPoolWorkers` | |
| `CreatePool` `UpdatePool` `RenamePool` `DeletePool` `AddPoolCommitment` | |
| `CreateAPIKey` `ListAPIKeys` `RevokeAPIKey` | |
| `BaseURL` `ControlBaseURL` `Provider` `Region` | `GetPolicies` `SetPolicies` |
| package-level `DiscoverRegions` | `ReadFile` `WriteFile` `SyncDir` |
| | `ListMounts` `CreateMount` `DeleteMount` `ConnectPTY` |

Two deliberate differences from Python and TypeScript, both because Go has no
optional arguments and no union return:

- **`ReadFile` / `WriteFile`** rather than one `sync(path, data?)`. Splitting
  them also removes the nil-versus-empty ambiguity when writing a zero-length
  file.
- **`Run` returns one `*RunResult`** with a `Type` of `"completed"` or
  `"background"` (`out.Completed()`), rather than a union of two result types.

Not implemented: **Dockerfile forks**. The API refuses `dockerfile` by design —
Python and TypeScript parse it client-side and replay the steps. Go returns a
typed error rather than sending a request that always 400s.

`VM.Update` changes a policy with a `PolicyUpdate`; `Info.Policies.Update()`
turns a policy read back into one, leaving out masked (`***`) secret values,
which the server keeps.

## Idempotency

A `Fork` sends an `Idempotency-Key` only when you ask: set
`ForkRequest.Idempotency` for a key generated per call, presented on every SDK
retry, or `ForkRequest.IdempotencyKey` for one you choose. If you catch an
`UnknownOutcomeError` and retry yourself, use an `IdempotencyKey` stable across
processes, or the second call forks a second VM.

```go
key := "provision-" + installationID + "-" + machineID   // deterministic
vm, err := client.Fork(ctx, arker.ForkRequest{SourceVMName: "ubuntu-base", IdempotencyKey: key})
```

A key is **one-shot**: it stays bound to the VM it created, so replaying a key
whose VM was deleted returns 404 rather than forking again. Reusing a key for a
*different* request is a 409 (`arker.IsConflict`).

## Errors

```go
var apiErr *arker.Error
if errors.As(err, &apiErr) { /* branch on apiErr.Code, never on Message */ }

arker.IsNotFound(err)   // 404 — ORG-SCOPED: another org's VM is indistinguishable from one that never existed
arker.IsConflict(err)   // 409
```

`UnknownOutcomeError` means a **mutation** failed at the transport layer, so
whether the server acted is unknown. The SDK does not retry those. Reads are
retried on retryable errors (and 429/502/503/504); a mutation is retried only
when it carries an idempotency key or the error says no work started.

## Sessions

Sessions are tabs: each keeps its own working directory, environment and
history, and each handles one run at a time. `SessionIdx` is find-or-create, and
**omitting it means index 0** — where a plain `Run` also lands. A run entering a
session that holds a live foreground process interrupts it, so give anything
long-lived its own session and probe from another.

```go
worker, _ := vm.CreateSession(ctx, arker.CreateSessionRequest{})
vm.Run(ctx, arker.RunRequest{Command: "server", SessionID: worker.SessionID, TimeToBackground: arker.Ptr(0)})
vm.Run(ctx, arker.RunRequest{Command: "echo probe"})   // default session; the server is untouched
```

`Run` is synchronous by default: if a run outlives the server's sync window the
API returns a background ack and `Run` polls it to a terminal state for you.
Pass `TimeToBackground: arker.Ptr(0)` to get the ack immediately and poll
`GetRun` yourself.

## Layout

```
go/
  *.go              the arker package
  examples/         fork-run, pty, sync-dir, filesystems
  tests/unit/       hermetic, httptest-backed — go test ./tests/unit/
  tests/e2e/        against a live deployment — see below
```

```bash
go test ./tests/unit/                                    # no network
ARKER_API_KEY=ark_... ARKER_BASE_URL=https://<env>/api \
  go test -v -timeout 30m ./tests/e2e/                   # creates real VMs
```

The e2e suite has **no default base URL** on purpose: it creates and deletes
real VMs, and defaulting to production would mean anyone with
`ARKER_API_KEY` exported for another reason silently runs it against prod. Set
`ARKER_TEST_GOLDEN` to fork from something other than `ubuntu-base`.

## Interactive PTY

`ConnectPTY` opens a terminal over a WebSocket. The connection outlives the
context passed in — that context bounds the dial only — so use `Close` to
detach and `Kill` to destroy the shell.

```go
pty, err := vm.ConnectPTY(ctx, arker.PTYOptions{
    Cols: arker.Ptr(120), Rows: arker.Ptr(40),
    OnData: func(b []byte) { os.Stdout.Write(b) },
})
pty.SendString("ls -la\n")
<-pty.Done()
```

PTYs are plain-text by default (`PlainPTYEnv`): the usual consumer is a program,
and stripping escape sequences after the fact is lossy. Pass
`Plain: arker.Ptr(false)` when a human is watching a full-screen TUI.

## Pools v2

Pools require the per-organization pools flag; a disabled organization receives
`404 not_found`. The three pool types are `managed` (Arker's multi-cloud
compute), `byoc` (workers Arker launches in your cloud account), and
`self_hosted` (BLAST workers you run).
This replaces pools v1 and the earlier BYOC API: quotes, fixed purchase terms, prices, and the
purchase idempotency key are removed with no compatibility layer.

Configure a regional endpoint (or provider and region) for pool operations.
Listing pools uses the control plane across regions; use each returned pool's
region for subsequent operations. Fork with exactly one of `pool_id` or
`pool_name`; names are case-insensitive within a region. A VM from outside a
`byoc` or `self_hosted` pool cannot be forked into it. Pool VMs do not support
interactive shells; use `Run` for commands.

`managed` and `byoc` pools request `resources` with all three quantities
(`vcpu`, `memory_mib`, `disk_mib`), including zeros; a `self_hosted` pool's
`resources` is an optional cap. Every pool reports `resources_reserved` (what
its workers contribute) and `resources_provisioned` (what its VMs use). A pool
is `creating` until its first worker is running (a `managed` pool starts
`running`), then `running`; `reason` says what needs attention. A `byoc` pool (provider `aws`)
needs setup: open `byoc.setup_url` in the specified AWS account and create the
CloudFormation stack. A `self_hosted` pool's platform is set by its first
worker; workers join with an API key limited to the pool,
`client.CreateAPIKey(ctx, arker.CreateAPIKeyRequest{Name: name, Scopes: []arker.APIKeyScope{{PoolID: poolID}}})`.
Only `managed` pools take commitments. Commitments and `self_hosted` pools are
not enabled in every deployment yet; there they return `unsupported_operation`.

Commitments last at least 24 hours, cannot overlap, and new commitments cannot
start in the past. Started commitments must remain unchanged, including expired
ones. The add-commitment helper reads the pool and appends to the entire list.
**Serialize commitment edits:** the API has no conditional update or atomic
append, so concurrent edits can overwrite unstarted commitments. Active
commitments prevent shrinking or deletion; resizing to zero otherwise releases
the reservation. Deleting a pool deletes every VM in it.

API errors retain their code, status, message, and server details. A
`429 quota_exceeded` is returned immediately; adjust the reservation/quota or
remove unused pools instead of retrying. Ordinary rate limits keep the
SDK's retry policy. `capacity_unavailable` means placement has no capacity;
inspect the pool's reserved resources, workers, and state. Fleet-to-pool fork
and unsupported shell refusals are surfaced without a fallback placement.
Creation has no documented idempotency guarantee: after an ambiguous transport
failure, inspect the pool list before creating again.

```go
pool, err := client.CreatePool(ctx, arker.CreatePoolRequest{
    Type: "managed", Platform: "icelake",
    Resources: &arker.PoolResources{VCPU: 8, MemoryMiB: 32768, DiskMiB: 102400},
})
if err != nil { return err }
_, err = client.AddPoolCommitment(ctx, pool.PoolID, arker.PoolCommitment{
    StartsAt: "2027-01-01T00:00:00Z", EndsAt: "2027-01-02T00:00:00Z",
})
```

Use `RenamePool(ctx, id, nil)` to clear a name. `UpdatePoolRequest.Commitments`
is a pointer to a slice: nil omits it, a pointer to an empty slice clears all
unstarted commitments. Inspect `Pool.Type` before using its type-specific fields.
