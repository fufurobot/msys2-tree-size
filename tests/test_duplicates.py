"""Tests for duplicate detection.

The subtle rule under test is the *parent/child* rule: when a directory's
subtree hash equals one of its children's hashes, the child contributes no new
content and must not be reported as a duplicate of its own ancestor.  Without
it, every byte in such a subtree would be counted twice.

The other rule is that unreadable files (null digest) are never duplicates:
they were not hashed, so nothing is known about their content.
"""

from __future__ import annotations

import unittest

from msys2_tree_size import duplicates  # noqa: E402
from msys2_tree_size.walk import Entry


def entry(
    path,
    *,
    sha256="",
    size=0,
    type="file",
    parent=None,
    depth=0,
    name=None,
    error=None,
):
    """Build an Entry with sensible defaults, for readable test data."""
    return Entry(
        path=path,
        name=name if name is not None else path.rsplit("/", 1)[-1],
        parent=parent,
        type=type,
        depth=depth,
        size=size,
        sha256=sha256,
        child_count=0,
        percent_of_parent=0.0,
        error=error,
    )


class TestFindDuplicates(unittest.TestCase):
    def test_two_identical_files_are_duplicates(self):
        rows = [
            entry("/r/a", sha256="h1", size=10),
            entry("/r/b", sha256="h1", size=10),
            entry("/r/c", sha256="h2", size=10),
        ]
        groups = duplicates.find_duplicates(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual({r.path for r in groups[0]}, {"/r/a", "/r/b"})

    def test_unique_files_produce_no_groups(self):
        rows = [entry("/r/a", sha256="h1"), entry("/r/b", sha256="h2")]
        self.assertEqual(duplicates.find_duplicates(rows), [])

    def test_null_digests_are_ignored(self):
        rows = [
            entry("/r/a", sha256="", error="permission denied"),
            entry("/r/b", sha256=""),
        ]
        self.assertEqual(duplicates.find_duplicates(rows), [])

    def test_three_way_duplicate_forms_one_group(self):
        rows = [entry(f"/r/{c}", sha256="h", size=5) for c in "abc"]
        groups = duplicates.find_duplicates(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 3)

    def test_groups_are_sorted_deterministically(self):
        rows = [
            entry("/r/z", sha256="h2", size=1),
            entry("/r/a", sha256="h1", size=1),
            entry("/r/b", sha256="h1", size=1),
            entry("/r/y", sha256="h2", size=1),
        ]
        groups = duplicates.find_duplicates(rows)
        self.assertEqual([g[0].sha256 for g in groups], ["h1", "h2"])
        self.assertEqual([r.path for r in groups[0]], ["/r/a", "/r/b"])
        self.assertEqual([r.path for r in groups[1]], ["/r/y", "/r/z"])

    def test_directories_are_included(self):
        # Two identical subtrees are genuinely duplicated content.
        rows = [
            entry("/r/x", sha256="h", type="dir", size=100),
            entry("/r/y", sha256="h", type="dir", size=100),
        ]
        groups = duplicates.find_duplicates(rows)
        self.assertEqual(len(groups), 1)


class TestParentChildRule(unittest.TestCase):
    def test_child_matching_parent_hash_is_dropped(self):
        # /r/dir and /r/dir/same hold byte-identical content; reporting both
        # would double-count the subtree.
        rows = [
            entry("/r/dir/same", sha256="H", type="dir", size=100, parent="/r/dir", depth=2),
            entry("/r/dir", sha256="H", type="dir", size=100, parent="/r", depth=1),
        ]
        groups = duplicates.find_duplicates(rows)
        self.assertEqual(groups, [])

    def test_grandchild_chain_is_fully_dropped(self):
        rows = [
            entry("/r/a/b/c", sha256="H", type="dir", size=10, parent="/r/a/b", depth=3),
            entry("/r/a/b", sha256="H", type="dir", size=10, parent="/r/a", depth=2),
            entry("/r/a", sha256="H", type="dir", size=10, parent="/r", depth=1),
        ]
        self.assertEqual(duplicates.find_duplicates(rows), [])

    def test_unrelated_same_hash_directory_still_reported(self):
        rows = [
            entry("/r/x/same", sha256="H", type="dir", size=10, parent="/r/x", depth=2),
            entry("/r/x", sha256="OTHER", type="dir", size=20, parent="/r", depth=1),
            entry("/r/y/same", sha256="H", type="dir", size=10, parent="/r/y", depth=2),
        ]
        groups = duplicates.find_duplicates(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual({r.path for r in groups[0]}, {"/r/x/same", "/r/y/same"})

    def test_rule_is_applied_per_parent_not_globally(self):
        # A file whose *name* collides is irrelevant; only the hash matters.
        rows = [
            entry("/r/a", sha256="H", size=1, parent="/r"),
            entry("/r/a2", sha256="H", size=1, parent="/r"),
            entry("/r2", sha256="H", type="dir", size=2, parent=None),
        ]
        groups = duplicates.find_duplicates(rows)
        self.assertEqual(len(groups), 1)


class TestDuplicateSummary(unittest.TestCase):
    def test_wasted_bytes_counts_all_but_one_copy(self):
        rows = [entry(f"/r/{c}", sha256="h", size=100) for c in "abc"]
        summary = duplicates.summarize(rows)
        self.assertEqual(summary["group_count"], 1)
        self.assertEqual(summary["duplicate_files"], 3)
        self.assertEqual(summary["wasted_bytes"], 200)

    def test_empty_input(self):
        summary = duplicates.summarize([])
        self.assertEqual(summary["group_count"], 0)
        self.assertEqual(summary["duplicate_files"], 0)
        self.assertEqual(summary["wasted_bytes"], 0)

    def test_no_duplicates(self):
        rows = [entry("/r/a", sha256="h1"), entry("/r/b", sha256="h2")]
        self.assertEqual(duplicates.summarize(rows)["group_count"], 0)

    def test_ignores_null_digests(self):
        rows = [entry("/r/a", sha256=""), entry("/r/b", sha256="")]
        self.assertEqual(duplicates.summarize(rows)["wasted_bytes"], 0)


class TestCrossDevice(unittest.TestCase):
    """``find_duplicates`` returns entries; device metadata lands on rows."""

    def test_same_hash_across_devices_flagged(self):
        rows = [
            entry("/d1/a", sha256="h", size=10),
            entry("/d2/b", sha256="h", size=10),
        ]
        groups = duplicates.find_duplicates(rows, device_of=lambda p: p.split("/")[1])
        out = duplicates.to_rows(groups, device_of=lambda p: p.split("/")[1])
        self.assertTrue(all(r["cross_device"] for r in out))
        self.assertEqual(out[0]["device_count"], 2)

    def test_same_device_not_flagged(self):
        rows = [entry("/d1/a", sha256="h", size=1), entry("/d1/b", sha256="h", size=1)]
        groups = duplicates.find_duplicates(rows, device_of=lambda p: p.split("/")[1])
        out = duplicates.to_rows(groups, device_of=lambda p: p.split("/")[1])
        self.assertFalse(any(r["cross_device"] for r in out))
        self.assertEqual(out[0]["device_count"], 1)

    def test_without_device_mapping_parent_is_used_as_proxy(self):
        rows = [
            entry("/d1/a", sha256="h", size=1, parent="/d1"),
            entry("/d2/b", sha256="h", size=1, parent="/d2"),
        ]
        groups = duplicates.find_duplicates(rows)
        out = duplicates.to_rows(groups)
        self.assertTrue(out[0]["cross_device"])


class TestToRows(unittest.TestCase):
    def test_rows_are_flat_dicts_with_group_metadata(self):
        rows = [entry("/r/a", sha256="h", size=10), entry("/r/b", sha256="h", size=10)]
        out = duplicates.to_rows(duplicates.find_duplicates(rows))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["duplicate_count"], 2)
        self.assertEqual(out[0]["sha256"], "h")
        self.assertIn("path", out[0])

    def test_sorted_by_hash_then_path(self):
        rows = [
            entry("/r/z", sha256="h1", size=1),
            entry("/r/a", sha256="h1", size=1),
            entry("/r/m", sha256="h2", size=1),
            entry("/r/n", sha256="h2", size=1),
        ]
        out = duplicates.to_rows(duplicates.find_duplicates(rows))
        self.assertEqual([r["path"] for r in out], ["/r/a", "/r/z", "/r/m", "/r/n"])


if __name__ == "__main__":
    unittest.main()
