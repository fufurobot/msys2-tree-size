"""Integration tests for behaviour that only exists under MSYS2.

These are the tests that justify the project's existence, so they are written
against the *real* MSYS2 semantics rather than a simulation:

* ``sys.platform == "msys"`` (MSYS2's own Python, not native Windows CPython)
* ``/c/...`` drive mounts that ``C:\\...`` and ``/c/...`` both resolve to
* byte-exact filenames, which a POSIX filesystem allows and NTFS does not

Every test skips cleanly when the property it needs is unavailable, so the
suite stays green on Linux and on native Windows while still providing real
coverage on an MSYS2 runner.
"""

from __future__ import annotations

import os
import sys
import unittest

from msys2_tree_size import devices, paths, walk
from support import TempDirTestCase


def _is_msys2() -> bool:
    """True when the interpreter itself resolves MSYS2 POSIX paths."""
    return paths.is_msys2()


def _has_msys2_install() -> bool:
    """True when an MSYS2 installation is present on this machine.

    Deliberately *not* the same as :func:`_is_msys2`.  Measured on a real MSYS2
    CLANG64 install, ``sys.platform`` is ``"win32"`` because MSYS2 ships a
    native Windows Python; the POSIX paths are virtual and are resolved by the
    MSYS2 runtime only for MSYS2 binaries.
    """
    return paths.msys2_root() is not None


def _drive_mount_ok() -> bool:
    """True when ``/c/...`` is a working mount for the current drive."""
    drive = os.path.splitdrive(os.getcwd())[0]
    if not drive:
        return False
    return os.path.isdir(f"/{drive[0].lower()}/")


class TestMsys2Platform(TempDirTestCase):
    def test_platform_is_recognised_as_a_posix_runtime(self):
        if not _is_msys2():
            self.skipTest("not running under an MSYS2/Cygwin runtime Python")
        # MSYS2's own Python reports "msys"; the GitHub Actions MSYS2 images
        # report "cygwin".  Both are POSIX runtime interpreters and both must be
        # accepted, so the assertion is membership rather than equality.
        self.assertIn(sys.platform, ("msys", "cygwin"))

    def test_posix_paths_are_reported(self):
        if not _is_msys2():
            self.skipTest("not running under an MSYS2/Cygwin runtime Python")
        root = self.make_temp_dir()
        (root / "f.txt").write_bytes(b"hello")
        entries = list(walk.walk(str(root)))
        self.assertTrue(entries)
        # A POSIX runtime must report POSIX paths, never a "C:\..." rendering.
        for entry in entries:
            self.assertTrue(entry.path.startswith("/"), entry.path)
            self.assertNotIn("\\", entry.path, entry.path)

    def test_drive_mount_is_usable(self):
        if not _is_msys2():
            self.skipTest("not running under an MSYS2/Cygwin runtime Python")
        if not _drive_mount_ok():
            self.skipTest("/c mount unavailable in this runtime")
        self.assertTrue(os.path.isdir("/c/"))


class TestDriveLetterEquivalence(TempDirTestCase):
    """``C:\\x`` and ``/c/x`` must name the same directory under MSYS2.

    The two spellings are built explicitly rather than by string-replacing the
    platform's own rendering, because that rendering differs between MSYS2's
    ``/c/...`` and Cygwin's ``/cygdrive/c/...``.  Deriving them from
    ``paths.to_posix`` / ``paths.from_posix`` keeps the test about *equivalence*
    instead of about which runtime is in use.
    """

    def windows_spelling(self, path) -> str:
        return paths.from_posix(paths.to_posix(str(path)))

    def test_both_spellings_walk_the_same_tree(self):
        if not _is_msys2():
            self.skipTest("not running under an MSYS2/Cygwin runtime Python")
        root = self.make_temp_dir()
        (root / "f.txt").write_bytes(b"x" * 12)

        posix = paths.to_posix(str(root))
        windows = self.windows_spelling(root)
        if windows == posix:
            self.skipTest("this runtime reports paths only one way")

        from_posix = {e.path for e in walk.walk(posix)}
        from_windows = {e.path for e in walk.walk(windows)}
        self.assertTrue(from_posix, "POSIX spelling produced no entries")
        self.assertEqual(from_posix, from_windows)

    def test_totals_agree_across_spellings(self):
        if not _is_msys2():
            self.skipTest("not running under an MSYS2/Cygwin runtime Python")
        root = self.make_temp_dir()
        (root / "a").write_bytes(b"x" * 100)
        sub = root / "sub"
        sub.mkdir()
        (sub / "b").write_bytes(b"y" * 200)

        def total(path):
            entries = list(walk.walk(path))
            self.assertTrue(entries, f"no entries for {path!r}")
            return max(e.size for e in entries)

        self.assertEqual(total(paths.to_posix(str(root))), 300)
        windows = self.windows_spelling(root)
        if windows != paths.to_posix(str(root)):
            self.assertEqual(total(windows), 300)


