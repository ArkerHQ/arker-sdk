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
print(result.stdout)

vm.sync("/tmp/hello.txt", "hello from Arker")
print(vm.sync("/tmp/hello.txt").decode())

vm.delete()
```

## Documentation and examples

Read the [Arker documentation](https://arker.ai/docs) and browse the runnable [examples](../examples).

## License

Apache-2.0

## Pools v2

Pools require the per-organization pools flag; a disabled organization receives
`404 not_found`. The three pool types are `managed` (Arker's multi-cloud
compute), `byoc` (workers Arker launches in your cloud account), and
`self_hosted` (BLAST workers you run).
This replaces pools v1 and the earlier BYOC API: quotes, fixed purchase terms, prices, and the
purchase idempotency key are removed with no compatibility layer.

Configure a regional endpoint (or provider and region) for pool operations.
Listing pools uses the control plane across regions; use each returned pool's
region for subsequent operations. Fork with exactly one of `pool_id` or
`pool_name`; names are case-insensitive within a region. A VM from outside a
`byoc` or `self_hosted` pool cannot be forked into it. Pool VMs do not support
interactive shells; use `run` for commands.

`managed` and `byoc` pools request `resources` with all three quantities
(`vcpu`, `memory_mib`, `disk_mib`), including zeros; a `self_hosted` pool's
`resources` is an optional cap. Every pool reports `resources_reserved` (what
its workers contribute) and `resources_provisioned` (what its VMs use). A pool
is `creating` until its first worker is running (a `managed` pool starts
`running`), then `running`; `reason` says what needs attention. A `byoc` pool (provider `aws`)
needs setup: open `byoc.setup_url` in the specified AWS account and create the
CloudFormation stack. A `self_hosted` pool's platform is set by its first
worker; workers join with an API key limited to the pool,
`arker.create_api_key(name=..., scopes=[{"pool_id": pool_id}])`.
Only `managed` pools take commitments. Commitments and `self_hosted` pools are
not enabled in every deployment yet; there they return `unsupported_operation`.

Commitments last at least 24 hours, cannot overlap, and new commitments cannot
start in the past. Started commitments must remain unchanged, including expired
ones. The add-commitment helper reads the pool and appends to the entire list.
**Serialize commitment edits:** the API has no conditional update or atomic
append, so concurrent edits can overwrite unstarted commitments. Active
commitments prevent shrinking or deletion; resizing to zero otherwise releases
the reservation. Deleting a pool deletes every VM in it.

API errors retain their code, status, message, and server details. A
`429 quota_exceeded` is returned immediately; adjust the reservation/quota or
remove unused pools instead of retrying. Ordinary rate limits keep the
SDK's retry policy. `capacity_unavailable` means placement has no capacity;
inspect the pool's reserved resources, workers, and state. Fleet-to-pool fork
and unsupported shell refusals are surfaced without a fallback placement.
Creation has no documented idempotency guarantee: after an ambiguous transport
failure, inspect the pool list before creating again.

```python
from arker import CreateManagedPoolRequest, PoolResources, PoolCommitment

pool = arker.create_pool(
    CreateManagedPoolRequest(
        type="managed",
        platform="icelake",
        resources=PoolResources(vcpu=8, memory_mib=32768, disk_mib=102400),
    )
)
arker.add_pool_commitment(
    pool.pool_id,
    PoolCommitment(
        starts_at="2027-01-01T00:00:00Z",
        ends_at="2027-01-02T00:00:00Z",
    ),
)
arker.fork(image="ubuntu", pool_id=pool.pool_id)
arker.rename_pool(pool.pool_id, None)  # Clear the name.
```

`update_pool` accepts `UpdatePoolRequest` or a dictionary. Dataclass fields set
to `None` are omitted; use `rename_pool(id, None)` or `{"name": None}` to clear a name.
