"""Tests for the device inventory.

Parsing is deliberately split from process execution so it can be tested
against captured fixture text, with no shell and no real disks involved.

The fixture below mirrors the real shape of ``ls -l /dev/disk/by-id`` and
``/proc/partitions`` under MSYS2, including the two awkward bits: the symlink
line has a variable number of columns, and ``/proc/partitions`` has a header.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

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

# Real MSYS2 output carries a FIFTH column, `win-mounts`, holding the drive
# letter.  A strict four-column parser silently discards every row, so this
# captured fixture is the important one.
PROC_PARTITIONS_MSYS2 = """\
major minor  #blocks  name   win-mounts

    8     0 250059096 sda
    8     1    102400 sda1
    8     2    131072 sda2
    8     3 234095616 sda3   C:\\
    8     4  15728640 sda4
    8    16 124999680 sdb
    8    17 124997632 sdb1   F:\\
    8    32 482623488 sdc
    8    33 482589696 sdc1   D:\\
    8    34     32768 sdc2   E:\\
"""

# Real MSYS2 by-id output: the link names are long and the targets are bare
# `../../sda`, with no `/dev/` prefix to strip.
LS_BY_ID_MSYS2 = """\
total 0
lrwxrwxrwx 1 fufu fufu 0 Oct  4 11:12 nvme-Great_Wall_GW3300_256GB_0000. -> ../../sda
lrwxrwxrwx 1 fufu fufu 0 Oct  4 11:12 nvme-Great_Wall_GW3300_256GB_0000.-part1 -> ../../sda1
lrwxrwxrwx 1 fufu fufu 0 Oct  4 11:12 nvme-Great_Wall_GW3300_256GB_0000.-part3 -> ../../sda3
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


class TestMsys2Partitions(unittest.TestCase):
    """Real MSYS2 /proc/partitions has an extra `win-mounts` column."""

    def test_every_row_survives_the_extra_column(self):
        rows = devices.parse_partitions(PROC_PARTITIONS_MSYS2)
        self.assertEqual(len(rows), 10)

    def test_blocks_are_still_read_correctly(self):
        rows = {r["name"]: r for r in devices.parse_partitions(PROC_PARTITIONS_MSYS2)}
        self.assertEqual(rows["sda3"]["#blocks"], 234095616)

    def test_win_mounts_is_captured_when_present(self):
        rows = {r["name"]: r for r in devices.parse_partitions(PROC_PARTITIONS_MSYS2)}
        self.assertEqual(rows["sda3"]["win-mounts"], "C:\\")

    def test_win_mounts_absent_is_empty(self):
        rows = {r["name"]: r for r in devices.parse_partitions(PROC_PARTITIONS_MSYS2)}
        self.assertEqual(rows["sda"]["win-mounts"], "")

    def test_classic_four_column_output_still_works(self):
        rows = {r["name"]: r for r in devices.parse_partitions(PROC_PARTITIONS)}
        self.assertEqual(rows["sda"]["#blocks"], 3907018584)
        self.assertEqual(rows["sda"]["win-mounts"], "")

    def test_header_is_still_rejected(self):
        rows = devices.parse_partitions(PROC_PARTITIONS_MSYS2)
        self.assertNotIn("name", [r["name"] for r in rows])

    def test_extra_columns_are_ignored_not_fatal(self):
        text = "major minor  #blocks  name   win-mounts  extra\n  8 0 100 sda C:\\ junk\n"
        rows = devices.parse_partitions(text)
        self.assertEqual(rows[0]["name"], "sda")
        self.assertEqual(rows[0]["#blocks"], 100)


class TestMsys2ByID(unittest.TestCase):
    def test_long_real_world_names_parse(self):
        rows = devices.parse_by_id(LS_BY_ID_MSYS2)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["name"], "sda")
        self.assertEqual(rows[1]["name"], "sda1")

    def test_join_with_msys2_partitions_yields_a_table(self):
        table = devices.build_table(LS_BY_ID_MSYS2, PROC_PARTITIONS_MSYS2)
        self.assertEqual(len(table), 3)
        self.assertEqual([row["name"] for row in table], ["sda1", "sda3", "sda"])

    def test_win_mounts_survives_the_join(self):
        table = {
            row["name"]: row for row in devices.build_table(LS_BY_ID_MSYS2, PROC_PARTITIONS_MSYS2)
        }
        self.assertEqual(table["sda3"]["win-mounts"], "C:\\")


class TestMsys2Detection(unittest.TestCase):
    """MSYS2 discovery must not hardcode one installation path."""

    def test_msys2_root_is_none_or_an_existing_directory(self):
        from msys2_tree_size import paths

        root = paths.msys2_root()
        if root is not None:
            self.assertTrue(os.path.isdir(root), root)

    def test_looks_like_msys2_rejects_empty_and_missing(self):
        from msys2_tree_size import paths

        self.assertFalse(paths._looks_like_msys2(""))
        self.assertFalse(paths._looks_like_msys2(str(Path(__file__).parent)))

    def test_msys2_shell_is_none_or_a_cmd_file(self):
        shell = devices.msys2_shell()
        if shell is not None:
            self.assertTrue(shell.lower().endswith(".cmd"), shell)
            self.assertTrue(os.path.isfile(shell), shell)

    def test_is_msys2_matches_platform(self):
        from msys2_tree_size import paths

        self.assertEqual(paths.is_msys2(), sys.platform in ("msys", "cygwin"))

    def test_diagnose_returns_explanatory_lines(self):
        lines = devices.diagnose()
        self.assertTrue(lines)
        self.assertTrue(any("MSYS2 root" in line for line in lines))


class TestLiveHardwareShape(unittest.TestCase):
    """Parsing must cope with the exact shapes real MSYS2 hardware produces.

    Captured from a machine whose /proc/partitions carries the win-mounts
    column and whose by-id names are long NVMe identifiers.
    """

    def test_captured_msys2_output_joins_completely(self):
        table = devices.build_table(LS_BY_ID_MSYS2, PROC_PARTITIONS_MSYS2)
        by_name = {row["name"]: row for row in table}
        # sda3 is the only by-id entry with a windows mount in the fixture.
        self.assertEqual(by_name["sda3"]["win-mounts"], "C:\\")
        self.assertEqual(by_name["sda3"]["blocks"], 234095616)
        self.assertEqual(by_name["sda3"]["id"].startswith("nvme-"), True)

    def test_smallest_device_is_first(self):
        table = devices.build_table(LS_BY_ID_MSYS2, PROC_PARTITIONS_MSYS2)
        self.assertEqual([row["name"] for row in table], ["sda1", "sda3", "sda"])


class TestInventoryNotes(unittest.TestCase):
    def test_inventory_is_never_empty_and_silent(self):
        result = devices.inventory()
        self.assertTrue(result.rows or result.notes)

    def test_absent_by_id_reports_a_note(self):
        result = devices.inventory(by_id_dir="/nonexistent/by-id")
        self.assertEqual(result.rows, [])
        self.assertTrue(result.notes)

    def test_absent_partitions_reports_a_note(self):
        result = devices.inventory(partitions_file="/nonexistent/partitions")
        self.assertEqual(result.rows, [])
        self.assertTrue(result.notes)


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
