from __future__ import annotations

import base64
import json
import os
from contextlib import ExitStack

import pytest

from arker import Arker, ArkerError
from arker._compat import arker_create


@pytest.mark.skipif(
    not all(
        os.environ.get(key)
        for key in (
            "ARKER_API_KEY",
            "COMPAT_PRIVATE_REGISTRY_IMAGE",
            "COMPAT_REGISTRY_USERNAME",
            "COMPAT_REGISTRY_PASSWORD",
        )
    ),
    reason="live Arker and authenticated private-registry fixture required",
)
def test_private_image_auth_is_import_only_and_children_are_independent(monkeypatch):
    credentials = {
        "username": os.environ["COMPAT_REGISTRY_USERNAME"],
        "password": os.environ["COMPAT_REGISTRY_PASSWORD"],
    }
    original_fork = Arker.fork
    imports = []
    forks = []

    def observed_fork(self, *args, **options):
        if options.get("image"):
            assert options["registry_auth"] == credentials
            imports.append(options["image"])
        else:
            assert options.get("source_vm_id")
            assert "registry_auth" not in options
            forks.append(options["source_vm_id"])
        return original_fork(self, *args, **options)

    monkeypatch.setattr(Arker, "fork", observed_fork)
    with ExitStack() as cleanup:
        parent = arker_create({"registry_auth": credentials}, image=os.environ["COMPAT_PRIVATE_REGISTRY_IMAGE"])
        cleanup.callback(parent.delete)
        parent.sync("/tmp/private-registry-marker", b"parent")
        monkeypatch.setattr(Arker, "fork", original_fork)
        with pytest.raises(ArkerError) as refused:
            unexpected = arker_create(
                {"registry_auth": {**credentials, "password": credentials["password"] + "-wrong"}},
                image=os.environ["COMPAT_PRIVATE_REGISTRY_IMAGE"],
            )
            cleanup.callback(unexpected.delete)
            pytest.fail("cached private image accepted invalid registry credentials")
        assert refused.value.status == 422
        assert refused.value.code == "operation_failed"
        assert refused.value.body.details.reason == "image_pull_failed"
        assert refused.value.body.recovery.work == "unknown"
        diagnostic = json.dumps(refused.value.raw)
        assert credentials["password"] not in diagnostic
        encoded_auth = base64.b64encode(f"{credentials['username']}:{credentials['password']}-wrong".encode()).decode()
        assert encoded_auth not in diagnostic
        monkeypatch.setattr(Arker, "fork", observed_fork)
        children = []
        for _ in range(3):
            child = arker_create({"source_vm_id": parent.id, "registry_auth": credentials})
            cleanup.callback(child.delete)
            children.append(child)
            assert child.sync("/tmp/private-registry-marker") == b"parent"
            assert credentials["password"] not in child.run("env").stdout
        assert imports == [os.environ["COMPAT_PRIVATE_REGISTRY_IMAGE"]]
        assert forks == [parent.id] * 3
        assert len({parent.id, *(child.id for child in children)}) == 4
        children[0].sync("/tmp/private-registry-marker", b"changed")
        assert parent.sync("/tmp/private-registry-marker") == b"parent"
        assert all(child.sync("/tmp/private-registry-marker") == b"parent" for child in children[1:])
