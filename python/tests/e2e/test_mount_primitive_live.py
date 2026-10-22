"""Explicit opt-in Arker filesystem/mount primitive probe.

Run only against owned resources:

    ARKER_MOUNT_PRIMITIVE_LIVE=1 ARKER_API_KEY=... \
      python -m pytest tests/e2e/test_mount_primitive_live.py -s
"""

import json
import os

import pytest
from mount_primitive_probe import run_probe

from arker import Arker, ArkerError

if os.environ.get("ARKER_MOUNT_PRIMITIVE_LIVE") != "1":
    pytest.skip("explicit live mount probe opt-in is not enabled", allow_module_level=True)
if not os.environ.get("ARKER_API_KEY"):
    pytest.skip("live Arker credentials are not configured", allow_module_level=True)


def test_owned_mount_primitives_live():
    client = Arker(
        provider=os.environ.get("ARKER_PROVIDER", "aws"),
        region=os.environ.get("ARKER_REGION", "us-west-2"),
    )
    result = run_probe(client)
    print(json.dumps(result, sort_keys=True))
    assert result["success"] is True
    assert result["mount_states"][-1] == "mounted"
    assert result["content_round_trip"] is True
    assert result["cleanup"] == {
        "mount": "deleted",
        "vm": "deleted",
        "filesystem": "deleted",
    }
    for kind, identifier in (("vm", result["vm_id"]), ("filesystem", result["filesystem_id"])):
        with pytest.raises(ArkerError) as raised:
            client.get_vm(identifier) if kind == "vm" else client.get_filesystem(identifier)
        assert raised.value.status == 404
