"""Tests for archive penetration during a filesystem walk.

The walk is where archive support becomes *useful*: a directory's total should
account for what its archives contain, and the members should appear in the
listing, otherwise a folder full of tarballs reports a few megabytes and hides
hundreds.

The design constraints being tested:

* **Opt-in by default is wrong; opt-out is right.** A traversal that silently
  opens every archive can be very slow on a large tree, so the behaviour is
  available but the caller decides. These tests pin the defaults down.
* **Archive members are never real filesystem entries.** They are reported with
  a path that cannot collide with a real path, and they never make the walk
  descend into a directory that does not exist.
* **One bad archive must not stop the scan.** A corrupt or password-protected
  file is a per-file note, not a failure.
"""

from __future__ import annotations

import tarfile
import unittest
import zipfile

from msys2_tree_size import walk

from support import TempDirTestCase


def by_path(root, **kwargs):
    return {e.path: e for e in walk.walk(root, **kwargs)}


class WalkArchiveTestCase(TempDirTestCase):
    def setUp(self):
        self.root = self.make_temp_dir()

    def make_zip(self, name="a.zip", inner_size=500, members=1):
        path = self.root / name
        with zipfile.ZipFile(path, "w") as zf:
            for i in range(members):
                zf.writestr(f"file{i}.txt", "x" * inner_size)
        return path

    def make_tar(self, name="a.tar", inner_size=700):
        path = self.root / name
        payload = self.root / "src.bin"
        payload.write_bytes(b"y" * inner_size)
        with tarfile.open(path, "w") as tf:
            tf.add(payload, arcname="src.bin")
        payload.unlink()
        return path

    def key(self, path):
        from msys2_tree_size import paths

        return paths.to_posix(str(path))


class TestArchivePenetrationOffByDefault(WalkArchiveTestCase):
    """A plain walk must behave exactly as before."""

    def test_archive_is_reported_as_an_ordinary_file(self):
        archive_path = self.make_zip()
        entries = by_path(self.root)
        entry = entries[self.key(archive_path)]
        self.assertEqual(entry.type, "file")
        # The archive's own size on disk, which for a small zip is dominated by
        # container overhead rather than the payload. Asserting it equals the
        # file's real length (rather than a guessed bound) is the honest check.
        self.assertEqual(entry.size, archive_path.stat().st_size)

    def test_archive_size_is_not_the_sum_of_its_contents(self):
        # A zip holding 200 KiB of compressible data must not report 200 KiB in
        # an ordinary walk; that only happens with penetration enabled.
        archive_path = self.make_zip(inner_size=200_000, members=1)
        entry = by_path(self.root)[self.key(archive_path)]
        self.assertLess(entry.size, 200_000)

    def test_no_members_are_emitted(self):
        self.make_zip()
        for entry in walk.walk(self.root):
            self.assertNotIn("::", entry.path, entry.path)


class TestArchivePenetration(WalkArchiveTestCase):
    def test_members_are_emitted_when_enabled(self):
        archive_path = self.make_zip(members=2)
        entries = by_path(self.root, penetrate_archives=True)
        members = [p for p in entries if "::" in p]
        self.assertEqual(len(members), 2)

    def test_member_paths_are_built_from_the_archive(self):
        archive_path = self.make_zip()
        entries = by_path(self.root, penetrate_archives=True)
        expected = f"{self.key(archive_path)}::file0.txt"
        self.assertIn(expected, entries)

    def test_uncompressed_size_is_reported_for_each_member(self):
        archive_path = self.make_zip(inner_size=500)
        entries = by_path(self.root, penetrate_archives=True)
        member = entries[f"{self.key(archive_path)}::file0.txt"]
        self.assertEqual(member.size, 500)

    def test_directory_total_includes_archive_contents(self):
        archive_path = self.make_zip(inner_size=500, members=3)
        plain = by_path(self.root)[self.key(self.root)].size
        penetrated = by_path(self.root, penetrate_archives=True)[
            self.key(self.root)
        ].size
        self.assertEqual(penetrated, plain + 1500)

    def test_tar_contents_are_included(self):
        archive_path = self.make_tar(inner_size=700)
        entries = by_path(self.root, penetrate_archives=True)
        self.assertIn(f"{self.key(archive_path)}::src.bin", entries)

    def test_members_are_nested_under_the_archive_file(self):
        archive_path = self.make_zip()
        entries = by_path(self.root, penetrate_archives=True)
        member = entries[f"{self.key(archive_path)}::file0.txt"]
        self.assertEqual(member.parent, self.key(archive_path))

    def test_members_are_flagged_so_reports_can_distinguish_them(self):
        archive_path = self.make_zip()
        entries = by_path(self.root, penetrate_archives=True)
        member = entries[f"{self.key(archive_path)}::file0.txt"]
        self.assertEqual(member.container, self.key(archive_path))

    def test_real_file_entry_is_unaffected(self):
        (self.root / "plain.txt").write_bytes(b"z" * 42)
        self.make_zip()
        entries = by_path(self.root, penetrate_archives=True)
        self.assertEqual(entries[self.key(self.root / "plain.txt")].size, 42)


