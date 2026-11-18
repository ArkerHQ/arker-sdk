"""Live tests for the Daytona compatibility layer: common Daytona code,
unchanged except for its imports, against a real deployment.

Run with:

    ARKER_API_KEY=... python -m pytest tests/e2e/test_daytona_e2e.py
"""

from __future__ import annotations

import asyncio
import io
import os
import time
import urllib.request
import uuid

import pytest

from arker import Arker
from arker.daytona import (
    CodeRunParams,
    CreateSandboxFromImageParams,
    CreateSandboxFromSnapshotParams,
    Daytona,
    DaytonaError,
    DaytonaNotFoundError,
    DaytonaNotSupportedError,
    DaytonaTimeoutError,
    FileDownloadRequest,
    FileUpload,
    PtySize,
    Resources,
    SessionExecuteRequest,
    VolumeMount,
)

if not os.environ.get("ARKER_API_KEY"):
    pytest.skip("live Arker credentials are not configured", allow_module_level=True)

if not os.environ.get("ARKER_BASE_URL"):
    os.environ.setdefault("ARKER_PROVIDER", "aws")
    os.environ.setdefault("ARKER_REGION", "us-east-1")


@pytest.fixture(scope="module")
def daytona() -> Daytona:
    return Daytona()


@pytest.fixture(scope="module")
def sandbox(daytona: Daytona):
    sandbox = daytona.create(CreateSandboxFromSnapshotParams(env_vars={"SANDBOX_VAR": "from-create"}, public=True))
    yield sandbox
    daytona.delete(sandbox)


def test_is_an_arker_vm(sandbox) -> None:
    assert Arker().get_vm(sandbox.id).vm_id == sandbox.id
    assert sandbox.arker.run("echo native").stdout == "native\n"
    with pytest.raises(DaytonaNotSupportedError, match=r"Sandbox\.computer_use is not supported"):
        sandbox.computer_use.start()


def test_exec(sandbox) -> None:
    response = sandbox.process.exec(
        "echo $SANDBOX_VAR $RUN_VAR $(pwd); echo oops >&2; exit 3", cwd="/tmp", env={"RUN_VAR": "x"}
    )
    assert response.exit_code == 3
    assert response.result == "from-create x /tmp\noops\n"
    with pytest.raises(DaytonaTimeoutError):
        sandbox.process.exec("sleep 5", timeout=1)


def test_code_run(sandbox) -> None:
    response = sandbox.process.code_run("import sys; print(sys.argv[1:])", params=CodeRunParams(argv=["a", "b"]))
    assert response.result == "['a', 'b']\n"


def test_filesystem(sandbox) -> None:
    sandbox.fs.create_folder("/tmp/daytona", "755")
    sandbox.fs.upload_file(b"hello", "/tmp/daytona/a.txt")
    assert sandbox.fs.download_file("/tmp/daytona/a.txt") == b"hello"
    sandbox.fs.move_files("/tmp/daytona/a.txt", "/tmp/daytona/b.txt")
    assert [(f.name, f.is_dir, f.size) for f in sandbox.fs.list_files("/tmp/daytona")] == [("b.txt", False, 5)]
    sandbox.fs.delete_file("/tmp/daytona", recursive=True)
    with pytest.raises(DaytonaNotFoundError):
        sandbox.fs.download_file("/tmp/daytona/b.txt")


def test_preview_link_serves_a_port(daytona: Daytona, sandbox) -> None:
    sandbox.fs.upload_file(b"served", "/tmp/www/index.html")
    sandbox.process.exec("cd /tmp/www && (nohup python3 -m http.server 3000 >/dev/null 2>&1 &)")
    url = daytona.get(sandbox.id).get_preview_link(3000).url + "/index.html"
    for _ in range(20):
        try:
            assert urllib.request.urlopen(url, timeout=10).read() == b"served"
            return
        except OSError:
            time.sleep(1)
    pytest.fail(f"{url} never served")


def test_image_resources_and_language(daytona: Daytona) -> None:
    sandbox = daytona.create(
        CreateSandboxFromImageParams(image="node:22-slim", language="typescript", resources=Resources(cpu=2))
    )
    try:
        assert sandbox.process.exec("nproc").result == "2\n"
        response = sandbox.process.code_run("const n: number = 2; console.log(n * 21)")
        assert response.result == "42\n"
    finally:
        sandbox.delete()


def test_unknown_snapshot_is_not_found(daytona: Daytona) -> None:
    with pytest.raises(DaytonaNotFoundError):
        daytona.create(CreateSandboxFromSnapshotParams(snapshot="no-such-arker-vm"))


def test_network_allow_list(daytona: Daytona, reachable) -> None:
    sandbox = daytona.create(CreateSandboxFromSnapshotParams(network_allow_list="1.1.1.1/32"))
    try:
        run = lambda cmd: sandbox.process.exec(cmd).result
        assert reachable(run, "https://1.1.1.1", "https://example.com") == {
            "https://1.1.1.1": True,
            "https://example.com": False,
        }
    finally:
        sandbox.delete()


