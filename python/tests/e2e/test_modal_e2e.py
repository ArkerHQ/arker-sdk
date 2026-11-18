"""Live tests for the Modal compatibility layer: common Modal sandbox code,
unchanged except for its imports, against a real deployment.

Run with:

    ARKER_API_KEY=... python -m pytest tests/e2e/test_modal_e2e.py
"""

from __future__ import annotations

import os
import time
import urllib.request
import uuid

import pytest

from arker import Arker, modal

if not os.environ.get("ARKER_API_KEY"):
    pytest.skip("live Arker credentials are not configured", allow_module_level=True)

if not os.environ.get("ARKER_BASE_URL"):
    os.environ.setdefault("ARKER_PROVIDER", "aws")
    os.environ.setdefault("ARKER_REGION", "us-east-1")


@pytest.fixture(scope="module")
def sb():
    app = modal.App.lookup("arker-e2e", create_if_missing=True)
    sandbox = modal.Sandbox.create(
        app=app,
        env={"SANDBOX_VAR": "from-create"},
        secrets=[modal.Secret.from_dict({"TOKEN": "secret"})],
        encrypted_ports=[8000],
        workdir="/tmp",
    )
    yield sandbox
    sandbox.terminate()


def test_is_an_arker_vm(sb: modal.Sandbox) -> None:
    assert Arker().get_vm(sb.object_id).vm_id == sb.object_id
    assert sb.arker.run("echo native").stdout == "native\n"


def test_exec(sb: modal.Sandbox) -> None:
    process = sb.exec(
        "bash", "-c", "echo $SANDBOX_VAR $TOKEN $RUN_VAR $(pwd); echo oops >&2; exit 3", env={"RUN_VAR": "x"}
    )
    assert process.wait() == 3
    assert process.returncode == 3
    assert process.stdout.read() == "from-create secret x /tmp\n"
    assert process.stderr.read() == "oops\n"
    assert sb.exec("pwd", workdir="/").stdout.read() == "/\n"


def test_exec_streams_and_runs_concurrently(sb: modal.Sandbox) -> None:
    slow = sb.exec("bash", "-c", "for i in 1 2 3; do echo line$i; sleep 1; done")
    assert sb.exec("echo", "meanwhile").stdout.read() == "meanwhile\n"
    assert list(slow.stdout) == ["line1\n", "line2\n", "line3\n"]


def test_exec_timeout(sb: modal.Sandbox) -> None:
    with pytest.raises(modal.ExecTimeoutError):
        sb.exec("sleep", "5", timeout=1).wait()


def test_files(sb: modal.Sandbox) -> None:
    with sb.open("/tmp/modal/notes.txt", "w") as handle:
        handle.write("hello")
    with sb.open("/tmp/modal/notes.txt", "a") as handle:
        handle.write(" world")
    assert sb.open("/tmp/modal/notes.txt").read() == "hello world"
    assert "notes.txt" in sb.ls("/tmp/modal")
    with pytest.raises(modal.ExecutionError):
        sb.rm("/tmp/modal")
    sb.rm("/tmp/modal", recursive=True)
    with pytest.raises(modal.SandboxFilesystemNotFoundError):
        sb.open("/tmp/modal/notes.txt")


def test_tunnel_serves_a_port(sb: modal.Sandbox) -> None:
    with sb.open("/tmp/www/index.html", "w") as handle:
        handle.write("served")
    sb.exec("python3", "-m", "http.server", "8000", workdir="/tmp/www")
    url = modal.Sandbox.from_id(sb.object_id).tunnels()[8000].url + "/index.html"
    for _ in range(20):
        try:
            assert urllib.request.urlopen(url, timeout=10).read() == b"served"
            return
        except OSError:
            time.sleep(1)
    pytest.fail(f"{url} never served")


def test_image_entrypoint_and_options() -> None:
    image = modal.Image.debian_slim(python_version="3.12").pip_install("six").env({"FROM_IMAGE": "1"})
    sandbox = modal.Sandbox.create(
        "python",
        "-c",
        "import six, os; print(os.environ['FROM_IMAGE']); raise SystemExit(4)",
        image=image,
        cpu=2,
        block_network=True,
    )
    try:
        sandbox.wait()
        assert sandbox.returncode == 4
        assert sandbox.exec("nproc").stdout.read() == "2\n"
        fetch = "import urllib.request; urllib.request.urlopen('https://example.com', timeout=10)"
        blocked = sandbox.exec("python", "-c", fetch)
        assert blocked.wait() != 0
        assert "Error" in blocked.stderr.read()
    finally:
        sandbox.terminate()


