"""Daytona filesystem operations performed under the guest's Daytona account."""

import datetime
import errno
import grp
import json
import os
import pwd
import re
import shutil
import stat
import sys


def identity():
    try:
        account = pwd.getpwnam("daytona")
    except KeyError as error:
        raise ValueError("Daytona filesystem requires an existing non-root daytona account") from error
    if account.pw_uid == 0 or os.geteuid() != account.pw_uid:
        raise PermissionError("Daytona filesystem requires its non-root guest account")
    os.environ.update(HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name)
    os.chdir(account.pw_dir)


def path(value):
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("path must be a nonempty string without NUL")
    return os.path.abspath(value)


def mode(value):
    if not isinstance(value, str) or not value or any(c not in "01234567" for c in value):
        raise ValueError("invalid octal mode")
    result = int(value, 8)
    if result > 0o777:
        raise ValueError("special permission bits are not supported")
    return result


def mkdir_parents(target, permissions):
    try:
        os.mkdir(target, permissions)
    except FileNotFoundError:
        mkdir_parents(os.path.dirname(target), permissions)
        try:
            os.mkdir(target, permissions)
        except FileExistsError:
            if not os.path.isdir(target):
                raise
    except FileExistsError:
        if not os.path.isdir(target):
            raise


def info(target, include_path=False):
    value = os.stat(target)
    timestamp = (
        datetime.datetime.fromtimestamp(value.st_mtime, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    )
    seconds, nanos = divmod(value.st_mtime_ns, 1_000_000_000)
    date = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    fraction = ("." + f"{nanos:09d}".rstrip("0")) if nanos else ""
    result = {
        "name": os.path.basename(target) or "/",
        "size": value.st_size,
        "isDir": stat.S_ISDIR(value.st_mode),
        "mode": stat.filemode(value.st_mode),
        "modTime": date + fraction + " +0000 UTC",
        "modifiedAt": timestamp,
        "owner": str(value.st_uid),
        "group": str(value.st_gid),
        "permissions": format(stat.S_IMODE(value.st_mode) & 0o777, "04o"),
    }
    if include_path:
        result["path"] = target
    return result


def transfer(target, writing):
    if writing:
        mkdir_parents(os.path.dirname(target), 0o755)
    flags = os.O_NONBLOCK | (os.O_WRONLY | os.O_CREAT if writing else os.O_RDONLY)
    descriptor = os.open(target, flags, 0o666)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("buffered transfers require a regular file")
        if writing:
            os.ftruncate(descriptor, 0)
        with os.fdopen(descriptor, "wb" if writing else "rb", closefd=False) as stream:
            if writing:
                shutil.copyfileobj(sys.stdin.buffer, stream)
            else:
                shutil.copyfileobj(stream, sys.stdout.buffer)
    finally:
        os.close(descriptor)


def search_pattern(pattern):
    """Translate pathname glob syntax without interpreting content as a regex."""
    pieces = []
    index = 0
    while index < len(pattern):
        token = pattern[index]
        index += 1
        if token == "*":
            pieces.append("[^/]*")
        elif token == "?":
            pieces.append("[^/]")
        elif token == "\\":
            if index == len(pattern):
                return None
            pieces.append(re.escape(pattern[index]))
            index += 1
        elif token == "[":
            negated = index < len(pattern) and pattern[index] == "^"
            index += int(negated)
            ranges = []

            def character(position):
                if position >= len(pattern) or pattern[position] in "-]":
                    raise ValueError("invalid character class")
                if pattern[position] == "\\":
                    position += 1
                if position >= len(pattern) - 1:
                    raise ValueError("unterminated character class")
                return pattern[position], position + 1

            try:
                count = 0
                while True:
                    if index < len(pattern) and pattern[index] == "]" and count:
                        index += 1
                        break
                    low, index = character(index)
                    high = low
                    if pattern[index] == "-":
                        high, index = character(index + 1)
                    count += 1
                    if low <= high:
                        ranges.append(re.escape(low) + "-" + re.escape(high))
            except ValueError:
                return None
            pieces.append(
                "[" + ("^" if negated else "") + "".join(ranges) + "]"
                if ranges
                else ("[\\s\\S]" if negated else "(?!)")
            )
        else:
            pieces.append(re.escape(token))
    try:
        return re.compile("".join(pieces) + r"\Z")
    except re.error:
        return None


def search(target, requested, pattern, contents):
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("path and pattern are required")
    matcher = None if contents else search_pattern(pattern)
    matches = []

    def visit(current, display):
        try:
            value = os.lstat(current)
        except OSError:
            return
        if not contents and matcher is not None and matcher.fullmatch(os.path.basename(current)):
            matches.append(display)
        if stat.S_ISDIR(value.st_mode):
            try:
                names = sorted(os.listdir(current))
            except OSError:
                return
            for name in names:
                visit(os.path.join(current, name), os.path.join(display, name))
        elif contents and stat.S_ISREG(value.st_mode):
            try:
                with open(current, "rb") as stream:
                    if b"\0" in stream.read(512):
                        return
                    stream.seek(0)
                    for number, line in enumerate(iter(lambda: stream.readline(65536), b""), 1):
                        # The native scanner stops at its 64 KiB token limit.
                        if len(line) >= 65536 and not line.endswith(b"\n"):
                            break
                        text = line.rstrip(b"\n").removesuffix(b"\r").decode("utf-8", "surrogateescape")
                        text = "".join("\ufffd" if 0xDC80 <= ord(char) <= 0xDCFF else char for char in text)
                        if pattern in text:
                            matches.append({"file": display, "line": number, "content": text})
            except OSError:
                return

    visit(target, os.path.normpath(requested))
    return matches if contents else {"files": matches}


def main(request):
    identity()
    operation = request["operation"]
    if operation == "exec":
        if request.get("cwd") is not None:
            os.chdir(path(request["cwd"]))
        os.environ.update(request.get("env") or {})
        os.execvpe("/bin/sh", ["sh", "-c", request["command"]], os.environ)
    target = path(request["path"])
    if operation in {"upload", "download"}:
        transfer(target, operation == "upload")
        return
    result = None
    if operation in {"find", "search"}:
        result = search(target, request["path"], request.get("pattern"), operation == "find")
    elif operation == "info":
        result = info(target)
    elif operation == "list":
        depth = request.get("depth") or 1
        if type(depth) is not int or depth < 1:
            raise ValueError("depth must be a positive integer")
        result = []

        def visit(directory, remaining):
            for name in sorted(os.listdir(directory)):
                child = os.path.join(directory, name)
                try:
                    result.append(info(child, True))
                except OSError:
                    continue
                if remaining > 1 and os.path.isdir(child) and not os.path.islink(child):
                    visit(child, remaining - 1)

        visit(target, depth)
    elif operation == "mkdir":
        mkdir_parents(target, mode(request["mode"] or "755"))
    elif operation == "remove":
        value = os.stat(target)
        if stat.S_ISDIR(value.st_mode) and not request.get("recursive", False):
            raise ValueError("cannot delete directory without recursive flag")
        if stat.S_ISDIR(value.st_mode) and not os.path.islink(target):
            shutil.rmtree(target)
        else:
            os.unlink(target)
    elif operation == "move":
        os.rename(target, path(request["destination"]))
    elif operation == "permissions":
        os.stat(target)
        if request.get("mode"):
            os.chmod(target, mode(request["mode"]))
        owner, group = request.get("owner"), request.get("group")
        uid = int(owner) if owner and owner.isdecimal() else pwd.getpwnam(owner).pw_uid if owner else -1
        gid = int(group) if group and group.isdecimal() else grp.getgrnam(group).gr_gid if group else -1
        if owner or group:
            os.chown(target, uid, gid)
    else:
        raise ValueError("unsupported filesystem operation")
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main(json.loads(sys.argv[1]))
    except (OSError, ValueError, KeyError) as error:
        status = {errno.ENOENT: 404, errno.EACCES: 403, errno.EPERM: 403}.get(getattr(error, "errno", None), 400)
        print(json.dumps({"message": str(error), "status": status}), file=sys.stderr)
        sys.exit(1)
