"""Render traversal results as text, tables or JSON.

Everything here is a pure function of the entries, so the output format can be
tested without a filesystem or a terminal.

Two decisions worth naming:

* **Tree output is ASCII by default.**  Box-drawing characters are mangled by
  many Windows console code pages, and MSYS2 terminals are not guaranteed to be
  UTF-8.  Unicode glyphs are available but must be asked for.
* **JSON goes through** :func:`msys2_tree_size.paths.write_json`, so a path
  containing an undecodable byte survives as a ``\\udcXX`` escape instead of
  raising ``UnicodeEncodeError``.
"""

from __future__ import annotations

import io
import os
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from . import duplicates as duplicates_mod
from . import paths, sizes

#: Column headers for the duplicate CSV report, in output order.
DUPLICATE_COLUMNS = (
    "sha256",
    "duplicate_count",
    "device_count",
    "cross_device",
    "path",
    "name",
    "type",
    "size",
    "human",
)


def _field(row: Any, key: str, default: Any = None) -> Any:
    """Read *key* from a mapping or an object, uniformly."""
    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


def _as_dict(row: Any) -> dict[str, Any]:
    """Convert an entry-like row to a plain dict."""
    if isinstance(row, Mapping):
        return dict(row)
    as_dict = getattr(row, "as_dict", None)
    if callable(as_dict):
        return as_dict()
    return dict(vars(row))


def _sort_key(row: Any) -> tuple[int, str]:
    """Order by size descending, then path, for stable output."""
    try:
        size = -int(_field(row, "size", 0) or 0)
    except (TypeError, ValueError):
        size = 0
    return size, str(_field(row, "path", ""))


def select(
    rows: Iterable[Any],
    *,
    top: int | None = None,
    max_depth: int | None = None,
    min_size: int | None = None,
) -> list[Any]:
    """Filter and order *rows* for display.

    Filtering is applied before ``top`` so that ``--top 10`` means "the ten
    largest entries that passed the other filters", which is what a user
    asking for the biggest directories expects.
    """
    out = list(rows)
    if max_depth is not None:
        out = [r for r in out if int(_field(r, "depth", 0) or 0) <= max_depth]
    if min_size is not None:
        out = [r for r in out if int(_field(r, "size", 0) or 0) >= min_size]
    out.sort(key=_sort_key)
    if top is not None:
        out = out[:top]
    return out


def render_flat(rows: Iterable[Any], **kwargs: Any) -> str:
    """One line per entry: human size, percentage, type, path.

    Rows carrying an ``error`` get the message appended, so a partial scan is
    visibly partial rather than silently under-reporting.
    """
    selected = select(rows, **kwargs)
    if not selected:
        return ""

    total = _total(selected)
    width = max(len(sizes.human_readable(_field(row, "size", 0))) for row in selected)
    lines = []
    for row in selected:
        row_size = _field(row, "size", 0)
        human = sizes.human_readable(row_size)
        percent = sizes.percentage(row_size, total)
        kind = _field(row, "type", "?")
        target = paths.safe_text(str(_field(row, "path", "")))
        line = f"{human:>{width}}  {percent:>6.2f}%  {kind:<4}  {target}"
        error = _field(row, "error")
        if error:
            line += f"  [error: {paths.safe_text(str(error))}]"
        lines.append(line)
    return "\n".join(lines) + "\n"


def _total(rows: Sequence[Any]) -> int:
    """Size of the shallowest entry, i.e. the tree root's subtree total."""
    if not rows:
        return 0
    shallowest = min(rows, key=lambda r: int(_field(r, "depth", 0) or 0))
    return int(_field(shallowest, "size", 0) or 0)


_ASCII_GLYPHS = {"branch": "|-- ", "last": "`-- ", "pipe": "|   ", "blank": "    "}
_UNICODE_GLYPHS = {"branch": "├── ", "last": "└── ", "pipe": "│   ", "blank": "    "}


def render_tree(
    rows: Iterable[Any],
    *,
    ascii_only: bool = True,
    show_files: bool = True,
    **kwargs: Any,
) -> str:
    """Render entries as an indented tree.

    The tree is rebuilt from the flat ``parent`` links rather than assumed to
    arrive in traversal order, so a filtered or re-sorted input still renders
    correctly.
    """
    selected = select(rows, **kwargs)
    if not selected:
        return ""

    if not show_files:
        selected = [r for r in selected if _field(r, "type") == "dir"]
        if not selected:
            return ""

    glyphs = _ASCII_GLYPHS if ascii_only else _UNICODE_GLYPHS
    by_path = {str(_field(r, "path", "")): r for r in selected}

    children: dict[str | None, list[Any]] = {}
    for row in selected:
        parent = _field(row, "parent")
        if parent is None or str(parent) == "":
            children.setdefault(None, []).append(row)
        elif str(parent) in by_path:
            children.setdefault(str(parent), []).append(row)
        elif _is_archive_member(row):
            # The row is inside an archive whose own entry was filtered out
            # (by --top, --max-depth or --min-size).  Its parent is gone, so it
            # must be dropped rather than promoted to a root: rendering
            # "data.tar.gz::payload" as a top-level line would claim a file
            # exists at a path that no entry describes.
            continue
        else:
            # An ordinary entry whose directory was filtered out.  Showing it at
            # the top level is reasonable, since a real file does exist there.
            children.setdefault(None, []).append(row)

    for group in children.values():
        group.sort(key=lambda r: (-int(_field(r, "size", 0) or 0), str(_field(r, "path", ""))))

    roots = children.get(None, [])
    out = io.StringIO()

    def emit(row: Any, prefix: str, is_last: bool, is_root: bool) -> None:
        if is_root:
            out.write(_label(row) + "\n")
            child_prefix = ""
        else:
            out.write(
                prefix + (glyphs["last"] if is_last else glyphs["branch"]) + _label(row) + "\n"
            )
            child_prefix = prefix + (glyphs["blank"] if is_last else glyphs["pipe"])

        kids = children.get(str(_field(row, "path", "")), [])
        for index, kid in enumerate(kids):
            emit(kid, child_prefix, index == len(kids) - 1, False)

    for index, root in enumerate(roots):
        emit(root, "", index == len(roots) - 1, True)

    return out.getvalue()


