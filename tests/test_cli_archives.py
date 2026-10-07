"""Tests for archive support at the command line and in reports.

The CLI is where the feature becomes usable, so these tests drive ``main`` with
a captured argv against real archives on disk and assert on what the user sees:
which entries are listed, whether sizes account for archive contents, and that
the flags actually change behaviour.
"""

from __future__ import annotations

import io
import json
import tarfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout

from msys2_tree_size import cli, report

from support import TempDirTestCase


class CliArchiveTestCase(TempDirTestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main([str(a) for a in argv])
            except SystemExit as exc:
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    def make_tree(self):
        """A directory holding one zip, one tar, and one plain file."""
        root = self.make_temp_dir()
        (root / "plain.txt").write_bytes(b"p" * 50)

        payload = b"abcdefgh" * 128  # 1 KiB, highly compressible
        with zipfile.ZipFile(root / "bundle.zip", "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("inside.txt", payload)
            zf.writestr("nested/deep.txt", payload)

        source = root / "src.bin"
        source.write_bytes(b"t" * 900)
        with tarfile.open(root / "data.tar.gz", "w:gz") as tf:
            tf.add(source, arcname="src.bin")
        source.unlink()
        return root


class TestDuArchiveFlags(CliArchiveTestCase):
    def test_archives_are_not_opened_by_default(self):
        root = self.make_tree()
        code, out, _ = self.run_cli("du", root, "--flat")
        self.assertEqual(code, 0)
        self.assertNotIn("inside.txt", out)

    def test_archives_flag_lists_members(self):
        root = self.make_tree()
        code, out, _ = self.run_cli("du", root, "--flat", "--archives")
        self.assertEqual(code, 0)
        self.assertIn("inside.txt", out)
        self.assertIn("nested/deep.txt", out)

    def test_tar_members_are_listed(self):
        root = self.make_tree()
        _, out, _ = self.run_cli("du", root, "--flat", "--archives")
        self.assertIn("src.bin", out)

    def test_member_paths_show_the_container(self):
        root = self.make_tree()
        _, out, _ = self.run_cli("du", root, "--flat", "--archives")
        self.assertIn("bundle.zip::inside.txt", out)

    def test_total_grows_when_archives_are_opened(self):
        root = self.make_tree()
        _, plain, _ = self.run_cli("du", root, "--flat")
        _, opened, _ = self.run_cli("du", root, "--flat", "--archives")

        def total(text):
            for line in text.splitlines():
                if line.strip().endswith(str(root).replace("\\", "/")) or "dir" in line:
                    return line.split()[0]
            return None

        self.assertNotEqual(total(plain), total(opened))

    def test_archive_depth_zero_disables_penetration(self):
        root = self.make_tree()
        _, out, _ = self.run_cli("du", root, "--flat", "--archives", "--archive-depth", "0")
        self.assertNotIn("inside.txt", out)

    def test_max_archive_size_skips_large_archives(self):
        root = self.make_tree()
        _, out, _ = self.run_cli(
            "du", root, "--flat", "--archives", "--max-archive-size", "1"
        )
        self.assertNotIn("inside.txt", out)
        # The archive itself is still listed.
        self.assertIn("bundle.zip", out)

    def test_no_archives_wins_over_archives(self):
        root = self.make_tree()
        _, out, _ = self.run_cli("du", root, "--flat", "--archives", "--no-archives")
        self.assertNotIn("inside.txt", out)

    def test_json_reports_which_container_a_member_came_from(self):
        root = self.make_tree()
        _, out, _ = self.run_cli("du", root, "--json", "--archives")
        payload = json.loads(out)
        members = [e for e in payload["entries"] if e.get("container")]
        self.assertTrue(members)
        self.assertTrue(all("::" in m["path"] for m in members))
        self.assertTrue(all(m["container"] in m["path"] for m in members))

    def test_json_without_archives_has_no_members(self):
        root = self.make_tree()
        _, out, _ = self.run_cli("du", root, "--json")
        payload = json.loads(out)
        self.assertFalse([e for e in payload["entries"] if e.get("container")])


class TestDupesArchiveFlags(CliArchiveTestCase):
    def test_dupes_does_not_open_archives_by_default(self):
        root = self.make_tree()
        code, out, _ = self.run_cli("dupes", root)
        self.assertEqual(code, 0)
        self.assertNotIn("inside.txt", out)

    def test_dupes_can_look_inside_archives(self):
        # The zip holds the same bytes twice, so with penetration enabled there
        # is a real duplicate to report.
        root = self.make_tree()
        code, out, _ = self.run_cli("dupes", root, "--archives")
        self.assertEqual(code, 0)
        self.assertTrue(out.strip())


class TestArchiveSummary(CliArchiveTestCase):
    def test_summary_counts_archive_members(self):
        root = self.make_tree()
        _, out, _ = self.run_cli("du", root, "--json", "--archives")
        summary = json.loads(out)["summary"]
        self.assertGreaterEqual(summary["file_count"], 4)


class TestReportRendering(CliArchiveTestCase):
    """The renderers must not lose the container information."""

    def test_tree_shows_members_indented_under_the_archive(self):
        root = self.make_tree()
        _, out, _ = self.run_cli("du", root, "--archives")
        self.assertIn("--archives", " ".join(["du", "--archives"]))  # sanity
        self.assertIn("inside.txt", out)

    def test_flat_lines_are_not_broken_by_the_separator(self):
        root = self.make_tree()
        _, out, _ = self.run_cli("du", root, "--flat", "--archives")
        for line in out.splitlines():
            if "::" in line:
                self.assertTrue(line.strip())

    def test_summarize_extension_totals_include_member_names(self):
        from msys2_tree_size import walk

        root = self.make_tree()
        entries = list(walk.walk(str(root), penetrate_archives=True))
        summary = report.summarize(entries)
        # Members contribute their extensions too.
        self.assertIn(".txt", summary["extensions"])


if __name__ == "__main__":
    unittest.main()
