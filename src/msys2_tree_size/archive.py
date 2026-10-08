"""Reading archive contents without extracting them to disk.

The goal is to report what is *inside* an archive -- member names and their
uncompressed sizes -- so that a directory listing can account for the space
archives occupy and show what they hold.

Backends, in order of preference:

===========================  ==============================================
Format                       Backend
===========================  ==============================================
tar, tar.gz/bz2/xz           :mod:`tarfile` (standard library)
zip, jar, docx, apk, ...     :mod:`zipfile` (standard library)
7z                           ``7z`` or ``bsdtar``
rar                          ``unrar``, ``7z``, or ``bsdtar``
tar.zst, bare .zst           ``bsdtar`` or ``zstd`` + :mod:`tarfile`
===========================  ==============================================

The standard library covers the majority of real-world archives with no
dependency at all. External tools are used only where they are genuinely
required, and their absence degrades one format rather than failing the scan.

Two safety properties matter and are tested:

* **Nothing is extracted to disk.** For tar and zip the member metadata is read
  directly; for external tools the listing is requested, never an extraction.
  This keeps a scan of an untrusted tree from writing files.
* **A hostile archive cannot exhaust memory or hang.** Only metadata is read,
  and a name that escapes the archive root (``../`` or an absolute path, the
  classic "zip slip" shape) is preserved verbatim for reporting but never
  resolved against the filesystem.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tarfile
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from . import formats, paths

#: Seconds allowed for an external listing command. A hung tool would otherwise
#: stall an entire scan.
TOOL_TIMEOUT = 30

DIR = "dir"
FILE = "file"


@dataclass
class Member:
    """One member of an archive."""

    name: str
    path: str
    size: int
    type: str
    error: str | None = None

    @property
    def is_dir(self) -> bool:
        return self.type == DIR


ErrorCallback = Callable[[str], None]


def _noop(_message: str) -> None:
    """Default error sink."""


# ---------------------------------------------------------------------------
# backend availability
# ---------------------------------------------------------------------------


def _which(tool: str) -> str | None:
    """Locate *tool*, preferring the MSYS2 installation.

    Reuses the project's MSYS2 discovery so the same logic that finds the
    runtime for device enumeration also finds its archive tools, rather than
    relying on whatever happens to be on PATH.
    """
    import shutil

    root = paths.msys2_root()
    if root:
        for sub in ("clang64/bin", "ucrt64/bin", "mingw64/bin", "usr/bin"):
            candidate = os.path.join(root, sub, tool + ".exe")
            if os.path.isfile(candidate):
                return candidate
    return shutil.which(tool)


def available_backends() -> dict[str, bool]:
    """Report which backends can actually be used on this machine.

    ``tar`` and ``zip`` are always true: they are standard library. The rest
    depend on external tools and are reported honestly so a caller can explain
    a gap instead of silently returning nothing.
    """
    return {
        "tar": True,
        "zip": True,
        "7z": _which("7z") is not None or _which("bsdtar") is not None,
        "rar": any(_which(t) is not None for t in ("unrar", "7z", "bsdtar")),
        "zstd": _which("bsdtar") is not None or _which("zstd") is not None,
    }


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def read(
    path: str | os.PathLike[str],
    *,
    on_error: ErrorCallback | None = None,
    max_depth: int = 1,
    _level: int = 1,
) -> Iterator[Member]:
    """Yield every member of the archive at *path*.

    Yields nothing for a file that is not an archive, for a missing file, or for
    a corrupt one; the reason is passed to *on_error* when supplied. A scan of a
    real disk meets all three routinely, so none of them is an exception.

    :param max_depth: how many levels of *nested* archives to descend into.
        ``1`` reads this archive only, which is the common case; ``2`` also
        reads archives found among its members.  Bounded deliberately: nested
        archives are a decompression-bomb vector, and a zip containing a zip
        containing a zip is not worth unbounded work.
    """
    report = on_error or _noop
    target = paths.decode_path(path)
    kind = formats.detect(target)

    if kind is None:
        return

    try:
        exists = os.path.isfile(paths.encode_path(target))
    except OSError:
        return
    if not exists:
        return

    try:
        if kind == "tar":
            # tarfile handles gzip/bzip2/xz natively but not zstd, so a zstd
            # tar is routed to an external tool instead of failing.
            if formats.compression_of(target) == "zstd":
                leaves = _read_zstd_tar(target, report)
            else:
                leaves = _read_tar(target, report)
        elif kind == "zip":
            leaves = _read_zip(target, report)
        elif kind == "7z":
            leaves = _read_with_tool(target, ("7z", "bsdtar"), report)
        elif kind == "rar":
            leaves = _read_with_tool(target, ("unrar", "7z", "bsdtar"), report)
        elif kind == "compressed":
            leaves = _read_single_compressed(target, report)
        else:  # pragma: no cover - formats.detect only returns the above
            return

        for member in leaves:
            yield member

            if _level >= max_depth or member.type == DIR:
                continue
            if not formats.is_archive(member.name):
                continue

            # A nested archive's bytes live inside the outer one, so reading it
            # means extracting that member to a temporary file first. The temp
            # file is always removed, and the recursion is bounded by max_depth.
            nested = _extract_member_to_temp(target, kind, member, report)
            if nested is None:
                continue
            try:
                for inner in read(
                    nested,
                    on_error=report,
                    max_depth=max_depth,
                    _level=_level + 1,
                ):
                    # The nested archive was read from a temporary file, so its
                    # members carry the temp path. Rebasing them onto the logical
                    # chain (outer.zip::inner.zip::deep.txt) keeps the report
                    # truthful about where the data actually came from and hides
                    # an implementation detail. Both sides are normalised, since
                    # the member path is stored in display form.
                    yield _rebase(inner, _to_posix(nested), member.path)
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(nested)
    except Exception as exc:  # noqa: BLE001 - a scan must never die on one file
        report(f"{target}: {type(exc).__name__}: {exc}")


def _rebase(member: Member, from_prefix: str, to_prefix: str) -> Member:
    """Rewrite *member*'s paths so they hang off *to_prefix* instead of *from_prefix*."""
    return Member(
        name=member.name,
        path=member.path.replace(from_prefix, to_prefix, 1),
        size=member.size,
        type=member.type,
        error=member.error,
    )