def test_timeout_deletes_the_sandbox() -> None:
    sandbox = modal.Sandbox.create(timeout=5)
    deadline = time.monotonic() + 120
    while _exists(sandbox.object_id):
        assert time.monotonic() < deadline, "sandbox outlived its timeout"
        time.sleep(5)


def _exists(vm_id: str) -> bool:
    try:
        Arker().get_vm(vm_id)
    except Exception:
        return False
    return True


def test_stdout_then_stderr(sb: modal.Sandbox) -> None:
    process = sb.exec("bash", "-c", "echo o; echo e >&2")
    assert list(process.stdout) == ["o\n"]
    assert list(process.stderr) == ["e\n"]
    assert process.stdout.read() == "o\n"
    assert list(sb.exec("printf", "a\\nb").stdout) == ["a\n", "b"]


def test_cidr_allowlist(reachable) -> None:
    sandbox = modal.Sandbox.create(cidr_allowlist=["1.1.1.1/32"], timeout=120)
    try:
        run = lambda cmd: sandbox.exec("bash", "-c", cmd).stdout.read()
        assert reachable(run, "https://1.1.1.1", "https://example.com") == {
            "https://1.1.1.1": True,
            "https://example.com": False,
        }
    finally:
        sandbox.terminate()


def test_image_build_steps(tmp_path, monkeypatch) -> None:
    (tmp_path / "data.txt").write_text("data")
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "keep.txt").write_text("keep")
    (tmp_path / "dir" / "skip.log").write_text("skip")
    (tmp_path / "arker_e2e_pkg").mkdir()
    (tmp_path / "arker_e2e_pkg" / "__init__.py").write_text("VALUE = 'local source'\n")
    (tmp_path / "arker_e2e_pkg" / "notes.txt").write_text("not python")
    (tmp_path / "requirements.txt").write_text("packaging\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "e2e"\nversion = "0"\ndependencies = ["idna"]\n'
        '[project.optional-dependencies]\nextra = ["six"]\n'
    )
    (tmp_path / "uv").mkdir()
    (tmp_path / "uv" / "pyproject.toml").write_text(
        '[project]\nname = "e2e-uv"\nversion = "0"\nrequires-python = ">=3.8"\ndependencies = ["tomli"]\n'
    )
    (tmp_path / ".env").write_text("# local settings\nexport FROM_DOTENV='dotenv value'\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("E2E_LOCAL", "local value")
    image = (
        modal.Image.from_registry("python:3.12-slim")
        .add_local_file(tmp_path / "data.txt", "/opt/data.txt")
        .add_local_dir(tmp_path / "dir", "/opt/dir", ignore=["*.log"])
        .add_local_python_source("arker_e2e_pkg")
        .pip_install("attrs", index_url="https://pypi.org/simple", extra_options="--no-cache-dir")
        .pip_install_from_requirements(str(tmp_path / "requirements.txt"))
        .pip_install_from_pyproject(str(tmp_path / "pyproject.toml"), optional_dependencies=["extra"])
        .uv_pip_install("iniconfig")
        .uv_sync(str(tmp_path / "uv"), frozen=False)
        .run_commands("echo $BUILD_SECRET > /opt/secret.txt", secrets=[modal.Secret.from_dict({"BUILD_SECRET": "s"})])
        .dockerfile_commands(
            ["ENV DOCKER_ENV=d", "ARG STAGE=build", "WORKDIR /srv", "RUN echo $STAGE > stage.txt", "COPY data.txt ."],
            context_dir=tmp_path,
        )
        .entrypoint(["bash", "-c"])
        .cmd(["echo ran > /tmp/ran.txt"])
    )
    secrets = [modal.Secret.from_local_environ(["E2E_LOCAL"]), modal.Secret.from_dotenv(tmp_path)]
    sandbox = modal.Sandbox.create(image=image, secrets=secrets, timeout=600)
    try:
        sandbox.wait()
        assert sandbox.returncode == 0

        def run(script: str) -> str:
            process = sandbox.exec("bash", "-c", script, workdir="/")
            assert process.wait() == 0, process.stderr.read()
            return process.stdout.read()

        assert run("cat /tmp/ran.txt /opt/data.txt /opt/secret.txt") == "ran\ndatas\n"
        assert run("ls /opt/dir /root/arker_e2e_pkg") == "/opt/dir:\nkeep.txt\n\n/root/arker_e2e_pkg:\n__init__.py\n"
        imports = "import arker_e2e_pkg, attrs, packaging, idna, six, iniconfig, tomli; print(arker_e2e_pkg.VALUE)"
        assert run(f"python -c '{imports}'") == "local source\n"
        assert run("echo $DOCKER_ENV/$STAGE/$BUILD_SECRET/$E2E_LOCAL/$FROM_DOTENV") == "d///local value/dotenv value\n"
        assert sandbox.exec("pwd").stdout.read() == "/srv\n"
        assert run("cat /srv/stage.txt /srv/data.txt") == "build\ndata"
    finally:
        sandbox.terminate()
    with pytest.raises(modal.InvalidError, match="E2E_MISSING"):
        modal.Secret.from_local_environ(["E2E_MISSING"])


def test_pip_options_reach_pip() -> None:
    image = modal.Image.from_registry("python:3.12-slim").pip_install("six", index_url="https://example.invalid/simple")
    with pytest.raises(modal.ExecutionError, match=r"example\.invalid"):
        modal.Sandbox.create(image=image, timeout=120)


def test_image_from_dockerfile(tmp_path) -> None:
    (tmp_path / "data.txt").write_text("data")
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.12-slim\nARG GREETING=hi\nENV APP=1\nWORKDIR /app\nCOPY data.txt .\n"
        'RUN echo $GREETING > greeting.txt\nENTRYPOINT ["bash", "-c"]\n'
    )
    image = modal.Image.from_dockerfile(tmp_path / "Dockerfile", context_dir=tmp_path, build_args={"GREETING": "hello"})
    sandbox = modal.Sandbox.create("echo $APP $GREETING $(cat greeting.txt data.txt) > out.txt", image=image)
    try:
        sandbox.wait()
        assert sandbox.exec("cat", "/app/out.txt").stdout.read() == "1 hello data\n"
    finally:
        sandbox.terminate()


