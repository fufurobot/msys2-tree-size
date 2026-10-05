"""Size formatting, parsing and aggregation.

Sizes are always integer **bytes** internally.  Formatting is base-2 to match
``du -h`` and the ``/proc/partitions`` KiB convention, so a "1K" in this package
means 1024 bytes, never 1000.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

#: Base-2 unit suffixes, in ascending order.  ``E`` is terminal: see
#: :func:`human_readable`.
_UNITS = ("B", "K", "M", "G", "T", "P", "E")

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGTPE]?)B?\s*$", re.IGNORECASE)

_MULTIPLIERS = {
    "": 1,
    "K": 1024,
    "M": 1024**2,
    "G": 1024**3,
    "T": 1024**4,
    "P": 1024**5,
    "E": 1024**6,
}

NO_EXTENSION = "<none>"


def human_readable(num_bytes: Any) -> str:
    """Format *num_bytes* as a short base-2 string such as ``1.5K``.

    Bytes are printed without a decimal point (``1023B``), larger units with
    one.  ``None`` and negative values are treated as zero so callers do not
    need to guard every call site.
    """
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return "0B"
    if not math.isfinite(size) or size <= 0:
        return "0B"

    for unit in _UNITS:
        if size < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(size)}B"
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}E"  # unreachable, keeps type checkers happy


def parse_size(text: str) -> int:
    """Parse a human size such as ``1.5K`` or ``10MB`` into bytes.

    Suffixes are base-2 and case-insensitive, and a trailing ``B`` is optional.
    Raises :class:`ValueError` for anything that is not a size.
    """
    if not isinstance(text, str):
        raise ValueError(f"not a size: {text!r}")
    match = _SIZE_RE.match(text)
    if not match:
        raise ValueError(f"not a size: {text!r}")
    number, suffix = match.group(1), match.group(2).upper()
    return int(float(number) * _MULTIPLIERS[suffix])


def percentage(part: Any, whole: Any) -> float:
    """``part`` as a percentage of ``whole``, rounded to 4 decimals.

    A zero (or non-numeric) ``whole`` yields ``0.0`` rather than raising, since
    empty directories are normal and should not crash a report.
    """
    try:
        whole_f = float(whole)
        if whole_f == 0:
            return 0.0
        return round(100.0 * float(part) / whole_f, 4)
    except (TypeError, ValueError):
        return 0.0


def entropy_bits(sizes: Iterable[Any]) -> float:
    """Shannon entropy in bits of the distribution of *sizes*.

    Used as a cheap "is this directory balanced or dominated by one child?"
    signal.  A single child gives 0 bits; four equal children give 2 bits.
    Non-positive entries are ignored, and an all-zero input yields 0.
    """
    values = []
    for value in sizes:
        try:
            f = float(value)
        except (TypeError, ValueError):
            continue
        if f > 0:
            values.append(f)
    total = sum(values)
    if total <= 0:
        return 0.0
    bits = 0.0
    for value in values:
        p = value / total
        bits -= p * math.log2(p)
    return bits


def extension_totals(rows: Iterable[Mapping[str, Any] | Any]) -> dict[str, int]:
    """Total bytes per file extension across *rows*.

    Directories are skipped so their subtree totals are not counted twice.
    Extensionless files are grouped under :data:`NO_EXTENSION`.
    """
    from .paths import extension

    totals: dict[str, int] = defaultdict(int)
    for row in rows:
        if _field(row, "type") != "file":
            continue
        try:
            size = int(_field(row, "size") or 0)
        except (TypeError, ValueError):
            size = 0
        totals[extension(str(_field(row, "name") or ""))] += size
    return dict(totals)


def top_extension(rows: Iterable[Mapping[str, Any] | Any]) -> tuple[str, int]:
    """Return the ``(extension, bytes)`` pair consuming the most space.

    Ties are broken by extension name so the result is deterministic.
    """
    totals = extension_totals(rows)
    if not totals:
        return NO_EXTENSION, 0
    name = max(sorted(totals), key=lambda key: totals[key])
    return name, totals[name]


def _field(row: Any, key: str) -> Any:
    """Read *key* from a mapping or an object, uniformly."""
    if isinstance(row, Mapping):
        return row.get(key)
    return getattr(row, key, None)
