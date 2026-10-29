import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from arker import _compat_e2b_artifact as artifact


def test_offline_cache_stages_verified_binary_without_network_or_policy_calls(tmp_path, monkeypatch):
    content = b"pinned fixture"
    digest = hashlib.sha256(content).hexdigest()
    (tmp_path / "_guest_e2b_envd_manifest.json").write_text(
        json.dumps({"artifacts": {"arm64": {"url": "unused", "sha256": digest}}})
    )
    monkeypatch.setattr(artifact, "files", lambda package: tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / digest).write_bytes(content)
    monkeypatch.setattr(
        artifact.urllib.request, "urlopen", lambda *a, **k: pytest.fail("offline cache must not download")
    )
    calls = []
    vm = SimpleNamespace(
        sync=lambda path, data: calls.append((path, data)), run=lambda command: SimpleNamespace(exit_code=0)
    )
    path = artifact.stage_envd_artifact(vm, "/root/.arker/e2b-envd", "arm64", cache)
    assert calls == [(path, content)]
    assert "/.candidate-" in path and not path.endswith(digest)
    second = artifact.stage_envd_artifact(vm, "/root/.arker/e2b-envd", "arm64", cache)
    assert second != path
    (cache / digest).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        artifact.stage_envd_artifact(vm, "/root/.arker/e2b-envd", "arm64", cache)
    assert len(calls) == 2


def test_download_verified_before_any_guest_write(tmp_path, monkeypatch):
    digest = hashlib.sha256(b"correct").hexdigest()
    (tmp_path / "_guest_e2b_envd_manifest.json").write_text(
        json.dumps({"artifacts": {"arm64": {"url": "unused", "sha256": digest}}})
    )
    monkeypatch.setattr(artifact, "files", lambda package: tmp_path)
    monkeypatch.setattr(artifact.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"incorrect"))
    vm = SimpleNamespace(sync=lambda *a: pytest.fail("must verify before sync"))
    with pytest.raises(ValueError, match="checksum"):
        artifact.stage_envd_artifact(vm, "/root/.arker/e2b-envd", "arm64", tmp_path / "cache")


def test_cache_read_is_bounded_without_stat_authorization(tmp_path, monkeypatch):
    monkeypatch.setattr(artifact, "_MAX_BYTES", 4)
    digest = hashlib.sha256(b"large cached content").hexdigest()
    (tmp_path / "_guest_e2b_envd_manifest.json").write_text(
        json.dumps({"artifacts": {"arm64": {"url": "unused", "sha256": digest}}})
    )
    monkeypatch.setattr(artifact, "files", lambda package: tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / digest).write_bytes(b"large cached content")
    vm = SimpleNamespace(sync=lambda *a: pytest.fail("oversized cache must not sync"))
    with pytest.raises(ValueError, match="checksum"):
        artifact.stage_envd_artifact(vm, "/root/.arker/e2b-envd", "arm64", cache)


@pytest.mark.parametrize("failure", ["sync", "chmod_exception", "chmod_status"])
def test_stage_removes_candidate_on_failure(tmp_path, monkeypatch, failure):
    content = b"artifact"
    digest = hashlib.sha256(content).hexdigest()
    (tmp_path / "_guest_e2b_envd_manifest.json").write_text(
        json.dumps({"artifacts": {"arm64": {"url": "unused", "sha256": digest}}})
    )
    monkeypatch.setattr(artifact, "files", lambda package: tmp_path)
    (tmp_path / digest).write_bytes(content)
    writes, commands = [], []

    def sync(path, data):
        writes.append(path)
        if failure == "sync":
            raise RuntimeError("sync failed")

    def run(command):
        commands.append(command)
        if command.startswith("chmod"):
            if failure == "chmod_exception":
                raise RuntimeError("chmod failed")
            return SimpleNamespace(exit_code=1)
        return SimpleNamespace(exit_code=0)

    with pytest.raises(RuntimeError):
        artifact.stage_envd_artifact(SimpleNamespace(sync=sync, run=run), "/root/.arker/e2b-envd", "arm64", tmp_path)
    assert commands[-1] == "rm -f -- " + writes[0]
