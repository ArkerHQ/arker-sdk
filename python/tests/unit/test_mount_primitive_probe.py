import importlib.util
from pathlib import Path
from types import SimpleNamespace

PROBE = Path(__file__).parents[1] / "e2e" / "mount_primitive_probe.py"
SPEC = importlib.util.spec_from_file_location("mount_primitive_probe", PROBE)
module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(module)


class FakeVm:
    id = "vmh-0000000000000000-00000000000000000000000000"

    def __init__(self, calls):
        self.calls = calls

    def create_mount(self, **request):
        self.calls.append(("create_mount", request))
        return SimpleNamespace(mount_id="mount-1")

    def run(self, command, **options):
        self.calls.append(("run", command, options))
        return SimpleNamespace(type="completed", exit_code=0)

    def list_mounts(self, **request):
        self.calls.append(("list_mounts", request))
        return SimpleNamespace(
            mounts=[
                SimpleNamespace(
                    mount_id="mount-1",
                    path="/mnt/primitive-probe-fixed",
                    state="mounted",
                    state_detail=None,
                )
            ]
        )

    def sync(self, path, data=None):
        self.calls.append(("sync", path, data))
        return None if data is not None else b"arker-filesystem-probe:fixed\n"

    def delete_mount(self, identifier):
        self.calls.append(("delete_mount", identifier))
        return SimpleNamespace(deleted=True)

    def delete(self):
        self.calls.append(("delete_vm", self.id))


class FakeClient:
    def __init__(self, create_error=False, fork_response_lost=False):
        self.calls = []
        self.create_error = create_error
        self.fork_response_lost = fork_response_lost
        self.vm = FakeVm(self.calls)

    def create_filesystem(self, **request):
        self.calls.append(("create_filesystem", request))
        if self.create_error:
            raise OSError("response lost")
        return SimpleNamespace(filesystem_id="filesystem-1")

    def list_filesystems(self, **request):
        self.calls.append(("list_filesystems", request))
        return SimpleNamespace(filesystems=[])

    def fork(self, **request):
        self.calls.append(("fork", request))
        if self.fork_response_lost:
            self.fork_response_lost = False
            raise TypeError("response lost")
        return self.vm

    def delete_filesystem(self, identifier):
        self.calls.append(("delete_filesystem", identifier))
        return SimpleNamespace(deleted=True)


def test_probe_reconciles_before_polling_and_cleans_exact_resources_in_order():
    client = FakeClient()
    result = module.run_probe(client, nonce="fixed", sleep=lambda _: None)

    assert result["success"] is True
    actions = [call[0] for call in client.calls]
    assert actions.index("run") < actions.index("list_mounts")
    assert actions[-3:] == ["delete_mount", "delete_vm", "delete_filesystem"]
    assert client.calls[1][1]["idempotency_key"] == "primitive-probe-fixed"


def test_probe_never_retries_an_ambiguous_filesystem_create_without_exact_recovery():
    client = FakeClient(create_error=True)
    result = module.run_probe(client, nonce="fixed", sleep=lambda _: None)

    assert result["success"] is False
    assert result["unknown_filesystem_create"] is True
    assert [call[0] for call in client.calls].count("create_filesystem") == 1
    assert "fork" not in [call[0] for call in client.calls]
    assert result["cleanup"] == {}


def test_probe_replays_an_ambiguous_fork_with_the_same_key_and_cleans_recovered_vm():
    client = FakeClient(fork_response_lost=True)
    result = module.run_probe(client, nonce="fixed", sleep=lambda _: None)

    forks = [call[1] for call in client.calls if call[0] == "fork"]
    assert len(forks) == 2
    assert {request["idempotency_key"] for request in forks} == {"primitive-probe-fixed"}
    assert result["success"] is True
    assert result["cleanup"]["vm"] == "deleted"