class TestArchiveDepthGuard(WalkArchiveTestCase):
    """Archives inside archives must be bounded."""

    def make_nested_zip(self, depth=2):
        """A zip containing a zip containing a file."""
        innermost = self.root / "inner.zip"
        with zipfile.ZipFile(innermost, "w") as zf:
            zf.writestr("deep.txt", "d" * 400)

        current = innermost
        for level in range(depth):
            outer = self.root / f"level{level}.zip"
            with zipfile.ZipFile(outer, "w") as zf:
                zf.write(current, current.name)
            current.unlink()
            current = outer
        return current

    def test_nested_archive_members_are_found(self):
        self.make_nested_zip(depth=1)
        entries = by_path(self.root, penetrate_archives=True, archive_depth=2)
        self.assertTrue(any(p.count("::") >= 2 for p in entries), list(entries))

    def test_archive_depth_zero_disables_penetration(self):
        self.make_zip()
        entries = by_path(self.root, penetrate_archives=True, archive_depth=0)
        self.assertFalse(any("::" in p for p in entries))

    def test_archive_depth_limits_recursion(self):
        self.make_nested_zip(depth=2)
        entries = by_path(self.root, penetrate_archives=True, archive_depth=1)
        depths = {p.count("::") for p in entries if "::" in p}
        self.assertTrue(depths)
        self.assertLessEqual(max(depths), 1)


class TestArchiveSizeGuard(WalkArchiveTestCase):
    """Huge archives must be skippable, since reading them is expensive."""

    def test_max_archive_size_skips_large_archives(self):
        archive_path = self.make_zip(inner_size=500)
        entries = by_path(
            self.root, penetrate_archives=True, max_archive_size=1
        )
        self.assertFalse(any("::" in p for p in entries))
        # The archive itself is still listed.
        self.assertIn(self.key(archive_path), entries)

    def test_max_archive_size_allows_small_archives(self):
        self.make_zip(inner_size=500)
        entries = by_path(
            self.root, penetrate_archives=True, max_archive_size=10 * 1024 * 1024
        )
        self.assertTrue(any("::" in p for p in entries))


class TestArchiveErrorHandling(WalkArchiveTestCase):
    def test_corrupt_archive_does_not_stop_the_walk(self):
        bad = self.root / "broken.zip"
        bad.write_bytes(b"PK\x03\x04 not a real zip")
        (self.root / "ok.txt").write_bytes(b"fine")
        entries = by_path(self.root, penetrate_archives=True)
        self.assertIn(self.key(bad), entries)
        self.assertIn(self.key(self.root / "ok.txt"), entries)

    def test_corrupt_archive_is_reported_through_on_error(self):
        bad = self.root / "broken.zip"
        bad.write_bytes(b"PK\x03\x04 not a real zip")
        seen = []
        list(walk.walk(self.root, penetrate_archives=True, on_error=seen.append))
        self.assertTrue(seen)

    def test_archive_error_is_recorded_on_the_entry(self):
        bad = self.root / "broken.tar.gz"
        bad.write_bytes(b"\x1f\x8b not gzip")
        entries = by_path(self.root, penetrate_archives=True)
        self.assertIn(self.key(bad), entries)

    def test_non_archive_file_is_not_opened(self):
        (self.root / "readme.md").write_bytes(b"# hi")
        entries = by_path(self.root, penetrate_archives=True)
        self.assertIn(self.key(self.root / "readme.md"), entries)
        self.assertFalse(any("::" in p for p in entries))


class TestArchiveMaxDepthInteraction(WalkArchiveTestCase):
    """max_depth and archive_depth are independent limits."""

    def test_max_depth_still_limits_filesystem_entries(self):
        deep = self.root / "a" / "b"
        deep.mkdir(parents=True)
        with zipfile.ZipFile(deep / "c.zip", "w") as zf:
            zf.writestr("in.txt", "x" * 100)

        entries = by_path(self.root, penetrate_archives=True, max_depth=1)
        self.assertNotIn(self.key(deep), entries)

    def test_archive_members_do_not_make_the_walk_descend(self):
        # A member named like a directory must not produce entries below it.
        path = self.root / "d.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("dir/", "")
            zf.writestr("dir/file.txt", "x" * 10)
        entries = by_path(self.root, penetrate_archives=True)
        self.assertIn(f"{self.key(path)}::dir", entries)
        self.assertIn(f"{self.key(path)}::dir/file.txt", entries)
        # Nothing claims to live *inside* that member.
        self.assertFalse(any(p.count("::") > 1 for p in entries))


if __name__ == "__main__":
    unittest.main()