def test_dockerfile_copy_stays_in_the_context_and_honours_ignores(tmp_path) -> None:
    context = tmp_path / "context"
    (context / "sub").mkdir(parents=True)
    (context / "app.py").write_text("app")
    (context / ".env").write_text("TOKEN=local")
    (context / "sub" / "keep.txt").write_text("keep")
    (context / "sub" / "debug.log").write_text("log")
    (context / ".dockerignore").write_text(".env\n**/*.log\n")
    (tmp_path / "outside.txt").write_text("outside")
    for escape in ("COPY ../outside.txt /x", "COPY /etc/hostname /x"):
        with pytest.raises(modal.InvalidError, match="outside the build context"):
            modal.Image().dockerfile_commands([escape], context_dir=context)
    secret = f"secret-{uuid.uuid4().hex}"
    key = f"-----BEGIN KEY-----\n{secret}\n-----END KEY-----\n"
    image = (
        modal.Image()
        .dockerfile_commands(["COPY . /app"], context_dir=context)
        .dockerfile_commands(["COPY . /other"], context_dir=context, ignore=["sub"])
        .run_commands('printf %s "$KEY" > /tmp/key', secrets=[modal.Secret.from_dict({"KEY": key})])
    )
    sandbox = modal.Sandbox.create(image=image, timeout=300)
    try:
        assert sandbox.exec("cat", "/tmp/key").stdout.read() == key
        listing = sandbox.exec("bash", "-c", "cd / && find app other -type f | sort").stdout.read()
        copied = ["app/.dockerignore", "app/app.py", "app/sub/keep.txt", "other/.dockerignore", "other/.env"]
        assert listing.split() == [*copied, "other/app.py"]
        runs = sandbox.arker.list_runs().runs
        assert [run.command for run in runs if secret in (run.command or "")] == []
    finally:
        sandbox.terminate()


def test_exec_stdin(sb: modal.Sandbox) -> None:
    process = sb.exec("bash", "-c", "read first; echo got $first; cat")
    process.stdin.write(b"one\n")
    process.stdin.drain()
    process.stdin.write("two\n")
    process.stdin.write_eof()
    process.stdin.drain()
    assert process.wait() == 0
    assert process.stdout.read() == "got one\ntwo\n"


def test_exec_stream_options(sb: modal.Sandbox) -> None:
    quiet = sb.exec("bash", "-c", "echo out; echo err >&2", stdout=modal.StreamType.DEVNULL)
    assert quiet.wait() == 0
    assert (quiet.stdout.read(), quiet.stderr.read()) == ("", "err\n")
    raw = sb.exec("printf", "a\\nb", text=False, bufsize=1)
    assert list(raw.stdout) == [b"a\n", b"b"]
    assert raw.stdout.read() == b"a\nb"
    with pytest.raises(modal.NotSupportedError, match="pty"):
        sb.exec("true", pty=True)
    with pytest.raises(modal.NotSupportedError, match="stdout"):
        sb.exec("true", stdout=modal.StreamType.STDOUT)


