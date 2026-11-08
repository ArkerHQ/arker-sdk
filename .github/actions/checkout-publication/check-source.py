"""Verify optional dispatch source identity without changing direct releases."""

import json
import os
import re
import subprocess
from pathlib import Path


def check_source(root: Path, package: str, commit: str, version: str) -> None:
    if not commit and not version:
        return
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not version:
        raise ValueError(
            "Provide both a full SDK commit SHA and an exact package version"
        )
    actual_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    if actual_commit != commit:
        raise ValueError("Checked-out source does not match the requested SDK commit")

    if package in ("typescript", "cli"):
        actual_version = json.loads((root / package / "package.json").read_text())[
            "version"
        ]
    elif package == "python":
        match = re.search(
            r'^version = "([^"]+)"$',
            (root / "python/pyproject.toml").read_text(),
            re.MULTILINE,
        )
        if match is None:
            raise ValueError("Could not read the Python package version")
        actual_version = match[1]
    elif package == "go":
        actual_version = (root / "go/VERSION").read_text().strip()
    else:
        raise ValueError("Unknown publication package")
    if actual_version != version:
        raise ValueError("Package version does not match the requested SDK version")


if __name__ == "__main__":
    check_source(
        Path.cwd(),
        os.environ["PUBLICATION_PACKAGE"],
        os.environ["PUBLICATION_COMMIT"],
        os.environ["PUBLICATION_VERSION"],
    )
