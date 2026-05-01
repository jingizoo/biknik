from __future__ import annotations

from ofam_mass_additions.models import (
    CmdbAsset,
    EnrichmentDecision,
    MassAddition,
    ProposedOracleUpdate,
)


# Default per-unit price threshold for Capitalized vs Expensed.  Per the
# Citadel runbook (Asset Type Correction Report): below this amount the
# row should be Expensed, otherwise Capitalized.  AFBDI populates the
# field incorrectly for some rows, so we always recompute.
DEFAULT_CAPITALIZE_THRESHOLD: float = 1000.0


def apply_enrichment_rules(
    mass_addition: MassAddition,
    cmdb_asset: CmdbAsset | None,
    pilot_book: str,
    pilot_region: str,
    capitalize_threshold: float = DEFAULT_CAPITALIZE_THRESHOLD,
) -> EnrichmentDecision:
    """Decide what to do with a single mass-addition row.

    Output one of:
      * ``skip``         — outside pilot scope.
      * ``exception``    — needed data missing or invalid.
      * ``auto_update``  — produce an ``updateMassAddition`` payload.

    The payload includes asset-type (Capitalized vs Expensed), location,
    expense-CCID, employee, tag and serial number, queue=POST, and posting
    status=POST so a single ``updateMassAddition`` call moves the row to
    posted state.
    """
    if mass_addition.book_type_code != pilot_book:
        return EnrichmentDecision(action="skip", reason="outside_pilot_scope")
    # Fusion's getMassAddition response often omits X_REGION (the parser
    # then sets ``region=""``). Only enforce a region match when both the
    # pilot scope and the row declare one — book_type_code already encodes
    # the country segment for Citadel's books, so a blank row-region
    # shouldn't drop an otherwise valid pilot row.
    if pilot_region and mass_addition.region and mass_addition.region != pilot_region:
        return EnrichmentDecision(action="skip", reason="outside_pilot_scope")

    if cmdb_asset is None:
        return EnrichmentDecision(action="exception", reason="CMDB not found")

    if not cmdb_asset.ccid_active:
        return EnrichmentDecision(action="exception", reason="Inactive CCID")

    if cmdb_asset.location_id is None:
        return EnrichmentDecision(action="exception", reason="Missing location")

    if cmdb_asset.expense_ccid is None:
        return EnrichmentDecision(action="exception", reason="Missing CCID")

    asset_type = _determine_asset_type(mass_addition, capitalize_threshold)

    params: dict[str, object] = {
        "P_MASS_ADDITION_ID": mass_addition.mass_addition_id,
        "P_BOOK_TYPE_CODE": mass_addition.book_type_code,
        "P_QUEUE_NAME": "POST",
        "P_POSTING_STATUS": "POST",
        "P_ASSET_TYPE": asset_type,
        "P_LOCATION_ID_TBL": [cmdb_asset.location_id],
        "P_DEPRN_EXPENSE_CCID_TBL": [cmdb_asset.expense_ccid],
    }
    # Many CMDB rows (servers, infrastructure) have no assigned_to.
    # Don't send P_EMPLOYEE_ID_TBL=[None] — Oracle would reject that.
    if cmdb_asset.employee_id is not None:
        params["P_EMPLOYEE_ID_TBL"] = [cmdb_asset.employee_id]
    if mass_addition.tag_number:
        params["P_TAG_NUMBER"] = mass_addition.tag_number
    if mass_addition.serial_number:
        params["P_SERIAL_NUMBER"] = mass_addition.serial_number

    payload = ProposedOracleUpdate(
        mass_addition_id=mass_addition.mass_addition_id,
        params=params,
    )

    return EnrichmentDecision(action="auto_update", reason="ready_for_update", proposed_update=payload)


def _determine_asset_type(ma: MassAddition, threshold: float) -> str:
    """Classify a mass-addition as CAPITALIZED or EXPENSED by per-unit price.

    Falls back to CAPITALIZED when cost or quantity are unknown — safer to
    capitalize and let an accountant re-classify than to silently expense
    a high-value row.
    """
    qty = ma.received_quantity or ma.invoice_quantity or 0
    if not ma.cost or qty <= 0:
        return "CAPITALIZED"
    unit_price = ma.cost / qty
    return "EXPENSED" if unit_price < threshold else "CAPITALIZED"
