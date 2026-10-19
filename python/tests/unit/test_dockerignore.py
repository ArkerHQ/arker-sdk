"""Tests for `.dockerignore` matching."""

from __future__ import annotations

from arker.dockerignore import load_dockerignore


def write(tmp_path, content: str):
    (tmp_path / ".dockerignore").write_text(content)
    return load_dockerignore(str(tmp_path))


def test_no_dockerignore_excludes_nothing(tmp_path):
    ig = load_dockerignore(str(tmp_path))
    assert not ig.ignores("anything")
    assert not ig.ignores("a/b/c.txt")


def test_comments_and_blank_lines_are_skipped(tmp_path):
    ig = write(tmp_path, "# a comment\n\n   \nnode_modules\n")
    assert ig.ignores("node_modules")
    assert not ig.ignores("src")


def test_a_directory_pattern_excludes_everything_under_it(tmp_path):
    """A bare `node_modules` must exclude its contents, which is what everyone
    writing that line means."""
    ig = write(tmp_path, "node_modules\n.git\n")
    assert ig.ignores("node_modules")
    assert ig.ignores("node_modules/left-pad/index.js")
    assert ig.ignores(".git/config")
    assert not ig.ignores("src/node_modules_helper.js")


def test_star_does_not_cross_a_separator(tmp_path):
    ig = write(tmp_path, "*.log\n")
    assert ig.ignores("debug.log")
    assert not ig.ignores("logs/debug.log"), "* must not match across /"


def test_double_star_crosses_separators(tmp_path):
    ig = write(tmp_path, "**/*.log\n")
    assert ig.ignores("debug.log"), "**/ must also match zero directories"
    assert ig.ignores("logs/debug.log")
    assert ig.ignores("a/b/c/debug.log")


def test_question_mark_matches_one_character(tmp_path):
    ig = write(tmp_path, "file?.txt\n")
    assert ig.ignores("file1.txt")
    assert not ig.ignores("file10.txt")


def test_a_leading_slash_is_context_relative(tmp_path):
    ig = write(tmp_path, "/build\n")
    assert ig.ignores("build")
    assert ig.ignores("build/out.js")


def test_negation_and_last_match_wins(tmp_path):
    """`*` then `!keep.txt` is the canonical allowlist shape, and it only works
    because the last matching rule decides."""
    ig = write(tmp_path, "*\n!keep.txt\n")
    assert ig.ignores("secret.env")
    assert not ig.ignores("keep.txt")


def test_negation_order_matters(tmp_path):
    """Reversed, the exclusion wins - same rules, opposite outcome."""
    ig = write(tmp_path, "!keep.txt\n*\n")
    assert ig.ignores("keep.txt")


def test_the_dockerfile_itself_can_be_ignored_without_breaking_anything(tmp_path):
    """Ignoring the Dockerfile affects COPY but not Dockerfile parsing."""
    ig = write(tmp_path, "Dockerfile\n.dockerignore\n")
    assert ig.ignores("Dockerfile")
    assert ig.ignores(".dockerignore")


def test_a_bare_name_does_not_match_at_depth(tmp_path):
    """A bare name is anchored to the build-context root."""
    ig = write(tmp_path, "abc\n")
    assert ig.ignores("abc")
    assert ig.ignores("abc/inner.txt")
    assert not ig.ignores("somedir/abc"), "a bare name must not match at depth"
    assert not ig.ignores("somedir/abc/inner.txt")


def test_a_subdirectory_can_be_re_added_under_an_ignored_parent(tmp_path):
    """Docker allows this where git does not: git never walks into an ignored
    subtree, so it cannot re-add from within one. Docker can."""
    ig = write(tmp_path, "node_modules\n!node_modules/keep\n")
    assert ig.ignores("node_modules/left-pad/index.js")
    assert not ig.ignores("node_modules/keep")
    assert not ig.ignores("node_modules/keep/index.js")


def test_trailing_slashes_are_equivalent(tmp_path):
    """`/a`, `a/` and `/a/` all mean `a`."""
    for spelling in ("/build", "build/", "/build/"):
        ig = write(tmp_path, f"{spelling}\n")
        assert ig.ignores("build"), spelling
        assert ig.ignores("build/out.js"), spelling
