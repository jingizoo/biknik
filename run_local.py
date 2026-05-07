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

from ofam_asset_xfer.bip_client import BIPClient, BIPConfig  # noqa: E402
from ofam_asset_xfer.entity_resolver import EntityBookResolver  # noqa: E402
from ofam_asset_xfer.exceptions import ConfigError  # noqa: E402
from ofam_asset_xfer.fusion_sync import FusionIUSync, DFFConfig  # noqa: E402
from ofam_asset_xfer.fusion_token_provider import build_token_provider  # noqa: E402
from ofam_asset_xfer.gcs_publisher import GCSPublisherConfig, GCSResultPublisher  # noqa: E402
from ofam_asset_xfer.slack_publisher import SlackPublisher, PagerDutyPublisher  # noqa: E402
from ofam_asset_xfer.local_publisher import LocalResultPublisher  # noqa: E402
from ofam_asset_xfer.oracle_client import OracleConfig, OracleErpIntegrationsClient  # noqa: E402
from ofam_asset_xfer.pre_bip_dff import (  # noqa: E402
    PreBipDffConfig,
    PreBipDffUpdater,
    load_pre_bip_dff_requests,
)


def _load_config(path: str) -> dict:
    raw = Path(path).read_text()
    try:
        return json.loads(raw)
    except Exception as e:
        raise ConfigError(f"Config is not valid JSON: {path}") from e


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for the local runner."""
    p = argparse.ArgumentParser(
        description="Run OFAM IU asset transfers locally via BIP report discovery.",
    )
    p.add_argument(
        "--config",
        required=True,
        help="Path to JSON config file (books, entity map, BIP + Oracle settings).",
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
    p.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Enable DEBUG logging and dump every Fusion request/response "
            "(auth-redacted) to <out-dir>/debug-dumps/ for offline inspection."
        ),
    )
    p.add_argument(
        "--debug-dir",
        default=None,
        help="Override directory for --debug dumps (default: <out-dir>/debug-dumps/).",
    )
    args = p.parse_args(argv)

    effective_log_level = "DEBUG" if args.debug else args.log_level
    log_level = getattr(logging, effective_log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-5s %(name)s — %(message)s",
    )
    log = logging.getLogger("run_local")

    # Optionally attach Google Cloud Logging so all log.* calls are also
    # shipped to GCP.  Requires ``google-cloud-logging`` and either
    # GOOGLE_APPLICATION_CREDENTIALS or Application Default Credentials.
    if os.getenv("ENABLE_CLOUD_LOGGING", "").strip().lower() in ("1", "true", "yes"):
        try:
            import google.cloud.logging as cloud_logging  # type: ignore[import-untyped]

            cl_client = cloud_logging.Client()
            cl_client.setup_logging(log_level=log_level)
            log.info("Google Cloud Logging enabled")
        except Exception:
            log.warning("Could not initialise Google Cloud Logging — continuing with local logs only", exc_info=True)

    # Resolve output dir.
    if args.out_dir:
        out_dir = args.out_dir
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = str(Path("output") / ts)
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    debug_dir: str | None = None
    if args.debug:
        debug_dir = args.debug_dir or str(Path(out_dir) / "debug-dumps")

    # Execution flag: --execute wins, else dry-run (default).
    execute = args.execute and not args.dry_run
    dry_run = not execute

    mode = "EXECUTE" if execute else "DRY-RUN"
    log.info("Config  : %s", args.config)
    log.info("Output  : %s", out_dir)
    log.info("Mode    : %s", mode)

    try:
        config = _load_config(args.config)

        # --- Build Fusion token provider (keytab/static) ---
        # If ``fusion_auth`` is present, it drives authentication for both
        # Oracle REST and BIP SOAP clients.  Otherwise the clients fall back
        # to the legacy ``bearer_token_env`` inside oracle/bip blocks.
        token_provider = build_token_provider(config.get("fusion_auth"))

        # --- Build clients from config ---
        oracle_cfg = OracleConfig.from_dict(config.get("oracle", {}))
        fusion_client = OracleErpIntegrationsClient(
            oracle_cfg, token_provider=token_provider, debug_dir=debug_dir
        )

        bip_cfg = BIPConfig.from_dict(config.get("bip", {}))
        bip_client = BIPClient(bip_cfg, token_provider=token_provider)

        entity_map = config.get("entity_book_map", {})
        if not entity_map:
            raise ConfigError("Config must include non-empty 'entity_book_map'.")
        entity_resolver = EntityBookResolver(entity_map)

        books = config.get("books", [])
        if not books:
            raise ConfigError("Config must include non-empty 'books' list.")

        # Optional DFF column overrides
        dff_config = None
        if config.get("dff_columns"):
            dff_config = DFFConfig(**config["dff_columns"])

        # Optional BIP report parameters (e.g. P_BOOK_TYPE_CODE)
        bip_params = config.get("bip_params")

        max_transfers = int(config.get("max_transfers", 500))

        # --- Optional pre-BIP DFF updates ---
        # Block may be inline OR loaded from a separate file via
        # ``pre_bip_dff_updates_file`` (path is resolved relative to the
        # main config file when not absolute).
        pre_bip_file = config.get("pre_bip_dff_updates_file")
        if pre_bip_file:
            pre_path = Path(pre_bip_file)
            if not pre_path.is_absolute():
                pre_path = Path(args.config).resolve().parent / pre_bip_file
            log.info("Loading pre-BIP DFF updates from %s", pre_path)
            pre_bip_block = json.loads(Path(pre_path).read_text()) or {}
        else:
            pre_bip_block = config.get("pre_bip_dff_updates") or {}
        if pre_bip_block.get("enabled"):
            pre_cfg = PreBipDffConfig.from_dict(pre_bip_block)
            pre_requests = load_pre_bip_dff_requests(pre_bip_block)
            if not pre_requests:
                raise ConfigError(
                    "pre_bip_dff_updates.enabled=true but no requests were supplied."
                )
            updater = PreBipDffUpdater(fusion_client, pre_cfg)
            pre_summary = updater.apply_updates(pre_requests, dry_run=dry_run)

            pre_summary_path = Path(out_dir) / "pre_bip_dff_summary.json"
            pre_summary_path.write_text(json.dumps(pre_summary, indent=2, default=str))

            pre_results_path = Path(out_dir) / "pre_bip_dff_results.json"
            pre_results_path.write_text(
                json.dumps(pre_summary.get("results", []), indent=2, default=str)
            )

            pre_counts = pre_summary.get("counts", {})
            log.info(
                "Pre-BIP DFF stage complete: total=%s updated=%s failed=%s dry_run=%s",
                pre_counts.get("total", 0),
                pre_counts.get("updated", 0),
                pre_counts.get("failed", 0),
                pre_counts.get("dry_run", 0),
            )

            delay = float(pre_cfg.bip_visibility_delay_seconds)
            if execute and delay > 0 and pre_counts.get("updated", 0) > 0:
                import time

                log.info(
                    "Sleeping %.1f second(s) before running BIP so updated DFF values are visible",
                    delay,
                )
                time.sleep(delay)

        # --- Run ---
        sync = FusionIUSync(
            fusion_client,
            entity_resolver,
            bip_client,
            dff_config=dff_config,
            default_transfer_date=config.get("default_transfer_date"),
            blocked_books=config.get("blocked_books") or [],
            transfer_overrides=config.get("transfer_overrides") or {},
            transfer_overrides_by_book=config.get("transfer_overrides_by_book") or {},
        )
        summary = sync.run_full_sync(
            books=books,
            dry_run=dry_run,
            max_transfers=max_transfers,
            bip_params=bip_params,
        )

        # Write raw results (JSON)
        summary_path = Path(out_dir) / "summary.json"
        summary_path.write_text(json.dumps(summary, indent=2, default=str))

        results_path = Path(out_dir) / "results.json"
        results_path.write_text(
            json.dumps(summary.get("results", []), indent=2, default=str)
        )

        # Publish Tableau-friendly NDJSON (always local, optionally GCS)
        results_list = summary.get("results", [])

        local_pub = LocalResultPublisher(out_dir)
        local_pub.publish(summary, results_list)

        gcs_block = config.get("gcs")
        if gcs_block:
            try:
                gcs_cfg = GCSPublisherConfig.from_dict(gcs_block)
                gcs_pub = GCSResultPublisher(gcs_cfg)
                gcs_pub.publish(summary, results_list)
            except Exception:
                log.exception("Failed to publish to GCS (non-fatal)")

        slack_block = config.get("slack")
        if slack_block:
            try:
                slack_pub = SlackPublisher.from_dict(slack_block)
                slack_pub.publish(summary, results_list)
            except Exception:
                log.exception("Failed to send Slack notification (non-fatal)")

        pd_block = config.get("pagerduty")
        if pd_block:
            try:
                pd_pub = PagerDutyPublisher.from_dict(pd_block)
                pd_pub.check_and_trigger(summary, results_list)
            except Exception:
                log.exception("Failed to check PagerDuty threshold (non-fatal)")

    except Exception as exc:
        log.exception("Job failed")
        pd_block = config.get("pagerduty") if "config" in dir() else None
        if pd_block:
            try:
                pd_pub = PagerDutyPublisher.from_dict(pd_block)
                pd_pub.trigger_if_hard_error(exc)
            except Exception:
                log.exception("Failed to trigger PagerDuty (non-fatal)")
        return 1

    # Print summary.
    counts = summary.get("counts", {})
    log.info("——— Summary ———")
    log.info("  Total       : %s", counts.get("total", 0))
    log.info("  Transferred : %s", counts.get("transferred", 0))
    log.info("  Failed      : %s", counts.get("failed", 0))
    log.info("  Dry-run     : %s", counts.get("dry_run", 0))
    log.info("Full results  : %s", Path(out_dir) / "results.json")
    log.info("NDJSON logs   : %s", Path(out_dir) / date.today().isoformat())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
