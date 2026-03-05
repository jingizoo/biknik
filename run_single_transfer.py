#!/usr/bin/env python3
"""Transfer a single asset to a specific book/location.

Uses the existing OFAM library (oracle_client, fusion_ops) to call
getAssetInformation, build the transfer payload, and POST it to Fusion.

Usage:
  # Cross-book transfer (dry-run by default):
  python run_single_transfer.py --config config.json \
      --asset-number 101533 \
      --source-book "US CORP BOOK" \
      --target-book "UK CORP BOOK"

  # Same-book location transfer:
  python run_single_transfer.py --config config.json \
      --asset-number 101533 \
      --source-book "US CORP BOOK" \
      --target-book "US CORP BOOK" \
      --target-location-id 300000004818147

  # With effective date and real execution:
  python run_single_transfer.py --config config.json \
      --asset-number 101533 \
      --source-book "US CORP BOOK" \
      --target-book "UK CORP BOOK" \
      --effective-date 2026-02-10 \
      --execute

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
from ofam_asset_xfer.oracle_client import OracleConfig, OracleErpIntegrationsClient  # noqa: E402


def _load_config(path: str) -> dict:
    raw = Path(path).read_text()
    try:
        return json.loads(raw)
    except Exception as e:
        raise ConfigError(f"Config is not valid JSON: {path}") from e


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Transfer a single asset to a specific book/location.",
    )
    p.add_argument(
        "--config", required=True,
        help="Path to JSON config file (must contain 'oracle' connection settings).",
    )
    p.add_argument("--asset-number", required=True, help="Asset number to transfer.")
    p.add_argument("--source-book", required=True, help="Current book type code.")
    p.add_argument("--target-book", required=True, help="Destination book type code.")
    p.add_argument(
        "--target-location-id", default=None,
        help="Target location ID (CCID). Required for same-book transfers.",
    )
    p.add_argument(
        "--effective-date", default=None,
        help="Transaction date (YYYY-MM-DD). Default: today.",
    )
    p.add_argument(
        "--asset-id", default=None,
        help="Optional asset ID (if known). Otherwise looked up via getAssetInformation.",
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
    is_cross_book = args.source_book.strip().upper() != args.target_book.strip().upper()
    effective_date = args.effective_date or date.today().isoformat()
    request_id = f"SINGLE_XFER_{args.asset_number}_{int(time.time())}"

    transfer_type = "cross-book (bookTransfer)" if is_cross_book else "same-book (transferAsset)"
    mode = "EXECUTE" if execute else "DRY-RUN"
    log.info("Asset          : %s", args.asset_number)
    log.info("Source book    : %s", args.source_book)
    log.info("Target book    : %s", args.target_book)
    log.info("Target location: %s", args.target_location_id or "(none)")
    log.info("Effective date : %s", effective_date)
    log.info("Transfer type  : %s", transfer_type)
    log.info("Mode           : %s", mode)

    try:
        config = _load_config(args.config)

        # Build Fusion client
        oracle_cfg = OracleConfig.from_dict(config.get("oracle", {}))
        client = OracleErpIntegrationsClient(oracle_cfg)

        # Step 1: Get current asset state
        log.info("Calling getAssetInformation for asset=%s book=%s ...",
                 args.asset_number, args.source_book)
        _raw, _pl, state = get_asset_information(
            client,
            args.source_book,
            args.asset_number,
            asset_id=args.asset_id,
        )
        log.info("Asset state: id=%s, distributions=%d, cost=%s",
                 state.asset_id, len(state.distribution_ids), state.cost)

        # Step 2: Build transfer params
        overrides = {}
        if args.target_location_id:
            overrides["location_ccid"] = args.target_location_id

        if is_cross_book:
            params = build_book_transfer_params(
                state=state,
                dest_book_type_code=args.target_book,
                effective_date=effective_date,
                overrides=overrides,
                request_id=request_id,
            )
            handle = "bookTransfer"
        else:
            if not args.target_location_id:
                log.error("Same-book transfer requires --target-location-id")
                return 1
            params, is_noop = build_same_book_transfer_params(
                state=state,
                effective_date=effective_date,
                overrides=overrides,
                request_id=request_id,
            )
            if is_noop:
                log.info("NOOP: no distribution changes needed — asset already at target.")
                result = {"status": "NOOP", "asset_number": args.asset_number}
                _write_result(result, args.out_file)
                return 0
            handle = "transferAsset"

        log.info("Payload built for processTransaction-%s", handle)

        # Step 3: Execute or dry-run
        if dry_run:
            log.info("DRY-RUN: not posting to Fusion.")
            result = {
                "status": "DRY_RUN",
                "asset_number": args.asset_number,
                "source_book": args.source_book,
                "target_book": args.target_book,
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
                "asset_number": args.asset_number,
                "source_book": args.source_book,
                "target_book": args.target_book,
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
