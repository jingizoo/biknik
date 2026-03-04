#!/usr/bin/env python3
"""Local runner for OFAM Asset Transfer — use while Batch is not yet set up.

Usage:
  # Dry-run (default — safe, no writes to Fusion):
  python run_local.py --config sample_requests.json

  # Execute for real:
  python run_local.py --config sample_requests.json --execute

  # Override output directory:
  python run_local.py --config sample_requests.json --out-dir ./my_output

  # Verbose logging:
  python run_local.py --config sample_requests.json --log-level DEBUG

Environment variables:
  FUSION_JWT          Bearer token for Oracle Fusion
  FUSION_BASE_URL     (optional) override oracle.base_url from config
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Ensure the package is importable when running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src" / "main" / "python"))

from ofam_asset_xfer.job import run_job  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Run OFAM asset transfers locally (no Batch infrastructure needed).",
    )
    p.add_argument(
        "--config",
        required=True,
        help="Path to requests JSON config file (see sample_requests.json).",
    )
    p.add_argument(
        "--out-dir",
        default=None,
        help="Output directory for audit artifacts. Default: ./output/<timestamp>",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Read/validate only — no writes to Fusion (this is the default).",
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="Actually post transactions to Fusion. USE WITH CARE.",
    )
    p.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Logging level (default: INFO).",
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s — %(message)s",
    )
    log = logging.getLogger("run_local")

    # Resolve output dir.
    if args.out_dir:
        out_dir = args.out_dir
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = str(Path("output") / ts)
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    # Execution flag: --execute wins, else --dry-run wins, else defer to config.
    cli_execute = True if args.execute else (False if args.dry_run else None)

    mode = "EXECUTE" if cli_execute else "DRY-RUN" if cli_execute is False else "config-default"
    log.info("Config  : %s", args.config)
    log.info("Output  : %s", out_dir)
    log.info("Mode    : %s", mode)

    try:
        run_job(
            config_uri=args.config,
            out_dir_uri=out_dir,
            cli_execute=cli_execute,
        )
    except Exception:
        log.exception("Job failed")
        return 1

    # Print results summary.
    summary_path = Path(out_dir) / "summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        log.info("——— Summary ———")
        log.info("  Total    : %s", summary.get("total"))
        log.info("  Success  : %s", summary.get("success"))
        log.info("  Failed   : %s", summary.get("failed"))
        log.info("  NOOP     : %s", summary.get("noop"))
        log.info("  Dry-run? : %s", summary.get("dry_run"))
    else:
        log.warning("No summary.json found in output — check logs above for errors.")

    results_path = Path(out_dir) / "results.json"
    if results_path.exists():
        log.info("Full results: %s", results_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
