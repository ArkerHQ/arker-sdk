import asyncio
import itertools
import json
import os
import pwd
import shutil
import stat
import subprocess
import tempfile
import types
from pathlib import Path

import pytest

pytest.importorskip("daytona")

from daytona import DaytonaBadRequestError, DaytonaForbiddenError, DaytonaNotFoundError, FileUpload
from daytona_toolbox_api_client import FileInfo
from daytona_toolbox_api_client_async import FileInfo as AsyncFileInfo

from arker import _daytona_filesystem as implementation
from arker._compat import CommandResult
from arker._compat_daytona_async import AsyncSandbox
from arker.daytona import Sandbox


def test_daytona_exposes_filesystem_on_arker_and_preserves_native():
    from arker.daytona import Sandbox

    assert callable(Sandbox(vm=types.SimpleNamespace(id="local")).fs.upload_file)
    native = types.SimpleNamespace(id="native", process=object(), fs=object())
    assert Sandbox(native=native).fs is native.fs


@pytest.fixture
def guest(monkeypatch):
    home = Path(tempfile.mkdtemp(prefix="daytona-guest-test-"))
    home.chmod(0o777)
    uid, gid = (65534, 65534) if os.getuid() == 0 else (os.getuid(), os.getgid())
    account = pwd.getpwuid(uid)
    replacement = "pwd.struct_passwd(" + repr((account.pw_name, "x", uid, gid, "", str(home), "/bin/sh")) + ")"
    monkeypatch.setattr(
        implementation, "_SOURCE", implementation._SOURCE.replace('pwd.getpwnam("daytona")', replacement)
    )
    stages = []

    def run(vm, command, **kwargs):
        command = command.replace(
            'setpriv --reuid=daytona --regid="$(id -g daytona)" --init-groups --',
            f"setpriv --reuid={uid} --regid={gid} --keep-groups --",
        )
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=5)
        return CommandResult(stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode)

    def sync(path, data=None):
        stage = Path(path)
        stages.append(stage)
        assert stage.parent.name.startswith(".arker-daytona-fs-")
        assert stat.S_IMODE(stage.parent.stat().st_mode) == 0o700
        assert not stage.is_symlink() and stat.S_ISREG(stage.stat().st_mode)
        if data is not None:
            stage.write_bytes(data)
            return None
        return stage.read_bytes()

    def read_chunks(path):
        yield sync(path)

    monkeypatch.setattr(implementation, "run_command", run)
    monkeypatch.setattr("arker.daytona.run_command", run)
    sandbox = Sandbox(
        vm=types.SimpleNamespace(
            id="local",
            sync=sync,
            _sync_read_chunks=read_chunks,
            _sync_write_chunks=lambda path, size, read, **kwargs: sync(path, read(0, size)),
        )
    )
    yield sandbox, home, uid, stages
    assert all(not stage.parent.exists() for stage in stages)
    shutil.rmtree(home)


def test_core_binary_metadata_permissions_paths_and_mutations(guest, tmp_path):
    sandbox, home, uid, _ = guest
    fs = sandbox.fs
    payload = bytes(range(256))
    fs.create_folder("dir/nested", "750")
    fs.upload_file(payload, "dir/nested/line\nbreak")
    assert fs.download_file("dir/nested/line\nbreak") == payload
    info = fs.get_file_info("dir/nested/line\nbreak")
    assert isinstance(info, FileInfo)
    assert (info.owner, info.size, info.name, info.is_dir) == (str(uid), 256, "line\nbreak", False)
    fs.set_file_permissions("dir/nested/line\nbreak", mode="640")
    fs.upload_file(b"short", "dir/nested/line\nbreak")
    assert fs.get_file_info("dir/nested/line\nbreak").permissions == "0640"
    local = tmp_path / "upload"
    local.write_bytes(payload)
    fs.upload_files([FileUpload(str(local), "dir/local"), FileUpload(b"", "dir/empty")])
    output = tmp_path / "download"
    assert fs.download_file("dir/local", str(output)) is None
    assert output.read_bytes() == payload
    assert fs.download_file("dir/empty") == b""
    assert {v.path for v in fs.list_files("dir", depth=2)} == {
        str(home / p) for p in ["dir/nested", "dir/nested/line\nbreak", "dir/local", "dir/empty"]
    }
    fs.move_files("dir/local", "dir/moved")
    with pytest.raises(DaytonaNotFoundError):
        fs.get_file_info("dir/local")
    with pytest.raises(DaytonaBadRequestError):
        fs.delete_file("dir")
    assert fs.download_file("dir/moved") == payload
    fs.delete_file("dir", recursive=True)
    assert not (home / "dir").exists()


