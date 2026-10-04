"""The single filesystem traversal every command is built on.

:func:`walk` yields one :class:`Entry` per filesystem object in **post-order**:
every descendant is emitted strictly before its ancestor.  That single choice
buys three properties at once:

1. **Directory sizes are complete on arrival.**  By the time a directory is
   emitted, all of its children have been measured, so ``size`` is the subtree
   total with no second pass and no ``os.walk`` + ``getsize`` double counting.
2. **Subtree hashes are Merkle hashes.**  A directory's digest is computed from
   its children's digests, which are already known.
3. **Memory stays bounded by depth, not by tree size**, because aggregation
   happens on the way up as the generator unwinds.

The filesystem is accessed through a tiny duck-typed adapter so the traversal
can be driven by :class:`FakeFS` in tests without touching a disk.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Callable

from . import hashing, paths, sizes

DIR = "dir"
FILE = "file"
LINK = "link"
OTHER = "other"


@dataclass
class Entry:
    """One filesystem object, fully measured."""

    path: str
    name: str
    parent: str | None
    type: str
    depth: int
    size: int
    sha256: str
    child_count: int
    percent_of_parent: float
    error: str | None = None

    @property
    def human(self) -> str:
        """Size as a short base-2 string."""
        return sizes.human_readable(self.size)

    @property
    def is_dir(self) -> bool:
        return self.type == DIR

    def as_dict(self) -> dict[str, Any]:
        """Plain-dict view with a stable key order for serialisation."""
        return {
            "path": self.path,
            "name": self.name,
            "parent": self.parent,
            "type": self.type,
            "depth": self.depth,
            "size": self.size,
            "human": self.human,
            "sha256": self.sha256,
            "child_count": self.child_count,
            "percent_of_parent": self.percent_of_parent,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# filesystem adapters
# ---------------------------------------------------------------------------


class RealFS:
    """Adapter over :mod:`os`, with symlinks never followed.

    Not following symlinks is deliberate: following them double-counts bytes
    that already appear under their real path, and makes a cyclic link an
    infinite descent.

    Paths arriving here are in the package's canonical *display* form, which
    under MSYS2 is POSIX (``/c/Users``).  Each call converts to a form the
    running interpreter can actually open via :func:`paths.resolve_for_os`.
    """

    @staticmethod
    def _os_path(path: str) -> bytes:
        return paths.encode_path(paths.resolve_for_os(path))

    def listdir(self, path: str) -> list[str]:
        with os.scandir(self._os_path(path)) as it:
            return [paths.decode_path(entry.name) for entry in it]

    def stat(self, path: str) -> os.stat_result:
        return os.lstat(self._os_path(path))

    def scandir(self, path: str) -> list[tuple[str, os.stat_result]]:
        out = []
        with os.scandir(self._os_path(path)) as it:
            for entry in it:
                try:
                    out.append((paths.decode_path(entry.name), entry.stat(follow_symlinks=False)))
                except OSError:
                    continue
        return out

    def read(self, path: str) -> bytes:
        with open(self._os_path(path), "rb") as fh:
            return fh.read()

    def hash(self, path: str) -> str:
        return hashing.hash_file(self._os_path(path))


class FakeFS:
    """In-memory tree used by tests.

    Constructed from a nested dict keyed by *absolute* path, or from a root
    path plus a nested dict::

        FakeFS({"/r": {"a": b"data", "sub": {"b": b"more"}}})
        FakeFS({}, root="/r")

    A ``bytes`` leaf is a file; a nested ``dict`` is a directory.  Paths can be
    marked unreadable with :meth:`deny` to exercise the error paths.
    """

    def __init__(self, tree: dict[str, Any] | bytes, root: str | None = None):
        if root is None and isinstance(tree, dict) and len(tree) == 1:
            only = next(iter(tree))
            if only.startswith("/"):
                root, tree = only, tree[only]

        # A non-dict tree means the root path itself is a file, e.g.
        # ``FakeFS({"/r": b"data"})``: it exists, but it is not a directory, so
        # walking it yields nothing.
        self.root_is_file = not isinstance(tree, dict)
        # ``FakeFS({})`` means "nothing exists at all", as opposed to
        # ``FakeFS({}, root="/r")``, which is an existing but empty directory.
        self.exists = self.root_is_file or bool(tree) or root is not None

        self.tree: dict[str, Any] = tree if isinstance(tree, dict) else {}
        self.root = root or "/"
        self._denied: set[str] = set()
        self._sizes: dict[str, int] = {}
        self._index()

    def _stat_root(self) -> _FakeStat:
        """Stat result for the root itself."""
        if self.root_is_file:
            return _FakeStat(0, False)
        return _FakeStat(0, True)

    def _index(self, node: dict[str, Any] | None = None, prefix: str | None = None) -> None:
        if node is None:
            node = self.tree
        if prefix is None:
            prefix = self.root
        for name, value in node.items():
            full = paths.join(prefix, name)
            if isinstance(value, dict):
                self._sizes[full] = 0
                self._index(value, full)
            else:
                self._sizes[full] = len(value)

    def deny(self, path: str) -> None:
        """Make *path* raise ``PermissionError`` on access."""
        self._denied.add(path)

    def _children(self, node: dict[str, Any]) -> dict[str, Any]:
        return node

    def _lookup(self, path: str) -> Any:
        if path in self._denied:
            raise PermissionError(13, "Permission denied", path)
        if path == self.root:
            return self.tree
        relative = self._relative(path)
        node: Any = self.tree
        for part in relative:
            if not isinstance(node, dict) or part not in node:
                raise FileNotFoundError(2, "No such file or directory", path)
            node = node[part]
        return node

    def _relative(self, path: str) -> list[str]:
        """Split *path* into components relative to this fake filesystem's root."""
        if path == self.root:
            return []
        prefix = self.root.rstrip("/") + "/"
        if not path.startswith(prefix):
            raise FileNotFoundError(2, "No such file or directory", path)
        return path[len(prefix) :].split("/")

    def stat(self, path: str) -> Any:
        if path == self.root:
            return self._stat_root()
        value = self._lookup(path)
        return _FakeStat(len(value) if not isinstance(value, dict) else 0, isinstance(value, dict))

    def scandir(self, path: str) -> list[tuple[str, Any]]:
        node = self._lookup(path)
        if not isinstance(node, dict):
            raise NotADirectoryError(20, "Not a directory", path)
        out = []
        for name, value in node.items():
            if isinstance(value, dict):
                out.append((name, _FakeStat(0, True)))
            else:
                out.append((name, _FakeStat(len(value), False)))
        return out

    def read(self, path: str) -> bytes:
        value = self._lookup(path)
        if isinstance(value, dict):
            raise IsADirectoryError(21, "Is a directory", path)
        return value

    def hash(self, path: str) -> str:
        try:
            return hashing.hash_bytes(self.read(path))
        except OSError:
            return hashing.NULL_HASH


