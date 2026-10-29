from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "e2e" / "e2b_baseline.py"
    spec = importlib.util.spec_from_file_location("e2b_baseline", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeVm:
    def __init__(self, name="computesdk-e2b-baseline-owned", results=(0, 0)):
        self.name = name
        self.results = iter(results)
        self.commands = []
        self.deleted = 0

    def refresh(self):
        return self

    def run(self, command):
        self.commands.append(command)
        return types.SimpleNamespace(exit_code=next(self.results), stderr="conflict")

    def delete(self):
        self.deleted += 1
        return types.SimpleNamespace(deleted=True)


def test_baseline_uses_one_stable_key_digest_and_explicit_x86_platform():
    baseline = _module()
    vm = FakeVm()
    calls = []

    class Client:
        def fork(self, **options):
            calls.append(options)
            if len(calls) == 1:
                raise ConnectionError("ambiguous write")
            return vm

    assert baseline.create_e2b_baseline(Client(), marker=vm.name, idempotency_key="stable", attempts=2) is vm
    assert calls[0] == calls[1]
    assert calls[0]["image"].endswith("sha256:8aebbf5e7d1209ebdc4bbc5999624556c2d9eebe91b9fded756ff2d688367065")
    assert calls[0]["platforms"] == ["icelake"]
    assert "! getent passwd 1000" in vm.commands[0]
    assert "rmdir /home/user" in vm.commands[1]


def test_baseline_conflict_deletes_only_marker_verified_owned_vm():
    baseline = _module()
    vm = FakeVm(results=(1,))
    client = types.SimpleNamespace(fork=lambda **options: vm)
    with pytest.raises(RuntimeError, match="preflight conflict"):
        baseline.create_e2b_baseline(client, marker=vm.name)
    assert vm.deleted == 1


def test_baseline_cleanup_refuses_marker_mismatch():
    baseline = _module()
    vm = FakeVm(name="customer-vm")
    with pytest.raises(RuntimeError, match="unproven"):
        baseline.delete_e2b_baseline(vm, "computesdk-e2b-baseline-owned")
    assert vm.deleted == 0


@pytest.mark.parametrize("cleanup_step", ["refresh", "delete", "ownership"])
def test_failed_preparation_preserves_error_when_cleanup_fails(monkeypatch, cleanup_step):
    baseline = _module()
    vm = FakeVm(results=(1,))
    cleanup_error = ConnectionError("cleanup unavailable")
    refreshes = 0

    def refresh():
        nonlocal refreshes
        refreshes += 1
        if refreshes > 1:
            if cleanup_step == "refresh":
                raise cleanup_error
            if cleanup_step == "ownership":
                vm.name = "customer-vm"
        return vm

    def delete():
        vm.deleted += 1
        raise cleanup_error

    monkeypatch.setattr(vm, "refresh", refresh)
    monkeypatch.setattr(vm, "delete", delete)
    client = types.SimpleNamespace(fork=lambda **options: vm)
    with pytest.raises((ConnectionError, RuntimeError)) as caught:
        baseline.create_e2b_baseline(client, marker=vm.name)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert "preflight conflict" in str(caught.value.__cause__)
    if cleanup_step == "ownership":
        assert "unproven" in str(caught.value)
    else:
        assert caught.value is cleanup_error
    assert vm.deleted == (1 if cleanup_step == "delete" else 0)


def test_creation_ownership_mismatch_never_runs_or_deletes_customer_vm():
    baseline = _module()
    vm = FakeVm(name="customer-vm")
    client = types.SimpleNamespace(fork=lambda **options: vm)
    with pytest.raises(RuntimeError, match="unproven") as caught:
        baseline.create_e2b_baseline(client, marker="computesdk-e2b-baseline-owned")
    assert "ownership marker mismatch" in str(caught.value.__cause__)
    assert not vm.commands
    assert vm.deleted == 0


def test_failed_cleanup_preserves_interruption_as_cause(monkeypatch):
    baseline = _module()
    vm = FakeVm()
    interrupted = KeyboardInterrupt()
    cleanup_error = ConnectionError("cleanup unavailable")

    def run(command):
        raise interrupted

    def delete():
        raise cleanup_error

    monkeypatch.setattr(vm, "run", run)
    monkeypatch.setattr(vm, "delete", delete)
    client = types.SimpleNamespace(fork=lambda **options: vm)
    with pytest.raises(ConnectionError) as caught:
        baseline.create_e2b_baseline(client, marker=vm.name)
    assert caught.value is cleanup_error
    assert caught.value.__cause__ is interrupted
