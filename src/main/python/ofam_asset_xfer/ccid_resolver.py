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
    ) -> Dict[str, Any]:
        ...


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
    target_company: Optional[str] = None,
    company_segment_key: str = "Segment1",
    segment_overrides: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Copy source segments and apply each configured swap.

    Two ways to specify swaps (combinable):

    * ``target_company`` + ``company_segment_key`` — single-segment
      shortcut, kept for backwards compatibility with callers that
      only ever swap the Company segment.
    * ``segment_overrides`` — dict of ``{segment_key: new_value}``
      mapping, e.g. ``{"Segment1": "US01", "Segment5": "ZZZ"}``.
      Use this when more than one segment needs to change in the
      same cross/in-book move.

    When both forms reference the same segment key with different
    values, that's almost always a config bug, so raise rather than
    silently picking one.

    Raises ValidationError if any override key is missing from
    ``src_segments`` (no point swapping a segment that doesn't exist
    on the source CCID), or when no overrides are supplied at all.
    """
    overrides: Dict[str, str] = dict(segment_overrides or {})
    if target_company is not None:
        existing = overrides.get(company_segment_key)
        if existing is not None and existing != target_company:
            raise ValidationError(
                f"Segment '{company_segment_key}' set in both "
                f"segment_overrides ({existing!r}) and target_company "
                f"({target_company!r}) — pick one."
            )
        overrides[company_segment_key] = target_company

    if not overrides:
        raise ValidationError(
            "build_target_segments requires target_company and/or "
            "segment_overrides; both were empty."
        )

    missing = [k for k in overrides if k not in src_segments]
    if missing:
        raise ValidationError(
            f"Override segment(s) {missing} not found in source segments. "
            f"Available: {sorted(src_segments.keys())}"
        )

    target = deepcopy(src_segments)
    for key, new_val in overrides.items():
        old_val = target[key]
        target[key] = new_val
        log.info(
            "build_target_segments: %s changed from '%s' to '%s'",
            key,
            old_val,
            new_val,
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
    target_company: Optional[str] = None,
    company_segment_key: str = "Segment1",
    segment_overrides: Optional[Dict[str, str]] = None,
) -> int:
    """End-to-end: given a source expense CCID and a set of segment swaps,
    return the destination expense CCID.

    Pass either the ``target_company``/``company_segment_key`` shortcut,
    a multi-segment ``segment_overrides`` dict, or both — see
    :func:`build_target_segments` for the merge rules.

    Raises:
        ValidationError  if target_ccid == src_expense_ccid (would create
            identical distribution lines).
    """
    details = get_ccid_details(client, src_expense_ccid)
    src_segments = details["segments"]
    coa_id = details["chart_of_accounts_id"]

    target_segments = build_target_segments(
        src_segments,
        target_company=target_company,
        company_segment_key=company_segment_key,
        segment_overrides=segment_overrides,
    )

    target_ccid = lookup_ccid_by_segments(client, target_segments, coa_id)

    if target_ccid == src_expense_ccid:
        applied = [f"{k}={v!r}" for k, v in (segment_overrides or {}).items()]
        if target_company is not None:
            applied.append(f"{company_segment_key}={target_company!r}")
        raise ValidationError(
            f"Target CCID ({target_ccid}) equals source CCID "
            f"({src_expense_ccid}); would create identical distribution "
            f"lines. Overrides applied: {', '.join(applied)}; source "
            f"segments={src_segments}."
        )

    log.info(
        "resolve_target_expense_ccid: src=%s → target=%s (overrides=%s)",
        src_expense_ccid,
        target_ccid,
        {
            **(segment_overrides or {}),
            **({company_segment_key: target_company} if target_company else {}),
        },
    )
    return target_ccid
