from __future__ import annotations

import asyncio
import errno
import io
import runpy
import types
from importlib.resources import files

import pytest
from test_compat_process import LocalVM


def test_modal_filesystem_native_provider_remains_exact_passthrough():
    from arker.modal import Sandbox

    native = types.SimpleNamespace(filesystem=object(), object_id="native")
    assert Sandbox(native=native).filesystem is native.filesystem


@pytest.fixture
def modal_box(tmp_path):
    pytest.importorskip("modal")
    from arker.modal import Sandbox

    vm = LocalVM(tmp_path)
    vm.id = "local"
    box = Sandbox(vm=vm)
    yield box
    if box._processes is not None:
        for process in box._processes.list():
            if process.status == "running":
                box._processes.kill(process.name)
                box._processes.wait(process.name, max_wait=5000, interval=10)
    for process, _, _ in vm.runs.values():
        process.wait(timeout=5)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_modal_filesystem_reuses_native_types_and_binary_local_copy(tmp_path, asynchronous, modal_box):
    from modal.exception import SandboxFilesystemDirectoryNotEmptyError, SandboxFilesystemNotFoundError
    from modal.types import FileInfo, FileType

    fs = modal_box.filesystem
    root = str(tmp_path / "remote")
    payload = bytes(range(256)) * 1000

    async def exercise():
        await fs.make_directory.aio(root)
        await fs.write_bytes.aio(payload, root + "/binary")
        assert await fs.read_bytes.aio(root + "/binary") == payload
        await fs.write_text.aio("hello λ\n", root + "/nested/text")
        assert await fs.read_text.aio(root + "/nested/text") == "hello λ\n"
        info = await fs.stat.aio(root + "/binary")
        assert isinstance(info, FileInfo)
        assert info.type == FileType.FILE and info.size == len(payload)
        assert [entry.name for entry in await fs.list_files.aio(root)] == ["binary", "nested"]
        target = tmp_path / "download" / "binary"
        await fs.copy_to_local.aio(root + "/binary", target)
        assert target.read_bytes() == payload
        await fs.copy_from_local.aio(target, root + "/uploaded")
        assert await fs.read_bytes.aio(root + "/uploaded") == payload
        with pytest.raises(SandboxFilesystemDirectoryNotEmptyError):
            await fs.remove.aio(root)
        await fs.remove.aio(root, recursive=True)
        with pytest.raises(SandboxFilesystemNotFoundError):
            await fs.stat.aio(root)

    if asynchronous:
        asyncio.run(exercise())
    else:
        fs.make_directory(root)
        fs.write_bytes(payload, root + "/binary")
        assert fs.read_bytes(root + "/binary") == payload
        info = fs.stat(root + "/binary")
        assert isinstance(info, FileInfo) and info.type == FileType.FILE
        fs.remove(root, recursive=True)


def test_modal_filesystem_validates_paths_errors_and_symlink_metadata(modal_box, tmp_path):
    from modal.exception import (
        InvalidError,
        SandboxFilesystemNotADirectoryError,
        SandboxFilesystemPathAlreadyExistsError,
    )
    from modal.types import FileType

    from arker._compat import UnsupportedCompatibilityOption

    fs = modal_box.filesystem
    before = len(modal_box._vm.calls)
    with pytest.raises(InvalidError):
        fs.read_text("relative")
    with pytest.raises(UnsupportedCompatibilityOption, match="ordered guest event stream"):
        next(fs.watch(str(tmp_path)))
    assert len(modal_box._vm.calls) == before
    directory = str(tmp_path / "remote")
    fs.make_directory(directory)
    with pytest.raises(SandboxFilesystemPathAlreadyExistsError):
        fs.make_directory(directory, create_parents=False)
    fs.write_text("content", directory + "/file")
    with pytest.raises(SandboxFilesystemNotADirectoryError):
        fs.write_text("content", directory + "/file/child")
    (tmp_path / "remote/link").symlink_to("file")
    entry = fs.stat(directory + "/link")
    assert entry.type == FileType.SYMLINK and entry.symlink_target == "file"
    fs.write_text("replacement", directory + "/link")
    assert fs.stat(directory + "/link").type == FileType.FILE
    assert fs.read_text(directory + "/file") == "content"
    assert fs.read_text(directory + "/link") == "replacement"
    assert fs.stat(directory + "/link").permissions == "0644"