def test_git(sandbox) -> None:
    git = sandbox.git
    sandbox.process.exec("git init --bare --initial-branch main /tmp/origin.git")
    git.init("repo", initial_branch="main")
    git.configure_user("Arker E2E", "e2e@arker.ai")
    sandbox.fs.upload_file(b"one", "/root/repo/a.txt")
    status = git.status("repo")
    assert status.current_branch == "main"
    assert [(f.name, f.staging, f.worktree) for f in status.file_status] == [("a.txt", "Untracked", "Untracked")]
    git.add("repo", ["a.txt"])
    assert git.status("repo").file_status[0].staging == "Added"
    commit = git.commit("repo", "first", "Arker E2E", "e2e@arker.ai")
    assert sandbox.process.exec("git -C repo rev-parse HEAD").result.strip() == commit.sha
    git.remote_add("repo", "origin", "/tmp/origin.git")
    assert [(r.name, r.url) for r in git.remotes("repo").remotes] == [("origin", "/tmp/origin.git")]
    git.push("repo", set_upstream=True)
    assert git.status("repo").branch_published
    git.create_branch("repo", "feature")
    assert (git.branches("repo").branches, git.branches("repo").current) == (["feature", "main"], "feature")
    git.checkout_branch("repo", "main")
    git.delete_branch("repo", "feature")

    git.clone("/tmp/origin.git", "copy")
    sandbox.fs.upload_file(b"two", "/root/copy/b.txt")
    git.add("copy", ["b.txt"])
    second = git.commit("copy", "second", "Other", "other@arker.ai")
    git.push("copy")
    git.pull("repo")
    assert sandbox.fs.download_file("/root/repo/b.txt") == b"two"
    git.clone("/tmp/origin.git", "pinned", commit_id=commit.sha)
    assert sandbox.process.exec("git -C pinned rev-parse HEAD").result.strip() == commit.sha != second.sha
    git.reset("repo", mode="hard", target="HEAD~1")
    assert sandbox.process.exec("test -e repo/b.txt").exit_code == 1
    with pytest.raises(DaytonaError):
        git.checkout_branch("repo", "missing")


def test_code_interpreter(sandbox) -> None:
    interpreter = sandbox.code_interpreter
    interpreter.run_code("counter = 40")
    printed: list[str] = []
    result = interpreter.run_code("print(counter + 2)", on_stdout=lambda message: printed.append(message.output))
    assert (result.stdout, result.error, printed) == ("42\n", None, ["42\n"])
    failed = interpreter.run_code("raise ValueError('bad')")
    assert (failed.error.name, failed.error.value) == ("ValueError", "bad")
    context = interpreter.create_context(cwd="/tmp")
    assert interpreter.run_code("import os\nprint('counter' in globals(), os.getcwd())", context=context).stdout == (
        "False /tmp\n"
    )
    assert [listed.id for listed in interpreter.list_contexts()] == [context.id]
    with pytest.raises(DaytonaTimeoutError):
        interpreter.run_code("import time\ntime.sleep(30)", context=context, timeout=2)
    interpreter.delete_context(context)
    assert interpreter.list_contexts() == []


def test_volumes(daytona: Daytona) -> None:
    volume = daytona.volume.get(f"e2e-{uuid.uuid4().hex[:12]}", create=True)
    try:
        assert volume.id in [listed.id for listed in daytona.volume.list()]
        writer = daytona.create(CreateSandboxFromSnapshotParams(volumes=[VolumeMount(volume.id, "/mnt/data")]))
        try:
            writer.fs.upload_file(b"shared", "/mnt/data/note.txt")
        finally:
            writer.delete()
        reader = daytona.create(CreateSandboxFromSnapshotParams(volumes=[VolumeMount(volume.name, "/data")]))
        try:
            assert reader.fs.download_file("/data/note.txt") == b"shared"
        finally:
            reader.delete()
    finally:
        daytona.volume.delete(volume)
    with pytest.raises(DaytonaNotFoundError):
        daytona.volume.delete(volume)


def test_sessions(sandbox) -> None:
    process = sandbox.process
    process.create_session("work")
    with pytest.raises(DaytonaError):
        process.create_session("work")
    first = process.execute_session_command("work", SessionExecuteRequest(command="cd /tmp && export STEP=one"))
    assert first.exit_code == 0
    second = process.execute_session_command("work", SessionExecuteRequest(command="echo $STEP $(pwd) $SANDBOX_VAR"))
    assert (second.stdout, second.exit_code) == ("one /tmp from-create\n", 0)
    failed = process.execute_session_command("work", SessionExecuteRequest(command="echo bad >&2; false"))
    assert (failed.stderr, failed.output, failed.exit_code) == ("bad\n", "bad\n", 1)
    session = process.get_session("work")
    assert [(c.command, c.exit_code) for c in session.commands] == [
        ("cd /tmp && export STEP=one", 0),
        ("echo $STEP $(pwd) $SANDBOX_VAR", 0),
        ("echo bad >&2; false", 1),
    ]
    assert process.get_session_command("work", second.cmd_id).command == "echo $STEP $(pwd) $SANDBOX_VAR"
    assert "work" in [s.session_id for s in process.list_sessions()]
    process.delete_session("work")
    assert "work" not in [s.session_id for s in process.list_sessions()]
    with pytest.raises(DaytonaNotFoundError):
        process.get_session("work")


