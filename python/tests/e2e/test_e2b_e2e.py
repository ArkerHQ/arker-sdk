"""Live tests for the E2B compatibility layer: common E2B code, unchanged
except for its imports, against a real deployment.

Run with:

    ARKER_API_KEY=... python -m pytest tests/e2e/test_e2b_e2e.py
"""

from __future__ import annotations

import os
import time
import urllib.request
import uuid

import pytest

from arker import Arker, e2b_code_interpreter
from arker.e2b import (
    ALL_TRAFFIC,
    CommandExitException,
    FileNotFoundException,
    FilesystemEventType,
    FileType,
    GitAuthException,
    GitBranches,
    InvalidArgumentException,
    NotFoundException,
    NotSupportedException,
    PtySize,
    Sandbox,
    SandboxException,
    SandboxQuery,
    TemplateException,
    TimeoutException,
    Volume,
    VolumeInfo,
    VolumeNotFoundException,
)

if not os.environ.get("ARKER_API_KEY"):
    pytest.skip("live Arker credentials are not configured", allow_module_level=True)

if not os.environ.get("ARKER_BASE_URL"):
    os.environ.setdefault("ARKER_PROVIDER", "aws")
    os.environ.setdefault("ARKER_REGION", "us-east-1")


@pytest.fixture(scope="module")
def sbx():
    with Sandbox.create(envs={"SANDBOX_VAR": "from-create"}, metadata={"suite": "e2e"}) as sandbox:
        yield sandbox


def test_is_an_arker_vm(sbx: Sandbox) -> None:
    assert Arker().get_vm(sbx.sandbox_id).vm_id == sbx.sandbox_id
    assert sbx.arker.run("echo native").stdout == "native\n"


def test_what_is_missing_says_so(sbx: Sandbox) -> None:
    with pytest.raises(NotSupportedException, match=r"(?s)Sandbox\.get_mcp_url is not supported.*\.arker"):
        sbx.get_mcp_url()


def test_default_image_has_python_and_node(sbx: Sandbox) -> None:
    result = sbx.commands.run("pip --version && node --version && pwd")
    assert result.stdout.strip().endswith("/home/user")


def test_run_options(sbx: Sandbox) -> None:
    seen: list[str] = []
    result = sbx.commands.run(
        "echo $SANDBOX_VAR $CMD_VAR $(pwd)",
        envs={"CMD_VAR": "from-run"},
        cwd="/tmp",
        on_stdout=seen.append,
    )
    assert result.stdout == "from-create from-run /tmp\n"
    assert "".join(seen) == result.stdout
    assert sbx.commands.run("pwd").stdout == "/home/user\n"


def test_failures_raise_like_e2b(sbx: Sandbox) -> None:
    with pytest.raises(CommandExitException) as exit_error:
        sbx.commands.run("echo oops >&2; exit 3")
    assert exit_error.value.exit_code == 3
    assert exit_error.value.stderr == "oops\n"
    with pytest.raises(TimeoutException):
        sbx.commands.run("sleep 5", timeout=1)


def test_background_command(sbx: Sandbox) -> None:
    handle = sbx.commands.run("sleep 1; echo bg-done", background=True)
    assert sbx.commands.run("echo meanwhile").stdout == "meanwhile\n"
    assert handle.wait().stdout == "bg-done\n"
    sleeper = sbx.commands.run("sleep 600", background=True, timeout=0)
    assert sleeper.kill()


def test_files(sbx: Sandbox) -> None:
    info = sbx.files.write("project/main.py", "print('hi')\n")
    assert info.path == "/home/user/project/main.py"
    sbx.files.write_files([{"path": "/tmp/e2b-a.bin", "data": b"\x00\x01"}])
    assert sbx.files.read("/tmp/e2b-a.bin", format="bytes") == bytearray(b"\x00\x01")
    assert sbx.commands.run("python3 project/main.py").stdout == "hi\n"
    entries = {entry.name: entry.type for entry in sbx.files.list("/home/user")}
    assert entries["project"] == FileType.DIR
    assert "main.py" not in entries, "list() defaults to one level deep"
    assert sbx.files.make_dir("/tmp/e2b-dir") is True
    assert sbx.files.make_dir("/tmp/e2b-dir") is False
    sbx.files.rename("/tmp/e2b-dir", "/tmp/e2b-moved")
    assert sbx.files.exists("/tmp/e2b-moved")
    sbx.files.remove("/tmp/e2b-moved")
    assert not sbx.files.exists("/tmp/e2b-moved")
    with pytest.raises(FileNotFoundException):
        sbx.files.read("/tmp/e2b-missing")


