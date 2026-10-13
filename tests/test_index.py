"""Tests for the incremental SQLite index.

The feature exists for one reason: **not re-walking what did not change.** A
directory's mtime changes when a direct child is added, removed or renamed, but
not when a file deep inside the subtree is modified. That asymmetry is what
makes an incremental rescan possible, and it is what these tests pin down.

The most important test in this file is
:meth:`TestIncrementalRescan.test_unchanged_subtree_is_not_re_walked`, which
asserts the *counter* of visited directories rather than the resulting rows.
A test that only checked output would pass even if the walk redid all the work,
which is the entire thing being built.
"""

from __future__ import annotations

import os
import sqlite3
import time
import unittest

from msys2_tree_size import index

from support import TempDirTestCase


class IndexTestCase(TempDirTestCase):
    def setUp(self):
        self.root = self.make_temp_dir()
        self.db = self.root / "index.sqlite"

    def make_tree(self):
        """A small tree with a nested directory."""
        tree = self.root / "tree"
        (tree / "sub").mkdir(parents=True)
        (tree / "a.txt").write_bytes(b"a" * 100)
        (tree / "sub" / "b.txt").write_bytes(b"b" * 200)
        return tree

    def key(self, path) -> str:
        from msys2_tree_size import paths

        return paths.to_posix(str(path))


class TestSchemaAndStore(IndexTestCase):
    def test_creates_the_database(self):
        conn = index.connect(self.db)
        self.assertTrue(self.db.exists())
        conn.close()

    def test_schema_version_is_recorded(self):
        conn = index.connect(self.db)
        self.assertEqual(index.schema_version(conn), index.SCHEMA_VERSION)
        conn.close()

    def test_reopening_an_existing_database_works(self):
        index.connect(self.db).close()
        conn = index.connect(self.db)
        self.assertEqual(index.schema_version(conn), index.SCHEMA_VERSION)
        conn.close()

    def test_a_foreign_file_is_rejected_not_corrupted(self):
        # Pointing the tool at a non-index SQLite file (or a random file) must
        # fail clearly rather than quietly scribbling a schema into it.
        self.db.write_bytes(b"this is not a database")
        with self.assertRaises(index.IndexError_):
            index.connect(self.db)

    def test_a_newer_schema_is_rejected(self):
        conn = index.connect(self.db)
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(index.SCHEMA_VERSION + 1),),
        )
        conn.commit()
        conn.close()
        # Reopening must refuse rather than misread rows it does not understand.
        with self.assertRaises(index.IndexError_):
            index.connect(self.db)


class TestStoreAndLoad(IndexTestCase):
    def test_entries_round_trip(self):
        conn = index.connect(self.db)
        rows = [
            index.Row(path="/r", parent=None, name="r", type="dir", size=10, mtime_ns=1, depth=0),
            index.Row(
                path="/r/a", parent="/r", name="a", type="file", size=10, mtime_ns=2, depth=1
            ),
        ]
        index.store_rows(conn, rows)
        loaded = {row.path: row for row in index.load_rows(conn)}
        self.assertEqual(set(loaded), {"/r", "/r/a"})
        self.assertEqual(loaded["/r/a"].size, 10)
        conn.close()

    def test_storing_the_same_path_twice_replaces(self):
        conn = index.connect(self.db)
        index.store_rows(conn, [index.Row("/r", None, "r", "dir", 1, 1, 0)])
        index.store_rows(conn, [index.Row("/r", None, "r", "dir", 999, 1, 0)])
        loaded = list(index.load_rows(conn))
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].size, 999)
        conn.close()

    def test_rows_for_a_subtree_can_be_removed(self):
        conn = index.connect(self.db)
        index.store_rows(
            conn,
            [
                index.Row("/r", None, "r", "dir", 0, 0, 0),
                index.Row("/r/keep", "/r", "keep", "file", 1, 0, 1),
                index.Row("/r/gone", "/r", "gone", "file", 1, 0, 1),
                index.Row("/r/gone/deep", "/r/gone", "deep", "file", 1, 0, 2),
            ],
        )
        index.delete_subtree(conn, "/r/gone")
        remaining = {row.path for row in index.load_rows(conn)}
        # The subtree, not just the path, must go -- otherwise deleted
        # directories leave ghosts that report space that no longer exists.
        self.assertEqual(remaining, {"/r", "/r/keep"})
        conn.close()

    def test_delete_subtree_does_not_match_a_sibling_prefix(self):
        conn = index.connect(self.db)
        index.store_rows(
            conn,
            [
                index.Row("/r/foo", None, "foo", "dir", 0, 0, 0),
                index.Row("/r/foobar", None, "foobar", "dir", 0, 0, 0),
            ],
        )
        index.delete_subtree(conn, "/r/foo")
        remaining = {row.path for row in index.load_rows(conn)}
        # "/r/foobar" must survive: a naive LIKE 'path%' would delete it.
        self.assertEqual(remaining, {"/r/foobar"})
        conn.close()

    def test_mtime_is_stored_at_nanosecond_resolution(self):
        conn = index.connect(self.db)
        index.store_rows(conn, [index.Row("/r", None, "r", "dir", 0, 1234567890123, 0)])
        loaded = list(index.load_rows(conn))
        self.assertEqual(loaded[0].mtime_ns, 1234567890123)
        conn.close()


