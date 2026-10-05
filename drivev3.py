#!/usr/bin/env python
"""Export mapping of /dev/disk/by-id symlinks to device metadata."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd
from tqdm.auto import tqdm

# ---------------------------------------------------------------------------
# Byte-level string handling
# ---------------------------------------------------------------------------
# POSIX filenames are arbitrary byte sequences.  Python represents non-UTF-8
# bytes as *lone surrogates* (U+DC80..U+DCFF) via the "surrogateescape" error
# handler.  Pandas >= 2.1 / 3.x can pick a pyarrow-backed string dtype, and
# pyarrow refuses to encode lone surrogates -> UnicodeEncodeError.
#
# So: (a) keep pandas on plain Python `str` (which happily stores surrogates),
# and (b) round-trip every path / file through UTF-8 + surrogateescape so the
# original bytes survive verbatim.  No encoding decisions are made by us.
for _opt, _val in (("future.infer_string", False),
                   ("mode.string_storage", "python")):
    try:
        pd.set_option(_opt, _val)
    except Exception:
        pass

_ENCODING = "utf-8"
_ERRORS = "surrogateescape"


def _b2s(b: bytes) -> str:
    """bytes -> str, keeping undecodable bytes as lone surrogates."""
    return b.decode(_ENCODING, _ERRORS)


def _s2b(s: str) -> bytes:
    """str -> bytes, turning lone surrogates back into the original bytes."""
    return s.encode(_ENCODING, _ERRORS)


def _write_text(path: Path, text: str) -> None:
    with open(path, "wb") as f:
        f.write(_s2b(text))


def _write_json(path: Path, obj: object) -> None:
    _write_text(path, json.dumps(obj))


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    """Write a DataFrame to CSV without lossy re-encoding of path bytes."""
    try:
        with open(path, "w", encoding=_ENCODING, errors=_ERRORS, newline="") as f:
            df.to_csv(f, index=False)
    except UnicodeEncodeError as e:
        try:
            print(e, file=sys.stderr)
            df.to_parquet(path.with_suffix(".parquet"))
        except Exception as e:
            print(e, file=sys.stderr)
            df.to_pickle(path.with_suffix(".pkl"))


def _read_csv(path: Path) -> pd.DataFrame:
    with open(path, "r", encoding=_ENCODING, errors=_ERRORS, newline="") as f:
        return pd.read_csv(f)


BY_ID_DIR = "/dev/disk/by-id"
PARTITIONS_FILE = "/proc/partitions"
OUTPUT_CSV = "disk_id.csv"
DEVICE_INFO_FILENAME = "device_info.json"
DU_CSV = "du.csv"
DU_SUMMARY_CSV = "du_summary.csv"
DUPLICATES_CSV = "duplicates.csv"
DUPLICATES_ALL_CSV = "duplicates_all.csv"


def run(cmd: str) -> str:
    """Run *cmd* and return stdout, preserving the raw bytes."""
    return _b2s(subprocess.check_output(cmd, shell=True))


def read_by_id() -> pd.DataFrame:
    """Parse `ls -l /dev/disk/by-id` into id -> actual -> name."""
    lines = run(f"ls -l {BY_ID_DIR}").splitlines()
    df = pd.DataFrame(line.split() for line in lines if line.strip())
    df = df.iloc[:, -3:].dropna()
    df.columns = ["id", "to", "actual"]
    df["name"] = df["actual"].str.split("/", expand=True).iloc[:, -1]
    return df


def read_partitions() -> pd.DataFrame:
    """Parse /proc/partitions into a DataFrame."""
    lines = run(f"cat {PARTITIONS_FILE}").splitlines()
    df = pd.DataFrame(line.split() for line in lines if line.strip())
    df.columns = df.iloc[0]
    return df.iloc[1:].reset_index(drop=True)


def build_table() -> pd.DataFrame:
    """Merged id/partition table, sorted smallest device first (by KiB)."""
    df = pd.merge(read_by_id(), read_partitions())
    df["#blocks"] = pd.to_numeric(df["#blocks"], errors="coerce")
    df = df.sort_values("#blocks", ascending=True, na_position="last")
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# du / size statistics helpers
# ---------------------------------------------------------------------------

def human_readable(num_bytes: int) -> str:
    """Convert a byte count into a human readable string (base 2, `du -h`-ish)."""
    size = float(num_bytes)
    for unit in ("B", "K", "M", "G", "T", "P", "E"):
        if size < 1024 or unit == "E":
            return f"{int(size)}B" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}E"


def _file_sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's contents."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _path_bytes(path: Path) -> int:
    """Recursively total the byte size of a file or directory."""
    try:
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for fname in files:
            try:
                total += os.path.getsize(os.path.join(root, fname))
            except OSError:
                continue
    return total


def du(path: Path, depth: int = 0):
    """Recursively yield a size entry for every descendant of *path*.

    Entries are produced depth-first in post-order: a directory is yielded
    only after all of its contents, so its ``bytes`` value already accounts
    for its entire subtree.  Each yielded item is a dict with the keys
    ``name``, ``path``, ``parent``, ``type``, ``depth``, ``bytes``,
    ``human`` and ``sha256``.
    """
    path = Path(path)
    if not path.is_dir():
        return
    try:
        path.iterdir()
    except (PermissionError, FileNotFoundError) as e:
        print(e, file=sys.stderr)
        return
    for child in sorted(path.iterdir()):
        if child.is_dir():
            total = 0
            child_hashes = []
            for entry in du(child, depth + 1):
                if entry["depth"] == depth + 1:
                    total += entry["bytes"]
                    child_hashes.append((entry["name"], entry["sha256"]))
                yield entry
            # Compute directory hash from all direct child hashes (sorted by name)
            child_hashes.sort(key=lambda x: x[0])
            h = hashlib.sha256()
            for name, sha in child_hashes:
                h.update(sha.encode("utf-8"))
            child_sha = h.hexdigest()
            yield {
                "name": child.name,
                "path": str(child),
                "parent": str(path),
                "type": "dir",
                "depth": depth,
                "bytes": total,
                "human": human_readable(total),
                "sha256": child_sha,
            }
        else:
            try:
                size = child.stat().st_size
            except OSError:
                size = 0
            sha = _file_sha256(child)
            yield {
                "name": child.name,
                "path": str(child),
                "parent": str(path),
                "type": "file",
                "depth": depth,
                "bytes": size,
                "human": human_readable(size),
                "sha256": sha,
            }


def du_stats(path: Path) -> tuple[list[dict], dict]:
    """Return the recursive du listing plus aggregate statistics for *path*."""
    path = Path(path)
    rows = list(tqdm(du(path), desc="checking files and directories"))

    totals: dict[str, int] = {
        str(path): sum(r["bytes"] for r in rows if r["depth"] == 0)
    }
    for r in rows:
        if r["type"] == "dir":
            totals[r["path"]] = r["bytes"]

    for r in rows:
        parent_total = totals.get(r["parent"], 0)
        r["percent_of_parent"] = (
            round(100.0 * r["bytes"] / parent_total, 4) if parent_total else 0.0
        )

    total = totals[str(path)]

    entropy_bits = 0.0
    if total:
        for r in rows:
            if r["depth"] == 0 and r["bytes"] > 0:
                p = r["bytes"] / total
                entropy_bits -= p * math.log2(p)

    ext_sizes = _extension_sizes(path)
    if ext_sizes:
        top_ext = max(ext_sizes, key=ext_sizes.get)
        top_ext_bytes = ext_sizes[top_ext]
    else:
        top_ext, top_ext_bytes = "<none>", 0
    top_ext_pct = round(100.0 * top_ext_bytes / total, 4) if total else 0.0

    summary = {
        "total_bytes": total,
        "total_human": human_readable(total),
        "child_count": sum(1 for r in rows if r["depth"] == 0),
        "total_items": len(rows),
        "entropy_bits": round(entropy_bits, 6),
        "top_extension": top_ext,
        "top_extension_bytes": top_ext_bytes,
        "top_extension_human": human_readable(top_ext_bytes),
        "top_extension_percent": top_ext_pct,
    }
    return rows, summary


def _extension_sizes(path: Path) -> dict[str, int]:
    """Map file extension -> total bytes for every file under *path*."""
    ext_sizes: dict[str, int] = defaultdict(int)
    if not path.exists():
        return ext_sizes
    for root, _dirs, files in os.walk(path):
        for fname in files:
            fp = os.path.join(root, fname)
            try:
                ext_sizes[Path(fname).suffix.lower() or "<none>"] += os.path.getsize(fp)
            except OSError:
                continue
    return ext_sizes


# ---------------------------------------------------------------------------
# writers
# ---------------------------------------------------------------------------

def write_device_info(table: pd.DataFrame) -> None:
    for record in tqdm(table.dropna().to_dict(orient="records"),
                       desc="processing drives"):
        path = Path(record["id"])
        path.mkdir(exist_ok=True)

        _write_json(path / DEVICE_INFO_FILENAME, record)
        _write_json(path / "filelist.txt", record)

        rows, summary = du_stats(Path(record["win-mounts"]))
        df_du = pd.DataFrame(rows)
        _write_csv(df_du, path / DU_CSV)
        _write_csv(pd.DataFrame([summary]), path / DU_SUMMARY_CSV)

        if not df_du.empty and "sha256" in df_du.columns:
            hash_counts = df_du["sha256"].value_counts()
            dup_hashes = hash_counts[hash_counts >= 2].index
            df_dup = df_du[df_du["sha256"].isin(dup_hashes)].copy()

            # Rule: if a parent hash matches the child hash, drop the child.
            if not df_dup.empty:
                path_to_sha = dict(zip(df_dup["path"], df_dup["sha256"]))
                keep = []
                for _, row in df_dup.iterrows():
                    parent = row["parent"]
                    if parent in path_to_sha and path_to_sha[parent] == row["sha256"]:
                        keep.append(False)
                    else:
                        keep.append(True)
                df_dup = df_dup[keep]

            _write_csv(df_dup, path / DUPLICATES_CSV)


# ---------------------------------------------------------------------------
# global duplicate aggregation (auxiliary)
# ---------------------------------------------------------------------------

def _device_dirs(output_csv: Path = Path(OUTPUT_CSV)) -> list[Path]:
    """Return the per-device directories that hold a ``du.csv``."""
    csv_path = Path(output_csv)
    if csv_path.exists():
        try:
            table = _read_csv(csv_path)
        except (OSError, pd.errors.EmptyDataError):
            table = pd.DataFrame()
        if "id" in table.columns:
            return [Path(str(i)) for i in table["id"].dropna().unique()]
    return [
        p for p in sorted(Path(".").iterdir())
        if p.is_dir() and (p / DU_CSV).exists()
    ]


def _load_device_du(device_dir: Path) -> pd.DataFrame:
    """Read one device's ``du.csv`` and tag every row with its device id."""
    du_path = device_dir / DU_CSV
    if not du_path.exists():
        return pd.DataFrame()
    try:
        df = _read_csv(du_path)
    except (OSError, pd.errors.EmptyDataError):
        return pd.DataFrame()
    if df.empty:
        return df
    df["device"] = device_dir.name
    return df


