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
