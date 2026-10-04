"""Tests for the device inventory.

Parsing is deliberately split from process execution so it can be tested
against captured fixture text, with no shell and no real disks involved.

The fixture below mirrors the real shape of ``ls -l /dev/disk/by-id`` and
``/proc/partitions`` under MSYS2, including the two awkward bits: the symlink
line has a variable number of columns, and ``/proc/partitions`` has a header.
"""

from __future__ import annotations

import unittest

from msys2_tree_size import devices  # noqa: E402

LS_BY_ID = """\
total 0
lrwxrwxrwx 1 root root  9 Feb  1 10:00 nvme-Samsung_SSD_970_EVO_1TB_S1234 -> ../../nvme0n1
lrwxrwxrwx 1 root root 10 Feb  1 10:00 nvme-Samsung_SSD_970_EVO_1TB_S1234-part1 -> ../../nvme0n1p1
lrwxrwxrwx 1 root root 10 Feb  1 10:00 nvme-Samsung_SSD_970_EVO_1TB_S1234-part2 -> ../../nvme0n1p2
lrwxrwxrwx 1 root root  9 Feb  1 10:00 ata-WDC_WD40EZRZ-00GXCB0_WD-WCC7K1234567 -> ../../sda
lrwxrwxrwx 1 root root 10 Feb  1 10:00 ata-WDC_WD40EZRZ-00GXCB0_WD-WCC7K1234567-part1 -> ../../sda1
lrwxrwxrwx 1 root root  9 Feb  1 10:00 usb-Generic_STORAGE_DEVICE_000000000001-0:0 -> ../../sdb
"""

PROC_PARTITIONS = """\
major minor  #blocks  name

 259        0  976762584 nvme0n1
 259        1     512000 nvme0n1p1
 259        2  976248832 nvme0n1p2
   8        0 3907018584 sda
   8        1 3907017216 sda1
   8       16   30031872 sdb
"""


class TestParseByID(unittest.TestCase):
    def test_parses_every_symlink(self):
        rows = devices.parse_by_id(LS_BY_ID)
        self.assertEqual(len(rows), 6)

    def test_extracts_id_and_device_name(self):
        rows = {r["id"]: r for r in devices.parse_by_id(LS_BY_ID)}
        self.assertEqual(rows["nvme-Samsung_SSD_970_EVO_1TB_S1234-part1"]["name"], "nvme0n1p1")

    def test_actual_column_preserves_relative_target(self):
        rows = {r["id"]: r for r in devices.parse_by_id(LS_BY_ID)}
        self.assertEqual(rows["ata-WDC_WD40EZRZ-00GXCB0_WD-WCC7K1234567"]["actual"], "../../sda")

    def test_total_line_is_skipped(self):
        rows = devices.parse_by_id(LS_BY_ID)
        self.assertNotIn("total", [r["id"] for r in rows])

    def test_empty_input(self):
        self.assertEqual(devices.parse_by_id(""), [])

    def test_malformed_lines_are_skipped_not_fatal(self):
        text = LS_BY_ID + "garbage\n\n"
        self.assertEqual(len(devices.parse_by_id(text)), 6)


class TestParsePartitions(unittest.TestCase):
    def test_parses_every_partition(self):
        rows = devices.parse_partitions(PROC_PARTITIONS)
        self.assertEqual(len(rows), 6)

    def test_blocks_are_ints_in_kib(self):
        rows = {r["name"]: r for r in devices.parse_partitions(PROC_PARTITIONS)}
        self.assertEqual(rows["nvme0n1p2"]["#blocks"], 976248832)

    def test_major_minor_are_ints(self):
        rows = {r["name"]: r for r in devices.parse_partitions(PROC_PARTITIONS)}
        self.assertEqual(rows["sda"]["major"], 8)
        self.assertEqual(rows["sda"]["minor"], 0)
        self.assertEqual(rows["sda1"]["minor"], 1)

    def test_header_line_is_not_a_row(self):
        rows = devices.parse_partitions(PROC_PARTITIONS)
        self.assertNotIn("name", [r["name"] for r in rows])

    def test_empty_input(self):
        self.assertEqual(devices.parse_partitions(""), [])

    def test_missing_columns_are_skipped(self):
        text = "major minor  #blocks  name\n\n  8  0\n"
        self.assertEqual(devices.parse_partitions(text), [])


class TestBuildTable(unittest.TestCase):
    def test_inner_join_on_device_name(self):
        table = devices.build_table(LS_BY_ID, PROC_PARTITIONS)
        self.assertEqual(len(table), 6)
        self.assertTrue(all("blocks" in row for row in table))

    def test_sorted_smallest_device_first(self):
        table = devices.build_table(LS_BY_ID, PROC_PARTITIONS)
        blocks = [row["blocks"] for row in table]
        self.assertEqual(blocks, sorted(blocks))

    def test_by_id_without_partition_entry_is_dropped(self):
        ls = LS_BY_ID + ("lrwxrwxrwx 1 root root 9 Feb  1 10:00 usb-Ghost -> ../../sdz\n")
        table = devices.build_table(ls, PROC_PARTITIONS)
        self.assertNotIn("usb-Ghost", [row["id"] for row in table])

    def test_partition_without_symlink_is_dropped(self):
        proc = PROC_PARTITIONS + "\n   8       32   1000000 sdc\n"
        table = devices.build_table(LS_BY_ID, proc)
        self.assertNotIn("sdc", [row["name"] for row in table])

    def test_row_carries_id_name_and_blocks(self):
        table = devices.build_table(LS_BY_ID, PROC_PARTITIONS)
        smallest = table[0]
        self.assertEqual(set(smallest) >= {"id", "name", "blocks", "major", "minor"}, True)

    def test_duplicate_ids_are_removed(self):
        ls = LS_BY_ID + ("lrwxrwxrwx 1 root root  9 Feb  1 10:00 dup-alias -> ../../nvme0n1\n")
        table = devices.build_table(ls, PROC_PARTITIONS)
        names = [row["name"] for row in table]
        self.assertEqual(len(names), len(set(names)))


class TestReadByIDLive(unittest.TestCase):
    """The live readers must degrade gracefully where the paths do not exist."""

    def test_read_by_id_returns_empty_when_absent(self):
        rows = devices.read_by_id("/nonexistent/by-id")
        self.assertEqual(rows, [])

    def test_read_partitions_returns_empty_when_absent(self):
        rows = devices.read_partitions("/nonexistent/partitions")
        self.assertEqual(rows, [])


class TestInventoryText(unittest.TestCase):
    def test_inventory_reports_absent_paths(self):
        inv = devices.inventory(by_id_dir="/nonexistent/by-id")
        self.assertEqual(inv.rows, [])
        self.assertTrue(inv.notes)

    def test_table_to_rows_is_json_ready(self):
        table = devices.build_table(LS_BY_ID, PROC_PARTITIONS)
        rows = [dict(r) for r in table]
        self.assertTrue(all(isinstance(r["blocks"], int) for r in rows))


if __name__ == "__main__":
    unittest.main()
