"""Content hashing and Merkle subtree hashing.

Two kinds of digest are produced:

* **Content hash** (``hash_file``) — SHA-256 of a file's bytes, streamed so
  large files do not need to fit in memory.  Two files with equal content
  hashes are byte-identical.
* **Subtree hash** (``merkle_hash``) — SHA-256 over the sorted list of
  ``(child name, child digest)`` pairs.  A directory whose subtree hash equals
  a child's hash adds no new content, which is what makes the dedupe rule in
  :mod:`msys2_tree_size.duplicates` sound.

The child *name* is hashed alongside the digest, separated by a NUL.  Without
that, ``[("ab", h1), ("c", h2)]`` and ``[("a", h1), ("bc", h2)]`` would
concatenate to the same byte stream and collide.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Sequence

#: Read size for streaming.  1 MiB balances syscall overhead against memory.
CHUNK_SIZE = 1024 * 1024

#: Sentinel meaning "not hashed", e.g. when hashing is disabled.  Deliberately
#: the empty string so that it can never collide with a 64-char hex digest.
NULL_HASH = ""


def hash_bytes(data: bytes) -> str:
    """Return the SHA-256 hex digest of *data*."""
    return hashlib.sha256(data).hexdigest()


def hash_file(path: os.PathLike[str] | str) -> str:
    """Return the SHA-256 hex digest of the file at *path*.

    Returns :data:`NULL_HASH` for an unreadable path or a directory, so a single
    bad file degrades one row instead of aborting the traversal.
    """
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
                digest.update(chunk)
    except (OSError, ValueError):
        return NULL_HASH
    return digest.hexdigest()


def merkle_hash(children: Iterable[tuple[str, str]]) -> str:
    """Combine ``(name, digest)`` pairs into one subtree digest.

    Order-independent by construction (the input is sorted), so filesystem
    iteration order cannot change the result.  Children with a null digest are
    still included, keyed by name, so an unreadable file changes its parent's
    hash rather than silently vanishing from it.
    """
    # Each child becomes ONE length-prefixed record, and the records are
    # sorted.  Sorting whole records (not individual fields) is what makes
    # [("ab", X), ("c", Y)] differ from [("a", X), ("bc", Y)]: the former sorts
    # as two records, the latter as two different ones.
    records: list[bytes] = []
    for name, digest in children:
        name_bytes = name.encode("utf-8", "surrogateescape")
        digest_bytes = (digest or NULL_HASH).encode("ascii", "replace")
        record = (
            len(name_bytes).to_bytes(8, "big")
            + name_bytes
            + len(digest_bytes).to_bytes(8, "big")
            + digest_bytes
        )
        records.append(record)
    records.sort()

    digest_obj = hashlib.sha256()
    for record in records:
        digest_obj.update(len(record).to_bytes(8, "big"))
        digest_obj.update(record)
    return digest_obj.hexdigest()


def is_null(digest: str | None) -> bool:
    """True when *digest* does not represent a real hash."""
    return not digest


def group_by_digest(rows: Sequence[object], key: str = "sha256") -> dict[str, list[object]]:
    """Group *rows* by digest, dropping null digests.

    Accepts mappings or objects.  Rows whose digest is null are omitted so that
    unreadable files are never reported as duplicates of one another.
    """
    groups: dict[str, list[object]] = {}
    for row in rows:
        digest = row.get(key) if isinstance(row, dict) else getattr(row, key, None)
        if is_null(digest):
            continue
        groups.setdefault(digest, []).append(row)
    return groups
