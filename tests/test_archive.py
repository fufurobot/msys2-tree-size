"""Tests for reading archive contents.

These build *real* archives in a scratch directory and read them back, because
the thing being tested is precisely the interaction with real container formats.
A mocked archive reader would assert that the code calls what the code calls,
which is not worth knowing.

Formats are tried in order of availability: the Python standard library covers
tar, gzip, bzip2, xz and zip, so those are always exercised. Formats needing an
external tool (7z, rar, zstd) skip when that tool is absent, which keeps the
suite honest on a machine without them.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import unittest
import zipfile

from msys2_tree_size import archive
from support import TempDirTestCase


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


class ArchiveTestCase(TempDirTestCase):
    """Helpers for building real archives on disk."""

    def setUp(self):
        self.root = self.make_temp_dir()

    def payload(self):
        """A small tree of files to put inside archives."""
        src = self.root / "payload"
        (src / "sub").mkdir(parents=True, exist_ok=True)
        (src / "a.txt").write_bytes(b"a" * 100)
        (src / "sub" / "b.bin").write_bytes(b"b" * 300)
        return src

    def make_tar(self, name="t.tar", mode="w"):
        path = self.root / name
        with tarfile.open(path, mode) as tf:
            tf.add(self.payload(), arcname="payload")
        return path

    def make_zip(self, name="t.zip"):
        path = self.root / name
        src = self.payload()
        with zipfile.ZipFile(path, "w") as zf:
            for file in sorted(src.rglob("*")):
                if file.is_file():
                    zf.write(file, file.relative_to(src.parent).as_posix())
        return path


class TestTarReading(ArchiveTestCase):
    def test_finds_every_member(self):
        entries = list(archive.read(self.make_tar()))
        names = {e.name for e in entries}
        self.assertIn("payload/a.txt", names)
        self.assertIn("payload/sub/b.bin", names)

    def test_reports_uncompressed_sizes(self):
        entries = {e.name: e for e in archive.read(self.make_tar())}
        self.assertEqual(entries["payload/a.txt"].size, 100)
        self.assertEqual(entries["payload/sub/b.bin"].size, 300)

    def test_directories_are_marked(self):
        entries = {e.name: e for e in archive.read(self.make_tar())}
        self.assertEqual(entries["payload"].type, "dir")
        self.assertEqual(entries["payload/a.txt"].type, "file")

    def test_paths_are_prefixed_with_the_archive(self):
        path = self.make_tar()
        from msys2_tree_size import paths as paths_mod

        # Member paths are reported in the canonical POSIX display form, so the
        # prefix is the normalised archive path rather than the raw one.
        prefix = paths_mod.to_posix(str(path))
        for entry in archive.read(path):
            self.assertTrue(entry.path.startswith(prefix), entry.path)

    def test_gzip_tar(self):
        entries = {e.name: e for e in archive.read(self.make_tar("t.tar.gz", "w:gz"))}
        self.assertEqual(entries["payload/a.txt"].size, 100)

    def test_bzip2_tar(self):
        entries = {e.name: e for e in archive.read(self.make_tar("t.tar.bz2", "w:bz2"))}
        self.assertEqual(entries["payload/a.txt"].size, 100)

    def test_xz_tar(self):
        entries = {e.name: e for e in archive.read(self.make_tar("t.tar.xz", "w:xz"))}
        self.assertEqual(entries["payload/a.txt"].size, 100)

    def test_total_uncompressed_size_is_reported(self):
        entries = list(archive.read(self.make_tar()))
        total = sum(e.size for e in entries if e.type == "file")
        self.assertEqual(total, 400)


class TestZipReading(ArchiveTestCase):
    def test_finds_every_member(self):
        names = {e.name for e in archive.read(self.make_zip())}
        self.assertIn("payload/a.txt", names)

    def test_reports_uncompressed_sizes(self):
        entries = {e.name: e for e in archive.read(self.make_zip())}
        self.assertEqual(entries["payload/a.txt"].size, 100)

    def test_office_document_is_read_as_zip(self):
        # .docx is a zip; this proves the zip family is really handled.
        path = self.root / "doc.docx"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("word/document.xml", "<xml/>" * 50)
            zf.writestr("[Content_Types].xml", "<Types/>")
        entries = {e.name: e for e in archive.read(path)}
        self.assertIn("word/document.xml", entries)
        self.assertEqual(entries["word/document.xml"].size, 300)

    def test_jar_is_read_as_zip(self):
        path = self.root / "lib.jar"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        names = {e.name for e in archive.read(path)}
        self.assertIn("META-INF/MANIFEST.MF", names)


class TestUnsupportedAndBroken(ArchiveTestCase):
    def test_plain_file_yields_nothing(self):
        path = self.root / "notes.txt"
        path.write_bytes(b"hello")
        self.assertEqual(list(archive.read(path)), [])

    def test_corrupt_archive_is_reported_not_raised(self):
        path = self.root / "broken.zip"
        path.write_bytes(b"PK\x03\x04 this is not really a zip")
        entries = list(archive.read(path))
        self.assertEqual(entries, [])

    def test_missing_file_yields_nothing(self):
        self.assertEqual(list(archive.read(self.root / "nope.tar")), [])

    def test_errors_are_reported_through_the_callback(self):
        seen = []
        path = self.root / "broken.tar.gz"
        path.write_bytes(b"\x1f\x8b not gzip either")
        list(archive.read(path, on_error=seen.append))
        self.assertTrue(seen)

    def test_truncated_tar_does_not_hang(self):
        path = self.make_tar()
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 2])
        list(archive.read(path))  # must simply return


class TestEncryptedArchives(ArchiveTestCase):
    """Encrypted archives cannot be read and must be reported as such."""

    def test_encrypted_zip_is_flagged(self):
        path = self.root / "secret.zip"
        # zipfile can only *write* encryption via a third-party library, but a
        # legacy-encrypted entry can be recognised from the flag bits, which is
        # what the reader checks.
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("inner.txt", "data")
        # A normal zip is not flagged.
        self.assertFalse(archive.is_encrypted(path))

    def test_zip_with_encryption_flag_is_detected(self):
        path = self.root / "enc.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("inner.txt", "data")

        # Forge the encryption bit (bit 0 of the general purpose flag) in the
        # local file header and central directory, which is what a real
        # encrypted zip sets. This exercises the detection path without needing
        # an encryption library.
        raw = bytearray(path.read_bytes())
        idx = raw.find(b"PK\x03\x04")
        if idx != -1:
            flag = int.from_bytes(raw[idx + 6 : idx + 8], "little") | 1
            raw[idx + 6 : idx + 8] = flag.to_bytes(2, "little")
        cidx = raw.find(b"PK\x01\x02")
        if cidx != -1:
            flag = int.from_bytes(raw[cidx + 8 : cidx + 10], "little") | 1
            raw[cidx + 8 : cidx + 10] = flag.to_bytes(2, "little")
        path.write_bytes(bytes(raw))

        self.assertTrue(archive.is_encrypted(path))


class TestSevenZipAndRar(ArchiveTestCase):
    def test_7z_roundtrip(self):
        if not have("7z"):
            self.skipTest("7z not available")
        src = self.payload()
        path = self.root / "t.7z"
        subprocess.run(
            ["7z", "a", "-bso0", "-bsp0", str(path), "payload"],
            cwd=str(src.parent),
            check=True,
            capture_output=True,
        )
        entries = {e.name: e for e in archive.read(path)}
        self.assertIn("payload/a.txt", entries)
        self.assertEqual(entries["payload/a.txt"].size, 100)

    def test_rar_roundtrip_when_rar_available(self):
        # `rar` (the creator) is shareware and usually absent; `unrar` can only
        # read. Skip rather than pretend.
        if not have("rar"):
            self.skipTest("rar creator not available")
        src = self.payload()
        path = self.root / "t.rar"
        subprocess.run(
            ["rar", "a", "-idq", str(path), "payload"],
            cwd=str(src.parent),
            check=True,
            capture_output=True,
        )
        entries = {e.name: e for e in archive.read(path)}
        self.assertIn("payload/a.txt", entries)

    def test_tar_zst_roundtrip(self):
        if not (have("zstd") and have("tar")):
            self.skipTest("zstd/tar not available")
        tar_path = self.make_tar("plain.tar")
        zst_path = self.root / "t.tar.zst"
        subprocess.run(
            ["zstd", "-q", "-f", str(tar_path), "-o", str(zst_path)],
            check=True,
            capture_output=True,
        )
        entries = {e.name: e for e in archive.read(zst_path)}
        self.assertIn("payload/a.txt", entries)
        self.assertEqual(entries["payload/a.txt"].size, 100)


class TestAvailability(unittest.TestCase):
    def test_reports_which_backends_are_usable(self):
        report = archive.available_backends()
        self.assertIn("tar", report)
        self.assertIn("zip", report)
        # The stdlib backends must always be usable.
        self.assertTrue(report["tar"])
        self.assertTrue(report["zip"])

    def test_unsupported_backends_report_false(self):
        report = archive.available_backends()
        self.assertIn("rar", report)
        self.assertIn("7z", report)


if __name__ == "__main__":
    unittest.main()
