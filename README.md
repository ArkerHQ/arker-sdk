<div align="center">

<img src="./assets/banner.png" alt="Arker" width="480" />

[Docs](https://arker.ai/docs) / [Benchmarks](https://arker.ai/benchmarks) / [Console](https://arker.ai/console)

</div>

# Arker

Arker provides hyper-elastic, durable virtual machines for agent workloads. Its core primitives are fork, run, and sync.

This repository contains the Arker CLI and the Python and TypeScript SDKs, providing convenient access to Arker.

[![PyPI](https://img.shields.io/pypi/v/arker.svg?style=flat-square&label=pypi)](https://pypi.org/project/arker/)
[![CLI](https://img.shields.io/npm/v/@arker-ai/cli.svg?style=flat-square&label=cli)](https://www.npmjs.com/package/@arker-ai/cli)
[![npm](https://img.shields.io/npm/v/@arker-ai/sdk.svg?style=flat-square&label=npm)](https://www.npmjs.com/package/@arker-ai/sdk)

## Get started

Sign up and get your API key at [arker.ai/console](https://arker.ai/console).

Read the [Arker documentation](https://arker.ai/docs), or see the package-specific guides:

- CLI: [CLI guide](./cli/README.md)
- TypeScript SDK: [TypeScript guide](./typescript/README.md)
- Python SDK: [Python guide](./python/README.md)

## VM-scoped PTY keys

A key issued with `vm_id`, `pty:connect`, and `time_to_delete` can create a
session on that VM and attach its PTY, but cannot list, patch, delete, or mint
PTY tickets. The TypeScript, Python, and Go PTY clients try the ticket endpoint
first; if it returns 403, they connect with the key in the WebSocket
`Sec-WebSocket-Protocol: arker-pty-key.<key>` handshake. The router consumes
that header and never forwards the raw key to the worker. Never put an API key
in a WebSocket URL. Legacy clients using a separately minted, short-lived HMAC
PTY ticket in `?ticket=` remain supported; that ticket is **not** the scoped
API key and the two credentials are not interchangeable.

If passing a scoped key to browser code, treat it as a secret: only hand it to
trusted code, and limit its VM scope and expiry at issuance.

## Examples

- [Browser](./examples/browser): Open two Wikipedia pages and fork a live checkpoint at each.
- [Coding agents](./examples/coding-agent): Run Claude Code, Codex, or Cursor in the background.
- [Firmware](./examples/firmware): Use a coding agent to edit firmware and run it with QEMU.
- [GPU coding agents](./examples/gpu-coding-agents): Run coding agents on parallel GPU workloads.
- [Policies](./examples/policies): Configure host-enforced egress policies.
- [React policy](./examples/react-policy): Control a VM's network policy from a React application.
- [Autoresearch](./examples/autoresearch): Tune a model in parallel with fractional GPUs.

## License

Apache-2.0
