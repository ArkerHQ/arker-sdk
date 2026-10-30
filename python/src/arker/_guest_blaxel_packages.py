"""Offline firewall prerequisites for a validated Alpine runtime profile.

The caller holds the bootstrap lock. Installation never admits the daemon.
"""

import hashlib
import io
import json
import os
import platform
import stat
import subprocess
import tarfile
import tempfile
import zlib
from pathlib import Path

ROOT = Path("/")
CACHE = Path("/root/.arker/blaxel/packages")
MANIFEST = json.loads(Path(__file__).with_suffix(".json").read_text())


def _directories(path, *, allow_missing=False):
    parts = path.relative_to(ROOT).parts
    current = ROOT
    for part in ("", *parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing:
                return
            raise
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError("Unsafe firewall package directory")


def _file(path):
    _directories(path.parent)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError("Firewall prerequisite file must be root-owned and immutable to other users")
    return path.read_bytes()


def _digest(path):
    return hashlib.sha256(_file(path)).hexdigest()


def _versions():
    versions = {}
    for record in (ROOT / "lib/apk/db/installed").read_text().split("\n\n"):
        fields = dict(line.split(":", 1) for line in record.splitlines() if ":" in line)
        if "P" in fields:
            versions[fields["P"]] = fields.get("V")
    return versions


def _payload(archive):
    remaining = _file(archive)
    while remaining:
        decoder = zlib.decompressobj(31)
        payload = decoder.decompress(remaining) + decoder.flush()
        remaining = decoder.unused_data
    return tarfile.open(fileobj=io.BytesIO(payload), mode="r:")


def _payload_digest(archive):
    records = []
    with _payload(archive) as package:
        for item in package:
            path = ROOT / item.name
            _directories(path.parent)
            if item.issym():
                if not path.is_symlink():
                    raise RuntimeError("Installed firewall package link mismatch")
                records.append((item.name, "link", os.readlink(path)))
            elif item.isfile():
                records.append((item.name, "sha256", _digest(path)))
            elif not item.isdir():
                raise RuntimeError("Unsupported firewall package payload record")
    return hashlib.sha256(json.dumps(sorted(records), separators=(",", ":")).encode()).hexdigest()


def profile():
    release = ROOT / "etc/alpine-release"
    if platform.machine() != MANIFEST["architecture"] or not release.exists():
        return None
    if _file(release).decode().strip() != MANIFEST["alpine_release"]:
        return None
    return MANIFEST["profile"]


def verify(expected_profile):
    _directories(CACHE, allow_missing=True)
    if expected_profile != MANIFEST["profile"] or profile() != expected_profile:
        raise RuntimeError("Firewall package runtime profile mismatch")
    key = MANIFEST["key"]
    if _digest(ROOT / "etc/apk/keys" / key["name"]) != key["sha256"]:
        raise RuntimeError("Firewall package signing key mismatch")
    installed = _versions()
    if any(installed.get(name) != version for name, version in MANIFEST["dependencies"].items()):
        raise RuntimeError("Firewall prerequisite dependency version mismatch")
    if any(name in installed for name in MANIFEST["conflicts"]):
        raise RuntimeError("Conflicting firewall package is already installed")
    complete = True
    for package in MANIFEST["packages"]:
        version = installed.get(package["name"])
        if version is None:
            complete = False
            continue
        if version != package["version"]:
            raise RuntimeError("Refusing to replace an existing firewall package version")
        archive = CACHE / (package["sha256"] + ".apk")
        if not archive.exists():
            complete = False
            continue
        if _digest(archive) != package["sha256"]:
            raise RuntimeError("Cached firewall package checksum mismatch")
        if _payload_digest(archive) != package["payload_sha256"]:
            raise RuntimeError("Installed firewall package checksum mismatch")
    return complete


def install(expected_profile, candidates):
    if verify(expected_profile):
        return
    if len(candidates) != len(MANIFEST["packages"]):
        raise RuntimeError("Incomplete firewall package closure")
    paths = []
    missing = []
    installed = _versions()
    for package, candidate in zip(MANIFEST["packages"], candidates, strict=True):
        path = Path(candidate)
        if not path.is_absolute() or path.suffix != ".apk" or path.stat().st_size != package["bytes"]:
            raise RuntimeError("Firewall package candidate size mismatch")
        if _digest(path) != package["sha256"]:
            raise RuntimeError("Firewall package candidate checksum mismatch")
        if package["name"] in installed:
            if _payload_digest(path) != package["payload_sha256"]:
                raise RuntimeError("Installed firewall package checksum mismatch")
        else:
            with _payload(path) as payload:
                for item in payload:
                    target = ROOT / item.name
                    _directories(target.parent, allow_missing=True)
                    if not item.isdir() and (target.exists() or target.is_symlink()):
                        raise RuntimeError("Refusing to overwrite an existing firewall prerequisite file")
            missing.append(str(path))
        paths.append(str(path))
    command = ["/sbin/apk", "--no-network", "--no-cache", "--repositories-file", "/dev/null"]
    environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root"}
    # Restrict both verification and installation to the pinned vendor signing key.
    with tempfile.TemporaryDirectory(prefix="blaxel-apk-keys-") as directory:
        key = MANIFEST["key"]
        Path(directory, key["name"]).write_bytes(_file(ROOT / "etc/apk/keys" / key["name"]))
        command.extend(["--keys-dir", directory])
        subprocess.run([*command, "verify", *paths], check=True, timeout=30, env=environment, capture_output=True)
        if missing:
            subprocess.run(
                [*command, "--no-scripts", "--no-commit-hooks", "add", *missing],
                check=True,
                timeout=60,
                env=environment,
                capture_output=True,
            )
    _directories(CACHE, allow_missing=True)
    CACHE.mkdir(mode=0o700, parents=True, exist_ok=True)
    _directories(CACHE)
    for package, path in zip(MANIFEST["packages"], paths, strict=True):
        content = _file(Path(path))
        if hashlib.sha256(content).hexdigest() != package["sha256"]:
            raise RuntimeError("Firewall package changed during installation")
        target = CACHE / (package["sha256"] + ".apk")
        with tempfile.NamedTemporaryFile(dir=CACHE, delete=False) as staged:
            staged.write(content)
        try:
            os.replace(staged.name, target)
        finally:
            Path(staged.name).unlink(missing_ok=True)
    if not verify(expected_profile):
        raise RuntimeError("Firewall package installation did not verify")
