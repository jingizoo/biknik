#!/usr/bin/env python3
"""
Interactive CMDB → FA Location Mismatch Tester.

Connects to CMDB and Oracle Fusion, finds up to 10 assets where the CMDB location
implies a different FA book than the asset currently lives in, displays them in a
table, and lets the user select one to execute a transfer.

Environment variables:
  CMDB_BASE_URL       e.g. https://instance.service-now.com
  CMDB_USER           CMDB username
  CMDB_PASS           CMDB password

  FUSION_BASE_URL     e.g. https://your-fusion-host
  FUSION_API_VERSION  e.g. 11.13.18.05
  FUSION_USER         Oracle Fusion username
  FUSION_PASS         Oracle Fusion password
  (or FUSION_JWT for JWT auth — set FUSION_USER=jwt and FUSION_PASS=<token>)

Usage:
  python tester_cmdb_fa.py
  python tester_cmdb_fa.py --execute          # actually perform the transfer
  python tester_cmdb_fa.py --limit 20         # find up to 20 mismatches
  python tester_cmdb_fa.py --cmdb-query "company=Citadel"
"""

import argparse
import json
import os
import sys
from datetime import date

# Add source path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src", "main", "python"))

from ofam_asset_xfer.cmdb_client import CMDBClient, CMDBConfig
from ofam_asset_xfer.oracle_client import OracleConfig, OracleErpIntegrationsClient
from ofam_asset_xfer.location_resolver import LocationResolver, DEFAULT_COUNTRY_BOOK_MAP
from ofam_asset_xfer.cmdb_sync import CMDBFASync, AssetMismatch


def get_env_or_die(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"ERROR: Environment variable {name} is not set.")
        sys.exit(1)
    return val


def print_header():
    print("=" * 90)
    print("  CMDB → FA LOCATION MISMATCH TESTER")
    print("=" * 90)


def print_mismatches_table(mismatches: list):
    print(f"\n{'#':<4} {'CMDB Tag':<16} {'Display Name':<25} {'CMDB Loc':<14} "
          f"{'Country':<8} {'FA Book (Current)':<20} {'Target Book':<20} {'Type':<12}")
    print(f"{'─'*4} {'─'*16} {'─'*25} {'─'*14} "
          f"{'─'*8} {'─'*20} {'─'*20} {'─'*12}")

    for i, mm in enumerate(mismatches, 1):
        name = (mm.cmdb_display_name or "")[:24]
        print(f"{i:<4} {mm.cmdb_tag:<16} {name:<25} {mm.cmdb_location_code:<14} "
              f"{mm.cmdb_location_country:<8} {mm.fa_book_type_code:<20} "
              f"{mm.target_book_type_code:<20} {mm.mismatch_type:<12}")


def print_mismatch_detail(mm: AssetMismatch):
    print(f"\n{'─' * 70}")
    print(f"  CMDB Tag           : {mm.cmdb_tag}")
    print(f"  Display Name       : {mm.cmdb_display_name}")
    print(f"  CMDB Location Code : {mm.cmdb_location_code}")
    print(f"  CMDB Country       : {mm.cmdb_location_country}")
    print(f"  FA Asset Number    : {mm.fa_asset_number}")
    print(f"  FA Asset ID        : {mm.fa_asset_id}")
    print(f"  FA Current Book    : {mm.fa_book_type_code}")
    print(f"  Target Book        : {mm.target_book_type_code}")
    print(f"  Mismatch Type      : {mm.mismatch_type}")
    if mm.fa_state:
        print(f"  FA Cost            : {mm.fa_state.cost or 'N/A'}")
        print(f"  FA Description     : {mm.fa_state.description or 'N/A'}")
        print(f"  FA Distributions   : {len(mm.fa_state.distribution_ids)}")
    print(f"{'─' * 70}")