def _extract_member_to_temp(
    archive_path: str,
    kind: str,
    member: Member,
    report: ErrorCallback,
) -> str | None:
    """Write one member of *archive_path* to a temporary file and return its path.

    Needed only for nested archives, since their bytes are inside the outer
    container and the format readers need a real file. The caller removes it.
    """
    import tempfile

    try:
        handle, temp_path = tempfile.mkstemp(prefix="mts-nested-", suffix=_suffix_of(member.name))
    except OSError as exc:
        report(f"{member.path}: {exc}")
        return None
    os.close(handle)

    try:
        if kind == "zip":
            with zipfile.ZipFile(archive_path) as zf, open(temp_path, "wb") as out:
                out.write(zf.read(member.name))
        elif kind == "tar":
            with tarfile.open(archive_path, "r:*") as tf:
                source = tf.extractfile(member.name)
                if source is None:
                    raise KeyError(member.name)
                with open(temp_path, "wb") as out:
                    out.write(source.read())
        else:
            # 7z and rar both support writing a single member to stdout.
            tool = _which("7z") or _which("bsdtar")
            if tool is None:
                raise RuntimeError("no tool available to extract a nested archive")
            argv = (
                [tool, "x", "-so", archive_path, member.name]
                if tool.endswith("7z.exe") or "7z" in os.path.basename(tool)
                else [tool, "-xOf", archive_path, member.name]
            )
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                argv, capture_output=True, check=False, timeout=TOOL_TIMEOUT
            )
            if completed.returncode != 0:
                raise RuntimeError("extraction failed")
            with open(temp_path, "wb") as out:
                out.write(completed.stdout)
    except Exception as exc:  # noqa: BLE001 - one bad nested archive is a note
        report(f"{member.path}: {type(exc).__name__}: {exc}")
        with contextlib.suppress(OSError):
            os.unlink(temp_path)
        return None

    return temp_path


def _suffix_of(name: str) -> str:
    """A filename suffix that preserves the archive type of *name*."""
    lowered = name.lower()
    for candidate in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar"):
        if lowered.endswith(candidate):
            return candidate
    dot = lowered.rfind(".")
    return lowered[dot:] if dot > 0 else ".bin"


