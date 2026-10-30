import contextlib
import json
import os
import re
import stat
import sys

root = "/root"
directory = root + "/.arker-modal"
operation = sys.argv[1]
name = sys.argv[2] if len(sys.argv) > 2 else "defaults.json"
if name != "defaults.json" and not re.fullmatch(r"exec-[0-9a-f]{32}\.json", name):
    raise ValueError("Invalid Modal control filename")
for location in (root, directory):
    if location == directory and operation == "prepare":
        with contextlib.suppress(FileExistsError):
            os.mkdir(location, 0o700)
    info = os.lstat(location)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise RuntimeError("Unsafe Modal defaults directory")
path = directory + "/" + name


def private_open(filename):
    fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o077:
        os.close(fd)
        raise RuntimeError("Unsafe Modal private file")
    return os.fdopen(fd)


if operation == "prepare":
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
elif operation == "remove":
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
elif operation == "exec":
    if name == "defaults.json":
        raise ValueError("Exec requires a private control file")
    try:
        with private_open(path) as handle:
            control = json.load(handle)
    finally:
        os.unlink(path)
    defaults = {}
    if control["defaults"]:
        with private_open(directory + "/defaults.json") as handle:
            defaults = json.load(handle)
    environment = {**os.environ, **defaults.get("env", {}), **control["env"]}
    workdir = control["workdir"] if control["workdir"] is not None else defaults.get("workdir", "/")
    try:
        os.chdir(workdir)
        os.execvpe(control["argv"][0], control["argv"], environment)
    except OSError as error:
        print(str(error), file=sys.stderr)
        sys.exit(128)
else:
    # Sync may replace the prepared inode. Validate its new inode before sealing.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise RuntimeError("Unsafe Modal defaults file") from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
            raise RuntimeError("Unsafe Modal defaults file")
        if operation == "seal":
            os.fchmod(fd, 0o600)
        elif info.st_mode & 0o077:
            raise RuntimeError("Unsafe Modal defaults mode")
    finally:
        os.close(fd)