@dataclass
class _FakeStat:
    st_size: int
    is_dir: bool


def _classify(st: Any) -> str:
    """Map a stat result onto one of the entry type constants."""
    mode = getattr(st, "st_mode", None)
    if mode is None:
        return DIR if getattr(st, "is_dir", False) else FILE
    if stat.S_ISLNK(mode):
        return LINK
    if stat.S_ISDIR(mode):
        return DIR
    if stat.S_ISREG(mode):
        return FILE
    return OTHER


# ---------------------------------------------------------------------------
# traversal
# ---------------------------------------------------------------------------

ErrorCallback = Callable[[str], None]


def walk(
    root: Any,
    *,
    max_depth: int | None = None,
    hash_contents: bool = True,
    on_error: ErrorCallback | None = None,
) -> Iterator[Entry]:
    """Traverse *root* and yield an :class:`Entry` per object, post-order.

    :param root: a path, or an object exposing ``stat``/``scandir``/``read``/
        ``hash`` (see :class:`RealFS` and :class:`FakeFS`).
    :param max_depth: do not *emit* entries deeper than this, measured from
        ``root`` (``root`` itself is depth 0).  Sizes and hashes of directories
        at the limit still account for their whole subtree, because the
        traversal continues internally; only emission stops.
    :param hash_contents: when false, digests are left as
        :data:`hashing.NULL_HASH`, which turns a content scan into a cheap
        size-only scan.
    :param on_error: called with the path of every object that could not be
        read.  Errors never abort the traversal.

    The root must be a directory; if it is not (or does not exist), nothing is
    yielded.
    """
    adapter = _is_adapter(root)
    fs = root if adapter else RealFS()

    if adapter:
        # An injected filesystem defines its own absolute namespace (FakeFS
        # uses "/r", ...), so it must be used verbatim: translating it through
        # resolve_for_os would rewrite the very paths it is keyed by.
        root_path = str(getattr(root, "root", "") or "")
        display_root = root_path
    else:
        root_path = paths.resolve_for_os(paths.decode_path(root))
        display_root = paths.to_posix(root_path)

    if not root_path:
        return
    if adapter and not getattr(root, "exists", True):
        return

    try:
        st = fs.stat(root_path)
    except OSError:
        return
    if _classify(st) != DIR:
        return

    yield from _descend(
        fs,
        display_root,
        paths.basename(display_root) or display_root,
        None,
        0,
        max_depth,
        hash_contents,
        on_error,
    )