def test_get_host_serves_a_port(sbx: Sandbox) -> None:
    sbx.files.write("/tmp/www/index.html", "served")
    sbx.commands.run("cd /tmp/www && python3 -m http.server 8000", background=True, timeout=0)
    url = f"https://{sbx.get_host(8000)}/index.html"
    for _ in range(20):
        try:
            assert urllib.request.urlopen(url, timeout=10).read() == b"served"
            return
        except OSError:
            time.sleep(1)
    pytest.fail(f"{url} never served")


def test_info_reconnect_and_lifecycle(sbx: Sandbox) -> None:
    info = sbx.get_info()
    assert info.metadata == {"suite": "e2e"}
    assert info.template_id
    again = Sandbox.connect(sbx.sandbox_id, timeout=600)
    assert again.commands.run("echo $SANDBOX_VAR").stdout == "from-create\n"
    assert again.is_running()
    assert again.pause()


def test_timeout_deletes_the_sandbox() -> None:
    sandbox = Sandbox.create(timeout=5)
    deadline = time.monotonic() + 120
    while sandbox.is_running():
        assert time.monotonic() < deadline, "sandbox outlived its timeout"
        time.sleep(5)


def test_unknown_template_is_a_clear_error() -> None:
    with pytest.raises(TemplateException, match="No Arker VM named"):
        Sandbox.create("my-custom-e2b-template")


def test_create_options() -> None:
    with Sandbox.create(cpu_count=4, allow_internet_access=False) as sandbox:
        assert sandbox.commands.run("nproc").stdout == "4\n"
        with pytest.raises(CommandExitException):
            sandbox.commands.run("curl -sS -m 10 https://example.com")


def test_e2b_doc_patterns(sbx: Sandbox) -> None:
    assert sbx.commands.run("echo $E2B_SANDBOX_ID").stdout == f"{sbx.sandbox_id}\n"
    handle = sbx.commands.run("echo out; echo err >&2", background=True)
    chunks = list(handle)
    assert ("out\n", None, None) in chunks and (None, "err\n", None) in chunks
    assert sbx.connect() is sbx
    listed = Sandbox.list(query=SandboxQuery(metadata={"suite": "e2e"})).next_items()
    assert sbx.sandbox_id in [info.sandbox_id for info in listed]


def test_kill_by_id() -> None:
    sandbox = Sandbox.create()
    assert Sandbox.kill(sandbox.sandbox_id) is True
    assert not sandbox.is_running()
    assert sandbox.kill() is False


def test_two_ports(sbx: Sandbox) -> None:
    assert sbx.get_host(3000) != sbx.get_host(3001)


def test_every_reader_sees_all_output(sbx: Sandbox) -> None:
    text = "é€😀" * 400
    handle = sbx.commands.run(f"echo out1; printf '{text}'; echo err1 >&2; sleep 1; echo out2", background=True)
    assert handle.exit_code is None
    seen: list[str] = []
    assert handle.wait(on_stdout=seen.append).stdout == f"out1\n{text}out2\n"
    assert "".join(seen) == f"out1\n{text}out2\n"
    assert "".join(out for out, _, _ in handle if out) == f"out1\n{text}out2\n"


def test_background_commands_leave_no_sessions(sbx: Sandbox) -> None:
    vm = sbx._sandbox.vm
    before = len(vm.list_sessions().sessions)
    for _ in range(3):
        sbx.commands.run("true", background=True).wait()
    sleeper = sbx.commands.run("sleep 300", background=True, timeout=0)
    assert sleeper.kill()
    assert len(vm.list_sessions().sessions) == before
    assert sbx.commands.run("pgrep -x sleep || true").stdout == ""


