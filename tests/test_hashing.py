"""Tests for content hashing and Merkle subtree hashing."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from msys2_tree_size import hashing  # noqa: E402


class TestHashFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_matches_hashlib(self):
        p = self.tmp / "a.bin"
        p.write_bytes(b"hello world")
        self.assertEqual(
            hashing.hash_file(p), hashlib.sha256(b"hello world").hexdigest()
        )

    def test_empty_file(self):
        p = self.tmp / "empty"
        p.write_bytes(b"")
        self.assertEqual(hashing.hash_file(p), hashlib.sha256(b"").hexdigest())

    def test_identical_content_identical_hash(self):
        a, b = self.tmp / "a", self.tmp / "b"
        a.write_bytes(b"x" * 5000)
        b.write_bytes(b"x" * 5000)
        self.assertEqual(hashing.hash_file(a), hashing.hash_file(b))

    def test_different_content_differs(self):
        a, b = self.tmp / "a", self.tmp / "b"
        a.write_bytes(b"x")
        b.write_bytes(b"y")
        self.assertNotEqual(hashing.hash_file(a), hashing.hash_file(b))

    def test_large_file_is_streamed_correctly(self):
        # Larger than the read chunk, to prove the loop does not truncate.
        p = self.tmp / "big.bin"
        payload = bytes(range(256)) * 4096  # 1 MiB
        p.write_bytes(payload)
        self.assertEqual(hashing.hash_file(p), hashlib.sha256(payload).hexdigest())

    def test_missing_file_returns_empty_string(self):
        self.assertEqual(hashing.hash_file(self.tmp / "nope"), "")

    def test_directory_returns_empty_string(self):
        d = self.tmp / "adir"
        d.mkdir()
        self.assertEqual(hashing.hash_file(d), "")


class TestMerkleHash(unittest.TestCase):
    def test_empty_directory_hash_is_stable(self):
        h1 = hashing.merkle_hash([])
        h2 = hashing.merkle_hash([])
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    def test_order_independent(self):
        a = ("a.txt", "1" * 64)
        b = ("b.txt", "2" * 64)
        self.assertEqual(hashing.merkle_hash([a, b]), hashing.merkle_hash([b, a]))

    def test_changing_a_child_hash_changes_result(self):
        a = ("a.txt", "1" * 64)
        self.assertNotEqual(
            hashing.merkle_hash([a]), hashing.merkle_hash([("a.txt", "9" * 64)])
        )

    def test_changing_a_child_name_changes_result(self):
        self.assertNotEqual(
            hashing.merkle_hash([("a.txt", "1" * 64)]),
            hashing.merkle_hash([("b.txt", "1" * 64)]),
        )

    def test_extra_child_changes_result(self):
        one = hashing.merkle_hash([("a.txt", "1" * 64)])
        two = hashing.merkle_hash([("a.txt", "1" * 64), ("b.txt", "2" * 64)])
        self.assertNotEqual(one, two)

    def test_interleaved_names_are_unambiguous(self):
        # Concatenation without separators would collide these two.
        x = hashing.merkle_hash([("ab", "1" * 64), ("c", "2" * 64)])
        y = hashing.merkle_hash([("a", "1" * 64), ("bc", "2" * 64)])
        self.assertNotEqual(x, y)

    def test_identical_trees_hash_identically(self):
        tree_a = [("f1", hashing.hash_bytes(b"one")), ("f2", hashing.hash_bytes(b"two"))]
        tree_b = [("f1", hashing.hash_bytes(b"one")), ("f2", hashing.hash_bytes(b"two"))]
        self.assertEqual(hashing.merkle_hash(tree_a), hashing.merkle_hash(tree_b))


class TestHashBytes(unittest.TestCase):
    def test_matches_hashlib(self):
        self.assertEqual(hashing.hash_bytes(b"abc"), hashlib.sha256(b"abc").hexdigest())

    def test_empty(self):
        self.assertEqual(hashing.hash_bytes(b""), hashlib.sha256(b"").hexdigest())


class TestNullHash(unittest.TestCase):
    def test_null_hash_is_not_a_real_digest(self):
        # Used to mark "not hashed"; must never collide with a real digest.
        self.assertEqual(hashing.NULL_HASH, "")
        self.assertNotEqual(len(hashing.NULL_HASH), 64)

    def test_is_null_helper(self):
        self.assertTrue(hashing.is_null(""))
        self.assertFalse(hashing.is_null("a" * 64))


if __name__ == "__main__":
    unittest.main()