def test_permissions_symlinks_and_fifos_never_gain_root_access(guest):
    sandbox, home, _, _ = guest
    fs = sandbox.fs
    protected = home / "protected"
    protected.write_bytes(b"protected")
    protected.chmod(0o000)
    (home / "link").symlink_to(protected)
    for action in [lambda: fs.download_file("link"), lambda: fs.upload_file(b"bad", "link")]:
        with pytest.raises(DaytonaForbiddenError):
            action()
    protected.chmod(0o444)
    assert protected.read_bytes() == b"protected"
    fs.upload_file(b"allowed", "owned")
    (home / "owned-link").symlink_to(home / "owned")
    assert fs.download_file("owned-link") == b"allowed"
    fs.upload_file(b"changed", "owned-link")
    assert fs.download_file("owned") == b"changed"
    if os.getuid() == 0:
        with pytest.raises(DaytonaForbiddenError):
            fs.set_file_permissions("owned", owner="0")
    os.mkfifo(home / "fifo", 0o666)
    (home / "fifo").chmod(0o666)
    for action in [lambda: fs.download_file("fifo"), lambda: fs.upload_file(b"bad", "fifo")]:
        with pytest.raises(DaytonaBadRequestError):
            action()


def test_async_filesystem_preserves_native_model_types(guest):
    sandbox, _, _, _ = guest

    async def program():
        fs = AsyncSandbox(sandbox).fs
        await fs.upload_file(b"async", "async")
        assert await fs.download_file("async") == b"async"
        assert isinstance(await fs.get_file_info("async"), AsyncFileInfo)

    asyncio.run(program())


def test_transfer_zero_disables_timeout_and_invalid_path_still_fails(guest):
    fs = guest[0].fs
    fs.upload_file(b"x", "x", timeout=0)
    assert fs.download_file("x", 0) == b"x"
    assert fs.list_files(".", request_timeout=1)
    with pytest.raises(DaytonaBadRequestError):
        fs.upload_file(b"x", "")


def test_interrupted_upload_cleans_staging_and_keeps_primary_error(guest, monkeypatch):
    sandbox, _, _, stages = guest

    def fail(path, size, read, **transport):
        stages.append(Path(path))
        Path(path).write_bytes(b"partial")
        raise OSError("upload interrupted")

    monkeypatch.setattr(sandbox._vm, "_sync_write_chunks", fail)
    with pytest.raises(OSError, match="upload interrupted"):
        sandbox.fs.upload_file(b"data", "destination")


def test_bulk_download_reports_each_failure_without_discarding_success(guest, tmp_path):
    from daytona import FileDownloadRequest
    from daytona.common.filesystem import FileDownloadResponse

    fs = guest[0].fs
    fs.upload_file(b"ok", "present")
    output = tmp_path / "output"
    results = fs.download_files(
        [
            FileDownloadRequest(source="missing"),
            FileDownloadRequest(source="present"),
            FileDownloadRequest(source="present", destination=str(output)),
        ]
    )
    assert all(isinstance(result, FileDownloadResponse) for result in results)
    assert results[0].error_details.status_code == 404
    assert results[1].result == b"ok"
    assert results[2].result == str(output)
    assert output.read_bytes() == b"ok"


