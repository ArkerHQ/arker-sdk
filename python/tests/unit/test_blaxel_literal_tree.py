from arker.blaxel import FileSystem


def test_tree_destination_keeps_literal_tilde(monkeypatch):
    fs = FileSystem(vm=object())
    writes = []
    monkeypatch.setattr(fs, "write", lambda path, content: writes.append((path, content)))
    monkeypatch.setattr(fs, "ls", lambda path: path)
    assert fs.write_tree([{"path": "file", "content": "value"}], "~/tree") == "/~/tree"
    assert writes == [("/~/tree/file", "value")]
