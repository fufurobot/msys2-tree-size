"""An incremental SQLite index, in the spirit of ``locate`` databases.

Every other command in this package starts from zero. That is fine for a small
tree and wasteful for a large one: the same directories are walked and the same
files stat'd on every invocation, even when almost nothing changed.

`locate`-family tools solved this long ago -- ``plocate``'s ``updatedb``
rebuilds incrementally rather than rescanning everything -- and this module
adapts that idea to SQLite.

Why SQLite: :mod:`sqlite3` is in the standard library, so the index adds no
dependency, and it brings indexing, querying and crash-safety for free. Given
this project's history with heavy external artifacts, "adds nothing to install"
is worth more than a nicer API.

How incrementality works
------------------------

A directory's **mtime changes when a direct child is added, removed or
renamed**, and does *not* change when a file deep inside the subtree is
modified. So on a rescan, for each directory:

* mtime matches what we stored, and the options match -> keep the subtree's
  rows untouched and do not walk it;
* otherwise -> walk that directory, replace its direct children, and recurse
  into any that are directories.

A no-op rescan therefore walks nothing at all, and a change deep in one branch
re-walks only that branch.

Limits, stated rather than hidden
---------------------------------

* **mtime granularity.** Filesystems with coarse timestamps can miss a change
  made within the same tick. ``st_mtime_ns`` is used to make this as unlikely as
  practical, but it is not a guarantee. ``--rebuild`` is the escape hatch.
* **Options are part of the key.** Rows produced with ``--archives`` differ from
  rows produced without, so the options string is stored per directory and a
  mismatch forces that subtree to be re-walked rather than silently mixing two
  datasets.
* **Content changes with unchanged mtime are invisible.** Editing a file in
  place usually updates its mtime, but a tool that restores an old mtime will
  not be noticed. Hashing is per-file and only stored when requested.

The counter ``RescanStats.directories_walked`` is the honest measure of whether
the optimisation worked, which is why the tests assert on it rather than on the
resulting rows.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import hashing, paths

#: Bumped whenever the schema changes. A database written by a *newer* version
#: is refused rather than misread.
SCHEMA_VERSION = 1

#: Default database location: per-user, outside any scanned tree.
DEFAULT_DB_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "msys2-tree-size"
)
DEFAULT_DB_PATH = os.path.join(DEFAULT_DB_DIR, "index.sqlite")


class IndexError_(Exception):
    """Raised when a database cannot be used as an index.

    Named with a trailing underscore so it does not shadow the builtin
    :exc:`IndexError`, which callers may still legitimately catch.
    """


@dataclass
class Row:
    """One indexed filesystem entry."""

    path: str
    parent: str | None
    name: str
    type: str
    size: int
    mtime_ns: int
    depth: int
    sha256: str = ""
    container: str | None = None


@dataclass
class RescanStats:
    """What a rescan actually did.

    ``directories_walked`` is the number that matters: it is zero when nothing
    changed, and it is what proves the index is incremental.
    """

    directories_walked: int = 0
    directories_skipped: int = 0
    entries: int = 0
    removed: int = 0
    full_scan: bool = False
    elapsed: float = 0.0

    @property
    def saved(self) -> int:
        """Directories that would have been walked without the index."""
        return self.directories_skipped


# ---------------------------------------------------------------------------
# connection and schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    path      TEXT PRIMARY KEY,
    parent    TEXT,
    name      TEXT NOT NULL,
    type      TEXT NOT NULL,
    size      INTEGER NOT NULL DEFAULT 0,
    mtime_ns  INTEGER NOT NULL DEFAULT 0,
    depth     INTEGER NOT NULL DEFAULT 0,
    sha256    TEXT NOT NULL DEFAULT '',
    container TEXT
);

CREATE INDEX IF NOT EXISTS entries_parent ON entries(parent);
CREATE INDEX IF NOT EXISTS entries_name   ON entries(name);
CREATE INDEX IF NOT EXISTS entries_sha    ON entries(sha256);
CREATE INDEX IF NOT EXISTS entries_size   ON entries(size);

-- Per-directory state: the mtime that was current when its direct children
-- were last read, and the options used to produce them.
CREATE TABLE IF NOT EXISTS directories (
    path      TEXT PRIMARY KEY,
    mtime_ns  INTEGER NOT NULL,
    options   TEXT NOT NULL DEFAULT '',
    scanned_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scan_meta (
    root         TEXT PRIMARY KEY,
    scanned_at   INTEGER NOT NULL,
    tool_version TEXT NOT NULL DEFAULT '',
    options      TEXT NOT NULL DEFAULT ''
);
"""