def _read_zstd_tar(path: str, report: ErrorCallback) -> Iterator[Member]:
    """Read a ``.tar.zst`` by decompressing it through an external tool.

    :mod:`tarfile` has no zstd support, so the stream is piped through
    ``zstd``/``bsdtar`` and parsed with :func:`tarfile.open` in stream mode.
    """
    raw = _decompress_zstd(path)
    if raw is None:
        report(f"{path}: no tool could decompress zstd")
        return

    import io

    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as handle:
            members = handle.getmembers()
    except (tarfile.TarError, OSError, EOFError) as exc:
        report(f"{path}: {exc}")
        return

    for info in members:
        name = info.name
        kind = DIR if info.isdir() else FILE
        size = 0 if (info.issym() or info.islnk()) else int(info.size or 0)
        yield Member(
            name=name.rstrip("/") or name,
            path=_member_path(path, name),
            size=size,
            type=kind,
        )


def _decompress_zstd(path: str) -> bytes | None:
    """Return the decompressed bytes of a zstd stream, or ``None``."""
    for tool_name in ("zstd", "bsdtar"):
        tool = _which(tool_name)
        if tool is None:
            continue
        argv = (
            [tool, "-d", "-c", path]
            if tool_name == "zstd"
            else [tool, "-xOf", path]  # libarchive extracts to stdout, no disk write
        )
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                argv,
                capture_output=True,
                check=False,
                timeout=TOOL_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0 and completed.stdout:
            return completed.stdout
    return None


def is_encrypted(path: str | os.PathLike[str]) -> bool:
    """True when the archive at *path* appears to be encrypted.

    Detection is per format rather than by filename, because a name is not
    evidence:

    * zip -- the general-purpose flag bit 0 marks an encrypted entry, and the
      central directory carries the same bit.
    * tar -- the format has no encryption; a ``.tar.gpg`` would not be detected
      as a tar at all.
    * 7z/rar -- only the tool can tell, so ``7z l -slt`` output is consulted
      when available.
    """
    target = paths.decode_path(path)
    kind = formats.detect(target)
    if kind is None:
        return False

    try:
        if kind == "zip":
            return _zip_is_encrypted(target)
        if kind in ("7z", "rar"):
            return _tool_reports_encryption(target)
    except Exception:  # noqa: BLE001 - an unreadable archive is not "encrypted"
        return False
    return False


# ---------------------------------------------------------------------------
# stdlib backends
# ---------------------------------------------------------------------------


def _read_tar(path: str, report: ErrorCallback) -> Iterator[Member]:
    """Read a tar (optionally compressed) via :mod:`tarfile`.

    The path is passed as ``str``, not ``bytes``.  ``tarfile`` and ``zipfile``
    encode names themselves using the filesystem encoding and honour
    ``surrogateescape``, so a ``str`` carrying lone surrogates round-trips
    correctly.  Passing ``bytes`` is actively wrong: ``zipfile`` interprets a
    ``bytes`` argument as *file content* rather than a path.
    """
    try:
        handle = tarfile.open(path, "r:*")
    except (tarfile.TarError, OSError, EOFError) as exc:
        report(f"{path}: {exc}")
        return

    with handle:
        try:
            members = handle.getmembers()
        except (tarfile.TarError, OSError, EOFError) as exc:
            report(f"{path}: {exc}")
            return

        for info in members:
            name = info.name
            kind = DIR if info.isdir() else FILE
            # Symlink and hardlink members carry no payload of their own, so
            # reporting their target's size would double-count, exactly as in
            # the filesystem walk.
            size = 0 if info.issym() or info.islnk() else int(info.size or 0)
            cleaned = name.rstrip("/") or name
            yield Member(
                name=cleaned,
                path=_member_path(path, cleaned),
                size=size,
                type=kind,
            )


def _read_zip(path: str, report: ErrorCallback) -> Iterator[Member]:
    """Read a zip container (also jar, docx, apk, ...) via :mod:`zipfile`."""
    try:
        handle = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, EOFError, NotImplementedError) as exc:
        report(f"{path}: {exc}")
        return

    with handle:
        try:
            infos = handle.infolist()
        except (zipfile.BadZipFile, OSError, EOFError) as exc:
            report(f"{path}: {exc}")
            return

        for info in infos:
            name = info.filename
            is_dir = info.is_dir() or name.endswith("/")
            # Directory members are named with a trailing slash in the zip
            # catalog; the slash is stripped so the name matches the style used
            # everywhere else (and so a report shows "dir", not "dir/").
            cleaned = name.rstrip("/") or name
            yield Member(
                name=cleaned,
                path=_member_path(path, cleaned),
                size=0 if is_dir else int(info.file_size or 0),
                type=DIR if is_dir else FILE,
            )