def test_staging_collision_never_syncs_or_removes_an_unowned_path(monkeypatch):
    commands = []

    def fail(vm, command, **transport):
        commands.append(command)
        return CommandResult(stdout="", stderr="File exists", exit_code=1)

    monkeypatch.setattr(implementation, "run_command", fail)
    fs = implementation.FileSystem(types.SimpleNamespace(sync=lambda *args: pytest.fail("must not sync")))
    with pytest.raises(Exception, match="File exists"):
        fs.upload_file(b"x", "file")
    assert len(commands) == 1 and commands[0].startswith("mkdir -m 700 -- ")


def test_local_fifo_transfer_is_rejected_without_blocking(guest, tmp_path):
    from daytona import DaytonaError

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    fs = guest[0].fs
    with pytest.raises(DaytonaError, match="regular file"):
        fs.upload_file(str(fifo), "remote")
    fs.upload_file(b"x", "remote")
    with pytest.raises(DaytonaError):
        fs.download_file("remote", str(fifo))


@pytest.mark.parametrize("creation_fails", [False, True])
def test_live_scenario_preserves_primary_failure_and_attempts_all_cleanup(monkeypatch, creation_fails):
    import runpy

    scenarios = Path(__file__).resolve().parents[1] / "e2e"
    monkeypatch.syspath_prepend(str(scenarios))
    monkeypatch.delenv("COMPAT_COVERAGE_EVIDENCE", raising=False)
    program = runpy.run_path(str(scenarios / "test_daytona_filesystem_import_only.py"))["program"]
    primary = ValueError("controlled primary failure")
    actions = []

    def fail(*args):
        raise primary

    class Client:
        def create(self):
            if creation_fails:
                raise primary
            return types.SimpleNamespace(fs=types.SimpleNamespace(create_folder=fail))

        def delete(self, sandbox):
            actions.append("delete")
            raise RuntimeError("controlled deletion failure")

        def close(self):
            actions.append("close")
            raise RuntimeError("controlled close failure")

    with pytest.raises(ValueError) as captured:
        asyncio.run(program(Client))
    assert captured.value is primary
    assert actions == (["close"] if creation_fails else ["delete", "close"])
    assert primary.__notes__ == ["Owned-resource cleanup also failed; verify deletion independently."]


def test_live_sync_scenario_without_close_completes_and_deletes(monkeypatch):
    import runpy

    from daytona import AsyncDaytona, Daytona

    assert not hasattr(Daytona, "close")
    assert callable(AsyncDaytona.close)
    scenarios = Path(__file__).resolve().parents[1] / "e2e"
    monkeypatch.syspath_prepend(str(scenarios))
    program = runpy.run_path(str(scenarios / "test_daytona_filesystem_import_only.py"))["program"]
    actions = []
    fs = types.SimpleNamespace(
        create_folder=lambda *args: None,
        upload_file=lambda *args: None,
        download_file=lambda *args: bytes(range(256)),
        get_file_info=lambda *args: types.SimpleNamespace(size=256, is_dir=False, owner="1000", permissions="0640"),
        set_file_permissions=lambda *args, **kwargs: None,
        move_files=lambda *args: None,
        list_files=lambda *args: [types.SimpleNamespace(name="moved")],
        delete_file=lambda *args, **kwargs: None,
    )

    class Client:
        def create(self):
            return types.SimpleNamespace(
                fs=fs, process=types.SimpleNamespace(exec=lambda *args: types.SimpleNamespace(result="1000\n"))
            )

        def delete(self, sandbox):
            actions.append("delete")

    stages = []
    asyncio.run(program(Client, stages.append))
    assert actions == ["delete"]
    assert "identity" in stages
    assert stages[-1] == "cleanup-delete"


def test_live_scenario_creation_failure_stage_survives_cleanup(monkeypatch):
    import runpy

    from arker._compat import CompatibilityCreationNotStartedError

    scenarios = Path(__file__).resolve().parents[1] / "e2e"
    monkeypatch.syspath_prepend(str(scenarios))
    program = runpy.run_path(str(scenarios / "test_daytona_filesystem_import_only.py"))["program"]
    primary = CompatibilityCreationNotStartedError("controlled")

    class Client:
        def create(self):
            raise primary

    stages = []
    with pytest.raises(CompatibilityCreationNotStartedError) as captured:
        asyncio.run(program(Client, stages.append))
    assert captured.value is primary
    assert stages == ["client-init", "create"]


