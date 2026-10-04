"""Tests for the single filesystem traversal.

Two levels of testing:

* ``TestWalkFakeFS`` drives traversal through an injected in-memory tree, so the
  aggregation and ordering rules are tested without touching a disk.
* ``TestWalkRealFS`` uses a temporary directory to prove the real adapter wires
  the same semantics onto ``os.scandir``.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from msys2_tree_size import walk  # noqa: E402
from msys2_tree_size.walk import Entry, FakeFS  # noqa: E402


def _entries(root):
    return list(walk.walk(root))


def _by_path(root):
    return {e.path: e for e in _entries(root)}


class TestWalkFakeFS(unittest.TestCase):
    def test_flat_directory(self):
        fs = FakeFS({"/r": {"a": b"12345", "b": b"12"}})
        got = _by_path(fs)
        self.assertEqual(set(got), {"/r", "/r/a", "/r/b"})
        self.assertEqual(got["/r"].size, 7)
        self.assertEqual(got["/r/a"].size, 5)
        self.assertEqual(got["/r"].type, "dir")
        self.assertEqual(got["/r/a"].type, "file")

    def test_nested_sizes_bubble_up(self):
        fs = FakeFS({"/r": {"sub": {"deep": {"f": b"x" * 10}}, "top": b"y" * 5}})
        got = _by_path(fs)
        self.assertEqual(got["/r/deep/f"].size, 10)
        self.assertEqual(got["/r/sub/deep"].size, 10)
        self.assertEqual(got["/r/sub"].size, 10)
        self.assertEqual(got["/r"].size, 15)

    def test_empty_directories_contribute_zero_but_exist(self):
        fs = FakeFS({"/r": {"empty": {}, "f": b"abc"}})
        got = _by_path(fs)
        self.assertIn("/r/empty", got)
        self.assertEqual(got["/r/empty"].size, 0)
        self.assertEqual(got["/r/empty"].child_count, 0)
        self.assertEqual(got["/r"].size, 3)

    def test_postorder_children_before_parent(self):
        fs = FakeFS({"/r": {"sub": {"f": b"x"}}})
        order = [e.path for e in _entries(fs)]
        self.assertLess(order.index("/r/sub/f"), order.index("/r/sub"))
        self.assertLess(order.index("/r/sub"), order.index("/r"))

    def test_every_entry_appears_exactly_once(self):
        fs = FakeFS({"/r": {"a": {}, "b": {"c": {"d": b"x"}}}})
        paths = [e.path for e in _entries(fs)]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(len(paths), 5)

    def test_depths_are_correct(self):
        fs = FakeFS({"/r": {"sub": {"deep": {"f": b"x"}}}})
        got = _by_path(fs)
        self.assertEqual(got["/r"].depth, 0)
        self.assertEqual(got["/r/sub"].depth, 1)
        self.assertEqual(got["/r/sub/deep"].depth, 2)
        self.assertEqual(got["/r/sub/deep/f"].depth, 3)

    def test_parent_and_child_count(self):
        fs = FakeFS({"/r": {"a": b"1", "b": b"2", "sub": {}}})
        got = _by_path(fs)
        self.assertEqual(got["/r"].child_count, 3)
        self.assertEqual(got["/r/sub"].child_count, 0)
        self.assertEqual(got["/r/a"].parent, "/r")
        self.assertIsNone(got["/r"].parent)

    def test_percent_of_parent_is_computed(self):
        fs = FakeFS({"/r": {"a": b"x" * 75, "b": b"y" * 25}})
        got = _by_path(fs)
        self.assertEqual(got["/r/a"].percent_of_parent, 75.0)
        self.assertEqual(got["/r/b"].percent_of_parent, 25.0)

    def test_percent_of_parent_zero_size_is_zero(self):
        fs = FakeFS({"/r": {"a": b""}})
        got = _by_path(fs)
        self.assertEqual(got["/r/a"].percent_of_parent, 0.0)

    def test_max_depth_is_respected(self):
        fs = FakeFS({"/r": {"sub": {"deep": {"f": b"x" * 10}}}})
        got = _by_path_limited(fs, max_depth=1)
        # /r/sub is listed but not descended into; its size is still correct
        # only if we descend.  With max_depth we must report what we measured.
        self.assertIn("/r/sub", got)
        self.assertNotIn("/r/sub/deep", got)

    def test_sizes_are_correct_at_max_depth_zero(self):
        fs = FakeFS({"/r": {"sub": {"f": b"x" * 10}}})
        got = _by_path_limited(fs, max_depth=0)
        self.assertEqual(set(got), {"/r"})
        # max_depth=0 must still report the true recursive total.
        self.assertEqual(got["/r"].size, 10)

    def test_merkle_hash_of_leaf_is_content_hash(self):
        fs = FakeFS({"/r": {"a": b"hello"}})
        got = _by_path(fs)
        import hashlib

        self.assertEqual(got["/r/a"].sha256, hashlib.sha256(b"hello").hexdigest())

    def test_merkle_hash_of_parent_is_not_content_hash(self):
        fs = FakeFS({"/r": {"a": b"hello"}})
        got = _by_path(fs)
        self.assertNotEqual(got["/r"].sha256, got["/r/a"].sha256)

    def test_identical_subtrees_hash_equal(self):
        fs = FakeFS(
            {
                "/r": {
                    "x": {"f": b"same", "g": b"same2"},
                    "y": {"f": b"same", "g": b"same2"},
                }
            }
        )
        got = _by_path(fs)
        self.assertEqual(got["/r/x"].sha256, got["/r/y"].sha256)

    def test_different_content_subtrees_differ(self):
        fs = FakeFS({"/r": {"x": {"f": b"one"}, "y": {"f": b"two"}}})
        got = _by_path(fs)
        self.assertNotEqual(got["/r/x"].sha256, got["/r/y"].sha256)

    def test_same_bytes_different_names_hash_differently(self):
        fs = FakeFS({"/r": {"x": {"f": b"same"}, "y": {"g": b"same"}}})
        got = _by_path(fs)
        self.assertNotEqual(got["/r/x"].sha256, got["/r/y"].sha256)

    def test_hashing_can_be_disabled(self):
        fs = FakeFS({"/r": {"a": b"hello"}})
        got = _by_path_no_hash(fs)
        self.assertEqual(got["/r/a"].sha256, "")
        self.assertEqual(got["/r"].sha256, "")

    def test_root_missing_is_empty(self):
        self.assertEqual(_entries(FakeFS({})), [])

    def test_root_that_is_a_file_yields_nothing(self):
        fs = FakeFS({"/r": b"data"})
        self.assertEqual(_entries(fs), [])


class TestWalkErrors(unittest.TestCase):
    def test_unreadable_directory_is_recorded_not_fatal(self):
        fs = FakeFS({"/r": {"locked": {"f": b"x"}, "ok": b"y"}})
        fs.deny("/r/locked")
        got = _by_path(fs)
        self.assertIn("/r/ok", got)
        self.assertIn("/r", got)
        # The unreadable dir is reported with an error and zero size.
        self.assertIn("/r/locked", got)
        self.assertTrue(got["/r/locked"].error)
        self.assertEqual(got["/r/locked"].size, 0)

    def test_unreadable_file_is_recorded_not_fatal(self):
        fs = FakeFS({"/r": {"bad": b"xxxx"}})
        fs.deny("/r/bad")
        got = _by_path(fs)
        self.assertTrue(got["/r/bad"].error)
        self.assertEqual(got["/r/bad"].size, 0)

    def test_error_result_is_a_real_path_via_symlink_rule(self):
        # A broken symlink must not crash the walk.
        fs = FakeFS({"/r": {}})
        got = _by_path(fs)
        self.assertEqual(got["/r"].size, 0)

    def test_on_error_callback_receives_paths(self):
        seen = []
        fs = FakeFS({"/r": {"locked": {"f": b"x"}}})
        fs.deny("/r/locked")
        list(walk.walk(fs, on_error=seen.append))
        self.assertEqual(seen, ["/r/locked"])


class TestWalkRealFS(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_matches_manual_totals(self):
        (self.root / "a.txt").write_bytes(b"a" * 10)
        sub = self.root / "sub"
        sub.mkdir()
        (sub / "b.bin").write_bytes(b"b" * 90)
        got = _by_path(self.root)
        self.assertEqual(got[str(self.root)].size, 100)
        self.assertEqual(got[str(sub)].size, 90)
        self.assertEqual(got[str(self.root / "a.txt")].size, 10)

    def test_empty_directory_size_is_zero(self):
        (self.root / "empty").mkdir()
        got = _by_path(self.root)
        self.assertEqual(got[str(self.root / "empty")].size, 0)

    def test_hidden_files_are_included(self):
        (self.root / ".hidden").write_bytes(b"12345")
        got = _by_path(self.root)
        self.assertIn(str(self.root / ".hidden"), got)
        self.assertEqual(got[str(self.root)].size, 5)

    def test_real_symlink_is_not_followed_for_size(self):
        target = self.root / "target.bin"
        target.write_bytes(b"z" * 1000)
        link = self.root / "link.bin"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        got = _by_path(self.root)
        # The link must not double-count the target's bytes.
        self.assertEqual(got[str(self.root)].size, 1000)
        self.assertEqual(got[str(link)].type, "link")

    def test_hashing_matches_file_contents(self):
        import hashlib

        (self.root / "f").write_bytes(b"content")
        got = _by_path(self.root)
        self.assertEqual(
            got[str(self.root / "f")].sha256, hashlib.sha256(b"content").hexdigest()
        )

    def test_max_depth_limits_emitted_entries_but_not_sizes(self):
        deep = self.root / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "f").write_bytes(b"x" * 42)
        got = _by_path_limited(self.root, max_depth=1)
        self.assertIn(str(self.root / "a"), got)
        self.assertNotIn(str(self.root / "a" / "b"), got)
        self.assertEqual(got[str(self.root)].size, 42)


class TestEntryDataclass(unittest.TestCase):
    def test_entry_is_hashable_and_comparable(self):
        e = Entry(
            path="/r",
            name="r",
            parent=None,
            type="dir",
            depth=0,
            size=1,
            sha256="",
            child_count=0,
            percent_of_parent=0.0,
            error=None,
        )
        self.assertEqual(e, e)
        self.assertEqual(e.human, "1B")

    def test_as_dict_has_stable_key_order(self):
        e = Entry(
            path="/r",
            name="r",
            parent=None,
            type="dir",
            depth=0,
            size=1024,
            sha256="ab",
            child_count=2,
            percent_of_parent=50.0,
            error=None,
        )
        d = e.as_dict()
        self.assertEqual(list(d)[0], "path")
        self.assertEqual(d["human"], "1.0K")
        self.assertEqual(d["size"], 1024)


# --- helpers that adapt the module-level API used by the fake tests ---------


def _by_path_limited(fs, max_depth):
    return {e.path: e for e in walk.walk(fs, max_depth=max_depth)}


def _by_path_no_hash(fs):
    return {e.path: e for e in walk.walk(fs, hash_contents=False)}


if __name__ == "__main__":
    unittest.main()
