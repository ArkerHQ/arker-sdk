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
