# Arker Go SDK

```go
import arker "github.com/ArkerHQ/arker-sdk/go"

client, err := arker.New(arker.Options{APIKey: os.Getenv("ARKER_API_KEY")})
vm, err := client.Fork(ctx, arker.ForkRequest{SourceVMName: "ubuntu-base"})
```

## Idempotency

Every `Fork` carries an `Idempotency-Key`. One is generated per call, so the
SDK's **own** retries can never produce a second VM.

Set `ForkRequest.IdempotencyKey` yourself to extend that across processes —
that is the only form that protects **your** retry after an
`UnknownOutcomeError`, because a fresh `Fork` call otherwise mints a new key
and the server sees an unrelated request.

```go
vm, err := client.Fork(ctx, arker.ForkRequest{
    SourceVMName:   "ubuntu-base",
    IdempotencyKey: fmt.Sprintf("provision-%s", jobID), // stable across your retries
})
```

A key is one-shot and bound to the VM it created: reusing it for a different
request is a 409 (`arker.IsConflict`), and reusing it once that VM is deleted
is a 404 — never a second machine.

## Retries

`Attempts` is the total number of wire attempts (default 4), not four on top of
the first. Retried on 429/502/503/504, and on whatever the server's `retryable`
field says when present — that field wins over the status.

A **transport** failure on a mutation is never retried. The server may already
have acted, so the call returns `*UnknownOutcomeError` instead. Retry it only
with the same `IdempotencyKey`.
