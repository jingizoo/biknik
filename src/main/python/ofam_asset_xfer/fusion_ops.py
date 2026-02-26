from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .exceptions import FusionApiError, ValidationError
from .oracle_client import OracleErpIntegrationsClient
from .paramlist import parse_rosetta, ensure_same_len
from .ccid_resolver import resolve_target_expense_ccid


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssetState:
    asset_id: str
    asset_number: str
    book_type_code: str

    # Distribution rosetta tables (strings as returned)
    distribution_ids: List[str]
    units_assigned: List[str]
    assigned_to: List[str]
    expense_ccids: List[str]
    location_ccids: List[str]

    # Common non-distribution fields used for xBook orchestration (best-effort)
    category_id: Optional[str] = None
    date_placed_in_service: Optional[str] = None
    cost: Optional[str] = None
    description: Optional[str] = None
    tag_number: Optional[str] = None

    @staticmethod
    def from_get_asset_information(pl: Dict[str, Any]) -> "AssetState":
        # getAssetInformation returns X_* fields. We rely on those.
        # Required identity
        asset_id = str(pl.get("X_ASSET_ID") or "").strip()
        asset_number = str(pl.get("X_ASSET_NUMBER") or "").strip()
        book_type_code = str(pl.get("X_BOOK_TYPE_CODE") or pl.get("P_BOOK_TYPE_CODE") or "").strip()
        if not asset_id or not asset_number or not book_type_code:
            raise FusionApiError(f"Missing identity fields in getAssetInformation response: {pl}")

        dist_ids = parse_rosetta(pl.get("X_DISTRIBUTION_ID_TBL"))
        units = parse_rosetta(pl.get("X_UNITS_ASSIGNED_TBL"))
        assigned = parse_rosetta(pl.get("X_ASSIGNED_TO_TBL"))
        expense = parse_rosetta(pl.get("X_EXPENSE_CCID_TBL"))
        location = parse_rosetta(pl.get("X_LOCATION_CCID_TBL"))

        # For assigned_to, Oracle notes it can be '-1,-1' or ',' (not all assets assigned).
        # For other tables, counts should match.
        if assigned and (len(assigned) != len(dist_ids)):
            # When assigned_to is present but misaligned, treat it as invalid.
            raise FusionApiError(
                f"Inconsistent assigned_to rosetta length (dist={len(dist_ids)}, assigned={len(assigned)}): {pl}"
            )

        ensure_same_len(dist_ids, units, expense, location)

        # If assigned_to missing, fill with empty string placeholders.
        # Oracle does not require assigned_to; '-1' is not a valid person ID.
        if not assigned:
            assigned = [""] * len(dist_ids)

        return AssetState(
            asset_id=asset_id,
            asset_number=asset_number,
            book_type_code=book_type_code,
            distribution_ids=dist_ids,
            units_assigned=units,
            assigned_to=assigned,
            expense_ccids=expense,
            location_ccids=location,
            category_id=str(pl.get("X_CATEGORY_ID") or "").strip() or None,
            date_placed_in_service=str(pl.get("X_DATE_PLACED_IN_SERVICE") or "").strip() or None,
            cost=str(pl.get("X_COST") or "").strip() or None,
            description=str(pl.get("X_DESCRIPTION") or "").strip() or None,
            tag_number=str(pl.get("X_TAG_NUMBER") or "").strip() or None,
        )


def get_asset_information(
    client: OracleErpIntegrationsClient,
    book_type_code: str,
    asset_number: str,
) -> Tuple[Dict[str, Any], Dict[str, Any], AssetState]:
    # The doc indicates you can query using asset_number, tag, serial, etc.
    # OFAM canonical identifier is (book_type_code, asset_number).
    raw, pl = client.process_transaction("getAssetInformation", {
        "P_BOOK_TYPE_CODE": book_type_code,
        "P_ASSET_NUMBER": asset_number,
    })
    status = str(pl.get("X_RETURN_STATUS") or "").strip()
    if status and status != "S":
        raise FusionApiError(f"getAssetInformation failed (status={status}): {pl}")

    state = AssetState.from_get_asset_information(pl)
    return raw, pl, state


