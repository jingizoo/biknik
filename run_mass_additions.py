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
from dataclasses import replace as _dc_replace
from datetime import datetime
from pathlib import Path

# Ensure the package is importable when running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src" / "main" / "python"))

from ofam_mass_additions.cmdb.servicenow_client import ServiceNowConfig  # noqa: E402
from ofam_mass_additions.cmdb.bip_translations import (  # noqa: E402
    BipTranslationsConfig,
    BipTranslationsLoader,
)
from ofam_mass_additions.exceptions import ConfigError  # noqa: E402
from ofam_mass_additions.publishers.gcs_publisher import GCSAuditPublisher  # noqa: E402
from ofam_mass_additions.publishers.slack_publisher import (  # noqa: E402
    PagerDutyPublisher,
    SlackPublisher,
)
from ofam_mass_additions.runner.cycle import seed_oracle_rows_from_csv  # noqa: E402
from ofam_mass_additions.runner.live_run import LiveRunResult, run_live  # noqa: E402


def _load_config(path: str) -> dict:
    """Read + parse the config JSON, tolerating Windows-saved cp1252 files."""
    raw_bytes = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            raw = raw_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ConfigError(
            f"Config could not be decoded as utf-8 or cp1252: {path}"
        )
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
        from ofam_mass_additions.oracle.discovery import build_discovery
        from ofam_mass_additions.oracle.fusion_client import (
            FusionFaClient,
            FusionFaConfig,
        )

        log.info("Building real Fusion FA client (base_url=%s)", oracle_block.get("base_url"))
        token_provider = build_token_provider(fusion_auth_block) if fusion_auth_block else None
        discovery = build_discovery(oracle_block.get("discovery"))
        log.info("Mass-addition discovery: %s", type(discovery).__name__)
        return FusionFaClient(
            FusionFaConfig.from_dict(oracle_block),
            discovery=discovery,
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
    p.add_argument(
        "--debug-dump",
        action="store_true",
        help=(
            "Log each parsed mass-addition row + rule decision + CMDB hit. "
            "Use when rows are getting skipped and you need to see why."
        ),
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

    config: dict | None = None
    result: LiveRunResult | None = None
    try:
        config = _load_config(args.config)

        sn_block = config.get("servicenow")
        if not sn_block:
            raise ConfigError("Config must include a 'servicenow' block.")
        sn_cfg = ServiceNowConfig.from_dict(sn_block)
        if args.debug_dump:
            sn_cfg = _dc_replace(sn_cfg, debug_dump=True)

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

        cmdb_overrides = config.get("cmdb_overrides") or {}
        oracle_translations = _load_translations(
            config, pilot_book, config.get("fusion_auth"), log
        )

        result = run_live(
            oracle_client=oracle_client,
            servicenow_config=sn_cfg,
            output_dir=out_dir,
            run_mode=run_mode,
            pilot_book=pilot_book,
            pilot_region=pilot_region,
            capitalize_threshold=capitalize_threshold,
            debug_dump=args.debug_dump,
            cmdb_overrides=cmdb_overrides,
            oracle_translations=oracle_translations,
        )

        _publish_optional_sinks(config, result, log)

    except Exception as exc:
        log.exception("Mass-additions run failed")
        if config is not None:
            _trigger_pagerduty_hard_error(config, exc, log)
        return 1

    counts = result.counts
    log.info("——— Summary ———")
    log.info("  Total       : %s", counts.get("total", 0))
    log.info("  Auto-update : %s", counts.get("auto_update", 0))
    log.info("  Exceptions  : %s", counts.get("exception", 0))
    log.info("Proposed updates : %s", out_dir / "proposed_updates.csv")
    log.info("Exceptions       : %s", out_dir / "exceptions.csv")
    log.info("Audit log        : %s", out_dir / "audit.jsonl")
    return 0


def _load_translations(
    config: dict,
    pilot_book: str,
    fusion_auth_block: dict | None,
    log: logging.Logger,
) -> dict:
    """Return oracle_translations from a live BIP report or the static JSON block.

    Priority:
      1. ``translations_bip`` block in config → call the BIP report
         (overrides any static ``oracle_translations`` block).
      2. ``oracle_translations`` block → use as-is (existing behaviour).
    """
    bip_block = config.get("translations_bip")
    if bip_block:
        bip_cfg = BipTranslationsConfig.from_dict(
            {"book_type_code": pilot_book, **bip_block}
        )
        token_provider = None
        if fusion_auth_block:
            from ofam_mass_additions.fusion_token_provider import build_token_provider
            token_provider = build_token_provider(fusion_auth_block)
        return BipTranslationsLoader(bip_cfg, token_provider=token_provider).load()
    return config.get("oracle_translations") or {}


def _pagerduty_routing_key_available(pd_block: dict) -> bool:
    """True if PD config will yield a routing key (literal or via env var).

    Treats a configured ``routing_key_env`` whose env var is empty as
    "not available" so the runner doesn't try to instantiate
    ``PagerDutyPublisher`` and crash on a ``ValueError``.
    """
    if pd_block.get("routing_key"):
        return True
    env_var = pd_block.get("routing_key_env")
    if env_var and os.getenv(str(env_var), "").strip():
        return True
    return False


def _publish_optional_sinks(
    config: dict, result: LiveRunResult, log: logging.Logger
) -> None:
    """Run Slack / GCS / PagerDuty publishers when their config blocks exist."""
    gcs_block = config.get("gcs")
    if gcs_block:
        try:
            GCSAuditPublisher.from_dict(gcs_block).publish(result)
        except Exception:
            log.exception("Failed to publish to GCS (non-fatal)")

    slack_block = config.get("slack")
    if slack_block and (slack_block.get("webhook_url") or slack_block.get("webhook_url_env")):
        try:
            SlackPublisher.from_dict(slack_block).publish(result)
        except Exception:
            log.exception("Failed to send Slack notification (non-fatal)")

    pd_block = config.get("pagerduty")
    if pd_block and _pagerduty_routing_key_available(pd_block):
        try:
            PagerDutyPublisher.from_dict(pd_block).check_and_trigger(result)
        except Exception:
            log.exception("Failed to check PagerDuty threshold (non-fatal)")


def _trigger_pagerduty_hard_error(
    config: dict, error: Exception, log: logging.Logger
) -> None:
    pd_block = config.get("pagerduty") or {}
    if not _pagerduty_routing_key_available(pd_block):
        return
    try:
        PagerDutyPublisher.from_dict(pd_block).trigger_if_hard_error(error)
    except Exception:
        log.exception("Failed to trigger PagerDuty (non-fatal)")


if __name__ == "__main__":
    raise SystemExit(main())