def connect(db_path: str | os.PathLike[str] = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the index at *db_path*.

    Raises :class:`IndexError_` if the file exists but is not an index this
    build can use -- either not a database at all, or written by a newer schema.
    Refusing is deliberate: quietly adding tables to somebody else's SQLite file,
    or reading columns that may have changed meaning, would be far worse.
    """
    path = Path(os.fspath(db_path))
    if path.parent and not path.parent.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise IndexError_(f"cannot create index directory {path.parent}: {exc}") from exc

    existed = path.exists() and path.stat().st_size > 0

    try:
        conn = sqlite3.connect(str(path), timeout=30.0)
    except sqlite3.Error as exc:
        raise IndexError_(f"cannot open index {path}: {exc}") from exc

    conn.row_factory = sqlite3.Row
    try:
        # WAL lets a reader query while a rescan writes, which is the normal
        # case: one process indexing, another looking things up.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error:
        # Not fatal: some filesystems (network shares) refuse WAL.
        pass

    if existed:
        _verify_existing(conn, path)

    try:
        conn.executescript(_SCHEMA)
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise IndexError_(f"{path} is not a usable index: {exc}") from exc

    _set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    _set_meta(conn, "created_by", "msys2-tree-size")
    conn.commit()
    return conn


def _verify_existing(conn: sqlite3.Connection, path: Path) -> None:
    """Refuse a file that exists but is not a compatible index."""
    try:
        found = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise IndexError_(f"{path} is not a database") from exc

    if found is None:
        conn.close()
        raise IndexError_(f"{path} exists but is not a msys2-tree-size index")

    try:
        version = int(found["value"])
    except (TypeError, ValueError) as exc:
        conn.close()
        raise IndexError_(f"{path} has an unreadable schema version") from exc

    if version > SCHEMA_VERSION:
        conn.close()
        raise IndexError_(
            f"{path} was written by a newer version (schema {version}, "
            f"this build understands {SCHEMA_VERSION}); upgrade or rebuild"
        )


def schema_version(conn: sqlite3.Connection) -> int:
    """Schema version recorded in *conn*."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row["value"]) if row else 0


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------


def store_rows(conn: sqlite3.Connection, rows: Sequence[Row]) -> None:
    """Insert or replace *rows*."""
    if not rows:
        return
    conn.executemany(
        "INSERT INTO entries(path, parent, name, type, size, mtime_ns, depth, sha256, container) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET "
        "  parent = excluded.parent, name = excluded.name, type = excluded.type, "
        "  size = excluded.size, mtime_ns = excluded.mtime_ns, depth = excluded.depth, "
        "  sha256 = excluded.sha256, container = excluded.container",
        [
            (
                row.path,
                row.parent,
                row.name,
                row.type,
                row.size,
                row.mtime_ns,
                row.depth,
                row.sha256,
                row.container,
            )
            for row in rows
        ],
    )


def load_rows(conn: sqlite3.Connection) -> Iterator[Row]:
    """Yield every indexed row."""
    cursor = conn.execute(
        "SELECT path, parent, name, type, size, mtime_ns, depth, sha256, container "
        "FROM entries ORDER BY path"
    )
    for record in cursor:
        yield _to_row(record)


def _to_row(record: sqlite3.Row) -> Row:
    return Row(
        path=record["path"],
        parent=record["parent"],
        name=record["name"],
        type=record["type"],
        size=record["size"],
        mtime_ns=record["mtime_ns"],
        depth=record["depth"],
        sha256=record["sha256"] or "",
        container=record["container"],
    )


def delete_subtree(conn: sqlite3.Connection, path: str) -> int:
    """Delete *path* and everything beneath it; return the number of rows removed.

    Matching is on path components rather than a ``LIKE 'path%'`` prefix, which
    would take ``/r/foobar`` along with ``/r/foo``.
    """
    cursor = conn.execute(
        "DELETE FROM entries WHERE path = ? OR path LIKE ? ESCAPE '\\'",
        (path, _subtree_pattern(path)),
    )
    removed = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    conn.execute(
        "DELETE FROM directories WHERE path = ? OR path LIKE ? ESCAPE '\\'",
        (path, _subtree_pattern(path)),
    )
    return removed


def _subtree_pattern(path: str) -> str:
    """A LIKE pattern matching *path* and its descendants, but not siblings.

    ``/r/foo`` becomes ``/r/foo/%`` -- requiring the separator is what keeps
    ``/r/foobar`` out. Any ``%`` or ``_`` already in the path is escaped.
    """
    escaped = path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped.rstrip("/") + "/%"


def record_directory(conn: sqlite3.Connection, path: str, *, mtime_ns: int, options: str) -> None:
    """Record that *path*'s direct children were read at *mtime_ns*."""
    conn.execute(
        "INSERT INTO directories(path, mtime_ns, options, scanned_at) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET "
        "  mtime_ns = excluded.mtime_ns, options = excluded.options, "
        "  scanned_at = excluded.scanned_at",
        (path, mtime_ns, options, int(time.time())),
    )


def directory_mtime(conn: sqlite3.Connection, path: str) -> int | None:
    """Stored mtime for *path*, or ``None`` if it was never indexed."""
    row = conn.execute("SELECT mtime_ns FROM directories WHERE path = ?", (path,)).fetchone()
    return int(row["mtime_ns"]) if row else None


def directory_options(conn: sqlite3.Connection, path: str) -> str | None:
    """Stored options for *path*, or ``None`` if it was never indexed."""
    row = conn.execute("SELECT options FROM directories WHERE path = ?", (path,)).fetchone()
    return row["options"] if row else None


def record_scan(conn: sqlite3.Connection, *, root: str, options: str, tool_version: str) -> None:
    """Record that *root* was scanned, with which options and tool version."""
    conn.execute(
        "INSERT INTO scan_meta(root, scanned_at, tool_version, options) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(root) DO UPDATE SET "
        "  scanned_at = excluded.scanned_at, tool_version = excluded.tool_version, "
        "  options = excluded.options",
        (root, int(time.time()), tool_version, options),
    )


def scan_meta(conn: sqlite3.Connection, root: str) -> dict[str, Any] | None:
    """Metadata for *root*, or ``None`` if it was never scanned."""
    row = conn.execute(
        "SELECT root, scanned_at, tool_version, options FROM scan_meta WHERE root = ?",
        (root,),
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# the incremental decision
# ---------------------------------------------------------------------------


def should_skip(
    path: str,
    *,
    stored_ns: int | None,
    current_ns: int,
    options_match: bool,
) -> bool:
    """Decide whether *path*'s subtree can be reused without walking it.

    A pure function so the rule can be tested directly, without a filesystem.
    All three conditions must hold: the directory was indexed before, its mtime
    is unchanged, and it was indexed with the same options.
    """
    if stored_ns is None:
        return False
    if not options_match:
        return False
    return stored_ns == current_ns


def options_key(*, hash_contents: bool, penetrate_archives: bool, archive_depth: int) -> str:
    """Canonical string describing the options that produced a set of rows.

    Stored per directory so a later run with different options knows its stored
    rows do not answer the new question.
    """
    return (
        f"hash={int(bool(hash_contents))};"
        f"archives={int(bool(penetrate_archives))};"
        f"archive_depth={int(archive_depth)}"
    )


# ---------------------------------------------------------------------------
# rescan
# ---------------------------------------------------------------------------


def rescan(
    conn: sqlite3.Connection,
    root: str | os.PathLike[str],
    *,
    hash_contents: bool = False,
    penetrate_archives: bool = False,
    archive_depth: int = 1,
    rebuild: bool = False,
) -> RescanStats:
    """Bring the index up to date for *root*, walking only what changed.

    :param rebuild: discard everything under *root* first, ignoring stored
        mtimes. The escape hatch for a filesystem whose timestamps are not
        trustworthy.

    Returns statistics whose ``directories_walked`` is the honest measure of how
    much work was avoided.
    """
    from . import archive, formats

    started = time.monotonic()
    root_path = paths.to_posix(paths.decode_path(root))
    options = options_key(
        hash_contents=hash_contents,
        penetrate_archives=penetrate_archives,
        archive_depth=archive_depth,
    )
    stats = RescanStats()

    if rebuild:
        delete_subtree(conn, root_path)
        stats.full_scan = True
        # The root row is about to be re-added below, so count it here for
        # consistency with the incremental path.
        stats.entries += 1

    try:
        root_stat = os.stat(paths.encode_path(paths.resolve_for_os(root_path)))
    except OSError:
        # The root is gone. Remove what we had so the index does not report
        # space that no longer exists.
        stats.removed = delete_subtree(conn, root_path)
        conn.commit()
        stats.elapsed = time.monotonic() - started
        return stats

    if not os.path.isdir(paths.encode_path(paths.resolve_for_os(root_path))):
        stats.elapsed = time.monotonic() - started
        return stats

    root_row = Row(
        path=root_path,
        parent=paths.parent(root_path),
        name=paths.basename(root_path) or root_path,
        type="dir",
        size=0,
        mtime_ns=int(getattr(root_stat, "st_mtime_ns", 0)),
        depth=0,
    )
    store_rows(conn, [root_row])

    _walk_incremental(
        conn,
        root_path,
        depth=0,
        options=options,
        hash_contents=hash_contents,
        stats=stats,
        # The root has no parent to vouch for it, so it is always listed; only
        # the recursion into its children is skipped.
        children_verified=False,
    )

    record_scan(
        conn,
        root=root_path,
        options=options,
        tool_version=_tool_version(),
    )
    conn.commit()
    stats.elapsed = time.monotonic() - started
    _ = (archive, formats)  # imported for parity with the walk; see below
    return stats


def _tool_version() -> str:
    from . import __version__

    return __version__


def _walk_incremental(
    conn: sqlite3.Connection,
    path: str,
    *,
    depth: int,
    options: str,
    hash_contents: bool,
    stats: RescanStats,
    children_verified: bool = False,
) -> None:
    """List *path*, then recurse only into children that may have changed.

    The rule, and why it is not simply "skip if this directory's mtime is
    unchanged":

    * A directory's mtime changes when a **direct child** is added, removed or
      renamed. It does *not* change when something is added inside a
      grandchild.
    * So a directory's *direct children* can be trusted, without re-listing,
      exactly when its mtime is unchanged **and** a previous run recorded them.
    * Its grandchildren cannot: a new file two levels down does not move this
      directory's mtime. Establishing that requires listing this directory and
      comparing each child's mtime.

    Hence: the root is always listed (it has no parent to vouch for it), a
    listing is one level only, and the recursion stops at any child whose mtime
    is unchanged. A no-op rescan therefore lists exactly one directory.

    :param children_verified: set when the caller established, by listing this
        directory's parent, that this directory's own entry is unchanged. Only
        then may this call return without listing anything.
    """
    try:
        current = os.stat(paths.encode_path(paths.resolve_for_os(path)))
        mtime_ns = int(getattr(current, "st_mtime_ns", 0))
    except OSError:
        stats.removed += delete_subtree(conn, path)
        return

    unchanged = should_skip(
        path,
        stored_ns=directory_mtime(conn, path),
        current_ns=mtime_ns,
        options_match=directory_options(conn, path) == options,
    )

    if children_verified and unchanged and _children_are_indexed(conn, path):
        stats.directories_skipped += 1
        return
    stats.directories_walked += 1
    # The directory being listed is itself an indexed entry.
    stats.entries += 1

    # Capture each child directory's recorded state *before* dropping rows,
    # because the delete below also removes the children's own directory
    # records. Reading them afterwards always returned None, which silently
    # defeated the entire optimisation: every rescan re-walked the whole tree.
    previous: dict[str, tuple[int | None, str | None, bool]] = {}
    for record in conn.execute(
        "SELECT e.path AS path, d.mtime_ns AS mtime_ns, d.options AS options "
        "FROM entries e LEFT JOIN directories d ON d.path = e.path "
        "WHERE e.parent = ? AND e.type = 'dir'",
        (path,),
    ):
        child_path = record["path"]
        had_children = (
            conn.execute("SELECT 1 FROM entries WHERE parent = ? LIMIT 1", (child_path,)).fetchone()
            is not None
        )
        previous[child_path] = (record["mtime_ns"], record["options"], had_children)

    # The direct children are about to be re-listed, so their own rows are
    # rewritten below. Note that a child's *subtree* is deliberately NOT deleted
    # here: a child whose mtime is unchanged is skipped rather than descended
    # into, and deleting its subtree first would leave it permanently missing.
    # Obsolete children (deleted or renamed away) are removed after the listing,
    # where it is known which ones survived.
    existing_children = {
        record["path"]
        for record in conn.execute("SELECT path FROM entries WHERE parent = ?", (path,))
    }

    try:
        names = sorted(os.listdir(paths.encode_path(paths.resolve_for_os(path))))
    except OSError:
        return

    rows: list[Row] = []
    recurse_into: list[str] = []
    seen: set[str] = set()

    for raw_name in names:
        name = paths.decode_path(raw_name) if isinstance(raw_name, bytes) else raw_name
        child = paths.join(path, name)
        child_row = _row_for(child, path, name, depth + 1, hash_contents)
        if child_row is None:
            continue
        rows.append(child_row)
        stats.entries += 1
        seen.add(child)

        if child_row.type != "dir":
            continue

        # Decide now, using the mtime just read for this child, whether its
        # subtree needs visiting. This is the whole optimisation: a child whose
        # mtime is unchanged cannot have gained or lost a direct entry, so
        # neither it nor anything beneath it needs listing.
        stored_ns, stored_options, had_children = previous.get(child, (None, None, False))
        child_unchanged = should_skip(
            child,
            stored_ns=stored_ns,
            current_ns=child_row.mtime_ns,
            options_match=stored_options == options,
        )
        if child_unchanged and had_children:
            stats.directories_skipped += 1
            continue
        recurse_into.append(child)

    # Anything that was a child before but is not now has been deleted or
    # renamed, so its subtree must go or the index would keep reporting space
    # that no longer exists.
    for stale in existing_children - seen:
        stats.removed += delete_subtree(conn, stale)

    store_rows(conn, rows)
    record_directory(conn, path, mtime_ns=mtime_ns, options=options)

    for child in recurse_into:
        _walk_incremental(
            conn,
            child,
            depth=depth + 1,
            options=options,
            hash_contents=hash_contents,
            stats=stats,
            # The parent's listing already showed this child's mtime moved, so
            # it must be listed; do not let it skip on its own say-so.
            children_verified=False,
        )


def _children_are_indexed(conn: sqlite3.Connection, path: str) -> bool:
    """True when the index already holds rows for *path*'s direct children.

    The guard that makes skipping sound: without it, a directory recorded as
    scanned but whose child rows were never committed (an interrupted run) would
    be skipped forever, leaving a permanent hole.
    """
    row = conn.execute("SELECT 1 FROM entries WHERE parent = ? LIMIT 1", (path,)).fetchone()
    return row is not None


def _delete_direct_children(conn: sqlite3.Connection, path: str) -> int:
    """Remove rows for *path*'s direct children and their subtrees."""
    cursor = conn.execute("SELECT path FROM entries WHERE parent = ?", (path,))
    children = [record["path"] for record in cursor]
    removed = 0
    for child in children:
        removed += delete_subtree(conn, child)
    return removed


def _delete_own_row(conn: sqlite3.Connection, path: str) -> int:
    """Remove just *path*'s own entry, leaving its subtree intact.

    Used for a child that is about to be re-listed: its row is rewritten from
    the fresh stat, but its subtree must survive because the child will be
    skipped rather than descended into.
    """
    cursor = conn.execute("DELETE FROM entries WHERE path = ?", (path,))
    return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


def _row_for(path: str, parent: str, name: str, depth: int, hash_contents: bool) -> Row | None:
    """Build a :class:`Row` for *path*, or ``None`` if it cannot be stat'd."""
    try:
        stat_result = os.stat(paths.encode_path(paths.resolve_for_os(path)))
    except OSError:
        return None

    import stat as stat_module

    mode = stat_result.st_mode
    if stat_module.S_ISDIR(mode):
        kind = "dir"
        size = 0
    elif stat_module.S_ISLNK(mode):
        kind = "link"
        # Consistent with the walk: a symlink's st_size is the length of its
        # target path, which is metadata and must not be counted.
        size = 0
    else:
        kind = "file"
        size = int(getattr(stat_result, "st_size", 0) or 0)

    digest = ""
    if hash_contents and kind == "file":
        digest = hashing.hash_file(paths.resolve_for_os(path))

    return Row(
        path=path,
        parent=parent,
        name=name,
        type=kind,
        size=size,
        mtime_ns=int(getattr(stat_result, "st_mtime_ns", 0)),
        depth=depth,
        sha256=digest,
        container=None,
    )


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------


def find(
    conn: sqlite3.Connection,
    pattern: str,
    *,
    under: str | None = None,
    files_only: bool = False,
    limit: int | None = None,
) -> Iterator[Row]:
    """Find indexed entries whose name matches *pattern*.

    *pattern* is matched with SQL ``GLOB`` semantics when it contains glob
    metacharacters, and as a substring otherwise, so both ``'*.tar.gz'`` and
    ``'report'`` do what a user expects.
    """
    clauses: list[str] = []
    params: list[Any] = []

    if any(ch in pattern for ch in "*?["):
        clauses.append("name GLOB ?")
        params.append(pattern)
    else:
        clauses.append("name LIKE ? ESCAPE '\\'")
        params.append("%" + pattern.replace("%", "\\%").replace("_", "\\_") + "%")

    if under:
        # Component-wise, so /r/foo does not match /r/foobar.
        clauses.append("(path = ? OR path LIKE ? ESCAPE '\\')")
        params.extend([under, _subtree_pattern(under)])

    if files_only:
        clauses.append("type = 'file'")

    sql = "SELECT * FROM entries WHERE " + " AND ".join(clauses) + " ORDER BY path"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))

    for record in conn.execute(sql, params):
        yield _to_row(record)