class TestByteExactFilenames(TempDirTestCase):
    """POSIX allows filenames that are not valid UTF-8; reports must survive."""

    def test_non_utf8_filename_is_reported_byte_exactly(self):
        if not _is_msys2():
            self.skipTest("POSIX byte filenames need an MSYS2 filesystem")

        root = self.make_temp_dir()
        raw_name = b"bad\xff\xfename"
        raw_path = paths.encode_path(str(root)) + b"/" + raw_name
        try:
            with open(raw_path, "wb") as fh:
                fh.write(b"data")
        except OSError:
            self.skipTest("filesystem rejects non-UTF-8 names")

        entries = {paths.encode_path(e.path) for e in walk.walk(str(root))}
        self.assertIn(raw_path, entries)

    def test_json_report_keeps_the_byte(self):
        if not _is_msys2():
            self.skipTest("POSIX byte filenames need an MSYS2 filesystem")

        import json

        from msys2_tree_size import report

        root = self.make_temp_dir()
        raw_path = paths.encode_path(str(root)) + b"/bad\xff"
        try:
            with open(raw_path, "wb") as fh:
                fh.write(b"data")
        except OSError:
            self.skipTest("filesystem rejects non-UTF-8 names")

        entries = list(walk.walk(str(root)))
        payload = json.loads(report.render_json(entries))
        recovered = {paths.encode_path(e["path"]) for e in payload["entries"]}
        self.assertIn(raw_path, recovered)


class TestRealDevices(TempDirTestCase):
    """The live readers must work on a real MSYS2 system, or say why not."""

    def test_by_id_is_readable(self):
        if not _has_msys2_install():
            self.skipTest("no MSYS2 installation found")
        rows = devices.read_by_id()
        if not rows:
            self.skipTest("MSYS2 virtual device tree not readable in this environment")
        self.assertTrue(all(r["name"] for r in rows))
        self.assertTrue(all(r["id"] for r in rows))

    def test_partitions_is_readable(self):
        if not _has_msys2_install():
            self.skipTest("no MSYS2 installation found")
        rows = devices.read_partitions()
        if not rows:
            self.skipTest("/proc/partitions not readable in this environment")
        self.assertTrue(all(isinstance(r["#blocks"], int) for r in rows))

    def test_every_row_exposes_win_mounts(self):
        if not _has_msys2_install():
            self.skipTest("no MSYS2 installation found")
        rows = devices.read_partitions()
        if not rows:
            self.skipTest("/proc/partitions not readable in this environment")
        self.assertTrue(all("win-mounts" in row for row in rows))

    def test_inventory_joins_on_a_real_system(self):
        if not _has_msys2_install():
            self.skipTest("no MSYS2 installation found")
        result = devices.inventory()
        # Must never be both empty and silent.
        self.assertTrue(result.rows or result.notes)

    def test_inventory_rows_have_positive_sizes(self):
        if not _has_msys2_install():
            self.skipTest("no MSYS2 installation found")
        result = devices.inventory()
        if not result.rows:
            self.skipTest("device tree not readable in this environment")
        self.assertTrue(all(row["blocks"] > 0 for row in result.rows))


if __name__ == "__main__":
    unittest.main()
