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

## Documentation and examples

Read the [Arker documentation](https://arker.ai/docs) and browse the runnable [examples](../examples).

## License

Apache-2.0

## Directory sync

Remote paths are absolute or relative to the default guest session's working
directory. Use `--session-id` to select another session. No shell expansion is
applied to paths.

```sh
arker sync-dir <vm_id> ./project ./project --dry-run \
  --exclude .env --exclude .git --exclude node_modules
arker sync-dir <vm_id> ./project ./project \
  --exclude .env --exclude .git --exclude node_modules
```

`--exclude` is repeatable. A pattern without a slash matches a file or directory
name at any depth. A pattern with a slash matches a path relative to the local
root; for example, `--exclude 'docs/**'`. Quote globs so the local shell does not
expand them. Matching includes dotfiles. Excluding a directory skips its contents.
There are no automatic exclusions, and `.gitignore` is not loaded.

`--dry-run` reads the remote manifest and lists files that would be uploaded. It
does not write guest files or run extraction. With `--json`, the preview has
`dryRun: true`, a `planned` array of relative paths, byte counts, modes and entry kinds, and
zero `sent` and `bytesSent` counts. Unchanged files are counted in `skipped`.

Directory sync preserves symbolic links, including dangling links, without reading
their targets. Empty directories are uploaded too. The remote manifest currently
lists regular files only, so links and empty directories are sent on every sync.
Unchanged regular files are still skipped. Upload counts include all entry kinds;
byte counts include regular-file contents only.
