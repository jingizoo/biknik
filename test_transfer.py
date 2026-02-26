#!/usr/bin/env python3
"""
Standalone test tool for Oracle Fusion Fixed Asset transfer.

Takes ONLY an asset number and book type code as input.
Queries Fusion via getAssetInformation to auto-fill ALL parameters,
then builds and optionally executes a same-book transferAsset call.

The destination values default to the source values (safe dry-run test).
Override any field via CLI args to test actual changes.

Environment variables:
  FUSION_BASE_URL    e.g. https://your-fusion-host
  FUSION_API_VERSION e.g. 11.13.18.05
  FUSION_JWT         raw JWT token

NOTE: getAssetInformation does NOT return X_ASSET_ID.
  You must supply --asset-id manually (look it up in Oracle FA).

Usage examples:
  # Dry-run: query asset info and show what transfer payload would look like
  python test_transfer.py --asset-number 125183 --asset-id 12345 --book-code "OPS CORP"

  # Override specific fields for the destination
  python test_transfer.py --asset-number 125183 --asset-id 12345 --book-code "OPS CORP" \
      --location-ccid 300100071633789 --expense-ccid 300100071633798

  # Transfer to a different destination book
  python test_transfer.py --asset-number 125183 --asset-id 12345 --book-code "OPS CORP" \
      --dest-book-code "TAX CORP"

  # Actually execute the transfer
  python test_transfer.py --asset-number 125183 --asset-id 12345 --book-code "OPS CORP" --execute
"""

import argparse
import json
import os
import re
import sys
from datetime import date
from typing import Any, Dict, List, Tuple

import requests


# ---------------------------------------------------------------------------
# Utility helpers (self-contained, mirrors ofam_asset_xfer.paramlist)
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_date_str(s: str) -> bool:
    return bool(_DATE_RE.match(s.strip()))


def _quote_single(s: str) -> str:
    s = s.replace("'", "''")
    return f"'{s}'"


def to_rosetta_str(values) -> str:
    flat = []
    for v in values:
        flat.append("" if v is None else str(v).strip())
    return _quote_single(",".join(flat))


def parse_rosetta(value) -> List[str]:
    if value is None:
        return []
    s = str(value).strip()
    if s in ("", ","):
        return []
    parts = [p.strip() for p in s.split(",")]
    return [p for p in parts if p != ""]


def build_parameter_list(params: Dict[str, Any]) -> str:
    items = []
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            items.append(f"{k}: {to_rosetta_str(v)}")
            continue
        if isinstance(v, str) and (k.endswith("_TBL") or k.endswith("_TABLE")):
            items.append(f"{k}: {to_rosetta_str([p.strip() for p in v.split(',') if p.strip()])}")
            continue
        if isinstance(v, str) and _is_date_str(v):
            items.append(f"{k}: {_quote_single(v.strip())}")
            continue
        items.append(f"{k}: {v}")
    return "{" + ", ".join(items) + "}"


# ---------------------------------------------------------------------------
# Fusion API caller (JWT Bearer auth)
# ---------------------------------------------------------------------------

def call_fusion(
    base_url: str,
    api_version: str,
    jwt_token: str,
    handle: str,
    params: Dict[str, Any],
    verify_ssl: bool = True,
    timeout_seconds: int = 60,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    op_name = f"processTransaction-{handle}"
    payload = {
        "OperationName": op_name,
        "ParameterList": build_parameter_list(params),
    }
    url = f"{base_url.rstrip('/')}/fscmRestApi/resources/{api_version}/erpintegrations/{op_name}"

    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/vnd.oracle.adf.resourceitem+json",
        "REST-header-version": "4",
        "ACCEPT": "application/json",
        "Authorization": f"Bearer {jwt_token}",
    })

    print(f"\n>>> POST {url}")
    print(f">>> Payload: {json.dumps(payload, indent=2)[:2000]}")

    resp = session.post(url, json=payload, timeout=timeout_seconds, verify=verify_ssl)
    try:
        raw = resp.json()
    except Exception:
        raise RuntimeError(f"Non-JSON response (status={resp.status_code}): {resp.text[:500]}")

    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {json.dumps(raw, indent=2)[:2000]}")

    pl_raw = raw.get("ParameterList")
    pl: Dict[str, Any] = {}
    if isinstance(pl_raw, str) and pl_raw.strip():
        try:
            pl = json.loads(pl_raw)
        except Exception:
            pl = {"_raw": pl_raw}
    elif isinstance(pl_raw, dict):
        pl = pl_raw

    return raw, pl


