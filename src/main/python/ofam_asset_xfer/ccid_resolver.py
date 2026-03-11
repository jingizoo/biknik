"""Option B: Derive destination EXPENSE_CCID by replacing the Company segment.

Given a source EXPENSE_CCID, look up its GL segments via accountCombinationsLOV,
swap the Company segment to the target value, and find (or fail on) the matching
destination CodeCombinationId.

All HTTP I/O goes through the injected client so this module is unit-testable
with a mock.
"""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from typing import Any, Dict, List, Optional, Protocol, Tuple

from .exceptions import FusionApiError, ValidationError

log = logging.getLogger(__name__)

# Segment fields returned by accountCombinationsLOV (Segment1 .. Segment30).
_SEGMENT_RE = re.compile(r"^Segment\d+$")


# ---------------------------------------------------------------------------
# Protocol for the HTTP client (allows mocking in tests)
# ---------------------------------------------------------------------------
class _RestClient(Protocol):
    def get_resource(
        self, resource_path: str, query_params: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]: ...


# ---------------------------------------------------------------------------
# 1) get_ccid_details
# ---------------------------------------------------------------------------
def get_ccid_details(client: _RestClient, ccid: int) -> Dict[str, Any]:
    """Look up a CodeCombinationId via accountCombinationsLOV PrimaryKey finder.

    Returns::

        {
            "ccid": 626955,
            "chart_of_accounts_id": 12345,          # may be None if not in response
            "concatenated_segments": "US01-100-...", # for logging
            "segments": {"Segment1": "US01", "Segment2": "100", ...},
        }
    """
    log.info("get_ccid_details: looking up CCID=%s", ccid)

    resp = client.get_resource(
        "accountCombinationsLOV",
        {
            "finder": f"PrimaryKey;_CODE_COMBINATION_ID={ccid}",
            "onlyData": "true",
        },
    )

    items = resp.get("items") or []
    if not items:
        raise FusionApiError(
            f"accountCombinationsLOV returned no items for CCID={ccid}. "
            f"Response: {resp}"
        )

    row = items[0]

    segments: Dict[str, str] = {}
    for key, val in row.items():
        if _SEGMENT_RE.match(key) and val is not None and str(val).strip():
            segments[key] = str(val).strip()

    if not segments:
        raise FusionApiError(
            f"No Segment* fields found in accountCombinationsLOV response for CCID={ccid}. "
            f"Row keys: {list(row.keys())}"
        )

    coa_id = row.get("ChartOfAccountsId") or row.get("chartOfAccountsId")
    concat_segs = (
        row.get("ConcatenatedSegments") or row.get("concatenatedSegments") or ""
    )

    result = {
        "ccid": ccid,
        "chart_of_accounts_id": int(coa_id) if coa_id else None,
        "concatenated_segments": str(concat_segs),
        "segments": segments,
    }

    log.info(
        "get_ccid_details: CCID=%s → coa=%s segments=%s concatenated=%s",
        ccid,
        result["chart_of_accounts_id"],
        segments,
        concat_segs,
    )
    return result


# ---------------------------------------------------------------------------
# 2) build_target_segments
# ---------------------------------------------------------------------------
def build_target_segments(
    src_segments: Dict[str, str],
    target_company: str,
    company_segment_key: str = "Segment1",
) -> Dict[str, str]:
    """Copy source segments and replace the Company segment with *target_company*.

    Raises ValidationError if *company_segment_key* is not present in
    *src_segments*.
    """
    if company_segment_key not in src_segments:
        raise ValidationError(
            f"Company segment key '{company_segment_key}' not found in source segments. "
            f"Available: {sorted(src_segments.keys())}"
        )

    target = deepcopy(src_segments)
    old_val = target[company_segment_key]
    target[company_segment_key] = target_company

    log.info(
        "build_target_segments: %s changed from '%s' to '%s'  (other segments unchanged)",
        company_segment_key,
        old_val,
        target_company,
    )
    return target


