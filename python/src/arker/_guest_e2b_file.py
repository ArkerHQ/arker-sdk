"""Buffered E2B file I/O, executed under the caller-selected guest identity."""

import os
import shutil
import stat
import sys


def main(operation, path):
    writing = operation == "write"
    if writing:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    # Check the opened descriptor before truncation; a FIFO must never block.
    flags = os.O_NONBLOCK | (os.O_WRONLY | os.O_CREAT if writing else os.O_RDONLY)
    fd = os.open(path, flags, 0o666)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("E2B buffered file I/O requires a regular file")
        if writing:
            os.ftruncate(fd, 0)
        with os.fdopen(fd, "wb" if writing else "rb", closefd=False) as stream:
            if writing:
                shutil.copyfileobj(sys.stdin.buffer, stream)
            else:
                shutil.copyfileobj(stream, sys.stdout.buffer)
    finally:
        os.close(fd)


if __name__ == "__main__":
    main(*sys.argv[1:])
