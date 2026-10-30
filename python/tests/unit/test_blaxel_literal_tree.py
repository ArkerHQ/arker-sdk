import json

from test_blaxel_filesystem import filesystem as filesystem


def test_native_tree_keeps_literal_destination_and_file_content(filesystem):
    fs, calls, response = filesystem
    response.clear()
    response.update(name="tree", path="/~/tree", files=[], subdirectories=[])
    tree = fs.write_tree([{"path": "file", "content": "value"}], "~/tree")
    assert calls[0].url.path == "/port/49983/filesystem/tree/~/tree"
    assert json.loads(calls[0].content) == {"files": {"file": "value"}}
    assert tree.path == "/~/tree"
