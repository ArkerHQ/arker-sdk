"""Root-only Blaxel daemon startup; private port transport is the only ingress."""

import fcntl
import http.client
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import _guest_e2b_envd as security

ROOT = Path("/root/.arker/blaxel")
STATE = ROOT / "state.json"


def initial_configuration(path=Path("/etc/arker/image-config.json")):
    """Use the resolved image environment only when creating daemon state."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
    except FileNotFoundError:
        return {"envs": {}, "user": ""}
    try:
        if len(raw) > 1024 * 1024:
            raise ValueError()
        image = json.loads(raw)
        entries = image.get("env", [])
        if not isinstance(entries, list) or any(
            not isinstance(entry, list)
            or len(entry) != 2
            or not all(isinstance(value, str) and "\0" not in value for value in entry)
            for entry in entries
        ):
            raise ValueError()
        return {"envs": dict(entries), "user": ""}
    except (ValueError, TypeError, AttributeError):
        raise security.BootstrapError("invalid_image_environment") from None


def startup_configuration(explicit, state):
    if explicit is not None:
        return explicit
    # Named reuse and reconnect retain the configuration of the winning creation.
    return state["configuration"] if state else initial_configuration()


def ensure(request_path):
    if os.geteuid() != 0 or request_path.parent != ROOT / "requests":
        raise security.BootstrapError("invalid_request")
    config = security.load(request_path)
    request_path.unlink()
    lock = security.private_open(ROOT / "lock", os.O_CREAT | os.O_RDWR)
    child = None
    candidate = None
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Port tunnels enter via root-owned loopback. Keep both external and
        # unprivileged local traffic blocked even after readiness.
        security.Isolation().block()
        manifest = security.load(ROOT / "manifest.json")
        entry = manifest["artifacts"][security.architecture()]
        target = ROOT / "bin" / entry["sha256"]
        candidate = Path(config["candidate"]) if config.get("candidate") else None
        if candidate is not None:
            if candidate.parent != ROOT / "bin" or not candidate.name.startswith(".candidate-"):
                raise security.BootstrapError("invalid_candidate")
            if security.digest(candidate) != entry["sha256"]:
                raise security.BootstrapError("artifact_hash_mismatch")
            if not target.exists():
                candidate.chmod(0o700)
                os.replace(candidate, target)
        if security.digest(target) != entry["sha256"]:
            raise security.BootstrapError("artifact_hash_mismatch")
        state = security.load(STATE) if STATE.exists() else None
        desired = config.get("configuration")
        if state and security.same_process(state):
            if state["executable"] != str(target) or not security.owns_listener(state):
                raise security.BootstrapError("daemon_identity_mismatch")
            if desired is not None and desired != state["configuration"]:
                raise security.BootstrapError("running_configuration_conflict")
            reused = True
        else:
            desired = startup_configuration(desired, state)
            envs = desired["envs"]
            if not isinstance(envs, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in envs.items()
            ):
                raise security.BootstrapError("invalid_environment")
            environment = {
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "HOME": "/blaxel",
                "SHELL": "/bin/bash",
                **envs,
                "DISABLE_REQUEST_LOGGING": "true",
            }
            # Configuration is a daemon environment, never an SDK credential map.
            if any(k.startswith(("ARKER_", "BL_API_", "BL_CLIENT_")) for k in environment):
                raise security.BootstrapError("control_credentials_forbidden")
            home = environment["HOME"]
            if not home or not Path(home).is_absolute():
                raise security.BootstrapError("invalid_home")
            Path(home).mkdir(parents=True, exist_ok=True)
            command = [str(target), "-port", "49983", "-disable-telemetry"]
            if desired["user"]:
                command.extend(["-user", desired["user"]])
            with os.fdopen(
                security.private_open(ROOT / "daemon.log", os.O_CREAT | os.O_WRONLY | os.O_APPEND), "ab"
            ) as log:
                child = subprocess.Popen(
                    command,
                    cwd=home,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            state = {**security.fingerprint(child.pid), "configuration": desired}
            reused = False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if security.owns_listener(state):
                connection = http.client.HTTPConnection("127.0.0.1", 49983, timeout=1)
                try:
                    connection.connect()
                    # TCP connect can complete before the daemon accepts its socket.
                    while not security.connected_peer_owned(connection, state):
                        if not security.same_process(state) or time.monotonic() >= deadline:
                            raise security.BootstrapError("daemon_peer_mismatch")
                        time.sleep(0.01)
                    connection.request("GET", "/health")
                    if connection.getresponse().status == 200:
                        security.save(STATE, {**state, "vm_id": config["vm_id"]})
                        return {
                            "ready": True,
                            "reused": reused,
                            "version": manifest["version"],
                            "arch": security.architecture(),
                        }
                finally:
                    connection.close()
            if child and child.poll() is not None:
                break
            time.sleep(0.05)
        raise security.BootstrapError("daemon_not_ready")
    except BaseException:
        if child is not None:
            child.kill()
            child.wait(timeout=5)
        raise
    finally:
        if candidate is not None:
            candidate.unlink(missing_ok=True)
        os.close(lock)


def main():
    try:
        for directory in (ROOT, ROOT / "bin", ROOT / "requests"):
            security.private_directory(directory)
        if sys.argv[1] == "prepare":
            manifest = security.load(ROOT / "manifest.json")
            entry = manifest["artifacts"][security.architecture()]
            target = ROOT / "bin" / entry["sha256"]
            verified = target.exists() and security.digest(target) == entry["sha256"]
            result = {"arch": security.architecture(), "verified": verified}
        else:
            result = ensure(Path(sys.argv[2]))
        print(json.dumps(result))
    except Exception as error:
        code = str(error) if isinstance(error, security.BootstrapError) else type(error).__name__
        print(json.dumps({"ready": False, "error": code}))
        sys.exit(1)


if __name__ == "__main__":
    main()
