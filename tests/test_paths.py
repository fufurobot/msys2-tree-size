"""Tests for byte-exact path handling.

POSIX filenames are arbitrary byte sequences.  Python surfaces non-UTF-8 bytes
as lone surrogates (U+DC80..U+DCFF).  Every layer that re-encodes must not
raise and must not lose information, otherwise we silently corrupt paths in
reports.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from msys2_tree_size import paths  # noqa: E402
from support import TempDirTestCase


class TestDecodeEncode(unittest.TestCase):
    def test_ascii_roundtrip(self):
        self.assertEqual(paths.decode_path(b"/tmp/hello.txt"), "/tmp/hello.txt")
        self.assertEqual(paths.encode_path("/tmp/hello.txt"), b"/tmp/hello.txt")

    def test_utf8_roundtrip(self):
        raw = "/tmp/ünïcødé/文件.txt".encode()
        s = paths.decode_path(raw)
        self.assertEqual(s, "/tmp/ünïcødé/文件.txt")
        self.assertEqual(paths.encode_path(s), raw)

    def test_invalid_utf8_survives_roundtrip(self):
        raw = b"/tmp/\xff\xfe/bad\x80name"
        s = paths.decode_path(raw)
        self.assertEqual(paths.encode_path(s), raw)

    def test_invalid_utf8_produces_lone_surrogates(self):
        s = paths.decode_path(b"\xff")
        self.assertEqual(s, "\udcff")
        self.assertEqual(len(s), 1)

    def test_encode_accepts_bytes_passthrough(self):
        self.assertEqual(paths.encode_path(b"/tmp/x"), b"/tmp/x")

    def test_encode_accepts_pathlike(self):
        self.assertEqual(paths.encode_path(Path("/tmp/x")), b"/tmp/x")

    def test_decode_accepts_str_passthrough(self):
        self.assertEqual(paths.decode_path("/tmp/x"), "/tmp/x")


class TestSafeText(TempDirTestCase):
    def test_safe_text_escapes_surrogates_losslessly(self):
        s = paths.decode_path(b"/tmp/\xff")
        escaped = paths.safe_text(s)
        # Must be encodable as strict UTF-8, unlike the original.
        escaped.encode("utf-8")
        self.assertNotIn("\udcff", escaped)
        # The escape names the exact byte that was lost, so it stays debuggable.
        self.assertEqual(escaped, "/tmp/\\udcff")

    def test_safe_text_leaves_plain_text_alone(self):
        self.assertEqual(paths.safe_text("/tmp/plain.txt"), "/tmp/plain.txt")


class TestWriters(TempDirTestCase):
    def setUp(self):
        self.tmp = self.make_temp_dir()

    def test_write_text_is_byte_exact(self):
        target = self.tmp / "out.txt"
        raw = b"/tmp/\xff/bad\x80name"
        paths.write_text(target, paths.decode_path(raw))
        self.assertEqual(target.read_bytes(), raw)

    def test_write_json_is_ascii_safe_and_parseable(self):
        target = self.tmp / "out.json"
        weird = paths.decode_path(b"/tmp/\xff")
        paths.write_json(target, {"path": weird, "n": 1})

        # A strict-UTF-8 reader must be able to read the file at all.
        text = target.read_text(encoding="utf-8")
        loaded = json.loads(text)
        self.assertEqual(loaded["n"], 1)
        # The surrogate must be represented as an escape, not raw bytes.
        self.assertIn("\\udcff", text)
        self.assertEqual(paths.encode_path(loaded["path"]), b"/tmp/\xff")

    def test_write_json_handles_nested_structures(self):
        target = self.tmp / "nested.json"
        weird = paths.decode_path(b"\xfe")
        paths.write_json(target, {"rows": [{"path": weird}], "meta": {"count": 1}})
        loaded = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(paths.encode_path(loaded["rows"][0]["path"]), b"\xfe")

    def test_write_csv_row_is_byte_exact(self):
        target = self.tmp / "out.csv"
        raw = b"/tmp/\xff"
        paths.write_csv(target, ["path", "size"], [[paths.decode_path(raw), 12]])
        data = target.read_bytes()
        self.assertIn(raw, data)
        self.assertTrue(data.endswith(b"\n"))

    def test_write_csv_quotes_fields_containing_separators(self):
        target = self.tmp / "out.csv"
        paths.write_csv(target, ["path"], [['/tmp/a,b"c']])
        text = target.read_text(encoding="utf-8")
        self.assertIn('"/tmp/a,b""c"', text)

    def test_write_csv_empty_rows_still_writes_header(self):
        target = self.tmp / "empty.csv"
        paths.write_csv(target, ["path", "size"], [])
        self.assertEqual(target.read_text(encoding="utf-8"), "path,size\n")


class TestToPosix(unittest.TestCase):
    def test_drive_letter_is_converted(self):
        self.assertEqual(paths.to_posix("C:\\Users\\fufu"), "/c/Users/fufu")

    def test_lowercase_drive_letter(self):
        self.assertEqual(paths.to_posix("d:\\data"), "/d/data")

    def test_unc_path_is_left_alone(self):
        # No sensible /x/ mapping exists; do not invent one.
        self.assertEqual(paths.to_posix("\\\\server\\share"), "\\\\server\\share")

    def test_posix_path_is_unchanged(self):
        self.assertEqual(paths.to_posix("/c/Users/fufu"), "/c/Users/fufu")

    def test_relative_path_is_unchanged(self):
        self.assertEqual(paths.to_posix("sub/dir"), "sub/dir")

    def test_forward_slashes_with_drive_letter(self):
        self.assertEqual(paths.to_posix("C:/Users/fufu"), "/c/Users/fufu")

    def test_bare_drive_root(self):
        self.assertEqual(paths.to_posix("C:\\"), "/c/")


class TestNormalizeSeparators(unittest.TestCase):
    def test_absolute_win32_path_becomes_posix(self):
        self.assertEqual(paths.normalize_separators("\\tmp\\x"), "/tmp/x")

    def test_drive_path_is_delegated_to_to_posix(self):
        self.assertEqual(paths.normalize_separators("C:\\Users\\a"), "/c/Users/a")

    def test_relative_backslash_is_preserved(self):
        # On POSIX a backslash is a legal filename character; rewriting it
        # would silently retarget the path.
        self.assertEqual(paths.normalize_separators("a\\b"), "a\\b")

    def test_plain_posix_untouched(self):
        self.assertEqual(paths.normalize_separators("/tmp/x"), "/tmp/x")

    def test_unc_left_alone(self):
        self.assertEqual(paths.normalize_separators("\\\\srv\\share"), "\\\\srv\\share")


class TestJoinAndSplit(unittest.TestCase):
    def test_join_preserves_surrogates(self):
        s = paths.decode_path(b"/tmp/\xff")
        joined = paths.join(s, "child")
        self.assertEqual(paths.encode_path(joined), b"/tmp/\xff/child")

    def test_basename_of_surrogate_path(self):
        s = paths.decode_path(b"/tmp/\xff/file.txt")
        self.assertEqual(paths.basename(s), "file.txt")

    def test_basename_strips_trailing_slash(self):
        self.assertEqual(paths.basename("/tmp/dir/"), "dir")

    def test_parent_of_nested(self):
        self.assertEqual(paths.parent("/tmp/a/b"), "/tmp/a")

    def test_split_extension(self):
        self.assertEqual(paths.extension("archive.tar.gz"), ".gz")
        self.assertEqual(paths.extension("noext"), "<none>")
        self.assertEqual(paths.extension(".bashrc"), "<none>")
        self.assertEqual(paths.extension("A.TXT"), ".txt")


if __name__ == "__main__":
    unittest.main()
