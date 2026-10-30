import json
from types import SimpleNamespace

import pytest

from arker import _compat_blaxel_packages as module


@pytest.mark.parametrize("failure", [None, "download", "rename", "install", "cleanup", "both"])
def test_staging_owns_candidates_and_preserves_failures(monkeypatch, tmp_path, failure):
    staged, commands = [], []
    primary = RuntimeError("installation failed")
    cleanup_error = RuntimeError("cleanup failed")

    def stage(*args):
        if failure == "download" and staged:
            raise primary
        candidate = module.ROOT + f"/bin/.candidate-{len(staged)}"
        staged.append(candidate)
        return candidate

    def run(command, **kwargs):
        commands.append(command)
        if command.startswith("/bin/mv") and failure == "rename":
            raise primary
        if command.startswith("/bin/rm") and failure in ("cleanup", "both"):
            raise cleanup_error
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr(module, "stage_pinned_artifact", stage)
    vm = SimpleNamespace(run=run)

    def attempt():
        with module.staged_firewall_packages(
            vm,
            json.loads(module.files("arker").joinpath("_guest_blaxel_packages.json").read_text())["profile"],
            tmp_path,
        ) as candidates:
            assert candidates == [path + ".apk" for path in staged]
            assert len(candidates) == 4
            if failure in ("install", "both"):
                raise primary

    if failure:
        with pytest.raises(RuntimeError) as caught:
            attempt()
        assert caught.value is (cleanup_error if failure == "cleanup" else primary)
        if failure == "both":
            assert caught.value.cleanup_error is cleanup_error
    else:
        attempt()
    cleanup = commands[-1]
    assert cleanup.startswith("/bin/rm -f -- ")
    for original in staged:
        assert original in cleanup and original + ".apk" in cleanup
    assert "*" not in cleanup


def test_unqualified_image_has_no_transport_activity(tmp_path):
    with (
        pytest.raises(ValueError, match="runtime profile"),
        module.staged_firewall_packages(SimpleNamespace(), "blaxel/base-image:latest", tmp_path),
    ):
        pytest.fail("must not yield")


@pytest.mark.parametrize(
    "configuration", [None, {}, {"entrypoint": []}, {"cmd": []}, {"entrypoint": ["daemon"], "cmd": []}]
)
def test_qualified_startup_must_be_suppressed_before_creation(configuration):
    image = json.loads(module.files("arker").joinpath("_guest_blaxel_packages.json").read_text())["image"]
    with pytest.raises(ValueError, match="empty entrypoint and cmd"):
        module.validate_firewall_startup(image, configuration)
    module.validate_firewall_startup(image, {"entrypoint": [], "cmd": [], "env": {"HOME": "/blaxel"}})
    module.validate_firewall_startup("other-explicit-image", configuration)


def test_facade_rejects_unsafe_qualified_startup_before_fork(monkeypatch):
    from arker import blaxel

    image = json.loads(module.files("arker").joinpath("_guest_blaxel_packages.json").read_text())["image"]
    monkeypatch.setattr(blaxel.SyncSandboxInstance, "_native_class", staticmethod(lambda: None))
    monkeypatch.setattr(blaxel, "routed_create", lambda vendor, config, handlers: ("arker", handlers["arker"]()))
    monkeypatch.setattr(blaxel, "arker_create", lambda *args, **kwargs: pytest.fail("must reject before fork"))
    with pytest.raises(ValueError, match="empty entrypoint and cmd"):
        blaxel.SyncSandboxInstance.create({"name": "test", "image": image})