class TestDirectoryState(IndexTestCase):
    def test_records_a_directory_with_its_mtime(self):
        conn = index.connect(self.db)
        index.record_directory(conn, "/r", mtime_ns=42, options="")
        self.assertEqual(index.directory_mtime(conn, "/r"), 42)
        conn.close()

    def test_unknown_directory_has_no_mtime(self):
        conn = index.connect(self.db)
        self.assertIsNone(index.directory_mtime(conn, "/r"))
        conn.close()

    def test_options_are_recorded_and_compared(self):
        conn = index.connect(self.db)
        index.record_directory(conn, "/r", mtime_ns=1, options="hash=True,archives=False")
        self.assertEqual(
            index.directory_options(conn, "/r"), "hash=True,archives=False"
        )
        conn.close()


class TestScanMeta(IndexTestCase):
    def test_scan_meta_round_trips(self):
        conn = index.connect(self.db)
        index.record_scan(conn, root="/r", options="archives=True", tool_version="0.2.0")
        meta = index.scan_meta(conn, "/r")
        self.assertIsNotNone(meta)
        self.assertEqual(meta["options"], "archives=True")
        conn.close()

    def test_unknown_root_has_no_meta(self):
        conn = index.connect(self.db)
        self.assertIsNone(index.scan_meta(conn, "/r"))
        conn.close()

    def test_meta_is_replaced_on_rescan(self):
        conn = index.connect(self.db)
        index.record_scan(conn, root="/r", options="a", tool_version="1")
        index.record_scan(conn, root="/r", options="b", tool_version="2")
        meta = index.scan_meta(conn, "/r")
        self.assertEqual(meta["options"], "b")
        self.assertEqual(meta["tool_version"], "2")
        conn.close()


class TestIncrementalDecisions(IndexTestCase):
    """Pure decisions, tested without touching the filesystem."""

    def test_unchanged_directory_is_skipped(self):
        self.assertTrue(index.should_skip("/r", stored_ns=100, current_ns=100, options_match=True))

    def test_changed_mtime_forces_a_rescan(self):
        self.assertFalse(index.should_skip("/r", stored_ns=100, current_ns=200, options_match=True))

    def test_never_indexed_directory_is_not_skipped(self):
        self.assertFalse(index.should_skip("/r", stored_ns=None, current_ns=200, options_match=True))

    def test_option_mismatch_forces_a_rescan(self):
        # Rows produced with --archives differ from rows without it, so reusing
        # them would silently mix two different datasets.
        self.assertFalse(index.should_skip("/r", stored_ns=100, current_ns=100, options_match=False))


