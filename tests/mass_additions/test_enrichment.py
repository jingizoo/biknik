"""Unit tests for ``apply_enrichment_rules``.

Pilot scope decisions used to be exercised only indirectly via
``test_dry_run.py``. After loosening the region check (Fusion's
``getMassAddition`` often returns no ``X_REGION``), these direct tests
pin the new behaviour.
"""

from __future__ import annotations

from ofam_mass_additions.models import CmdbAsset, MassAddition
from ofam_mass_additions.rules.enrichment import apply_enrichment_rules


def _ma(book: str = "US CORP BOOK", region: str = "US", **kw) -> MassAddition:
    return MassAddition(
        mass_addition_id=kw.pop("mass_addition_id", "MA-1"),
        book_type_code=book,
        region=region,
        tag_number=kw.pop("tag_number", "TAG-1"),
        cost=kw.pop("cost", 5_000.0),
        received_quantity=kw.pop("received_quantity", 1),
        **kw,
    )


def _cmdb(**overrides) -> CmdbAsset:
    base = dict(
        source_key="tag_number",
        source_value="TAG-1",
        location_id=501,
        expense_ccid=12001,
        employee_id=8801,
    )
    base.update(overrides)
    return CmdbAsset(**base)


def test_book_mismatch_skips() -> None:
    decision = apply_enrichment_rules(
        _ma(book="OTHER_BOOK"), _cmdb(), "US CORP BOOK", "US"
    )
    assert decision.action == "skip"
    assert decision.reason == "outside_pilot_scope"


def test_blank_row_region_passes_when_book_matches() -> None:
    """Fusion frequently omits X_REGION; book match alone should be enough."""
    decision = apply_enrichment_rules(
        _ma(region=""), _cmdb(), "US CORP BOOK", "US"
    )
    assert decision.action == "auto_update"


def test_explicit_region_mismatch_still_skips() -> None:
    decision = apply_enrichment_rules(
        _ma(region="EU"), _cmdb(), "US CORP BOOK", "US"
    )
    assert decision.action == "skip"
    assert decision.reason == "outside_pilot_scope"


def test_blank_pilot_region_disables_region_filter() -> None:
    """pilot_region='' means 'don't filter on region at all'."""
    decision = apply_enrichment_rules(
        _ma(region="EU"), _cmdb(), "US CORP BOOK", ""
    )
    assert decision.action == "auto_update"


def test_missing_cmdb_is_exception() -> None:
    decision = apply_enrichment_rules(_ma(), None, "US CORP BOOK", "US")
    assert decision.action == "exception"
    assert "CMDB" in decision.reason


def test_inactive_ccid_is_exception() -> None:
    decision = apply_enrichment_rules(
        _ma(), _cmdb(ccid_active=False), "US CORP BOOK", "US"
    )
    assert decision.action == "exception"
    assert decision.reason == "Inactive CCID"


def test_auto_update_carries_cmdb_fields_into_payload() -> None:
    decision = apply_enrichment_rules(
        _ma(tag_number="TAG-100"),
        _cmdb(location_id=999, expense_ccid=42, employee_id=7),
        "US CORP BOOK",
        "US",
    )
    assert decision.action == "auto_update"
    assert decision.proposed_update is not None
    params = decision.proposed_update.params
    assert params["P_LOCATION_ID_TBL"] == [999]
    assert params["P_DEPRN_EXPENSE_CCID_TBL"] == [42]
    assert params["P_EMPLOYEE_ID_TBL"] == [7]
    assert params["P_TAG_NUMBER"] == "TAG-100"


def test_auto_update_omits_employee_when_cmdb_has_none() -> None:
    """Servers / unowned assets have no assigned_to in CMDB. Don't send
    P_EMPLOYEE_ID_TBL=[None] — Oracle would reject that."""
    decision = apply_enrichment_rules(
        _ma(),
        _cmdb(employee_id=None),
        "US CORP BOOK",
        "US",
    )
    assert decision.action == "auto_update"
    assert decision.proposed_update is not None
    assert "P_EMPLOYEE_ID_TBL" not in decision.proposed_update.params


def test_auto_update_passes_string_location_codes_through() -> None:
    """CMDB returns location codes ('LC000238'); they flow into the
    payload as-is until Oracle-ID translation is wired up."""
    decision = apply_enrichment_rules(
        _ma(),
        _cmdb(location_id="LC000238", expense_ccid="cc-sysid-abc"),
        "US CORP BOOK",
        "US",
    )
    assert decision.action == "auto_update"
    params = decision.proposed_update.params
    assert params["P_LOCATION_ID_TBL"] == ["LC000238"]
    assert params["P_DEPRN_EXPENSE_CCID_TBL"] == ["cc-sysid-abc"]
