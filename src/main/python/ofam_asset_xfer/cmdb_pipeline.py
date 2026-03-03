"""Production CMDB → FA sync pipeline entry point.

Designed to run as a CDX Batch job. Reads configuration from a JSON file,
connects to CMDB and Oracle Fusion, identifies all location mismatches,
and executes transfers (or dry-runs).

Config JSON shape:
{
    "oracle": { ... },          // Same as existing sample_requests.json oracle block
    "cmdb": {
        "base_url": "https://instance.service-now.com",
        "username_env": "CMDB_USER",
        "password_env": "CMDB_PASS",
        "table": "alm_hardware"
    },
    "sync": {
        "country_book_map": { "US": "US CORP BOOK", "UK": "UK CORP BOOK", ... },
        "location_resource": "fixedAssetLocations",
        "max_transfers": 500,
        "cmdb_query": null,
        "effective_date": null    // null = today
    },
    "execute": false
}
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict, Optional

from .cmdb_client import CMDBClient, CMDBConfig
from .cmdb_sync import CMDBFASync
from .exceptions import ConfigError
from .location_resolver import LocationResolver, build_resolver_from_config
from .oracle_client import OracleConfig, OracleErpIntegrationsClient
from .store import ArtifactStore

log = logging.getLogger(__name__)


def run_cmdb_sync_pipeline(
    config_uri: str,
    out_dir_uri: str,
    cli_execute: Optional[bool] = None,
) -> None:
    """Main entry point for the CMDB → FA sync pipeline.

    Args:
        config_uri: Path/URI to the JSON config file.
        out_dir_uri: Output directory/URI for results and audit artifacts.
        cli_execute: Override for execute flag (None = use config value).
    """
    store = ArtifactStore(base_uri=".")
    try:
        raw = store.read_text(config_uri)
    except Exception as e:
        raise ConfigError(f"Failed to read config: {config_uri}") from e
    try:
        config = json.loads(raw)
    except Exception as e:
        raise ConfigError(f"Config is not valid JSON: {config_uri}") from e

    out_store = ArtifactStore(base_uri=out_dir_uri)
    out_store.ensure_dir()
    out_store.ensure_dir("audit")

    # Build clients
    oracle_cfg = OracleConfig.from_dict(config.get("oracle", {}))
    fusion_client = OracleErpIntegrationsClient(oracle_cfg)

    cmdb_cfg = CMDBConfig.from_dict(config.get("cmdb", {}))
    cmdb_client = CMDBClient(cmdb_cfg)

    # Build location resolver
    sync_config = config.get("sync", {})
    resolver = build_resolver_from_config(fusion_client, sync_config)

    # Determine execution mode
    execute_cfg = bool(config.get("execute", False))
    execute = execute_cfg if cli_execute is None else bool(cli_execute)

    # Build sync engine
    search_books_cfg = sync_config.get("search_books")
    sync_engine = CMDBFASync(
        cmdb_client=cmdb_client,
        fusion_client=fusion_client,
        location_resolver=resolver,
        search_books=search_books_cfg if isinstance(search_books_cfg, list) else None,
    )

    # Run
    max_transfers = int(sync_config.get("max_transfers", 500))
    cmdb_query = sync_config.get("cmdb_query")
    effective_date = sync_config.get("effective_date")

    log.info(
        "CMDB→FA sync pipeline starting: execute=%s, max=%d, date=%s",
        execute, max_transfers, effective_date or "today",
    )

    summary = sync_engine.run_full_sync(
        dry_run=not execute,
        effective_date=effective_date,
        cmdb_query=cmdb_query,
        max_transfers=max_transfers,
    )

    # Write outputs
    out_store.write_json("sync_summary.json", summary)
    out_store.write_json("sync_results.json", summary.get("results", []))

    log.info("Pipeline complete. Results: %s", summary.get("counts", {}))
    log.info("Output written to: %s", out_dir_uri)
