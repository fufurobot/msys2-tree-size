# msys2-tree-size

A CLI re-implementation of [TreeSize](https://www.jam-software.com/treesize_free)
for **MSYS2**, because GUIs are not scriptable and `du` does not tell you *why* a
directory is large.

The project exists to answer three questions from a terminal:

1. **Where did my disk space go?** — recursive sizes, sorted, with a tree/treemap view.
2. **What is duplicated?** — content hashing (SHA-256) across files and whole
   subtrees, including cross-device duplicates.
3. **Which physical device is it on?** — joins `/dev/disk/by-id` against
   `/proc/partitions` so results are attributed to a real drive.

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

## Install

```bash
# from a checkout
uv run msys2-tree-size --help

# or install the console script
uv pip install -e .
msys2-tree-size --help
```

## Usage

```bash
# size of a directory tree, largest first
msys2-tree-size du /c/Users

# only the top 20 entries, at most 2 levels deep
msys2-tree-size du /c/Users --depth 2 --top 20

# machine-readable
msys2-tree-size du /c/Users --json tree.json

# find duplicate files by content hash
msys2-tree-size dupes /c/Users --min-size 1M

# enumerate physical devices
msys2-tree-size devices
```

## Layout

```
src/msys2_tree_size/
  __init__.py      public API surface
  paths.py         byte-exact path <-> str helpers (surrogateescape)
  sizes.py         human-readable formatting and size aggregation
  hashing.py       content hashing + subtree (Merkle) hashing
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
