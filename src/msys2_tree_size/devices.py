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
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from . import paths

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


def _run_msys2(shell: str, script: str) -> str:
    """Run *script* through ``msys2_shell.cmd`` and return its stdout.

    This is the fallback for reaching MSYS2's virtual filesystems from a Win32
    process.  MSYS2's ``usr/bin`` tools are POSIX binaries that need the MSYS2
    runtime: started directly they die with::

        NtCreateDirectoryObject(\\BaseNamedObjects\\msys-2.0S5-...): 0xC0000022

    because they cannot create their shared-memory section outside an MSYS2
    console.  Routing through the launcher supplies the environment they need.

    ``msys2_shell.cmd`` is a batch file, so it is invoked through ``cmd.exe``;
    ``subprocess`` cannot start a ``.cmd`` directly.

    Note: some restricted environments (notably sandboxes that constrain child
    processes) deny the MSYS2 runtime the object-directory creation it needs, in
    which case this returns an empty string and the caller reports a note rather
    than raising.  That is a property of the host, not of the parsing code.
    """
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    command = [
        comspec,
        "/c",
        shell,
        "-defterm",
        "-no-start",
        "-here",
        "-c",
        script,
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command,
            capture_output=True,
            check=False,
        )
    except (OSError, ValueError):
        return ""

    # The launcher emits its own banner on stdout; the payload follows the
    # sentinel the script echoes.
    text = completed.stdout.decode("utf-8", "surrogateescape")
    marker = "===DATA==="
    if marker in text:
        return text.split(marker, 1)[1]
    return text


def _read_virtual(path: str, shell: str | None) -> str:
    """Read an MSYS2 virtual path, preferring a direct call and falling back.

    A direct ``cat`` succeeds when the caller is already an MSYS2 process;
    otherwise the launcher is used.
    """
    cat = _tool("cat")
    if cat is not None:
        direct = _run([cat, path])
        if direct.strip():
            return direct
    if shell:
        return _run_msys2(shell, f"echo '===DATA==='; cat '{path}'")
    return ""


def _list_virtual(path: str, shell: str | None) -> str:
    """List an MSYS2 virtual directory, with the same fallback as above."""
    ls = _tool("ls")
    if ls is not None:
        direct = _run([ls, "-l", path])
        if direct.strip():
            return direct
    if shell:
        return _run_msys2(shell, f"echo '===DATA==='; ls -l '{path}'")
    return ""


def msys2_shell() -> str | None:
    """Path to ``msys2_shell.cmd``, or ``None`` when MSYS2 is not installed."""
    root = paths.msys2_root()
    if not root:
        return None
    shell = os.path.join(root, "msys2_shell.cmd")
    return shell if os.path.isfile(shell) else None


def _tool(name: str) -> str | None:
    """Resolve an MSYS2 tool to an absolute path, or ``None``.

    ``/dev/disk/by-id`` and ``/proc/partitions`` are MSYS2 *virtual* paths: a
    native Windows interpreter cannot open them even when MSYS2 is installed,
    because the MSYS2 runtime resolves them only for MSYS2 binaries.  The
    tools are therefore located explicitly under the MSYS2 root so the lookup
    does not depend on PATH ordering.
    """
    root = paths.msys2_root()
    if root:
        for sub in ("usr/bin", "clang64/bin", "ucrt64/bin", "mingw64/bin"):
            candidate = os.path.join(root, sub, name + ".exe")
            if os.path.isfile(candidate):
                return candidate
    return shutil.which(name)


def has_msys2_tools() -> bool:
    """True when the MSYS2 tools needed to read the virtual paths exist."""
    return _tool("ls") is not None and _tool("cat") is not None


def diagnose() -> list[str]:
    """Explain how the MSYS2 environment was detected, for ``--verbose``-style use."""
    root = paths.msys2_root()
    lines = [f"MSYS2 root: {root or '<not found>'}"]
    lines.append(f"interpreter is MSYS/Cygwin python: {paths.is_msys2()}")
    for tool in ("ls", "cat"):
        lines.append(f"{tool}: {_tool(tool) or '<not found>'}")
    return lines


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
    """Parse ``/proc/partitions`` into rows of major/minor/blocks/name.

    MSYS2 adds a **fifth** column, ``win-mounts``, holding the Windows drive
    letter (``C:\\``) for partitions that have one.  A strict four-column check
    would therefore discard every row on a real MSYS2 system, so the first four
    columns are read positionally and whatever follows is kept as
    ``win-mounts``.  Upstream Linux output has no such column and yields an
    empty string, so both shapes expose identical keys.
    """
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        columns = line.split()
        # Data rows need at least the four positional fields; the header line
        # has "name" where the device name goes and fails the int() below.
        if len(columns) < 4:
            continue
        major, minor, blocks, name = columns[:4]
        try:
            row = {
                "major": int(major),
                "minor": int(minor),
                "#blocks": int(blocks),
                "name": name,
                # Trailing columns are the MSYS2 win-mounts field; rejoin them
                # in case a mount label ever contains a space.
                "win-mounts": " ".join(columns[4:]),
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
                "win-mounts": partition.get("win-mounts", ""),
            }
        )

    table.sort(key=lambda r: (r["blocks"], r["name"]))
    return table


def read_by_id(by_id_dir: str | os.PathLike[str] = BY_ID_DIR) -> list[dict[str, Any]]:
    """Read and parse the by-id directory; empty list when unavailable.

    Existence is decided by ``ls`` itself rather than ``os.path.isdir``, because
    these are virtual paths that a Win32 interpreter cannot stat.
    """
    return parse_by_id(_list_virtual(os.fspath(by_id_dir), msys2_shell()))


def read_partitions(
    partitions_file: str | os.PathLike[str] = PARTITIONS_FILE,
) -> list[dict[str, Any]]:
    """Read and parse the partitions file; empty list when unavailable."""
    return parse_partitions(_read_virtual(os.fspath(partitions_file), msys2_shell()))


def inventory(
    by_id_dir: str | os.PathLike[str] = BY_ID_DIR,
    partitions_file: str | os.PathLike[str] = PARTITIONS_FILE,
) -> Inventory:
    """Collect the device table, recording why it may be empty.

    Everything that can go wrong is *expected* on some systems: no MSYS2
    installation, or one whose virtual device tree is not populated (containers).
    Each case becomes a note so the caller can explain itself rather than fail.
    """
    notes: list[str] = []
    shell = msys2_shell()

    if not has_msys2_tools() and shell is None:
        notes.append("MSYS2 not found; cannot read /dev/disk/by-id or /proc/partitions")
        return Inventory(rows=[], notes=notes)

    by_id_text = _list_virtual(os.fspath(by_id_dir), shell)
    if not by_id_text.strip():
        notes.append(f"{by_id_dir} is not readable on this system")
        return Inventory(rows=[], notes=notes)

    partitions_text = _read_virtual(os.fspath(partitions_file), shell)
    if not partitions_text.strip():
        notes.append(f"{partitions_file} is not readable on this system")
        return Inventory(rows=[], notes=notes)

    rows = build_table(by_id_text, partitions_text)
    if not rows:
        notes.append("no device could be joined between by-id and /proc/partitions")
    return Inventory(rows=rows, notes=notes)
