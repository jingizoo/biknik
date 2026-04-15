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

from ofam_asset_xfer.bip_client import BIPClient, BIPConfig
from ofam_asset_xfer.entity_resolver import EntityBookResolver
from ofam_asset_xfer.exceptions import ConfigError
from ofam_asset_xfer.fusion_sync import FusionIUSync, DFFConfig
from ofam_asset_xfer.gcs_publisher import GCSPublisherConfig, GCSResultPublisher
from ofam_asset_xfer.slack_publisher import SlackPublisher, PagerDutyPublisher
from ofam_asset_xfer.local_publisher import LocalResultPublisher
from ofam_asset_xfer.oracle_client import OracleConfig, OracleErpIntegrationsClient
from ofam_asset_xfer.pre_bip_dff import (
    PreBipDffConfig,
    PreBipDffUpdater,
    load_pre_bip_dff_requests,
)
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
    args = _build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    log = logging.getLogger("batch_entrypoint")

    # Execution flag: --execute wins, else dry-run (default).
    execute = args.execute and not args.dry_run
    dry_run = not execute

    try:
        store = ArtifactStore(base_uri=".")
        raw = store.read_text(args.config)
        config = json.loads(raw)

        # --- Build clients from config ---
        oracle_cfg = OracleConfig.from_dict(config.get("oracle", {}))
        fusion_client = OracleErpIntegrationsClient(oracle_cfg)

        bip_cfg = BIPConfig.from_dict(config.get("bip", {}))
        bip_client = BIPClient(bip_cfg)

        entity_map = config.get("entity_book_map", {})
        if not entity_map:
            raise ConfigError("Config must include non-empty 'entity_book_map'.")
        entity_resolver = EntityBookResolver(entity_map)

        books = config.get("books", [])
        if not books:
            raise ConfigError("Config must include non-empty 'books' list.")

        dff_config = None
        if config.get("dff_columns"):
            dff_config = DFFConfig(**config["dff_columns"])

        bip_params = config.get("bip_params")
        max_transfers = int(config.get("max_transfers", 500))

        # --- Optional pre-BIP DFF updates ---
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

            out_store = ArtifactStore(base_uri=args.out_dir)
            out_store.ensure_dir()
            out_store.write_json("pre_bip_dff_summary.json", pre_summary)
            out_store.write_json("pre_bip_dff_results.json", pre_summary.get("results", []))

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
        )
        summary = sync.run_full_sync(
            books=books,
            dry_run=dry_run,
            max_transfers=max_transfers,
            bip_params=bip_params,
        )

        # Write raw results (JSON)
        out_store = ArtifactStore(base_uri=args.out_dir)
        out_store.ensure_dir()
        out_store.write_json("summary.json", summary)
        out_store.write_json("results.json", summary.get("results", []))

        # Publish Tableau-friendly NDJSON (always local, optionally GCS)
        results_list = summary.get("results", [])

        local_pub = LocalResultPublisher(args.out_dir)
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

        counts = summary.get("counts", {})
        log.info("Completed: total=%s transferred=%s failed=%s dry_run=%s",
                 counts.get("total", 0), counts.get("transferred", 0),
                 counts.get("failed", 0), counts.get("dry_run", 0))
        return 0

    except Exception as exc:
        log.exception("Fatal error running job")
        pd_block = config.get("pagerduty") if "config" in dir() else None
        if pd_block:
            try:
                pd_pub = PagerDutyPublisher.from_dict(pd_block)
                pd_pub.trigger_if_hard_error(exc)
            except Exception:
                log.exception("Failed to trigger PagerDuty (non-fatal)")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
