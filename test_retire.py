#!/usr/bin/env python3
"""
Standalone test tool for Oracle Fusion Fixed Asset retirement.

Takes an asset number, asset ID and book type code as input.
Queries Fusion via getAssetInformation to get current cost/units,
then builds and optionally executes a retireAsset call.

This is typically the NEXT STEP after test_transfer.py has moved the
asset to a new book/entity.  The source asset should be retired.

Environment variables:
  FUSION_BASE_URL    e.g. https://your-fusion-host
  FUSION_API_VERSION e.g. 11.13.18.05
  FUSION_JWT         raw JWT token

NOTE: getAssetInformation does NOT return X_ASSET_ID.
  You must supply --asset-id manually (look it up in Oracle FA).

Usage examples:
  # Dry-run: query asset info and show what retirement payload would look like
  python test_retire.py --asset-number 125183 --asset-id 706 --book-code "UK CORP BOOK"

  # Full retirement with sale proceeds
  python test_retire.py --asset-number 125183 --asset-id 706 --book-code "UK CORP BOOK" \
      --proceeds 50000 --execute

  # Partial retirement (retire 3 out of 10 units)
  python test_retire.py --asset-number 125183 --asset-id 706 --book-code "UK CORP BOOK" \
      --units-to-retire 3 --execute

  # Retirement as abandonment (no sale proceeds)
  python test_retire.py --asset-number 125183 --asset-id 706 --book-code "UK CORP BOOK" \
      --retirement-type ABANDONMENT --execute
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
# Utility helpers (shared with test_transfer.py)
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")


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
    if s.startswith("'") and s.endswith("'"):
        s = s[1:-1].replace("''", "'")
    parts = [p.strip() for p in s.split(",")]
    return [p for p in parts if p != ""]


def build_parameter_list(params: Dict[str, Any]) -> str:
    items = []
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            items.append(f"{k}:{to_rosetta_str(v)}")
            continue
        if isinstance(v, str) and (k.endswith("_TBL") or k.endswith("_TABLE")):
            items.append(f"{k}:{to_rosetta_str([p.strip() for p in v.split(',') if p.strip()])}")
            continue
        if isinstance(v, str) and _is_date_str(v):
            items.append(f"{k}: {_quote_single(v.strip())}")
            continue
        sv = str(v)
        if _NUMERIC_RE.match(sv.strip()):
            items.append(f"{k}: {sv}")
        else:
            items.append(f"{k}:{_quote_single(sv)}")
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
    verify_ssl: bool = False,
    timeout_seconds: int = 60,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    op_name = f"processTransaction-{handle}"
    payload = {
        "OperationName": op_name,
        "ParameterList": build_parameter_list(params),
    }
    url = f"{base_url.rstrip('/')}/fscmRestApi/resources/{api_version}/erpintegrations"

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

    api_asset_id = str(pl.get("X_ASSET_ID") or "").strip() or None
    asset_id = cli_asset_id or api_asset_id
    asset_num = str(pl.get("X_ASSET_NUMBER") or asset_number).strip()
    book = str(pl.get("X_BOOK_TYPE_CODE") or pl.get("P_BOOK_TYPE_CODE") or book_code).strip()

    dist_ids = parse_rosetta(pl.get("X_DISTRIBUTION_ID_TBL"))
    units = parse_rosetta(pl.get("X_UNITS_ASSIGNED_TBL"))
    expense_ccids = parse_rosetta(pl.get("X_EXPENSE_CCID_TBL"))
    location_ccids = parse_rosetta(pl.get("X_LOCATION_CCID_TBL"))

    category_id = str(pl.get("X_CATEGORY_ID") or "").strip() or None
    date_placed = str(pl.get("X_DATE_PLACED_IN_SERVICE") or "").strip() or None
    cost = str(pl.get("X_COST") or "").strip() or None
    description = str(pl.get("X_DESCRIPTION") or "").strip() or None

    # Retirement status
    retirement_id = str(pl.get("X_RETIREMENT_ID") or "").strip()
    date_retired = str(pl.get("X_DATE_RETIRED") or "").strip()
    current_units = str(pl.get("X_CURRENT_UNITS") or "").strip()
    period_retired = str(pl.get("X_PERIOD_COUNTER_FULLY_RETIRED") or "").strip()

    is_retired = bool(retirement_id or date_retired or period_retired
                      or current_units == "0")

    if not asset_num:
        print("\nERROR: Missing asset number in response.")
        print(f"\nFull ParameterList:\n{json.dumps(pl, indent=2)[:3000]}")
        sys.exit(1)

    if not asset_id:
        print("\nERROR: Asset ID is required but was NOT returned.")
        print("       You must supply it via --asset-id <id>.")
        sys.exit(1)

    # Compute total units
    total_units = 0
    for u in units:
        try:
            total_units += int(float(u))
        except (ValueError, TypeError):
            pass

    # Display
    print("\n" + "-" * 70)
    print("ASSET STATE (from Fusion)")
    print("-" * 70)
    print(f"  Asset ID               : {asset_id}")
    print(f"  Asset Number           : {asset_num}")
    print(f"  Book Type Code         : {book}")
    print(f"  Category ID            : {category_id or 'N/A'}")
    print(f"  Date Placed in Service : {date_placed or 'N/A'}")
    print(f"  Cost                   : {cost or 'N/A'}")
    print(f"  Total Units            : {total_units}")
    print(f"  Description            : {description or 'N/A'}")
    print(f"  Already Retired?       : {'YES' if is_retired else 'NO'}")
    if is_retired:
        if retirement_id:
            print(f"  Retirement ID          : {retirement_id}")
        if date_retired:
            print(f"  Date Retired           : {date_retired}")
    print()
    print(f"  Distributions ({len(dist_ids)}):")
    print(f"    {'Dist ID':<20} {'Units':<10} {'Expense Acct':<25} {'Location CCID':<25}")
    print(f"    {'-------':<20} {'-----':<10} {'------------':<25} {'-------------':<25}")
    for i in range(len(dist_ids)):
        print(f"    {dist_ids[i]:<20} {units[i] if i < len(units) else 'N/A':<10} "
              f"{expense_ccids[i] if i < len(expense_ccids) else 'N/A':<25} "
              f"{location_ccids[i] if i < len(location_ccids) else 'N/A':<25}")

    return {
        "asset_id": asset_id,
        "asset_number": asset_num,
        "book_type_code": book,
        "distribution_ids": dist_ids,
        "units_assigned": units,
        "total_units": total_units,
        "expense_ccids": expense_ccids,
        "location_ccids": location_ccids,
        "category_id": category_id,
        "date_placed_in_service": date_placed,
        "cost": cost,
        "description": description,
        "is_retired": is_retired,
    }, pl


# ---------------------------------------------------------------------------
# Step 2: Build retirement payload
# ---------------------------------------------------------------------------

def build_retire_payload(state, args):
    print("\n" + "=" * 70)
    print("STEP 2: Build retireAsset Payload")
    print("=" * 70)

    # Determine cost and units to retire
    full_cost = state["cost"] or "0"
    full_units = state["total_units"]

    if args.units_to_retire:
        units_to_retire = int(args.units_to_retire)
        if units_to_retire > full_units:
            print(f"\n  ERROR: --units-to-retire ({units_to_retire}) exceeds total units ({full_units}).")
            sys.exit(1)
        is_partial = units_to_retire < full_units
        # For partial retirement, prorate cost by units
        try:
            cost_to_retire = str(round(float(full_cost) * units_to_retire / full_units, 2))
        except (ValueError, ZeroDivisionError):
            cost_to_retire = full_cost
    else:
        # Full retirement
        units_to_retire = full_units
        cost_to_retire = full_cost
        is_partial = False

    if args.cost_to_retire:
        cost_to_retire = args.cost_to_retire

    retirement_date = args.retirement_date or date.today().isoformat()
    retirement_type = args.retirement_type or "SALE"
    proceeds = args.proceeds or "0"
    cost_of_removal = args.cost_of_removal or "0"

    print(f"\n  Retirement Type        : {retirement_type}")
    print(f"  Retirement Date        : {retirement_date}")
    print(f"  Full/Partial           : {'PARTIAL' if is_partial else 'FULL'}")
    print(f"  Cost to Retire         : {cost_to_retire} (of {full_cost})")
    print(f"  Units to Retire        : {units_to_retire} (of {full_units})")
    print(f"  Proceeds of Sale       : {proceeds}")
    print(f"  Cost of Removal        : {cost_of_removal}")

    params = {
        "P_BOOK_TYPE_CODE": state["book_type_code"],
        "P_ASSET_ID": state["asset_id"],
        "P_TRANSACTION_DATE_ENTERED": retirement_date,
        "P_COST_RETIRED": cost_to_retire,
        "P_UNITS_RETIRED": str(units_to_retire),
        "P_RETIREMENT_TYPE_CODE": retirement_type,
        "P_PROCEEDS_OF_SALE": proceeds,
        "P_COST_OF_REMOVAL": cost_of_removal,
    }

    print(f"\n  ParameterList string:")
    print(f"    {build_parameter_list(params)[:2000]}")

    return params


# ---------------------------------------------------------------------------
# Step 3: Execute retirement
# ---------------------------------------------------------------------------

def execute_retire(base_url, api_version, jwt, params):
    handle = "retireAsset"
    print("\n" + "=" * 70)
    print(f"STEP 3: Execute {handle}")
    print("=" * 70)

    raw, pl = call_fusion(base_url, api_version, jwt, handle, params)

    status = str(pl.get("X_RETURN_STATUS") or "").strip()

    print("\n" + "-" * 70)
    print("RETIREMENT RESULT")
    print("-" * 70)
    print(f"  X_RETURN_STATUS           : {status or 'N/A'}")
    print(f"  X_RETIREMENT_ID           : {pl.get('X_RETIREMENT_ID', 'N/A')}")
    print(f"  X_TRANSACTION_HEADER_ID   : {pl.get('X_TRANSACTION_HEADER_ID', 'N/A')}")
    print(f"  X_MSG_COUNT               : {pl.get('X_MSG_COUNT', 'N/A')}")
    print(f"  X_MSG_DATA                : {pl.get('X_MSG_DATA', 'N/A')}")

    if status == "S":
        print("\n  >>> RETIREMENT SUCCEEDED <<<")
    else:
        print(f"\n  >>> RETIREMENT FAILED (status={status}) <<<")
        print(f"\n  Full ParameterList:\n{json.dumps(pl, indent=2)[:3000]}")

    return raw, pl


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Test Fusion Fixed Asset retirement. Queries asset info and builds/executes retireAsset.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run (default) - show retirement payload without executing:
  python test_retire.py --asset-number 125183 --asset-id 706 --book-code "UK CORP BOOK"

  # Full retirement as SALE with proceeds:
  python test_retire.py --asset-number 125183 --asset-id 706 --book-code "UK CORP BOOK" \\
      --proceeds 50000 --execute

  # Partial retirement (retire 3 units):
  python test_retire.py --asset-number 125183 --asset-id 706 --book-code "UK CORP BOOK" \\
      --units-to-retire 3 --execute

  # Retirement as ABANDONMENT (no proceeds):
  python test_retire.py --asset-number 125183 --asset-id 706 --book-code "UK CORP BOOK" \\
      --retirement-type ABANDONMENT --execute

  # Retire with specific date and cost override:
  python test_retire.py --asset-number 125183 --asset-id 706 --book-code "UK CORP BOOK" \\
      --retirement-date 2026-01-31 --cost-to-retire 10000 --execute
        """,
    )
    parser.add_argument("--asset-number", required=True, help="Asset number (e.g. 125183)")
    parser.add_argument("--asset-id", required=True,
                        help="Asset ID (required — look it up in Oracle FA)")
    parser.add_argument("--book-code", required=True, help="Book type code (e.g. 'UK CORP BOOK')")

    parser.add_argument("--retirement-type", default=None,
                        help="Retirement type code (default: SALE). Common values: SALE, ABANDONMENT.")
    parser.add_argument("--retirement-date", default=None,
                        help="Retirement date in YYYY-MM-DD format (default: today)")
    parser.add_argument("--proceeds", default=None,
                        help="Proceeds of sale (default: 0)")
    parser.add_argument("--cost-of-removal", default=None,
                        help="Cost of removal (default: 0)")
    parser.add_argument("--units-to-retire", default=None,
                        help="Number of units to retire. Defaults to all units (full retirement). "
                             "Specify a smaller number for partial retirement.")
    parser.add_argument("--cost-to-retire", default=None,
                        help="Override cost to retire. Defaults to full cost (or prorated for partial).")

    parser.add_argument("--execute", action="store_true", default=False,
                        help="Actually POST the retireAsset call. Without this flag, only dry-run.")

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

    print("=" * 70)
    print("  FUSION ASSET RETIREMENT TEST TOOL")
    print("=" * 70)
    print(f"  Fusion Host            : {base_url}")
    print(f"  API Version            : {api_version}")
    print(f"  Asset Number           : {args.asset_number}")
    print(f"  Asset ID               : {args.asset_id}")
    print(f"  Book                   : {args.book_code}")
    print(f"  Retirement Type        : {args.retirement_type or 'SALE (default)'}")
    print(f"  Retirement Date        : {args.retirement_date or 'today'}")
    print(f"  Mode                   : {'EXECUTE' if args.execute else 'DRY-RUN'}")

    # Step 1: Get asset information
    state, raw_pl = get_asset_info(base_url, api_version, jwt_token,
                                   args.book_code, args.asset_number,
                                   cli_asset_id=args.asset_id)

    # Block if already retired
    if state["is_retired"]:
        print("\n" + "!" * 70)
        print("  ERROR: Asset is already RETIRED — cannot retire again.")
        print("!" * 70)
        sys.exit(1)

    # Step 2: Build retirement payload
    params = build_retire_payload(state, args)

    # Step 3: Execute or dry-run
    if not args.execute:
        print("\n" + "=" * 70)
        print("DRY-RUN COMPLETE")
        print("  Retirement payload built but NOT sent to Fusion.")
        print("  Add --execute flag to actually perform the retirement.")
        print("=" * 70)

        output_file = f"test_retire_payload_{args.asset_number}.json"
        with open(output_file, "w") as f:
            json.dump({
                "asset_state": state,
                "retire_params": params,
                "raw_getAssetInformation": raw_pl,
            }, f, indent=2)
        print(f"\n  Payload saved to: {output_file}")

        print("\n" + "-" * 70)
        print("NEXT STEPS after retirement:")
        print("-" * 70)
        print("  1. Run this tool with --execute to perform the retirement")
        print("  2. Run Depreciation for the book/period to process the retirement")
        print("     (Calculate Depreciation scheduled process in Oracle FA)")
        print("  3. Create Accounting to generate GL journal entries")
        print("  4. Close the FA period")
        return

    raw, pl = execute_retire(base_url, api_version, jwt_token, params)

    # Save full results
    output_file = f"test_retire_result_{args.asset_number}.json"
    with open(output_file, "w") as f:
        json.dump({
            "asset_state": state,
            "retire_params": params,
            "retire_response_raw": raw,
            "retire_response_pl": pl,
        }, f, indent=2)
    print(f"\n  Full results saved to: {output_file}")

    print("\n" + "-" * 70)
    print("NEXT STEPS:")
    print("-" * 70)
    print("  1. Run Depreciation for the book/period to process the retirement")
    print("     (Calculate Depreciation scheduled process in Oracle FA)")
    print("  2. Create Accounting to generate GL journal entries")
    print("  3. Close the FA period")


if __name__ == "__main__":
    main()
