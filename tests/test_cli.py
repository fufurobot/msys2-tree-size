"""Tests for the command line interface.

``main`` is exercised in-process with a captured ``argv`` and captured output,
so the tests assert on behaviour (exit codes, what lands on stdout vs stderr,
which files are written) rather than on argparse internals.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from msys2_tree_size import cli  # noqa: E402
from support import TempDirTestCase


class CliTestCase(TempDirTestCase):
    """Runs ``cli.main`` with captured argv and output."""

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main([str(a) for a in argv])
            except SystemExit as exc:  # argparse --help / usage errors
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    def make_tree(self):
        """A small directory with a known layout and a duplicate pair."""
        root = self.make_temp_dir()
        (root / "a.txt").write_bytes(b"x" * 100)
        (root / "b.txt").write_bytes(b"x" * 100)  # duplicate of a.txt
        (root / "c.bin").write_bytes(b"y" * 300)
        sub = root / "sub"
        sub.mkdir()
        (sub / "d.txt").write_bytes(b"z" * 50)
        return root


class TestParser(CliTestCase):
    def test_parser_builds(self):
        self.assertIsNotNone(cli.build_parser())

    def test_help_exits_zero(self):
        code, out, _ = self.run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("usage", out.lower())

    def test_no_command_is_an_error(self):
        code, _, err = self.run_cli()
        self.assertNotEqual(code, 0)
        self.assertTrue(err.strip())

    def test_unknown_command_is_an_error(self):
        code, _, err = self.run_cli("nonsense")
        self.assertNotEqual(code, 0)
        self.assertIn("nonsense", err)

    def test_version_is_reported(self):
        from msys2_tree_size import __version__

        code, out, _ = self.run_cli("--version")
        self.assertEqual(code, 0)
        # Read the version rather than hardcoding it, so bumping a release does
        # not require editing a CLI test.
        self.assertIn(__version__, out)
        self.assertIn("msys2-tree-size", out)

    def test_subcommands_are_registered(self):
        parser = cli.build_parser()
        actions = [a for a in parser._actions if isinstance(a, cli.argparse._SubParsersAction)]
        self.assertTrue(actions)
        self.assertEqual(set(actions[0].choices), {"du", "dupes", "devices"})


class TestDuCommand(CliTestCase):
    def test_lists_entries_for_a_directory(self):
        root = self.make_tree()
        code, out, _ = self.run_cli("du", root)
        self.assertEqual(code, 0)
        self.assertIn("a.txt", out)
        self.assertIn("sub", out)

    def test_default_output_is_a_tree(self):
        root = self.make_tree()
        _, out, _ = self.run_cli("du", root)
        self.assertIn("--", out)

    def test_flat_mode(self):
        root = self.make_tree()
        _, out, _ = self.run_cli("du", root, "--flat")
        self.assertNotIn("`--", out)
        self.assertIn("a.txt", out)

    def test_no_files_hides_files(self):
        root = self.make_tree()
        _, out, _ = self.run_cli("du", root, "--no-files")
        self.assertNotIn("a.txt", out)
        self.assertIn("sub", out)

    def test_top_limits_output(self):
        root = self.make_tree()
        _, out, _ = self.run_cli("du", root, "--flat", "--top", "1")
        self.assertEqual(len([ln for ln in out.splitlines() if ln.strip()]), 1)

    def test_max_depth_limits_output(self):
        root = self.make_tree()
        _, out, _ = self.run_cli("du", root, "--flat", "--max-depth", "0")
        self.assertEqual(len([ln for ln in out.splitlines() if ln.strip()]), 1)

    def test_min_size_filters_tiny_entries(self):
        root = self.make_tree()
        _, out, _ = self.run_cli("du", root, "--flat", "--min-size", "1M")
        self.assertNotIn("a.txt", out)

    def test_total_is_reported(self):
        root = self.make_tree()
        _, out, _ = self.run_cli("du", root, "--flat")
        self.assertIn("550B", out)

    def test_json_output_to_stdout(self):
        root = self.make_tree()
        code, out, _ = self.run_cli("du", root, "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["summary"]["total_bytes"], 550)

    def test_json_output_to_file(self):
        root = self.make_tree()
        target = root / "report.json"
        code, _, _ = self.run_cli("du", root, "--json", str(target))
        self.assertEqual(code, 0)
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload["summary"]["total_bytes"], 550)

    def test_missing_path_is_an_error(self):
        code, _, err = self.run_cli("du", self.make_temp_dir() / "nope")
        self.assertNotEqual(code, 0)
        self.assertTrue(err.strip())

    def test_file_as_root_is_an_error(self):
        root = self.make_tree()
        code, _, err = self.run_cli("du", root / "a.txt")
        self.assertNotEqual(code, 0)
        self.assertTrue(err.strip())

    def test_unicode_tree_is_opt_in(self):
        root = self.make_tree()
        _, out, _ = self.run_cli("du", root, "--unicode")
        self.assertTrue(any(ord(ch) > 127 for ch in out))


class TestDupesCommand(CliTestCase):
    def test_reports_the_duplicate_pair(self):
        root = self.make_tree()
        code, out, _ = self.run_cli("dupes", root)
        self.assertEqual(code, 0)
        self.assertIn("a.txt", out)
        self.assertIn("b.txt", out)

    def test_non_duplicate_is_not_reported(self):
        root = self.make_tree()
        _, out, _ = self.run_cli("dupes", root)
        self.assertNotIn("c.bin", out)

    def test_reports_wasted_space(self):
        root = self.make_tree()
        _, out, _ = self.run_cli("dupes", root)
        self.assertIn("100B", out)

    def test_clean_tree_says_so(self):
        root = self.make_temp_dir()
        (root / "only.txt").write_bytes(b"unique")
        code, out, _ = self.run_cli("dupes", root)
        self.assertEqual(code, 0)
        self.assertIn("no duplicates", out.lower())

    def test_csv_output(self):
        root = self.make_tree()
        target = root / "dupes.csv"
        code, _, _ = self.run_cli("dupes", root, "--csv", str(target))
        self.assertEqual(code, 0)
        text = target.read_text(encoding="utf-8")
        self.assertIn("sha256", text)
        self.assertIn("a.txt", text)

    def test_json_output(self):
        root = self.make_tree()
        code, out, _ = self.run_cli("dupes", root, "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(len(payload["duplicates"]), 2)

    def test_min_size_skips_small_files(self):
        root = self.make_tree()
        _, out, _ = self.run_cli("dupes", root, "--min-size", "1M")
        self.assertIn("no duplicates", out.lower())


class TestDevicesCommand(CliTestCase):
    def test_runs_and_reports_absence_gracefully(self):
        code, out, err = self.run_cli("devices")
        # Either a table or an explanatory note; never a traceback.
        self.assertEqual(code, 0)
        self.assertTrue((out + err).strip())

    def test_json_mode_is_parseable(self):
        code, out, _ = self.run_cli("devices", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertIn("devices", payload)


if __name__ == "__main__":
    unittest.main()
