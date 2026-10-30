"""Guest implementation of Modal's sandbox filesystem command wire format."""

from __future__ import annotations

import contextlib
import errno
import grp
import json
import os
import pwd
import shutil
import stat
import sys
import tempfile


def info(path):
    value = os.lstat(path)
    kind = "symlink" if stat.S_ISLNK(value.st_mode) else "directory" if stat.S_ISDIR(value.st_mode) else "file"
    try:
        owner = pwd.getpwuid(value.st_uid).pw_name
    except KeyError:
        owner = str(value.st_uid)
    try:
        group = grp.getgrgid(value.st_gid).gr_name
    except KeyError:
        group = str(value.st_gid)
    return {
        "name": os.path.basename(path.rstrip("/")),
        "path": path,
        "type": kind,
        "size": value.st_size,
        "mode": value.st_mode,
        "permissions": format(stat.S_IMODE(value.st_mode), "04o"),
        "owner": owner,
        "group": group,
        "modified_time": value.st_mtime,
        "symlink_target": os.readlink(path) if kind == "symlink" else None,
    }


def is_directory(path):
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def main(command):
    operation, options = next(iter(command.items()))
    path = options["path"]
    if operation == "ReadFile":
        with open(path, "rb") as source:
            shutil.copyfileobj(source, sys.stdout.buffer)
    elif operation == "WriteFile":
        if is_directory(path):
            raise IsADirectoryError(errno.EISDIR, "Destination is a directory", path)
        parent = os.path.dirname(path)
        os.makedirs(parent, exist_ok=True)
        # Until EOF the upload has no pathname and cannot replace visible data.
        with tempfile.TemporaryFile(dir=parent) as staged:
            shutil.copyfileobj(sys.stdin.buffer, staged)
            staged.seek(0)
            descriptor, pending = tempfile.mkstemp(prefix=".arker-modal-upload-", dir=parent)
            try:
                with os.fdopen(descriptor, "wb") as target:
                    mask = os.umask(0)
                    os.umask(mask)
                    os.fchmod(target.fileno(), 0o666 & ~mask)
                    shutil.copyfileobj(staged, target)
                    target.flush()
                    os.fsync(target.fileno())
                try:
                    os.replace(pending, path)
                except OSError as error:
                    if is_directory(path):
                        raise IsADirectoryError(errno.EISDIR, "Destination is a directory", path) from error
                    raise
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(pending)
    elif operation == "Stat":
        print(json.dumps(info(path)))
    elif operation == "ListFiles":
        print(json.dumps([info(os.path.join(path, name)) for name in sorted(os.listdir(path))]))
    elif operation == "MakeDirectory":
        if options.get("parents", True):
            os.makedirs(path, exist_ok=True)
        else:
            os.mkdir(path)
    elif operation == "Remove":
        if os.path.isdir(path) and not os.path.islink(path):
            if options.get("recursive", False):
                shutil.rmtree(path)
            else:
                os.rmdir(path)
        else:
            os.unlink(path)
    else:
        raise ValueError("Unsupported filesystem operation")


if __name__ == "__main__":
    command = json.loads(sys.argv[1])
    try:
        main(command)
    except OSError as error:
        kind = {
            errno.ENOENT: "NotFound",
            errno.ENOTDIR: "NotDirectory",
            errno.EISDIR: "IsDirectory",
            errno.EACCES: "PermissionDenied",
            errno.EPERM: "PermissionDenied",
            errno.ENOTEMPTY: "DirectoryNotEmpty",
            errno.EEXIST: "AlreadyExists" if "WriteFile" in command else "PathAlreadyExists",
        }.get(error.errno, "Other")
        print(json.dumps({"error_kind": kind, "message": str(error)}), file=sys.stderr)
        sys.exit(1)
