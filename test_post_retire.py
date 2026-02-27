#!/usr/bin/env python3
"""
Post-retirement pipeline for Oracle Fusion Fixed Assets.

Extends the original post-retire runner to cover the full "next steps
after an asset retirement" flow, using FA REST Transaction handles:

  0)  getAssetBookInformation
        -> auto-resolves current open period (X_PERIOD_NAME, X_DEPRN_RUN)
  0b) getAssetRetirementHistory
        -> auto-resolves latest retirement id (X_RETIREMENT_ID_TBL, X_STATUS_TBL)
  0c) updateAssetRetirementDescriptiveDetails
        -> update reference number / DFFs on the retirement record

Then the period/accounting steps:
  1) calculateDepreciation   (processTransaction)
  2) createAccounting        (processTransaction)
  3) closePeriod             (processTransaction)

Auth (supports both):
  * JWT bearer  — env: FUSION_JWT
  * Basic auth  — env: FUSION_USERNAME + FUSION_PASSWORD, or FUSION_BASIC_AUTH

Env:
  FUSION_BASE_URL       e.g. https://your-fusion-host
  FUSION_API_VERSION    e.g. 11.13.18.05
  FUSION_JWT            raw JWT token (Bearer)
  FUSION_USERNAME       basic auth username (if not using JWT)
  FUSION_PASSWORD       basic auth password (if not using JWT)
  FUSION_BASIC_AUTH     base64("user:pass") (alternative to username/password)

Examples:
  # Dry-run: resolve period + retirement id, show what would run
  python test_post_retire.py --book-code "OPS CORP" --asset-id 121161

  # Execute depreciation + accounting (period auto-resolved)
  python test_post_retire.py --book-code "OPS CORP" --execute

  # Execute all steps including close period (requires --close-period safety flag)
  python test_post_retire.py --book-code "OPS CORP" --step all --execute --close-period

  # Update retirement reference number, then run depreciation + accounting
  python test_post_retire.py --book-code "OPS CORP" --asset-id 121161 \
      --ret-reference-num RET01 --execute

  # Run only create accounting for explicit period
  python test_post_retire.py --book-code "OPS CORP" --step 2 --period-name "Jan-26" --execute

  # Run only close period
  python test_post_retire.py --book-code "OPS CORP" --step 3 --period-name "Jan-26" \
      --execute --close-period
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


# ---------------------------------------------------------------------------
# Utility helpers
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
    """Build ERP Integrations 'ParameterList' string in Rosetta format."""
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
        sv = str(v).strip()
        if _NUMERIC_RE.match(sv):
            items.append(f"{k}: {sv}")
        else:
            items.append(f"{k}:{_quote_single(sv)}")
    return "{" + ", ".join(items) + "}"


def split_tbl_value(v: Any) -> List[str]:
    """Split comma-separated JTF_*_TABLE values returned by Fusion."""
    if v is None:
        return []
    return [p.strip() for p in str(v).split(",")]


def now_utc_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@dataclass
class FusionAuth:
    mode: str  # "bearer" or "basic"
    header_value: str  # full Authorization header value


def build_auth_from_env() -> FusionAuth:
    jwt = (os.environ.get("FUSION_JWT") or "").strip()
    if jwt:
        return FusionAuth(mode="bearer", header_value=f"Bearer {jwt}")

    basic_b64 = (os.environ.get("FUSION_BASIC_AUTH") or "").strip()
    if basic_b64:
        return FusionAuth(mode="basic", header_value=f"Basic {basic_b64}")

    user = (os.environ.get("FUSION_USERNAME") or "").strip()
    pw = (os.environ.get("FUSION_PASSWORD") or "").strip()
    if user and pw:
        b64 = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")
        return FusionAuth(mode="basic", header_value=f"Basic {b64}")

    raise RuntimeError(
        "Missing auth env vars. Set FUSION_JWT (Bearer) OR "
        "FUSION_BASIC_AUTH (base64 user:pass) OR FUSION_USERNAME/FUSION_PASSWORD."
    )


# ---------------------------------------------------------------------------
# Fusion API caller
# ---------------------------------------------------------------------------

def call_fusion(
    base_url: str,
    api_version: str,
    auth: FusionAuth,
    handle: str,
    params: Dict[str, Any],
    verify_ssl: bool = False,
    timeout_seconds: int = 120,
    audit_dir: Optional[Path] = None,
    audit_tag: Optional[str] = None,
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
        "Authorization": auth.header_value,
    })

    print(f"\n>>> POST {url}")
    print(f">>> Operation: {op_name}")
    print(f">>> ParameterList: {payload['ParameterList'][:2000]}")

    resp = session.post(url, json=payload, timeout=timeout_seconds, verify=verify_ssl)
    try:
        raw = resp.json()
    except Exception:
        raise RuntimeError(f"Non-JSON response (status={resp.status_code}): {resp.text[:1000]}")

    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {json.dumps(raw, indent=2)[:4000]}")

    pl_raw = raw.get("ParameterList")
    pl: Dict[str, Any] = {}
    if isinstance(pl_raw, str) and pl_raw.strip():
        try:
            pl = json.loads(pl_raw)
        except Exception:
            pl = {"_raw": pl_raw}
    elif isinstance(pl_raw, dict):
        pl = pl_raw

    # Audit trail
    if audit_dir:
        audit_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "ts_utc": now_utc_iso(),
            "audit_tag": audit_tag,
            "url": url,
            "request": {
                "OperationName": payload["OperationName"],
                "ParameterList": payload["ParameterList"],
                "AuthorizationMode": auth.mode,
            },
            "http_status": resp.status_code,
            "response": raw,
            "parsed_parameter_list": pl,
        }
        fn = f"{_dt.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}_{handle}"
        if audit_tag:
            fn += f"_{re.sub(r'[^A-Za-z0-9_.-]+', '_', audit_tag)[:80]}"
        fn += ".json"
        (audit_dir / fn).write_text(json.dumps(record, indent=2), encoding="utf-8")

    return raw, pl


def classify_status(pl: Dict[str, Any]) -> str:
    return str(pl.get("X_RETURN_STATUS") or "").strip()


# ---------------------------------------------------------------------------
# Step 0: Get Asset Book Information (resolve current open period)
# ---------------------------------------------------------------------------

def run_get_asset_book_information(base_url, api_version, auth, book_code, verify_ssl, timeout_seconds, audit_dir):
    print("\n" + "=" * 70)
    print("STEP 0: Get Asset Book Information (current open period)")
    print("=" * 70)

    params = {"P_BOOK_TYPE_CODE": book_code}
    raw, pl = call_fusion(
        base_url, api_version, auth, "getAssetBookInformation", params,
        verify_ssl=verify_ssl, timeout_seconds=timeout_seconds,
        audit_dir=audit_dir, audit_tag=f"book={book_code}",
    )
    status = classify_status(pl)
    period = str(pl.get("X_PERIOD_NAME") or "").strip()
    deprn_run = str(pl.get("X_DEPRN_RUN") or "").strip()

    print("\n" + "-" * 70)
    print("BOOK INFO RESULT")
    print("-" * 70)
    print(f"  X_RETURN_STATUS : {status or 'N/A'}")
    print(f"  X_PERIOD_NAME   : {period or 'N/A'}")
    print(f"  X_DEPRN_RUN     : {deprn_run or 'N/A'}")
    print(f"  X_MSG_COUNT     : {pl.get('X_MSG_COUNT', 'N/A')}")
    print(f"  X_MSG_DATA      : {pl.get('X_MSG_DATA', 'N/A')}")

    return (status == "S"), raw, pl


# ---------------------------------------------------------------------------
# Step 0b: Get Asset Retirement History (resolve retirement id)
# ---------------------------------------------------------------------------

def run_get_asset_retirement_history(base_url, api_version, auth, book_code, asset_id, verify_ssl, timeout_seconds, audit_dir):
    print("\n" + "=" * 70)
    print("STEP 0b: Get Asset Retirement History (resolve retirement id)")
    print("=" * 70)

    params = {"P_BOOK_TYPE_CODE": book_code, "P_ASSET_ID": asset_id}
    raw, pl = call_fusion(
        base_url, api_version, auth, "getAssetRetirementHistory", params,
        verify_ssl=verify_ssl, timeout_seconds=timeout_seconds,
        audit_dir=audit_dir, audit_tag=f"book={book_code}_asset={asset_id}",
    )
    status = classify_status(pl)

    print("\n" + "-" * 70)
    print("RETIREMENT HISTORY RESULT")
    print("-" * 70)
    print(f"  X_RETURN_STATUS       : {status or 'N/A'}")
    print(f"  X_MSG_COUNT           : {pl.get('X_MSG_COUNT', 'N/A')}")
    print(f"  X_MSG_DATA            : {pl.get('X_MSG_DATA', 'N/A')}")
    print(f"  X_RETIREMENT_ID_TBL   : {pl.get('X_RETIREMENT_ID_TBL', 'N/A')}")
    print(f"  X_STATUS_TBL          : {pl.get('X_STATUS_TBL', 'N/A')}")
    print(f"  X_RETIREMENT_DATE_TBL : {pl.get('X_RETIREMENT_DATE_TBL', 'N/A')}")
    print(f"  X_REFERENCE_NUM_TBL   : {pl.get('X_REFERENCE_NUM_TBL', 'N/A')}")

    return (status == "S"), raw, pl


def pick_latest_retirement_id(pl: Dict[str, Any]) -> Optional[int]:
    """Pick the latest non-deleted retirement id from the history response."""
    ids = split_tbl_value(pl.get("X_RETIREMENT_ID_TBL"))
    statuses = split_tbl_value(pl.get("X_STATUS_TBL"))
    parsed: List[Tuple[int, str]] = []
    for i, id_s in enumerate(ids):
        id_s = id_s.strip()
        if not id_s:
            continue
        try:
            rid = int(float(id_s))
        except Exception:
            continue
        st = statuses[i].strip().upper() if i < len(statuses) else ""
        parsed.append((rid, st))

    if not parsed:
        return None
    non_deleted = [rid for rid, st in parsed if st and st != "DELETED"]
    if non_deleted:
        return max(non_deleted)
    return max(rid for rid, _ in parsed)


# ---------------------------------------------------------------------------
# Step 0c: Update Retirement Descriptive Details
# ---------------------------------------------------------------------------

def run_update_retirement_descriptive_details(
    base_url, api_version, auth, book_code, asset_id, retirement_id, update_params,
    verify_ssl, timeout_seconds, audit_dir,
):
    print("\n" + "=" * 70)
    print("STEP 0c: Update Asset Retirement Descriptive Details")
    print("=" * 70)

    params = {
        "P_BOOK_TYPE_CODE": book_code,
        "P_ASSET_ID": asset_id,
        "P_RETIREMENT_ID": retirement_id,
    }
    params.update(update_params)

    raw, pl = call_fusion(
        base_url, api_version, auth, "updateAssetRetirementDescriptiveDetails", params,
        verify_ssl=verify_ssl, timeout_seconds=timeout_seconds,
        audit_dir=audit_dir, audit_tag=f"book={book_code}_asset={asset_id}_ret={retirement_id}",
    )
    status = classify_status(pl)

    print("\n" + "-" * 70)
    print("UPDATE RETIREMENT DESCRIPTIVE DETAILS RESULT")
    print("-" * 70)
    print(f"  X_RETURN_STATUS : {status or 'N/A'}")
    print(f"  X_MSG_COUNT     : {pl.get('X_MSG_COUNT', 'N/A')}")
    print(f"  X_MSG_DATA      : {pl.get('X_MSG_DATA', 'N/A')}")

    return (status == "S"), raw, pl


# ---------------------------------------------------------------------------
# Step 1: Calculate Depreciation
# ---------------------------------------------------------------------------

def run_calculate_depreciation(base_url, api_version, auth, book_code, period_name, verify_ssl, timeout_seconds, audit_dir):
    print("\n" + "=" * 70)
    print("STEP 1: Calculate Depreciation")
    print("=" * 70)

    params = {"P_BOOK_TYPE_CODE": book_code}
    if period_name:
        params["P_PERIOD_NAME"] = period_name

    raw, pl = call_fusion(
        base_url, api_version, auth, "calculateDepreciation", params,
        verify_ssl=verify_ssl, timeout_seconds=timeout_seconds,
        audit_dir=audit_dir, audit_tag=f"book={book_code}_period={period_name or 'current'}",
    )
    status = classify_status(pl)

    print("\n" + "-" * 70)
    print("DEPRECIATION RESULT")
    print("-" * 70)
    print(f"  X_RETURN_STATUS : {status or 'N/A'}")
    print(f"  X_MSG_COUNT     : {pl.get('X_MSG_COUNT', 'N/A')}")
    print(f"  X_MSG_DATA      : {pl.get('X_MSG_DATA', 'N/A')}")

    if status == "S":
        print("\n  >>> DEPRECIATION SUCCEEDED <<<")
    else:
        print(f"\n  >>> DEPRECIATION FAILED (status={status}) <<<")
        print(f"\n  Full ParameterList:\n{json.dumps(pl, indent=2)[:3000]}")

    return (status == "S"), raw, pl


# ---------------------------------------------------------------------------
# Step 2: Create Accounting
# ---------------------------------------------------------------------------

def run_create_accounting(base_url, api_version, auth, book_code, period_name, verify_ssl, timeout_seconds, audit_dir):
    print("\n" + "=" * 70)
    print("STEP 2: Create Accounting")
    print("=" * 70)

    params = {"P_BOOK_TYPE_CODE": book_code}
    if period_name:
        params["P_PERIOD_NAME"] = period_name

    raw, pl = call_fusion(
        base_url, api_version, auth, "createAccounting", params,
        verify_ssl=verify_ssl, timeout_seconds=timeout_seconds,
        audit_dir=audit_dir, audit_tag=f"book={book_code}_period={period_name or 'current'}",
    )
    status = classify_status(pl)

    print("\n" + "-" * 70)
    print("CREATE ACCOUNTING RESULT")
    print("-" * 70)
    print(f"  X_RETURN_STATUS : {status or 'N/A'}")
    print(f"  X_MSG_COUNT     : {pl.get('X_MSG_COUNT', 'N/A')}")
    print(f"  X_MSG_DATA      : {pl.get('X_MSG_DATA', 'N/A')}")

    if status == "S":
        print("\n  >>> CREATE ACCOUNTING SUCCEEDED <<<")
    else:
        print(f"\n  >>> CREATE ACCOUNTING FAILED (status={status}) <<<")
        print(f"\n  Full ParameterList:\n{json.dumps(pl, indent=2)[:3000]}")

    return (status == "S"), raw, pl


# ---------------------------------------------------------------------------
# Step 3: Close Period
# ---------------------------------------------------------------------------

def run_close_period(base_url, api_version, auth, book_code, period_name, verify_ssl, timeout_seconds, audit_dir):
    print("\n" + "=" * 70)
    print("STEP 3: Close Period")
    print("=" * 70)

    if not period_name:
        raise RuntimeError("Close Period requires a period name (--period-name or auto-resolved).")

    params = {"P_BOOK_TYPE_CODE": book_code, "P_PERIOD_NAME": period_name}
    raw, pl = call_fusion(
        base_url, api_version, auth, "closePeriod", params,
        verify_ssl=verify_ssl, timeout_seconds=timeout_seconds,
        audit_dir=audit_dir, audit_tag=f"book={book_code}_period={period_name}",
    )
    status = classify_status(pl)

    print("\n" + "-" * 70)
    print("CLOSE PERIOD RESULT")
    print("-" * 70)
    print(f"  X_RETURN_STATUS : {status or 'N/A'}")
    print(f"  X_MSG_COUNT     : {pl.get('X_MSG_COUNT', 'N/A')}")
    print(f"  X_MSG_DATA      : {pl.get('X_MSG_DATA', 'N/A')}")

    if status == "S":
        print(f"\n  >>> PERIOD '{period_name}' CLOSED SUCCESSFULLY <<<")
    else:
        print(f"\n  >>> CLOSE PERIOD FAILED (status={status}) <<<")
        print(f"\n  Full ParameterList:\n{json.dumps(pl, indent=2)[:3000]}")

    return (status == "S"), raw, pl


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_kv_list(items: List[str]) -> Dict[str, str]:
    """Parse KEY=VALUE pairs from --ret-param args."""
    out: Dict[str, str] = {}
    for it in items:
        if "=" not in it:
            raise ValueError(f"Expected KEY=VALUE, got: {it}")
        k, v = it.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            raise ValueError(f"Empty key in {it}")
        out[k] = v
    return out


def _save_results(args, results: Dict[str, Any]) -> None:
    output_file = f"post_retire_result_{str(args.book_code).replace(' ', '_')}.json"
    serializable = {}
    for k, v in results.items():
        serializable[k] = {"success": v.get("success"), "raw": v.get("raw"), "pl": v.get("pl")}
    Path(output_file).write_text(json.dumps(serializable, indent=2), encoding="utf-8")
    print(f"\nResults saved to: {output_file}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Post-retirement pipeline: resolve period + retirement details + depreciation/accounting/close.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--book-code", required=True,
                        help="Book type code (e.g. 'OPS CORP')")
    parser.add_argument("--period-name", default=None,
                        help="Period name (e.g. 'Jan-26'). If omitted, auto-resolved via getAssetBookInformation.")
    parser.add_argument("--asset-id", type=int, default=None,
                        help="Asset ID. Needed to resolve retirement id or update retirement details.")
    parser.add_argument("--retirement-id", type=int, default=None,
                        help="Retirement ID. If omitted and --asset-id provided, auto-resolved via getAssetRetirementHistory.")
    parser.add_argument("--ret-reference-num", default=None,
                        help="If supplied, runs updateAssetRetirementDescriptiveDetails with P_REFERENCE_NUM=<value>.")
    parser.add_argument("--ret-param", action="append", default=[],
                        help="Additional retirement update param as KEY=VALUE. Repeatable. "
                             "Example: --ret-param P_RET_ATTRIBUTE1=foo")
    parser.add_argument("--step", default="all",
                        help="Which step(s): 1 (depreciation), 2 (create accounting), 3 (close period), or 'all' (default: all)")
    parser.add_argument("--close-period", action="store_true", default=False,
                        help="Safety switch: allow Step 3 (Close Period). Without this, step 3 is skipped.")
    parser.add_argument("--execute", action="store_true", default=False,
                        help="Actually execute. Without this flag, only dry-run.")
    parser.add_argument("--verify-ssl", action="store_true", default=False,
                        help="Enable SSL verification (default: disabled).")
    parser.add_argument("--timeout", type=int, default=120,
                        help="HTTP timeout seconds (default: 120)")
    parser.add_argument("--audit-dir", default="audit_post_retire",
                        help="Directory to save request/response audit JSON files.")

    args = parser.parse_args()

    base_url = (os.environ.get("FUSION_BASE_URL") or "").strip()
    api_version = (os.environ.get("FUSION_API_VERSION") or "").strip()
    if not base_url or not api_version:
        print("ERROR: Set these environment variables before running:")
        print("  FUSION_BASE_URL    = https://your-fusion-host")
        print("  FUSION_API_VERSION = 11.13.18.05")
        sys.exit(1)

    try:
        auth = build_auth_from_env()
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    verify_ssl = bool(args.verify_ssl)
    timeout_seconds = int(args.timeout)
    audit_dir = Path(args.audit_dir) if args.audit_dir else None

    # Parse steps
    step_names = {1: "Calculate Depreciation", 2: "Create Accounting", 3: "Close Period"}
    steps_to_run: List[int] = []
    step_val = args.step.strip().lower()
    if step_val == "all":
        steps_to_run = [1, 2, 3]
    else:
        try:
            s = int(step_val.split()[0])
            if s not in (1, 2, 3):
                raise ValueError
            steps_to_run = [s]
        except (ValueError, IndexError):
            print(f"ERROR: --step must be 1, 2, 3, or 'all'. Got: '{args.step}'")
            sys.exit(1)

    # Retirement update params
    update_params: Dict[str, Any] = {}
    if args.ret_reference_num is not None:
        update_params["P_REFERENCE_NUM"] = args.ret_reference_num
    if args.ret_param:
        update_params.update(parse_kv_list(args.ret_param))
    want_update_retirement = bool(update_params)

    print("=" * 70)
    print("  FUSION POST-RETIREMENT PIPELINE")
    print("=" * 70)
    print(f"  Fusion Host    : {base_url}")
    print(f"  API Version    : {api_version}")
    print(f"  Auth Mode      : {auth.mode}")
    print(f"  Book           : {args.book_code}")
    print(f"  Asset ID       : {args.asset_id or '(not provided)'}")
    print(f"  Retirement ID  : {args.retirement_id or '(auto if asset-id provided)'}")
    print(f"  Period         : {args.period_name or '(auto: current open period)'}")
    print(f"  Steps          : {', '.join(step_names[s] for s in steps_to_run)}")
    print(f"  Close Period?  : {'YES (enabled)' if args.close_period else 'NO (disabled unless --close-period)'}")
    print(f"  Mode           : {'EXECUTE' if args.execute else 'DRY-RUN'}")

    # ── DRY-RUN ──────────────────────────────────────────────────────────
    if not args.execute:
        print("\n" + "=" * 70)
        print("DRY-RUN — showing what would be executed")
        print("=" * 70)

        print("\n  [0] getAssetBookInformation")
        print(f"      Handle: processTransaction-getAssetBookInformation")
        print(f"      Params: {build_parameter_list({'P_BOOK_TYPE_CODE': args.book_code})}")

        if args.asset_id and not args.retirement_id:
            print("\n  [0b] getAssetRetirementHistory (to resolve retirement id)")
            print(f"      Handle: processTransaction-getAssetRetirementHistory")
            print(f"      Params: {build_parameter_list({'P_BOOK_TYPE_CODE': args.book_code, 'P_ASSET_ID': args.asset_id})}")

        if want_update_retirement:
            print("\n  [0c] updateAssetRetirementDescriptiveDetails")
            tmp: Dict[str, Any] = {
                "P_BOOK_TYPE_CODE": args.book_code,
                "P_ASSET_ID": args.asset_id or "<REQUIRED>",
                "P_RETIREMENT_ID": args.retirement_id or "<AUTO_FROM_HISTORY>",
            }
            tmp.update(update_params)
            print(f"      Handle: processTransaction-updateAssetRetirementDescriptiveDetails")
            print(f"      Params: {build_parameter_list(tmp)}")

        for s in steps_to_run:
            if s == 3 and not args.close_period:
                print(f"\n  [{s}] {step_names[s]} (SKIPPED — enable with --close-period)")
                continue
            params: Dict[str, Any] = {"P_BOOK_TYPE_CODE": args.book_code}
            if args.period_name:
                params["P_PERIOD_NAME"] = args.period_name
            handle = {1: "calculateDepreciation", 2: "createAccounting", 3: "closePeriod"}[s]
            print(f"\n  [{s}] {step_names[s]}")
            print(f"      Handle: processTransaction-{handle}")
            print(f"      Params: {build_parameter_list(params)}")

        print("\n" + "=" * 70)
        print("DRY-RUN COMPLETE")
        print("  Add --execute flag to actually run these steps.")
        print("=" * 70)
        return

    # ── EXECUTION ────────────────────────────────────────────────────────
    results: Dict[str, Any] = {}

    # Step 0: Resolve current open period
    ok, raw, pl = run_get_asset_book_information(
        base_url, api_version, auth, args.book_code,
        verify_ssl, timeout_seconds, audit_dir,
    )
    results["getAssetBookInformation"] = {"success": ok, "raw": raw, "pl": pl}
    if not ok:
        print("\nStopping — getAssetBookInformation failed.")
        _save_results(args, results)
        sys.exit(1)

    resolved_period = args.period_name or str(pl.get("X_PERIOD_NAME") or "").strip() or None

    # Step 0b: Resolve retirement id (if asset-id provided and no explicit retirement-id)
    resolved_retirement_id: Optional[int] = args.retirement_id

    if not resolved_retirement_id and args.asset_id:
        ok, raw, pl = run_get_asset_retirement_history(
            base_url, api_version, auth, args.book_code, args.asset_id,
            verify_ssl, timeout_seconds, audit_dir,
        )
        results["getAssetRetirementHistory"] = {"success": ok, "raw": raw, "pl": pl}
        if not ok:
            print("\nStopping — getAssetRetirementHistory failed.")
            _save_results(args, results)
            sys.exit(1)

        resolved_retirement_id = pick_latest_retirement_id(pl)
        print(f"\nResolved retirement id: {resolved_retirement_id if resolved_retirement_id else 'N/A'}")

    # Step 0c: Update retirement descriptive details
    if want_update_retirement:
        if not args.asset_id:
            raise RuntimeError("Updating retirement descriptive details requires --asset-id.")
        if not resolved_retirement_id:
            raise RuntimeError(
                "Updating retirement descriptive details requires --retirement-id "
                "or a resolvable retirement history."
            )
        ok, raw, pl = run_update_retirement_descriptive_details(
            base_url, api_version, auth, args.book_code, args.asset_id,
            resolved_retirement_id, update_params,
            verify_ssl, timeout_seconds, audit_dir,
        )
        results["updateAssetRetirementDescriptiveDetails"] = {"success": ok, "raw": raw, "pl": pl}
        if not ok:
            print("\nStopping — updateAssetRetirementDescriptiveDetails failed.")
            _save_results(args, results)
            sys.exit(1)

    # Step 1: Calculate Depreciation
    if 1 in steps_to_run:
        ok, raw, pl = run_calculate_depreciation(
            base_url, api_version, auth, args.book_code, resolved_period,
            verify_ssl, timeout_seconds, audit_dir,
        )
        results["calculateDepreciation"] = {"success": ok, "raw": raw, "pl": pl}
        if not ok:
            print("\nStopping — depreciation failed.")
            _save_results(args, results)
            sys.exit(1)

    # Step 2: Create Accounting
    if 2 in steps_to_run:
        ok, raw, pl = run_create_accounting(
            base_url, api_version, auth, args.book_code, resolved_period,
            verify_ssl, timeout_seconds, audit_dir,
        )
        results["createAccounting"] = {"success": ok, "raw": raw, "pl": pl}
        if not ok:
            print("\nStopping — create accounting failed.")
            _save_results(args, results)
            sys.exit(1)

    # Step 3: Close Period (requires --close-period safety switch)
    if 3 in steps_to_run:
        if not args.close_period:
            print("\nNOTE: Step 3 (Close Period) skipped. Re-run with --close-period to enable.")
        else:
            if not resolved_period:
                raise RuntimeError(
                    "Close period requires a resolved --period-name "
                    "or a valid X_PERIOD_NAME from book info."
                )
            ok, raw, pl = run_close_period(
                base_url, api_version, auth, args.book_code, resolved_period,
                verify_ssl, timeout_seconds, audit_dir,
            )
            results["closePeriod"] = {"success": ok, "raw": raw, "pl": pl}
            if not ok:
                _save_results(args, results)
                sys.exit(1)

    # Summary
    print("\n" + "=" * 70)
    print("PIPELINE COMPLETED")
    print("=" * 70)
    for step_name, result in results.items():
        print(f"  {step_name:<45} : {'OK' if result.get('success') else 'FAILED'}")

    _save_results(args, results)


if __name__ == "__main__":
    main()
