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

## Scoped API keys

A key reaches what its resource scopes and its operation scopes both allow:

| Kind | Scopes |
| --- | --- |
| Resource | `*` (everything; the default, and only valid alone), `vmid:<id>` (those VMs), `pool:<id>` (that pool's workers) |
| Operation | `op:sync`, `op:pty`, `op:run`, `op:heartbeat` (only those operations) |

A worker key is exactly `["pool:<id>", "op:heartbeat"]`, and neither scope is valid in any other set; only an admin can create one. A new key's role and reach cannot exceed the calling key's.

```python
from arker import Scopes

key = arker.create_api_key(name="ci", scopes=[Scopes.vm(vm.id), Scopes.op("run")]).key
worker = arker.create_api_key(name="worker", scopes=Scopes.worker(pool_id))
```

## Terminals in a browser

`vm.connect_pty()` authenticates with the `Authorization` header. A browser WebSocket cannot set headers, so a browser passes a short-lived key limited to one VM and `op:pty` as `api_key`; Arker refuses any broader key there. To hand a terminal URL to a browser:

```python
key = arker.create_pty_key(vm.id).key  # ["vmid:<id>", "op:pty"], expires in 5 minutes
url = vm.pty_url(vm.create_session().session_id, key)
```

## Documentation and examples

Read the [Arker documentation](https://arker.ai/docs) and browse the runnable [examples](../examples).

## License

Apache-2.0
