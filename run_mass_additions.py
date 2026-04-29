#!/usr/bin/env python3
"""Local runner for OFAM Mass-Additions enrichment.

Walks the mass-additions cycle: pull NEW rows from Oracle FA, look each up
in ServiceNow CMDB, build an ``updateMassAddition`` payload, and (in
``--execute`` mode) post it back to Fusion.

Usage:
  # Dry-run with mock Oracle + real ServiceNow (default):
  python run_mass_additions.py --config sample_mass_additions.json

  # Override output directory:
  python run_mass_additions.py --config sample_mass_additions.json \\
      --out-dir ./mass_addition_run

  # Execute for real (requires a non-mock Oracle client; see config):
  python run_mass_additions.py --config sample_mass_additions.json --execute

Environment variables:
  SNOW_BEARER_TOKEN   ServiceNow bearer token (name configurable in JSON)
  HTTP_APP_PROXY      Corp HTTP proxy (only when require_proxy=true)
  HTTPS_APP_PROXY     Corp HTTPS proxy
  INETPROXY_USER      Proxy basic-auth username
  INETPROXY_PASSWD    Proxy basic-auth password
  ENABLE_CLOUD_LOGGING  Set to 1/true/yes to ship logs to GCP

Config schema (JSON):
  {
    "servicenow": { ...ServiceNowConfig fields... },
    "oracle":     { "mode": "mock_csv", "mock_csv": "data/samples/x.csv" },
    "pilot_book": "CORP_BOOK",
    "pilot_region": "US"
  }
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

from ofam_mass_additions.cmdb.servicenow_client import ServiceNowConfig  # noqa: E402
from ofam_mass_additions.exceptions import ConfigError  # noqa: E402
from ofam_mass_additions.runner.cycle import seed_oracle_rows_from_csv  # noqa: E402
from ofam_mass_additions.runner.live_run import run_live  # noqa: E402


def _load_config(path: str) -> dict:
    raw = Path(path).read_text()
    try:
        return json.loads(raw)
    except Exception as e:
        raise ConfigError(f"Config is not valid JSON: {path}") from e


def _build_oracle_client(
    oracle_block: dict,
    fusion_auth_block: dict | None,
    config_path: str,
    log: logging.Logger,
) -> object:
    """Build the Oracle FA client per ``oracle.mode`` in the config.

    Supported modes:

    * ``fusion``    — call real Oracle Fusion (FA mass-additions REST resource
                      for list/get + processTransaction-updateMassAddition for
                      writes).  This is the production path.
    * ``mock_csv``  — read NEW rows from a local CSV.  Offline rehearsal only.

    When ``fusion_auth_block`` is provided, the static/keytab/password JWT
    flow is wired so the client gets a fresh token per call.  Otherwise
    the client reads the env var named in ``oracle.bearer_token_env``.
    """
    mode = (oracle_block or {}).get("mode", "fusion")

    if mode == "fusion":
        from ofam_mass_additions.fusion_token_provider import build_token_provider
        from ofam_mass_additions.oracle.fusion_client import (
            FusionFaClient,
            FusionFaConfig,
        )

        log.info("Building real Fusion FA client (base_url=%s)", oracle_block.get("base_url"))
        token_provider = build_token_provider(fusion_auth_block) if fusion_auth_block else None
        return FusionFaClient(
            FusionFaConfig.from_dict(oracle_block),
            token_provider=token_provider,
        )

    if mode == "mock_csv":
        from ofam_mass_additions.oracle.mock_client import MockOracleFaClient

        csv_rel = oracle_block.get("mock_csv")
        if not csv_rel:
            raise ConfigError(
                "oracle.mode='mock_csv' requires 'oracle.mock_csv' (path to CSV)."
            )
        csv_path = Path(csv_rel)
        if not csv_path.is_absolute():
            csv_path = Path(config_path).resolve().parent / csv_rel
        log.info("Seeding mock Oracle FA client from %s", csv_path)
        return MockOracleFaClient(rows=seed_oracle_rows_from_csv(csv_path))

    raise ConfigError(
        f"Unknown oracle.mode={mode!r}. Supported: 'fusion', 'mock_csv'."
    )


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
    """Entrypoint for the mass-additions local runner."""
    p = argparse.ArgumentParser(
        description="Run OFAM mass-additions enrichment locally (real ServiceNow CMDB).",
    )
    p.add_argument(
        "--config",
        required=True,
        help="Path to JSON config file (servicenow, oracle, pilot_book, pilot_region).",
    )
    p.add_argument(
        "--out-dir",
        default=None,
        help="Output directory for audit artifacts. Default: ./output/mass_additions/<timestamp>",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Build payloads only — no writes to Fusion (this is the default).",
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="Actually call updateMassAddition. USE WITH CARE.",
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
    log = logging.getLogger("run_mass_additions")
    _maybe_setup_cloud_logging(log, log_level)

    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path("output") / "mass_additions" / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    execute = args.execute and not args.dry_run
    run_mode = "live" if execute else "dry-run"

    log.info("Config   : %s", args.config)
    log.info("Output   : %s", out_dir)
    log.info("Run mode : %s", run_mode.upper())

    try:
        config = _load_config(args.config)

        sn_block = config.get("servicenow")
        if not sn_block:
            raise ConfigError("Config must include a 'servicenow' block.")
        sn_cfg = ServiceNowConfig.from_dict(sn_block)

        oracle_client = _build_oracle_client(
            config.get("oracle") or {},
            config.get("fusion_auth"),
            args.config,
            log,
        )

        pilot_book = config.get("pilot_book", "CORP_BOOK")
        pilot_region = config.get("pilot_region", "US")
        capitalize_threshold = float(
            config.get("capitalize_threshold_amount", 1000.0)
        )

        result = run_live(
            oracle_client=oracle_client,
            servicenow_config=sn_cfg,
            output_dir=out_dir,
            run_mode=run_mode,
            pilot_book=pilot_book,
            pilot_region=pilot_region,
            capitalize_threshold=capitalize_threshold,
        )

    except Exception:
        log.exception("Mass-additions run failed")
        return 1

    log.info("——— Summary ———")
    log.info("  Total       : %s", result.get("total", 0))
    log.info("  Auto-update : %s", result.get("auto_update", 0))
    log.info("  Exceptions  : %s", result.get("exception", 0))
    log.info("Proposed updates : %s", out_dir / "proposed_updates.csv")
    log.info("Exceptions       : %s", out_dir / "exceptions.csv")
    log.info("Audit log        : %s", out_dir / "audit.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
