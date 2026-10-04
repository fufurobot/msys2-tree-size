"""Duplicate detection over hashed entries.

Two rules make the results trustworthy, and both exist to stop byte counts
from being inflated:

**The parent/child rule.**  When a directory's subtree hash equals one of its
children's hashes, the child holds exactly the same content as its ancestor, so
it adds nothing.  Reporting both would double-count the entire subtree.  The
rule is applied transitively along a path, so a chain ``/a`` -> ``/a/b`` ->
``/a/b/c`` collapses to just ``/a``.

**Null digests are never duplicates.**  A file that could not be read has no
digest; two unreadable files are not evidence of duplicated content, so they
are excluded rather than grouped together.

Groups themselves are ordered deterministically (by digest, then by path) so
output is reproducible across runs and filesystems.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Callable

from .hashing import is_null

DeleteRule = Callable[[Any], str]


def _field(row: Any, key: str) -> Any:
    """Read *key* from a mapping or an object, uniformly."""
    if isinstance(row, Mapping):
        return row.get(key)
    return getattr(row, key, None)


def _path(row: Any) -> str:
    return str(_field(row, "path") or "")


def _digest(row: Any) -> str:
    return str(_field(row, "sha256") or "")


def _size(row: Any) -> int:
    try:
        return int(_field(row, "size") or 0)
    except (TypeError, ValueError):
        return 0


def _parent(row: Any) -> str | None:
    value = _field(row, "parent")
    return None if value is None else str(value)


def drop_redundant_children(rows: Sequence[Any]) -> list[Any]:
    """Remove entries whose digest equals that of one of their ancestors.

    Only entries in the same chain (an ancestor/descendant relationship) are
    compared: the walk's Merkle hash makes equal digests mean equal subtree
    content, and within one chain the descendant is the redundant copy.
    """
    by_path = {_path(row): row for row in rows}
    digest_of = {path: _digest(row) for path, row in by_path.items()}

    kept: list[Any] = []
    for row in rows:
        digest = _digest(row)
        if is_null(digest):
            kept.append(row)
            continue

        ancestor = _parent(row)
        redundant = False
        # Walk up the chain, stopping at the first ancestor that is not part of
        # this result set (nothing above it can have been compared).
        seen: set[str] = set()
        while ancestor is not None and ancestor not in seen:
            seen.add(ancestor)
            if ancestor in digest_of and digest_of[ancestor] == digest:
                redundant = True
                break
            ancestor_row = by_path.get(ancestor)
            ancestor = _parent(ancestor_row) if ancestor_row is not None else None

        if not redundant:
            kept.append(row)

    return kept


def find_duplicates(
    rows: Iterable[Any],
    *,
    device_of: Callable[[str], str] | None = None,
) -> list[list[Any]]:
    """Group *rows* by identical digest, applying the parent/child rule.

    Returns a list of groups, each a list of entries sharing one digest, with
    two or more members.  Groups are sorted by digest and each group's members
    are sorted by path, so the result is deterministic.

    :param device_of: optional ``path -> device`` mapping used to annotate
        groups that span more than one device.
    """
    rows = list(rows)
    rows = drop_redundant_children(rows)

    groups: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        digest = _digest(row)
        if is_null(digest):
            continue
        groups[digest].append(row)

    result: list[list[Any]] = []
    for digest in sorted(groups):
        members = sorted(groups[digest], key=_path)
        if len(members) >= 2:
            result.append(members)
    return result


def summarize(rows: Iterable[Any]) -> dict[str, int]:
    """Aggregate duplicate statistics for *rows*.

    ``wasted_bytes`` counts every copy except one per group: that is the space
    recoverable by keeping a single copy of each duplicated file.
    """
    groups = find_duplicates(rows)
    duplicate_files = 0
    wasted = 0
    for group in groups:
        duplicate_files += len(group)
        sizes = [_size(row) for row in group]
        # Subtracting the largest copy is conservative: it never claims more
        # reclaimable space than actually exists, even if copies differ in size.
        wasted += sum(sizes) - (max(sizes) if sizes else 0)
    return {
        "group_count": len(groups),
        "duplicate_files": duplicate_files,
        "wasted_bytes": wasted,
    }


def to_rows(
    groups: Sequence[Sequence[Any]],
    *,
    device_of: Callable[[str], str] | None = None,
) -> list[dict[str, Any]]:
    """Flatten duplicate *groups* into report rows with group metadata.

    Each row carries ``duplicate_count``, ``device_count`` and ``cross_device``
    so a consumer can see at a glance whether a group spans physical drives.
    """
    out: list[dict[str, Any]] = []

    for group in groups:
        members = sorted(group, key=_path)
        count = len(members)

        if device_of is not None:
            devices = {device_of(_path(row)) for row in members}
        else:
            # Without a device mapping, group by parent directory: the best
            # available proxy for "is this spread around the filesystem".
            devices = {_parent(row) or "" for row in members}
        device_count = len(devices)

        for row in members:
            if isinstance(row, Mapping):
                record = dict(row)
            else:
                as_dict = getattr(row, "as_dict", None)
                record = as_dict() if callable(as_dict) else dict(vars(row))
            record["duplicate_count"] = count
            record["device_count"] = device_count
            record["cross_device"] = device_count > 1
            out.append(record)

    return out


def total_wasted_human(rows: Iterable[Any]) -> str:
    """Human-readable form of :func:`summarize`'s ``wasted_bytes``."""
    from .sizes import human_readable

    return human_readable(summarize(rows)["wasted_bytes"])