def test_process_and_filesystem_share_actual_account_home_and_permissions(guest):
    sandbox, home, uid, _ = guest
    result = sandbox.process.exec('id -u; printf "%s\\n" "$HOME"; pwd')
    assert result.exit_code == 0
    assert result.result.splitlines() == [str(uid), str(home), str(home)]
    sandbox.fs.create_folder("commands", "700")
    result = sandbox.process.exec('printf "%s" "$VALUE" > from-command', cwd="commands", env={"VALUE": "user-owned"})
    assert result.exit_code == 0
    assert sandbox.fs.download_file("commands/from-command") == b"user-owned"
    assert sandbox.fs.get_file_info("commands/from-command").owner == str(uid)
    protected = home / "protected-command"
    protected.write_text("unchanged")
    protected.chmod(0o000)
    assert sandbox.process.exec("cat protected-command").exit_code != 0
    assert sandbox.process.exec("printf bad > protected-command").exit_code != 0
    protected.chmod(0o600)
    assert protected.read_text() == "unchanged"
    assert sandbox.process.exec("exit 17").exit_code == 17
    assert sandbox.process.exec("pwd").result.strip() == str(home)


def test_native_process_positional_arguments_and_directory_accessors(guest):
    sandbox, home, _, _ = guest
    assert sandbox.get_user_home_dir() == str(home)
    sandbox.fs.create_folder("positional", "700")
    result = sandbox.process.exec('printf "%s" "$VALUE"', "positional", {"VALUE": "positional"}, 5)
    assert result.exit_code == 0 and result.result == "positional"

    async def check_async():
        asynchronous = AsyncSandbox(sandbox)
        assert await asynchronous.get_user_home_dir() == str(home)
        result = await asynchronous.process.exec('printf "%s" "$VALUE"', "positional", {"VALUE": "async"}, 5)
        assert result.result == "async"

    asyncio.run(check_async())


def test_home_directory_preserves_native_routes_and_does_not_hide_guest_failure(monkeypatch):
    native = types.SimpleNamespace(id="native", process=object(), get_user_home_dir=lambda: "/native-home")
    assert Sandbox(native=native).get_user_home_dir() == "/native-home"

    async def native_home():
        return "/async-home"

    native.get_user_home_dir = native_home
    assert asyncio.run(AsyncSandbox(Sandbox(native=native)).get_user_home_dir()) == "/async-home"
    monkeypatch.setattr(
        "arker.daytona.run_command", lambda *args, **kwargs: CommandResult(stdout="", stderr="", exit_code=1)
    )
    from daytona import DaytonaError

    with pytest.raises(DaytonaError, match="Failed to get user home"):
        Sandbox(vm=types.SimpleNamespace(id="local")).get_user_home_dir()


def test_daytona_parent_id_selection_requires_independent_placement(monkeypatch):
    from arker._compat import arker_create

    monkeypatch.setenv("ARKER_API_KEY", "local-placeholder")
    monkeypatch.setenv("ARKER_SOURCE_VM_ID", "local-parent")
    for key in ("ARKER_SOURCE", "ARKER_SOURCE_VM", "ARKER_BASE_URL", "ARKER_PROVIDER", "ARKER_REGION"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="No placement configured"):
        arker_create()
    requests = []
    monkeypatch.setattr(
        "arker._compat.arker_client",
        lambda config: types.SimpleNamespace(fork=lambda **kwargs: requests.append(kwargs)),
    )
    arker_create()
    assert requests == [{"name": None, "source_vm_id": "local-parent"}]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_download_creates_local_parents_and_preserves_binary_overwrite(monkeypatch, tmp_path, asynchronous):
    sandbox = Sandbox(vm=types.SimpleNamespace(id="local"))
    remote = {}

    def transfer(self, operation, path, data=None, **transport):
        if operation == "upload":
            remote[path] = data
            return None
        return remote[path]

    monkeypatch.setattr(implementation.FileSystem, "_transfer", transfer)
    destination = tmp_path / "missing" / "nested" / "payload.bin"
    payload = bytes(range(256)) + b"\x00\xff"

    async def program():
        fs = AsyncSandbox(sandbox).fs if asynchronous else sandbox.fs

        async def call(method, *args):
            result = method(*args)
            return await result if asynchronous else result

        await call(fs.upload_file, payload, "remote")
        assert await call(fs.download_file, "remote", str(destination)) is None
        assert destination.read_bytes() == payload
        await call(fs.upload_file, b"short", "remote")
        await call(fs.download_file, "remote", str(destination))
        assert destination.read_bytes() == b"short"

    asyncio.run(program())


