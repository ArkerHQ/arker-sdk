"""A dispatch publishes the requested source; direct releases need no inputs."""

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "publication_source", ROOT / ".github/actions/checkout-publication/check-source.py"
)
publication = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publication)


class PublicationSource(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for package in ("typescript", "cli", "python", "go"):
            (self.root / package).mkdir()
        for package in ("typescript", "cli"):
            (self.root / package / "package.json").write_text(
                json.dumps({"version": "1.2.3"})
            )
        (self.root / "python/pyproject.toml").write_text(
            '[project]\nversion = "1.2.3"\n'
        )
        (self.root / "go/VERSION").write_text("1.2.3\n")
        self.git("init", "--quiet")
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--quiet",
            "-m",
            "Package source",
        )
        self.commit = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True)

    def test_direct_release_needs_no_dispatch_inputs(self):
        for package in ("typescript", "cli", "python", "go"):
            with self.subTest(package=package):
                publication.check_source(self.root, package, "", "")

    def test_pinned_dispatch_checks_each_package_version(self):
        for package in ("typescript", "cli", "python", "go"):
            with self.subTest(package=package):
                publication.check_source(self.root, package, self.commit, "1.2.3")
                with self.assertRaisesRegex(ValueError, "version"):
                    publication.check_source(self.root, package, self.commit, "9.9.9")

    def test_partial_or_mutable_source_identity_is_rejected(self):
        for commit, version in (
            ("", "1.2.3"),
            (self.commit, ""),
            ("main", "1.2.3"),
            ("go/v1.2.3", "1.2.3"),
        ):
            with (
                self.subTest(commit=commit, version=version),
                self.assertRaises(ValueError),
            ):
                publication.check_source(self.root, "go", commit, version)

    def test_checkout_must_match_requested_commit(self):
        with self.assertRaisesRegex(ValueError, "commit"):
            publication.check_source(self.root, "go", "a" * 40, "1.2.3")

    def test_pinned_checkout_is_independent_of_later_branch_changes(self):
        (self.root / "go/VERSION").write_text("2.0.0\n")
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--quiet",
            "-m",
            "Next version",
        )
        with self.assertRaisesRegex(ValueError, "commit"):
            publication.check_source(self.root, "go", self.commit, "1.2.3")
        self.git("checkout", "--quiet", "--detach", self.commit)
        publication.check_source(self.root, "go", self.commit, "1.2.3")

    def test_release_card_links_published_source_instead_of_workflow_commit(self):
        env = os.environ | {
            "CARD_DRY_RUN": "1",
            "PACKAGE": "typescript",
            "VERSION": "1.2.3",
            "REGISTRY_URL": "https://www.npmjs.com/package/@arker-ai/sdk/v/1.2.3",
            "SOURCE_COMMIT": self.commit,
            "GITHUB_SHA": "f" * 40,
            "GITHUB_ACTOR": "publisher",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "ArkerHQ/arker-sdk",
            "GITHUB_RUN_ID": "123",
        }
        for state in ("publishing", "published", "failed", "cancelled"):
            with self.subTest(state=state):
                payload = json.loads(
                    subprocess.check_output(
                        ["bash", str(ROOT / ".github/actions/release-card/card.sh")],
                        env=env | {"STATE": state},
                        text=True,
                    )
                )
                card = payload["attachments"][0]["blocks"][0]["text"]["text"]
                self.assertIn(f"/commit/{self.commit}", card)
                self.assertNotIn(env["GITHUB_SHA"], card)


if __name__ == "__main__":
    unittest.main()
