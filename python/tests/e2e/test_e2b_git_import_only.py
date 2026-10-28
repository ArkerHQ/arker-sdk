"""E2B Git helper semantics with only the Sandbox import changed."""

import os

import pytest
from compat_evidence import record_scenario

PROGRAM = r"""
from e2b import Sandbox

sandbox = Sandbox.create()
root = "/tmp/compat-e2b-git"
remote = root + "/remote.git"
checkout = root + "/checkout"
try:
    sandbox.commands.run("rm -rf " + root)
    sandbox.git.init(remote, bare=True, initial_branch="main")
    sandbox.git.clone(remote, checkout)
    sandbox.git.configure_user("Compatibility Test", "compat@arker.invalid", scope="local", path=checkout)
    sandbox.files.write(checkout + "/hello.txt", "hello\n")
    sandbox.git.add(checkout, files=["hello.txt"])
    sandbox.git.commit(checkout, "initial")
    assert sandbox.git.status(checkout).is_clean
    sandbox.git.create_branch(checkout, "feature")
    branches = sandbox.git.branches(checkout)
    assert branches.current_branch == "feature"
    assert set(branches.branches) == {"feature", "main"}
    sandbox.git.checkout_branch(checkout, "main")
    assert sandbox.git.status(checkout).current_branch == "main"
finally:
    sandbox.kill()
"""


@pytest.mark.parametrize("provider,shim", [("e2b", False), ("e2b", True), ("arker", True)])
def test_git_import_only(provider, shim, live_router, monkeypatch):
    pytest.importorskip("e2b")
    if (provider == "e2b" and not os.environ.get("E2B_API_KEY")) or (
        provider == "arker" and not os.environ.get("ARKER_API_KEY")
    ):
        pytest.skip("live credentials required")
    if provider == "arker":
        monkeypatch.delenv("E2B_API_KEY", raising=False)
    source = PROGRAM.replace("from e2b import Sandbox", "from arker.e2b import Sandbox") if shim else PROGRAM
    with record_scenario(
        "e2b",
        "sync",
        provider,
        shim,
        "import-only-local-git-workflow",
        [
            "Sandbox.create",
            "Sandbox.git.init",
            "Sandbox.git.clone",
            "Sandbox.git.configure_user",
            "Sandbox.git.add",
            "Sandbox.git.commit",
            "Sandbox.git.status",
            "Sandbox.git.create_branch",
            "Sandbox.git.branches",
            "Sandbox.git.checkout_branch",
            "Sandbox.kill",
        ],
    ):
        exec(compile(source, "e2b_git_import_only.py", "exec"), {})