def test_download_invalid_local_parent_returns_bulk_error_without_mutation(monkeypatch, tmp_path):
    from daytona import FileDownloadRequest

    fs = implementation.FileSystem(None)
    monkeypatch.setattr(fs, "_transfer", lambda *args, **kwargs: b"remote")
    parent = tmp_path / "not-a-directory"
    parent.write_bytes(b"keep")
    result = fs.download_files([FileDownloadRequest(source="remote", destination=str(parent / "child"))])[0]
    assert result.error
    assert result.result is None
    assert parent.read_bytes() == b"keep"


@pytest.fixture
def streamed_guest(guest):
    sandbox, _home, _uid, stages = guest

    def chunks(path, **kwargs):
        stages.append(Path(path))
        with open(path, "rb") as stream:
            while chunk := stream.read(65536):
                yield chunk

    def writes(path, size, read, **kwargs):
        stages.append(Path(path))
        with open(path, "wb") as stream:
            for offset in range(0, max(size, 1), 65536):
                stream.write(read(offset, min(size - offset, 65536)))

    sandbox.fs._vm._sync_read_chunks = chunks
    sandbox.fs._vm._sync_write_chunks = writes
    return guest


def test_streamed_transfer_preserves_binary_progress_and_caller_ownership(streamed_guest):
    import io

    sandbox, _home, _, _ = streamed_guest
    payload = bytes(range(256)) * 1025
    source = io.BytesIO(b"prefix" + payload)
    source.seek(6)
    sent, received = [], []
    sandbox.fs.upload_file_stream(source, "streamed", on_progress=sent.append)
    assert not source.closed
    assert source.tell() == 6 + len(payload)
    chunks = list(sandbox.fs.download_file_stream("streamed", on_progress=received.append))
    assert b"".join(chunks) == payload
    assert max(map(len, chunks)) <= 65536
    assert sent[-1].bytes_sent == received[-1].bytes_received == len(payload)
    assert all(a.bytes_received < b.bytes_received for a, b in itertools.pairwise(received))


def test_download_stream_close_and_cancellation_remove_staging(streamed_guest):
    import threading

    from daytona import DaytonaError

    sandbox, _, _, stages = streamed_guest
    sandbox.fs.upload_file(b"a" * 150000, "streamed")
    stream = sandbox.fs.download_file_stream("streamed")
    assert next(stream) == b"a" * 65536
    staged = stages[-1]
    assert staged.exists()
    stream.close()
    assert not staged.parent.exists()
    cancel = threading.Event()
    stream = sandbox.fs.download_file_stream("streamed", cancel_event=cancel)
    next(stream)
    cancel.set()
    with pytest.raises(DaytonaError, match="Download cancelled"):
        next(stream)
    assert not stages[-1].parent.exists()


def test_unknown_length_upload_reads_bounded_chunks_and_preserves_reader(streamed_guest):
    sandbox, _, _, _ = streamed_guest

    class Reader:
        def __init__(self):
            self.remaining = 200000
            self.calls = []

        def read(self, size):
            assert 0 < size <= 65536
            self.calls.append(size)
            data = b"z" * min(self.remaining, size)
            self.remaining -= len(data)
            return data

    source = Reader()
    sandbox.fs.upload_file_stream(source, "unknown")
    assert len(source.calls) > 2
    assert sandbox.fs.download_file("unknown") == b"z" * 200000