def test_reconnect_keeps_pause_on_timeout() -> None:
    sandbox = Sandbox.create(lifecycle={"on_timeout": "pause"}, timeout=600)
    try:
        Sandbox.connect(sandbox.sandbox_id, timeout=600)
        rules = Arker().get_vm(sandbox.sandbox_id).policies.rules
        assert rules["compat-timeout"].action == "suspend"
    finally:
        sandbox.kill()


def test_network_allow_and_deny_lists(reachable) -> None:
    urls = (
        "https://example.com",
        "https://www.example.com",
        "https://github.com",
        "https://1.1.1.1",
        "https://8.8.8.8",
    )
    allow = {"allow_out": ["example.com", "1.1.1.1"], "deny_out": [ALL_TRAFFIC]}
    with Sandbox.create(network=allow, timeout=120) as sandbox:
        # Allowed names resolve with no nameserver allowed, and none is.
        assert reachable(lambda cmd: sandbox.commands.run(cmd).stdout, *urls) == {
            "https://example.com": True,
            "https://www.example.com": False,
            "https://github.com": False,
            "https://1.1.1.1": True,
            "https://8.8.8.8": False,
        }
    # An allow beats a deny, and what neither names stays reachable.
    precedence = {"allow_out": ["1.1.1.1"], "deny_out": ["1.0.0.0/8"]}
    with Sandbox.create(network=precedence, timeout=120) as sandbox:
        assert reachable(
            lambda cmd: sandbox.commands.run(cmd).stdout, "https://1.1.1.1", "https://1.0.0.1", "https://example.com"
        ) == {
            "https://1.1.1.1": True,
            "https://1.0.0.1": False,
            "https://example.com": True,
        }
    with pytest.raises(InvalidArgumentException, match="ALL_TRAFFIC"):
        Sandbox.create(network={"allow_out": ["example.com"]})


def test_git(sbx: Sandbox) -> None:
    git = sbx.git
    sbx.commands.run("git init --bare --initial-branch main /tmp/origin.git")
    git.init("repo", initial_branch="main")
    git.configure_user("Arker E2E", "e2e@arker.ai")
    assert git.get_config("user.name") == "Arker E2E"
    sbx.files.write("repo/a.txt", "one")
    status = git.status("repo")
    assert (status.current_branch, status.has_untracked, status.file_status[0].name) == ("main", True, "a.txt")
    git.add("repo")
    assert git.status("repo").staged_count == 1
    git.commit("repo", "first")
    assert git.status("repo").is_clean
    git.remote_add("repo", "origin", "/tmp/origin.git")
    assert git.remote_get("repo", "origin") == "/tmp/origin.git"
    git.push("repo")
    assert git.status("repo").upstream == "origin/main"
    git.create_branch("repo", "feature")
    assert git.branches("repo") == GitBranches(["feature", "main"], "feature")
    git.checkout_branch("repo", "main")
    git.delete_branch("repo", "feature")
    assert git.branches("repo").branches == ["main"]

    git.clone("/tmp/origin.git", "copy")
    sbx.files.write("copy/b.txt", "two")
    git.add("copy", ["b.txt"])
    git.commit("copy", "second", author_name="Other", author_email="other@arker.ai")
    git.push("copy")
    git.pull("repo")
    assert sbx.files.read("repo/b.txt") == "two"
    sbx.files.write("repo/a.txt", "changed")
    assert git.status("repo").file_status[0].status == "modified"
    git.restore("repo", ["a.txt"])
    assert sbx.files.read("repo/a.txt") == "one"
    git.reset("repo", mode="hard", target="HEAD~1")
    assert not sbx.files.exists("repo/b.txt")
    with pytest.raises(CommandExitException):
        git.checkout_branch("repo", "missing")
    with pytest.raises(InvalidArgumentException, match="starts with '-'"):
        git.checkout_branch("repo", "--orphan=x")
    sbx.commands.run("cd repo && git mv a.txt 'new näme.txt' && touch 'tab\there.txt'")
    changes = [(change.name, change.status, change.renamed_from) for change in git.status("repo").file_status]
    assert changes == [("new näme.txt", "renamed", "a.txt"), ("tab\there.txt", "untracked", None)]


