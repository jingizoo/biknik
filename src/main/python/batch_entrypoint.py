#!/usr/bin/env python3
"""CDX Batch entrypoint for OFAM IU Asset Transfer (BIP-driven discovery).

Discovers all pending Inter-Unit transfers from a BI Publisher report and
executes them.  No individual asset IDs or numbers are required — the BIP
report (called with just the book code) returns all eligible IU assets.

Example:
  python batch_entrypoint.py --config /tmp/config.json --out-dir /tmp/out --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os

from ofam_asset_xfer.pipeline import run_pipeline
from ofam_asset_xfer.store import ArtifactStore


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="OFAM IU Asset Transfer Automation (BIP-driven).")
    p.add_argument("--config", required=True, help="Path/URI to config JSON.")
    p.add_argument("--out-dir", required=True, help="Output directory/URI for artifacts.")
    p.add_argument("--dry-run", action="store_true", help="Reads/validation only (no writes to Fusion).")
    p.add_argument("--execute", action="store_true", help="Post transactions to Fusion. Overrides --dry-run.")
    p.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"), help="Logging level (INFO, DEBUG, ...).")
    return p


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for the CDX batch job."""
    args = _build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    log = logging.getLogger("batch_entrypoint")

    execute = args.execute and not args.dry_run
    dry_run = not execute

    raw = ArtifactStore(base_uri=".").read_text(args.config)
    config = json.loads(raw)

    result = run_pipeline(
        config=config,
        out_dir=args.out_dir,
        dry_run=dry_run,
        log=log,
        config_path=args.config,
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
