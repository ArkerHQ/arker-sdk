#!/usr/bin/env python3
"""Require release evidence for the checked-out package before publication."""

import base64
import json
import os
import re
import subprocess
import tempfile
import tomllib
import urllib.request
from pathlib import Path


def credential_env(env: dict[str, str]) -> dict[str, str]:
    url = env["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=sts.amazonaws.com"
    request = urllib.request.Request(
        url,
        headers={"Authorization": "bearer " + env["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        token = json.load(response)["value"]
    with tempfile.TemporaryDirectory(prefix="release-identity-") as td:
        token_file = Path(td) / "token"
        token_file.write_text(token)
        result = subprocess.run(
            [
                "aws",
                "sts",
                "assume-role-with-web-identity",
                "--role-arn",
                env["RELEASE_AUTHORIZATION_ROLE"],
                "--role-session-name",
                "release-publisher",
                "--web-identity-token",
                "file://" + str(token_file),
                "--region",
                env["RELEASE_AUTHORIZATION_REGION"],
                "--output",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    credentials = json.loads(result.stdout)["Credentials"]
    return env | {
        "AWS_ACCESS_KEY_ID": credentials["AccessKeyId"],
        "AWS_SECRET_ACCESS_KEY": credentials["SecretAccessKey"],
        "AWS_SESSION_TOKEN": credentials["SessionToken"],
        "AWS_DEFAULT_REGION": env["RELEASE_AUTHORIZATION_REGION"],
    }


def authorize(root: Path, env: dict[str, str]) -> None:
    required = (
        "RELEASE_CANDIDATE",
        "RELEASE_STEP",
        "RELEASE_ATTEMPT",
        "RELEASE_SDK_COMMIT",
        "RELEASE_SDK_VERSION",
        "RELEASE_ECOSYSTEM",
        "RELEASE_PACKAGE",
        "RELEASE_DIRECTORY",
        "RELEASE_AUTHORIZATION_SCRIPT",
        "RELEASE_AUTHORIZATION_PROJECT",
        "ARKER_RELEASE_BUCKET",
        "RELEASE_AUTHORIZATION_REPOSITORY",
        "RELEASE_AUTHORIZATION_REF",
        "RELEASE_AUTHORIZATION_READ_TOKEN",
        "RELEASE_AUTHORIZATION_ROLE",
        "RELEASE_AUTHORIZATION_REGION",
    )
    if any(not env.get(key) for key in required):
        raise ValueError("release authorization configuration is incomplete")
    commit = env["RELEASE_SDK_COMMIT"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("SDK source must be a full commit SHA")
    source = root / "source"
    actual = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True, timeout=30
    ).strip()
    if actual != commit:
        raise ValueError("SDK source differs from the candidate commit")
    subprocess.run(
        ["git", "diff", "--exit-code", "HEAD", "--"], cwd=source, check=True, timeout=30
    )
    directory = env["RELEASE_DIRECTORY"]
    expected = {
        "python": ("pypi", "arker"),
        "typescript": ("npm", "@arker-ai/sdk"),
        "cli": ("npm", "@arker-ai/cli"),
        "go": ("go", "github.com/ArkerHQ/arker-sdk/go"),
    }
    if directory not in expected or expected[directory] != (
        env["RELEASE_ECOSYSTEM"],
        env["RELEASE_PACKAGE"],
    ):
        raise ValueError("package identity does not match the publisher")
    package = source / directory
    if directory == "python":
        metadata = tomllib.loads((package / "pyproject.toml").read_text())["project"]
        name, version = metadata["name"], metadata["version"]
    elif directory == "go":
        name = (
            (package / "go.mod")
            .read_text()
            .splitlines()[0]
            .removeprefix("module ")
            .strip()
        )
        version = (package / "VERSION").read_text().strip()
    else:
        metadata = json.loads((package / "package.json").read_text())
        name, version = metadata["name"], metadata["version"]
    if (name, version) != (env["RELEASE_PACKAGE"], env["RELEASE_SDK_VERSION"]):
        raise ValueError("package name or version differs from the candidate")
    controller = root / ".authorization"
    repository = env["RELEASE_AUTHORIZATION_REPOSITORY"]
    revision = env["RELEASE_AUTHORIZATION_REF"]
    if not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
    ) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError(
            "authorization source must have an exact repository and commit"
        )
    git_env = dict(env)
    credential = base64.b64encode(
        ("x-access-token:" + env["RELEASE_AUTHORIZATION_READ_TOKEN"]).encode()
    ).decode()
    git_env.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + credential,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    if not controller.exists():
        subprocess.run(
            ["git", "init", str(controller)], check=True, capture_output=True
        )
    subprocess.run(
        [
            "git",
            "fetch",
            "--depth=1",
            "https://github.com/" + repository + ".git",
            revision,
        ],
        cwd=controller,
        env=git_env,
        check=True,
        capture_output=True,
        timeout=600,
    )
    subprocess.run(
        ["git", "checkout", "--detach", "FETCH_HEAD"],
        cwd=controller,
        check=True,
        capture_output=True,
        timeout=600,
    )
    script = (controller / env["RELEASE_AUTHORIZATION_SCRIPT"]).resolve()
    project = (controller / env["RELEASE_AUTHORIZATION_PROJECT"]).resolve()
    if not script.is_relative_to(controller.resolve()) or not project.is_relative_to(
        controller.resolve()
    ):
        raise ValueError("authorization paths must stay inside their checkout")
    subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(project),
            "python",
            str(script),
            "authorize",
            env["RELEASE_CANDIDATE"],
            "--step",
            env["RELEASE_STEP"],
            "--attempt",
            env["RELEASE_ATTEMPT"],
            "--kind",
            "sdk",
            "--target",
            env["RELEASE_ECOSYSTEM"],
            "--resource",
            name + "@" + version,
            "--commit",
            commit,
        ],
        cwd=root,
        env=credential_env(env),
        check=True,
        capture_output=True,
        timeout=600,
    )


if __name__ == "__main__":
    try:
        authorize(Path.cwd(), dict(os.environ))
    except (ValueError, OSError, KeyError, subprocess.SubprocessError):
        raise SystemExit(
            "Package release authorization failed; verify candidate evidence and configuration."
        ) from None
