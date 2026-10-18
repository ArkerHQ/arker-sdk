# Arker Go SDK

Go client for the Arker VM API. The surface mirrors the [Python](../python) and
[TypeScript](../typescript) SDKs: a `Client` for org-wide calls, and a `VM`
handle for everything scoped to one machine.

```bash
go get github.com/ArkerHQ/arker-sdk/go
```

```go
client, err := arker.New(arker.Options{})       // ARKER_API_KEY + ARKER_BASE_URL
vm, err := client.Fork(ctx, arker.ForkRequest{SourceVMName: "ubuntu-base"})
defer vm.Delete(ctx)

out, err := vm.Run(ctx, arker.RunRequest{Command: "echo hello"})
fmt.Print(out.Stdout)
```

## Configuration

`New` reads `ARKER_API_KEY`, `ARKER_BASE_URL`, `ARKER_CONTROL_BASE_URL`,
`ARKER_PROVIDER` and `ARKER_REGION`, and explicit `Options` win over all of them.

`BaseURL` overrides `Provider` and `Region`; otherwise provide both. Org-wide
calls do not require a placement. VM operations require a regional endpoint.

## Surface

| Client | VM |
| --- | --- |
| `VM` `Fork` `GetVM` `ListVMs` | `Refresh` `Fork` `Update` `Delete` |
| `ListRuns` `ListRegions` `Whoami` | `Run` `ListRuns` `GetRun` `CancelRun` |
| `ListFilesystems` `CreateFilesystem` | `ListSessions` `CreateSession` `GetSession` |
| `GetFilesystem` `DeleteFilesystem` | `UpdateSession` `DeleteSession` |
| `ListPools` `GetPool` `ListPoolWorkers` | |
| `CreatePool` `UpdatePool` `RenamePool` `DeletePool` `AddPoolCommitment` | |
| `BaseURL` `ControlBaseURL` `Provider` `Region` | `GetPolicies` `SetPolicies` |
| package-level `DiscoverRegions` | `ReadFile` `WriteFile` `SyncDir` |
| | `ListMounts` `CreateMount` `DeleteMount` `ConnectPTY` |

Two differences from Python and TypeScript follow Go's type system:

- **`ReadFile` / `WriteFile`** rather than one `sync(path, data?)`. Splitting
  them also removes the nil-versus-empty ambiguity when writing a zero-length
  file.
- **`Run` returns one `*RunResult`** with a `Type` of `"completed"` or
  `"background"` (`out.Completed()`), rather than a union of two result types.

Dockerfile forks are not supported by the Go SDK.

## Idempotency

Every `Fork` carries an idempotency key. For retries outside the SDK, set
`ForkRequest.IdempotencyKey` to a value stable across processes.

```go
key := "provision-" + installationID + "-" + machineID   // deterministic
vm, err := client.Fork(ctx, arker.ForkRequest{SourceVMName: "ubuntu-base", IdempotencyKey: key})
```

A key remains bound to its request. Reusing it for a different request returns
409 (`arker.IsConflict`).

## Errors

```go
var apiErr *arker.Error
if errors.As(err, &apiErr) { /* branch on apiErr.Code, never on Message */ }

arker.IsNotFound(err)   // 404
arker.IsConflict(err)   // 409
```

`UnknownOutcomeError` means a **mutation** failed at the transport layer, so
whether the server acted is unknown. The SDK does not retry those: the work may
be done, and retrying blind is how one fork becomes two VMs. Reads are retried
normally, as are responses the server marks retryable (and 429/502/503/504).

## Sessions

Sessions are tabs: each keeps its own working directory, environment and
history, and each handles one run at a time. `SessionIdx` is find-or-create, and
**omitting it means index 0** - where a plain `Run` also lands. A run entering a
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

## Interactive PTY

`ConnectPTY` opens a terminal over a WebSocket. The connection outlives the
context passed in - that context bounds the dial only - so use `Close` to
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
Configure a regional endpoint (or provider and region) for pool operations.
Listing pools uses the control plane across regions; use each returned pool's
region for subsequent operations. Fork with exactly one of `pool_id` or
`pool_name`; names are case-insensitive within a region. A fleet VM cannot be
forked into a pool: use a supported image source instead. Pool VMs do not support
interactive shells; use `run` for commands.

`managed` and `byoc` pools request `resources` with all three quantities
(`vcpu`, `memory_mib`, `disk_mib`), including zeros; a `self_hosted` pool's
`resources` is an optional cap. Every pool reports `resources_reserved` (what
its workers contribute) and `resources_provisioned` (what its VMs use). A pool
is `creating` until its first worker is running (a `managed` pool starts
`running`), then `running`; `reason` says what needs attention. A `byoc` pool (provider `aws`)
needs setup: open `byoc.setup_url` in the specified AWS account and create the
CloudFormation stack. A `self_hosted` pool's platform is set by its first
worker; workers join with an API key limited to the pool,
`c.CreateAPIKey(ctx, arker.CreateAPIKeyRequest{Scopes: []arker.APIKeyScope{{PoolID: poolID}}})`.
Only `managed` pools take commitments.

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