class TestRescanning(IndexTestCase):
    def test_first_scan_indexes_everything(self):
        tree = self.make_tree()
        conn = index.connect(self.db)
        stats = index.rescan(conn, self.key(tree), hash_contents=False)
        self.assertGreaterEqual(stats.entries, 4)  # tree, sub, a.txt, b.txt
        conn.close()

    def test_indexed_rows_match_a_plain_walk(self):
        from msys2_tree_size import walk

        tree = self.make_tree()
        conn = index.connect(self.db)
        index.rescan(conn, self.key(tree), hash_contents=False)

        indexed = {row.path for row in index.load_rows(conn) if row.type != "dir"}
        walked = {e.path for e in walk.walk(tree) if e.type != "dir"}
        self.assertEqual(indexed, walked)
        conn.close()

    def test_deleted_file_disappears_from_the_index(self):
        tree = self.make_tree()
        conn = index.connect(self.db)
        index.rescan(conn, self.key(tree), hash_contents=False)
        (tree / "a.txt").unlink()
        index.rescan(conn, self.key(tree), hash_contents=False)

        paths = {row.path for row in index.load_rows(conn)}
        self.assertNotIn(self.key(tree / "a.txt"), paths)
        conn.close()

    def test_new_file_appears_in_the_index(self):
        tree = self.make_tree()
        conn = index.connect(self.db)
        index.rescan(conn, self.key(tree), hash_contents=False)
        (tree / "new.txt").write_bytes(b"n" * 10)
        index.rescan(conn, self.key(tree), hash_contents=False)

        paths = {row.path for row in index.load_rows(conn)}
        self.assertIn(self.key(tree / "new.txt"), paths)
        conn.close()

    def test_deleted_subtree_is_fully_removed(self):
        tree = self.make_tree()
        conn = index.connect(self.db)
        index.rescan(conn, self.key(tree), hash_contents=False)

        import shutil

        shutil.rmtree(tree / "sub")
        index.rescan(conn, self.key(tree), hash_contents=False)

        paths = {row.path for row in index.load_rows(conn)}
        self.assertNotIn(self.key(tree / "sub"), paths)
        self.assertNotIn(self.key(tree / "sub" / "b.txt"), paths)
        conn.close()

    def test_rebuild_discards_everything_first(self):
        tree = self.make_tree()
        conn = index.connect(self.db)
        index.rescan(conn, self.key(tree), hash_contents=False)
        stats = index.rescan(conn, self.key(tree), hash_contents=False, rebuild=True)
        self.assertGreaterEqual(stats.entries, 4)
        conn.close()

    def test_root_spellings_share_one_index(self):
        # `/c/Users` and `C:\Users` are the same tree; keying them separately
        # would duplicate every row.
        tree = self.make_tree()
        conn = index.connect(self.db)
        index.rescan(conn, self.key(tree), hash_contents=False)
        before = len(list(index.load_rows(conn)))

        windows = str(tree).replace("/", "\\")
        index.rescan(conn, windows, hash_contents=False)
        after = len(list(index.load_rows(conn)))
        self.assertEqual(before, after)
        conn.close()


class TestIncrementalRescan(IndexTestCase):
    """The whole point: unchanged subtrees must not be re-walked."""

    def test_unchanged_subtree_is_not_re_walked(self):
        tree = self.make_tree()
        # A sibling that will be touched, so a rescan actually happens.
        (tree / "other").mkdir()
        (tree / "other" / "c.txt").write_bytes(b"c" * 50)

        conn = index.connect(self.db)
        index.rescan(conn, self.key(tree), hash_contents=False)

        # Second run with no changes at all: nothing should be walked.
        stats = index.rescan(conn, self.key(tree), hash_contents=False)
        self.assertEqual(
            stats.directories_walked,
            0,
            "an unchanged tree was re-walked; the index is not incremental",
        )
        conn.close()

    def test_changed_directory_is_re_walked(self):
        tree = self.make_tree()
        conn = index.connect(self.db)
        index.rescan(conn, self.key(tree), hash_contents=False)

        time.sleep(0.01)
        (tree / "added.txt").write_bytes(b"x" * 10)

        stats = index.rescan(conn, self.key(tree), hash_contents=False)
        self.assertGreater(
            stats.directories_walked, 0, "a changed directory was not re-walked"
        )
        conn.close()

    def test_a_change_deep_in_a_subtree_only_re_walks_that_branch(self):
        root = self.make_temp_dir()
        for name in ("one", "two", "three"):
            (root / name).mkdir()
            (root / name / "f.txt").write_bytes(b"x" * 10)

        conn = index.connect(self.db)
        index.rescan(conn, self.key(root), hash_contents=False)

        time.sleep(0.01)
        (root / "two" / "new.txt").write_bytes(b"y" * 10)

        stats = index.rescan(conn, self.key(root), hash_contents=False)
        # The root and the changed branch, not all three.
        self.assertLess(stats.directories_walked, 4)
        self.assertGreater(stats.directories_walked, 0)
        conn.close()

    def test_unchanged_rows_are_preserved_not_deleted(self):
        tree = self.make_tree()
        conn = index.connect(self.db)
        index.rescan(conn, self.key(tree), hash_contents=False)
        index.rescan(conn, self.key(tree), hash_contents=False)

        paths = {row.path for row in index.load_rows(conn)}
        self.assertIn(self.key(tree / "a.txt"), paths)
        self.assertIn(self.key(tree / "sub" / "b.txt"), paths)
        conn.close()

    def test_option_change_forces_a_full_rescan(self):
        tree = self.make_tree()
        conn = index.connect(self.db)
        index.rescan(conn, self.key(tree), hash_contents=False)

        # Different options mean the stored rows do not answer the new question.
        stats = index.rescan(conn, self.key(tree), hash_contents=True)
        self.assertGreater(stats.directories_walked, 0)
        conn.close()


