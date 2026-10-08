"""Tests for the release configuration.

A broken release workflow is expensive to discover: the only way to exercise it
is to push a tag, and by then the tag is public. These tests therefore check the
workflow's *structure* so the common mistakes fail in CI on a normal commit.

They intentionally do not parse YAML with a third-party library, because the
package has no runtime dependencies and adding one for tests would be a poor
trade. Structure is asserted with targeted text checks instead.
"""

from __future__ import annotations

import re
import unittest

from support import REPO_ROOT

WORKFLOWS = REPO_ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"
# The user-facing install and usage guide. Named for what it is rather than for
# the artifact it used to describe, since it is now the primary documentation.
INSTALL_GUIDE = REPO_ROOT / "docs" / "install.md"


class TestReleaseWorkflowExists(unittest.TestCase):
    def test_release_workflow_is_present(self):
        self.assertTrue(RELEASE.is_file(), f"missing {RELEASE}")

    def test_install_guide_is_present(self):
        self.assertTrue(INSTALL_GUIDE.is_file(), f"missing {INSTALL_GUIDE}")

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

    def test_does_not_trigger_on_every_push(self):
        # A push trigger with a branch filter would publish on ordinary commits.
        push_block = re.search(r"push:\n((?:\s{4,}.*\n)+)", self.text)
        self.assertIsNotNone(push_block, "no push trigger found")
        assert push_block is not None
        self.assertNotIn("branches:", push_block.group(1))


class TestPublishedArtifacts(unittest.TestCase):
    """The release ships a wheel and an sdist, and verifies both install."""

    def setUp(self):
        self.text = RELEASE.read_text(encoding="utf-8")

    def test_builds_the_distributions(self):
        self.assertRegex(self.text, r"uv build")

    def test_attaches_wheel_and_sdist(self):
        self.assertIn("*.whl", self.text)
        self.assertIn("*.tar.gz", self.text)

    def test_validates_distribution_metadata(self):
        # twine check catches a malformed README or missing license before the
        # artifact is public; there is no way to fix it afterwards.
        self.assertIn("twine check", self.text)

    def test_installs_the_wheel_and_runs_it(self):
        # Building successfully is not evidence the package works.
        self.assertRegex(self.text, r"--from dist/\*\.whl")
        self.assertRegex(self.text, r'msys2-tree-size" --version')

    def test_installs_the_sdist_and_runs_it(self):
        self.assertRegex(self.text, r"--from dist/\*\.tar\.gz")

    def test_checks_the_wheel_contains_every_module(self):
        self.assertIn("missing modules", self.text)

    def test_checks_for_build_residue(self):
        # A wheel that swallowed .venv or .uv-cache would be enormous and
        # would leak the build machine's layout.
        self.assertIn("build residue", self.text)

    def test_lists_the_assets_before_publishing(self):
        self.assertRegex(self.text, r"ls -la dist")

    def test_publish_depends_on_the_build(self):
        self.assertRegex(self.text, r"needs:\s*dist")

    def test_does_not_build_a_bundled_runtime(self):
        # The self-contained MSYS2 bundle was removed deliberately: packaging a
        # ~290 MB runtime failed repeatedly on steps unrelated to this project,
        # and a wheel that installs in seconds is worth more than shipping
        # nothing. If bundling returns, this test should be replaced by the
        # checks that verified the bundle actually ran.
        self.assertNotIn("msys2-installer", self.text)
        self.assertNotIn("pacman", self.text)


class TestReleaseIsVerifiedBeforePublishing(unittest.TestCase):
    """The publish job must depend on the test suite passing."""

    def setUp(self):
        self.text = RELEASE.read_text(encoding="utf-8")

    def test_a_verify_job_runs_the_suite(self):
        self.assertIn("verify", self.text)
        self.assertRegex(self.text, r"unittest discover -s tests")

    def test_lint_and_format_are_checked(self):
        self.assertRegex(self.text, r"ruff check")
        self.assertRegex(self.text, r"ruff format --check")

    def test_dist_job_needs_verify(self):
        self.assertRegex(self.text, r"needs:\s*verify")


class TestInstallGuide(unittest.TestCase):
    """The install guide is the first thing a user reads about the artifact."""

    def setUp(self):
        self.text = INSTALL_GUIDE.read_text(encoding="utf-8")

    def test_documents_installation(self):
        self.assertIn("uv tool install msys2-tree-size", self.text)

    def test_documents_the_four_commands(self):
        for command in ("du", "dupes", "devices", "diagnose"):
            self.assertIn(f"msys2-tree-size {command}", self.text)

    def test_explains_why_msys2_matters(self):
        self.assertIn("/dev/disk/by-id", self.text)
        self.assertIn("/proc/partitions", self.text)

    def test_states_that_plain_windows_cannot_see_devices(self):
        # This is the single most confusing aspect for a Windows user, so it is
        # stated rather than left to be discovered.
        self.assertIn("cmd.exe", self.text)

    def test_explains_how_to_get_archive_tools(self):
        self.assertIn("pacman -S", self.text)

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
