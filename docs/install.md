# msys2-tree-size — install and usage

A command-line replacement for TreeSize. It answers three questions:

1. **Where did my disk space go?** — recursive sizes, largest first.
2. **What is duplicated?** — content hashing, across directories.
3. **Which physical disk is it on?** — real device names, not drive letters.

Requires **MSYS2** on Windows, or any POSIX system (Linux, macOS, WSL). See
[Why MSYS2 matters](#why-msys2-matters) for the one thing that is Windows-specific.

## Install

```bash
uv tool install msys2-tree-size
```

That gives you two equivalent commands: `msys2-tree-size` and the short `mts`.

If you do not have `uv`, either install it first
(`winget install astral-sh.uv`, or `pacman -S mingw-w64-clang-x86_64-uv` inside
MSYS2), or use pip instead:

```bash
pip install msys2-tree-size
```

### Verify the install

```bash
msys2-tree-size --version
msys2-tree-size diagnose
```

`diagnose` reports which optional capabilities work on *your* machine — whether
MSYS2 was found and which archive formats can be read. Run it first if something
below does not behave as described.

## The four commands

### `du` — recursive sizes

```bash
msys2-tree-size du /c/Users
```

```
    17.9K  archive-demo
|--      3.8K  payload
|   |--      2.6K  nested
|   |   `--      2.6K  b.bin
|   `--      1.2K  a.txt
|--      2.0K  random.bin
`--      273B  bundle.zip
```

Common options:

| Option | Effect |
| --- | --- |
| `--flat` | one line per entry instead of a tree |
| `--top N` | show only the N largest entries |
| `--max-depth N` | do not descend deeper than N |
| `--min-size SIZE` | ignore anything smaller (e.g. `10M`) |
| `--archives` | also read inside archive files |
| `--json [FILE]` | machine-readable output (stdout if no file) |
| `--unicode` | box-drawing characters instead of ASCII |
| `--no-files` | directories only |

```bash
msys2-tree-size du /c/Users --flat --top 20
msys2-tree-size du /c/Users --max-depth 2 --min-size 100M
msys2-tree-size du /c/Users --json report.json
```

### `dupes` — duplicate files

```bash
msys2-tree-size dupes /c/Users --min-size 1M
```

Hashes file contents (not names) and reports groups of identical files, plus
how much space could be reclaimed. `--csv FILE` writes a report, `--json` prints
one, and `--archives` looks inside archives as well.

### `devices` — physical disks

```bash
msys2-tree-size devices
```

```
     32.0M  sdc2  usb-USB_SanDisk_3.2Gen1_...-part2
    100.0M  sda1  nvme-Great_Wall_GW3300_256GB_...-part1
    223.3G  sda3  nvme-Great_Wall_GW3300_256GB_...-part3
    460.3G  sdc   usb-USB_SanDisk_3.2Gen1_...
```

Sizes, kernel names, and stable hardware identifiers from `/dev/disk/by-id` —
including partitions Windows gives no drive letter.

### `diagnose` — what works here

```bash
msys2-tree-size diagnose
```

Reports the interpreter, whether MSYS2 was found, and which archive backends
are usable. Use it when a command reports nothing.

## Reading inside archives

A folder of tarballs reports a few megabytes and hides hundreds. `--archives`
opens them:

```console
$ msys2-tree-size du backup --flat
273B    4.13%  file  backup/bundle.zip

$ msys2-tree-size du backup --flat --archives
273B    1.49%  file  backup/bundle.zip
   2.6K  14.74%  file  backup/bundle.zip::payload/nested/b.bin
   1.2K   6.55%  file  backup/bundle.zip::payload/a.txt
```

`bundle.zip::payload/a.txt` means "a member of bundle.zip". Members are **display
strings, never real paths**: nothing is extracted to disk, so scanning an
untrusted tree cannot write files.

| Format | Needs |
| --- | --- |
| `tar`, `tar.gz`, `tar.bz2`, `tar.xz` | nothing |
| `zip`, `jar`, `apk`, `docx`, `xlsx`, `pptx`, `odt`/`ods`/`odp`, `epub`, `whl` | nothing |
| `7z` | `7z` or `bsdtar` |
| `rar` | `unrar`, `7z` or `bsdtar` |
| `tar.zst`, bare `.gz`/`.xz`/`.zst`/`.bz2` | `zstd` or `bsdtar` |

Inside MSYS2, everything is one command:

```bash
pacman -S mingw-w64-clang-x86_64-7zip mingw-w64-clang-x86_64-unrar
```

A missing tool costs exactly one format; run `diagnose` to see which are active.
Penetration is off unless asked for, and bounded by `--archive-depth` (nested
archives) and `--max-archive-size`.

## Why MSYS2 matters

On Windows, install and run this **inside MSYS2**. The reason is `devices`:

- `/dev/disk/by-id` gives stable hardware names instead of drive letters, which
  Windows reassigns. It also lists partitions that have **no drive letter at
  all** and are invisible in Explorer.
- `/proc/partitions` gives sizes for those partitions.

Those are MSYS2 paths. A plain `cmd.exe` prompt cannot see them, so `devices`
will report that it found nothing; `du` and `dupes` still work there.

MSYS2's own Python is a native Windows build, so it cannot open those paths
either — the tool detects the MSYS2 installation and reaches them through the
runtime. `diagnose` shows exactly what was found.

On Linux, macOS or WSL everything works, and `devices` reads that system's own
`/dev/disk/by-id` and `/proc/partitions`.

## Notes

- **Paths may be written either way.** `/c/Users` and `C:\Users` are the same
  place under MSYS2.
- **Filenames are handled byte-exactly.** A file whose name is not valid UTF-8
  round-trips through the reports intact rather than being mangled.
- **Symlinks are never followed** and report size 0, so bytes are not counted
  twice and a link loop cannot hang the scan.
- **Unreadable files are reported, not fatal.** A permission error becomes a
  warning on stderr and the scan continues.

## License

AGPL-3.0-or-later. See `LICENSE`.