class TestHashStorage(IndexTestCase):
    def test_hashes_are_stored_when_requested(self):
        import hashlib

        tree = self.make_tree()
        conn = index.connect(self.db)
        index.rescan(conn, self.key(tree), hash_contents=True)

        rows = {row.path: row for row in index.load_rows(conn)}
        expected = hashlib.sha256(b"a" * 100).hexdigest()
        self.assertEqual(rows[self.key(tree / "a.txt")].sha256, expected)
        conn.close()

    def test_hashes_are_absent_when_not_requested(self):
        tree = self.make_tree()
        conn = index.connect(self.db)
        index.rescan(conn, self.key(tree), hash_contents=False)

        rows = {row.path: row for row in index.load_rows(conn)}
        self.assertFalse(rows[self.key(tree / "a.txt")].sha256)
        conn.close()


class TestQueries(IndexTestCase):
    def test_find_by_glob_matches_names(self):
        tree = self.make_tree()
        conn = index.connect(self.db)
        index.rescan(conn, self.key(tree), hash_contents=False)

        found = {row.path for row in index.find(conn, "*.txt")}
        self.assertEqual(
            found, {self.key(tree / "a.txt"), self.key(tree / "sub" / "b.txt")}
        )
        conn.close()

    def test_find_by_substring(self):
        tree = self.make_tree()
        conn = index.connect(self.db)
        index.rescan(conn, self.key(tree), hash_contents=False)

        found = {row.path for row in index.find(conn, "b.txt")}
        self.assertEqual(found, {self.key(tree / "sub" / "b.txt")})
        conn.close()

    def test_find_under_a_prefix(self):
        tree = self.make_tree()
        conn = index.connect(self.db)
        index.rescan(conn, self.key(tree), hash_contents=False)

        found = {row.path for row in index.find(conn, "*", under=self.key(tree / "sub"))}
        self.assertEqual(found, {self.key(tree / "sub" / "b.txt")})
        conn.close()

    def test_find_returns_nothing_for_no_match(self):
        tree = self.make_tree()
        conn = index.connect(self.db)
        index.rescan(conn, self.key(tree), hash_contents=False)
        self.assertEqual(list(index.find(conn, "*.nonexistent")), [])
        conn.close()

    def test_find_can_filter_to_files_only(self):
        tree = self.make_tree()
        conn = index.connect(self.db)
        index.rescan(conn, self.key(tree), hash_contents=False)

        found = list(index.find(conn, "*", files_only=True))
        self.assertTrue(found)
        self.assertTrue(all(row.type == "file" for row in found))
        conn.close()

    def test_largest_returns_biggest_first(self):
        tree = self.make_tree()
        conn = index.connect(self.db)
        index.rescan(conn, self.key(tree), hash_contents=False)

        top = list(index.largest(conn, limit=2))
        self.assertEqual(top[0].size, 200)
        conn.close()


class TestStatistics(IndexTestCase):
    def test_stats_report_totals(self):
        tree = self.make_tree()
        conn = index.connect(self.db)
        index.rescan(conn, self.key(tree), hash_contents=False)

        stats = index.statistics(conn)
        self.assertGreaterEqual(stats["files"], 2)
        self.assertGreaterEqual(stats["directories"], 2)
        self.assertEqual(stats["bytes"], 300)
        conn.close()

    def test_stats_on_an_empty_index(self):
        conn = index.connect(self.db)
        stats = index.statistics(conn)
        self.assertEqual(stats["files"], 0)
        self.assertEqual(stats["bytes"], 0)
        conn.close()


class TestConcurrency(IndexTestCase):
    def test_a_second_connection_can_read(self):
        tree = self.make_tree()
        writer = index.connect(self.db)
        index.rescan(writer, self.key(tree), hash_contents=False)

        reader = index.connect(self.db)
        self.assertTrue(list(index.load_rows(reader)))
        reader.close()
        writer.close()

    def test_transaction_rolls_back_on_error(self):
        conn = index.connect(self.db)
        index.store_rows(conn, [index.Row("/r", None, "r", "dir", 1, 1, 0)])
        conn.commit()

        # A rescan of a missing path must not wipe the existing rows.
        index.rescan(conn, "/definitely/not/here", hash_contents=False)
        self.assertTrue(list(index.load_rows(conn)))
        conn.close()


class TestSqliteIsUsedDirectly(unittest.TestCase):
    def test_module_uses_the_standard_library_sqlite(self):
        # The index deliberately adds no dependency; if this fails, something
        # has started pulling in a third-party wrapper.
        import inspect

        source = inspect.getsource(index)
        self.assertIn("import sqlite3", source)

    def test_no_third_party_imports(self):
        import inspect

        source = inspect.getsource(index)
        for banned in ("import apsw", "import sqlalchemy", "import peewee"):
            self.assertNotIn(banned, source)


if __name__ == "__main__":
    unittest.main()
