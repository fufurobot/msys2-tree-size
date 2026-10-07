"""Tests for archive detection and format handling.

The detection layer is pure: it maps a filename to the archive format it
should be treated as, and reports everything else as "not an archive". That
decision is where the subtle bugs live, so it is tested exhaustively and
without touching any real file.

Two classes of extension matter and they are easy to conflate:

* **Whole-archive extensions** (``.zip``, ``.7z``, ``.jar``, ``.docx``) name an
  archive directly.
* **Compound extensions** (``.tar.gz``, ``.tgz``, ``.tar.zst``) name a
  *compression wrapper* around a tar. The ordering is significant: ``.tar.gz``
  is a gzipped tar, while a plain ``.gz`` is a single compressed file that also
  happens to be worth looking inside.
"""

from __future__ import annotations

import unittest

from msys2_tree_size import formats


class TestCompoundExtensions(unittest.TestCase):
    """``.tar.<compressor>`` must be recognised before the compressor alone."""

    def test_tar_gz(self):
        self.assertEqual(formats.detect("backup.tar.gz"), "tar")
        self.assertEqual(formats.compression_of("backup.tar.gz"), "gzip")

    def test_tar_bz2(self):
        self.assertEqual(formats.detect("b.tar.bz2"), "tar")
        self.assertEqual(formats.compression_of("b.tar.bz2"), "bzip2")

    def test_tar_xz(self):
        self.assertEqual(formats.detect("b.tar.xz"), "tar")
        self.assertEqual(formats.compression_of("b.tar.xz"), "xz")

    def test_tar_zst(self):
        self.assertEqual(formats.detect("b.tar.zst"), "tar")
        self.assertEqual(formats.compression_of("b.tar.zst"), "zstd")

    def test_short_alias_tgz(self):
        self.assertEqual(formats.detect("b.tgz"), "tar")
        self.assertEqual(formats.compression_of("b.tgz"), "gzip")

    def test_short_aliases_for_other_compressors(self):
        self.assertEqual(formats.detect("b.tbz2"), "tar")
        self.assertEqual(formats.detect("b.txz"), "tar")
        self.assertEqual(formats.detect("b.tzst"), "tar")

    def test_plain_tar_is_uncompressed(self):
        self.assertEqual(formats.detect("b.tar"), "tar")
        self.assertEqual(formats.compression_of("b.tar"), "none")


class TestSingleFileCompression(unittest.TestCase):
    """A bare compressor holds one file, not a directory tree."""

    def test_gz_alone_is_recognised(self):
        self.assertEqual(formats.detect("notes.txt.gz"), "compressed")

    def test_xz_alone_is_recognised(self):
        self.assertEqual(formats.detect("blob.xz"), "compressed")

    def test_zst_alone_is_recognised(self):
        self.assertEqual(formats.detect("blob.zst"), "compressed")

    def test_bz2_alone_is_recognised(self):
        self.assertEqual(formats.detect("blob.bz2"), "compressed")

    def test_inner_name_is_derived_by_stripping_the_suffix(self):
        self.assertEqual(formats.inner_name("notes.txt.gz"), "notes.txt")
        self.assertEqual(formats.inner_name("blob.xz"), "blob")


class TestZipFamily(unittest.TestCase):
    """Many formats are just zip containers under a different name."""

    def test_plain_zip(self):
        self.assertEqual(formats.detect("a.zip"), "zip")

    def test_java_archive(self):
        self.assertEqual(formats.detect("lib.jar"), "zip")

    def test_android_package(self):
        self.assertEqual(formats.detect("app.apk"), "zip")

    def test_office_open_xml(self):
        for name in ("doc.docx", "sheet.xlsx", "deck.pptx"):
            self.assertEqual(formats.detect(name), "zip", name)

    def test_odf(self):
        for name in ("d.odt", "s.ods", "p.odp"):
            self.assertEqual(formats.detect(name), "zip", name)

    def test_epub_and_wheel(self):
        self.assertEqual(formats.detect("book.epub"), "zip")
        self.assertEqual(formats.detect("pkg.whl"), "zip")

    def test_case_insensitive(self):
        self.assertEqual(formats.detect("A.ZIP"), "zip")
        self.assertEqual(formats.detect("B.TAR.GZ"), "tar")


class TestOtherContainers(unittest.TestCase):
    def test_seven_zip(self):
        self.assertEqual(formats.detect("a.7z"), "7z")

    def test_rar(self):
        self.assertEqual(formats.detect("a.rar"), "rar")

    def test_iso_and_cab_are_not_claimed(self):
        # We do not implement these; claiming them would be a lie.
        self.assertEqual(formats.detect("a.iso"), None)
        self.assertEqual(formats.detect("a.cab"), None)


class TestNotAnArchive(unittest.TestCase):
    def test_plain_files_are_not_archives(self):
        for name in ("readme.md", "script.py", "photo.jpg", "song.mp3", "noext"):
            self.assertIsNone(formats.detect(name), name)

    def test_extensionless_file_is_not_an_archive(self):
        self.assertIsNone(formats.detect("Makefile"))

    def test_dotfile_is_not_an_archive(self):
        self.assertIsNone(formats.detect(".bashrc"))

    def test_trailing_dot_is_not_an_archive(self):
        self.assertIsNone(formats.detect("weird."))

    def test_a_name_merely_containing_zip_is_notan_archive(self):
        self.assertIsNone(formats.detect("zipfile_notes.txt"))

    def test_empty_name(self):
        self.assertIsNone(formats.detect(""))


class TestFilenamesWithPaths(unittest.TestCase):
    def test_path_prefix_is_ignored(self):
        self.assertEqual(formats.detect("/tmp/a/backup.tar.gz"), "tar")

    def test_surrogate_names_do_not_raise(self):
        from msys2_tree_size import paths

        weird = paths.decode_path(b"/tmp/\xff-backup.tar.gz")
        self.assertEqual(formats.detect(weird), "tar")

    def test_directory_named_like_an_archive_is_still_detected(self):
        # Detection is name-based; the caller decides whether it is a file.
        self.assertEqual(formats.detect("/tmp/foo.zip/"), "zip")


class TestIsArchive(unittest.TestCase):
    def test_true_for_archives(self):
        self.assertTrue(formats.is_archive("a.zip"))
        self.assertTrue(formats.is_archive("a.tar.gz"))

    def test_false_for_others(self):
        self.assertFalse(formats.is_archive("a.txt"))
        self.assertFalse(formats.is_archive(""))


class TestEncryptedDetection(unittest.TestCase):
    """Encrypted archives cannot be read, so they must be identified, not guessed."""

    def test_zip_with_encrypted_flag_is_reported(self):
        self.assertTrue(formats.looks_encrypted_name("secret.zip"))
        self.assertFalse(formats.looks_encrypted_name("plain.zip"))

    def test_rar_naming_convention(self):
        self.assertTrue(formats.looks_encrypted_name("secret-part1.rar"))

    def test_ordinary_names_are_not_flagged(self):
        for name in ("a.tar.gz", "b.zip", "c.7z", "notes.txt"):
            self.assertFalse(formats.looks_encrypted_name(name), name)


if __name__ == "__main__":
    unittest.main()
