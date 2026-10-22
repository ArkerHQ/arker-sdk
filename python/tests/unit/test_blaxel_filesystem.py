from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from test_compat_process import LocalVM

from arker.blaxel import FileSystem, _AsyncComponent


@pytest.fixture
def filesystem(tmp_path):
    vm = LocalVM(tmp_path)
    vm.sync = lambda path: Path(path).read_bytes()
    return FileSystem(vm=vm)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_grep_context_with_literal_filenames(filesystem, tmp_path, asynchronous):
    path = tmp_path / "line\nbreak:λ.txt"
    path.write_text("before\nA Needle here\nafter\nlast needle\n")

    async def search():
        return await asyncio.wait_for(
            _AsyncComponent(filesystem).grep("needle", str(tmp_path), context_lines=1), timeout=10
        )

    response = asyncio.run(search()) if asynchronous else filesystem.grep("needle", str(tmp_path), context_lines=1)
    assert response.to_dict() == {
        "query": "needle",
        "total": 2,
        "matches": [
            {
                "path": path.name,
                "line": 2,
                "column": 3,
                "text": "A Needle here",
                "context": "before\nA Needle here\nafter",
            },
            {"path": path.name, "line": 4, "column": 6, "text": "last needle", "context": "after\nlast needle\n"},
        ],
    }


def test_grep_single_file_filters_and_default_limit(filesystem, tmp_path):
    path = tmp_path / "many.txt"
    path.write_text("Needle\n" * 105)
    assert filesystem.grep("needle", str(tmp_path)).total == 100
    assert filesystem.grep("needle", str(tmp_path), max_results=0).total == 0
    assert filesystem.grep("needle", str(tmp_path), case_sensitive=True).total == 0
    match = filesystem.grep("Needle", str(tmp_path), max_results=1).matches[0]
    assert match.to_dict() == {"path": path.name, "line": 1, "column": 1, "text": "Needle"}
    (tmp_path / "binary").write_bytes(b"needle\0binary")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "probe.txt").write_text("needle")
    assert filesystem.grep("needle", str(tmp_path), exclude_dirs=["ignored"]).total == 100
    assert filesystem.grep("needle", str(tmp_path), file_pattern="*.py").total == 0


def test_grep_invalid_context_and_missing_path(filesystem, tmp_path):
    with pytest.raises(ValueError, match="context_lines"):
        filesystem.grep("needle", str(tmp_path), context_lines=-1)
    with pytest.raises(RuntimeError):
        filesystem.grep("needle", str(tmp_path / "missing"))
    file = tmp_path / "single-file"
    file.write_text("needle")
    with pytest.raises(RuntimeError):
        filesystem.grep("needle", str(file))


def test_grep_without_optional_sdk(filesystem, tmp_path, monkeypatch):
    monkeypatch.setattr("arker.blaxel.import_optional", lambda _: None)
    (tmp_path / "text").write_text("before\nneedle\nafter")
    response = filesystem.grep("needle", str(tmp_path), context_lines=1)
    assert response.to_dict()["matches"][0]["context"] == "before\nneedle\nafter"