def test_stream_upload_cancel_does_not_replace_destination(streamed_guest):
    import io
    import threading

    from daytona import DaytonaError

    sandbox, _, _, stages = streamed_guest
    sandbox.fs.upload_file(b"original", "cancelled")
    cancel = threading.Event()
    source = io.BytesIO(b"a" * 200000)
    with pytest.raises(DaytonaError, match="Upload cancelled"):
        sandbox.fs.upload_file_stream(source, "cancelled", on_progress=lambda _: cancel.set(), cancel_event=cancel)
    assert not source.closed
    assert not stages[-1].parent.exists()
    assert sandbox.fs.download_file("cancelled") == b"original"


def test_async_streams_await_progress_and_close_on_early_exit(streamed_guest):
    import io

    from arker._daytona_filesystem import AsyncFileSystem

    sandbox, _, _, stages = streamed_guest
    fs = AsyncFileSystem(sandbox.fs._vm)

    async def scenario():
        progress = []

        async def receive(value):
            await asyncio.sleep(0)
            progress.append(value)

        source = io.BytesIO(b"q" * 150000)
        await fs.upload_file_stream(source, "async", on_progress=receive)
        assert not source.closed
        iterator = await fs.download_file_stream("async", on_progress=receive)
        assert await anext(iterator) == b"q" * 65536
        await iterator.aclose()
        assert not stages[-1].parent.exists()
        assert progress[-1].bytes_received == 65536

    asyncio.run(scenario())


def test_presigned_stream_closes_without_reading_remainder(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    import arker.computer as computer
    from arker.computer import VM

    events = []

    class Response:
        def raise_for_status(self):
            pass

        def iter_bytes(self, chunk_size):
            assert chunk_size == 65536
            events.append("read")
            yield b"first"
            pytest.fail("early close must not consume the remainder")

    @contextmanager
    def stream(*args, **kwargs):
        try:
            yield Response()
        finally:
            events.append("closed")

    monkeypatch.setattr(computer._http_client, "stream", stream)
    client = SimpleNamespace(
        _request=lambda *args, **kwargs: {
            "ok": True,
            "op": "read",
            "path": "/file",
            "size": 100000,
            "presigned_url": "https://storage.invalid/file",
            "expires_in": 900,
            "method": "GET",
        }
    )
    vm = VM(client, "vm-test", base_url="https://api.invalid")
    chunks = vm._sync_read_chunks("/file")
    assert next(chunks) == b"first"
    chunks.close()
    assert events == ["read", "closed"]


def test_stream_double_failure_retains_primary_cleanup_and_owned_stage():
    from arker._daytona_streams import staging

    primary = ValueError("transfer failed")
    cleanup = OSError("cleanup failed")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command.startswith("rm "):
            raise cleanup

    with pytest.raises(RuntimeError) as caught, staging(types.SimpleNamespace(_run=run)) as path:
        raise primary
    error = caught.value
    assert error.primary_error is primary
    assert error.cleanup_error is cleanup
    assert error.__cause__ is cleanup
    assert error.staging_directory == str(Path(path).parent)
    assert error.staging_directory in str(error)
    assert len(commands) == 2


def test_async_stream_double_cancel_drains_reader_before_cleanup():
    import threading

    from arker._daytona_streams import async_download

    started = threading.Event()
    release = threading.Event()
    reading = threading.Event()
    closed = threading.Event()
    cleaned = threading.Event()

    def chunks(path, **kwargs):
        try:
            reading.set()
            started.set()
            assert release.wait(5), "test must release blocked reader"
            reading.clear()
            yield b"chunk"
        finally:
            reading.clear()
            closed.set()

    def run(command, **kwargs):
        if command.startswith("rm "):
            assert not reading.is_set(), "cleanup raced the blocked reader"
            cleaned.set()

    fs = types.SimpleNamespace(
        _command=lambda *args: "read",
        _run=run,
        _vm=types.SimpleNamespace(_sync_read_chunks=chunks),
    )

    async def program():
        stream = async_download(fs, "file", None, None)
        task = asyncio.create_task(anext(stream))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=0.05)
            assert not done, "repeated cancellation must still wait for the gated reader"
            assert not cleaned.is_set()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert closed.is_set() and cleaned.is_set()

    asyncio.run(program())