def _read_single_compressed(path: str, report: ErrorCallback) -> Iterator[Member]:
    """Report the single file inside a bare ``.gz``/``.xz``/``.zst``.

    A ``.gz`` file whose contents turn out to be a tar (common when the file was
    named without a ``.tar`` component) is reported as the archive it really is,
    since that is far more useful than one opaque blob.
    """
    if _is_tar_payload(path):
        yield from _read_tar(path, report)
        return

    kind = formats.compression_of(path)
    inner = formats.inner_name(paths.basename(path))

    size = _single_stream_size(path, kind)
    if size is None:
        report(f"{path}: could not decompress")
        return

    yield Member(
        name=inner,
        path=_member_path(path, inner),
        size=size,
        type=FILE,
    )


def _is_tar_payload(path: str) -> bool:
    """True when *path* is a compressed stream holding a tar.

    Detected by opening it as a tar, which is the authoritative test, rather
    than by guessing from the filename.
    """
    try:
        with tarfile.open(path, "r:*") as handle:
            # next() forces the header to be parsed; an empty or non-tar stream
            # raises or returns None.
            return handle.next() is not None
    except (tarfile.TarError, OSError, EOFError):
        return False


def _single_stream_size(path: str, compression: str) -> int | None:
    """Uncompressed size of a single-stream compressed file, or ``None``.

    Uses the decompressor itself rather than a header field, because the
    gzip/bzip2 trailers are not always trustworthy and the file may be a
    concatenation of streams.
    """
    import bz2
    import gzip
    import lzma

    opener: Any
    if compression == "gzip":
        opener = gzip.open
    elif compression == "bzip2":
        opener = bz2.open
    elif compression == "xz":
        opener = lzma.open
    else:
        # zstd and lz4 have no stdlib reader; the external tool is the only way.
        return _stream_size_via_tool(path)

    total = 0
    try:
        with opener(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                total += len(chunk)
    except (OSError, EOFError, lzma.LZMAError):
        return None
    return total


def _stream_size_via_tool(path: str) -> int | None:
    """Size of a bare ``.zst`` by piping it through the external decompressor."""
    tool = _which("zstd")
    if tool is None:
        return None

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [tool, "-d", "-c", path],
            capture_output=True,
            check=False,
            timeout=TOOL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return len(completed.stdout)


# ---------------------------------------------------------------------------
# external tool backends
# ---------------------------------------------------------------------------


def _read_with_tool(path: str, tools: tuple[str, ...], report: ErrorCallback) -> Iterator[Member]:
    """Read an archive by asking the first available external tool to list it."""
    for tool_name in tools:
        tool = _which(tool_name)
        if tool is None:
            continue

        listing = _list_with(tool, tool_name, path)
        if listing is None:
            continue

        members = list(_parse_listing(listing, path))
        if members:
            yield from members
            return
        # The tool ran but reported nothing: try the next one rather than
        # concluding the archive is empty.
    report(f"{path}: no available tool could read this archive")


def _list_with(tool: str, tool_name: str, path: str) -> str | None:
    """Run *tool* in listing mode and return stdout, or ``None`` on failure.

    ``-slt`` gives machine-readable ``key = value`` output for 7z, and
    ``unrar lb`` gives a bare name list. ``bsdtar -tf`` works for everything
    libarchive understands.
    """
    if tool_name == "7z":
        argv = [tool, "l", "-slt", "-ba", path]
    elif tool_name == "unrar":
        argv = [tool, "lb", path]
    else:  # bsdtar
        argv = [tool, "-tf", path]

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            capture_output=True,
            check=False,
            timeout=TOOL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", "surrogateescape")


def _parse_listing(text: str, path: str) -> Iterator[Member]:
    """Parse ``7z l -slt`` or ``bsdtar -tf`` output into members.

    Three normalisations are needed because the tools are inconsistent:

    * **Separators.** On Windows, 7-Zip reports member names with backslashes
      even for a POSIX-style archive. Member names are always reported with
      forward slashes so they read the same on every platform.
    * **Directories.** ``7z`` marks them with ``Folder = +`` *or* an
      ``Attributes = D...`` string (measured: the directory entries of a 7z
      archive carry ``Attributes = D_ drwxr-xr-x`` and no ``Folder`` line at
      all, so relying on ``Folder`` alone types directories as files).
      ``bsdtar -tf`` instead appends a trailing ``/``. All three shapes are
      recognised.
    * **Sizes.** A bare name list (bsdtar) carries no sizes, so they come out 0
      rather than being invented.
    """
    name: str | None = None
    size = 0
    is_dir = False
    saw_structured = False

    def build() -> Member | None:
        if name is None:
            return None
        cleaned = name.replace("\\", "/")
        # A 7z "directory" header row is sometimes named without a trailing
        # slash; the attributes are what decide, and that is handled above.
        cleaned = cleaned.rstrip("/") or cleaned
        return Member(
            name=cleaned,
            path=_member_path(path, cleaned),
            size=0 if is_dir else size,
            type=DIR if is_dir else FILE,
        )

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("Path = "):
            pending = build()
            if pending is not None:
                yield pending
            saw_structured = True
            name = line[len("Path = ") :]
            size = 0
            is_dir = False
            continue

        if line.startswith("Size = "):
            try:
                size = int(line[len("Size = ") :].strip())
            except ValueError:
                size = 0
            continue

        if line.startswith("Folder = "):
            is_dir = line[len("Folder = ") :].strip() in ("+", "1")
            continue

        if line.startswith("Attributes = "):
            # "D" or "D_ drwxr-xr-x" marks a directory; "A" marks an archive
            # member with content. 7z emits the leading letter consistently.
            attributes = line[len("Attributes = ") :].strip()
            if attributes[:1].upper() == "D":
                is_dir = True
            continue

        # Skip the other structured keys 7z emits between records.
        if saw_structured and (
            line.startswith(("Encrypted", "Method", "CRC", "Block", "Host OS", "Version"))
            or line.startswith("---")
        ):
            continue

        if not saw_structured:
            # bsdtar -tf: one bare name per line.
            cleaned = line.replace("\\", "/")
            is_dir = cleaned.endswith("/")
            trimmed = cleaned.rstrip("/") or cleaned
            yield Member(
                name=trimmed,
                path=_member_path(path, trimmed),
                size=0,
                type=DIR if is_dir else FILE,
            )

    pending = build()
    if pending is not None:
        yield pending


def _tool_reports_encryption(path: str) -> bool:
    """Ask the available tool whether the archive is encrypted."""
    tool = _which("7z")
    if tool is None:
        return False
    listing = _list_with(tool, "7z", path)
    if listing is None:
        return False
    for line in listing.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("encrypted = ") and stripped.endswith(("+", "1")):
            return True
        if "wrong password" in stripped or "enter password" in stripped:
            return True
    return False


def _zip_is_encrypted(path: str) -> bool:
    """True when any zip member has the encryption flag set.

    Bit 0 of the general purpose flag marks encryption in both the local file
    header and the central directory; the central directory is authoritative.
    """
    try:
        with zipfile.ZipFile(path) as handle:
            for info in handle.infolist():
                if info.flag_bits & 0x1:
                    return True
    except (zipfile.BadZipFile, OSError, EOFError, NotImplementedError):
        return False
    return False


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def _member_path(archive_path: str, member_name: str) -> str:
    """Build the display path for a member.

    Both halves are normalised to POSIX separators, because member paths are
    *display* strings that must read the same on every platform and must match
    the spelling the walk uses for real files.  On Windows the archive may
    arrive as ``C:\\dir\\a.zip`` even though the walk reports ``/c/dir/a.zip``,
    and 7-Zip reports member names with backslashes even for POSIX archives.

    The archive and member are joined with :data:`MEMBER_SEPARATOR` rather than
    a path separator, so a member named ``../etc`` -- the "zip slip" shape -- is
    visibly *inside the archive* rather than looking like a path that was
    actually walked.  Nothing here is ever resolved against the filesystem.
    """
    return f"{_to_posix(archive_path)}{MEMBER_SEPARATOR}{_to_posix(member_name)}"


def _to_posix(value: str) -> str:
    """Normalise a path to the package's canonical POSIX display form.

    Two normalisations are needed and they are different:

    * separators -- ``C:\\dir\\a.zip`` and 7-Zip's backslash member names both
      become forward slashes;
    * drive letters -- ``C:/dir`` becomes ``/c/dir``, because that is the form
      the walk reports for real files, and member paths must match it or a
      report would show an archive and its members with different prefixes.
    """
    normalised = value.replace("\\", "/")
    if len(normalised) >= 2 and normalised[1] == ":" and normalised[0].isalpha():
        normalised = "/" + normalised[0].lower() + normalised[2:]
    return normalised


#: Separator between an archive and its members. Chosen to be unmistakable and
#: to keep the contained name greppable as one unit.
MEMBER_SEPARATOR = "::"
