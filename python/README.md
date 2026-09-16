<div align="center">

<img src="../assets/banner.png" alt="Arker" width="480" />

[Docs](https://arker.ai/docs) / [Benchmarks](https://arker.ai/benchmarks) / [Console](https://arker.ai/console)

</div>

# Arker Python SDK

Use the Arker Python SDK to fork VMs, run commands, and sync files.

[![PyPI](https://img.shields.io/pypi/v/arker.svg?style=flat-square&label=pypi)](https://pypi.org/project/arker/)

## Install

```bash
pip install arker
```

The SDK requires Python 3.10 or later.

## Get started

Sign up and get your API key at [arker.ai/console](https://arker.ai/console).

`ARKER_API_KEY`, `ARKER_PROVIDER`, and `ARKER_REGION` must be set in the environment or passed directly to `Arker()`.

Fork a source VM, run a command, sync a file, and delete the VM:

```python
from arker import Arker

arker = Arker()
vm = arker.fork(source_vm_name="ubuntu-coding")

result = vm.run("python3 -c 'print(2 + 2)'")
print(result.stdout.decode())

vm.sync("/tmp/hello.txt", "hello from Arker")
print(vm.sync("/tmp/hello.txt").decode())

vm.delete()
```

## Documentation and examples

Read the [Arker documentation](https://arker.ai/docs) and browse the runnable [examples](../examples).

## License

Apache-2.0

### API errors

`ArkerError.code`, `.message`, and `.status` remain available. For a recognized response, `.body` contains the generated HTTP or sync-file error model. Branch on `body.code` to inspect its typed details. Request metadata and partial-work handles remain on that body. `.raw` preserves the original error payload, including unknown future codes or fields; `.body` is `None` when the payload fails contract validation or decoding. Validation uses the packaged OpenAPI contract, including recovery requirements.

A retry delay is a scheduling hint, not permission to repeat a mutation. Automatic retries require a read, an idempotent request (including a keyed operation or upload chunk), or a typed `not_started` outcome. Failures with continuing, stopped, or unknown work are surfaced with their recovery context. Network failures keep their existing unknown-outcome behavior.
