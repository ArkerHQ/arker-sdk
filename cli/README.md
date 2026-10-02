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

For persistent configuration, create `~/.arker/config.json`:

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

## JSON output

Use `--json` to keep results machine-readable. File reads return `path`,
`content`, and `encoding: "base64"`, including for empty or binary files.
File writes return `path`, `written`, and the number of `bytes` written.
Delete and cancel commands return their API result object and exit nonzero
when `deleted` or `cancelled` is false.

Run results use `run_id` in pending, running, and terminal states. Completed
results retain `runId` as a compatibility alias. Run stdout and stderr in JSON
are base64 strings with explicit encoding fields. Human output remains raw bytes.

## Live command output

`arker run` prints stdout and stderr while the command runs and exits with the
command's exit code. A command that finishes within a second is answered by one
request. After that the CLI polls the run, starting at 500 ms and backing off to
3 seconds, and writes each new byte once.

The service retains the last 10 MiB of a run's output. If a stream outgrows
that, the CLI warns, stops printing that stream, and prints what the service
retained when the command finishes; those bytes can repeat or skip output.

`--json` prints one JSON object when the command finishes, and
`--time-to-background 0` returns a run ID immediately.

## Standard input

`arker run` reads stdin only with `--stdin`, so a caller that leaves a pipe
open cannot stall it:

```bash
printf 'hello\n' | arker run --stdin <vm-id> cat
arker run --stdin <vm-id> sha256sum < archive.tar
```

Input is limited to 1 MiB and sent as exact bytes followed by EOF; an empty
input still sends EOF. The command then runs in a child shell with the
session's working directory and exported environment, so its `cd` and `export`
changes do not persist. Use `arker shell` for interactive input.

## Documentation and examples

Read the [Arker documentation](https://arker.ai/docs) and browse the runnable [examples](../examples).

## License

Apache-2.0
