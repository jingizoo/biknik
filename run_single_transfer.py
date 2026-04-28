#!/usr/bin/env python3
"""Transfer a single asset to a specific book/location.

Prompts interactively for transfer parameters, then uses the existing
OFAM library (oracle_client, fusion_ops) to call getAssetInformation,
build the transfer payload, and POST it to Fusion.

Usage:
  # Interactive prompts (dry-run by default):
  python run_single_transfer.py --config config.json

  # Execute for real:
  python run_single_transfer.py --config config.json --execute

Environment variables:
  FUSION_JWT          Bearer token for Oracle Fusion (if using bearer_token_env)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

# Ensure the package is importable when running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src" / "main" / "python"))

from ofam_asset_xfer.exceptions import ConfigError, FusionApiError  # noqa: E402
from ofam_asset_xfer.fusion_ops import (  # noqa: E402
    build_book_transfer_params,
    build_same_book_transfer_params,
    get_asset_information,
)
from ofam_asset_xfer.fusion_token_provider import build_token_provider  # noqa: E402
from ofam_asset_xfer.oracle_client import OracleConfig, OracleErpIntegrationsClient  # noqa: E402


def _load_config(path: str) -> dict:
    raw = Path(path).read_text()
    try:
        return json.loads(raw)
    except Exception as e:
        raise ConfigError(f"Config is not valid JSON: {path}") from e


def _prompt(label: str, default: str | None = None, required: bool = True) -> str:
    """Prompt user for input with optional default."""
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{label}{suffix}: ").strip()
        if not value and default:
            return default
        if value:
            return value
        if not required:
            return ""
        print(f"  ** {label} is required. Please enter a value.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Transfer a single asset to a specific book/location (interactive).",
    )
    p.add_argument(
        "--config", required=True,
        help="Path to JSON config file (must contain 'oracle' connection settings).",
    )
    p.add_argument("--dry-run", action="store_true", help="Build payload only (default).")
    p.add_argument("--execute", action="store_true", help="Actually POST to Fusion.")
    p.add_argument(
        "--out-file", default=None,
        help="Write result JSON to this file. Default: stdout.",
    )
    p.add_argument(
        "--log-level", default=os.getenv("LOG_LEVEL", "INFO"),
        help="Logging level (default: INFO).",
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s — %(message)s",
    )
    log = logging.getLogger("run_single_transfer")

    execute = args.execute and not args.dry_run
    dry_run = not execute

    # --- Interactive prompts ---
    print("\n=== Single Asset Transfer ===\n")
    asset_number = _prompt("Asset Number")
    source_book = _prompt("Source Book Type Code")
    target_book = _prompt("Target Book Type Code")
    target_location_id = _prompt("Target Location ID", required=False) or None
    asset_id = _prompt("Asset ID (if known, otherwise leave blank)", required=False) or None
    effective_date = _prompt("Effective Date (YYYY-MM-DD)", default=date.today().isoformat())

    is_cross_book = source_book.strip().upper() != target_book.strip().upper()
    request_id = f"SINGLE_XFER_{asset_number}_{int(time.time())}"

    transfer_type = "cross-book (bookTransfer)" if is_cross_book else "same-book (transferAsset)"
    mode = "EXECUTE" if execute else "DRY-RUN"

    print()
    log.info("Asset          : %s", asset_number)
    log.info("Source book    : %s", source_book)
    log.info("Target book    : %s", target_book)
    log.info("Target location: %s", target_location_id or "(none)")
    log.info("Effective date : %s", effective_date)
    log.info("Transfer type  : %s", transfer_type)
    log.info("Mode           : %s", mode)

    if not is_cross_book and not target_location_id:
        log.error("Same-book transfer requires a Target Location ID.")
        return 1

    try:
        config = _load_config(args.config)

        # Build Fusion client (keytab/static token provider, matches batch path)
        token_provider = build_token_provider(config.get("fusion_auth"))
        oracle_cfg = OracleConfig.from_dict(config.get("oracle", {}))
        client = OracleErpIntegrationsClient(oracle_cfg, token_provider=token_provider)

        # Step 1: Get current asset state
        log.info("Calling getAssetInformation for asset=%s book=%s ...",
                 asset_number, source_book)
        _raw, _pl, state = get_asset_information(
            client,
            source_book,
            asset_number,
            asset_id=asset_id,
        )
        log.info("Asset state: id=%s, distributions=%d, cost=%s",
                 state.asset_id, len(state.distribution_ids), state.cost)

        # Step 2: Build transfer params
        overrides = {}
        if target_location_id:
            overrides["location_ccid"] = target_location_id

        if is_cross_book:
            params = build_book_transfer_params(
                state=state,
                dest_book_type_code=target_book,
                effective_date=effective_date,
                overrides=overrides,
                request_id=request_id,
            )
            handle = "bookTransfer"
        else:
            params, is_noop = build_same_book_transfer_params(
                state=state,
                effective_date=effective_date,
                overrides=overrides,
                request_id=request_id,
            )
            if is_noop:
                log.info("NOOP: no distribution changes needed — asset already at target.")
                result = {"status": "NOOP", "asset_number": asset_number}
                _write_result(result, args.out_file)
                return 0
            handle = "transferAsset"

        log.info("Payload built for processTransaction-%s", handle)

        # Step 3: Execute or dry-run
        if dry_run:
            log.info("DRY-RUN: not posting to Fusion.")
            result = {
                "status": "DRY_RUN",
                "asset_number": asset_number,
                "source_book": source_book,
                "target_book": target_book,
                "effective_date": effective_date,
                "handle": handle,
                "planned_params": params,
            }
        else:
            log.info("Posting processTransaction-%s ...", handle)
            raw, pl = client.process_transaction(handle, params)
            status_code = str(pl.get("X_RETURN_STATUS") or "").strip()
            success = status_code == "S"

            result = {
                "status": "TRANSFERRED" if success else "FAILED",
                "asset_number": asset_number,
                "source_book": source_book,
                "target_book": target_book,
                "effective_date": effective_date,
                "handle": handle,
                "fusion_response": pl,
                "error": None if success else f"X_RETURN_STATUS={status_code}",
            }

            if success:
                log.info("Transfer successful.")
            else:
                log.error("Transfer FAILED: %s", pl)

        _write_result(result, args.out_file)
        return 0 if result["status"] != "FAILED" else 1

    except FusionApiError as e:
        log.error("Fusion API error: %s", e)
        return 1
    except Exception:
        log.exception("Unexpected error")
        return 2


def _write_result(result: dict, out_file: str | None) -> None:
    text = json.dumps(result, indent=2, default=str)
    if out_file:
        Path(out_file).write_text(text)
        logging.getLogger("run_single_transfer").info("Result written to %s", out_file)
    else:
        print(text)


if __name__ == "__main__":
    raise SystemExit(main())
