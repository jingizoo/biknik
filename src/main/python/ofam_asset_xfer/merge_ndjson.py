#!/usr/bin/env python3
"""Merge daily NDJSON files into a single Tableau-ready file.

Scans all ``<base_dir>/YYYY-MM-DD/results.ndjson`` files, concatenates them
into one ``results_all.ndjson`` (and ``errors_all.ndjson``), and optionally
filters by date range.

Usage::

    # Merge everything in ./output into one file:
    python -m ofam_asset_xfer.merge_ndjson ./output

    # Only last 90 days:
    python -m ofam_asset_xfer.merge_ndjson ./output --days 90

    # Custom output path:
    python -m ofam_asset_xfer.merge_ndjson ./output -o /tmp/tableau_feed

    # Also produce CSV for Tableau Desktop:
    python -m ofam_asset_xfer.merge_ndjson ./output --csv

The merged files are what you point Tableau at — one flat table, all days.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def collect_ndjson_files(
    base_dir: Path,
    filename: str = "results.ndjson",
    after: Optional[date] = None,
) -> List[Path]:
    """Return sorted list of daily NDJSON files, optionally filtered by date."""
    files: List[Path] = []
    if not base_dir.exists():
        return files
    for child in sorted(base_dir.iterdir()):
        if not child.is_dir():
            continue
        try:
            folder_date = date.fromisoformat(child.name)
        except ValueError:
            continue
        if after and folder_date < after:
            continue
        ndjson_file = child / filename
        if ndjson_file.exists():
            files.append(ndjson_file)
    return files


def merge_ndjson_files(files: List[Path]) -> str:
    """Read and concatenate all NDJSON files into one string."""
    parts = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        if text and not text.endswith("\n"):
            text += "\n"
        parts.append(text)
    return "".join(parts)


def ndjson_to_csv(ndjson_text: str) -> str:
    """Convert NDJSON text to CSV text for Tableau Desktop."""
    if not ndjson_text.strip():
        return ""
    rows = [json.loads(line) for line in ndjson_text.strip().split("\n") if line.strip()]
    if not rows:
        return ""
    # Use keys from first row as header (all rows should have same schema)
    fieldnames = list(rows[0].keys())
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def merge(
    base_dir: str,
    out_dir: Optional[str] = None,
    days: Optional[int] = None,
    write_csv: bool = False,
) -> Dict[str, Any]:
    """Merge daily NDJSON into consolidated files.

    Returns a dict with row counts for results and errors.
    """
    base = Path(base_dir)
    dest = Path(out_dir) if out_dir else base
    dest.mkdir(parents=True, exist_ok=True)

    after = date.today() - timedelta(days=days) if days else None

    stats = {}
    for kind in ("results", "errors"):
        files = collect_ndjson_files(base, f"{kind}.ndjson", after=after)
        merged = merge_ndjson_files(files)
        row_count = merged.count("\n")

        ndjson_path = dest / f"{kind}_all.ndjson"
        ndjson_path.write_text(merged, encoding="utf-8")
        log.info("Wrote %d rows to %s (%d files merged)", row_count, ndjson_path, len(files))

        if write_csv and merged.strip():
            csv_path = dest / f"{kind}_all.csv"
            csv_path.write_text(ndjson_to_csv(merged), encoding="utf-8")
            log.info("Wrote CSV to %s", csv_path)

        stats[kind] = {"rows": row_count, "files": len(files)}

    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Merge daily NDJSON into a single Tableau-ready file."
    )
    p.add_argument("base_dir", help="Directory containing YYYY-MM-DD/ subfolders.")
    p.add_argument("-o", "--out-dir", default=None, help="Output dir (default: same as base_dir).")
    p.add_argument("--days", type=int, default=None, help="Only include last N days.")
    p.add_argument("--csv", action="store_true", help="Also produce CSV output.")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s — %(message)s",
    )

    stats = merge(args.base_dir, args.out_dir, args.days, args.csv)

    print(f"Results: {stats['results']['rows']} rows from {stats['results']['files']} files")
    print(f"Errors:  {stats['errors']['rows']} rows from {stats['errors']['files']} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