# ---------------------------------------------------------------------------
# Step 1: Get asset information
# ---------------------------------------------------------------------------

def get_asset_info(base_url, api_version, jwt, book_code, asset_number, cli_asset_id=None):
    print("=" * 70)
    print("STEP 1: getAssetInformation")
    print(f"  Book Type Code : {book_code}")
    print(f"  Asset Number   : {asset_number}")
    if cli_asset_id:
        print(f"  Asset ID (CLI) : {cli_asset_id}")
    print("=" * 70)

    params = {
        "P_BOOK_TYPE_CODE": book_code,
        "P_ASSET_NUMBER": asset_number,
    }
    raw, pl = call_fusion(base_url, api_version, jwt, "getAssetInformation", params)

    status = str(pl.get("X_RETURN_STATUS") or "").strip()
    if status and status != "S":
        print(f"\nERROR: getAssetInformation failed (X_RETURN_STATUS={status})")
        print(f"  X_MSG_DATA: {pl.get('X_MSG_DATA', 'N/A')}")
        print(f"\nFull ParameterList:\n{json.dumps(pl, indent=2)[:3000]}")
        sys.exit(1)

    # Extract all relevant fields
    # NOTE: X_ASSET_ID is NOT returned by getAssetInformation.
    # The asset_id MUST be supplied via --asset-id CLI argument.
    api_asset_id = str(pl.get("X_ASSET_ID") or "").strip() or None
    asset_id = cli_asset_id or api_asset_id
    asset_num = str(pl.get("X_ASSET_NUMBER") or asset_number).strip()
    book = str(pl.get("X_BOOK_TYPE_CODE") or pl.get("P_BOOK_TYPE_CODE") or book_code).strip()

    dist_ids = parse_rosetta(pl.get("X_DISTRIBUTION_ID_TBL"))
    units = parse_rosetta(pl.get("X_UNITS_ASSIGNED_TBL"))
    assigned_to = parse_rosetta(pl.get("X_ASSIGNED_TO_TBL"))
    expense_ccids = parse_rosetta(pl.get("X_EXPENSE_CCID_TBL"))
    location_ccids = parse_rosetta(pl.get("X_LOCATION_CCID_TBL"))

    category_id = str(pl.get("X_CATEGORY_ID") or "").strip() or None
    date_placed = str(pl.get("X_DATE_PLACED_IN_SERVICE") or "").strip() or None
    cost = str(pl.get("X_COST") or "").strip() or None
    description = str(pl.get("X_DESCRIPTION") or "").strip() or None
    tag_number = str(pl.get("X_TAG_NUMBER") or "").strip() or None

    if not asset_num:
        print("\nERROR: Missing asset number in response.")
        print(f"\nFull ParameterList:\n{json.dumps(pl, indent=2)[:3000]}")
        sys.exit(1)

    if not asset_id:
        print("\nERROR: Asset ID is required for transferAsset but was NOT returned")
        print("       by getAssetInformation (X_ASSET_ID is not in the response).")
        print("       You must supply it via --asset-id <id>.")
        print(f"\n       Example: python test_transfer.py --asset-number {asset_number} --asset-id <YOUR_ASSET_ID> --book-code \"{book_code}\"")
        sys.exit(1)

    if cli_asset_id:
        print(f"\n  Using Asset ID from --asset-id: {asset_id}")
    elif api_asset_id:
        print(f"\n  X_ASSET_ID found in response: {asset_id}")


    # Fill missing assigned_to with placeholders
    if not assigned_to:
        assigned_to = ["-1"] * len(dist_ids)

    # Display
    print("\n" + "-" * 70)
    print("ASSET STATE (from Fusion)")
    print("-" * 70)
    print(f"  Asset ID               : {asset_id or 'N/A (not returned)'}")
    print(f"  Asset Number           : {asset_num}")
    print(f"  Book Type Code         : {book}")
    print(f"  Category ID            : {category_id or 'N/A'}")
    print(f"  Date Placed in Service : {date_placed or 'N/A'}")
    print(f"  Cost                   : {cost or 'N/A'}")
    print(f"  Description            : {description or 'N/A'}")
    print(f"  Tag Number             : {tag_number or 'N/A'}")
    print()
    print(f"  Distributions ({len(dist_ids)}):")
    print(f"    {'Dist ID':<20} {'Units':<10} {'Assigned To':<25} {'Expense CCID':<25} {'Location CCID':<25}")
    print(f"    {'-------':<20} {'-----':<10} {'-----------':<25} {'------------':<25} {'-------------':<25}")
    for i in range(len(dist_ids)):
        print(f"    {dist_ids[i]:<20} {units[i] if i < len(units) else 'N/A':<10} "
              f"{assigned_to[i] if i < len(assigned_to) else 'N/A':<25} "
              f"{expense_ccids[i] if i < len(expense_ccids) else 'N/A':<25} "
              f"{location_ccids[i] if i < len(location_ccids) else 'N/A':<25}")

    return {
        "asset_id": asset_id,
        "asset_number": asset_num,
        "book_type_code": book,
        "distribution_ids": dist_ids,
        "units_assigned": units,
        "assigned_to": assigned_to,
        "expense_ccids": expense_ccids,
        "location_ccids": location_ccids,
        "category_id": category_id,
        "date_placed_in_service": date_placed,
        "cost": cost,
        "description": description,
        "tag_number": tag_number,
    }, pl


