"""Bounded transfers through sync, with guest access under the Daytona user."""

import io
import os
import shlex
import tempfile
import uuid
from contextlib import contextmanager

CHUNK_SIZE = 64 * 1024


def cancelled(event, operation, path):
    from daytona.common.errors import DaytonaError

    if event is not None and event.is_set():
        raise DaytonaError(f"{operation} cancelled: {path}")


class TransferCleanupError(RuntimeError):
    def __init__(self, directory, primary, cleanup):
        super().__init__(f"Daytona transfer and staging cleanup failed for {directory}")
        self.staging_directory = directory
        self.primary_error = primary
        self.cleanup_errors = tuple(cleanup) if isinstance(cleanup, list) else (cleanup,)
        self.cleanup_error = self.cleanup_errors[0]


@contextmanager
def staging(fs):
    directory = "/tmp/.arker-daytona-fs-" + uuid.uuid4().hex
    try:
        fs._run("mkdir -m 700 -- " + shlex.quote(directory))
    except BaseException as error:
        # Without a successful mkdir receipt, deletion could remove a foreign path.
        error.staging_directory = directory
        error.__notes__ = [*getattr(error, "__notes__", ()), f"Staging creation outcome uncertain: {directory}"]
        raise
    try:
        yield directory + "/data"
    except BaseException as primary:
        try:
            fs._run("rm -rf -- " + shlex.quote(directory))
        except Exception as cleanup:
            raise TransferCleanupError(directory, primary, cleanup) from cleanup
        raise
    else:
        try:
            fs._run("rm -rf -- " + shlex.quote(directory))
        except BaseException as error:
            error.staging_directory = directory
            error.__notes__ = [*getattr(error, "__notes__", ()), f"Staging cleanup failed: {directory}"]
            raise


@contextmanager
def transfer_files():
    resources = []
    primary = None

    def own(stream, identity):
        resources.append((stream, identity))
        return stream

    try:
        yield own
    except BaseException as error:
        primary = error
        raise
    finally:
        errors = []
        for stream, _ in reversed(resources):
            try:
                stream.close()
            except BaseException as error:
                errors.append(error)
        if errors:
            identities = ", ".join(identity for _, identity in resources)
            raise TransferCleanupError(identities, primary, errors) from errors[-1]


def download(fs, path, progress, cancel):
    from daytona.common.filesystem import DownloadProgress

    command = fs._command("download", path)
    cancelled(cancel, "Download", path)
    with staging(fs) as staged:
        fs._run("umask 077; set -C; " + command + " > " + shlex.quote(staged))
        chunks = fs._vm._sync_read_chunks(staged)
        received = 0
        try:
            for chunk in chunks:
                cancelled(cancel, "Download", path)
                received += len(chunk)
                if progress is not None:
                    progress(DownloadProgress(bytes_received=received))
                yield chunk
        finally:
            chunks.close()


def upload(fs, source, path, progress, cancel):
    from daytona.common.errors import DaytonaError
    from daytona.common.filesystem import UploadProgress

    command = fs._command("upload", path)
    cancelled(cancel, "Upload", path)
    with transfer_files() as own:
        if isinstance(source, (bytes, bytearray)):
            stream = own(io.BytesIO(source), "in-memory upload source")
        elif isinstance(source, str):
            stream = own(open(source, "rb"), source)  # noqa: SIM115 - transfer_files owns and reports every close
        elif hasattr(source, "read"):
            stream = source
        else:
            raise DaytonaError(f"Unsupported upload source: {type(source).__name__}")
        # Sync chunks require a total size. Seekable inputs transfer directly;
        # unknown-length readers spool to disk without retaining the file in RAM.
        try:
            start = stream.tell()
            stream.seek(0, os.SEEK_END)
            size = stream.tell() - start
            stream.seek(start)
        except (AttributeError, OSError, io.UnsupportedOperation):
            spool = tempfile.TemporaryFile()  # noqa: SIM115 - registered with transfer_files below
            own(spool, f"anonymous upload spool fd={spool.fileno()}")
            while True:
                cancelled(cancel, "Upload", path)
                chunk = stream.read(CHUNK_SIZE)
                if not chunk:
                    break
                spool.write(chunk)
            size = spool.tell()
            spool.seek(0)
            stream, start = spool, 0
        sent = 0

        def read(offset, length):
            nonlocal sent
            cancelled(cancel, "Upload", path)
            stream.seek(start + offset)
            chunks = []
            remaining = length
            while remaining:
                cancelled(cancel, "Upload", path)
                chunk = stream.read(remaining)
                if not chunk:
                    raise DaytonaError("Upload source ended before its declared size")
                chunks.append(chunk)
                remaining -= len(chunk)
                sent += len(chunk)
                if progress is not None:
                    progress(UploadProgress(bytes_sent=sent))
            return b"".join(chunks)

        with staging(fs) as staged:
            fs._run("umask 077; set -C; : > " + shlex.quote(staged))
            fs._vm._sync_write_chunks(staged, size, read)
            cancelled(cancel, "Upload", path)
            fs._run(command + " < " + shlex.quote(staged))


class AsyncCancellation:
    def __init__(self, external):
        import threading

        self.local = threading.Event()
        self.external = external

    def is_set(self):
        return self.local.is_set() or (self.external is not None and self.external.is_set())


async def offload(function, cancellation):
    import asyncio

    from ._compat_async import _drain

    task = asyncio.create_task(asyncio.to_thread(function))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancellation.local.set()
        # The iterator must not be closed while its worker still reads it.
        try:
            await _drain(task)
        except Exception as error:
            raise asyncio.CancelledError from error
        raise


def progress_on_loop(callback):
    import asyncio
    import inspect

    if callback is None:
        return None
    loop = asyncio.get_running_loop()

    async def invoke(value):
        result = callback(value)
        if inspect.isawaitable(result):
            await result

    return lambda value: asyncio.run_coroutine_threadsafe(invoke(value), loop).result()


def async_download(fs, path, progress, cancel):
    cancellation = AsyncCancellation(cancel)
    iterator = download(fs, path, progress_on_loop(progress), cancellation)
    end = object()

    async def iterate():
        try:
            while True:
                chunk = await offload(lambda: next(iterator, end), cancellation)
                if chunk is end:
                    break
                yield chunk
        finally:
            await offload(iterator.close, cancellation)

    return iterate()


async def async_upload(fs, source, path, progress, cancel):
    cancellation = AsyncCancellation(cancel)
    callback = progress_on_loop(progress)
    await offload(lambda: upload(fs, source, path, callback, cancellation), cancellation)
