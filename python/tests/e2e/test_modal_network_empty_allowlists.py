"""Traffic checks, not certification of opaque Modal app/image translation."""

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(key) for key in ("ARKER_API_KEY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET")),
    reason="Live Arker and Modal credentials required",
)

CONNECT = """
import socket
try:
    connection = socket.create_connection(('1.1.1.1', 443), timeout=3)
except OSError:
    print('blocked')
else:
    connection.close()
    print('connected')
"""


@pytest.fixture(scope="module")
def sources():
    import modal

    from arker import Arker

    app = modal.App.lookup("arker-compat-image-test", create_if_missing=True)
    parent = Arker().fork(image="python:3.12-slim")
    try:
        yield app, parent
    finally:
        parent.delete()


@pytest.mark.parametrize("route", ["reference", "native", "arker"])
@pytest.mark.parametrize("option", [None, "outbound_cidr_allowlist", "outbound_domain_allowlist"])
def test_empty_modal_allowlist_blocks_actual_egress(sources, tmp_path, monkeypatch, route, option):
    import modal

    from arker.modal import Sandbox as ShimSandbox

    app, parent = sources
    provider = "arker" if route == "arker" else "modal"
    config = tmp_path / "router.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "existing": {"strategy": "priority", "order": [provider]},
                "create": {"strategy": "priority", "order": [provider]},
            }
        )
    )
    monkeypatch.setenv("COMPUTESDK_ROUTER_CONFIG", str(config))
    options = {option: []} if option else {}
    if route == "arker":
        sandbox = ShimSandbox.create(arker={"source_vm_id": parent.id}, **options)
    else:
        cls = modal.Sandbox if route == "reference" else ShimSandbox
        sandbox = cls.create(app=app, timeout=90, **options)
    try:
        process = sandbox.exec("python3", "-c", CONNECT)
        output = process.stdout.read()
        assert process.wait() == 0
        assert output == ("blocked\n" if option else "connected\n"), (route, option, output)
    finally:
        sandbox.terminate()