# ---------------------------------------------------------------------------
# Step 2: Build transfer payload
# ---------------------------------------------------------------------------

def build_transfer_payload(state, args):
    print("\n" + "=" * 70)
    print("STEP 2: Build transferAsset Payload")
    print("=" * 70)

    n = len(state["distribution_ids"])

    # Determine destination book type code
    dest_book_code = args.dest_book_code or state["book_type_code"]
    is_cross_book = dest_book_code != state["book_type_code"]

    print(f"\n  Source Book      : {state['book_type_code']}")
    print(f"  Destination Book : {dest_book_code}")
    if is_cross_book:
        print("  ** Cross-book transfer **")

    # Determine destination values: use overrides if provided, else keep source
    dest_location = state["location_ccids"][:]
    dest_expense = state["expense_ccids"][:]
    dest_assigned = state["assigned_to"][:]

    overrides_applied = []

    if args.location_ccid:
        dest_location = [args.location_ccid] * n
        overrides_applied.append(f"  location_ccid  : {args.location_ccid} (applied to all {n} distributions)")
    if args.expense_ccid:
        dest_expense = [args.expense_ccid] * n
        overrides_applied.append(f"  expense_ccid   : {args.expense_ccid} (applied to all {n} distributions)")
    if args.assigned_to:
        dest_assigned = [args.assigned_to] * n
        overrides_applied.append(f"  assigned_to    : {args.assigned_to} (applied to all {n} distributions)")

    if overrides_applied:
        print("\n  Overrides applied:")
        for ov in overrides_applied:
            print(ov)
    else:
        print("\n  No overrides specified -> destination = source values (same location/expense/assigned_to)")

    # Check for NOOP
    is_noop = (
        not is_cross_book
        and dest_location == state["location_ccids"]
        and dest_expense == state["expense_ccids"]
        and dest_assigned == state["assigned_to"]
    )
    if is_noop:
        print("\n  WARNING: Destination values match source values exactly.")
        print("           This is a NO-OP transfer (nothing will change).")
        print("           Use --location-ccid, --expense-ccid, --assigned-to, or --dest-book-code to specify new values.")

    # Build dual-leg rosetta tables (source leg negative, destination leg positive)
    dist_tbl = []
    txn_units_tbl = []
    units_tbl = []
    assigned_tbl = []
    expense_tbl = []
    location_tbl = []

    for i in range(n):
        src_dist = state["distribution_ids"][i]
        units = state["units_assigned"][i]
        src_assigned = state["assigned_to"][i]
        src_exp = state["expense_ccids"][i]
        src_loc = state["location_ccids"][i]

        dst_assigned = dest_assigned[i]
        dst_exp = dest_expense[i]
        dst_loc = dest_location[i]

        # Source leg (negative units = transfer FROM)
        dist_tbl.append(src_dist)
        txn_units_tbl.append(f"-{units}")
        units_tbl.append(units)
        assigned_tbl.append(src_assigned or "-1")
        expense_tbl.append(src_exp)
        location_tbl.append(src_loc)

        # Destination leg (positive units = transfer TO, -1 = new distribution)
        dist_tbl.append("-1")
        txn_units_tbl.append(str(units))
        units_tbl.append(units)
        assigned_tbl.append(dst_assigned or "-1")
        expense_tbl.append(dst_exp)
        location_tbl.append(dst_loc)

    today = date.today().isoformat()

    params = {
        "P_BOOK_TYPE_CODE": state["book_type_code"],
        "P_DEST_BOOK_TYPE_CODE": dest_book_code,
        "P_DISTRIBUTION_ID_TBL": dist_tbl,
        "P_TRANSACTION_UNITS_TBL": txn_units_tbl,
        "P_UNITS_ASSIGNED_TBL": units_tbl,
        "P_ASSIGNED_TO_TBL": assigned_tbl,
        "P_EXPENSE_CCID_TBL": expense_tbl,
        "P_LOCATION_CCID_TBL": location_tbl,
        "P_TRX_ATTRIBUTE1": f"TEST-{today}",
        "P_TRX_ATTRIBUTE2": "TEST_SAME_BOOK_XFER",
        "P_TRANSACTION_DATE_ENTERED": today,
    }

    # P_ASSET_ID is required by transferAsset (asset_id is guaranteed
    # present at this point — either from --asset-id CLI or API response)
    params["P_ASSET_ID"] = state["asset_id"]

    print("\n  Transfer Payload (dual-leg):")
    print(f"    {'Leg':<6} {'Dist ID':<20} {'Txn Units':<12} {'Units':<10} "
          f"{'Assigned To':<25} {'Expense CCID':<25} {'Location CCID':<25}")
    print(f"    {'---':<6} {'-------':<20} {'---------':<12} {'-----':<10} "
          f"{'-----------':<25} {'------------':<25} {'-------------':<25}")
    for i in range(len(dist_tbl)):
        leg = "SRC" if i % 2 == 0 else "DST"
        print(f"    {leg:<6} {dist_tbl[i]:<20} {txn_units_tbl[i]:<12} {units_tbl[i]:<10} "
              f"{assigned_tbl[i]:<25} {expense_tbl[i]:<25} {location_tbl[i]:<25}")

    print(f"\n  ParameterList string:")
    print(f"    {build_parameter_list(params)[:2000]}")

    return params, is_noop