def test_git_credentials(sbx: Sandbox) -> None:
    private = "https://github.com/ArkerHQ/no-such-repository-for-e2e"
    with pytest.raises(GitAuthException, match="terminal prompts disabled"):
        sbx.git.clone(private, "/tmp/private")
    # Given credentials, git uses them rather than asking for them.
    with pytest.raises(GitAuthException) as refused:
        sbx.git.clone(private, "/tmp/private", username="someone", password="not-a-token")
    assert "terminal prompts disabled" not in str(refused.value)
    sbx.git.dangerously_authenticate("someone", "not-a-token", host="example.com")
    assert sbx.commands.run("cat /root/.git-credentials").stdout == "https://someone:not-a-token@example.com\n"


def test_volume_mounts() -> None:
    name = f"e2e-{uuid.uuid4().hex[:12]}"
    volume = Volume.create(name)
    try:
        with Sandbox.create(volume_mounts={"/mnt/data": volume}, timeout=120) as writer:
            writer.files.write("/mnt/data/note.txt", "shared")
        with Sandbox.create(volume_mounts={"/data": name}, timeout=120) as reader:
            assert reader.files.read("/data/note.txt") == "shared"
        assert VolumeInfo(volume.volume_id, name) in Volume.list()
        assert Volume.get_info(volume.volume_id).name == name
        with pytest.raises(NotSupportedException, match=r"Volume\.read_file"):
            volume.read_file("/note.txt")
    finally:
        assert Volume.destroy(volume.volume_id)
    with pytest.raises(VolumeNotFoundException):
        Volume.connect(volume.volume_id)


@pytest.fixture(scope="module")
def interpreter():
    with e2b_code_interpreter.Sandbox.create() as sandbox:
        yield sandbox


def test_run_code_keeps_state(interpreter: e2b_code_interpreter.Sandbox) -> None:
    printed: list[str] = []
    interpreter.run_code("x = 40\nprint('hi')", on_stdout=lambda message: printed.append(message.line))
    assert printed == ["hi\n"]
    execution = interpreter.run_code("x + 2")
    assert (execution.text, execution.results[0].is_main_result, execution.error) == ("42", True, None)
    execution = interpreter.run_code("import os\nprint(os.environ['RUN_VAR'], os.getcwd())", envs={"RUN_VAR": "v"})
    assert execution.logs.stdout == ["v /home/user\n"]
    failed = interpreter.run_code("x / 0")
    assert (failed.error.name, failed.error.value) == ("ZeroDivisionError", "division by zero")
    assert "x / 0" in failed.error.traceback
    html = interpreter.run_code("class Bold:\n    def _repr_html_(self):\n        return '<b>x</b>'\nBold()")
    assert html.results[0].html == "<b>x</b>"
    with pytest.raises(NotSupportedException):
        interpreter.run_code("1", language="r")


def test_output_cannot_end_a_run_early(interpreter: e2b_code_interpreter.Sandbox) -> None:
    execution = interpreter.run_code("print('<arker-interpreter-ready>')\n1 + 1")
    assert (execution.logs.stdout, execution.text) == (["<arker-interpreter-ready>\n"], "2")
    assert interpreter.run_code("3").text == "3"


def test_code_contexts(interpreter: e2b_code_interpreter.Sandbox) -> None:
    context = interpreter.create_code_context(cwd="/tmp")
    interpreter.run_code("y = 1", context=context)
    assert interpreter.run_code("import os\n(y, os.getcwd())", context=context).text == "(1, '/tmp')"
    assert interpreter.run_code("'y' in globals()").text == "False"
    assert context.id in [listed.id for listed in interpreter.list_code_contexts()]
    interpreter.restart_code_context(context)
    assert interpreter.run_code("'y' in globals()", context=context).text == "False"
    with pytest.raises(TimeoutException):
        interpreter.run_code("import time\ntime.sleep(30)", context=context, timeout=2)
    assert interpreter.run_code("1 + 1", context=context).text == "2"
    interpreter.remove_code_context(context)
    assert context.id not in [listed.id for listed in interpreter.list_code_contexts()]


