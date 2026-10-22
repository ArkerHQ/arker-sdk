import os
import stat
import subprocess
import types
from pathlib import Path

import pytest

import arker.e2b as shim


@pytest.mark.parametrize("attack", ["symlink", "fifo"])
def test_upload_never_syncs_through_attacker_controlled_staging(tmp_path, monkeypatch, attack):
    marker = tmp_path / "protected"
    marker.write_bytes(b"protected")
    destination = tmp_path / "destination"
    attacks = []
    stages = []

    def sync(path, content):
        stage = Path(path)
        stages.append(stage)
        if stat.S_IMODE(stage.parent.stat().st_mode) & 0o022:
            attacks.append(stage)
            if attack == "symlink":
                stage.symlink_to(marker)
            else:
                os.mkfifo(stage)
        assert not stage.is_symlink(), "privileged sync would follow attacker symlink"
        assert not stage.exists() or stat.S_ISREG(stage.stat().st_mode), "privileged sync would open FIFO"
        stage.write_bytes(content)

    def run(vm, command, **kwargs):
        command = command.replace(
            "--reuid=root --regid=root --init-groups", f"--reuid={os.getuid()} --regid={os.getgid()} --keep-groups"
        )
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=3)
        return shim._CommonCommandResult(stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode)

    monkeypatch.setattr(shim, "run_command", run)
    try:
        shim.Files(vm=types.SimpleNamespace(sync=sync)).write(str(destination), b"payload", user="root")
        assert destination.read_bytes() == b"payload"
        assert marker.read_bytes() == b"protected"
        assert not attacks
        assert all(not path.exists() for path in stages)
    finally:
        for path in attacks:
            path.unlink(missing_ok=True)


@pytest.mark.parametrize("kind", ["directory", "symlink", "fifo"])
def test_staging_collision_is_not_opened_or_cleaned(tmp_path, monkeypatch, kind):
    identifier = "collision-" + tmp_path.name
    path = Path("/tmp/.computesdk-e2b-write-" + identifier)
    if kind == "directory":
        path.mkdir()
        (path / "marker").write_text("owned by someone else")
    elif kind == "symlink":
        path.symlink_to(tmp_path, target_is_directory=True)
    else:
        os.mkfifo(path)
    monkeypatch.setattr(shim.uuid, "uuid4", lambda: types.SimpleNamespace(hex=identifier))

    def run(vm, command, **kwargs):
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=3)
        return shim._CommonCommandResult(stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode)

    monkeypatch.setattr(shim, "run_command", run)
    try:
        with pytest.raises(RuntimeError):
            shim.Files(vm=types.SimpleNamespace(sync=lambda *a: pytest.fail("must not sync"))).write("/unused", b"x")
        assert path.exists() or path.is_symlink()
        if kind == "directory":
            assert (path / "marker").read_text() == "owned by someone else"
    finally:
        if kind == "directory":
            (path / "marker").unlink()
            path.rmdir()
        else:
            path.unlink()


def test_requested_user_permissions_binary_modes_and_fifo_rejection(monkeypatch):
    import tempfile

    # A fresh public parent lets the isolated unprivileged guest identity enter.
    with tempfile.TemporaryDirectory(prefix="e2b-permissions-") as directory:
        parent = Path(directory)
        parent.chmod(0o777)
        uid, gid = (65534, 65534) if os.getuid() == 0 else (os.getuid(), os.getgid())
        stages = []

        def sync(path, content):
            stage = Path(path)
            stages.append(stage)
            assert stat.S_IMODE(stage.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(stage.stat().st_mode) == 0o600
            stage.write_bytes(content)

        def run(vm, command, **kwargs):
            command = command.replace(
                "--reuid=user --regid=user --init-groups", f"--reuid={uid} --regid={gid} --keep-groups"
            )
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=3)
            return shim._CommonCommandResult(stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode)

        monkeypatch.setattr(shim, "run_command", run)
        files = shim.Files(vm=types.SimpleNamespace(sync=sync))
        destination = parent / "binary"
        payload = bytes(range(256))
        files.write(str(destination), payload)
        assert destination.read_bytes() == payload
        assert destination.stat().st_uid == uid
        destination.chmod(0o640)
        files.write(str(destination), b"short")
        assert destination.read_bytes() == b"short"
        assert stat.S_IMODE(destination.stat().st_mode) == 0o640
        link = parent / "link"
        link.symlink_to(destination)
        files.write(str(link), b"linked")
        assert destination.read_bytes() == b"linked"
        protected = parent / "protected"
        protected.write_bytes(b"unchanged")
        protected.chmod(0o444)
        link.unlink()
        link.symlink_to(protected)
        with pytest.raises(RuntimeError, match="Permission denied"):
            files.write(str(link), b"forbidden")
        assert protected.read_bytes() == b"unchanged"
        fifo = parent / "fifo"
        os.mkfifo(fifo, 0o666)
        fifo.chmod(0o666)
        with pytest.raises(RuntimeError):
            files.write(str(fifo), b"must not block")
        assert all(not stage.parent.exists() for stage in stages)