def test_async_session_command_input_and_logs(sandbox) -> None:
    process = sandbox.process
    process.create_session("async")
    started = process.execute_session_command(
        "async", SessionExecuteRequest(command="read name; echo hi $name; echo warn >&2; sleep 1", run_async=True)
    )
    assert started.exit_code is None
    process.send_session_command_input("async", started.cmd_id, "arker\n")
    stdout: list[str] = []
    stderr: list[str] = []
    asyncio.run(process.get_session_command_logs_async("async", started.cmd_id, stdout.append, stderr.append))
    assert ("".join(stdout), "".join(stderr)) == ("hi arker\n", "warn\n")
    logs = process.get_session_command_logs("async", started.cmd_id)
    assert (logs.stdout, logs.stderr) == ("hi arker\n", "warn\n")
    assert process.get_session_command("async", started.cmd_id).exit_code == 0
    with pytest.raises(DaytonaError):
        process.send_session_command_input("async", started.cmd_id, "late\n")
    process.delete_session("async")


def test_pty_session(sandbox) -> None:
    pty = sandbox.process.create_pty_session("term", cwd="/tmp", envs={"GREETING": "hi"}, pty_size=PtySize(24, 80))
    pty.wait_for_connection()
    sandbox.process.resize_pty_session("term", PtySize(rows=30, cols=111))
    info = sandbox.process.get_pty_session_info("term")
    assert (info.id, info.cwd, info.envs, info.cols, info.rows) == ("term", "/tmp", {"GREETING": "hi"}, 111, 30)
    assert [listed.id for listed in sandbox.process.list_pty_sessions()] == ["term"]
    pty.send_input("echo $GREETING $(pwd) $(tput cols); exit 4\n")
    output = bytearray()
    result = pty.wait(on_data=output.extend)
    assert result.exit_code == 4
    assert b"hi /tmp 111" in output
    other = sandbox.process.create_pty_session("other")
    assert sorted(listed.id for listed in sandbox.process.list_pty_sessions()) == ["other"]
    sandbox.process.kill_pty_session("other")
    assert other.wait().exit_code is None
    assert sandbox.process.list_pty_sessions() == []
    with pytest.raises(DaytonaNotFoundError):
        sandbox.process.get_pty_session_info("other")
    with pytest.raises(DaytonaNotFoundError):
        sandbox.process.connect_pty_session("other")


def test_filesystem_extras(sandbox, tmp_path) -> None:
    fs = sandbox.fs
    local = tmp_path / "local.txt"
    local.write_text("from disk")
    fs.upload_files([FileUpload(b"alpha TODO one\n", "/tmp/fsx/a.py"), FileUpload(str(local), "/tmp/fsx/sub/b.txt")])
    fs.upload_file_stream(io.BytesIO(b"TODO streamed\n"), "/tmp/fsx/sub/c.py")
    assert b"".join(fs.download_file_stream("/tmp/fsx/sub/c.py")) == b"TODO streamed\n"
    info = fs.get_file_info("/tmp/fsx/a.py")
    assert (info.name, info.is_dir, info.size, info.owner) == ("a.py", False, 15, "root")
    fs.set_file_permissions("/tmp/fsx/a.py", mode="600")
    assert (fs.get_file_info("/tmp/fsx/a.py").permissions, fs.get_file_info("/tmp/fsx/a.py").mode) == (
        "0600",
        "-rw-------",
    )
    assert fs.get_file_info("/tmp/fsx/sub").is_dir
    assert sorted(f.name for f in fs.list_files("/tmp/fsx", depth=2)) == ["a.py", "b.txt", "c.py", "sub"]
    assert sorted(f.name for f in fs.list_files("/tmp/fsx")) == ["a.py", "sub"]
    matches = sorted((m.file, m.line, m.content) for m in fs.find_files("/tmp/fsx", "TODO"))
    assert matches == [("/tmp/fsx/a.py", 1, "alpha TODO one"), ("/tmp/fsx/sub/c.py", 1, "TODO streamed")]
    assert sorted(fs.search_files("/tmp/fsx", "*.py").files) == ["/tmp/fsx/a.py", "/tmp/fsx/sub/c.py"]
    results = fs.replace_in_files(["/tmp/fsx/a.py", "/tmp/fsx/missing.py"], "TODO", "DONE")
    assert [(r.file, r.success) for r in results] == [("/tmp/fsx/a.py", True), ("/tmp/fsx/missing.py", False)]
    assert fs.download_file("/tmp/fsx/a.py") == b"alpha DONE one\n"
    downloads = fs.download_files(
        [FileDownloadRequest("/tmp/fsx/sub/b.txt"), FileDownloadRequest("/tmp/fsx/nope", str(tmp_path / "n"))]
    )
    assert downloads[0].result == b"from disk" and downloads[1].error
    with pytest.raises(DaytonaNotFoundError):
        fs.get_file_info("/tmp/fsx/nope")
