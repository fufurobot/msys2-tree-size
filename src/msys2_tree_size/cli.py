"""Command line entry point.

Design notes:

* :func:`main` takes ``argv`` explicitly and returns an exit code instead of
  calling ``sys.exit`` itself, so it is testable in-process.
* Failures are reported on **stderr** with a non-zero exit code, never as a
  traceback: a missing path or a file passed where a directory was expected is
  a user error, not a crash.
* Human output goes to stdout; diagnostics go to stderr, so ``--json`` can be
  piped without contamination.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from collections.abc import Sequence
from typing import Any

from . import __version__, devices, duplicates, paths, report, sizes, walk

PROG = "msys2-tree-size"

EXIT_OK = 0
EXIT_ERROR = 1


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for every subcommand."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Report where disk space went, and what is duplicated. "
            "Understands MSYS2 paths such as /c/Users and C:\\Users."
        ),
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    du = subparsers.add_parser("du", help="recursive sizes for a directory tree")
    _add_walk_options(du)
    _add_archive_options(du)
    du.add_argument("path", help="directory to scan")
    du.add_argument("--flat", action="store_true", help="flat listing instead of a tree")
    du.add_argument(
        "--unicode",
        action="store_true",
        help="use box-drawing characters (default is ASCII for console safety)",
    )
    du.add_argument("--no-files", action="store_true", help="show directories only")
    du.add_argument("--json", nargs="?", const="-", metavar="FILE", help="write JSON (or '-')")

    dupes = subparsers.add_parser("dupes", help="find duplicate content by hash")
    _add_walk_options(dupes)
    _add_archive_options(dupes)
    dupes.add_argument("path", help="directory to scan")
    dupes.add_argument("--csv", metavar="FILE", help="write a CSV report")
    dupes.add_argument("--json", action="store_true", help="write JSON to stdout")

    dev = subparsers.add_parser("devices", help="list physical devices and partitions")
    dev.add_argument("--json", action="store_true", help="write JSON to stdout")
    dev.add_argument("--by-id-dir", default=devices.BY_ID_DIR, help=argparse.SUPPRESS)
    dev.add_argument("--partitions", default=devices.PARTITIONS_FILE, help=argparse.SUPPRESS)

    return parser


def _add_walk_options(parser: argparse.ArgumentParser) -> None:
    """Add the scan-shaping options shared by ``du`` and ``dupes``."""
    parser.add_argument("--max-depth", type=int, default=None, help="do not descend deeper")
    parser.add_argument("--top", type=int, default=None, help="show only the largest N entries")
    parser.add_argument(
        "--min-size",
        default=None,
        metavar="SIZE",
        help="ignore entries smaller than SIZE (e.g. 10M)",
    )
    parser.add_argument(
        "--no-hash",
        action="store_true",
        help="skip content hashing (much faster, disables duplicate detection)",
    )


def _add_archive_options(parser: argparse.ArgumentParser) -> None:
    """Add the archive-penetration options shared by ``du`` and ``dupes``.

    Penetration is opt-in because opening every archive on a large tree is
    expensive; a scan should be fast unless the user asks for depth.
    """
    parser.add_argument(
        "--archives",
        action="store_true",
        help="also read inside archive files (.tar, .zip, .7z, .rar, .jar, .docx, ...)",
    )
    parser.add_argument(
        "--no-archives",
        action="store_true",
        help="never read inside archives, even if --archives was given",
    )
    parser.add_argument(
        "--archive-depth",
        type=int,
        default=1,
        metavar="N",
        help="how many levels of nested archives to read (default: 1, 0 disables)",
    )
    parser.add_argument(
        "--max-archive-size",
        default=None,
        metavar="SIZE",
        help="skip archives larger than SIZE (e.g. 100M); they are still listed",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI, returning a process exit code."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])

    if not args.command:
        parser.print_usage(sys.stderr)
        print(f"{PROG}: error: a command is required", file=sys.stderr)
        return EXIT_ERROR

    try:
        if args.command == "du":
            return _cmd_du(args)
        if args.command == "dupes":
            return _cmd_dupes(args)
        if args.command == "devices":
            return _cmd_devices(args)
    except ValueError as exc:
        # e.g. a malformed --min-size
        print(f"{PROG}: error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    parser.print_usage(sys.stderr)
    print(f"{PROG}: error: unknown command {args.command!r}", file=sys.stderr)
    return EXIT_ERROR


def _min_size(args: argparse.Namespace) -> int | None:
    """Parse ``--min-size`` into bytes, or ``None`` when unset."""
    if getattr(args, "min_size", None) is None:
        return None
    return sizes.parse_size(args.min_size)


def _scan(args: argparse.Namespace) -> tuple[list[Any], str] | None:
    """Walk ``args.path``, returning entries plus the resolved root.

    Returns ``None`` after printing an error when the path cannot be scanned,
    which keeps the error handling in one place.
    """
    target = paths.to_posix(str(args.path))
    resolved = paths.resolve_for_os(target)

    # Stat through bytes rather than Path: the name may contain lone surrogates
    # from a non-UTF-8 filename, and Path would force it back through the
    # filesystem codec.
    try:
        stat_result = os.stat(paths.encode_path(resolved))  # noqa: PTH116
    except OSError:
        print(f"{PROG}: error: no such path: {args.path}", file=sys.stderr)
        return None
    if not stat.S_ISDIR(stat_result.st_mode):
        print(f"{PROG}: error: not a directory: {args.path}", file=sys.stderr)
        return None

    entries = list(
        walk.walk(
            target,
            max_depth=args.max_depth,
            hash_contents=not getattr(args, "no_hash", False),
            on_error=lambda p: print(f"{PROG}: warning: cannot read {p}", file=sys.stderr),
            **archive_options(args),
        )
    )
    return entries, target


def archive_options(args: argparse.Namespace) -> dict[str, Any]:
    """Translate the archive flags into :func:`walk.walk` keyword arguments.

    ``--no-archives`` is honoured over ``--archives`` so that a user can turn
    penetration off in an alias or wrapper without having to remove the earlier
    flag from the command line.
    """
    enabled = bool(getattr(args, "archives", False)) and not bool(
        getattr(args, "no_archives", False)
    )
    depth = int(getattr(args, "archive_depth", 1) or 0)

    max_size_raw = getattr(args, "max_archive_size", None)
    return {
        "penetrate_archives": enabled,
        "archive_depth": depth,
        "max_archive_size": sizes.parse_size(max_size_raw) if max_size_raw else None,
    }


def _cmd_du(args: argparse.Namespace) -> int:
    scanned = _scan(args)
    if scanned is None:
        return EXIT_ERROR
    entries, root = scanned

    selection = {"top": args.top, "min_size": _min_size(args), "max_depth": args.max_depth}

    if args.json is not None:
        text = report.render_json(entries, indent=2, **selection)
        if args.json == "-":
            sys.stdout.write(text + "\n")
        else:
            paths.write_text(args.json, text)
        return EXIT_OK

    if args.flat:
        sys.stdout.write(report.render_flat(entries, **selection))
    else:
        sys.stdout.write(
            report.render_tree(
                entries, ascii_only=not args.unicode, show_files=not args.no_files, **selection
            )
        )

    summary = report.summarize(entries)
    print(
        f"\n{summary['total_human']} in {summary['file_count']} files, "
        f"{summary['dir_count']} directories  ({root})",
        file=sys.stderr,
    )
    if summary["error_count"]:
        print(
            f"{PROG}: warning: {summary['error_count']} entries could not be read",
            file=sys.stderr,
        )
    return EXIT_OK


def _cmd_dupes(args: argparse.Namespace) -> int:
    scanned = _scan(args)
    if scanned is None:
        return EXIT_ERROR
    entries, root = scanned

    min_size = _min_size(args)
    candidates = entries
    if args.max_depth is not None or min_size is not None:
        candidates = report.select(entries, max_depth=args.max_depth, min_size=min_size)

    groups = duplicates.find_duplicates(candidates)
    rows = duplicates.to_rows(groups)
    summary = duplicates.summarize(candidates)

    if args.json:
        sys.stdout.write(report.render_json([], duplicates=rows, indent=2) + "\n")
        return EXIT_OK

    if not rows:
        print(f"no duplicates found under {root}")
        return EXIT_OK

    if args.csv:
        report.write_duplicates_csv(args.csv, rows)
        print(f"wrote {len(rows)} rows to {args.csv}", file=sys.stderr)

    width = max(len(sizes.human_readable(r.get("size", 0))) for r in rows)
    for row in rows:
        human = f"{sizes.human_readable(row.get('size', 0)):>{width}}"
        cross = " (cross-device)" if row.get("cross_device") else ""
        target = paths.safe_text(str(row.get("path", "")))
        print(f"{human}  {row.get('duplicate_count')}x  {target}{cross}")

    wasted = sizes.human_readable(summary["wasted_bytes"])
    print(
        f"\n{summary['group_count']} duplicate group(s), "
        f"{summary['duplicate_files']} files, {wasted} reclaimable",
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_devices(args: argparse.Namespace) -> int:
    table = devices.inventory(by_id_dir=args.by_id_dir, partitions_file=args.partitions)

    if args.json:
        sys.stdout.write(
            report.json_dumps({"devices": table.rows, "notes": table.notes}, indent=2) + "\n"
        )
        return EXIT_OK

    if not table.rows:
        for note in table.notes or ["no devices found"]:
            print(note)
        return EXIT_OK

    width = max(len(row["id"]) for row in table.rows)
    for row in table.rows:
        kib = row["blocks"]
        human = sizes.human_readable(kib * 1024)
        print(f"{human:>10}  {row['name']:<12}  {row['id']:<{width}}")

    print(f"\n{len(table.rows)} device(s)", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
