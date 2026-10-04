"""Tests for human-readable size formatting and aggregation."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from msys2_tree_size import sizes  # noqa: E402


class TestHumanReadable(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(sizes.human_readable(0), "0B")

    def test_bytes_are_integral_and_unscaled(self):
        self.assertEqual(sizes.human_readable(1), "1B")
        self.assertEqual(sizes.human_readable(1023), "1023B")

    def test_one_kib_boundary(self):
        self.assertEqual(sizes.human_readable(1024), "1.0K")

    def test_binary_units(self):
        self.assertEqual(sizes.human_readable(1536), "1.5K")
        self.assertEqual(sizes.human_readable(1024**2), "1.0M")
        self.assertEqual(sizes.human_readable(1024**3), "1.0G")
        self.assertEqual(sizes.human_readable(1024**4), "1.0T")
        self.assertEqual(sizes.human_readable(1024**5), "1.0P")

    def test_exbibyte_is_terminal(self):
        # Must not run off the end of the unit table.
        self.assertEqual(sizes.human_readable(1024**6), "1.0E")
        self.assertEqual(sizes.human_readable(1024**7), "1024.0E")

    def test_negative_is_tolerated(self):
        self.assertEqual(sizes.human_readable(-5), "0B")

    def test_none_is_tolerated(self):
        self.assertEqual(sizes.human_readable(None), "0B")


class TestParseSize(unittest.TestCase):
    def test_plain_int(self):
        self.assertEqual(sizes.parse_size("1024"), 1024)

    def test_suffixes_are_binary(self):
        self.assertEqual(sizes.parse_size("1K"), 1024)
        self.assertEqual(sizes.parse_size("1M"), 1024**2)
        self.assertEqual(sizes.parse_size("1G"), 1024**3)
        self.assertEqual(sizes.parse_size("1T"), 1024**4)

    def test_suffix_is_case_insensitive(self):
        self.assertEqual(sizes.parse_size("1m"), 1024**2)
        self.assertEqual(sizes.parse_size("1mB"), 1024**2)

    def test_fractional_values(self):
        self.assertEqual(sizes.parse_size("1.5K"), 1536)

    def test_whitespace_is_stripped(self):
        self.assertEqual(sizes.parse_size("  2K  "), 2048)

    def test_invalid_raises_value_error(self):
        for bad in ("", "abc", "1X", "K"):
            with self.assertRaises(ValueError):
                sizes.parse_size(bad)


class TestPercentage(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(sizes.percentage(25, 100), 25.0)

    def test_zero_total_is_zero_not_error(self):
        self.assertEqual(sizes.percentage(5, 0), 0.0)

    def test_rounding(self):
        self.assertEqual(sizes.percentage(1, 3), 33.3333)


class TestEntropy(unittest.TestCase):
    def test_single_child_has_zero_entropy(self):
        self.assertEqual(sizes.entropy_bits([100]), 0.0)

    def test_even_split_of_two_is_one_bit(self):
        self.assertAlmostEqual(sizes.entropy_bits([50, 50]), 1.0)

    def test_even_split_of_four_is_two_bits(self):
        self.assertAlmostEqual(sizes.entropy_bits([25, 25, 25, 25]), 2.0)

    def test_empty_is_zero(self):
        self.assertEqual(sizes.entropy_bits([]), 0.0)

    def test_zero_total_is_zero(self):
        self.assertEqual(sizes.entropy_bits([0, 0]), 0.0)

    def test_zero_entries_are_ignored(self):
        self.assertAlmostEqual(sizes.entropy_bits([50, 50, 0]), 1.0)


class TestExtensionTotals(unittest.TestCase):
    def test_groups_and_lowercases(self):
        rows = [
            {"type": "file", "name": "a.txt", "size": 10},
            {"type": "file", "name": "b.TXT", "size": 5},
            {"type": "file", "name": "c.bin", "size": 1},
        ]
        self.assertEqual(
            sizes.extension_totals(rows), {".txt": 15, ".bin": 1}
        )

    def test_directories_are_ignored(self):
        rows = [{"type": "dir", "name": "d.txt", "size": 999}]
        self.assertEqual(sizes.extension_totals(rows), {})

    def test_extensionless_uses_placeholder(self):
        rows = [{"type": "file", "name": "Makefile", "size": 7}]
        self.assertEqual(sizes.extension_totals(rows), {"<none>": 7})

    def test_empty_input(self):
        self.assertEqual(sizes.extension_totals([]), {})

    def test_top_extension_helper(self):
        rows = [
            {"type": "file", "name": "a.txt", "size": 10},
            {"type": "file", "name": "b.bin", "size": 500},
        ]
        name, total = sizes.top_extension(rows)
        self.assertEqual((name, total), (".bin", 500))

    def test_top_extension_on_empty(self):
        self.assertEqual(sizes.top_extension([]), ("<none>", 0))


if __name__ == "__main__":
    unittest.main()
