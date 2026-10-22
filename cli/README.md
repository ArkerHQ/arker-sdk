<div align="center">

<img src="../assets/banner.png" alt="Arker" width="480" />

[Docs](https://arker.ai/docs) / [Benchmarks](https://arker.ai/benchmarks) / [Console](https://arker.ai/console)

</div>

# Arker CLI

The Arker CLI provides command-line access to Arker VMs.

[![npm](https://img.shields.io/npm/v/@arker-ai/cli.svg?style=flat-square&label=npm)](https://www.npmjs.com/package/@arker-ai/cli)

## Install

```bash
bun add --global @arker-ai/cli
```

The CLI requires Node.js 18 or later.

## Get started

Sign up and get your API key at [arker.ai/console](https://arker.ai/console).

`ARKER_API_KEY` is required. Compute commands also require a provider and
region unless `ARKER_BASE_URL` is configured.

```bash
export ARKER_API_KEY=ark_live_...
```

You can override the default placement:

```bash
export ARKER_PROVIDER=aws
export ARKER_REGION=us-west-2
```

Or save them once:

```bash
arker config set provider aws
arker config set region us-west-2
```

Flags override environment variables, which override saved values.
`arker config list` shows what is saved and `arker config unset region`
removes a value. The file is `~/.arker/config.json`:

```json
{
  "apiKey": "ark_live_...",
  "provider": "aws",
  "region": "us-west-2"
}
```

List the public source VMs:

```bash
arker vms ls --source-org-id ArkerHQ --public
```

Fork a source VM:

```bash
arker fork <source-vm-name>
```

Use the returned VM ID to run commands, sync files, or open a terminal:

```bash
arker run <vm-id> python3 -c 'print(2 + 2)'

arker sync <vm-id> /tmp/hello.txt "hello from Arker"
arker sync <vm-id> /tmp/hello.txt --read

arker shell <vm-id>
```

Delete the VM when you are finished:

```bash
arker rm <vm-id>
```

Run `arker --help` for the available commands and flags.

## Secret file uploads (unreleased preview)

The updated CLI supports `arker sync <vm-id> /run/credential - --secret`.
Provide credential bytes on stdin, never as a command-line argument. Every
uploaded chunk is marked secret to keep file contents out of sync logs and
previews; this does not suppress deliberate later process output. `--secret` refuses reads.
Before provisioning a VM, automated launchers must verify `arker sync --help`
explicitly advertises `--secret`. Do not infer support from the version: the
tracked package version is unchanged and published older CLIs lack this feature.

The preview CLI also accepts `arker fork <source> --idempotency-key <key>`.
Reuse the same key and unchanged request after an ambiguous lost response;
the CLI sends it as the `Idempotency-Key` header, never in the request body.
Unkeyed forks remain unkeyed. Launchers must check fork help before depending
on explicit keys rather than assuming older versions support this option.

## JSON output

`--json` prints one JSON object on stdout for every command, including `sync`
and the delete and cancel commands. Run results use `run_id` in every state;
completed results also keep `runId`.

## Running commands

`arker run` prints output as the command runs and exits with the command's
exit code. The service keeps the last 10 MiB of output; past that the CLI
warns and prints what was kept when the command finishes. `--json` prints one
object when the command finishes, and `--time-to-background 0` returns a run
ID immediately.

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
is `creating` until its first worker is running (a `managed` pool starts
`running`), then `running`; `reason` says what needs attention. A `byoc` pool (provider `aws`)
needs setup: open `byoc.setup_url` in the specified AWS account and create the
CloudFormation stack. A `self_hosted` pool's platform is set by its first
worker; workers join with an API key limited to the pool,
`arker api-keys create --name <name> --pool-id <pool_id>`.
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

```sh
arker pools create --type byoc --platform icelake --name ci \
  --provider aws --region us-west-2 \
  --vcpu 8 --memory-mib 32768 --disk-mib 102400 \
  --account-id 123456789012 --subnet-id subnet-123
# Open the printed setup_url in that AWS account and create the stack.
arker pools get <pool_id>
arker pools workers <pool_id>
arker pools update <pool_id> --file update.json
arker pools add-commitment <pool_id> \
  --starts-at 2027-01-01T00:00:00Z --ends-at 2027-01-02T00:00:00Z
arker pools create --type self_hosted --name edge
arker fork ubuntu --pool-id <pool_id>
arker pools rm <pool_id>
```

`create --file request.json` accepts the full create request, including
commitments. `update --file update.json` accepts fields such as
`{"resources":{"vcpu":0,"memory_mib":0,"disk_mib":0}}`. Use `/dev/stdin` to read a request from stdin.
`create --json` writes only the response JSON, including `byoc.setup_url`.
Repeat `--subnet-id`, `--security-group-id`, and `--instance-type` for multiple
values; `--isolation vms` keeps VM snapshots in the customer's account.
