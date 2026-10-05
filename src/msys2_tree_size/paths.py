"""Byte-exact path handling.

POSIX filenames are arbitrary byte sequences (excluding ``NUL`` and ``/``).
Python's ``os`` layer decodes them with the filesystem encoding using the
``surrogateescape`` error handler, which maps undecodable bytes to *lone
surrogates* in ``U+DC80``..``U+DCFF``.

The round-trip ``bytes -> str -> bytes`` is lossless **only** if no intermediate
layer re-encodes the string.  Several do, and they fail in different ways:

===================  ==========================================================
``open(errors=...)`` fine, as long as ``surrogateescape`` is requested
``json``             emits ``\\udcXX`` escapes; readable back, but not by a
                     consumer that will not re-decode surrogates
``csv``              depends entirely on the file object's error handler
``pyarrow``          raises ``UnicodeEncodeError`` outright
===================  ==========================================================

This module is the single place that knows about that.  Every writer in the
package funnels through here so the decision is made once.

Design rules enforced here:

* ``decode_path`` / ``encode_path`` are the only sanctioned conversions.
* Text is written as **bytes** through ``encode_path``, never re-encoded.
* JSON uses ``ensure_ascii=True`` so a surrogate becomes the escape
  ``\\udcff``, which survives a round-trip through a strict-UTF-8 reader.
"""

from __future__ import annotations

import json
import os
import posixpath
import shutil
import sys
from collections.abc import Iterable, Sequence
from typing import IO, Any

ENCODING = "utf-8"
ERRORS = "surrogateescape"

#: Extensions are reported under this key when a file has none.
NO_EXTENSION = "<none>"


