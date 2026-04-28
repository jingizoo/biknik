from __future__ import annotations

from ofam_mass_additions.models import CmdbAsset, EnrichmentDecision, MassAddition, ProposedOracleUpdate


def apply_enrichment_rules(
    mass_addition: MassAddition,
    cmdb_asset: CmdbAsset | None,
    pilot_book: str,
    pilot_region: str,
) -> EnrichmentDecision:
    if mass_addition.book_type_code != pilot_book or mass_addition.region != pilot_region:
        return EnrichmentDecision(action="skip", reason="outside_pilot_scope")

    if cmdb_asset is None:
        return EnrichmentDecision(action="exception", reason="CMDB not found")

    if not cmdb_asset.ccid_active:
        return EnrichmentDecision(action="exception", reason="Inactive CCID")

    if cmdb_asset.location_id is None:
        return EnrichmentDecision(action="exception", reason="Missing location")

    if cmdb_asset.expense_ccid is None:
        return EnrichmentDecision(action="exception", reason="Missing CCID")

    payload = ProposedOracleUpdate(
        mass_addition_id=mass_addition.mass_addition_id,
        params={
            "P_MASS_ADDITION_ID": mass_addition.mass_addition_id,
            "P_BOOK_TYPE_CODE": mass_addition.book_type_code,
            "P_QUEUE_NAME": "POST",
            "P_POSTING_STATUS": "POST",
            "P_LOCATION_ID_TBL": [cmdb_asset.location_id],
            "P_DEPRN_EXPENSE_CCID_TBL": [cmdb_asset.expense_ccid],
            "P_EMPLOYEE_ID_TBL": [cmdb_asset.employee_id],
        },
    )

    return EnrichmentDecision(action="auto_update", reason="ready_for_update", proposed_update=payload)