def build_same_book_transfer_params(
    state: AssetState,
    effective_date: Optional[str],
    overrides: Dict[str, Any],
    request_id: str,
) -> Tuple[Dict[str, Any], bool]:
    """Build transferAsset ParameterList using OFAM rule:
    - Copy source distribution state
    - Overlay only the intended deltas (location/expense/assigned_to)
    - Use -1 destination distribution id(s) and +/- units per Oracle examples
    """
    n = len(state.distribution_ids)

    # Determine destination assignment values (apply scalar override to all dists).
    def _expand_override(key: str, source: List[str]) -> List[str]:
        v = overrides.get(key)
        if v is None:
            return list(source)
        if isinstance(v, list):
            if len(v) != n:
                raise ValidationError(f"Override list {key} length {len(v)} does not match distributions {n}")
            return [str(x) for x in v]
        return [str(v)] * n

    dest_location = _expand_override("location_ccid", state.location_ccids)
    dest_expense = _expand_override("expense_ccid", state.expense_ccids)
    dest_assigned = _expand_override("assigned_to", state.assigned_to)

    # NOOP detection: if no destination values changed, do not post transfer.
    if dest_location == state.location_ccids and dest_expense == state.expense_ccids and dest_assigned == state.assigned_to:
        return {}, True

    dist_tbl: List[str] = []
    txn_units_tbl: List[str] = []
    units_tbl: List[str] = []
    assigned_tbl: List[str] = []
    expense_tbl: List[str] = []
    location_tbl: List[str] = []

    for i in range(n):
        src_dist = state.distribution_ids[i]
        units = state.units_assigned[i]
        src_assigned = state.assigned_to[i] if state.assigned_to else ""
        src_exp = state.expense_ccids[i]
        src_loc = state.location_ccids[i]

        dst_assigned = dest_assigned[i] if dest_assigned else ""
        dst_exp = dest_expense[i]
        dst_loc = dest_location[i]

        # Source leg
        dist_tbl.append(src_dist)
        txn_units_tbl.append(f"-{units}")
        units_tbl.append(units)
        assigned_tbl.append(src_assigned)
        expense_tbl.append(src_exp)
        location_tbl.append(src_loc)

        # Destination leg (new distribution id)
        dist_tbl.append("-1")
        txn_units_tbl.append(str(units))
        units_tbl.append(units)
        assigned_tbl.append(dst_assigned)
        expense_tbl.append(dst_exp)
        location_tbl.append(dst_loc)

    params: Dict[str, Any] = {
        "P_ASSET_ID": state.asset_id,
        "P_BOOK_TYPE_CODE": state.book_type_code,
        "P_DISTRIBUTION_ID_TBL": dist_tbl,
        "P_TRANSACTION_UNITS_TBL": txn_units_tbl,
        "P_UNITS_ASSIGNED_TBL": units_tbl,
        "P_ASSIGNED_TO_TBL": assigned_tbl,
        "P_EXPENSE_CCID_TBL": expense_tbl,
        "P_LOCATION_CCID_TBL": location_tbl,
        # Traceability in transaction context:
        "P_TRX_ATTRIBUTE1": request_id,
        "P_TRX_ATTRIBUTE2": "OFAM_SAME_BOOK_XFER",
    }
    if effective_date:
        # Used for prior period transfers as well; always pass in canonical format.
        params["P_TRANSACTION_DATE_ENTERED"] = effective_date

    return params, False