def test_run_code_in_javascript_and_typescript(interpreter: e2b_code_interpreter.Sandbox) -> None:
    printed: list[str] = []
    interpreter.run_code("const x = 40; console.log('hi')", language="js", on_stdout=lambda m: printed.append(m.line))
    assert printed == ["hi\n"]
    execution = interpreter.run_code("await new Promise((done) => setTimeout(done, 10)); x + 2", language="javascript")
    assert (execution.text, execution.results[0].is_main_result, execution.error) == ("42", True, None)
    assert interpreter.run_code(
        "process.env.RUN_VAR + ' ' + process.cwd()", language="js", envs={"RUN_VAR": "v"}
    ).text == ("'v /home/user'")
    failed = interpreter.run_code("null.x", language="js")
    assert failed.error.name == "TypeError"
    assert "<cell-" in failed.error.traceback
    typed = interpreter.run_code("const n: number = 2; interface P { n: number }; n * 21", language="ts")
    assert (typed.text, typed.error) == ("42", None)
    # Each language has its own default context, and a context keeps its language.
    assert interpreter.run_code("typeof x", language="ts").text == "'undefined'"
    assert interpreter.run_code("typeof n", language="js").text == "'undefined'"
    context = interpreter.create_code_context(language="js")
    assert context.language == "javascript"
    interpreter.run_code("let kept = 'yes'", context=context)
    assert interpreter.run_code("kept", context=context).text == "'yes'"
    assert {"javascript", "typescript"} <= {listed.language for listed in interpreter.list_code_contexts()}
    interpreter.restart_code_context(context)
    assert interpreter.run_code("typeof kept", context=context).text == "'undefined'"
    with pytest.raises(TimeoutException):
        interpreter.run_code("await new Promise(() => {})", context=context, timeout=2)
    assert interpreter.run_code("typeof process", context=context).text == "'object'"
    interpreter.remove_code_context(context)


def test_run_code_charts(interpreter: e2b_code_interpreter.Sandbox) -> None:
    interpreter.commands.run("python3 -m pip install --quiet matplotlib", timeout=300)
    execution = interpreter.run_code("import matplotlib.pyplot as plt\nplt.plot([1, 2, 3])\nplt.show()")
    assert execution.results[0].png.startswith("iVBORw0KGgo")


def test_reconnecting_keeps_the_default_context(interpreter: e2b_code_interpreter.Sandbox) -> None:
    interpreter.run_code("kept = 'yes'")
    assert e2b_code_interpreter.Sandbox.connect(interpreter.sandbox_id).run_code("kept").text == "'yes'"


def test_processes_by_pid(sbx: Sandbox) -> None:
    handle = sbx.commands.run("sleep 600", background=True, cwd="/tmp", envs={"TAG": "listed"}, timeout=0)
    assert sbx.commands.run(f"cat /proc/{handle.pid}/cmdline").exit_code == 0
    listed = {process.pid: process for process in sbx.commands.list()}
    assert listed[handle.pid].args == ["-l", "-c", "sleep 600"]
    assert listed[handle.pid].cwd == "/tmp"
    assert listed[handle.pid].envs == {"TAG": "listed"}
    assert sbx.commands.kill(handle.pid) is True
    assert handle.pid not in {process.pid for process in sbx.commands.list()}
    assert sbx.commands.kill(handle.pid) is False


def test_connect_to_a_running_command(sbx: Sandbox) -> None:
    handle = sbx.commands.run("echo early; sleep 2; echo late", background=True)
    handle.disconnect()
    assert list(handle) == []
    again = Sandbox.connect(sbx.sandbox_id).commands.connect(handle.pid)
    assert again.wait().stdout == "early\nlate\n"
    with pytest.raises(NotFoundException):
        sbx.commands.connect(handle.pid)


def test_stdin(sbx: Sandbox) -> None:
    handle = sbx.commands.run("read name; echo hello $name; cat", background=True, stdin=True)
    handle.send_stdin("arker\n")
    sbx.commands.send_stdin(handle.pid, b"line 2\n")
    sbx.commands.stream_input(handle.pid, iter(["line ", "3\n"]))
    sbx.commands.close_stdin(handle.pid)
    assert handle.wait().stdout == "hello arker\nline 2\nline 3\n"
    with pytest.raises(InvalidArgumentException, match="stdin"):
        sbx.commands.run("sleep 600", background=True, timeout=0).send_stdin("x")


