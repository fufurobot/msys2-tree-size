# Design notes

## The core problem: byte-exact paths

POSIX filenames are arbitrary byte strings excluding `NUL` and `/`. Python's
`os` layer decodes them using the *filesystem encoding* (`utf-8` on Linux and on
MSYS2) with the `surrogateescape` error handler. Bytes that are not valid UTF-8
become **lone surrogates**:

```python
>>> b"\xff".decode("utf-8", "surrogateescape")
'\udcff'
>>> '\udcff'.encode("utf-8", "surrogateescape")
b'\xff'
```

This round-trip is lossless *only if every layer in between agrees to keep the
surrogates*. Many do not:

| Layer | Behaviour |
| --- | --- |
| `str`, `pathlib.Path` | stores surrogates happily |
| `open(..., errors="surrogateescape")` | lossless |
| `json.dumps` | **lossy**: emits `\udcff` escapes, and `json.loads` gives back a lone surrogate you can re-encode, but third-party consumers choke |
| `csv` writer | lossy unless the file object uses `surrogateescape` |
| `pyarrow` / pandas `string[pyarrow]` | **raises** `UnicodeEncodeError` |

### Consequence for this project

1. Never let a path reach a serializer by accident. `paths.py` owns the
   conversion, and every writer in the codebase passes bytes explicitly.
2. Do not enable pandas' `future.infer_string`; keep `mode.string_storage=python`
   when pandas is used at all.
3. Prefer JSON with `ensure_ascii=True` for machine output, because the `\uXXXX`
   escapes survive a round-trip through a non-`surrogateescape` reader.

`paths.py` exposes:

- `decode_path(bytes) -> str`
- `encode_path(str) -> bytes`
- `write_text(path, text)` — writes through `encode_path`
- `write_json(path, obj)` — `json.dumps(..., ensure_ascii=True)` + `write_text`

## Traversal model

`walk.py` performs **one** traversal and yields one record per entry, in
post-order (children strictly before their parent). This single ordering gives
three properties for free:

1. **Directory sizes are subtree totals.** By the time a directory is yielded,
   every descendant has already been seen, so its `size` is complete.
2. **Subtree hashes are Merkle hashes.** A directory's hash is
   `SHA256(concat(sorted(child_name, child_hash)))`. Two directories with equal
   hashes are byte-identical in content *and* in structure, so the dedupe rule
   "if a directory's hash equals its child's hash, the child adds nothing" is
   sound.
3. **Memory stays bounded per level**, not per tree, because aggregation happens
   on the way up rather than by holding every path.

Records are plain dataclasses, so the traversal is testable without pandas,
tqdm, or a real disk: the tests drive a `FakeFS`.

## Why post-order matters for the dedupe rule

Consider `a/` containing `b/`, where both hash to `H`. The rule drops `b/`
because `a/` already accounts for those bytes. Without post-order, `a`'s hash
would not be known when `b` was emitted, and the naive `value_counts() >= 2`
approach would report *both* as duplicates of each other — double-counting every
byte in the subtree. The prototype in `drivev3.py` handles this with a
`path_to_sha` lookup after the fact; doing it during traversal is cheaper and
removes an O(n) dict rebuild per device.

## Device inventory

`devices.py` exists because `/dev/disk/by-id` is a directory of *symlinks* whose
names encode the hardware (e.g. `nvme-Samsung_SSD_970_...-part1`). The inventory:

1. `ls -l /dev/disk/by-id` → columns `id`, `->`, `actual`; the symlink target's
   basename is the kernel device name (`nvme0n1p1`).
2. `/proc/partitions` → blocks in KiB, keyed by that same device name.
3. Inner-join on the device name, then sort ascending by block count so small
   devices are processed first (they finish fast, giving early output).

Parsing is split from process execution (`parse_by_id`, `parse_partitions`) so it
can be unit-tested against captured fixtures without shelling out.

## MSYS2 path translation

Under MSYS2, `/c/Users` and `C:\Users` name the same directory. Input paths are
normalised through `paths.to_posix()` so that a Windows-style argument typed at a
`cmd.exe` prompt still works, but the canonical form in all output is POSIX.

## What running on real MSYS2 actually revealed

Every claim in this section was wrong the first time. The corrections came from
running against a real MSYS2 CLANG64 installation, not from reasoning about it.

### `sys.platform` is `win32`, not `msys`

MSYS2 ships a **native Windows** Python build. `sys.platform` is `"win32"`, and
such an interpreter cannot open `/c/Users`, `/dev/disk/by-id` or
`/proc/partitions` **at all** — those paths are virtual, and the MSYS2 runtime
resolves them only for MSYS2 *binaries*, never for an arbitrary Win32 process.

So `paths.is_msys2()` means "the interpreter itself resolves POSIX paths" (true
only for a genuine Cygwin/MSYS runtime Python), MSYS2 discovery lives in
`paths.msys2_root()` and probes the filesystem rather than trusting
`sys.platform`, and device reporting from a Win32 interpreter necessarily goes
through a subprocess.

### Do not hardcode `C:\msys64`

MSYS2 is routinely installed elsewhere: a portable extract, a per-user
directory, a package-manager prefix. The machine this was developed on has it
under `Downloads`. A hardcoded path silently disables device reporting for
everyone else, so `msys2_root()` searches, in order: `MSYS2_ROOT`/`MSYS2_DIR`,
the conventional prefixes, anything already on `PATH` (a `bash`/`ls`/`cat`
reveals its installation root), then a depth-bounded scan.

### `/proc/partitions` has a fifth column

On MSYS2 it reads:

```
major minor  #blocks  name   win-mounts

    8     3 234095616 sda3   C:\
```

`win-mounts` holds the Windows drive letter. A strict four-column parser matches
**nothing** there, so the tool reported an empty device table on a machine with
ten disks. The first four columns are now read positionally and the remainder
kept as `win-mounts`, so Linux output still yields identical keys.

### MSYS2 `usr/bin` tools cannot be exec'd directly

`usr/bin/ls.exe` and `cat.exe` abort when started by a Win32 process:

```
NtCreateDirectoryObject(\BaseNamedObjects\msys-2.0S5-...): 0xC0000022
```

They are POSIX binaries and need the MSYS2 runtime to create their shared-memory
section. Reaching the virtual paths therefore goes through `msys2_shell.cmd`,
invoked via `cmd.exe` because it is a batch file. Where a host forbids even that
— some sandboxes constrain child processes — the reader returns empty and the
command reports a note rather than raising or quietly lying.

## Testing strategy

- **Unit tests** (`tests/`) never touch the real filesystem for logic: they use
  `FakeFS`, an in-memory tree injected into the traversal.
- **Fixtures are verbatim captures.** `PROC_PARTITIONS_MSYS2` and
  `LS_BY_ID_MSYS2` in `tests/test_devices.py` reproduce the real `win-mounts`
  column and long NVMe by-id names. A fixture test is what catches a format
  assumption like the four-column bug.
- **Integration tests** use a scratch directory inside the checkout and skip on
  the specific capability they need, so they run on MSYS2 and skip honestly
  elsewhere.
- **MSYS2-only guards are narrow.** `paths.is_msys2()` is deliberately stricter
  than "MSYS2 is installed"; the device tests guard on
  `msys2_root() is not None` instead, because device reading works from a Win32
  interpreter through the launcher.

Scratch directories use **fixed** paths under `.tmp/` rather than `mkdtemp`.
Cleanup failures must not be reported as test failures, and a write grant is
often provisioned per fixed path, so a dynamically named directory can be
readable but not writable.

