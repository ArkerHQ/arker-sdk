"""Root-only native envd bootstrap shared by SDK clients.

Run ``prepare VM_ID`` before syncing the manifest into root_path and a unique
request file into request_directory, both with is_secret=True. Deliver
{"vm_id": ..., "token": ...} using secret sync, never command text or environment
variables. For offline guests, sync the architecture-pinned binary to
root_path/bin/.candidate-<uuid> with mode 0700 and pass artifact_path in the
request. Ensure consumes the candidate under its lock, preserving an existing
verified executable inode. ``ensure REQUEST``
consumes that file and emits only non-secret readiness metadata. Reconnect reads
state_path over the authenticated file API and supplies its existing token.

The caller must retain private, owner-authenticated ingress on port 49983. This
helper never changes API policies or receives an Arker API key. Only approved
sources within the same full trust boundary may inherit an initialized daemon:
reconnect and fork retain its token and process registry, not sanitize memory.
Do not use this helper to clone an initialized VM across a trust boundary.
Root is trusted; other local users are isolated before startup and permanently.
The initialization INPUT guard covers non-loopback traffic; localhost ingress
continues to depend on the caller retaining owner-authenticated API policies.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import http.client
import json
import os
import platform
import pwd
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path("/root/.arker/e2b-envd")
PORT = 49983
MAX_ARTIFACT = 128 * 1024 * 1024
READINESS_SECONDS = 30
MANIFEST_NAME = "_guest_e2b_envd_manifest.json"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BootstrapError(Exception):
    """A stable, secret-free bootstrap failure code."""


def private_directory(path):
    for parent in reversed((path, *path.parents)):
        if not parent.exists():
            parent.mkdir(mode=0o700)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise BootstrapError("unsafe_state_directory")
    path.chmod(0o700)


def private_open(path, flags=os.O_RDONLY):
    fd = os.open(path, (flags & ~os.O_TRUNC) | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1:
        os.close(fd)
        raise BootstrapError("unsafe_state_file")
    if flags & os.O_TRUNC:
        os.ftruncate(fd, 0)
    return fd


def load(path):
    with os.fdopen(private_open(path)) as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise BootstrapError("invalid_state")
    return value


def save(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def vm_directory(vm_id):
    if not isinstance(vm_id, str) or not vm_id or len(vm_id) > 256 or "\0" in vm_id:
        raise BootstrapError("invalid_vm_id")
    return ROOT / "vms" / hashlib.sha256(vm_id.encode()).hexdigest()


def architecture():
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if arch is None:
        raise BootstrapError("unsupported_artifact")
    return arch


def prepare(vm_id):
    if os.geteuid() != 0:
        raise BootstrapError("root_required")
    directory = vm_directory(vm_id)
    for path in (ROOT, ROOT / "requests", ROOT / "bin", directory):
        private_directory(path)
    active_path = ROOT / "active.json"
    active = load(active_path) if active_path.exists() else None
    inherited_state = vm_directory(active["vm_id"]) / "state.json" if active else None
    return {
        "root_path": str(ROOT),
        "arch": architecture(),
        "request_directory": str(ROOT / "requests"),
        "state_path": str(directory / "state.json"),
        "port": PORT,
        "existing_state_path": str(inherited_state) if inherited_state else None,
    }


def run(args):
    try:
        return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BootstrapError("isolation_command_failed") from error


class Isolation:
    """Own only exact port-specific rules; never flush or replace a rule chain."""

    external = (
        "INPUT",
        "!",
        "-i",
        "lo",
        "-p",
        "tcp",
        "--dport",
        str(PORT),
        "-m",
        "comment",
        "--comment",
        "arker-envd-initializing",
        "-j",
        "DROP",
    )
    local = (
        "OUTPUT",
        "-o",
        "lo",
        "-p",
        "tcp",
        "--dport",
        str(PORT),
        "-m",
        "owner",
        "!",
        "--uid-owner",
        "0",
        "-m",
        "comment",
        "--comment",
        "arker-envd-local-owner",
        "-j",
        "DROP",
    )

    def block(self):
        for tool in ("iptables", "ip6tables"):
            for rule in (self.external, self.local):
                # Always prepend: an existing copy may be below an ACCEPT rule.
                if run([tool, "-w", "2", "-I", rule[0], "1", *rule[1:]]).returncode:
                    raise BootstrapError("isolation_unavailable")
                listed = run([tool, "-w", "2", "-S", rule[0]])
                if listed.returncode:
                    raise BootstrapError("isolation_unavailable")
                rules = [shlex.split(line) for line in listed.stdout.decode().splitlines() if line.startswith("-A ")]
                if not rules or run([tool, "-w", "2", "-C", *rule]).returncode:
                    raise BootstrapError("isolation_unavailable")
                # Remove older exact copies, preserving the newly inserted guard.
                # Use the tool's canonical representation for normalized options.
                for index in range(len(rules) - 1, 0, -1):
                    if rules[index] == rules[0] and run([tool, "-w", "2", "-D", rule[0], str(index + 1)]).returncode:
                        raise BootstrapError("isolation_unavailable")

    def release_external(self):
        for tool in ("iptables", "ip6tables"):
            if run([tool, "-w", "2", "-D", *self.external]).returncode:
                raise BootstrapError("isolation_release_failed")
        # Non-root local access stays blocked, including across a memory fork.


def fingerprint(pid):
    process = Path("/proc") / str(pid)
    info = process.stat()
    if info.st_uid != 0:
        raise BootstrapError("daemon_identity_mismatch")
    return {
        "pid": pid,
        "start": (process / "stat").read_text().rsplit(")", 1)[1].split()[19],
        "executable": os.readlink(process / "exe"),
    }


def same_process(record):
    try:
        return fingerprint(record["pid"]) == {key: record[key] for key in ("pid", "start", "executable")}
    except (FileNotFoundError, ProcessLookupError):
        return False


def owns_listener(record):
    if not same_process(record):
        return False
    sockets = set()
    try:
        for entry in (Path("/proc") / str(record["pid"]) / "fd").iterdir():
            try:
                target = os.readlink(entry)
            except FileNotFoundError:
                continue
            if target.startswith("socket:["):
                sockets.add(target[8:-1])
        listeners = set()
        for table in ("tcp", "tcp6"):
            for line in (Path("/proc/net") / table).read_text().splitlines()[1:]:
                fields = line.split()
                if fields[3] == "0A" and fields[1].rsplit(":", 1)[1] == f"{PORT:04X}":
                    listeners.add(fields[9])
        return bool(listeners) and listeners <= sockets and same_process(record)
    except (OSError, IndexError):
        return False


def artifact(manifest):
    arch = architecture()
    if manifest.get("port") != PORT or manifest.get("version") != "0.10.2":
        raise BootstrapError("unsupported_artifact")
    entry = manifest["artifacts"][arch]
    prefix = "https://storage.googleapis.com/e2b-artifact-binaries/envd/v0.10.2/"
    suffix = "arm64/envd" if arch == "arm64" else "envd"
    if entry["url"] != prefix + suffix or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
        raise BootstrapError("invalid_artifact_pin")
    return arch, entry


def digest(path):
    result = hashlib.sha256()
    with os.fdopen(private_open(path), "rb") as stream:
        total = 0
        while chunk := stream.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_ARTIFACT:
                raise BootstrapError("artifact_download_limit")
            result.update(chunk)
    return result.hexdigest()


def candidate_path(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise BootstrapError("invalid_artifact_path")
    path = Path(value)
    identifier = r"(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})"
    if path.parent != ROOT / "bin" or not re.fullmatch(r"\.candidate-" + identifier, path.name):
        raise BootstrapError("invalid_artifact_path")
    return path


def verify(vm_id):
    prepare(vm_id)
    arch, entry = artifact(load(ROOT / MANIFEST_NAME))
    try:
        verified = digest(ROOT / "bin" / entry["sha256"]) == entry["sha256"]
    except FileNotFoundError:
        verified = False
    return {"verified": verified, "arch": arch}


def install(entry, candidate=None, *, offline=False):
    target = ROOT / "bin" / entry["sha256"]
    if candidate is not None and digest(candidate) != entry["sha256"]:
        raise BootstrapError("artifact_hash_mismatch")
    if target.exists():
        if digest(target) != entry["sha256"]:
            raise BootstrapError("artifact_hash_mismatch")
        return target
    if candidate is not None:
        candidate.chmod(0o700)
        os.replace(candidate, target)
        return target
    if offline:
        raise BootstrapError("artifact_missing")
    fd, temporary = tempfile.mkstemp(dir=target.parent)
    deadline = time.monotonic() + 60
    try:
        with os.fdopen(fd, "wb") as output, urllib.request.urlopen(entry["url"], timeout=10) as source:
            total = 0
            while chunk := source.read(512 * 1024):
                total += len(chunk)
                if total > MAX_ARTIFACT or time.monotonic() > deadline:
                    raise BootstrapError("artifact_download_limit")
                output.write(chunk)
        if digest(Path(temporary)) != entry["sha256"]:
            raise BootstrapError("artifact_hash_mismatch")
        os.chmod(temporary, 0o700)
        os.replace(temporary, target)
        return target
    finally:
        Path(temporary).unlink(missing_ok=True)


def connected_peer_owned(connection, record):
    """Verify the accepted peer of this connection before sending any secret."""
    if not same_process(record):
        return False
    client_port = connection.sock.getsockname()[1]
    try:
        sockets = {
            os.readlink(entry)[8:-1]
            for entry in (Path("/proc") / str(record["pid"]) / "fd").iterdir()
            if os.readlink(entry).startswith("socket:[")
        }
        for table in ("tcp", "tcp6"):
            for line in (Path("/proc/net") / table).read_text().splitlines()[1:]:
                fields = line.split()
                if (
                    fields[3] == "01"
                    and fields[1].rsplit(":", 1)[0] in ("0100007F", "0000000000000000FFFF00000100007F")
                    and fields[2].rsplit(":", 1)[0] in ("0100007F", "0000000000000000FFFF00000100007F")
                    and fields[1].rsplit(":", 1)[1] == f"{PORT:04X}"
                    and fields[2].rsplit(":", 1)[1] == f"{client_port:04X}"
                    and fields[9] in sockets
                ):
                    return same_process(record)
        return False
    except (OSError, IndexError):
        return False


def request(path, token=None, body=None, record=None):
    headers = {"X-Access-Token": token} if token else {}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if token is not None or body is not None:
        if record is None:
            raise BootstrapError("daemon_identity_required")
        connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=1)
        try:
            connection.connect()
            deadline = time.monotonic() + 1
            while not connected_peer_owned(connection, record):
                if time.monotonic() >= deadline:
                    raise BootstrapError("connected_peer_identity_mismatch")
                time.sleep(0.01)
            # HTTPConnection must use this already-connected socket; never retry.
            connection.request("POST" if data is not None else "GET", path, body=data, headers=headers)
            response = connection.getresponse()
            return response.status, response.read(4096)
        finally:
            connection.close()
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, headers=headers)
    # A customer's proxy environment must never receive bootstrap credentials.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=1) as response:
            return response.status, response.read(4096)
    except urllib.error.HTTPError as error:
        return error.code, b""


def authenticated(token, marker, record):
    path = "/files?" + urllib.parse.urlencode({"path": str(marker)})
    status, data = request(path, token, record=record)
    if status != 200 or data != marker.read_bytes():
        return False
    return request(path)[0] in (401, 403) and request(path, "invalid-bootstrap-token", record=record)[0] in (401, 403)


def ensure_user():
    try:
        user = pwd.getpwnam("user")
    except KeyError:
        if run(["useradd", "--create-home", "--shell", "/bin/bash", "user"]).returncode:
            raise BootstrapError("user_creation_failed") from None
        user = pwd.getpwnam("user")
    if user.pw_uid == 0 or user.pw_dir != "/home/user":
        raise BootstrapError("unsupported_user_home")


def ensure(request_path):
    if os.geteuid() != 0:
        raise BootstrapError("root_required")
    private_directory(ROOT)
    if request_path.parent != ROOT / "requests":
        raise BootstrapError("invalid_request_path")
    private_directory(request_path.parent)
    config = load(request_path)
    request_path.unlink()
    candidate = candidate_path(config.get("artifact_path"))
    lock = None
    try:
        vm_id, token = config.get("vm_id"), config.get("token")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
            raise BootstrapError("invalid_token")
        if "envs" in config and (
            not isinstance(config["envs"], dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in config["envs"].items())
        ):
            raise BootstrapError("invalid_envs")
        paths = prepare(vm_id)
        state_path = Path(paths["state_path"])
        lock = private_open(ROOT / "lock", os.O_RDWR | os.O_CREAT)
        deadline = time.monotonic() + READINESS_SECONDS
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BootstrapError("bootstrap_busy") from None
                time.sleep(0.05)
        return start(config, state_path)
    finally:
        try:
            if candidate is not None:
                candidate.unlink(missing_ok=True)
        finally:
            if lock is not None:
                os.close(lock)


def start(config, state_path):
    vm_id, token = config["vm_id"], config["token"]
    isolation = Isolation()
    process = None
    ready = False
    try:
        isolation.block()
        arch, entry = artifact(load(ROOT / MANIFEST_NAME))
        binary = install(entry, candidate_path(config.get("artifact_path")), offline=config.get("offline") is True)
        ensure_user()
        active_path = ROOT / "active.json"
        active = load(active_path) if active_path.exists() else None
        previous = load(state_path) if state_path.exists() else None
        if previous and (previous["vm_id"] != vm_id or not hmac.compare_digest(previous["token"], token)):
            raise BootstrapError("reconnect_token_mismatch")
        inherited_envs = previous.get("envs") if previous else None
        if active and inherited_envs is None:
            inherited_envs = load(vm_directory(active["vm_id"]) / "state.json").get("envs")
        envs = config.get("envs", inherited_envs)
        traffic_token = config.get("traffic_access_token", previous.get("traffic_access_token") if previous else None)
        if traffic_token is not None and (
            not isinstance(traffic_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", traffic_token)
        ):
            raise BootstrapError("invalid_traffic_token")
        if previous and previous.get("traffic_access_token") != traffic_token:
            raise BootstrapError("reconnect_traffic_token_mismatch")
        saved = {"vm_id": vm_id, "token": token}
        if traffic_token is not None:
            saved["traffic_access_token"] = traffic_token
        if envs is not None:
            saved["envs"] = envs
        marker = state_path.parent / "ready.json"
        save(marker, {"vm_id": vm_id})
        if active:
            if active["executable"] != str(binary):
                raise BootstrapError("daemon_identity_mismatch")
            source = load(vm_directory(active["vm_id"]) / "state.json")
            if not hmac.compare_digest(source["token"], token):
                raise BootstrapError("live_daemon_token_mismatch")
            if same_process(active):
                if owns_listener(active) and authenticated(token, marker, active):
                    if "envs" in config:
                        status, _ = request(
                            "/init", token=token, body={"accessToken": token, "envVars": envs}, record=active
                        )
                        if status != 204:
                            raise BootstrapError("environment_setup_failed")
                    save(state_path, saved)
                    save(active_path, {**active, "vm_id": vm_id})
                    isolation.release_external()
                    ready = True
                    return {
                        "ready": True,
                        "reused": True,
                        "port": PORT,
                        "state_path": str(state_path),
                        "version": "0.10.2",
                        "arch": arch,
                    }
                raise BootstrapError("live_daemon_authentication_failed")
        save(state_path, saved)
        log_path = ROOT / "daemon.log"
        log_fd = private_open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        with os.fdopen(log_fd, "wb") as log:
            process = subprocess.Popen(
                [str(binary), "-isnotfc", "-no-cgroups", "-port", str(PORT)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
                env={
                    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                    "HOME": "/root",
                    "LANG": "C.UTF-8",
                    "ENVD_DEFAULT_USER": "user",
                },
            )
        record = {**fingerprint(process.pid), "vm_id": vm_id}
        save(active_path, record)
        deadline = time.monotonic() + READINESS_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise BootstrapError("daemon_exited")
            try:
                if owns_listener(record) and 200 <= request("/health")[0] < 300:
                    break
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.05)
        else:
            raise BootstrapError("readiness_timeout")
        initialization = {"accessToken": token, "defaultUser": "user", "defaultWorkdir": "/home/user"}
        if envs is not None:
            initialization["envVars"] = envs
        status, _ = request("/init", body=initialization, record=record)
        if status != 204 or not authenticated(token, marker, record):
            raise BootstrapError("authentication_setup_failed")
        isolation.release_external()
        ready = True
        return {
            "ready": True,
            "reused": False,
            "port": PORT,
            "state_path": str(state_path),
            "version": "0.10.2",
            "arch": arch,
        }
    finally:
        if not ready:
            # Restore a closed gate if only one family's release succeeded.
            try:
                isolation.block()
            finally:
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)


def main():
    try:
        if len(sys.argv) != 3:
            raise BootstrapError("invalid_arguments")
        if sys.argv[1] == "prepare":
            result = prepare(sys.argv[2])
        elif sys.argv[1] == "verify":
            result = verify(sys.argv[2])
        elif sys.argv[1] == "ensure":
            result = ensure(Path(sys.argv[2]))
        else:
            raise BootstrapError("invalid_arguments")
        print(json.dumps(result))
    except Exception as error:
        # Never expose request data, tokens, daemon logs or HTTP error bodies.
        print(
            json.dumps(
                {"ready": False, "error": str(error) if isinstance(error, BootstrapError) else "bootstrap_failed"}
            )
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
