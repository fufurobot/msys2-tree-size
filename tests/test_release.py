"""Tests for the release configuration.

A broken release workflow is expensive to discover: the only way to exercise it
is to push a tag, and by then the tag is public. These tests therefore check the
workflow's *structure* so the common mistakes fail in CI on a normal commit:

* the tag trigger is wired up at all;
* the bundle is built from a downloaded MSYS2 release rather than any local
  installation, so the artifact is reproducible from a clean checkout;
* the release job cannot run before the test suite passes;
* the shipped bundle README exists and documents how to start the thing.

They intentionally do not try to parse YAML with a third-party library, because
the package has no runtime dependencies and adding one for tests would be a poor
trade. Structure is asserted with targeted text checks instead.
"""

from __future__ import annotations

import re
import unittest

from support import REPO_ROOT

WORKFLOWS = REPO_ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"
BUNDLE_README = REPO_ROOT / "docs" / "bundle-README.md"


class TestReleaseWorkflowExists(unittest.TestCase):
    def test_release_workflow_is_present(self):
        self.assertTrue(RELEASE.is_file(), f"missing {RELEASE}")

    def test_bundle_readme_is_present(self):
        self.assertTrue(BUNDLE_README.is_file(), f"missing {BUNDLE_README}")

    def test_release_workflow_is_not_empty(self):
        self.assertGreater(len(RELEASE.read_text(encoding="utf-8").strip()), 200)


class TestReleaseTriggers(unittest.TestCase):
    """A release must be triggered by a version tag, and only by a tag."""

    def setUp(self):
        self.text = RELEASE.read_text(encoding="utf-8")

    def test_tag_push_is_a_trigger(self):
        self.assertRegex(self.text, r"tags:\s*\[?\s*[\"']?v\*")

    def test_manual_dispatch_is_available(self):
        self.assertIn("workflow_dispatch:", self.text)

    def test_job_has_write_permission_for_releases(self):
        # Without contents:write the release step cannot create a release.
        self.assertRegex(self.text, r"contents:\s*write")


class TestBundleIsBuiltFromADownload(unittest.TestCase):
    """The bundle must not depend on anyone's local MSYS2 installation.

    This is the property that makes the artifact reproducible: a pristine
    runtime is fetched inside the job, so the same tag produces the same bundle
    on any machine.
    """

    def setUp(self):
        self.text = RELEASE.read_text(encoding="utf-8")

    def test_downloads_the_official_msys2_release(self):
        self.assertIn("msys2/msys2-installer/releases", self.text)

    def test_uses_the_base_archive_asset(self):
        self.assertRegex(self.text, r"msys2-base-x86_64-.*\.sfx\.exe")

    def test_does_not_reference_a_local_installation_path(self):
        # A hardcoded developer path would silently make the build machine
        # specific, which is exactly what the download step exists to avoid.
        # Plain substring checks avoid regex escaping pitfalls with backslashes.
        for suspicious in ("C:\\msys64", "Downloads\\msys64", "~\\msys64"):
            self.assertNotIn(
                suspicious,
                self.text,
                f"release workflow references a local install path: {suspicious}",
            )

    def test_installs_python_into_the_bundled_runtime(self):
        self.assertIn("pacman", self.text)
        self.assertRegex(self.text, r"python python-pip|python-pip python")

    def test_installs_the_project_itself(self):
        self.assertRegex(self.text, r"pip install[^\n]*\.")


class TestBundleIsVerifiedBeforeRelease(unittest.TestCase):
    """The bundle job must depend on the test suite passing."""

    def setUp(self):
        self.text = RELEASE.read_text(encoding="utf-8")

    def test_a_verify_job_runs_the_suite(self):
        self.assertIn("verify", self.text)
        self.assertRegex(self.text, r"unittest discover -s tests")

    def test_bundle_job_needs_verify(self):
        self.assertRegex(self.text, r"needs:\s*verify")

    def test_bundle_job_runs_the_tool_after_installing(self):
        # Installing successfully is not evidence the tool works; the workflow
        # must actually invoke it.
        self.assertRegex(self.text, r"msys2-tree-size --version")
        self.assertRegex(self.text, r"msys2-tree-size devices")

    def test_uploads_and_attaches_the_asset(self):
        self.assertIn("upload-artifact", self.text)
        self.assertIn("action-gh-release", self.text)


class TestBundleReadmeContent(unittest.TestCase):
    """The README inside the archive is the user's first contact with it."""

    def setUp(self):
        self.text = BUNDLE_README.read_text(encoding="utf-8")

    def test_documents_the_launcher_command(self):
        self.assertIn("msys2_shell.cmd", self.text)

    def test_documents_the_three_commands(self):
        for command in ("du", "dupes", "devices"):
            self.assertIn(f"msys2-tree-size {command}", self.text)

    def test_explains_why_msys2_is_bundled(self):
        self.assertIn("/dev/disk/by-id", self.text)
        self.assertIn("/proc/partitions", self.text)

    def test_explains_that_a_sibling_console_cannot_see_devices(self):
        # This is the single most confusing aspect of the bundle, so it must be
        # stated rather than left for the user to discover.
        self.assertIn("cmd.exe", self.text)

    def test_mentions_the_license(self):
        self.assertIn("AGPL", self.text)


class TestVersionConsistency(unittest.TestCase):
    """The tag, pyproject and __version__ must agree, or the release lies."""

    def test_pyproject_and_package_versions_match(self):
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        init = (REPO_ROOT / "src" / "msys2_tree_size" / "__init__.py").read_text(encoding="utf-8")

        declared = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
        self.assertIsNotNone(declared, "pyproject.toml has no version")
        runtime = re.search(r'__version__\s*=\s*"([^"]+)"', init)
        self.assertIsNotNone(runtime, "__init__.py has no __version__")

        self.assertEqual(
            declared.group(1),
            runtime.group(1),
            "pyproject version and __version__ disagree",
        )

    def test_version_is_semver_like(self):
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        declared = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
        assert declared is not None
        self.assertRegex(declared.group(1), r"^\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
