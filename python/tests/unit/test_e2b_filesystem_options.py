import asyncio
import os
import subprocess
from types import SimpleNamespace

import pytest
from e2b import FileType, InvalidArgumentException

import arker.e2b as shim
from arker._compat_e2b_async import _AsyncFiles


@pytest.fixture
def local_files(monkeypatch):
    def run(vm, command, **kwargs):
        command = command.replace(
            "--reuid=root --regid=root --init-groups",
            f"--reuid={os.getuid()} --regid={os.getgid()} --keep-groups",
        )
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=3)
        return shim._CommonCommandResult(stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode)

    monkeypatch.setattr(shim, "run_command", run)
    return shim.Files(vm=object())


def test_directory_creation_reports_creation_and_existing_directory(local_files, tmp_path):
    path = str(tmp_path / "nested" / "directory")
    assert local_files.make_dir(path, user="root") is True
    assert local_files.make_dir(path, user="root") is False
    regular = tmp_path / "file"
    regular.touch()
    with pytest.raises(RuntimeError):
        local_files.make_dir(str(regular / "child"), user="root")


def test_list_depth_preserves_names_and_dangling_symlinks(local_files, tmp_path):
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "child").write_text("child")
    unusual = "tab\tnewline\nseparator\x1f"
    (tmp_path / unusual).write_text("content")
    (tmp_path / "dangling").symlink_to("missing\n")
    entries = {entry.name: entry for entry in local_files.list(str(tmp_path), user="root")}
    assert set(entries) == {"directory", unusual, "dangling"}
    assert entries[unusual].size == 7
    assert entries["dangling"].type == FileType.SYMLINK
    assert entries["dangling"].symlink_target == "missing\n"
    for depth in (2,):
        entries = local_files.list(str(tmp_path), depth=depth, user="root")
        assert {entry.path for entry in entries} == {
            str(tmp_path / name) for name in ("directory", "directory/child", unusual, "dangling")
        }
    assert {entry.name for entry in local_files.list(str(tmp_path), depth=None)} == {"directory", unusual, "dangling"}
    with pytest.raises(InvalidArgumentException):
        local_files.list(str(tmp_path), depth=0)
    with pytest.raises(RuntimeError):
        local_files.list(str(tmp_path / "missing"), user="root")


def test_async_files_share_directory_options(local_files, tmp_path):
    async def check():
        files = _AsyncFiles(local_files)
        path = str(tmp_path / "async")
        assert await files.make_dir(path, user="root") is True
        assert await files.make_dir(path, user="root") is False
        assert await files.list(path, depth=None, user="root") == []

    asyncio.run(check())


def test_native_directory_options_are_forwarded():
    calls = []
    native = SimpleNamespace(
        make_dir=lambda *args, **kwargs: calls.append((args, kwargs)) or True,
        list=lambda *args, **kwargs: calls.append((args, kwargs)) or [],
    )
    files = shim.Files(native=native)
    assert files.make_dir("/directory", user="root", request_timeout=7) is True
    assert files.list("/directory", depth=2, user="root", request_timeout=8) == []
    assert calls == [
        (("/directory",), {"user": "root", "request_timeout": 7}),
        (("/directory",), {"depth": 2, "user": "root", "request_timeout": 8}),
    ]


def test_listing_a_symlink_to_a_directory_lists_its_contents(local_files, tmp_path):
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "child").touch()
    link = tmp_path / "link"
    link.symlink_to(directory, target_is_directory=True)
    assert [entry.name for entry in local_files.list(str(link), user="root")] == ["child"]
