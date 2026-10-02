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

Sync a whole directory, uploading only the files whose content or permissions differ:

```bash
arker sync-dir <vm-id> ./project project --exclude .env --exclude .git --exclude node_modules --dry-run
arker sync-dir <vm-id> ./project project --exclude .env --exclude .git --exclude node_modules
```

A relative remote path resolves against the working directory of the VM's default session, or of the session named by `--session-id`; `~` is not expanded. `--dry-run` lists what would be uploaded and writes nothing to the VM.

`--exclude` takes a glob (`*`, `?`, `**`) and is repeatable. A pattern without a slash matches a file or directory name at any depth; a pattern with a slash, such as `/build` or `'docs/**'`, matches the path relative to the local directory. An excluded directory is skipped with everything under it. Nothing is excluded by default, `.gitignore` is not read, and files already on the VM are never deleted.

Delete the VM when you are finished:

```bash
arker rm <vm-id>
```

Run `arker --help` for the available commands and flags.

## Documentation and examples

Read the [Arker documentation](https://arker.ai/docs) and browse the runnable [examples](../examples).

## License

Apache-2.0
