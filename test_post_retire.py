#!/usr/bin/env python3
"""
Post-retirement processing for Oracle Fusion Fixed Assets.

Runs the steps that follow after an asset has been retired:
  Step 1: Calculate Depreciation  (processTransaction-calculateDepreciation)
  Step 2: Create Accounting       (processTransaction-createAccounting)
  Step 3: Close Period            (processTransaction-closePeriod)

Each step can be run individually (--step 1, --step 2, --step 3)
or all together (--step all, the default).

Uses the same env vars as test_transfer.py:
  FUSION_BASE_URL    e.g. https://your-fusion-host
  FUSION_API_VERSION e.g. 11.13.18.05
  FUSION_JWT         raw JWT token

Usage examples:
  # Dry-run all steps:
  python test_post_retire.py --book-code "UK CORP BOOK"

  # Execute all steps:
  python test_post_retire.py --book-code "UK CORP BOOK" --execute

  # Run only depreciation:
  python test_post_retire.py --book-code "UK CORP BOOK" --step 1 --execute

  # Run only create accounting:
  python test_post_retire.py --book-code "UK CORP BOOK" --step 2 --execute

  # Run only close period:
  python test_post_retire.py --book-code "UK CORP BOOK" --step 3 --period-name "Jan-26" --execute
"""

import argparse
import json
import os
import re
import sys
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
    timeout_seconds: int = 120,
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
# Step 1: Calculate Depreciation
# ---------------------------------------------------------------------------

def run_calculate_depreciation(base_url, api_version, jwt, book_code, period_name=None):
    print("\n" + "=" * 70)
    print("STEP 1: Calculate Depreciation")
    print("=" * 70)

    params = {
        "P_BOOK_TYPE_CODE": book_code,
    }
    if period_name:
        params["P_PERIOD_NAME"] = period_name

    print(f"  Book Type Code : {book_code}")
    print(f"  Period Name    : {period_name or '(current open period)'}")
    print(f"  ParameterList  : {build_parameter_list(params)}")

    raw, pl = call_fusion(base_url, api_version, jwt, "calculateDepreciation", params)

    status = str(pl.get("X_RETURN_STATUS") or "").strip()

    print("\n" + "-" * 70)
    print("DEPRECIATION RESULT")
    print("-" * 70)
    print(f"  X_RETURN_STATUS  : {status or 'N/A'}")
    print(f"  X_MSG_COUNT      : {pl.get('X_MSG_COUNT', 'N/A')}")
    print(f"  X_MSG_DATA       : {pl.get('X_MSG_DATA', 'N/A')}")

    if status == "S":
        print("\n  >>> DEPRECIATION SUCCEEDED <<<")
    else:
        print(f"\n  >>> DEPRECIATION FAILED (status={status}) <<<")
        print(f"\n  Full ParameterList:\n{json.dumps(pl, indent=2)[:3000]}")

    return status == "S", raw, pl


# ---------------------------------------------------------------------------
# Step 2: Create Accounting
# ---------------------------------------------------------------------------

def run_create_accounting(base_url, api_version, jwt, book_code, period_name=None):
    print("\n" + "=" * 70)
    print("STEP 2: Create Accounting")
    print("=" * 70)

    params = {
        "P_BOOK_TYPE_CODE": book_code,
    }
    if period_name:
        params["P_PERIOD_NAME"] = period_name

    print(f"  Book Type Code : {book_code}")
    print(f"  Period Name    : {period_name or '(current open period)'}")
    print(f"  ParameterList  : {build_parameter_list(params)}")

    raw, pl = call_fusion(base_url, api_version, jwt, "createAccounting", params)

    status = str(pl.get("X_RETURN_STATUS") or "").strip()

    print("\n" + "-" * 70)
    print("CREATE ACCOUNTING RESULT")
    print("-" * 70)
    print(f"  X_RETURN_STATUS  : {status or 'N/A'}")
    print(f"  X_MSG_COUNT      : {pl.get('X_MSG_COUNT', 'N/A')}")
    print(f"  X_MSG_DATA       : {pl.get('X_MSG_DATA', 'N/A')}")

    if status == "S":
        print("\n  >>> CREATE ACCOUNTING SUCCEEDED <<<")
    else:
        print(f"\n  >>> CREATE ACCOUNTING FAILED (status={status}) <<<")
        print(f"\n  Full ParameterList:\n{json.dumps(pl, indent=2)[:3000]}")

    return status == "S", raw, pl


# ---------------------------------------------------------------------------
# Step 3: Close Period
# ---------------------------------------------------------------------------

