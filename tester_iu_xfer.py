#!/usr/bin/env python3
"""
Interactive IU (Inter-Unit) Transfer Tester.

Connects to Oracle Fusion FA, scans assets for DFF fields "Transfer Date" and
"Transfer to Entity", finds assets where the target entity implies a different
book than the asset currently lives in, displays them in a table, and lets the
user select one to execute a cross-book transfer.

NO CMDB required — everything comes from Fusion itself.

Environment variables:
  FUSION_BASE_URL     e.g. https://your-fusion-host
  FUSION_API_VERSION  e.g. 11.13.18.05
  FUSION_JWT          raw JWT bearer token

Usage:
  python tester_iu_xfer.py --entity-book-map entity_map.json --books "US CORP BOOK,UK CORP BOOK"
  python tester_iu_xfer.py --entity-book-map entity_map.json --execute
  python tester_iu_xfer.py --entity-book-map entity_map.json --limit 20

Entity book map JSON format:
  {
    "US Entity": "US CORP BOOK",
    "UK Entity": "UK CORP BOOK",
    "JP Entity": "JP CORP BOOK"
  }
"""

import argparse
import json
import os
import sys
from datetime import date

# Add source path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src", "main", "python"))

from ofam_asset_xfer.oracle_client import OracleConfig, OracleErpIntegrationsClient
from ofam_asset_xfer.entity_resolver import EntityBookResolver
from ofam_asset_xfer.fusion_sync import FusionIUSync, DFFConfig, PendingTransfer


def get_env_or_die(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"ERROR: Environment variable {name} is not set.")
        sys.exit(1)
    return val


def print_header():
    print("=" * 90)
    print("  FUSION FA — IU TRANSFER TESTER  (DFF-based, no CMDB)")
    print("=" * 90)


def print_pending_table(pending: list):
    print(f"\n{'#':<4} {'Asset #':<12} {'Tag':<14} {'Description':<26} "
          f"{'Current Book':<20} {'→ Entity':<18} {'Target Book':<20} {'Xfer Date':<12}")
    print(f"{'─'*4} {'─'*12} {'─'*14} {'─'*26} "
          f"{'─'*20} {'─'*18} {'─'*20} {'─'*12}")

    for i, pt in enumerate(pending, 1):
        desc = (pt.description or "")[:25]
        tag = (pt.tag_number or "")[:13]
        entity = (pt.transfer_to_entity or "")[:17]
        print(f"{i:<4} {pt.asset_number:<12} {tag:<14} {desc:<26} "
              f"{pt.book_type_code:<20} {entity:<18} "
              f"{pt.target_book_type_code:<20} {pt.transfer_date:<12}")


def print_pending_detail(pt: PendingTransfer):
    print(f"\n{'─' * 70}")
    print(f"  Asset Number       : {pt.asset_number}")
    print(f"  Asset ID           : {pt.asset_id}")
    print(f"  Tag Number         : {pt.tag_number or 'N/A'}")
    print(f"  Description        : {pt.description or 'N/A'}")
    print(f"  Current Book       : {pt.book_type_code}")
    print(f"  Cost               : {pt.cost or 'N/A'}")
    print(f"  Transfer to Entity : {pt.transfer_to_entity}")
    print(f"  Transfer Date      : {pt.transfer_date}")
    print(f"  Transfer Location  : {pt.transfer_to_location or 'N/A'}")
    print(f"  Target Book        : {pt.target_book_type_code}")
    if pt.fa_state:
        print(f"  Distributions      : {len(pt.fa_state.distribution_ids)}")
    print(f"{'─' * 70}")


