<div align="center">

<img src="../assets/banner.png" alt="Arker" width="480" />

[Docs](https://arker.ai/docs) / [Benchmarks](https://arker.ai/benchmarks) / [Console](https://arker.ai/console)

</div>

# Arker TypeScript SDK

Use the Arker TypeScript SDK to fork VMs, run commands, and sync files.

[![npm](https://img.shields.io/npm/v/@arker-ai/sdk.svg?style=flat-square&label=npm)](https://www.npmjs.com/package/@arker-ai/sdk)

## Install

```bash
bun add @arker-ai/sdk
```

The SDK requires Node.js 18 or later.

## Get started

Sign up and get your API key at [arker.ai/console](https://arker.ai/console).

`ARKER_API_KEY`, `ARKER_PROVIDER`, and `ARKER_REGION` must be set in the environment or passed directly to `new Arker()`.

Fork a source VM, run a command, sync a file, and delete the VM:

```ts
import { Arker } from "@arker-ai/sdk";

const arker = new Arker();
const vm = await arker.fork({ source_vm_name: "ubuntu-coding" });

const result = await vm.run("python3 -c 'print(2 + 2)'");
if (result.type === "completed") {
  console.log(new TextDecoder().decode(result.stdout));
}

await vm.sync("/tmp/hello.txt", "hello from Arker");
const data = await vm.sync("/tmp/hello.txt");
console.log(new TextDecoder().decode(data));

await vm.delete();
```

## Documentation and examples

Read the [Arker documentation](https://arker.ai/docs) and browse the runnable [examples](../examples).

## License

Apache-2.0

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
`pool_name`; names are case-insensitive within a region. A fleet VM cannot be
forked into a pool: use a supported image source instead. Pool VMs do not support
interactive shells; use `run` for commands.

`managed` and `byoc` pools request `resources` with all three quantities
(`vcpu`, `memory_mib`, `disk_mib`), including zeros; a `self_hosted` pool's
`resources` is an optional cap. Every pool reports `resources_reserved` (what
its workers contribute) and `resources_provisioned` (what its VMs use). A pool
is `creating` until `resources_reserved` first reaches `resources`, then
`active`; `reason` says what needs attention. A `byoc` pool (provider `aws`)
needs setup: open `byoc.setup_url` in the specified AWS account and create the
CloudFormation stack. A `self_hosted` pool's platform is set by its first
worker; workers join with an API key of the pool with action `join`. Only
`managed` pools take commitments.

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

```ts
const pool = await arker.createPool({
  type: "managed", platform: "icelake",
  resources: { vcpu: 8, memory_mib: 32768, disk_mib: 102400 },
});
await arker.addPoolCommitment(pool.pool_id, {
  starts_at: "2027-01-01T00:00:00Z", ends_at: "2027-01-02T00:00:00Z",
});
await arker.fork({ image: "ubuntu", pool_id: pool.pool_id });
```

See [the BYOC pool example](examples/aws-pool.ts) for AWS setup and readiness.
