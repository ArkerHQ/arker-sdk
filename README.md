<div align="center">

<img src="./assets/banner.png" alt="Arker" width="480" />

[Docs](https://arker.ai/docs) / [Benchmarks](https://arker.ai/benchmarks) / [Console](https://arker.ai/console)

</div>

# Arker

Arker provides hyper-elastic, durable virtual machines for agent workloads. Its core primitives are fork, run, and sync.

This repository contains the Arker CLI and the Python, TypeScript, and Go SDKs.

[![PyPI](https://img.shields.io/pypi/v/arker.svg?style=flat-square&label=pypi)](https://pypi.org/project/arker/)
[![CLI](https://img.shields.io/npm/v/@arker-ai/cli.svg?style=flat-square&label=cli)](https://www.npmjs.com/package/@arker-ai/cli)
[![npm](https://img.shields.io/npm/v/@arker-ai/sdk.svg?style=flat-square&label=npm)](https://www.npmjs.com/package/@arker-ai/sdk)

## Get started

Sign up and get your API key at [arker.ai/console](https://arker.ai/console).

Read the [Arker documentation](https://arker.ai/docs), or see the package-specific guides:

- CLI: [CLI guide](./cli/README.md)
- TypeScript SDK: [TypeScript guide](./typescript/README.md)
- Python SDK: [Python guide](./python/README.md)
- Go SDK: [Go guide](./go/README.md)

## Examples

- [Browser](./examples/browser): Open two Wikipedia pages and fork a live checkpoint at each.
- [GPU coding agents](./examples/gpu-coding-agents): Run coding agents on parallel GPU workloads.
- [Policies](./examples/policies): Configure host-enforced egress policies.
- [React policy](./examples/react-policy): Control a VM's network policy from a React application.
- [Autoresearch](./examples/autoresearch): Tune a model in parallel with fractional GPUs.

## License

Apache-2.0
