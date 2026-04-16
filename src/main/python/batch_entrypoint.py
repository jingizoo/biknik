#!/usr/bin/env python3
"""CDX Batch entrypoint for OFAM IU Asset Transfer (BIP-driven discovery).

Discovers all pending Inter-Unit transfers from a BI Publisher report and
executes them.  No individual asset IDs or numbers are required — the BIP
report (called with just the book code) returns all eligible IU assets.

Example:
  python batch_entrypoint.py --config /tmp/config.json --out-dir /tmp/out --dry-run
"""

from __future__ import annotations

import json
import logging
import os
import typer

from ofam_asset_xfer.bip_client import BIPClient, BIPConfig
from ofam_asset_xfer.entity_resolver import EntityBookResolver
from ofam_asset_xfer.exceptions import ConfigError
from ofam_asset_xfer.fusion_sync import FusionIUSync, DFFConfig
from ofam_asset_xfer.fusion_token_provider import build_token_provider
from ofam_asset_xfer.gcs_publisher import GCSPublisherConfig, GCSResultPublisher
from ofam_asset_xfer.local_publisher import LocalResultPublisher
from ofam_asset_xfer.slack_publisher import SlackPublisher, PagerDutyPublisher
from ofam_asset_xfer.oracle_client import OracleConfig, OracleErpIntegrationsClient
from ofam_asset_xfer.store import ArtifactStore

app = typer.Typer(help="OFAM IU Asset Transfer Automation (BIP-driven).")


@app.command()
def main(
    config: str = typer.Option(..., help="Path/URI to config JSON."),
    out_dir: str = typer.Option(..., help="Output directory/URI for artifacts."),
    dry_run: bool = typer.Option(False, help="Reads/validation only (no writes to Fusion)."),
    execute: bool = typer.Option(False, help="Post transactions to Fusion. Overrides --dry-run."),
    log_level: str = typer.Option(
        os.getenv("LOG_LEVEL", "INFO"), help="Logging level (INFO, DEBUG, ...)."
    ),
) -> None:
    logging.basicConfig(
        level=getattr(logging, str(log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    log = logging.getLogger("batch_entrypoint")

    # Execution flag: --execute wins, else dry-run (default).
    should_execute = execute and not dry_run

    try:
        store = ArtifactStore(base_uri=".")
        raw = store.read_text(config)
        cfg = json.loads(raw)

        # --- Build Fusion token provider (keytab/static) ---
        token_provider = build_token_provider(cfg.get("fusion_auth"))

        # --- Build clients from config ---
        oracle_cfg = OracleConfig.from_dict(cfg.get("oracle", {}))
        fusion_client = OracleErpIntegrationsClient(
            oracle_cfg, token_provider=token_provider
        )

        bip_cfg = BIPConfig.from_dict(cfg.get("bip", {}))
        bip_client = BIPClient(bip_cfg, token_provider=token_provider)

        entity_map = cfg.get("entity_book_map", {})
        if not entity_map:
            raise ConfigError("Config must include non-empty 'entity_book_map'.")
        entity_resolver = EntityBookResolver(entity_map)

        books = cfg.get("books", [])
        if not books:
            raise ConfigError("Config must include non-empty 'books' list.")

        dff_config = None
        if cfg.get("dff_columns"):
            dff_config = DFFConfig(**cfg["dff_columns"])

        bip_params = cfg.get("bip_params")
        max_transfers = int(cfg.get("max_transfers", 500))

        # --- Run ---
        sync = FusionIUSync(
            fusion_client,
            entity_resolver,
            bip_client,
            dff_config=dff_config,
            default_transfer_date=cfg.get("default_transfer_date"),
        )
        summary = sync.run_full_sync(
            books=books,
            dry_run=(not should_execute),
            max_transfers=max_transfers,
            bip_params=bip_params,
        )

        # Write raw results (JSON)
        out_store = ArtifactStore(base_uri=out_dir)
        out_store.ensure_dir()
        out_store.write_json("summary.json", summary)
        out_store.write_json("results.json", summary.get("results", []))

        # Publish Tableau-friendly NDJSON (always local, optionally GCS)
        results_list = summary.get("results", [])

        local_pub = LocalResultPublisher(out_dir)
        local_pub.publish(summary, results_list)

        gcs_block = cfg.get("gcs")
        if gcs_block:
            try:
                gcs_cfg = GCSPublisherConfig.from_dict(gcs_block)
                gcs_pub = GCSResultPublisher(gcs_cfg)
                gcs_pub.publish(summary, results_list)
            except Exception:
                log.exception("Failed to publish to GCS (non-fatal)")

        slack_block = cfg.get("slack")
        if slack_block:
            try:
                slack_pub = SlackPublisher.from_dict(slack_block)
                slack_pub.publish(summary, results_list)
            except Exception:
                log.exception("Failed to send Slack notification (non-fatal)")

        pd_block = cfg.get("pagerduty")
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

    except Exception as exc:
        log.exception("Fatal error running job")
        pd_block = cfg.get("pagerduty") if "cfg" in dir() else None
        if pd_block:
            try:
                pd_pub = PagerDutyPublisher.from_dict(pd_block)
                pd_pub.trigger_if_hard_error(exc)
            except Exception:
                log.exception("Failed to trigger PagerDuty (non-fatal)")
        raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