def test_stream_uncertain_mkdir_reports_owned_candidate_without_blind_cleanup():
    from arker._daytona_streams import staging

    primary = OSError("lost mkdir response")
    calls = []

    def run(command):
        calls.append(command)
        raise primary

    with pytest.raises(OSError) as caught, staging(types.SimpleNamespace(_run=run)):
        pytest.fail("uncertain staging must not proceed")
    assert caught.value is primary
    assert primary.staging_directory.startswith("/tmp/.arker-daytona-fs-")
    assert primary.staging_directory in primary.__notes__[0]
    assert len(calls) == 1


def test_stream_owned_files_attempt_all_closes_and_retain_all_errors():
    from arker._daytona_streams import TransferCleanupError, transfer_files

    primary = DaytonaNotFoundError("native primary", status_code=404)
    first = OSError("first close")
    second = OSError("second close")
    calls = []

    def resource(name, error):
        def close():
            calls.append(name)
            raise error

        return types.SimpleNamespace(close=close)

    with pytest.raises(TransferCleanupError) as caught, transfer_files() as own:
        own(resource("first", first), "/owned/first")
        own(resource("second", second), "/owned/second")
        raise primary
    assert calls == ["second", "first"]
    assert caught.value.primary_error is primary
    assert caught.value.cleanup_errors == (second, first)
    assert "/owned/first" in str(caught.value) and "/owned/second" in str(caught.value)


def test_stream_success_cleanup_failure_preserves_identity_and_owned_stage():
    from arker._daytona_streams import staging

    cleanup = OSError("cleanup failed")
    calls = []

    def run(command):
        calls.append(command)
        if command.startswith("rm "):
            raise cleanup

    with pytest.raises(OSError) as caught, staging(types.SimpleNamespace(_run=run)) as path:
        pass
    assert caught.value is cleanup
    assert cleanup.staging_directory == str(Path(path).parent)
    assert cleanup.staging_directory in cleanup.__notes__[0]
    assert len(calls) == 2


def test_search_matches_native_literal_and_path_semantics(guest):
    fs = guest[0].fs
    fs.create_folder("search/nested", "700")
    fs.upload_file(b"hello TODO\nTODO.* literal\n", "search/a.txt")
    fs.upload_file(b"hello\n", "search/nested/b.py")
    assert fs.search_files("search", "[ab]*").files == ["search/a.txt", "search/nested/b.py"]
    assert fs.search_files("search", "[").files == []
    assert fs.search_files("search", "**").files == [
        "search",
        "search/a.txt",
        "search/nested",
        "search/nested/b.py",
    ]
    assert [(m.file, m.line, m.content) for m in fs.find_files("search", "TODO.*")] == [
        ("search/a.txt", 2, "TODO.* literal"),
    ]
    with pytest.raises(DaytonaBadRequestError):
        fs.search_files("search", "")


@pytest.mark.parametrize(
    ("pattern", "name", "expected"),
    [
        (r"[\]]", "]", True),
        (r"[\-]", "-", True),
        (r"[\\]", "\\", True),
        ("[z-a]", "z", False),
        ("[^z-a]", "a", True),
        ("[a-]", "a", False),
        ("[-a]", "a", False),
        ("[!a]", "!", True),
        ("[雪-雲]", "雪", True),
    ],
)
def test_search_glob_character_class_boundaries(pattern, name, expected):
    from arker._guest_daytona_filesystem import search_pattern

    matcher = search_pattern(pattern)
    assert bool(matcher and matcher.fullmatch(name)) is expected