def build_same_book_transfer_params_option_b(
    client: OracleErpIntegrationsClient,
    state: AssetState,
    effective_date: Optional[str],
    target_company: str,
    request_id: str,
    company_segment_key: str = "Segment1",
) -> Tuple[Dict[str, Any], bool]:
    """Build transferAsset payload for Option B: change only EXPENSE_CCID
    by replacing the Company segment, keeping LOCATION_CCID and ASSIGNED_TO as-is.

    For each source distribution line, resolves the destination expense CCID
    by looking up the source CCID's GL segments, swapping the Company segment
    to *target_company*, and finding the matching combination.

    Returns (params, is_noop).
    """
    n = len(state.distribution_ids)

    # Resolve per-distribution target expense CCIDs.  In most cases all dists
    # share the same expense CCID, but handle the general case.
    seen_resolutions: Dict[str, int] = {}  # cache: src_ccid_str -> target_ccid
    dest_expense: List[str] = []

    for i in range(n):
        src_ccid_str = state.expense_ccids[i]
        if src_ccid_str in seen_resolutions:
            dest_expense.append(str(seen_resolutions[src_ccid_str]))
        else:
            target_ccid = resolve_target_expense_ccid(
                client,
                int(src_ccid_str),
                target_company,
                company_segment_key,
            )
            seen_resolutions[src_ccid_str] = target_ccid
            dest_expense.append(str(target_ccid))

    # LOCATION_CCID and ASSIGNED_TO stay unchanged.
    dest_location = list(state.location_ccids)
    dest_assigned = list(state.assigned_to)

    # NOOP check (shouldn't happen since resolve_target_expense_ccid already
    # raises on same-ccid, but be defensive).
    if dest_expense == state.expense_ccids:
        return {}, True

    dist_tbl: List[str] = []
    txn_units_tbl: List[str] = []
    units_tbl: List[str] = []
    assigned_tbl: List[str] = []
    expense_tbl: List[str] = []
    location_tbl: List[str] = []

    for i in range(n):
        src_dist = state.distribution_ids[i]
        units = state.units_assigned[i]
        src_assigned = state.assigned_to[i] if state.assigned_to else ""
        src_exp = state.expense_ccids[i]
        src_loc = state.location_ccids[i]

        dst_assigned = dest_assigned[i] if dest_assigned else ""
        dst_exp = dest_expense[i]
        dst_loc = dest_location[i]

        # Source leg (negative)
        dist_tbl.append(src_dist)
        txn_units_tbl.append(f"-{units}")
        units_tbl.append(units)
        assigned_tbl.append(src_assigned)
        expense_tbl.append(src_exp)
        location_tbl.append(src_loc)

        # Destination leg (positive, new distribution id)
        dist_tbl.append("-1")
        txn_units_tbl.append(str(units))
        units_tbl.append(units)
        assigned_tbl.append(dst_assigned)
        expense_tbl.append(dst_exp)
        location_tbl.append(dst_loc)

    params: Dict[str, Any] = {
        "P_ASSET_ID": state.asset_id,
        "P_BOOK_TYPE_CODE": state.book_type_code,
        "P_DISTRIBUTION_ID_TBL": dist_tbl,
        "P_TRANSACTION_UNITS_TBL": txn_units_tbl,
        "P_UNITS_ASSIGNED_TBL": units_tbl,
        "P_ASSIGNED_TO_TBL": assigned_tbl,
        "P_EXPENSE_CCID_TBL": expense_tbl,
        "P_LOCATION_CCID_TBL": location_tbl,
        "P_COPY_SOURCE_LINES_FLAG": "N",
        "P_TRX_ATTRIBUTE1": request_id,
        "P_TRX_ATTRIBUTE2": "OFAM_SAME_BOOK_XFER_OPTB",
    }
    if effective_date:
        params["P_TRANSACTION_DATE_ENTERED"] = effective_date

    return params, False


