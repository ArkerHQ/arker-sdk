"""Publication must preserve exact source identity and reject missing evidence."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "authorize_release", ROOT / "scripts/authorize-release.py"
)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)
SHA = "a" * 40


class ReleaseAuthorization(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "source/typescript").mkdir(parents=True)
        (self.root / "source/typescript/package.json").write_text(
            json.dumps({"name": "@arker-ai/sdk", "version": "1.2.3"})
        )
        credentials = patch.object(guard, "credential_env", side_effect=lambda env: env)
        credentials.start()
        self.addCleanup(credentials.stop)
        self.env = {
            "PATH": "/usr/bin",
            "RELEASE_CANDIDATE": "candidate",
            "RELEASE_STEP": "sdk",
            "RELEASE_ATTEMPT": "attempt",
            "RELEASE_SDK_COMMIT": SHA,
            "RELEASE_SDK_VERSION": "1.2.3",
            "RELEASE_ECOSYSTEM": "npm",
            "RELEASE_PACKAGE": "@arker-ai/sdk",
            "RELEASE_DIRECTORY": "typescript",
            "RELEASE_AUTHORIZATION_SCRIPT": "authorize.py",
            "RELEASE_AUTHORIZATION_PROJECT": "project",
            "RELEASE_AUTHORIZATION_REPOSITORY": "example/authorization",
            "RELEASE_AUTHORIZATION_REF": "b" * 40,
            "RELEASE_AUTHORIZATION_READ_TOKEN": "test-only",
            "RELEASE_AUTHORIZATION_ROLE": "test-only",
            "RELEASE_AUTHORIZATION_REGION": "test-only",
            "ARKER_RELEASE_BUCKET": "test-only",
        }

    def test_missing_configuration_cannot_execute(self):
        for key in self.env:
            if key == "PATH":
                continue
            with self.subTest(key=key), patch.object(guard.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    guard.authorize(
                        self.root, {k: v for k, v in self.env.items() if k != key}
                    )
                run.assert_not_called()

    def test_source_and_package_mismatches_cannot_authorize(self):
        for key, value in [
            ("RELEASE_SDK_COMMIT", "main"),
            ("RELEASE_SDK_COMMIT", "c" * 40),
            ("RELEASE_SDK_VERSION", "9.9.9"),
            ("RELEASE_PACKAGE", "other"),
            ("RELEASE_DIRECTORY", "../typescript"),
            ("RELEASE_AUTHORIZATION_REF", "main"),
            ("RELEASE_AUTHORIZATION_SCRIPT", "../../outside.py"),
        ]:
            with (
                self.subTest(key=key),
                patch.object(guard.subprocess, "check_output", return_value=SHA),
                patch.object(guard.subprocess, "run") as run,
            ):
                with self.assertRaises(ValueError):
                    guard.authorize(self.root, self.env | {key: value})
                self.assertFalse(
                    any(call.args[0][0] == "uv" for call in run.call_args_list)
                )

    def test_authorizer_receives_exact_package_version_and_attempt(self):
        with (
            patch.object(guard.subprocess, "check_output", return_value=SHA),
            patch.object(guard.subprocess, "run") as run,
        ):
            guard.authorize(self.root, self.env)
        argv = run.call_args.args[0]
        self.assertEqual(
            argv[argv.index("authorize") :],
            [
                "authorize",
                "candidate",
                "--step",
                "sdk",
                "--attempt",
                "attempt",
                "--kind",
                "sdk",
                "--target",
                "npm",
                "--resource",
                "@arker-ai/sdk@1.2.3",
                "--commit",
                SHA,
            ],
        )
        self.assertTrue(run.call_args.kwargs["capture_output"])
        self.assertNotIn("test-only", " ".join(argv))

    def test_each_ecosystem_uses_its_actual_package_metadata(self):
        for directory, ecosystem, name in [
            ("python", "pypi", "arker"),
            ("go", "go", "github.com/ArkerHQ/arker-sdk/go"),
            ("cli", "npm", "@arker-ai/cli"),
        ]:
            package = self.root / "source" / directory
            package.mkdir()
            if directory == "python":
                (package / "pyproject.toml").write_text(
                    '[project]\nname = "arker"\nversion = "1.2.3"\n'
                )
            elif directory == "go":
                (package / "go.mod").write_text("module " + name + "\n")
                (package / "VERSION").write_text("1.2.3\n")
            else:
                (package / "package.json").write_text(
                    json.dumps({"name": name, "version": "1.2.3"})
                )
            with (
                self.subTest(directory=directory),
                patch.object(guard.subprocess, "check_output", return_value=SHA),
                patch.object(guard.subprocess, "run") as run,
            ):
                guard.authorize(
                    self.root,
                    self.env
                    | {
                        "RELEASE_DIRECTORY": directory,
                        "RELEASE_ECOSYSTEM": ecosystem,
                        "RELEASE_PACKAGE": name,
                    },
                )
                argv = run.call_args.args[0]
                self.assertEqual(argv[argv.index("--resource") + 1], name + "@1.2.3")

    def test_authorization_failure_propagates(self):
        def fail_authorize(argv, **kwargs):
            if argv[0] == "uv":
                raise subprocess.CalledProcessError(1, argv, stderr="denied")

        with (
            patch.object(guard.subprocess, "check_output", return_value=SHA),
            patch.object(guard.subprocess, "run", side_effect=fail_authorize),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            guard.authorize(self.root, self.env)

    def test_dirty_source_cannot_authorize(self):
        with (
            patch.object(guard.subprocess, "check_output", return_value=SHA),
            patch.object(
                guard.subprocess,
                "run",
                side_effect=subprocess.CalledProcessError(1, ["git"]),
            ) as run,
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                guard.authorize(self.root, self.env)
            self.assertEqual(run.call_count, 1)

    def test_all_publishers_require_dispatch_and_two_authorization_checks(self):
        for name in ("python", "typescript", "cli", "go"):
            with self.subTest(publisher=name):
                workflow = (ROOT / f".github/workflows/publish-{name}.yml").read_text()
                self.assertIn("workflow_dispatch:", workflow)
                self.assertNotIn("  push:", workflow)
                self.assertIn("if: github.ref == 'refs/heads/main'", workflow)
                self.assertEqual(
                    workflow.count(
                        "uses: ./.publisher/.github/actions/authorize-release"
                    ),
                    2,
                )
                last_guard = workflow.rindex(
                    "uses: ./.publisher/.github/actions/authorize-release"
                )
                mutation = workflow.index(
                    "pypa/gh-action-pypi-publish"
                    if name == "python"
                    else "git push"
                    if name == "go"
                    else "run: npm publish"
                )
                self.assertLess(last_guard, mutation)
                self.assertIn("ref: ${{ inputs.sdk_commit }}", workflow)


if __name__ == "__main__":
    unittest.main()