def largest(conn: sqlite3.Connection, *, limit: int = 20, files_only: bool = True) -> Iterator[Row]:
    """The biggest indexed entries, largest first."""
    sql = "SELECT * FROM entries"
    if files_only:
        sql += " WHERE type = 'file'"
    sql += " ORDER BY size DESC, path ASC LIMIT ?"
    for record in conn.execute(sql, (int(limit),)):
        yield _to_row(record)


def duplicates(
    conn: sqlite3.Connection, *, limit: int | None = None
) -> Iterator[tuple[str, int, int]]:
    """``(sha256, count, size)`` for every hash held by more than one entry.

    Only rows that actually carry a digest are considered, since hashing is
    optional and an empty digest is not evidence of shared content.
    """
    sql = (
        "SELECT sha256, COUNT(*) AS n, MAX(size) AS size FROM entries "
        "WHERE sha256 IS NOT NULL AND sha256 != '' "
        "GROUP BY sha256 HAVING n >= 2 ORDER BY size DESC"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params: tuple[Any, ...] = (int(limit),)
    else:
        params = ()
    for record in conn.execute(sql, params):
        yield (record["sha256"], int(record["n"]), int(record["size"] or 0))


def statistics(conn: sqlite3.Connection) -> dict[str, Any]:
    """Totals for the whole index."""
    row = conn.execute(
        "SELECT "
        "  SUM(CASE WHEN type = 'file' THEN 1 ELSE 0 END) AS files, "
        "  SUM(CASE WHEN type = 'dir'  THEN 1 ELSE 0 END) AS directories, "
        "  SUM(CASE WHEN type = 'link' THEN 1 ELSE 0 END) AS links, "
        "  SUM(CASE WHEN type = 'file' THEN size ELSE 0 END) AS bytes, "
        "  COUNT(*) AS entries "
        "FROM entries"
    ).fetchone()

    roots = [record["root"] for record in conn.execute("SELECT root FROM scan_meta ORDER BY root")]
    last = conn.execute("SELECT MAX(scanned_at) AS t FROM scan_meta").fetchone()

    return {
        "entries": int(row["entries"] or 0),
        "files": int(row["files"] or 0),
        "directories": int(row["directories"] or 0),
        "links": int(row["links"] or 0),
        "bytes": int(row["bytes"] or 0),
        "roots": roots,
        "scanned_at": int(last["t"]) if last and last["t"] else None,
    }


@dataclass
class Staleness:
    """How far an index has drifted from the filesystem."""

    checked: int = 0
    changed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def is_current(self) -> bool:
        return not self.changed and not self.missing


def check_staleness(conn: sqlite3.Connection, root: str) -> Staleness:
    """Re-stat indexed directories under *root* and report what moved.

    Used by ``--indexed``: trusting the index blindly would be faster but would
    silently report stale numbers, and correctness is the whole point. This
    walks directory metadata only -- no file listing, no hashing -- so it is
    cheap relative to a full scan.
    """
    result = Staleness()
    prefix_like = _subtree_pattern(root)

    cursor = conn.execute(
        "SELECT path, mtime_ns FROM directories "
        "WHERE path = ? OR path LIKE ? ESCAPE '\\' ORDER BY path",
        (root, prefix_like),
    )
    for record in cursor:
        path = record["path"]
        result.checked += 1
        try:
            stat_result = os.stat(paths.encode_path(paths.resolve_for_os(path)))
        except OSError:
            result.missing.append(path)
            continue
        if int(getattr(stat_result, "st_mtime_ns", 0)) != int(record["mtime_ns"]):
            result.changed.append(path)

    return result