# ---------------------------------------------------------------------------
# Step 3: Execute transfer
# ---------------------------------------------------------------------------

def execute_transfer(base_url, api_version, jwt, params):
    print("\n" + "=" * 70)
    print("STEP 3: Execute transferAsset")
    print("=" * 70)

    raw, pl = call_fusion(base_url, api_version, jwt, "transferAsset", params)

    status = str(pl.get("X_RETURN_STATUS") or "").strip()

    print("\n" + "-" * 70)
    print("TRANSFER RESULT")
    print("-" * 70)
    print(f"  X_RETURN_STATUS           : {status or 'N/A'}")
    print(f"  X_EVENT_ID                : {pl.get('X_EVENT_ID', 'N/A')}")
    print(f"  X_TRANSACTION_HEADER_ID   : {pl.get('X_TRANSACTION_HEADER_ID', 'N/A')}")
    print(f"  X_D_TRANSACTION_HEADER_ID : {pl.get('X_D_TRANSACTION_HEADER_ID', 'N/A')}")
    print(f"  X_MSG_COUNT               : {pl.get('X_MSG_COUNT', 'N/A')}")
    print(f"  X_MSG_DATA                : {pl.get('X_MSG_DATA', 'N/A')}")

    if status == "S":
        print("\n  >>> TRANSFER SUCCEEDED <<<")
    else:
        print(f"\n  >>> TRANSFER FAILED (status={status}) <<<")
        print(f"\n  Full ParameterList:\n{json.dumps(pl, indent=2)[:3000]}")

    return raw, pl


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Test Fusion Fixed Asset transfer. Queries asset info and builds/executes transfer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run (default) - just query asset and show transfer payload:
  python test_transfer.py --asset-number 125183 --asset-id 12345 --book-code "OPS CORP"

  # Override destination expense CCID:
  python test_transfer.py --asset-number 125183 --asset-id 12345 --book-code "OPS CORP" \\
      --expense-ccid 300100071633798

  # Transfer to a different destination book:
  python test_transfer.py --asset-number 125183 --asset-id 12345 --book-code "OPS CORP" \\
      --dest-book-code "TAX CORP"

  # Actually execute the transfer:
  python test_transfer.py --asset-number 125183 --asset-id 12345 --book-code "OPS CORP" \\
      --expense-ccid 300100071633798 --execute
        """,
    )
    parser.add_argument("--asset-number", required=True, help="Asset number (e.g. 125183)")
    parser.add_argument("--asset-id", required=True,
                        help="Asset ID (required — getAssetInformation does NOT return this. Look it up in Oracle FA.)")
    parser.add_argument("--book-code", required=True, help="Source book type code (e.g. 'OPS CORP')")
    parser.add_argument("--dest-book-code", default=None,
                        help="Destination book type code. Defaults to source book code (same-book transfer).")

    parser.add_argument("--location-ccid", default=None, help="Override destination location CCID (applied to all distributions)")
    parser.add_argument("--expense-ccid", default=None, help="Override destination expense CCID (applied to all distributions)")
    parser.add_argument("--assigned-to", default=None, help="Override destination assigned-to ID (applied to all distributions)")

    parser.add_argument("--execute", action="store_true", default=False,
                        help="Actually POST the transferAsset call. Without this flag, only dry-run.")
    parser.add_argument("--no-verify-ssl", action="store_true", default=False,
                        help="Disable SSL verification (for dev/test environments)")

    args = parser.parse_args()

    # Read Fusion connection from environment
    base_url = os.environ.get("FUSION_BASE_URL", "").strip()
    api_version = os.environ.get("FUSION_API_VERSION", "").strip()
    jwt_token = os.environ.get("FUSION_JWT", "").strip()

    if not base_url or not api_version or not jwt_token:
        print("ERROR: Set these environment variables before running:")
        print("  FUSION_BASE_URL    = https://your-fusion-host")
        print("  FUSION_API_VERSION = 11.13.18.05")
        print("  FUSION_JWT         = <your JWT token>")
        sys.exit(1)

    dest_book_display = args.dest_book_code or args.book_code
    print("=" * 70)
    print("  FUSION ASSET TRANSFER TEST TOOL")
    print("=" * 70)
    print(f"  Fusion Host  : {base_url}")
    print(f"  API Version  : {api_version}")
    print(f"  Asset Number : {args.asset_number}")
    print(f"  Asset ID     : {args.asset_id}")
    print(f"  Source Book  : {args.book_code}")
    print(f"  Dest Book    : {dest_book_display}")
    print(f"  Mode         : {'EXECUTE' if args.execute else 'DRY-RUN'}")
    print(f"  SSL Verify   : {not args.no_verify_ssl}")

    # Step 1: Get asset information
    state, raw_pl = get_asset_info(base_url, api_version, jwt_token, args.book_code, args.asset_number, cli_asset_id=args.asset_id)

    # Step 2: Build transfer payload
    params, is_noop = build_transfer_payload(state, args)

    # Step 3: Execute or dry-run
    if not args.execute:
        print("\n" + "=" * 70)
        print("DRY-RUN COMPLETE")
        print("  Transfer payload built but NOT sent to Fusion.")
        print("  Add --execute flag to actually perform the transfer.")
        if is_noop:
            print("  NOTE: This is a NO-OP since destination = source values.")
            print("        Specify --location-ccid, --expense-ccid, --assigned-to, or --dest-book-code to change destination.")
        print("=" * 70)

        # Save payload to file for inspection
        output_file = f"test_transfer_payload_{args.asset_number}.json"
        with open(output_file, "w") as f:
            json.dump({
                "asset_state": state,
                "transfer_params": params,
                "is_noop": is_noop,
                "raw_getAssetInformation": raw_pl,
            }, f, indent=2)
        print(f"\n  Payload saved to: {output_file}")
        return

    if is_noop:
        print("\n  WARNING: Destination = source. Proceeding anyway since --execute was specified...")

    raw, pl = execute_transfer(base_url, api_version, jwt_token, params)

    # Save full results
    output_file = f"test_transfer_result_{args.asset_number}.json"
    with open(output_file, "w") as f:
        json.dump({
            "asset_state": state,
            "transfer_params": params,
            "transfer_response_raw": raw,
            "transfer_response_pl": pl,
        }, f, indent=2)
    print(f"\n  Full results saved to: {output_file}")


if __name__ == "__main__":
    main()