def build_book_transfer_params(
    state: AssetState,
    dest_book_type_code: str,
    effective_date: Optional[str],
    overrides: Dict[str, Any],
    request_id: str,
) -> Dict[str, Any]:
    """Build transferAsset ParameterList for cross-book transfer.

    Always uses processTransaction-transferAsset; cross-book behaviour is
    driven by the params (P_DEST_BOOK_TYPE_CODE, P_BOOK_TRANSFER_TYPE_CODE,
    P_SRC_DISTRIBUTION_ID_TBL, etc.).
    """
    n = len(state.distribution_ids)

    def _expand_override(key: str, source: List[str]) -> List[str]:
        v = overrides.get(key)
        if v is None:
            return list(source)
        if isinstance(v, list):
            if len(v) != n:
                raise ValidationError(f"Override list {key} length {len(v)} does not match distributions {n}")
            return [str(x) for x in v]
        return [str(v)] * n

    dest_location = _expand_override("location_ccid", state.location_ccids)
    dest_expense = _expand_override("expense_ccid", state.expense_ccids)
    dest_assigned = _expand_override("assigned_to", state.assigned_to)

    # Read xbook-specific config overrides
    book_transfer_type_code = overrides.get("book_transfer_type_code", "NBV")
    cost_basis_code = overrides.get("cost_basis_code", "COST_RESERVE_SRC")

    # Option A: If no distribution change is intended, use copy-source-lines mode
    # to avoid the "identical distribution lines" validation error.
    no_dist_change = (
        dest_location == state.location_ccids
        and dest_expense == state.expense_ccids
        and dest_assigned == state.assigned_to
    )

    if no_dist_change:
        log.info("build_book_transfer_params: no dist change → Option A (P_COPY_SOURCE_LINES_FLAG=Y)")
        params: Dict[str, Any] = {
            "P_ASSET_ID": state.asset_id,
            "P_BOOK_TYPE_CODE": state.book_type_code,
            "P_DEST_BOOK_TYPE_CODE": dest_book_type_code,
            "P_BOOK_TRANSFER_TYPE_CODE": book_transfer_type_code,
            "P_COST_BASIS_CODE": cost_basis_code,
            "P_USE_DEST_CAT_DEPRN_RULES_FLAG": overrides.get("use_dest_cat_deprn_rules_flag", "Y"),
            "P_USE_XFR_DATE_AS_DPIS_FLAG": overrides.get("use_xfr_date_as_dpis_flag", "N"),
            "P_COPY_DFF_FLAG": overrides.get("copy_dff_flag", "Y"),
            "P_COPY_ASSET_KEY_FLAG": overrides.get("copy_asset_key_flag", "Y"),
            "P_CREATE_NEW_ASSET_FLAG": overrides.get("create_new_asset_flag", "Y"),
            "P_COPY_SOURCE_LINES_FLAG": "Y",
            "P_CONVERSION_RATE_TYPE": overrides.get("conversion_rate_type", ""),
            "P_TRX_ATTRIBUTE1": request_id,
            "P_TRX_ATTRIBUTE2": "OFAM_XBOOK_NATIVE",
        }
        if effective_date:
            params["P_TRANSACTION_DATE_ENTERED"] = effective_date
        return params

    # Option B / explicit override: build dual-leg rosetta tables
    txn_units_tbl: List[str] = []
    expense_tbl: List[str] = []
    location_tbl: List[str] = []
    assigned_tbl: List[str] = []
    src_dist_tbl: List[str] = []

    for i in range(n):
        units = state.units_assigned[i]

        # Source leg
        txn_units_tbl.append(f"-{units}")
        expense_tbl.append(state.expense_ccids[i])
        location_tbl.append(state.location_ccids[i])
        assigned_tbl.append(state.assigned_to[i] if state.assigned_to else "")
        src_dist_tbl.append("null")

        # Destination leg
        txn_units_tbl.append(str(units))
        expense_tbl.append(dest_expense[i])
        location_tbl.append(dest_location[i])
        assigned_tbl.append(dest_assigned[i] if dest_assigned else "")
        src_dist_tbl.append("1")

    params: Dict[str, Any] = {
        "P_ASSET_ID": state.asset_id,
        "P_BOOK_TYPE_CODE": state.book_type_code,
        "P_DEST_BOOK_TYPE_CODE": dest_book_type_code,
        "P_BOOK_TRANSFER_TYPE_CODE": book_transfer_type_code,
        "P_COST_BASIS_CODE": cost_basis_code,
        "P_USE_DEST_CAT_DEPRN_RULES_FLAG": overrides.get("use_dest_cat_deprn_rules_flag", "Y"),
        "P_USE_XFR_DATE_AS_DPIS_FLAG": overrides.get("use_xfr_date_as_dpis_flag", "N"),
        "P_COPY_DFF_FLAG": overrides.get("copy_dff_flag", "Y"),
        "P_COPY_ASSET_KEY_FLAG": overrides.get("copy_asset_key_flag", "Y"),
        "P_CREATE_NEW_ASSET_FLAG": overrides.get("create_new_asset_flag", "Y"),
        "P_COPY_SOURCE_LINES_FLAG": "N",
        "P_CONVERSION_RATE_TYPE": overrides.get("conversion_rate_type", ""),
        "P_TRANSACTION_UNITS_TBL": txn_units_tbl,
        "P_EXPENSE_CCID_TBL": expense_tbl,
        "P_LOCATION_CCID_TBL": location_tbl,
        "P_ASSIGNED_TO_TBL": assigned_tbl,
        "P_SRC_DISTRIBUTION_ID_TBL": src_dist_tbl,
        "P_TRX_ATTRIBUTE1": request_id,
        "P_TRX_ATTRIBUTE2": "OFAM_XBOOK_NATIVE",
    }
    if effective_date:
        params["P_TRANSACTION_DATE_ENTERED"] = effective_date

    return params


