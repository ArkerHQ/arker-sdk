from __future__ import annotations

import json
import os
import secrets
import shlex
from contextlib import ExitStack

import pytest

from arker._compat import arker_create


@pytest.mark.skipif(not os.environ.get("ARKER_API_KEY"), reason="live Arker credentials required")
def test_image_parent_can_fork_independent_children_with_host_policy_secrets():
    """One registry import, then VM forks; policy values must not become guest env."""
    token = secrets.token_hex(24)
    document = {
        "secrets": {"COMPAT_HOST_TOKEN": token},
        "policies": [{"type": "outbound", "action": "deny"}],
    }
    with ExitStack() as cleanup:
        parent = arker_create({"policies": document}, image="ubuntu:24.04")
        cleanup.callback(parent.delete)
        assert parent.run("printf image-ready").stdout == "image-ready"
        assert parent.get_policies().secrets == {"COMPAT_HOST_TOKEN": "***"}
        # Prove the image can connect before interpreting failure as enforcement.
        connect = "timeout 10 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443'"
        parent.set_policies({"policies": [{"type": "outbound", "action": "allow"}]})
        assert parent.run(connect, timeout=15).exit_code == 0
        parent.set_policies(document)
        parent.sync("/tmp/compat-image-marker", b"parent")
        children = []
        for _ in range(3):
            child = arker_create({"source_vm_id": parent.id})
            cleanup.callback(child.delete)
            children.append(child)
            assert child.sync("/tmp/compat-image-marker") == b"parent"
            stored = child.get_policies()
            assert stored.secrets == {"COMPAT_HOST_TOKEN": "***"}
            assert token not in repr(stored)
            assert child.run("printenv COMPAT_HOST_TOKEN").exit_code == 1
            assert child.run(connect, timeout=15).exit_code != 0
        assert len({parent.id, *(child.id for child in children)}) == 4
        children[0].sync("/tmp/compat-image-marker", b"changed")
        assert parent.sync("/tmp/compat-image-marker") == b"parent"
        for child in children[1:]:
            assert child.sync("/tmp/compat-image-marker") == b"parent"


@pytest.mark.skipif(not os.environ.get("ARKER_API_KEY"), reason="live Arker credentials required")
def test_shared_fork_injects_policy_secret_only_to_allowed_upstream():
    token = secrets.token_hex(24)
    document = {
        "secrets": {"COMPAT_HOST_TOKEN": token},
        "policies": [
            {
                "type": "outbound",
                "match": {"hosts": ["httpbingo.org"]},
                "action": {"rewrite": {"headers": {"X-Arker-Secret": "${secret:COMPAT_HOST_TOKEN}"}}},
            },
            {"type": "outbound", "action": "deny"},
        ],
    }
    sandbox = arker_create({"policies": document}, template_id="ubuntu")
    try:
        assert sandbox.get_policies().secrets == {"COMPAT_HOST_TOKEN": "***"}
        assert sandbox.run("printenv COMPAT_HOST_TOKEN").exit_code == 1
        denied = (
            "import urllib.request; "
            "urllib.request.urlopen('https://example.com', timeout=10).read()"
        )
        sandbox.set_policies({"policies": [{"type": "outbound", "action": "allow"}]})
        assert sandbox.run("python3 -c " + shlex.quote(denied), timeout=15).exit_code == 0
        sandbox.set_policies(document)
        code = (
            "import urllib.request; "
            "print(urllib.request.urlopen('https://httpbingo.org/anything', timeout=20).read().decode())"
        )
        result = sandbox.run("python3 -c " + shlex.quote(code), timeout=30)
        assert result.exit_code == 0, result.stderr
        headers = {key.lower(): value for key, value in json.loads(result.stdout)["headers"].items()}
        assert headers["x-arker-secret"] == [token]
        blocked = sandbox.run("python3 -c " + shlex.quote(denied), timeout=15)
        assert blocked.exit_code != 0
        # A hostname denied at ClientHello is reset before HTTP can return 403.
        assert "Connection reset by peer" in blocked.stderr or "403" in blocked.stderr, blocked.stderr
    finally:
        sandbox.delete()