def test_entrypoint_streams() -> None:
    sandbox = modal.Sandbox.create("bash", "-c", "read line; echo got $line; echo err >&2", timeout=120)
    try:
        sandbox.stdin.write("hello\n")
        sandbox.stdin.write_eof()
        sandbox.stdin.drain()
        sandbox.wait()
        assert sandbox.stdout.read() == "got hello\n"
        assert sandbox.stderr.read() == "err\n"
    finally:
        sandbox.terminate()


def test_volumes() -> None:
    name = f"e2e-{uuid.uuid4().hex[:12]}"
    with pytest.raises(modal.NotFoundError):
        modal.Sandbox.create(volumes={"/data": modal.Volume.from_name(name)})
    volume = modal.Volume.from_name(name, create_if_missing=True)
    try:
        writer = modal.Sandbox.create(volumes={"/mnt/data": volume}, timeout=120)
        reader = modal.Sandbox.create(volumes={"/data": modal.Volume.from_name(name)}, timeout=120)
        try:
            with writer.open("/mnt/data/note.txt", "w") as handle:
                handle.write("shared")
            volume.commit()
            # A sandbox already running sees the write once it reloads its volumes, as on Modal.
            reader.reload_volumes()
            assert reader.open("/data/note.txt").read() == "shared"
            volume.reload()
        finally:
            writer.terminate()
            reader.terminate()
    finally:
        modal.Volume.objects.delete(name)
    with pytest.raises(modal.NotFoundError):
        modal.Volume.objects.delete(name)


def test_filesystem(sb: modal.Sandbox, tmp_path) -> None:
    fs = sb.filesystem
    fs.write_text("hello", "/tmp/fs/a.txt")
    fs.write_bytes(b"\x00\x01", "/tmp/fs/b.bin")
    assert fs.read_text("/tmp/fs/a.txt") == "hello"
    assert fs.read_bytes("/tmp/fs/b.bin") == b"\x00\x01"
    fs.make_directory("/tmp/fs/sub/deeper")
    with pytest.raises(modal.ExecutionError):
        fs.make_directory("/tmp/fs/none/deeper", create_parents=False)
    listed = {info.name: info for info in fs.list_files("/tmp/fs")}
    assert set(listed) == {"a.txt", "b.bin", "sub"}
    assert listed["sub"].is_dir() and listed["a.txt"].is_file() and listed["a.txt"].size == 5
    sb.exec("chmod", "600", "/tmp/fs/a.txt").wait()
    info = fs.stat("/tmp/fs/a.txt")
    assert (info.path, info.permissions, info.owner) == ("/tmp/fs/a.txt", "rw-------", "root")
    local = tmp_path / "local.txt"
    local.write_text("from local")
    fs.copy_from_local(local, "/tmp/fs/copied.txt")
    fs.copy_to_local("/tmp/fs/copied.txt", tmp_path / "back" / "copied.txt")
    assert (tmp_path / "back" / "copied.txt").read_text() == "from local"
    with pytest.raises(modal.ExecutionError):
        fs.remove("/tmp/fs")
    fs.remove("/tmp/fs", recursive=True)
    with pytest.raises(modal.SandboxFilesystemNotFoundError):
        fs.stat("/tmp/fs")
    with pytest.raises(modal.SandboxFilesystemNotFoundError):
        fs.read_text("/tmp/fs/a.txt")


def test_filesystem_watch(sb: modal.Sandbox) -> None:
    sb.filesystem.make_directory("/tmp/watched")
    sb.exec("bash", "-c", "sleep 2; echo x > /tmp/watched/a; sleep 2; rm /tmp/watched/a")
    events = list(sb.filesystem.watch("/tmp/watched", timeout=8))
    assert [(event.paths, event.type) for event in events] == [
        (["/tmp/watched/a"], modal.FileWatchEventType.Create),
        (["/tmp/watched/a"], modal.FileWatchEventType.Remove),
    ]
    sb.exec("bash", "-c", "sleep 2; echo y > /tmp/watched/b; sleep 2; rm /tmp/watched/b")
    removes = sb.filesystem.watch("/tmp/watched", filter=[modal.FileWatchEventType.Remove], timeout=8)
    assert [event.type for event in removes] == [modal.FileWatchEventType.Remove]