def run_close_period(base_url, api_version, jwt, book_code, period_name):
    print("\n" + "=" * 70)
    print("STEP 3: Close Period")
    print("=" * 70)

    if not period_name:
        print("  ERROR: --period-name is required for Close Period.")
        print("         Example: --period-name 'Jan-26'")
        sys.exit(1)

    params = {
        "P_BOOK_TYPE_CODE": book_code,
        "P_PERIOD_NAME": period_name,
    }

    print(f"  Book Type Code : {book_code}")
    print(f"  Period Name    : {period_name}")
    print(f"  ParameterList  : {build_parameter_list(params)}")

    raw, pl = call_fusion(base_url, api_version, jwt, "closePeriod", params)

    status = str(pl.get("X_RETURN_STATUS") or "").strip()

    print("\n" + "-" * 70)
    print("CLOSE PERIOD RESULT")
    print("-" * 70)
    print(f"  X_RETURN_STATUS  : {status or 'N/A'}")
    print(f"  X_MSG_COUNT      : {pl.get('X_MSG_COUNT', 'N/A')}")
    print(f"  X_MSG_DATA       : {pl.get('X_MSG_DATA', 'N/A')}")

    if status == "S":
        print(f"\n  >>> PERIOD '{period_name}' CLOSED SUCCESSFULLY <<<")
    else:
        print(f"\n  >>> CLOSE PERIOD FAILED (status={status}) <<<")
        print(f"\n  Full ParameterList:\n{json.dumps(pl, indent=2)[:3000]}")

    return status == "S", raw, pl


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Post-retirement processing: Depreciation, Create Accounting, Close Period.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run all steps:
  python test_post_retire.py --book-code "UK CORP BOOK"

  # Execute all steps:
  python test_post_retire.py --book-code "UK CORP BOOK" --execute

  # Run only Calculate Depreciation:
  python test_post_retire.py --book-code "UK CORP BOOK" --step 1 --execute

  # Run only Create Accounting:
  python test_post_retire.py --book-code "UK CORP BOOK" --step 2 --execute

  # Run only Close Period:
  python test_post_retire.py --book-code "UK CORP BOOK" --step 3 --period-name "Jan-26" --execute

  # Run with specific period:
  python test_post_retire.py --book-code "UK CORP BOOK" --period-name "Feb-26" --execute
        """,
    )
    parser.add_argument("--book-code", required=True, help="Book type code (e.g. 'UK CORP BOOK')")
    parser.add_argument("--period-name", default=None,
                        help="Period name (e.g. 'Jan-26'). Required for Close Period. "
                             "If omitted for depreciation/accounting, uses current open period.")
    parser.add_argument("--step", default="all",
                        help="Which step(s) to run: 1 (depreciation), 2 (create accounting), "
                             "3 (close period), or 'all' (default: all)")
    parser.add_argument("--execute", action="store_true", default=False,
                        help="Actually execute. Without this flag, only dry-run.")

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

    steps_to_run = []
    step_val = args.step.strip()
    if step_val == "all":
        steps_to_run = [1, 2, 3]
    else:
        # Accept "1", "1 (Depreciation)", etc.
        try:
            s = int(step_val.split()[0])
            if s not in (1, 2, 3):
                raise ValueError
            steps_to_run = [s]
        except (ValueError, IndexError):
            print(f"ERROR: --step must be 1, 2, 3, or 'all'. Got: '{args.step}'")
            sys.exit(1)

    step_names = {1: "Calculate Depreciation", 2: "Create Accounting", 3: "Close Period"}

    print("=" * 70)
    print("  FUSION POST-RETIREMENT PROCESSING")
    print("=" * 70)
    print(f"  Fusion Host    : {base_url}")
    print(f"  API Version    : {api_version}")
    print(f"  Book           : {args.book_code}")
    print(f"  Period         : {args.period_name or '(current open period)'}")
    print(f"  Steps          : {', '.join(step_names[s] for s in steps_to_run)}")
    print(f"  Mode           : {'EXECUTE' if args.execute else 'DRY-RUN'}")

    if not args.execute:
        print("\n" + "=" * 70)
        print("DRY-RUN — showing what would be executed")
        print("=" * 70)

        for s in steps_to_run:
            print(f"\n  [{s}] {step_names[s]}")
            params = {"P_BOOK_TYPE_CODE": args.book_code}
            if args.period_name:
                params["P_PERIOD_NAME"] = args.period_name
            if s == 1:
                handle = "calculateDepreciation"
            elif s == 2:
                handle = "createAccounting"
            else:
                handle = "closePeriod"
                if not args.period_name:
                    print("      WARNING: --period-name required for Close Period")
            print(f"      Handle: processTransaction-{handle}")
            print(f"      Params: {build_parameter_list(params)}")

        print("\n" + "=" * 70)
        print("DRY-RUN COMPLETE")
        print("  Add --execute flag to actually run these steps.")
        print("=" * 70)
        return

    # Execute steps sequentially
    results = {}

    if 1 in steps_to_run:
        ok, raw, pl = run_calculate_depreciation(
            base_url, api_version, jwt_token, args.book_code, args.period_name)
        results["calculateDepreciation"] = {"success": ok, "raw": raw, "pl": pl}
        if not ok:
            print("\n  Stopping — depreciation failed. Fix before proceeding.")
            _save_results(args, results)
            sys.exit(1)

    if 2 in steps_to_run:
        ok, raw, pl = run_create_accounting(
            base_url, api_version, jwt_token, args.book_code, args.period_name)
        results["createAccounting"] = {"success": ok, "raw": raw, "pl": pl}
        if not ok:
            print("\n  Stopping — create accounting failed. Fix before proceeding.")
            _save_results(args, results)
            sys.exit(1)

    if 3 in steps_to_run:
        ok, raw, pl = run_close_period(
            base_url, api_version, jwt_token, args.book_code, args.period_name)
        results["closePeriod"] = {"success": ok, "raw": raw, "pl": pl}
        if not ok:
            _save_results(args, results)
            sys.exit(1)

    # Summary
    print("\n" + "=" * 70)
    print("ALL STEPS COMPLETED SUCCESSFULLY")
    print("=" * 70)
    for step_name, result in results.items():
        status = "OK" if result["success"] else "FAILED"
        print(f"  {step_name:<30} : {status}")

    _save_results(args, results)


def _save_results(args, results):
    output_file = f"test_post_retire_result_{args.book_code.replace(' ', '_')}.json"
    serializable = {}
    for k, v in results.items():
        serializable[k] = {"success": v["success"], "raw": v["raw"], "pl": v["pl"]}
    with open(output_file, "w") as f:
        json.dump(serializable, f, indent=2)
    print(f"\n  Results saved to: {output_file}")


if __name__ == "__main__":
    main()