def _is_archive_member(row: Any) -> bool:
    """True when *row* lives inside an archive rather than on disk."""
    if _field(row, "container") is not None:
        return True
    # Fall back to the separator, so this also works for plain dicts coming
    # from a JSON report that predates the container field.
    return "::" in str(_field(row, "path", ""))


def _label(row: Any) -> str:
    """One tree node's text: size, then name."""
    human = sizes.human_readable(_field(row, "size", 0))
    name = str(_field(row, "name", "") or _field(row, "path", ""))
    text = f"{human:>9}  {paths.safe_text(name)}"
    error = _field(row, "error")
    if error:
        text += f"  [error: {paths.safe_text(str(error))}]"
    return text


def summarize(rows: Sequence[Any]) -> dict[str, Any]:
    """Aggregate statistics for the JSON envelope and the CLI footer."""
    rows = list(rows)
    if not rows:
        return {
            "total_bytes": 0,
            "total_human": sizes.human_readable(0),
            "file_count": 0,
            "dir_count": 0,
            "error_count": 0,
            "entropy_bits": 0.0,
            "extensions": {},
            "largest": None,
        }

    total = _total(rows)
    files = [r for r in rows if _field(r, "type") == "file"]
    dirs = [r for r in rows if _field(r, "type") == "dir"]
    errors = [r for r in rows if _field(r, "error")]

    shallowest = min(rows, key=lambda r: int(_field(r, "depth", 0) or 0))
    direct = [
        r
        for r in rows
        if int(_field(r, "depth", 0) or 0) == int(_field(shallowest, "depth", 0) or 0) + 1
    ]

    # The root's size *is* total_bytes, so reporting it as "largest" would be
    # useless.  Candidates are everything below the shallowest entry.
    root_depth = int(_field(shallowest, "depth", 0) or 0)
    candidates = [r for r in rows if int(_field(r, "depth", 0) or 0) > root_depth] or rows
    largest = max(
        candidates, key=lambda r: (int(_field(r, "size", 0) or 0), str(_field(r, "path", "")))
    )

    return {
        "total_bytes": total,
        "total_human": sizes.human_readable(total),
        "file_count": len(files),
        "dir_count": len(dirs),
        "error_count": len(errors),
        "entropy_bits": round(sizes.entropy_bits([_field(r, "size", 0) for r in direct]), 6),
        "extensions": sizes.extension_totals(rows),
        "largest": {
            "path": str(_field(largest, "path", "")),
            "size": int(_field(largest, "size", 0) or 0),
            "human": sizes.human_readable(_field(largest, "size", 0)),
        },
    }


def render_json(
    rows: Iterable[Any],
    *,
    duplicates: Sequence[Mapping[str, Any]] | None = None,
    indent: int | None = None,
    **kwargs: Any,
) -> str:
    """Render a JSON envelope containing a summary block and the entries."""
    selected = select(rows, **kwargs)
    payload: dict[str, Any] = {
        "summary": summarize(selected),
        "entries": [_as_dict(row) for row in selected],
    }
    if duplicates is not None:
        payload["duplicates"] = [dict(row) for row in duplicates]
    return json_dumps(payload, indent=indent)


def json_dumps(obj: Any, indent: int | None = None) -> str:
    """JSON text that is safe for paths holding lone surrogates."""
    import json

    return json.dumps(obj, ensure_ascii=True, indent=indent)


# ---------------------------------------------------------------------------
# file writers
# ---------------------------------------------------------------------------


def write_flat(path: str | os.PathLike[str], rows: Iterable[Any], **kwargs: Any) -> None:
    """Write the flat listing to *path*."""
    paths.write_text(path, render_flat(rows, **kwargs))


def write_json(
    path: str | os.PathLike[str],
    rows: Iterable[Any],
    *,
    indent: int | None = 2,
    **kwargs: Any,
) -> None:
    """Write the JSON report to *path*."""
    paths.write_text(path, render_json(rows, indent=indent, **kwargs))


def write_duplicates_csv(
    path: str | os.PathLike[str],
    rows: Iterable[Mapping[str, Any]],
) -> None:
    """Write duplicate report *rows* as CSV using :data:`DUPLICATE_COLUMNS`."""
    header = list(DUPLICATE_COLUMNS)
    body = []
    for row in rows:
        body.append([_csv_cell(row.get(column)) for column in header])
    paths.write_csv(path, header, body)


def _csv_cell(value: Any) -> str:
    """Render one CSV cell; booleans become ``true``/``false``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def duplicate_rows(entries: Iterable[Any]) -> list[dict[str, Any]]:
    """Convenience: find duplicates in *entries* and flatten them to rows."""
    return duplicates_mod.to_rows(duplicates_mod.find_duplicates(entries))
