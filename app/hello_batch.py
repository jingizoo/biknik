#!/usr/bin/env python3
"""hello_batch.py

CDX Batch smoke-test program.

Purpose:
- Verify Batch plumbing: input vars -> download request file -> run python -> write outputs -> upload.

Behavior:
- Reads a JSON config file with either:
    {"requests": [...]}  or a raw list [...]
- Validates minimal fields for FA cross-book requests.
- Writes:
    - run_summary.csv
    - rerun_requests.json (only failures)

This program DOES NOT call Oracle.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

REQUIRED_FIELDS = [
    "src_book_type_code",
    "src_asset_number",
    "dst_book_type_code",
    "transfer_date",
]


def _load_requests(config_path: str) -> List[Dict[str, Any]]:
    raw = Path(config_path).read_text(encoding="utf-8")
    obj = json.loads(raw)
    if isinstance(obj, dict):
        reqs = obj.get("requests")
        if isinstance(reqs, list):
            return [r for r in reqs if isinstance(r, dict)]
        raise ValueError("config JSON dict must contain a 'requests' list")
    if isinstance(obj, list):
        return [r for r in obj if isinstance(r, dict)]
    raise ValueError("config JSON must be a dict or list")


def _validate_request(r: Dict[str, Any]) -> List[str]:
    missing = [f for f in REQUIRED_FIELDS if not str(r.get(f, "")).strip()]
    return missing


def write_csv(path: str, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="Path to requests JSON")
    ap.add_argument("--out-dir", required=True, help="Output directory")
    ap.add_argument("--dry-run", action="store_true", help="Validate only")
    ap.add_argument("--execute", action="store_true", help="Simulated execute (still no Oracle calls)")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    reqs = _load_requests(args.config)
    now = datetime.utcnow().isoformat()

    summary_rows: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    for i, r in enumerate(reqs, start=1):
        missing = _validate_request(r)
        status = "SUCCESS" if not missing else "FAILED"
        err = "" if not missing else f"missing: {', '.join(missing)}"

        # In a real program this is where you would call the transfer engine.
        # Here we just simulate success if required fields exist.
        summary_rows.append({
            "row": i,
            "status": status,
            "src_book_type_code": r.get("src_book_type_code", ""),
            "src_asset_number": r.get("src_asset_number", ""),
            "dst_book_type_code": r.get("dst_book_type_code", ""),
            "transfer_date": r.get("transfer_date", ""),
            "error": err,
            "timestamp_utc": now,
        })
        if status != "SUCCESS":
            failed.append(r)

    write_csv(
        str(out_dir / "run_summary.csv"),
        summary_rows,
        [
            "row",
            "status",
            "src_book_type_code",
            "src_asset_number",
            "dst_book_type_code",
            "transfer_date",
            "error",
            "timestamp_utc",
        ],
    )

    (out_dir / "rerun_requests.json").write_text(
        json.dumps({"requests": failed}, indent=2), encoding="utf-8"
    )

    print(f"WROTE_OUT_DIR={out_dir}")
    print(f"TOTAL={len(reqs)} SUCCESS={len(reqs)-len(failed)} FAILED={len(failed)}")

    # Return non-zero only if execute mode and we had failures (so ops sees FAIL in Batch)
    if args.execute and failed:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
