"""Tests for the report renderers.

Renderers are pure functions from entries to text, so these tests pin down the
*format* contract without touching a real filesystem or a terminal.
"""

from __future__ import annotations

import json
import unittest

from support import TempDirTestCase

from msys2_tree_size import report  # noqa: E402
from msys2_tree_size.walk import Entry  # noqa: E402


def entry(path, size=0, type="dir", depth=0, human=None, **kw):
    """Build an Entry with the fields the renderers care about."""
    name = kw.pop("name", path.rsplit("/", 1)[-1] or path)
    return Entry(
        path=path,
        name=name,
        parent=kw.pop("parent", None),
        type=type,
        depth=depth,
        size=size,
        sha256=kw.pop("sha256", ""),
        child_count=kw.pop("child_count", 0),
        percent_of_parent=kw.pop("percent_of_parent", 0.0),
        error=kw.pop("error", None),
    )


SAMPLE = [
    entry("/r/big.bin", size=1000, type="file", depth=1),
    entry("/r/small.txt", size=24, type="file", depth=1),
    entry("/r/sub", size=500, depth=1, child_count=1),
    entry("/r", size=1524, depth=0, child_count=3),
]


class TestFlat(unittest.TestCase):
    def test_one_line_per_entry(self):
        text = report.render_flat(SAMPLE)
        self.assertEqual(len(text.strip().splitlines()), 4)

    def test_human_size_is_shown(self):
        text = report.render_flat(SAMPLE)
        self.assertIn("1000B", text)

    def test_paths_are_shown(self):
        text = report.render_flat(SAMPLE)
        self.assertIn("/r/big.bin", text)

    def test_sorted_largest_first(self):
        text = report.render_flat(SAMPLE)
        lines = text.strip().splitlines()
        self.assertIn("/r", lines[0])
        self.assertIn("big.bin", lines[1])

    def test_empty_input_renders_empty(self):
        self.assertEqual(report.render_flat([]).strip(), "")

    def test_top_limit(self):
        text = report.render_flat(SAMPLE, top=2)
        self.assertEqual(len(text.strip().splitlines()), 2)

    def test_max_depth_filter(self):
        text = report.render_flat(SAMPLE, max_depth=0)
        self.assertEqual(len(text.strip().splitlines()), 1)

    def test_errors_are_marked(self):
        rows = [entry("/r/x", type="file", error="permission denied")]
        self.assertIn("permission denied", report.render_flat(rows))


class TestTree(unittest.TestCase):
    def test_indentation_follows_depth(self):
        text = report.render_tree(SAMPLE)
        lines = [line for line in text.splitlines() if line.strip()]
        root = next(line for line in lines if "/r" in line and "sub" not in line)
        child = next(line for line in lines if "big.bin" in line)
        self.assertLess(len(root) - len(root.lstrip()), len(child) - len(child.lstrip()))

    def test_children_precede_or_follow_consistently(self):
        text = report.render_tree(SAMPLE)
        self.assertLess(text.index("/r/big.bin"), text.index("small.txt"))

    def test_empty_input(self):
        self.assertEqual(report.render_tree([]).strip(), "")

    def test_uses_ascii_by_default_so_windows_consoles_work(self):
        text = report.render_tree(SAMPLE)
        text.encode("ascii")

    def test_unicode_option_uses_box_drawing(self):
        text = report.render_tree(SAMPLE, ascii_only=False)
        self.assertTrue(any(ch in text for ch in "├└─"))


class TestJson(unittest.TestCase):
    def test_round_trips(self):
        payload = json.loads(report.render_json(SAMPLE))
        self.assertEqual(len(payload["entries"]), 4)

    def test_summary_block_is_present(self):
        payload = json.loads(report.render_json(SAMPLE))
        self.assertEqual(payload["summary"]["total_bytes"], 1524)

    def test_summary_counts_files_and_dirs(self):
        payload = json.loads(report.render_json(SAMPLE))
        self.assertEqual(payload["summary"]["file_count"], 2)
        self.assertEqual(payload["summary"]["dir_count"], 2)

    def test_surrogate_paths_stay_parseable(self):
        from msys2_tree_size import paths

        weird = paths.decode_path(b"/r/\xff")
        payload = json.loads(report.render_json([entry(weird, size=1, type="file")]))
        self.assertEqual(paths.encode_path(payload["entries"][0]["path"]), b"/r/\xff")

    def test_empty_input(self):
        payload = json.loads(report.render_json([]))
        self.assertEqual(payload["entries"], [])
        self.assertEqual(payload["summary"]["total_bytes"], 0)


class TestSummaryBlock(unittest.TestCase):
    def test_totals_match_root_entry(self):
        summary = report.summarize(SAMPLE)
        self.assertEqual(summary["total_bytes"], 1524)

    def test_empty(self):
        summary = report.summarize([])
        self.assertEqual(summary["total_bytes"], 0)
        self.assertEqual(summary["file_count"], 0)

    def test_largest_entry_is_reported(self):
        summary = report.summarize(SAMPLE)
        self.assertEqual(summary["largest"]["path"], "/r/big.bin")

    def test_extension_breakdown_included(self):
        summary = report.summarize(SAMPLE)
        self.assertEqual(summary["extensions"][".bin"], 1000)

    def test_entropy_present(self):
        summary = report.summarize(SAMPLE)
        self.assertIn("entropy_bits", summary)


class TestWriteReports(TempDirTestCase):
    def setUp(self):
        self.tmp = self.make_temp_dir()

    def test_write_flat_report(self):
        target = self.tmp / "flat.txt"
        report.write_flat(target, SAMPLE)
        self.assertIn("/r/big.bin", target.read_text(encoding="utf-8"))

    def test_write_json_report(self):
        target = self.tmp / "out.json"
        report.write_json(target, SAMPLE)
        self.assertEqual(len(json.loads(target.read_text(encoding="utf-8"))["entries"]), 4)

    def test_write_duplicates_csv(self):
        target = self.tmp / "dupes.csv"
        rows = [{"sha256": "h", "path": "/r/a", "duplicate_count": 2}]
        report.write_duplicates_csv(target, rows)
        text = target.read_text(encoding="utf-8")
        self.assertIn("sha256", text)
        self.assertIn("/r/a", text)


if __name__ == "__main__":
    unittest.main()