@pytest.mark.parametrize("size", [65534, 65535, 65536])
@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"])
def test_search_scanner_token_boundary(tmp_path, size, ending):
    from arker._guest_daytona_filesystem import search

    file = tmp_path / "line"
    file.write_bytes(b"X" * size + ending)
    result = search(str(file), str(file), "X", True)
    accepted = size + len(ending) < 65536 or (size + len(ending) == 65536 and ending.endswith(b"\n"))
    assert [item["content"] for item in result] == (["X" * size] if accepted else [])


def test_search_invalid_utf8_replaces_each_invalid_byte(tmp_path):
    from arker._guest_daytona_filesystem import search

    file = tmp_path / "invalid"
    file.write_bytes(b"X\xe2\x82\n")
    assert search(str(file), str(file), "X", True)[0]["content"] == "X\ufffd\ufffd"


@pytest.mark.parametrize("operation", ["download", "upload"])
def test_transfer_timeout_is_client_only_and_reports_uncertain_stage(operation):
    import http.server
    import threading

    from daytona import DaytonaConnectionTimeoutError

    from arker.computer import Arker

    received = []
    release = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            release.wait(5)
            self.close_connection = True

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = Arker(api_key="test-only", base_url=f"http://127.0.0.1:{server.server_port}")
        fs = implementation.FileSystem(client.vm("vm-test"))
        with pytest.raises(DaytonaConnectionTimeoutError) as caught:
            if operation == "download":
                next(fs.download_file_stream("file", timeout=0.05))
            else:
                fs.upload_file_stream(b"data", "file", timeout=0.05)
        assert caught.value.staging_directory.startswith("/tmp/.arker-daytona-fs-")
        assert len(received) == 1
        path, body = received[0]
        assert path.endswith("/runs")
        assert "timeout" not in body
        assert "request_timeout" not in body
        assert "mkdir -m 700" in body["command"]
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize("timeout,expected", [(0, None), (0.125, 0.125), (2.5, 2.5)])
def test_stream_timeout_reaches_read_and_write_transport(timeout, expected):
    from arker._daytona_streams import Transfer
    from arker._request_timeout import transport_timeout

    calls = []
    fs = types.SimpleNamespace(
        _run=lambda command, **kw: calls.append(("run", {"request_timeout": transport_timeout(300)})),
        _vm=types.SimpleNamespace(
            _sync_read_chunks=lambda path, **kw: iter([b"data"]),
            _sync_write_chunks=lambda path, size, read, **kw: calls.append(
                ("write", {"request_timeout": transport_timeout(300)})
            ),
        ),
    )

    def chunks(path):
        calls.append(("read", {"request_timeout": transport_timeout(300)}))
        yield b"data"

    fs._vm._sync_read_chunks = chunks
    transfer = Transfer(fs, timeout)
    transfer.run("command")
    chunks = transfer.read("file")
    assert next(chunks) == b"data"
    assert transport_timeout(300) == 300
    chunks.close()
    transfer.write("file", 4, lambda offset, size: b"data")
    assert [operation for operation, _ in calls] == ["run", "read", "write"]
    for _, options in calls:
        assert options["request_timeout"].as_dict() == {
            "connect": 30.0,
            "pool": 60.0,
            "read": expected,
            "write": expected,
        }


@pytest.mark.parametrize("failed", [False, True])
def test_transfer_cleanup_ignores_timeout_scope_and_restores_caller(failed):
    import httpx2

    from arker._daytona_streams import staging
    from arker._request_timeout import request_timeout_scope, transport_timeout

    calls = []
    primary = OSError("transfer failed")
    fs = types.SimpleNamespace(_run=lambda command: calls.append((command, transport_timeout(300))))
    with request_timeout_scope(httpx2.Timeout(0.01)):
        try:
            with staging(fs):
                if failed:
                    raise primary
        except OSError as error:
            assert error is primary
        assert transport_timeout(300).read == 0.01
    assert calls[0][1].read == 0.01
    assert calls[-1][0].startswith("rm -rf -- ")
    assert calls[-1][1] == 300
    assert transport_timeout(300) == 300
