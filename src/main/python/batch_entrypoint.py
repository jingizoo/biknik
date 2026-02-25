#!/usr/bin/env python3
"""CDX Batch entrypoint for OFAM Asset Transfer (API-only).

Wrapper scripts should invoke this module.

Example:
  python batch_entrypoint.py --config /tmp/requests.json --out-dir /tmp/out --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from ofam_asset_xfer.job import run_job


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="OFAM Asset Transfer Automation (API-only).")
    p.add_argument("--config", required=True, help="Path/URI to requests JSON (local path or supported fsspec URI).")
    p.add_argument("--out-dir", required=True, help="Output directory/URI for artifacts.")
    p.add_argument("--dry-run", action="store_true", help="If set, performs reads/validation only (no writes to Fusion).")
    p.add_argument("--execute", action="store_true", help="If set, posts transactions to Fusion. Overrides --dry-run.")
    p.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"), help="Logging level (INFO, DEBUG, ...).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    # Execution precedence:
    # - If --execute is present, execute=True.
    # - Else if --dry-run is present, execute=False.
    # - Else defer to JSON config execute flag (default False).
    cli_execute = True if args.execute else (False if args.dry_run else None)

    try:
        run_job(
            config_uri=args.config,
            out_dir_uri=args.out_dir,
            cli_execute=cli_execute,
        )
        return 0
    except Exception:
        logging.getLogger("batch_entrypoint").exception("Fatal error running job")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