def build_add_asset_params(
    state: AssetState,
    target_book_type_code: str,
    target_asset_number: str,
    effective_date: Optional[str],
    overrides: Dict[str, Any],
    request_id: str,
) -> Dict[str, Any]:
    """Build addAsset params for xBook orchestration (fallback).

    This is best-effort based on getAssetInformation outputs.
    """
    if not state.category_id or not state.date_placed_in_service or not state.cost:
        raise ValidationError(
            "xBook orchestration requires X_CATEGORY_ID, X_DATE_PLACED_IN_SERVICE, X_COST "
            "to be present in getAssetInformation output."
        )

    n = len(state.distribution_ids)

    def _expand_override(key: str, source: List[str]) -> List[str]:
        v = overrides.get(key)
        if v is None:
            return list(source)
        if isinstance(v, list):
            if len(v) != n:
                raise ValidationError(f"Override list {key} length {len(v)} does not match distributions {n}")
            return [str(x) for x in v]
        return [str(v)] * n

    dest_location = _expand_override("location_ccid", state.location_ccids)
    dest_expense = _expand_override("expense_ccid", state.expense_ccids)
    dest_assigned = _expand_override("assigned_to", state.assigned_to)

    dist_ids = [str(i + 1) for i in range(n)]  # For new assets, use sequence numbers (not source distribution ids).
    units = list(state.units_assigned)
    txn_units = list(state.units_assigned)

    params: Dict[str, Any] = {
        "P_BOOK_TYPE_CODE": target_book_type_code,
        "PX_ASSET_NUMBER": target_asset_number,
        "P_CATEGORY_ID": state.category_id,
        # Preserve original in-service date unless overridden
        "P_DATE_PLACED_IN_SERVICE": state.date_placed_in_service,
        "P_COST": state.cost,
        "P_ASSET_TYPE": "CAPITALIZED",
        "P_DISTRIBUTION_ID_TBL": dist_ids,
        "P_UNITS_ASSIGNED_TBL": units,
        "P_TRANSACTION_UNITS_TBL": txn_units,
        "P_ASSIGNED_TO_TBL": dest_assigned if dest_assigned else [""] * n,
        "P_EXPENSE_CCID_TBL": dest_expense,
        "P_LOCATION_CCID_TBL": dest_location,
        "P_TRX_ATTRIBUTE1": request_id,
        "P_TRX_ATTRIBUTE2": "OFAM_XBOOK_ADD",
    }
    if state.description:
        params["P_DESCRIPTION"] = state.description
    if effective_date:
        # Not always required for addAsset, but keep as a transaction attribute date if needed.
        # If your tenant expects a specific date parameter for addAsset, supply via native templates.
        pass
    return params


def build_retire_asset_params(
    state: AssetState,
    effective_date: Optional[str],
    request_id: str,
) -> Dict[str, Any]:
    if not state.cost:
        raise ValidationError("retireAsset requires X_COST (for full retirement).")

    params: Dict[str, Any] = {
        "P_ASSET_ID": state.asset_id,
        "P_BOOK_TYPE_CODE": state.book_type_code,
        "P_COST_RETIRED": state.cost,  # full retirement by default
        "P_TRX_ATTRIBUTE1": request_id,
        "P_TRX_ATTRIBUTE2": "OFAM_XBOOK_RETIRE",
    }
    if effective_date:
        # If your tenant uses a specific retirement date parameter, add it here.
        # The REST transactions doc standardizes on YYYY-MM-DD for date formats.
        pass
    return params
