"""Live tests for tags, listing, names, filesystem snapshots, network
allowlists and termination in the Modal compatibility layer.

Run with:

    ARKER_API_KEY=... python -m pytest tests/e2e/test_modal_lifecycle_e2e.py
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
import uuid

import pytest

from arker import Arker, modal

if not os.environ.get("ARKER_API_KEY"):
    pytest.skip("live Arker credentials are not configured", allow_module_level=True)

if not os.environ.get("ARKER_BASE_URL"):
    os.environ.setdefault("ARKER_PROVIDER", "aws")
    os.environ.setdefault("ARKER_REGION", "us-east-1")

RUN = uuid.uuid4().hex[:8]


@pytest.fixture(scope="module")
def sb():
    sandbox = modal.Sandbox.create(
        name=f"modal-e2e-{RUN}", tags={"run": RUN}, env={"SANDBOX_VAR": "kept"}, workdir="/tmp", timeout=900
    )
    yield sandbox
    sandbox.terminate()


def _ids(sandboxes) -> list[str]:
    return [sandbox.object_id for sandbox in sandboxes]


def test_tags_list_and_names(sb: modal.Sandbox) -> None:
    assert _ids(modal.Sandbox.list(tags={"run": RUN})) == [sb.object_id]
    sb.set_tags({"run": RUN, "stage": "two"})
    assert modal.Sandbox.from_id(sb.object_id).get_tags() == {"run": RUN, "stage": "two"}
    assert _ids(modal.Sandbox.list(tags={"run": RUN, "stage": "two"})) == [sb.object_id]
    assert _ids(modal.Sandbox.list(tags={"run": RUN, "stage": "three"})) == []
    named = modal.Sandbox.from_name("any-app", f"modal-e2e-{RUN}")
    assert named.object_id == sb.object_id
    assert named.exec("pwd").stdout.read() == "/tmp\n"
    with pytest.raises(modal.NotFoundError):
        modal.Sandbox.from_name("any-app", f"no-such-sandbox-{RUN}")


def test_snapshot_filesystem_is_an_image(sb: modal.Sandbox) -> None:
    with sb.open("/tmp/state.txt", "w") as handle:
        handle.write("before")
    image = sb.snapshot_filesystem(ttl=3600)
    try:
        rules = Arker().get_vm(image.object_id).policies.rules
        assert rules["compat-timeout"].after_created == 3600
        assert image.object_id not in _ids(modal.Sandbox.list(tags={"run": RUN}))
        assert image.build() is image
        with pytest.raises(modal.NotFoundError):
            modal.Image.from_id(sb.object_id)
        restored = modal.Sandbox.create(image=modal.Image.from_id(image.object_id), timeout=300)
        try:
            assert restored.exec("cat", "/tmp/state.txt").stdout.read() == "before"
            assert restored.exec("sh", "-c", "echo $SANDBOX_VAR").stdout.read() == "kept\n"
        finally:
            restored.terminate()
    finally:
        Arker().get_vm(image.object_id).delete()


def test_outbound_allowlists(reachable) -> None:
    sandbox = modal.Sandbox.create(
        outbound_cidr_allowlist=["1.1.1.1/32"], outbound_domain_allowlist=["example.com"], timeout=300
    )
    try:

        def run(cmd: str) -> str:
            return sandbox.exec("bash", "-c", cmd).stdout.read()

        urls = ("https://1.1.1.1", "https://example.com", "https://github.com")
        assert reachable(run, *urls) == {
            "https://1.1.1.1": True,
            "https://example.com": True,
            "https://github.com": False,
        }
    finally:
        sandbox.terminate()


def test_inbound_cidr_allowlist() -> None:
    sandbox = modal.Sandbox.create(encrypted_ports=[8000], inbound_cidr_allowlist=["192.0.2.0/24"], timeout=300)
    try:
        sandbox.exec("python3", "-m", "http.server", "8000", workdir="/tmp")
        assert (
            sandbox.exec("bash", "-c", "sleep 2; curl -s -o /dev/null -w '%{http_code}' localhost:8000").stdout.read()
            == "200"
        )
        url = modal.Sandbox.from_id(sandbox.object_id).tunnels()[8000].url
        with pytest.raises(urllib.error.URLError):
            urllib.request.urlopen(url, timeout=10)
    finally:
        sandbox.terminate()


def test_ready_detach_and_terminate() -> None:
    finished = modal.Sandbox.create("true", timeout=300)
    finished.wait_until_ready()
    finished.wait()
    finished.detach()
    assert finished.terminate(wait=True) == 0
    running = modal.Sandbox.create(timeout=300)
    assert running.terminate(wait=True) == 137
    assert running.returncode == 137
    with pytest.raises(modal.InvalidError):
        running.wait_until_ready(timeout=0)
