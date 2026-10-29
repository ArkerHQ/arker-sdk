"""E2B Git helper semantics with only the Sandbox import changed."""

import asyncio
import os

import pytest
from compat_evidence import record_scenario
from compat_program import async_program

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
    sandbox.git.delete_branch(checkout, "feature")
    assert sandbox.git.branches(checkout).branches == ["main"]
    sandbox.git.set_config("compat.value", "value with spaces", scope="local", path=checkout)
    assert sandbox.git.get_config("compat.value", scope="local", path=checkout) == "value with spaces"
    assert sandbox.git.get_config("compat.missing", scope="local", path=checkout) is None
    sandbox.git.remote_add(checkout, "backup", remote)
    assert sandbox.git.remote_get(checkout, "backup") == remote
    assert sandbox.git.remote_get(checkout, "missing") is None
    sandbox.git.push(checkout, remote="origin", branch="main")
    peer = root + "/peer"
    sandbox.git.clone(remote, peer)
    sandbox.git.configure_user("Peer", "peer@arker.invalid", scope="local", path=peer)
    sandbox.files.write(peer + "/hello.txt", "updated\n")
    sandbox.git.add(peer, files=["hello.txt"])
    sandbox.git.commit(peer, "update")
    sandbox.git.push(peer, remote="origin", branch="main")
    sandbox.git.pull(checkout, remote="origin", branch="main")
    assert sandbox.files.read(checkout + "/hello.txt") == "updated\n"
    sandbox.files.write(checkout + "/hello.txt", "dirty\n")
    sandbox.git.add(checkout, files=["hello.txt"])
    sandbox.git.reset(checkout, paths=["hello.txt"])
    assert sandbox.files.read(checkout + "/hello.txt") == "dirty\n"
    sandbox.git.restore(checkout, paths=["hello.txt"], worktree=True)
    assert sandbox.files.read(checkout + "/hello.txt") == "updated\n"
    assert sandbox.git.status(checkout).is_clean
    sandbox.git.dangerously_authenticate("compat-user", "synthetic-token", host="compat.invalid")
    assert sandbox.git.get_config("credential.helper") == "store"
    assert sandbox.files.read("/home/user/.git-credentials") == "https://compat-user:synthetic-token@compat.invalid\n"
finally:
    sandbox.kill()
"""


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("provider,shim", [("e2b", False), ("e2b", True), ("arker", True)])
def test_git_import_only(provider, shim, live_router, monkeypatch, asynchronous):
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
        "async" if asynchronous else "sync",
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
            "Sandbox.git.delete_branch",
            "Sandbox.git.get_config",
            "Sandbox.git.set_config",
            "Sandbox.git.remote_add",
            "Sandbox.git.remote_get",
            "Sandbox.git.push",
            "Sandbox.git.pull",
            "Sandbox.git.reset",
            "Sandbox.git.restore",
            "Sandbox.git.dangerously_authenticate",
            "Sandbox.kill",
        ],
    ):
        if asynchronous:
            source = async_program(source, rename={"Sandbox": "AsyncSandbox"}, targets={"AsyncSandbox", "sandbox"})
        namespace = {}
        exec(compile(source, "e2b_git_import_only.py", "exec"), namespace)
        if asynchronous:
            asyncio.run(namespace["program"]())
