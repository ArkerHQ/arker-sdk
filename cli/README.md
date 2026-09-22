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

### Identify the active installation

`@arker-ai/cli` is the standalone CLI package. The SDK package also includes a
compatibility `arker` executable; its version is the SDK version. npm and Bun
global installs can put different executables on your `PATH`.

```bash
type -a arker
arker --version --json
```

The JSON result gives the owning package, version, and executable path without
connecting to Arker. Run each path reported by `type -a arker` with
`--version --json` to compare installations. Your shell uses the first match.
Upgrade `@arker-ai/cli` with the same package manager that owns the executable
you use, then check the path and version again. Older executables may print
only their version; use their path to find the corresponding installation.

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