def test_modal_guest_interrupted_upload_never_changes_destination_or_leaves_staged_paths(tmp_path, monkeypatch):
    protocol = runpy.run_path(str(files("arker").joinpath("_guest_filesystem.py")))

    class BrokenSource(io.BytesIO):
        def read(self, size=-1):
            if self.tell():
                raise PermissionError("controlled local source failure")
            return super().read(8)

    destination = tmp_path / "existing"
    destination.write_bytes(b"original")
    monkeypatch.setattr(protocol["sys"], "stdin", types.SimpleNamespace(buffer=BrokenSource(b"partial payload")))
    with pytest.raises(PermissionError, match="controlled local source failure"):
        protocol["main"]({"WriteFile": {"path": str(destination)}})
    assert destination.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [destination]


def test_failed_source_aborts_real_guest_upload_and_preserves_destination(modal_box, tmp_path):
    from modal._utils.async_utils import synchronizer

    class BrokenSource(io.BytesIO):
        def read(self, size=-1):
            if self.tell():
                raise OSError("controlled source interruption")
            return super().read(8)

    directory = tmp_path / "aborted-upload"
    directory.mkdir()
    destination = directory / "existing"
    destination.write_bytes(b"original")
    filesystem = synchronizer._translate_in(modal_box.filesystem)
    with pytest.raises(OSError, match="controlled source interruption"):
        asyncio.run(filesystem._exec_fs_tool_write(BrokenSource(b"partial payload"), str(destination), "write_bytes"))
    processes = modal_box._processes.list()
    assert len(processes) == 1
    result = modal_box._processes.wait(processes[0].name, max_wait=5000, interval=10)
    assert result.status != "running"
    assert destination.read_bytes() == b"original"
    assert list(directory.iterdir()) == [destination]


@pytest.mark.parametrize("nonempty", [False, True])
def test_modal_write_to_a_directory_always_has_the_native_type(modal_box, tmp_path, nonempty):
    from modal.exception import SandboxFilesystemIsADirectoryError

    destination = tmp_path / "remote-directory"
    destination.mkdir()
    if nonempty:
        (destination / "child").write_bytes(b"preserved")
    with pytest.raises(SandboxFilesystemIsADirectoryError):
        modal_box.filesystem.write_bytes(b"never replace a directory", str(destination))
    assert destination.is_dir()
    if nonempty:
        assert (destination / "child").read_bytes() == b"preserved"


def test_modal_guest_busy_directory_is_classified_before_staging(tmp_path, monkeypatch):
    protocol = runpy.run_path(str(files("arker").joinpath("_guest_filesystem.py")))
    destination = tmp_path / "busy-directory"
    destination.mkdir()
    monkeypatch.setattr(protocol["sys"], "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"payload")))

    def busy(*args):
        raise OSError(errno.EBUSY, "controlled mount-point error")

    monkeypatch.setattr(protocol["os"], "replace", busy)
    with pytest.raises(IsADirectoryError):
        protocol["main"]({"WriteFile": {"path": str(destination)}})
    assert list(tmp_path.iterdir()) == [destination]


def test_modal_guest_destination_becoming_a_directory_during_commit_preserves_it(tmp_path, monkeypatch):
    protocol = runpy.run_path(str(files("arker").joinpath("_guest_filesystem.py")))
    destination = tmp_path / "racing-directory"
    monkeypatch.setattr(protocol["sys"], "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"payload")))

    def busy(*args):
        destination.mkdir()
        (destination / "child").write_bytes(b"preserved")
        raise OSError(errno.EBUSY, "controlled mount-point race")

    monkeypatch.setattr(protocol["os"], "replace", busy)
    with pytest.raises(IsADirectoryError):
        protocol["main"]({"WriteFile": {"path": str(destination)}})
    assert list(tmp_path.iterdir()) == [destination]
    assert (destination / "child").read_bytes() == b"preserved"
