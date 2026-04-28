"""Shared pipeline runner for the OFAM IU Asset Transfer job.

The CLI entrypoints (``batch_entrypoint.py`` for the K8s/Batch image, and
``run_local.py`` for local dev) parse args, configure logging, load the
config dict, and then call :func:`run_pipeline` here. All the actual work
— building clients, optional pre-BIP DFF stage, the main BIP-driven
transfer sweep, and result publishing — lives in this module so the two
entrypoints stay thin.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bip_client import BIPClient, BIPConfig
from .entity_resolver import EntityBookResolver
from .exceptions import ConfigError
from .fusion_sync import DFFConfig, FusionIUSync
from .fusion_token_provider import build_token_provider
from .gcs_publisher import GCSPublisherConfig, GCSResultPublisher
from .local_publisher import LocalResultPublisher
from .oracle_client import OracleConfig, OracleErpIntegrationsClient
from .pre_bip_dff import (
    PreBipDffConfig,
    PreBipDffUpdater,
    load_pre_bip_dff_requests,
)
from .slack_publisher import PagerDutyPublisher, SlackPublisher
from .store import ArtifactStore


@dataclass
class PipelineResult:
    """Outcome of a pipeline run.

    ``exit_code`` follows the original entrypoint convention: 0 success,
    2 hard error (caller may map to a different code if it wants).
    ``summary`` is None if the run hard-failed before the main sync ran.
    ``pre_summary`` is None if the pre-BIP DFF stage was skipped.
    """

    summary: dict[str, Any] | None
    pre_summary: dict[str, Any] | None
    exit_code: int


def run_pipeline(
    *,
    config: dict[str, Any],
    out_dir: str,
    dry_run: bool,
    log: logging.Logger,
    config_path: str | None = None,
) -> PipelineResult:
    """Run the full asset-transfer pipeline against ``config``.

    ``config_path`` (when provided) is used to resolve relative paths
    inside the config (e.g. ``pre_bip_dff_updates_file``).
    """
    pre_summary: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None

    try:
        token_provider = build_token_provider(config.get("fusion_auth"))

        oracle_cfg = OracleConfig.from_dict(config.get("oracle", {}))
        fusion_client = OracleErpIntegrationsClient(
            oracle_cfg, token_provider=token_provider
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
        execute = not dry_run

        out_store = ArtifactStore(base_uri=out_dir)
        out_store.ensure_dir()

        # --- Optional pre-BIP DFF updates ---
        # Block may be inline OR loaded from a separate file via
        # ``pre_bip_dff_updates_file`` (path is resolved relative to the
        # main config file when not absolute).
        pre_bip_block = _load_pre_bip_block(config, config_path, log)
        if pre_bip_block.get("enabled"):
            pre_cfg = PreBipDffConfig.from_dict(pre_bip_block)
            pre_requests = load_pre_bip_dff_requests(pre_bip_block)
            if not pre_requests:
                raise ConfigError(
                    "pre_bip_dff_updates.enabled=true but no requests were supplied."
                )
            updater = PreBipDffUpdater(fusion_client, pre_cfg)
            pre_summary = updater.apply_updates(pre_requests, dry_run=dry_run)

            out_store.write_json("pre_bip_dff_summary.json", pre_summary)
            out_store.write_json(
                "pre_bip_dff_results.json", pre_summary.get("results", [])
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
                log.info(
                    "Sleeping %.1f second(s) before running BIP so updated DFF values are visible",
                    delay,
                )
                time.sleep(delay)

        # --- Main sync ---
        sync = FusionIUSync(
            fusion_client,
            entity_resolver,
            bip_client,
            dff_config=dff_config,
            default_transfer_date=config.get("default_transfer_date"),
            blocked_books=config.get("blocked_books") or [],
        )
        summary = sync.run_full_sync(
            books=books,
            dry_run=dry_run,
            max_transfers=max_transfers,
            bip_params=bip_params,
        )

        out_store.write_json("summary.json", summary)
        out_store.write_json("results.json", summary.get("results", []))

        results_list = summary.get("results", [])

        # Always publish locally (NDJSON for Tableau).
        local_pub = LocalResultPublisher(out_dir)
        local_pub.publish(summary, results_list)

        # Optional sinks — non-fatal on failure.
        gcs_block = config.get("gcs")
        if gcs_block:
            try:
                gcs_pub = GCSResultPublisher(GCSPublisherConfig.from_dict(gcs_block))
                gcs_pub.publish(summary, results_list)
            except Exception:
                log.exception("Failed to publish to GCS (non-fatal)")

        slack_block = config.get("slack")
        if slack_block:
            try:
                SlackPublisher.from_dict(slack_block).publish(summary, results_list)
            except Exception:
                log.exception("Failed to send Slack notification (non-fatal)")

        pd_block = config.get("pagerduty")
        if pd_block:
            try:
                PagerDutyPublisher.from_dict(pd_block).check_and_trigger(
                    summary, results_list
                )
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
        return PipelineResult(summary=summary, pre_summary=pre_summary, exit_code=0)

    except Exception as exc:
        log.exception("Fatal error running job")
        pd_block = config.get("pagerduty")
        if pd_block:
            try:
                PagerDutyPublisher.from_dict(pd_block).trigger_if_hard_error(exc)
            except Exception:
                log.exception("Failed to trigger PagerDuty (non-fatal)")
        return PipelineResult(
            summary=summary, pre_summary=pre_summary, exit_code=2
        )


def _load_pre_bip_block(
    config: dict[str, Any],
    config_path: str | None,
    log: logging.Logger,
) -> dict[str, Any]:
    """Return the pre-BIP DFF block, loading it from disk if referenced."""
    pre_bip_file = config.get("pre_bip_dff_updates_file")
    if not pre_bip_file:
        return config.get("pre_bip_dff_updates") or {}

    pre_path = Path(pre_bip_file)
    if not pre_path.is_absolute() and config_path:
        pre_path = Path(config_path).resolve().parent / pre_bip_file
    log.info("Loading pre-BIP DFF updates from %s", pre_path)
    return json.loads(Path(pre_path).read_text()) or {}
