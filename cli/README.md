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

## Documentation and examples

Read the [Arker documentation](https://arker.ai/docs) and browse the runnable [examples](../examples).

## License

Apache-2.0

## Live command output

The service retains a bounded output window (currently 10 MiB combined at completion). If
that window replaces bytes already printed, the CLI warns and pauses that
stream until completion, then prints its final retained output. Those bytes
can overlap earlier output, and bytes discarded between polls cannot be
recovered. The command's exit status is still reported.

Plain `arker run` output is read from the run-status API while the command runs.
While snapshots keep growing, the CLI writes each new stdout and stderr byte
once, then returns the command's exit code. Status polling starts at 500 ms and
backs off to 3 seconds.

`--json` keeps one final JSON object. An explicit `--time-to-background` keeps its
requested wait behavior; zero returns a run ID immediately. `--memory-mib` keeps
the synchronous response so partial memory-allocation feedback is retained.


Pipe a finite input into a command, or redirect a local file:

```sh
printf 'hello\n' | arker run <vm_id> cat
arker run <vm_id> sha256sum < archive.tar
```

Input is limited to 1 MiB and sent as exact bytes followed by EOF. An empty
pipe still sends EOF. The command runs in a child shell with the selected
session's working directory and exported environment; its `cd` and `export`
changes do not persist. Interactive terminal input uses `arker shell`.

During `arker run`, the first Ctrl-C sends SIGINT to that run. A second Ctrl-C force-cancels it. A signal received before the run ID arrives is held until the ID is known. Runs with an inline wait or memory override use an idempotency key so a read-only lookup can find that same run while the original request is pending. The lookup checks the original command and options and cannot create another run. `arker shell` remains the interactive terminal command.
