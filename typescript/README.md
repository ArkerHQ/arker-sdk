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

### API errors

`ArkerError.code`, `.message`, and `.status` remain available. For a recognized response, `.body` contains the generated HTTP or sync-file error union. Narrow `body.code` to access its typed details, request metadata, and recovery handles. `.raw` preserves the original error payload, including unknown future codes or fields; `.body` is `undefined` when validation fails.

A retry delay is a scheduling hint, not permission to repeat a mutation. Automatic retries require a read, an idempotent request (including a keyed operation or upload chunk), or a typed `not_started` outcome. Failures with continuing, stopped, or unknown work are surfaced with their recovery context. Network failures keep their existing unknown-outcome behavior.
