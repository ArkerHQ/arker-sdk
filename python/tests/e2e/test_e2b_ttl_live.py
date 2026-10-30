"""Explicit opt-in proof that Arker enforces E2B creation lifetimes server-side.

This creates exactly one owned child and keeps a command busy across its
deadline. It never mutates or deletes the configured source VM.
"""

import os
import time

import pytest

from arker import ArkerError
from arker.e2b import Sandbox

if os.environ.get("ARKER_E2B_TTL_LIVE") != "1":
    pytest.skip("explicit live E2B TTL probe opt-in is not enabled", allow_module_level=True)
if not os.environ.get("ARKER_API_KEY") or not os.environ.get("ARKER_SOURCE_VM_ID"):
    pytest.skip("live Arker credentials and an owned source VM are required", allow_module_level=True)


def test_busy_owned_child_is_deleted_at_server_lifetime():
    source_id = os.environ["ARKER_SOURCE_VM_ID"]
    config = {"api_key": os.environ["ARKER_API_KEY"], "source_vm_id": source_id}
    child = Sandbox._create_arker(config, None, 10, None, {})
    assert child.id != source_id
    try:
        schedules = [rule for rule in child.policies.rules.values() if rule.type == "schedule"]
        assert len(schedules) == 1
        assert (schedules[0].after_created, schedules[0].action) == (10, "delete")
        child.run("sleep 60", time_to_background=0)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                child.refresh()
            except ArkerError as error:
                if error.status == 404:
                    return
                raise
            time.sleep(1)
        pytest.fail("owned child still existed 40 seconds after its 10-second server lifetime")
    finally:
        try:
            child.delete()
        except ArkerError as error:
            if error.status != 404:
                raise