def main():
    parser = argparse.ArgumentParser(
        description="Interactive IU transfer tester — reads DFFs from Fusion FA directly",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--entity-book-map", required=True,
                        help="JSON file mapping entity names → book type codes.")
    parser.add_argument("--books", default=None,
                        help='Comma-separated book type codes to scan (default: all from entity map).')
    parser.add_argument("--limit", type=int, default=10,
                        help="Max pending transfers to find (default: 10)")
    parser.add_argument("--execute", action="store_true", default=False,
                        help="Actually execute the transfer. Without this, dry-run only.")
    parser.add_argument("--dff-entity-field", default="transferToEntity",
                        help="REST DFF field name for Transfer to Entity (default: transferToEntity)")
    parser.add_argument("--dff-date-field", default="transferDate",
                        help="REST DFF field name for Transfer Date (default: transferDate)")
    parser.add_argument("--dff-location-field", default="transferToLocation",
                        help="REST DFF field name for Transfer to Location (default: transferToLocation)")
    parser.add_argument("--output", default=None,
                        help="Save results to this JSON file.")
    args = parser.parse_args()

    print_header()

    # -----------------------------------------------------------------------
    # 1. Build clients
    # -----------------------------------------------------------------------
    print("\n[1/4] Connecting to Oracle Fusion...")

    oracle_cfg = OracleConfig(
        base_url=get_env_or_die("FUSION_BASE_URL"),
        api_version=get_env_or_die("FUSION_API_VERSION"),
        bearer_token=get_env_or_die("FUSION_JWT"),
    )
    fusion_client = OracleErpIntegrationsClient(oracle_cfg)
    print(f"  Fusion : {oracle_cfg.base_url} (api={oracle_cfg.api_version})")

    # Load entity→book mapping
    entity_resolver = EntityBookResolver.from_json_file(args.entity_book_map)
    entity_map = entity_resolver.entity_book_map
    print(f"  Entity map loaded: {len(entity_map)} mapping(s) from {args.entity_book_map}")
    for entity, book in sorted(entity_map.items()):
        print(f"    {entity} → {book}")

    # Determine books to scan
    if args.books:
        books = [b.strip() for b in args.books.split(",") if b.strip()]
    else:
        # Use all unique book codes from the entity map
        books = sorted(set(entity_map.values()))
    print(f"  Books to scan: {books}")

    # DFF config
    dff_config = DFFConfig(
        transfer_date=args.dff_date_field,
        transfer_to_entity=args.dff_entity_field,
        transfer_to_location=args.dff_location_field,
    )

    sync = FusionIUSync(fusion_client, entity_resolver, dff_config)

    # -----------------------------------------------------------------------
    # 2. Find pending transfers
    # -----------------------------------------------------------------------
    print(f"\n[2/4] Scanning for up to {args.limit} pending IU transfers...")

    pending = sync.find_pending_transfers(books=books, limit=args.limit)

    if not pending:
        print("\n  No pending IU transfers found.")
        print("  (No assets have Transfer-to-Entity DFF populated with a different book.)")
        return

    print(f"\n  Found {len(pending)} pending transfer(s):")

    # -----------------------------------------------------------------------
    # 3. Display and let user select
    # -----------------------------------------------------------------------
    print_pending_table(pending)

    print(f"\n[3/4] Select an asset to transfer (1-{len(pending)}), or 'q' to quit:")

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
            if 0 <= idx < len(pending):
                break
            print(f"  Please enter a number between 1 and {len(pending)}")
        except ValueError:
            print(f"  Invalid input. Enter 1-{len(pending)} or 'q' to quit.")

    selected = pending[idx]
    print_pending_detail(selected)

    # -----------------------------------------------------------------------
    # 4. Execute transfer
    # -----------------------------------------------------------------------
    mode = "EXECUTE" if args.execute else "DRY-RUN"

    print(f"\n[4/4] Executing IU transfer ({mode})...")
    print(f"  Transfer Date   : {selected.transfer_date}")
    print(f"  Transfer Entity : {selected.transfer_to_entity}")
    print(f"  Transfer        : {selected.book_type_code} → {selected.target_book_type_code}")

    if args.execute:
        confirm = input("  Are you sure you want to execute this transfer? (yes/no): ").strip().lower()
        if confirm not in ("yes", "y"):
            print("  Transfer cancelled.")
            return

    result = sync.execute_transfer(selected, dry_run=not args.execute)

    print(f"\n{'─' * 70}")
    print(f"  RESULT")
    print(f"{'─' * 70}")
    print(f"  Status           : {result.status}")
    print(f"  Asset Number     : {result.asset_number}")
    print(f"  Source Book      : {result.source_book}")
    print(f"  Target Book      : {result.target_book}")
    print(f"  Transfer Entity  : {result.transfer_to_entity}")
    print(f"  Transfer Date    : {result.transfer_date}")
    if result.error:
        print(f"  Error            : {result.error}")
    if result.fusion_response:
        print(f"  Fusion Response  : {json.dumps(result.fusion_response, indent=2, default=str)[:2000]}")
    print(f"{'─' * 70}")

    # Save output
    output_file = args.output or f"iu_xfer_result_{selected.asset_number}_{date.today().isoformat()}.json"
    output_data = {
        "selected_transfer": {
            "asset_number": selected.asset_number,
            "asset_id": selected.asset_id,
            "current_book": selected.book_type_code,
            "transfer_to_entity": selected.transfer_to_entity,
            "transfer_date": selected.transfer_date,
            "target_book": selected.target_book_type_code,
        },
        "transfer_result": {
            "status": result.status,
            "error": result.error,
            "fusion_response": result.fusion_response,
        },
        "all_pending": [
            {
                "asset_number": pt.asset_number,
                "current_book": pt.book_type_code,
                "transfer_to_entity": pt.transfer_to_entity,
                "transfer_date": pt.transfer_date,
                "target_book": pt.target_book_type_code,
            }
            for pt in pending
        ],
    }
    with open(output_file, "w") as f:
        json.dump(output_data, f, indent=2, default=str)
    print(f"\n  Results saved to: {output_file}")


if __name__ == "__main__":
    main()
