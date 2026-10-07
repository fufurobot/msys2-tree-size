"""Archive format detection.

Keeping detection separate from reading matters because the two fail
differently. Detection is a pure decision about a *name*: it must be cheap, must
never raise, and must not open anything. Reading touches real bytes and external
tools, and is allowed to fail per-file.

The rules encode one genuinely subtle distinction:

* **Whole-archive extensions** name a container directly -- ``.zip``, ``.7z``,
  ``.jar``, ``.docx``.
* **Compound extensions** wrap a tar in a compressor -- ``.tar.gz``,
  ``.tgz``, ``.tar.zst``. These must be matched *before* the bare compressor,
  or ``backup.tar.gz`` would be misread as a single gzipped file instead of an
  archive of many files.
* **Single-file compressors** (``.gz``, ``.xz``) genuinely hold one file. They
  are still worth looking inside, but they are not archives in the same sense.

Many unrelated-looking formats are zip containers and are handled by one
implementation: Office Open XML (``.docx``/``.xlsx``/``.pptx``), OpenDocument
(``.odt``/``.ods``/``.odp``), ``.jar``, ``.apk``, ``.epub`` and ``.whl``.
"""

from __future__ import annotations

import posixpath

#: Compound ``.tar.<compressor>`` suffixes, longest first so that ``.tar.gz``
#: wins against a bare ``.gz``.
TAR_COMPOUND: dict[str, str] = {
    ".tar.gz": "gzip",
    ".tar.bz2": "bzip2",
    ".tar.xz": "xz",
    ".tar.zst": "zstd",
    ".tar.lzma": "xz",
    ".tar.lz4": "lz4",
    # Short aliases for the same things.
    ".tgz": "gzip",
    ".tbz": "bzip2",
    ".tbz2": "bzip2",
    ".txz": "xz",
    ".tzst": "zstd",
}

#: Bare compressors that hold exactly one file.
SINGLE_COMPRESSION: dict[str, str] = {
    ".gz": "gzip",
    ".bz2": "bzip2",
    ".xz": "xz",
    ".zst": "zstd",
    ".lzma": "xz",
    ".lz4": "lz4",
    ".z": "compress",
}

#: Names that are zip containers under a domain-specific extension.
ZIP_FAMILY: frozenset[str] = frozenset(
    {
        ".zip",
        ".jar",
        ".war",
        ".ear",
        ".apk",
        ".aar",
        ".epub",
        ".whl",
        ".docx",
        ".xlsx",
        ".pptx",
        ".odt",
        ".ods",
        ".odp",
        ".odg",
        ".xpi",
        ".ipa",
        ".kmz",
        ".cbz",
    }
)

#: Formats with a dedicated reader.
OTHER_CONTAINERS: dict[str, str] = {
    ".7z": "7z",
    ".rar": "rar",
}

#: Formats whose *names* conventionally signal encryption. This is a heuristic
#: for reporting only; the authoritative check inspects the container itself.
ENCRYPTED_HINTS = ("-encrypted", "_encrypted", ".encrypted", "-password")


def detect(name: str) -> str | None:
    """Return the archive kind for *name*, or ``None`` if it is not one.

    Detection is by extension only and never touches the filesystem, so it is
    safe to call on every entry of a large scan. The result is one of
    ``"tar"``, ``"zip"``, ``"7z"``, ``"rar"``, ``"compressed"``, or ``None``.

    A trailing separator is ignored, so a directory called ``foo.zip`` is still
    recognised as such; the caller decides whether it is a file.
    """
    if not name:
        return None

    lowered = name.lower().rstrip("/")
    if not lowered:
        return None

    # Compound suffixes first: ".tar.gz" must beat ".gz".
    for suffix in TAR_COMPOUND:
        if lowered.endswith(suffix):
            return "tar"

    if lowered.endswith(".tar"):
        return "tar"

    extension = _extension(lowered)

    if extension in ZIP_FAMILY:
        return "zip"
    if extension in OTHER_CONTAINERS:
        return OTHER_CONTAINERS[extension]
    if extension in SINGLE_COMPRESSION:
        return "compressed"
    return None


def compression_of(name: str) -> str:
    """Return the compressor used by *name*, or ``"none"``.

    For a compound ``.tar.gz`` this is the inner compressor; for a bare ``.gz``
    it is the compressor itself.
    """
    if not name:
        return "none"

    lowered = name.lower().rstrip("/")
    for suffix, compressor in TAR_COMPOUND.items():
        if lowered.endswith(suffix):
            return compressor

    extension = _extension(lowered)
    if extension in SINGLE_COMPRESSION:
        return SINGLE_COMPRESSION[extension]
    return "none"


def is_archive(name: str) -> bool:
    """True when *name* should be opened as an archive."""
    return detect(name) is not None


def inner_name(name: str) -> str:
    """Name of the single file held by a bare compressed file.

    ``notes.txt.gz`` holds ``notes.txt``. When no suffix is recognised the
    original name is returned unchanged rather than guessing.
    """
    if not name:
        return name

    lowered = name.lower()
    for suffix in SINGLE_COMPRESSION:
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return name


def looks_encrypted_name(name: str) -> bool:
    """Heuristic: does *name* suggest an encrypted payload?

    Used only to annotate output. The authoritative answer comes from inspecting
    the container, because a name is not evidence.
    """
    if not name:
        return False
    lowered = name.lower()

    if any(hint in lowered for hint in ENCRYPTED_HINTS):
        return True
    # "-part1.rar" and similar are how multi-volume encrypted sets are named.
    return "-part" in lowered and lowered.endswith(".rar")


def _extension(lowered_name: str) -> str:
    """Final extension of an already-lowercased name, including the dot.

    A leading dot is not an extension, so ``.bashrc`` has none.
    """
    base = posixpath.basename(lowered_name)
    dot = base.rfind(".")
    if dot <= 0:
        return ""
    return base[dot:]
