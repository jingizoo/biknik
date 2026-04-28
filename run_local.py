#!/usr/bin/env python3
"""Local runner for OFAM IU Asset Transfers — BIP-driven discovery.

Discovers all pending Inter-Unit transfers from a BI Publisher report and
executes them.  No individual asset IDs or numbers are needed — the BIP
report (called with just the book code) returns all eligible IU assets.

Usage:
  # Dry-run (default — safe, no writes to Fusion):
  python run_local.py --config config.json

  # Execute for real:
  python run_local.py --config config.json --execute

  # Override output directory:
  python run_local.py --config config.json --out-dir ./my_output

  # Verbose logging:
  python run_local.py --config config.json --log-level DEBUG

Environment variables:
  FUSION_JWT          Bearer token for Oracle Fusion / BIP
  FUSION_BASE_URL     (optional) override oracle.base_url from config
  ENABLE_CLOUD_LOGGING set to 1/true/yes to ship logs to GCP Cloud Logging
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

# Ensure the package is importable when running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src" / "main" / "python"))

from ofam_asset_xfer.exceptions import ConfigError  # noqa: E402
from ofam_asset_xfer.pipeline import run_pipeline  # noqa: E402


def _load_config(path: str) -> dict:
    raw = Path(path).read_text()
    try:
        return json.loads(raw)
    except Exception as e:
        raise ConfigError(f"Config is not valid JSON: {path}") from e


def _maybe_setup_cloud_logging(log: logging.Logger, log_level: int) -> None:
    if os.getenv("ENABLE_CLOUD_LOGGING", "").strip().lower() not in ("1", "true", "yes"):
        return
    try:
        import google.cloud.logging as cloud_logging  # type: ignore[import-untyped]

        cloud_logging.Client().setup_logging(log_level=log_level)
        log.info("Google Cloud Logging enabled")
    except Exception:
        log.warning(
            "Could not initialise Google Cloud Logging — continuing with local logs only",
            exc_info=True,
        )


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for the local runner."""
    p = argparse.ArgumentParser(
        description="Run OFAM IU asset transfers locally via BIP report discovery.",
    )
    p.add_argument("--config", required=True, help="Path to JSON config file.")
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

    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-5s %(name)s — %(message)s",
    )
    log = logging.getLogger("run_local")
    _maybe_setup_cloud_logging(log, log_level)

    if args.out_dir:
        out_dir = args.out_dir
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = str(Path("output") / ts)
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    execute = args.execute and not args.dry_run
    dry_run = not execute

    log.info("Config  : %s", args.config)
    log.info("Output  : %s", out_dir)
    log.info("Mode    : %s", "EXECUTE" if execute else "DRY-RUN")

    config = _load_config(args.config)

    result = run_pipeline(
        config=config,
        out_dir=out_dir,
        dry_run=dry_run,
        log=log,
        config_path=args.config,
    )

    if result.summary is not None:
        counts = result.summary.get("counts", {})
        log.info("——— Summary ———")
        log.info("  Total       : %s", counts.get("total", 0))
        log.info("  Transferred : %s", counts.get("transferred", 0))
        log.info("  Failed      : %s", counts.get("failed", 0))
        log.info("  Dry-run     : %s", counts.get("dry_run", 0))
        log.info("Full results  : %s", Path(out_dir) / "results.json")
        log.info("NDJSON logs   : %s", Path(out_dir) / date.today().isoformat())

    # Map pipeline's hard-error code (2) to run_local's historical 1.
    return 0 if result.exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