def main():
    parser = argparse.ArgumentParser(
        description="Interactive CMDB → FA location mismatch tester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--limit", type=int, default=10,
                        help="Max mismatches to find (default: 10)")
    parser.add_argument("--execute", action="store_true", default=False,
                        help="Actually execute the transfer. Without this, dry-run only.")
    parser.add_argument("--effective-date", default=None,
                        help="Transfer effective date (YYYY-MM-DD). Default: today.")
    parser.add_argument("--cmdb-query", default=None,
                        help="Additional ServiceNow query filter for CMDB assets.")
    parser.add_argument("--cmdb-table", default="alm_hardware",
                        help="CMDB table name (default: alm_hardware)")
    parser.add_argument("--country-book-map", default=None,
                        help="JSON file with country→book mapping overrides.")
    parser.add_argument("--output", default=None,
                        help="Save results to this JSON file.")
    args = parser.parse_args()

    print_header()

    # -----------------------------------------------------------------------
    # 1. Build clients
    # -----------------------------------------------------------------------
    print("\n[1/4] Connecting to CMDB and Oracle Fusion...")

    cmdb_cfg = CMDBConfig(
        base_url=get_env_or_die("CMDB_BASE_URL"),
        username=get_env_or_die("CMDB_USER"),
        password=get_env_or_die("CMDB_PASS"),
        table=args.cmdb_table,
    )
    cmdb_client = CMDBClient(cmdb_cfg)
    print(f"  CMDB : {cmdb_cfg.base_url} (table={cmdb_cfg.table})")

    oracle_cfg = OracleConfig(
        base_url=get_env_or_die("FUSION_BASE_URL"),
        api_version=get_env_or_die("FUSION_API_VERSION"),
        username=get_env_or_die("FUSION_USER"),
        password=get_env_or_die("FUSION_PASS"),
    )
    fusion_client = OracleErpIntegrationsClient(oracle_cfg)
    print(f"  Fusion : {oracle_cfg.base_url} (api={oracle_cfg.api_version})")

    # Load country→book map
    country_map = dict(DEFAULT_COUNTRY_BOOK_MAP)
    if args.country_book_map:
        with open(args.country_book_map) as f:
            overrides = json.load(f)
        country_map.update(overrides)
        print(f"  Country→Book map loaded from: {args.country_book_map}")

    resolver = LocationResolver(fusion_client, country_book_map=country_map)
    sync = CMDBFASync(cmdb_client, fusion_client, resolver)

    # -----------------------------------------------------------------------
    # 2. Find mismatches
    # -----------------------------------------------------------------------
    print(f"\n[2/4] Scanning for up to {args.limit} location mismatches...")
    if args.cmdb_query:
        print(f"  Additional filter: {args.cmdb_query}")

    mismatches = sync.find_mismatches(limit=args.limit, cmdb_query=args.cmdb_query)

    if not mismatches:
        print("\n  No location mismatches found between CMDB and Oracle FA.")
        print("  All scanned assets have matching books.")
        return

    print(f"\n  Found {len(mismatches)} mismatch(es):")

    # -----------------------------------------------------------------------
    # 3. Display mismatches and let user select
    # -----------------------------------------------------------------------
    print_mismatches_table(mismatches)

    print(f"\n[3/4] Select an asset to transfer (1-{len(mismatches)}), or 'q' to quit:")

    while True:
        try:
            choice = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
            return

        if choice.lower() in ("q", "quit", "exit"):
            print("  Cancelled.")
            return

        try:
            idx = int(choice) - 1
            if 0 <= idx < len(mismatches):
                break
            print(f"  Please enter a number between 1 and {len(mismatches)}")
        except ValueError:
            print(f"  Invalid input. Enter 1-{len(mismatches)} or 'q' to quit.")

    selected = mismatches[idx]
    print_mismatch_detail(selected)

    # -----------------------------------------------------------------------
    # 4. Execute transfer
    # -----------------------------------------------------------------------
    effective_date = args.effective_date or date.today().isoformat()
    mode = "EXECUTE" if args.execute else "DRY-RUN"

    print(f"\n[4/4] Executing transfer ({mode})...")
    print(f"  Effective Date: {effective_date}")
    print(f"  Transfer: {selected.fa_book_type_code} → {selected.target_book_type_code}")

    if args.execute:
        confirm = input("  Are you sure you want to execute this transfer? (yes/no): ").strip().lower()
        if confirm not in ("yes", "y"):
            print("  Transfer cancelled.")
            return

    result = sync.execute_transfer(
        selected,
        dry_run=not args.execute,
        effective_date=effective_date,
    )

    print(f"\n{'─' * 70}")
    print(f"  RESULT")
    print(f"{'─' * 70}")
    print(f"  Status         : {result.status}")
    print(f"  CMDB Tag       : {result.cmdb_tag}")
    print(f"  FA Asset       : {result.fa_asset_number}")
    print(f"  Source Book    : {result.source_book}")
    print(f"  Target Book    : {result.target_book}")
    print(f"  Mismatch Type  : {result.mismatch_type}")
    if result.error:
        print(f"  Error          : {result.error}")
    if result.fusion_response:
        print(f"  Fusion Response: {json.dumps(result.fusion_response, indent=2, default=str)[:2000]}")
    print(f"{'─' * 70}")

    # Save output
    output_file = args.output or f"tester_result_{selected.cmdb_tag}_{date.today().isoformat()}.json"
    output_data = {
        "selected_mismatch": {
            "cmdb_tag": selected.cmdb_tag,
            "cmdb_location_code": selected.cmdb_location_code,
            "cmdb_country": selected.cmdb_location_country,
            "fa_asset_number": selected.fa_asset_number,
            "fa_asset_id": selected.fa_asset_id,
            "fa_current_book": selected.fa_book_type_code,
            "target_book": selected.target_book_type_code,
            "mismatch_type": selected.mismatch_type,
        },
        "transfer_result": {
            "status": result.status,
            "error": result.error,
            "fusion_response": result.fusion_response,
        },
        "all_mismatches": [
            {
                "cmdb_tag": mm.cmdb_tag,
                "cmdb_location_code": mm.cmdb_location_code,
                "cmdb_country": mm.cmdb_location_country,
                "fa_asset_number": mm.fa_asset_number,
                "fa_current_book": mm.fa_book_type_code,
                "target_book": mm.target_book_type_code,
                "mismatch_type": mm.mismatch_type,
            }
            for mm in mismatches
        ],
    }
    with open(output_file, "w") as f:
        json.dump(output_data, f, indent=2, default=str)
    print(f"\n  Results saved to: {output_file}")


if __name__ == "__main__":
    main()