def _drop_parent_child_dupes(df: pd.DataFrame) -> pd.DataFrame:
    """Per-device rule: drop a child whose hash equals its parent's hash."""
    if df.empty or "sha256" not in df.columns or "parent" not in df.columns:
        return df
    path_to_sha = dict(zip(df["path"], df["sha256"]))
    keep = [
        not (row["parent"] in path_to_sha
             and path_to_sha[row["parent"]] == row["sha256"])
        for _, row in df.iterrows()
    ]
    return df[keep]


def write_duplicates_all(
    output_csv: Path = Path(OUTPUT_CSV),
    out_path: Path = Path(DUPLICATES_ALL_CSV),
) -> pd.DataFrame:
    """Re-read every device's ``du.csv`` and write a global duplicate report."""
    frames: list[pd.DataFrame] = []
    for device_dir in tqdm(_device_dirs(output_csv), desc="reading du.csv"):
        df = _load_device_du(device_dir)
        if df.empty or "sha256" not in df.columns:
            continue
        df = _drop_parent_child_dupes(df)
        frames.append(df)

    if not frames:
        print("write_duplicates_all: no du.csv data found", file=sys.stderr)
        _write_csv(pd.DataFrame(), out_path)
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)

    combined = combined[combined["sha256"].astype(str).str.len() > 0]
    if combined.empty:
        _write_csv(combined, out_path)
        return combined

    counts = combined["sha256"].value_counts()
    dup_hashes = counts[counts >= 2].index
    result = combined[combined["sha256"].isin(dup_hashes)].copy()

    if result.empty:
        _write_csv(result, out_path)
        return result

    group_sizes = result.groupby("sha256").size()
    group_devices = result.groupby("sha256")["device"].nunique()
    result["duplicate_count"] = result["sha256"].map(group_sizes).astype(int)
    result["device_count"] = result["sha256"].map(group_devices).astype(int)
    result["cross_device"] = result["device_count"] > 1

    result = result.sort_values(
        ["sha256", "device", "bytes"], ascending=[True, True, False]
    )

    preferred = [
        "sha256", "duplicate_count", "device_count", "cross_device",
        "device", "name", "path", "parent", "type", "depth", "bytes", "human",
    ]
    result = result[[c for c in preferred if c in result.columns]]

    _write_csv(result, out_path)
    print(
        f"write_duplicates_all: {len(result)} rows across "
        f"{result['sha256'].nunique()} duplicate groups -> {out_path}"
    )
    return result


def main() -> None:
    table = build_table()
    _write_csv(table, Path(OUTPUT_CSV))
    write_device_info(table)
    write_duplicates_all()


if __name__ == "__main__":
    main()