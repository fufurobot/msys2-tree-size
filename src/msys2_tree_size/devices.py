"""Physical device inventory from ``/dev/disk/by-id`` and ``/proc/partitions``.

Both files are MSYS2/Cygwin-isms that expose information plain Win32 does not:
stable hardware-derived device names, and partitions that may have no drive
letter at all.

The join is deliberately split into pure parsing plus a thin reader:

* :func:`parse_by_id` and :func:`parse_partitions` take *text* and return rows,
  so they are unit-testable against captured output with no shell involved.
* :func:`read_by_id` and :func:`read_partitions` do the I/O and degrade to an
  empty list when the path is absent, which is the normal case on Windows.

Parsing quirks that the fixtures pin down:

* ``ls -l`` output has a variable number of columns, so only the last three are
  used: the link name, ``->``, and the target.
* ``/proc/partitions`` carries a header row that must not become a data row.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

BY_ID_DIR = "/dev/disk/by-id"
PARTITIONS_FILE = "/proc/partitions"


@dataclass
class Inventory:
    """The joined device table plus notes about anything that was missing."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _run(command: Sequence[str]) -> str:
    """Run *command*, returning stdout; empty string on any failure.

    Uses an argv list rather than ``shell=True`` so no argument can be
    reinterpreted by a shell.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, no shell
            list(command),
            capture_output=True,
            check=False,
        )
    except (OSError, ValueError):
        return ""
    return completed.stdout.decode("utf-8", "surrogateescape")


def parse_by_id(text: str) -> list[dict[str, Any]]:
    """Parse ``ls -l /dev/disk/by-id`` output into rows.

    Each row has ``id`` (the symlink name), ``to`` (the literal ``->``) and
    ``actual`` (the relative symlink target), plus ``name`` — the kernel device
    name taken from the target's basename.
    """
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("total"):
            continue
        columns = line.split()
        # A symlink line ends with "<name> -> <target>"; anything shorter is not
        # a usable entry, and malformed lines are skipped rather than fatal.
        if len(columns) < 3 or columns[-2] != "->":
            continue
        ident, actual = columns[-3], columns[-1]
        rows.append(
            {
                "id": ident,
                "to": columns[-2],
                "actual": actual,
                "name": os.path.basename(actual.rstrip("/")),
            }
        )
    return rows


def parse_partitions(text: str) -> list[dict[str, Any]]:
    """Parse ``/proc/partitions`` into rows of major/minor/blocks/name."""
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        columns = line.split()
        # Data rows have exactly four numeric-ish columns; the header has
        # "name" in the last position and is skipped by the same check.
        if len(columns) != 4:
            continue
        major, minor, blocks, name = columns
        try:
            row = {
                "major": int(major),
                "minor": int(minor),
                "#blocks": int(blocks),
                "name": name,
            }
        except ValueError:
            continue
        rows.append(row)
    return rows


def build_table(ls_text: str, partitions_text: str) -> list[dict[str, Any]]:
    """Join by-id symlinks against partitions, smallest device first.

    The join is on the kernel device name and is an *inner* join: an entry with
    no counterpart on either side is dropped, because a size with no device (or
    a device with no stable name) cannot be attributed usefully.

    Sorting ascending by ``#blocks`` is deliberate: small devices finish fast,
    so a caller processing the table in order produces output early.
    """
    by_id = parse_by_id(ls_text)
    partitions = {row["name"]: row for row in parse_partitions(partitions_text)}

    table: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in by_id:
        partition = partitions.get(row["name"])
        if partition is None or row["name"] in seen:
            continue
        seen.add(row["name"])
        table.append(
            {
                "id": row["id"],
                "name": row["name"],
                "actual": row["actual"],
                "major": partition["major"],
                "minor": partition["minor"],
                "blocks": partition["#blocks"],
            }
        )

    table.sort(key=lambda r: (r["blocks"], r["name"]))
    return table


def read_by_id(by_id_dir: str | os.PathLike[str] = BY_ID_DIR) -> list[dict[str, Any]]:
    """Read and parse the by-id directory; empty list when unavailable."""
    path = os.fspath(by_id_dir)
    if not os.path.isdir(path):
        return []
    return parse_by_id(_run(["ls", "-l", path]))


def read_partitions(
    partitions_file: str | os.PathLike[str] = PARTITIONS_FILE,
) -> list[dict[str, Any]]:
    """Read and parse the partitions file; empty list when unavailable."""
    path = os.fspath(partitions_file)
    if not os.path.isfile(path):
        return []
    return parse_partitions(_run(["cat", path]))


def inventory(
    by_id_dir: str | os.PathLike[str] = BY_ID_DIR,
    partitions_file: str | os.PathLike[str] = PARTITIONS_FILE,
) -> Inventory:
    """Collect the device table, recording why it may be empty.

    A missing ``/dev/disk/by-id`` or ``/proc/partitions`` is expected outside
    MSYS2, so it is reported as a note rather than raised.
    """
    notes: list[str] = []

    if not os.path.isdir(os.fspath(by_id_dir)):
        notes.append(f"{by_id_dir} is not available on this system")
        return Inventory(rows=[], notes=notes)

    if not os.path.isfile(os.fspath(partitions_file)):
        notes.append(f"{partitions_file} is not available on this system")
        return Inventory(rows=[], notes=notes)

    rows = build_table(
        _run(["ls", "-l", os.fspath(by_id_dir)]), _run(["cat", os.fspath(partitions_file)])
    )
    if not rows:
        notes.append("no device could be joined between by-id and /proc/partitions")
    return Inventory(rows=rows, notes=notes)