# ---------------------------------------------------------------------------
# 3) lookup_ccid_by_segments
# ---------------------------------------------------------------------------
def lookup_ccid_by_segments(
    client: _RestClient,
    target_segments: Dict[str, str],
    chart_of_accounts_id: Optional[int] = None,
) -> int:
    """Find an existing CodeCombinationId that matches all *target_segments*.

    Uses accountCombinationsLOV with a ``q`` filter.  If no match is found,
    raises a hard error with a clear message about the missing combination.
    """
    # Build q= filter:  Segment1='US01';Segment2='100';...
    # Sort segment keys numerically (Segment1, Segment2, ..., Segment10, ...)
    def _seg_sort_key(k: str) -> Tuple[Any, ...]:
        m = re.match(r"^(Segment)(\d+)$", k)
        if m:
            return (0, int(m.group(2)))
        return (1, k)

    sorted_keys = sorted(target_segments.keys(), key=_seg_sort_key)

    parts: List[str] = []
    for key in sorted_keys:
        parts.append(f"{key}={target_segments[key]}")
    if chart_of_accounts_id is not None:
        parts.append(f"ChartOfAccountsId={chart_of_accounts_id}")

    q_filter = ";".join(parts)

    # Request the segment fields we need for exact-match verification
    segment_fields = ",".join(sorted_keys)
    fields = (
        f"CodeCombinationId,ConcatenatedSegments,ChartOfAccountsId,{segment_fields}"
    )

    log.info("lookup_ccid_by_segments: q=%s fields=%s", q_filter, fields)

    resp = client.get_resource(
        "accountCombinationsLOV",
        {
            "q": q_filter,
            "onlyData": "true",
            "fields": fields,
        },
    )

    items = resp.get("items") or []

    if not items:
        segs_display = ", ".join(f"{k}={v}" for k, v in sorted(target_segments.items()))
        raise ValidationError(
            f"No account combination found for target segments: {segs_display}. "
            f"ChartOfAccountsId={chart_of_accounts_id}. "
            f"The combination may need to be created in Oracle GL first."
        )

    # If multiple matches, pick exact match (all segments equal).
    for row in items:
        match = True
        for key, expected in target_segments.items():
            actual = str(row.get(key, "")).strip()
            if actual != expected:
                match = False
                break
        if match:
            target_ccid = int(row["CodeCombinationId"])
            log.info(
                "lookup_ccid_by_segments: found exact match CCID=%s (%s)",
                target_ccid,
                row.get("ConcatenatedSegments", ""),
            )
            return target_ccid

    # Fallback: first item (if segments were not returned in response, trust the filter)
    target_ccid = int(items[0]["CodeCombinationId"])
    log.warning(
        "lookup_ccid_by_segments: could not verify exact segment match; "
        "using first result CCID=%s. items_count=%d",
        target_ccid,
        len(items),
    )
    return target_ccid


# ---------------------------------------------------------------------------
# 4) Top-level convenience: resolve_target_expense_ccid
# ---------------------------------------------------------------------------
def resolve_target_expense_ccid(
    client: _RestClient,
    src_expense_ccid: int,
    target_company: str,
    company_segment_key: str = "Segment1",
) -> int:
    """End-to-end: given a source expense CCID and a target Company value,
    return the destination expense CCID.

    Raises:
        ValidationError  if target_ccid == src_expense_ccid (would create
            identical distribution lines).
    """
    details = get_ccid_details(client, src_expense_ccid)
    src_segments = details["segments"]
    coa_id = details["chart_of_accounts_id"]

    target_segments = build_target_segments(
        src_segments, target_company, company_segment_key
    )

    target_ccid = lookup_ccid_by_segments(client, target_segments, coa_id)

    if target_ccid == src_expense_ccid:
        raise ValidationError(
            f"Target CCID ({target_ccid}) equals source CCID ({src_expense_ccid}); "
            f"would create identical distribution lines. "
            f"Check target_company='{target_company}' vs source "
            f"{company_segment_key}='{src_segments.get(company_segment_key)}'."
        )

    log.info(
        "resolve_target_expense_ccid: src=%s → target=%s (company %s→%s)",
        src_expense_ccid,
        target_ccid,
        src_segments.get(company_segment_key),
        target_company,
    )
    return target_ccid
