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

## Pools

[Pools](https://arker.ai/docs/api/pools) set capacity aside for your VMs: on Arker's machines (`arker`), on hosts Arker launches in your AWS account (`aws`), or on your own machines (`self_hosted`). `create_pool` creates the pool in the client's region; changing a pool needs an admin API key.

```python
from arker import AwsPool

pool = arker.create_pool(
    type="aws",
    platform="icelake",
    resources={"vcpu": 120, "memory_mib": 245760, "disk_mib": 1048576},
    aws={"account_id": "123456789012", "subnet_ids": ["subnet-0a1b2c3d"]},
)
if isinstance(pool, AwsPool):
    print(pool.aws.setup_url)
# Open the link signed in to that AWS account and create the stack. The pool is
# `provisioning` until its hosts join, then `ready`.

vm = arker.fork(image="ubuntu:24.04", pool_id=pool.pool_id)
```

`update_pool` renames or resizes a pool, and replaces its commitments as a whole; `add_pool_commitment` adds one and keeps the rest. `list_pool_workers` lists the machines of an `aws` or `self_hosted` pool, and `create_pool_join_key`, `list_pool_join_keys`, and `revoke_pool_join_key` manage the keys machines use to join a `self_hosted` pool.

Errors to expect, as `ArkerError.code`:

- `quota_exceeded` (429): past a pool, machine, or join key limit; `body.details.limit.metric` names it.
- `invalid_state` (409): shrinking or deleting a pool during a commitment.
- `capacity_unavailable` (503): forking into an `aws` or `self_hosted` pool while none of its machines is connected. Set `queueing_timeout` to wait for one.
- `unsupported_operation`: forking a VM from outside an `aws` or `self_hosted` pool into it (start from an image, a golden, or a VM in the pool), or opening a shell on a VM in such a pool. Use runs instead.

## Documentation and examples

Read the [Arker documentation](https://arker.ai/docs) and browse the runnable [examples](../examples).

## License

Apache-2.0