def decode_path(value: bytes | str | os.PathLike[str]) -> str:
    """Decode *value* to ``str``, preserving undecodable bytes as surrogates.

    ``str`` and path-like inputs are returned / converted unchanged, so this is
    safe to call on values of unknown provenance.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode(ENCODING, ERRORS)
    return os.fspath(value)


def encode_path(value: str | bytes | os.PathLike[str]) -> bytes:
    """Encode *value* back to the original bytes.

    Lone surrogates introduced by :func:`decode_path` are turned back into the
    bytes they came from.  ``bytes`` is passed straight through.

    ``os.PathLike`` inputs go through ``os.fspath`` first.  On Windows,
    ``Path("/tmp/x")`` renders as ``\\tmp\\x``; that leading backslash is a
    platform rendering artifact, so :func:`normalize_separators` restores the
    POSIX form rather than letting it change which path is opened.

    This function does **not** apply byte-exactness to path *semantics*; use
    :func:`resolve_for_os` when the goal is to open the path.
    """
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode(ENCODING, ERRORS)
    return normalize_separators(os.fspath(value)).encode(ENCODING, ERRORS)


def normalize_separators(value: str) -> str:
    """Convert Win32 path separators in *value* to POSIX ones, safely.

    Backslashes are only rewritten when the string is Windows-style: a drive
    path (``C:\\x``), a UNC path (``\\\\server\\share``), or an absolute path
    (``\\tmp\\x``).  A relative path such as ``a\\b`` is left alone, because on
    POSIX a backslash is a legal character in a filename and rewriting it would
    change which file is meant.
    """
    if "\\" not in value:
        return value
    if _has_drive(value):
        return to_posix(value)
    if value.startswith("\\\\") or value.startswith("//"):
        return value
    if value.startswith("\\"):
        return value.replace("\\", "/")
    return value


def _has_drive(value: str) -> bool:
    """True when *value* begins with a ``C:`` style drive designator."""
    return len(value) >= 2 and value[1] == ":"


def safe_text(value: str) -> str:
    """Return *value* as text that is guaranteed to encode as strict UTF-8.

    Lone surrogates are replaced by their ``\\udcXX`` escape.  This is for
    *display only*: use :func:`write_text` when the bytes must be preserved.
    """
    if value.isascii():
        return value
    out = []
    for ch in value:
        code = ord(ch)
        if 0xDC80 <= code <= 0xDCFF:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# writers
# ---------------------------------------------------------------------------


def write_text(path: str | os.PathLike[str], text: str) -> None:
    """Write *text* to *path* with byte-exact round-tripping.

    The string is encoded through :func:`encode_path`, so writing what
    :func:`decode_path` produced restores the original bytes.
    """
    with open(path, "wb") as fh:
        fh.write(encode_path(text))


def write_json(path: str | os.PathLike[str], obj: Any, indent: int | None = None) -> None:
    """Write *obj* as JSON that is safe for paths containing surrogates.

    ``ensure_ascii=True`` is deliberate: it turns a lone surrogate into the
    escape ``\\udcff``, which any strict-UTF-8 reader can at least parse.
    """
    text = json.dumps(obj, ensure_ascii=True, indent=indent, sort_keys=False)
    write_text(path, text)


def _csv_escape(field: str) -> str:
    """Quote *field* per RFC 4180 if it contains a comma, quote or newline."""
    if any(ch in field for ch in (",", '"', "\n", "\r")):
        return '"' + field.replace('"', '""') + '"'
    return field


def write_csv(
    path: str | os.PathLike[str],
    header: Sequence[str],
    rows: Iterable[Sequence[Any]],
) -> None:
    """Write a CSV file with byte-exact fields.

    Implemented directly rather than through :mod:`csv` so the output is built
    as one byte string via :func:`encode_path`; the stdlib writer would need a
    correctly-configured file object to do the same, and silently corrupts data
    otherwise.

    Fields keep their original bytes, so a path containing an undecodable byte
    round-trips.  Quoting follows RFC 4180 so the file stays parseable by
    ordinary CSV readers.
    """
    lines = [",".join(_csv_escape(col) for col in header)]
    for row in rows:
        cells = []
        for cell in row:
            text = cell if isinstance(cell, str) else str(cell)
            cells.append(_csv_escape(text))
        lines.append(",".join(cells))
    with open(path, "wb") as fh:
        fh.write(encode_path("\n".join(lines) + "\n"))


def open_text(path: str | os.PathLike[str], mode: str = "r") -> IO[str]:
    """Open *path* as text with the surrogateescape error handler."""
    return open(path, mode, encoding=ENCODING, errors=ERRORS, newline="")


# ---------------------------------------------------------------------------
# path manipulation that must not lose surrogates
# ---------------------------------------------------------------------------


def to_posix(value: str) -> str:
    """Convert a Windows-style path to its MSYS2 ``/c/...`` equivalent.

    MSYS2 accepts both ``C:\\Users`` and ``/c/Users`` for the same directory.
    Arguments arriving from a ``cmd.exe`` prompt are Win32-shaped, so this
    normalises them for display.

    The result is only *usable by the filesystem inside MSYS2*: native Windows
    Python has no ``/c`` mount point and cannot open ``/c/Users``.  Callers that
    intend to open the path must therefore guard on :func:`is_msys2` — see
    :func:`resolve_for_os`, which does exactly that.

    UNC paths (``\\\\server\\share``) have no sensible ``/x/`` mapping and are
    returned unchanged rather than being mangled.
    """
    if not value:
        return value
    if value.startswith("\\\\") or value.startswith("//"):
        return value
    if _has_drive(value):
        drive = value[0].lower()
        rest = value[2:].replace("\\", "/")
        if not rest.startswith("/"):
            rest = "/" + rest
        return "/" + drive + rest
    return value


def is_msys2() -> bool:
    """True when the *interpreter itself* understands MSYS2 POSIX paths.

    This is narrower than "the machine has MSYS2 installed".  Measured on a real
    MSYS2 CLANG64 install: ``sys.platform`` is ``"win32"``, because MSYS2 ships
    a *native Windows* Python build.  Such an interpreter cannot open
    ``/c/Users`` or ``/proc/partitions`` at all — those paths are virtual and
    are resolved by the MSYS2 runtime for MSYS2 *binaries*, never for an
    arbitrary Win32 process.

    So this is True only for a genuine Cygwin/MSYS runtime Python, which does
    provide the ``/c`` mounts.  Code needing MSYS2's virtual filesystems from a
    Win32 interpreter must shell out to an MSYS2 binary; see
    :func:`msys2_tree_size.devices.has_msys2_tools`.
    """
    return sys.platform in ("msys", "cygwin")


def msys2_root() -> str | None:
    """Locate an MSYS2 installation on this machine, or ``None``.

    Detection deliberately does not hardcode ``C:\\msys64``.  MSYS2 is routinely
    installed somewhere else (a portable extract, a per-user directory, a
    scoop/chocolatey prefix), and this project's own development machine has it
    under ``Downloads``.  Hardcoding one location would silently disable device
    reporting for everyone else, so the search order is:

    1. ``MSYS2_ROOT`` / ``MSYS2_DIR``, when the user has set them.
    2. The conventional prefixes, as a cheap first guess.
    3. **The PATH**: any ``bash``/``ls``/``cat`` already on PATH reveals an
       installation, because the MSYS2 layout is ``<root>/usr/bin/tool.exe`` or
       ``<root>/<env>/bin/tool.exe``.
    4. A recursive scan of a few likely parent directories, bounded in depth.

    Returns ``None`` when nothing is found, which callers treat as "MSYS2 is not
    installed" rather than an error.
    """
    candidates: list[str] = []
    for variable in ("MSYS2_ROOT", "MSYS2_DIR"):
        value = os.environ.get(variable)
        if value:
            candidates.append(value)

    candidates += [r"C:\msys64", r"C:\msys32", os.path.expanduser(r"~\msys64")]

    # A tool already on PATH reveals its installation root.
    for tool in ("bash", "ls", "cat"):
        resolved = shutil.which(tool)
        if resolved:
            directory = os.path.dirname(resolved)
            candidates.append(os.path.dirname(directory))
            candidates.append(os.path.dirname(os.path.dirname(directory)))

    # Bounded scan of plausible parents, for non-standard installs that are not
    # on PATH either.  Depth is capped so this stays cheap.
    for parent in (os.path.expanduser("~"), os.path.expanduser("~/Downloads"), "C:\\"):
        candidates.extend(_scan_for_msys2(parent, max_depth=2))

    for candidate in candidates:
        if _looks_like_msys2(candidate):
            return candidate
    return None


def _looks_like_msys2(path: str) -> bool:
    """True when *path* is the root of an MSYS2 installation."""
    if not path or not os.path.isdir(path):
        return False
    if os.path.isfile(os.path.join(path, "msys2_shell.cmd")):
        return True
    # A bare runtime is also usable (MSYS2 >= 3.x always ships usr/bin).
    return os.path.isdir(os.path.join(path, "usr", "bin"))


def _scan_for_msys2(parent: str, max_depth: int = 2) -> list[str]:
    """Find MSYS2 roots under *parent*, descending at most *max_depth* levels."""
    found: list[str] = []
    if not os.path.isdir(parent):
        return found

    base_depth = parent.rstrip("\\/").count(os.sep)
    for root, dirs, _files in os.walk(parent, topdown=True):
        if root.rstrip("\\/").count(os.sep) - base_depth >= max_depth:
            dirs[:] = []
        # Skip subtrees that cannot contain an installation root.
        dirs[:] = [
            d
            for d in dirs
            if d.lower().startswith("msys") or d.lower() in ("usr", "mingw64", "clang64", "ucrt64")
        ]
        if _looks_like_msys2(root):
            found.append(root)
    return found


def resolve_for_os(value: str) -> str:
    """Return *value* in the form the local filesystem can actually open.

    Under a real MSYS/Cygwin Python the canonical POSIX form (``/c/Users``)
    resolves natively and is kept.  Under a native Windows interpreter it does
    not, so a drive path is converted back to ``C:\\Users``.

    MSYS2's *virtual* paths (``/dev/...``, ``/proc/...``) have no Win32
    equivalent and are deliberately left untouched; reaching them needs an
    MSYS2 binary, not a path rewrite.
    """
    if is_msys2():
        return to_posix(value)
    return from_posix(value)


def from_posix(value: str) -> str:
    """Convert an MSYS2 ``/c/...`` path back to a Windows ``C:\\...`` path.

    Anything that is not an MSYS2 drive path is returned unchanged, so POSIX
    paths on a genuine POSIX system are untouched.
    """
    if len(value) >= 3 and value[0] == "/" and value[1].isalpha() and value[2] == "/":
        return value[1].upper() + ":\\" + value[3:].replace("/", "\\")
    return value


def join(base: str, *parts: str) -> str:
    """Join path components, preferring POSIX separators."""
    result = base
    for part in parts:
        if not result:
            result = part
        elif result.endswith("/"):
            result = result + part
        else:
            result = result + "/" + part
    return result


def basename(value: str) -> str:
    """Final component of *value*, ignoring a trailing separator."""
    stripped = value.rstrip("/") or "/"
    if stripped == "/":
        return "/"
    return posixpath.basename(stripped)


def parent(value: str) -> str | None:
    """Parent directory of *value*, or ``None`` if it is a root."""
    stripped = value.rstrip("/") or "/"
    if stripped == "/" or (len(stripped) == 2 and stripped[1] == ":"):
        return None
    head = posixpath.dirname(stripped)
    return head or None


def extension(name: str) -> str:
    """Lowercased extension of *name*, including the dot, or ``<none>``.

    A leading dot does not count as an extension: ``.bashrc`` is a dotfile, not
    a file with extension ``.bashrc``.
    """
    base = posixpath.basename(name)
    dot = base.rfind(".")
    if dot <= 0 or dot == len(base) - 1:
        return NO_EXTENSION
    return base[dot:].lower()
