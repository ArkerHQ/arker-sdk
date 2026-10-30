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


@pytest.fixture(autouse=True)
def bootstrap_canary(monkeypatch):
    from arker import _compat_e2b_bootstrap

    calls = []

    def bootstrap(vm):
        assert len(vm.commands) == 4
        calls.append(vm)

    monkeypatch.setattr(_compat_e2b_bootstrap, "bootstrap_envd", bootstrap)
    return calls


class FakeVm:
    def __init__(self, name="computesdk-e2b-baseline-owned", results=(0, 0, 0, 0)):
        self.name = name
        self.results = iter(results)
        self.commands = []
        self.deleted = 0

    def refresh(self):
        return self

    def run(self, command, **options):
        self.commands.append(command)
        return types.SimpleNamespace(exit_code=next(self.results), stderr="conflict")

    def delete(self):
        self.deleted += 1
        return types.SimpleNamespace(deleted=True)


def test_baseline_uses_one_stable_key_digest_and_explicit_x86_platform(bootstrap_canary):
    baseline = _module()
    vm = FakeVm()
    calls = []

    class Client:
        def fork(self, **options):
            calls.append(options)
            if len(calls) == 1:
                raise ConnectionError("ambiguous write")
            return vm

    assert (
        baseline.create_e2b_baseline(
            Client(), platform="graniterapids", marker=vm.name, idempotency_key="stable", attempts=2
        )
        is vm
    )
    assert calls[0] == calls[1]
    assert calls[0]["image"].endswith("sha256:8aebbf5e7d1209ebdc4bbc5999624556c2d9eebe91b9fded756ff2d688367065")
    assert calls[0]["platforms"] == ["graniterapids"]
    assert "! getent passwd 1000" in vm.commands[0]
    assert "apt-get install" in vm.commands[1]
    assert "rmdir /home/user" in vm.commands[2]
    assert "ip6tables" in vm.commands[3]
    assert bootstrap_canary == [vm]


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

    def run(command, **options):
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


def test_baseline_default_uses_regional_scheduling():
    baseline = _module()
    vm = FakeVm()
    calls = []
    client = types.SimpleNamespace(fork=lambda **options: calls.append(options) or vm)
    assert baseline.create_e2b_baseline(client, marker=vm.name) is vm
    assert "platforms" not in calls[0]


@pytest.mark.parametrize("platform", ["", "  ", 1])
def test_invalid_platform_refused_before_fork(platform):
    baseline = _module()
    with pytest.raises(ValueError, match="platform"):
        baseline.create_e2b_baseline(object(), platform=platform)


@pytest.mark.parametrize("stage", [1, 2, 3])
def test_baseline_prerequisite_or_capability_failure_cleans_owned_vm(stage):
    baseline = _module()
    vm = FakeVm(results=(*([0] * stage), 1))
    client = types.SimpleNamespace(fork=lambda **options: vm)
    with pytest.raises(RuntimeError):
        baseline.create_e2b_baseline(client, marker=vm.name)
    assert len(vm.commands) == stage + 1
    assert vm.deleted == 1


def test_failed_bootstrap_canary_refuses_baseline_and_deletes_it(monkeypatch):
    from arker import _compat_e2b_bootstrap

    baseline = _module()
    vm = FakeVm()
    failure = RuntimeError("envd isolation unavailable")

    def fail(current):
        assert current is vm and len(vm.commands) == 4
        raise failure

    monkeypatch.setattr(_compat_e2b_bootstrap, "bootstrap_envd", fail)
    client = types.SimpleNamespace(fork=lambda **options: vm)
    with pytest.raises(RuntimeError) as caught:
        baseline.create_e2b_baseline(client, marker=vm.name)
    assert caught.value is failure
    assert vm.deleted == 1