def test_pty(sbx: Sandbox) -> None:
    terminal = sbx.pty.create(PtySize(rows=24, cols=80), cwd="/tmp", envs={"GREETING": "hi"})
    assert terminal.pid in {process.pid for process in sbx.commands.list()}
    sbx.pty.resize(terminal.pid, PtySize(rows=30, cols=123))
    sbx.pty.send_stdin(terminal.pid, b"echo $GREETING $(pwd) $TERM $(tput cols); exit 3\n")
    output = bytearray()
    with pytest.raises(CommandExitException) as exit_error:
        terminal.wait(on_pty=output.extend)
    assert exit_error.value.exit_code == 3
    assert b"hi /tmp xterm-256color 123" in output
    other = sbx.pty.create(PtySize(rows=24, cols=80))
    assert sbx.pty.kill(other.pid) is True
    assert other.pid not in {process.pid for process in sbx.commands.list()}


def test_file_info_and_options(sbx: Sandbox) -> None:
    sbx.files.write("info/a.txt", "12345", request_timeout=5, use_octet_stream=True)
    sbx.commands.run("chmod 640 info/a.txt")
    info = sbx.files.get_info("info/a.txt")
    assert (info.name, info.type, info.size, info.permissions) == ("a.txt", FileType.FILE, 5, "rw-r-----")
    assert info.mode == 0o640 and info.owner == "root"
    assert sbx.files.get_info("info").type == FileType.DIR
    with pytest.raises(FileNotFoundException):
        sbx.files.get_info("info/missing")
    with pytest.warns(UserWarning, match="user"):
        assert sbx.files.read("info/a.txt", user="user") == "12345"
    with pytest.raises(NotSupportedException, match="metadata"):
        sbx.files.write("info/b.txt", "x", metadata={"k": "v"})


def test_watch_dir(sbx: Sandbox) -> None:
    sbx.files.write("watched/old.txt", "old")
    watcher = sbx.files.watch_dir("watched", recursive=True)
    sbx.files.write("watched/new/inner.txt", "new")
    sbx.files.write("watched/old.txt", "changed")
    sbx.commands.run("chmod 600 watched/old.txt")
    events = {(event.name, event.type) for event in watcher.get_new_events()}
    assert ("new", FilesystemEventType.CREATE) in events
    assert ("new/inner.txt", FilesystemEventType.CREATE) in events
    assert ("old.txt", FilesystemEventType.WRITE) in events
    sbx.files.remove("watched/old.txt")
    assert [(e.name, e.type) for e in watcher.get_new_events()] == [("old.txt", FilesystemEventType.REMOVE)]
    watcher.stop()
    with pytest.raises(SandboxException):
        watcher.get_new_events()


def test_secret_values_stay_out_of_what_the_runs_api_returns() -> None:
    secret = f"secret-{uuid.uuid4().hex}"
    # The API redacts what looks like an assignment in a command, but only its first line.
    key = f"-----BEGIN KEY-----\n{secret}\n-----END KEY-----\n"
    with e2b_code_interpreter.Sandbox.create() as sandbox:
        assert sandbox.commands.run('printf %s "$KEY"', envs={"KEY": key}).stdout == key
        handle = sandbox.commands.run("sleep 600", background=True, envs={"KEY": key}, timeout=0)
        assert [info.envs for info in sandbox.commands.list() if info.pid == handle.pid] == [{"KEY": key}]
        handle.kill()
        with pytest.raises(SandboxException):
            sandbox.git.clone("http://127.0.0.1:9/repo.git", "/tmp/repo", username="someone", password=secret)
        sandbox.git.dangerously_authenticate("someone", secret, host="example.com")
        assert sandbox.run_code("import os\nos.environ['KEY']", envs={"KEY": key}).text == repr(key)
        runs, cursor = [], None
        while True:
            page = sandbox.arker.list_runs(cursor=cursor)
            runs += page.runs
            cursor = page.next_cursor
            if not cursor:
                break
        assert [run.command for run in runs if secret in (run.command or "")] == []
        # Only the two commands that print it on purpose output it.
        assert len([run for run in runs if secret in sandbox.arker.get_run(run.run_id).stdout]) == 2
        # Nor does any file another user could read.
        found = sandbox.commands.run(f"grep -rlF {secret[:-1]} /tmp /etc /home 2>/dev/null; true").stdout
        assert found == ""
