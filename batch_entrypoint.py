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
from ofam_asset_xfer.fusion_sync import (
    DFFConfig,
    FusionIUSync,
    build_account_combination_client_from_config,
)
from ofam_asset_xfer.fusion_token_provider import build_token_provider
from ofam_asset_xfer.gcs_publisher import GCSPublisherConfig, GCSResultPublisher
from ofam_asset_xfer.local_publisher import LocalResultPublisher
from ofam_asset_xfer.oracle_client import OracleConfig, OracleErpIntegrationsClient
from ofam_asset_xfer.pre_bip_dff import (
    PreBipDffConfig,
    PreBipDffUpdater,
    load_pre_bip_dff_requests,
)
from ofam_asset_xfer.slack_publisher import PagerDutyPublisher, SlackPublisher
from ofam_asset_xfer.store import ArtifactStore


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="OFAM IU Asset Transfer Automation (BIP-driven).")
    p.add_argument("--config", required=True, help="Path/URI to config JSON.")
    p.add_argument("--out-dir", required=True, help="Output directory/URI for artifacts.")
    p.add_argument(
        "--dry-run", action="store_true", help="Reads/validation only (no writes to Fusion)."
    )
    p.add_argument(
        "--execute", action="store_true", help="Post transactions to Fusion. Overrides --dry-run."
    )
    p.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Logging level (INFO, DEBUG, ...).",
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
    return p


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for the CDX batch job."""
    args = _build_arg_parser().parse_args(argv)

    effective_log_level = "DEBUG" if args.debug else args.log_level
    logging.basicConfig(
        level=getattr(logging, str(effective_log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    log = logging.getLogger("batch_entrypoint")

    debug_dir: str | None = None
    if args.debug:
        from pathlib import Path as _P

        debug_dir = args.debug_dir or str(_P(args.out_dir) / "debug-dumps")

    # Execution flag: --execute wins, else dry-run (default).
    execute = args.execute and not args.dry_run
    dry_run = not execute

    try:
        store = ArtifactStore(base_uri=".")
        raw = store.read_text(args.config)
        config = json.loads(raw)

        # --- Build Fusion token provider (keytab/static) ---
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

        dff_config = None
        if config.get("dff_columns"):
            dff_config = DFFConfig(**config["dff_columns"])

        bip_params = config.get("bip_params")
        max_transfers = int(config.get("max_transfers", 500))

        # --- Optional pre-BIP DFF updates ---
        # Block may be inline OR loaded from a separate file via
        # ``pre_bip_dff_updates_file`` (path is resolved relative to the
        # main config file when not absolute).
        pre_bip_file = config.get("pre_bip_dff_updates_file")
        if pre_bip_file:
            from pathlib import Path as _Path

            pre_path = _Path(pre_bip_file)
            if not pre_path.is_absolute():
                pre_path = _Path(args.config).resolve().parent / pre_bip_file
            log.info("Loading pre-BIP DFF updates from %s", pre_path)
            pre_bip_block = json.loads(_Path(pre_path).read_text()) or {}
        else:
            pre_bip_block = config.get("pre_bip_dff_updates") or {}
        if pre_bip_block.get("enabled"):
            pre_cfg = PreBipDffConfig.from_dict(pre_bip_block)
            pre_requests = load_pre_bip_dff_requests(pre_bip_block)
            if not pre_requests:
                raise ConfigError("pre_bip_dff_updates.enabled=true but no requests were supplied.")
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

        # Optional Option B account-combination-service client.
        ac_client, ledger_map = build_account_combination_client_from_config(
            config.get("account_combination_service"),
            token_provider=token_provider,
        )

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
            post_transfer_attribute_update=config.get("post_transfer_attribute_update") or {},
            account_combination_client=ac_client,
            ledger_name_by_book=ledger_map,
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
        log.info(
            "Completed: total=%s transferred=%s failed=%s dry_run=%s",
            counts.get("total", 0),
            counts.get("transferred", 0),
            counts.get("failed", 0),
            counts.get("dry_run", 0),
        )
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