def _is_adapter(root: Any) -> bool:
    """True when *root* looks like a filesystem adapter rather than a path."""
    return not isinstance(root, (str, bytes, os.PathLike))


def _descend(
    fs: Any,
    path: str,
    name: str,
    parent: str | None,
    depth: int,
    max_depth: int | None,
    hash_contents: bool,
    on_error: ErrorCallback | None,
) -> Iterator[Entry]:
    """Recursively measure *path*, yielding children before *path* itself."""
    emit = max_depth is None or depth <= max_depth

    try:
        children = fs.scandir(path)
    except OSError as exc:
        if on_error is not None:
            on_error(path)
        unreadable = Entry(
            path=path,
            name=name,
            parent=parent,
            type=DIR,
            depth=depth,
            size=0,
            sha256=hashing.NULL_HASH,
            child_count=0,
            percent_of_parent=0.0,
            error=str(exc),
        )
        if emit:
            yield unreadable
        return unreadable

    total = 0
    count = 0
    merkle_input: list[tuple[str, str]] = []
    # Children are emitted (and therefore yield-ed) before this directory's
    # total is known, so their percentages are patched afterwards.  The
    # generator protocol makes the two-pass look unavoidable without buffering
    # the whole subtree; holding only one level is the cheaper trade.
    level: list[Entry] = []

    for child_name, child_stat in children:
        child_path = paths.join(path, child_name)
        kind = _classify(child_stat)

        if kind == DIR:
            child = yield from _descend(
                fs,
                child_path,
                child_name,
                path,
                depth + 1,
                max_depth,
                hash_contents,
                on_error,
            )
        elif kind in (FILE, LINK, OTHER):
            child = _measure_leaf(
                fs, child_path, child_name, path, depth + 1, kind, hash_contents, on_error
            )
            if max_depth is None or depth + 1 <= max_depth:
                yield child
        else:  # pragma: no cover - _classify only returns the four constants
            continue

        total += child.size
        count += 1
        level.append(child)
        if hash_contents:
            merkle_input.append((child.name, child.sha256 or hashing.NULL_HASH))

    for child in level:
        child.percent_of_parent = sizes.percentage(child.size, total)

    digest = hashing.merkle_hash(merkle_input) if hash_contents else hashing.NULL_HASH

    self_entry = Entry(
        path=path,
        name=name,
        parent=parent,
        type=DIR,
        depth=depth,
        size=total,
        sha256=digest,
        child_count=count,
        percent_of_parent=0.0,
        error=None,
    )

    if emit:
        yield self_entry
    return self_entry


def _measure_leaf(
    fs: Any,
    path: str,
    name: str,
    parent: str,
    depth: int,
    kind: str,
    hash_contents: bool,
    on_error: ErrorCallback | None,
) -> Entry:
    """Measure a non-directory entry, tolerating unreadable objects.

    Symlinks report size ``0`` deliberately.  On POSIX, ``lstat().st_size`` of a
    symlink is the byte length of the path it *points at* — metadata, not data.
    Reporting it would make a directory's total change when a link's target name
    changed length, and would make the same link appear to consume different
    amounts of space on different systems.  Counting the target's real bytes is
    wrong too, since those already appear under the target's own path.

    This was caught by CI: a 1000-byte file plus a relative symlink to it
    summed to 1126 on Linux, where the link's target string is 126 bytes.
    """
    error: str | None = None
    size = 0

    try:
        st = fs.stat(path)
        if kind != LINK:
            size = int(getattr(st, "st_size", 0) or 0)
    except OSError as exc:
        error = str(exc)
        if on_error is not None:
            on_error(path)

    digest = hashing.NULL_HASH
    if hash_contents and kind == FILE and error is None:
        digest = fs.hash(path)

    return Entry(
        path=path,
        name=name,
        parent=parent,
        type=kind,
        depth=depth,
        size=size,
        sha256=digest,
        child_count=0,
        percent_of_parent=0.0,
        error=error,
    )
