# msys2-tree-size

A CLI re-implementation of [TreeSize](https://www.jam-software.com/treesize_free)
for **MSYS2**, because GUIs are not scriptable and `du` does not tell you *why* a
directory is large.

The project exists to answer three questions from a terminal:

1. **Where did my disk space go?** — recursive sizes, sorted, with a tree view.
2. **What is duplicated?** — content hashing (SHA-256) across files.
3. **Which physical device is it on?** — joins `/dev/disk/by-id` against
   `/proc/partitions` so results are attributed to a real drive.

## Install and use

```bash
uv tool install msys2-tree-size     # gives you `msys2-tree-size` and `mts`
msys2-tree-size du /c/Users         # where did the space go
msys2-tree-size dupes /c/Users      # what is duplicated
msys2-tree-size devices             # which physical disk
msys2-tree-size diagnose            # what works on this machine
```

**See [`docs/install.md`](docs/install.md) for the full guide** — every option,
the archive formats and how to enable them, and the one thing that only works
inside an MSYS2 shell.

## Why MSYS2 specifically

MSYS2 exposes POSIX semantics that plain Win32 Python does not:

| Feature | Why it matters here |
| --- | --- |
| `/dev/disk/by-id/*` | Stable, hardware-derived device names instead of `C:`/`D:` |
| `/proc/partitions` | Partitions in KiB, including ones with no drive letter, plus the `win-mounts` column |
| POSIX filenames | Names are arbitrary bytes, not UTF-16 |

### Two traps worth knowing about

**MSYS2's Python is a native Windows build.** `sys.platform` is `"win32"`, not
`"msys"`, and such an interpreter cannot open `/c/Users`, `/dev/disk/by-id` or
`/proc/partitions` at all — those are *virtual* paths that the MSYS2 runtime
resolves only for MSYS2 binaries. This tool therefore detects the MSYS2
installation by probing for it and reaches the virtual paths through
`msys2_shell.cmd`. It does **not** hardcode `C:\msys64`: portable and per-user
installs are common, and a hardcoded path would silently disable device
reporting for everyone whose install lives elsewhere.

**POSIX filenames are arbitrary bytes, not text.** Python decodes undecodable
bytes into *lone surrogates* (`U+DC80`–`U+DCFF`) via the `surrogateescape`
handler. Anything that re-encodes such a string to UTF-8 — JSON, CSV, and
notably `pyarrow`-backed pandas string dtypes — raises `UnicodeEncodeError`.
This project keeps paths byte-exact through `paths.py`. See
[`docs/design.md`](docs/design.md) for the mechanism and the measurements behind
both points.

## Installing from a checkout

```bash
uv run msys2-tree-size --help      # run without installing
uv pip install -e .                # or install the console script
```

## Usage

```bash
# size of a directory tree, largest first
msys2-tree-size du /c/Users

# only the top 20 entries, at most 2 levels deep
msys2-tree-size du /c/Users --depth 2 --top 20

# look inside archives too, and account for what they hold
msys2-tree-size du /c/Users --archives

# machine-readable
msys2-tree-size du /c/Users --json tree.json

# find duplicate files by content hash
msys2-tree-size dupes /c/Users --min-size 1M

# enumerate physical devices
msys2-tree-size devices
```

## Looking inside archives

A directory full of tarballs reports a few megabytes and hides hundreds.
`--archives` opens them and adds their contents to the totals:

```console
$ msys2-tree-size du backup --flat
273B    4.13%  file  backup/bundle.zip
206B    3.11%  file  backup/data.tar.gz

$ msys2-tree-size du backup --flat --archives
273B    1.49%  file  backup/bundle.zip
   2.6K  14.74%  file  backup/bundle.zip::payload/nested/b.bin
   1.2K   6.55%  file  backup/bundle.zip::payload/a.txt
206B    1.12%  file  backup/data.tar.gz
   2.6K  14.74%  file  backup/data.tar.gz::payload/nested/b.bin
```

Members are shown as `archive::member`. They are **display strings, never real
paths** — nothing is extracted to disk, and a hostile member name such as
`../etc/passwd` is shown verbatim inside the container rather than resolved.

Supported with no dependency at all: `tar`, `tar.gz`, `tar.bz2`, `tar.xz`,
`zip`, `jar`, `apk`, `docx`, `xlsx`, `pptx`, `odt`/`ods`/`odp`, `epub`, `whl`.
With `7z`, `unrar`, `bsdtar` or `zstd` present as well: `7z`, `rar`, `tar.zst`
and bare `.gz`/`.xz`/`.zst`/`.bz2`. A missing tool costs exactly one format.

Penetration is **opt-in**, because opening every archive on a large tree is
expensive. Three independent limits bound the work:

| Flag | Effect |
| --- | --- |
| `--archives` | enable reading inside archives |
| `--no-archives` | force it off, overriding `--archives` |
| `--archive-depth N` | levels of *nested* archives to read (default 1, `0` disables) |
| `--max-archive-size SIZE` | skip archives larger than `SIZE`; still listed |

## Layout

```
src/msys2_tree_size/
  __init__.py      public API surface
  paths.py         byte-exact path <-> str helpers (surrogateescape)
  sizes.py         human-readable formatting and size aggregation
  hashing.py       content hashing + subtree (Merkle) hashing
  formats.py       archive format detection by name
  archive.py       archive readers (tar/zip stdlib, 7z/rar/zstd via tools)
  walk.py          the single filesystem traversal used by every command
  report.py        tree / flat / json renderers
  duplicates.py    duplicate grouping, local and cross-device
  devices.py       /dev/disk/by-id + /proc/partitions inventory
  cli.py           argparse entry point
drivev3.py         the original prototype this package was extracted from
```

## Development

```bash
# tests (run from the repository root)
uv run python -m unittest discover -s tests -t . -v

# lint and format
uvx ruff check src tests
uvx ruff format --check src tests
```

Test scratch space lives under `.tmp/` inside the checkout and is git-ignored, so
a run leaves the working tree clean.

CI runs the suite on `ubuntu-latest` **and** inside real MSYS2 (`UCRT64`,
`CLANG64`, `MINGW64`), because the POSIX path behaviour cannot be faithfully
emulated on Linux. The MSYS2 jobs print `sys.platform` and `uname` first, then
run the CLI against the runner's real device tree.

## License

AGPL-3.0-or-later. See [`LICENSE`](LICENSE).
