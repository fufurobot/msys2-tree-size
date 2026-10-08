# msys2-tree-size — portable Windows bundle

This directory **is** a complete MSYS2 installation with `msys2-tree-size`
already installed. Nothing else needs to be downloaded, installed, or
configured: extract the archive and run it.

## Quick start

Open a terminal in this directory and run:

```
msys2_shell.cmd -clang64 -here -c "msys2-tree-size du /c/Users"
```

Or start an interactive shell and work from there:

```
msys2_shell.cmd -clang64 -here
msys2-tree-size devices
```

`-here` makes the shell start in the current directory instead of the MSYS2
home. `-clang64` selects the environment this bundle was built against; use it
consistently, because the tool is installed into that environment only.

## Commands

```bash
# where did the disk space go (tree view, largest first)
msys2-tree-size du /c/Users

# flat listing, two levels deep, 20 biggest entries
msys2-tree-size du /c/Users --depth 2 --top 20 --flat

# machine-readable output
msys2-tree-size du /c/Users --json report.json

# look inside archives and account for what they hold
msys2-tree-size du /c/Users --archives

# find duplicate files by content hash
msys2-tree-size dupes /c/Users --min-size 1M

# list physical devices and their partitions
msys2-tree-size devices
```

Paths may be written either way; `/c/Users` and `C:\Users` are the same place.

Run `msys2-tree-size --help` or `msys2-tree-size du --help` for the full list.

## Reading inside archives

`--archives` opens archive files and adds their contents to the totals, which
matters when a folder holds tarballs rather than loose files:

```
273B    1.49%  file  backup/bundle.zip
   2.6K  14.74%  file  backup/bundle.zip::data/big.bin
```

`bundle.zip::data/big.bin` means "a member of bundle.zip". Nothing is extracted
to disk: only the archive's index is read, so scanning an untrusted tree cannot
write files.

This bundle ships `7z` and `unrar` alongside Python's own `tar`/`zip` support,
so the readable formats are `tar`, `tar.gz`, `tar.bz2`, `tar.xz`, `tar.zst`,
`zip`, `jar`, `apk`, `docx`, `xlsx`, `pptx`, `odt`/`ods`/`odp`, `epub`, `whl`,
`7z`, `rar`, and bare `.gz`/`.xz`/`.zst`/`.bz2`.

Penetration is off unless asked for, because opening every archive can be slow.
Use `--archive-depth` to bound nested archives and `--max-archive-size` to skip
large ones.

## Why MSYS2 is bundled

`msys2-tree-size` deliberately depends on things only MSYS2 provides:

- **`/dev/disk/by-id`** — stable, hardware-derived device names, so a disk is
  identified by its model and serial rather than by whatever drive letter
  Windows assigned this week.
- **`/proc/partitions`** — every partition with its size, including the ones
  that have no drive letter and therefore do not appear in Explorer at all.
- **POSIX filenames** — names are arbitrary bytes. Windows APIs re-encode them
  as UTF-16 and lose or mangle anything that is not valid Unicode.

That last one is the sharp edge: a file whose name is not valid UTF-8 is
represented in Python as *lone surrogates*, and most serialisation layers
(JSON, CSV, and `pyarrow`-backed pandas) either mangle or outright reject it.
This tool round-trips such names byte-exactly, which is only meaningful on a
filesystem that permits them.

A side effect worth knowing: MSYS2's own Python is a **native Windows** build,
so it cannot open `/c`, `/dev` or `/proc` by itself. The tool detects the MSYS2
installation and reaches those paths through the runtime. That is why running
`msys2-tree-size.exe` from a plain `cmd.exe` will not find your devices, while
running it inside this shell will.

## First run is slower

The bundle ships with the pacman package cache and Python byte-code caches
removed to keep the download small. The first invocation of a command may take
a moment while those are regenerated. Subsequent runs are immediate.

## Layout

```
msys2_shell.cmd     the launcher; start here
clang64/            the environment the tool is installed into
usr/                the MSYS2 runtime, which provides /c, /dev and /proc
etc/                runtime configuration
MANIFEST.sha256     SHA-256 of every file, for verifying the download
```

To confirm the archive extracted intact, from inside the shell:

```
sha256sum -c MANIFEST.sha256
```

## Uninstalling

Delete this directory. The bundle is self-contained and writes nothing outside
it except in your own temp directory.

## License

The tool is AGPL-3.0-or-later. The bundled MSYS2 runtime and its packages carry
their own licenses; see `LICENSE` in this directory and the individual package
directories under `clang64/` and `usr/`.
