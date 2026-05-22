"""Unit tests for ofam_asset_xfer.fusion_sync (BIP + getAssetInformation discovery).

All tests use mock clients — no network calls.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from ofam_asset_xfer.entity_resolver import EntityBookResolver
from ofam_asset_xfer.exceptions import FusionApiError
from ofam_asset_xfer.fusion_ops import AssetState
from ofam_asset_xfer.fusion_sync import (
    DFFConfig,
    FusionIUSync,
    PendingTransfer,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

ENTITY_MAP = {
    "US Entity": "US CORP BOOK",
    "UK Entity": "UK CORP BOOK",
    "JP Entity": "JP CORP BOOK",
}


def _resolver():
    return EntityBookResolver(ENTITY_MAP)


def _mock_fusion():
    return MagicMock()


def _mock_bip():
    return MagicMock()


def _bip_row(
    asset_id="100",
    asset_number="142847",
    book="US CORP BOOK",
    dff_entity="UK Entity",
    dff_date="2026-03-01",
    dff_location="",
    final_target_book="",
    target_location_id="",
    current_location_id="",
    target_expense_ccid="",
    target_ledger_name="",
):
    """Build a BIP report row dict (as returned by BIPClient.run_report).

    Column names match the All_IUT_Transfers_Rpt report.  P_BOOK_TYPE_CODE
    comes from a root-level element injected into each row by _parse_data_ds.
    ASSET_ID is returned directly from the report G_1 rows.
    """
    return {
        "ASSET_ID": asset_id,
        "ASSET_NUMBER": asset_number,
        "P_BOOK_TYPE_CODE": book,
        "TRANSFER_TO_ENTITY": dff_entity,
        "TRANSFER_DATE": dff_date,
        "TRANSFER_TO_LOCATION": dff_location,
        "FINAL_TARGET_BOOK_TYPE_CODE": final_target_book,
        "TARGET_LOCATION_ID": target_location_id,
        "CURRENT_LOCATION_ID": current_location_id,
        "TARGET_EXPENSE_CCID": target_expense_ccid,
        "TARGET_LEDGER_NAME": target_ledger_name,
    }


def _get_asset_info_response(
    asset_id="100",
    asset_number="142847",
    book="US CORP BOOK",
    cost="36129.54",
    include_identity=True,
):
    """Build a getAssetInformation processTransaction response.

    When *include_identity* is False, omit X_ASSET_ID and X_BOOK_TYPE_CODE
    to simulate Fusion versions that do not echo these fields back.
    """
    pl = {
        "X_RETURN_STATUS": "S",
        "X_ASSET_NUMBER": asset_number,
        "X_DISTRIBUTION_ID_TBL": "1001",
        "X_UNITS_ASSIGNED_TBL": "1",
        "X_ASSIGNED_TO_TBL": "",
        "X_EXPENSE_CCID_TBL": "626955",
        "X_LOCATION_CCID_TBL": "789012",
        "X_CATEGORY_ID": "501",
        "X_DATE_PLACED_IN_SERVICE": "2023-01-15",
        "X_COST": cost,
        "X_DESCRIPTION": "ARISTA 7130",
        "X_TAG_NUMBER": asset_number,
    }
    if include_identity:
        pl["X_ASSET_ID"] = asset_id
        pl["X_BOOK_TYPE_CODE"] = book
    return ({}, pl)


def _sample_state(asset_id="100", asset_number="142847", book="US CORP BOOK"):
    return AssetState(
        asset_id=asset_id,
        asset_number=asset_number,
        book_type_code=book,
        distribution_ids=["1001"],
        units_assigned=["1"],
        assigned_to=[""],
        expense_ccids=["626955"],
        location_ccids=["789012"],
        category_id="501",
        date_placed_in_service="2023-01-15",
        cost="36129.54",
        description="ARISTA 7130",
        tag_number="142847",
    )


def _make_pending(state=None, book="US CORP BOOK", target="UK CORP BOOK"):
    return PendingTransfer(
        asset_id="100",
        asset_number="142847",
        book_type_code=book,
        description="ARISTA 7130",
        tag_number="142847",
        cost="36129.54",
        transfer_date="2026-03-01",
        transfer_to_entity="UK Entity",
        transfer_to_location=None,
        target_book_type_code=target,
        fa_state=state,
    )


# ===================================================================
# FusionIUSync.find_pending_transfers
# ===================================================================


class TestFindPendingTransfers:
    def test_detects_pending_transfer(self):
        """Asset in US book with DFF entity=UK → pending cross-book."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        # BIP is called per book; only return rows for the US book call
        bip.run_report.side_effect = lambda params: (
            [_bip_row(book="US CORP BOOK", dff_entity="UK Entity")]
            if params.get("P_BOOK_TYPE_CODE") == "US CORP BOOK"
            else []
        )
        fusion.process_transaction.return_value = _get_asset_info_response()

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(
            books=["US CORP BOOK", "UK CORP BOOK"],
            limit=10,
        )

        assert len(pending) == 1
        pt = pending[0]
        assert pt.asset_number == "142847"
        assert pt.book_type_code == "US CORP BOOK"
        assert pt.target_book_type_code == "UK CORP BOOK"
        assert pt.transfer_to_entity == "UK Entity"
        assert pt.transfer_date == "2026-03-01"
        # Should have called getAssetInformation
        fusion.process_transaction.assert_called_once()

    def test_caches_asset_state_from_discovery(self):
        """fa_state should be populated during discovery (avoid double fetch)."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = [_bip_row()]
        fusion.process_transaction.return_value = _get_asset_info_response()

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)

        assert len(pending) == 1
        assert pending[0].fa_state is not None
        assert pending[0].fa_state.asset_id == "100"

    def test_skips_when_entity_matches_current_book(self):
        """Asset already in US book and entity resolves to US → skip."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = [
            _bip_row(book="US CORP BOOK", dff_entity="US Entity")
        ]

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)

        assert len(pending) == 0
        # Should NOT have called getAssetInformation (filtered before that)
        fusion.process_transaction.assert_not_called()

    def test_skips_when_entity_empty(self):
        """Asset with empty Transfer to Entity DFF → skip."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = [_bip_row(dff_entity="")]

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)

        assert len(pending) == 0

    def test_skips_when_entity_unknown(self):
        """Asset with entity not in map → skip (no crash)."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = [_bip_row(dff_entity="UNKNOWN ENTITY")]

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)

        assert len(pending) == 0

    def test_skips_when_book_not_in_search_list(self):
        """Asset in JP book but JP not in search books → skip."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = [
            _bip_row(book="JP CORP BOOK", dff_entity="US Entity")
        ]

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(
            books=["US CORP BOOK", "UK CORP BOOK"],
            limit=10,
        )

        assert len(pending) == 0

    def test_respects_limit(self):
        """Should stop collecting after reaching limit."""
        rows = [
            _bip_row(asset_number=str(1000 + i), dff_entity="UK Entity")
            for i in range(20)
        ]
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = rows
        # Each getAssetInformation call returns matching asset
        fusion.process_transaction.side_effect = [
            _get_asset_info_response(asset_id=str(i), asset_number=str(1000 + i))
            for i in range(20)
        ]

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=5)

        assert len(pending) == 5

    def test_custom_dff_column_names(self):
        """Should use custom DFF column names from DFFConfig."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = [
            {
                "FA_ASSET_ID": "100",
                "ASSET_NUM": "142847",
                "BOOK": "US CORP BOOK",
                "XFER_ENTITY": "UK Entity",
                "XFER_DATE": "2026-06-01",
                "XFER_LOC": "",
            }
        ]
        fusion.process_transaction.return_value = _get_asset_info_response()

        dff = DFFConfig(
            asset_id_col="FA_ASSET_ID",
            asset_number_col="ASSET_NUM",
            book_type_code_col="BOOK",
            transfer_date_col="XFER_DATE",
            transfer_to_entity_col="XFER_ENTITY",
            transfer_to_location_col="XFER_LOC",
        )
        sync = FusionIUSync(fusion, _resolver(), bip, dff_config=dff)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)

        assert len(pending) == 1
        assert pending[0].transfer_date == "2026-06-01"

    def test_handles_bip_failure(self):
        """BIP report failure should not crash, just return empty."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.side_effect = FusionApiError("Connection refused")

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)

        assert len(pending) == 0

    def test_handles_get_asset_info_failure(self):
        """getAssetInformation failure for one asset should skip it, not crash."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = [
            _bip_row(asset_number="AAA", dff_entity="UK Entity"),
            _bip_row(asset_number="BBB", dff_entity="UK Entity"),
        ]
        # First call fails, second succeeds
        fusion.process_transaction.side_effect = [
            FusionApiError("Timeout"),
            _get_asset_info_response(asset_id="200", asset_number="BBB"),
        ]

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)

        assert len(pending) == 1
        assert pending[0].asset_number == "BBB"

    def test_transfer_to_location_captured(self):
        """Transfer to Location DFF should be captured if present."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = [
            _bip_row(dff_entity="UK Entity", dff_location="LON-DC1")
        ]
        fusion.process_transaction.return_value = _get_asset_info_response()

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)

        assert pending[0].transfer_to_location == "LON-DC1"

    def test_passes_bip_params(self):
        """bip_params should be forwarded to BIPClient.run_report with P_BOOK_TYPE_CODE."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = []

        sync = FusionIUSync(fusion, _resolver(), bip)
        sync.find_pending_transfers(
            books=["US CORP BOOK"],
            limit=10,
            bip_params={"P_EXTRA": "value"},
        )

        bip.run_report.assert_called_once_with(
            params={"P_EXTRA": "value", "P_BOOK_TYPE_CODE": "US CORP BOOK"},
        )

    def test_uses_final_target_book_from_report(self):
        """When FINAL_TARGET_BOOK_TYPE_CODE is present, use it directly
        instead of resolving via entity."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.side_effect = lambda params: (
            [
                _bip_row(
                    book="US CORP BOOK",
                    dff_entity="UK Entity",
                    final_target_book="JP CORP BOOK",
                )
            ]
            if params.get("P_BOOK_TYPE_CODE") == "US CORP BOOK"
            else []
        )
        fusion.process_transaction.return_value = _get_asset_info_response()

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(
            books=["US CORP BOOK", "JP CORP BOOK"],
            limit=10,
        )

        assert len(pending) == 1
        # Should use the report's FINAL_TARGET_BOOK_TYPE_CODE, not entity resolution
        assert pending[0].target_book_type_code == "JP CORP BOOK"

    def test_final_target_book_skips_when_matches_current(self):
        """FINAL_TARGET_BOOK_TYPE_CODE == current book → skip (not pending)."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = [
            _bip_row(
                book="US CORP BOOK",
                dff_entity="UK Entity",
                final_target_book="US CORP BOOK",
            )
        ]

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)

        assert len(pending) == 0
        fusion.process_transaction.assert_not_called()

    def test_falls_back_to_entity_resolver_when_no_final_target(self):
        """When FINAL_TARGET_BOOK_TYPE_CODE is empty, fall back to entity resolver."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = [
            _bip_row(
                book="US CORP BOOK",
                dff_entity="UK Entity",
                final_target_book="",
            )
        ]
        fusion.process_transaction.return_value = _get_asset_info_response()

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)

        assert len(pending) == 1
        assert pending[0].target_book_type_code == "UK CORP BOOK"

    def test_target_location_id_captured(self):
        """TARGET_LOCATION_ID from report should be captured."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = [
            _bip_row(
                dff_entity="UK Entity",
                final_target_book="UK CORP BOOK",
                target_location_id="300100123456",
            )
        ]
        fusion.process_transaction.return_value = _get_asset_info_response()

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)

        assert len(pending) == 1
        assert pending[0].target_location_id == "300100123456"

    def test_cross_book_proceeds_even_when_location_matches(self):
        """Cross-book rows must not be skipped on location-match.

        Cross-book is an entity move; location may legitimately stay
        the same.  The previous skip rule treated location-match alone
        as a no-op even for cross-book rows, blocking entity-only
        transfers.
        """
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = [
            _bip_row(
                dff_entity="UK Entity",
                final_target_book="UK CORP BOOK",
                current_location_id="300100123456",
                target_location_id="300100123456",
            )
        ]
        fusion.process_transaction.return_value = _get_asset_info_response()

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)

        assert len(pending) == 1
        assert pending[0].is_cross_book is True

    def test_same_book_skips_when_location_matches_and_no_ccid_change(self):
        """Same-book + same location + no CCID change → genuinely a no-op."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = [
            _bip_row(
                book="US CORP BOOK",
                dff_entity="US Entity",
                final_target_book="US CORP BOOK",
                current_location_id="300100123456",
                target_location_id="300100123456",
            )
        ]

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)

        assert len(pending) == 0
        fusion.process_transaction.assert_not_called()

    def test_same_book_proceeds_when_location_matches_but_ccid_changes(self):
        """CCID-only same-book transfers must not be skipped on location-match.

        TARGET_EXPENSE_CCID is the actual delta when location stays
        the same; the row must reach the executor.
        """
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = [
            _bip_row(
                book="US CORP BOOK",
                dff_entity="US Entity",
                final_target_book="US CORP BOOK",
                current_location_id="300100123456",
                target_location_id="300100123456",
                target_expense_ccid="12001",
            )
        ]
        fusion.process_transaction.return_value = _get_asset_info_response()

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)

        assert len(pending) == 1
        assert pending[0].is_cross_book is False
        assert pending[0].target_expense_ccid == "12001"

    def test_proceeds_when_current_location_differs_from_target(self):
        """CURRENT_LOCATION_ID != TARGET_LOCATION_ID → proceed with transfer."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = [
            _bip_row(
                dff_entity="UK Entity",
                final_target_book="UK CORP BOOK",
                current_location_id="300100111111",
                target_location_id="300100222222",
            )
        ]
        fusion.process_transaction.return_value = _get_asset_info_response()

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)

        assert len(pending) == 1
        assert pending[0].target_location_id == "300100222222"

    def test_uses_bip_asset_id_when_oracle_omits_it(self):
        """When X_ASSET_ID is absent from getAssetInformation, the ASSET_ID
        from the BIP report row is used as fallback."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = [
            _bip_row(asset_id="115", book="US CORP BOOK", dff_entity="UK Entity")
        ]
        # getAssetInformation returns without X_ASSET_ID and X_BOOK_TYPE_CODE
        fusion.process_transaction.return_value = _get_asset_info_response(
            include_identity=False,
        )

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)

        assert len(pending) == 1
        assert pending[0].fa_state.asset_id == "115"
        assert pending[0].fa_state.book_type_code == "US CORP BOOK"
        # No REST resource call needed — asset_id came from BIP report
        fusion.get_resource.assert_not_called()


# ===================================================================
# FusionIUSync.execute_transfer
# ===================================================================


class TestExecuteTransfer:
    def test_dry_run(self):
        fusion = _mock_fusion()
        bip = _mock_bip()
        # getAssetInformation call
        fusion.process_transaction.return_value = (
            {},
            {
                "X_RETURN_STATUS": "S",
                "X_ASSET_ID": "100",
                "X_ASSET_NUMBER": "142847",
                "X_BOOK_TYPE_CODE": "US CORP BOOK",
                "X_DISTRIBUTION_ID_TBL": "1001",
                "X_UNITS_ASSIGNED_TBL": "1",
                "X_ASSIGNED_TO_TBL": "",
                "X_EXPENSE_CCID_TBL": "626955",
                "X_LOCATION_CCID_TBL": "789012",
                "X_CATEGORY_ID": "501",
                "X_DATE_PLACED_IN_SERVICE": "2023-01-15",
                "X_COST": "36129.54",
            },
        )

        sync = FusionIUSync(fusion, _resolver(), bip)
        pt = _make_pending()
        result = sync.execute_transfer(pt, dry_run=True)

        assert result.status == "DRY_RUN"
        assert result.source_book == "US CORP BOOK"
        assert result.target_book == "UK CORP BOOK"
        assert result.transfer_to_entity == "UK Entity"
        assert result.error is None

    def test_execute_success(self):
        fusion = _mock_fusion()
        bip = _mock_bip()
        state = _sample_state()
        pt = _make_pending(state=state)

        # transferAsset response
        fusion.process_transaction.return_value = (
            {},
            {"X_RETURN_STATUS": "S", "X_EVENT_ID": "99999"},
        )

        sync = FusionIUSync(fusion, _resolver(), bip)
        result = sync.execute_transfer(pt, dry_run=False)

        assert result.status == "TRANSFERRED"
        assert result.error is None
        fusion.process_transaction.assert_called_once()
        call_args = fusion.process_transaction.call_args
        assert call_args[0][0] == "bookTransfer"

    def test_execute_failure(self):
        fusion = _mock_fusion()
        bip = _mock_bip()
        state = _sample_state()
        pt = _make_pending(state=state)

        fusion.process_transaction.return_value = (
            {},
            {"X_RETURN_STATUS": "F", "X_MSG_DATA": "Period closed"},
        )

        sync = FusionIUSync(fusion, _resolver(), bip)
        result = sync.execute_transfer(pt, dry_run=False)

        assert result.status == "FAILED"
        assert "X_RETURN_STATUS=F" in result.error

    def test_fetches_state_when_missing(self):
        """When fa_state is None, should call getAssetInformation first."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        pt = _make_pending(state=None)

        fusion.process_transaction.return_value = (
            {},
            {
                "X_RETURN_STATUS": "S",
                "X_ASSET_ID": "100",
                "X_ASSET_NUMBER": "142847",
                "X_BOOK_TYPE_CODE": "US CORP BOOK",
                "X_DISTRIBUTION_ID_TBL": "1001",
                "X_UNITS_ASSIGNED_TBL": "1",
                "X_ASSIGNED_TO_TBL": "",
                "X_EXPENSE_CCID_TBL": "626955",
                "X_LOCATION_CCID_TBL": "789012",
            },
        )

        sync = FusionIUSync(fusion, _resolver(), bip)
        result = sync.execute_transfer(pt, dry_run=True)

        assert result.status == "DRY_RUN"
        # Should have called process_transaction for getAssetInformation
        assert fusion.process_transaction.call_count == 1

    def test_state_fetch_failure(self):
        fusion = _mock_fusion()
        bip = _mock_bip()
        fusion.process_transaction.side_effect = FusionApiError("Timeout")
        pt = _make_pending(state=None)

        sync = FusionIUSync(fusion, _resolver(), bip)
        result = sync.execute_transfer(pt, dry_run=False)

        assert result.status == "FAILED"
        assert "Timeout" in result.error

    def test_uses_transfer_date_from_dff(self):
        """Effective date should come from the DFF Transfer Date."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        state = _sample_state()
        pt = PendingTransfer(
            asset_id="100",
            asset_number="142847",
            book_type_code="US CORP BOOK",
            description="Test",
            tag_number="142847",
            cost="36129.54",
            transfer_date="2026-06-15",
            transfer_to_entity="UK Entity",
            transfer_to_location=None,
            target_book_type_code="UK CORP BOOK",
            fa_state=state,
        )

        fusion.process_transaction.return_value = (
            {},
            {"X_RETURN_STATUS": "S"},
        )

        sync = FusionIUSync(fusion, _resolver(), bip)
        result = sync.execute_transfer(pt, dry_run=False)

        assert result.transfer_date == "2026-06-15"
        # Check the params passed to transferAsset include this date
        call_params = fusion.process_transaction.call_args[0][1]
        assert call_params["P_TRANSACTION_DATE_ENTERED"] == "2026-06-15"

    def test_location_override_from_dff(self):
        """If Transfer to Location DFF is set, it should be used as location override."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        state = _sample_state()
        pt = PendingTransfer(
            asset_id="100",
            asset_number="142847",
            book_type_code="US CORP BOOK",
            description="Test",
            tag_number="142847",
            cost="36129.54",
            transfer_date="2026-03-01",
            transfer_to_entity="UK Entity",
            transfer_to_location="555555",
            target_book_type_code="UK CORP BOOK",
            fa_state=state,
        )

        fusion.process_transaction.return_value = (
            {},
            {"X_RETURN_STATUS": "S"},
        )

        sync = FusionIUSync(fusion, _resolver(), bip)
        result = sync.execute_transfer(pt, dry_run=False)

        assert result.status == "TRANSFERRED"
        call_params = fusion.process_transaction.call_args[0][1]
        # Location CCID tables should include the override
        assert "555555" in call_params["P_LOCATION_CCID_TBL"]

    def test_target_location_id_overrides_dff_location(self):
        """TARGET_LOCATION_ID should take priority over TRANSFER_TO_LOCATION."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        state = _sample_state()
        pt = PendingTransfer(
            asset_id="100",
            asset_number="142847",
            book_type_code="US CORP BOOK",
            description="Test",
            tag_number="142847",
            cost="36129.54",
            transfer_date="2026-03-01",
            transfer_to_entity="UK Entity",
            transfer_to_location="555555",
            target_book_type_code="UK CORP BOOK",
            target_location_id="300100999999",
            fa_state=state,
        )

        fusion.process_transaction.return_value = (
            {},
            {"X_RETURN_STATUS": "S"},
        )

        sync = FusionIUSync(fusion, _resolver(), bip)
        result = sync.execute_transfer(pt, dry_run=False)

        assert result.status == "TRANSFERRED"
        call_params = fusion.process_transaction.call_args[0][1]
        # TARGET_LOCATION_ID should win over TRANSFER_TO_LOCATION
        assert "300100999999" in call_params["P_LOCATION_CCID_TBL"]
        assert "555555" not in call_params["P_LOCATION_CCID_TBL"]

    def test_same_book_transfer_uses_transfer_asset(self):
        """Same-book transfer should call processTransaction-transferAsset."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        state = _sample_state()
        pt = PendingTransfer(
            asset_id="100",
            asset_number="142847",
            book_type_code="US CORP BOOK",
            description="Test",
            tag_number="142847",
            cost="36129.54",
            transfer_date="2026-03-01",
            transfer_to_entity="US Entity",
            transfer_to_location=None,
            target_book_type_code="US CORP BOOK",
            target_location_id="300100999999",
            is_cross_book=False,
            fa_state=state,
        )

        fusion.process_transaction.return_value = (
            {},
            {"X_RETURN_STATUS": "S"},
        )

        sync = FusionIUSync(fusion, _resolver(), bip)
        result = sync.execute_transfer(pt, dry_run=False)

        assert result.status == "TRANSFERRED"
        call_args = fusion.process_transaction.call_args
        assert call_args[0][0] == "transferAsset"

    def test_same_book_noop_when_no_distribution_changes(self):
        """Same-book transfer with no actual changes should return NOOP."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        state = _sample_state()
        # target_location_id matches the state's existing location
        pt = PendingTransfer(
            asset_id="100",
            asset_number="142847",
            book_type_code="US CORP BOOK",
            description="Test",
            tag_number="142847",
            cost="36129.54",
            transfer_date="2026-03-01",
            transfer_to_entity="US Entity",
            transfer_to_location=None,
            target_book_type_code="US CORP BOOK",
            target_location_id="789012",  # same as state.location_ccids[0]
            is_cross_book=False,
            fa_state=state,
        )

        sync = FusionIUSync(fusion, _resolver(), bip)
        result = sync.execute_transfer(pt, dry_run=False)

        assert result.status == "NOOP"
        fusion.process_transaction.assert_not_called()

    def test_discovery_detects_same_book_location_transfer(self):
        """Same book + different target location → pending same-book transfer."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = [
            _bip_row(
                book="US CORP BOOK",
                dff_entity="US Entity",
                final_target_book="US CORP BOOK",
                current_location_id="300100111111",
                target_location_id="300100222222",
            )
        ]
        fusion.process_transaction.return_value = _get_asset_info_response()

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)

        assert len(pending) == 1
        assert pending[0].is_cross_book is False
        assert pending[0].target_location_id == "300100222222"

    def test_target_expense_ccid_captured(self):
        """TARGET_EXPENSE_CCID from report should be captured on PendingTransfer."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = [
            _bip_row(
                dff_entity="UK Entity",
                final_target_book="UK CORP BOOK",
                target_expense_ccid="627564",
            )
        ]
        fusion.process_transaction.return_value = _get_asset_info_response()

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)

        assert len(pending) == 1
        assert pending[0].target_expense_ccid == "627564"

    def test_target_expense_ccid_used_in_cross_book_transfer(self):
        """TARGET_EXPENSE_CCID should be passed as expense_ccid override."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        state = _sample_state()
        pt = PendingTransfer(
            asset_id="100",
            asset_number="142847",
            book_type_code="US CORP BOOK",
            description="Test",
            tag_number="142847",
            cost="36129.54",
            transfer_date="2026-03-01",
            transfer_to_entity="UK Entity",
            transfer_to_location=None,
            target_book_type_code="UK CORP BOOK",
            target_expense_ccid="627564",
            fa_state=state,
        )

        fusion.process_transaction.return_value = (
            {},
            {"X_RETURN_STATUS": "S"},
        )

        sync = FusionIUSync(fusion, _resolver(), bip)
        result = sync.execute_transfer(pt, dry_run=False)

        assert result.status == "TRANSFERRED"
        call_params = fusion.process_transaction.call_args[0][1]
        assert "627564" in call_params["P_EXPENSE_CCID_TBL"]

    def test_same_book_with_expense_ccid_only(self):
        """Same book + target_expense_ccid but no target_location → should still trigger transfer."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = [
            _bip_row(
                book="US CORP BOOK",
                dff_entity="US Entity",
                final_target_book="US CORP BOOK",
                target_expense_ccid="627564",
            )
        ]
        fusion.process_transaction.return_value = _get_asset_info_response()

        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)

        assert len(pending) == 1
        assert pending[0].is_cross_book is False
        assert pending[0].target_expense_ccid == "627564"


# ===================================================================
# FusionIUSync.run_full_sync
# ===================================================================


class TestRunFullSync:
    def test_dry_run_summary(self):
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.side_effect = lambda params: (
            [_bip_row(dff_entity="UK Entity")]
            if params.get("P_BOOK_TYPE_CODE") == "US CORP BOOK"
            else []
        )
        # getAssetInformation for discovery, then getAssetInformation already cached
        fusion.process_transaction.return_value = _get_asset_info_response()

        sync = FusionIUSync(fusion, _resolver(), bip)
        summary = sync.run_full_sync(
            books=["US CORP BOOK", "UK CORP BOOK"],
            dry_run=True,
        )

        assert summary["dry_run"] is True
        assert summary["counts"]["total"] == 1
        assert summary["counts"]["dry_run"] == 1
        assert len(summary["results"]) == 1
        assert summary["results"][0]["status"] == "DRY_RUN"
        assert summary["results"][0]["fusion_response"] is not None

    def test_empty_report_returns_empty(self):
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = []

        sync = FusionIUSync(fusion, _resolver(), bip)
        summary = sync.run_full_sync(books=["US CORP BOOK"], dry_run=True)

        assert summary["counts"]["total"] == 0
        assert summary["results"] == []


# ---------------------------------------------------------------------------
# transfer_overrides — run-wide defaults for the Fusion FA payload
# (conversion_rate_type, copy_dff_flag, …).  Per-row BIP values still win.
# ---------------------------------------------------------------------------


class TestTransferOverridesDefaults:
    def test_no_default_conversion_rate_type(self):
        """build_book_transfer_params no longer hard-codes a rate type.

        Some Citadel ledgers don't publish 'Corporate', and same-currency
        transfers need the parameter omitted entirely.  When no override
        is supplied, P_CONVERSION_RATE_TYPE is None so
        ``build_parameter_list`` skips it on the wire.
        """
        from ofam_asset_xfer.fusion_ops import build_book_transfer_params
        from ofam_asset_xfer.paramlist import build_parameter_list

        params = build_book_transfer_params(
            state=_sample_state(),
            dest_book_type_code="JP CORP BOOK",
            effective_date="2026-05-01",
            overrides={},
            request_id="REQ-1",
        )
        assert params["P_CONVERSION_RATE_TYPE"] is None
        assert "P_CONVERSION_RATE_TYPE" not in build_parameter_list(params)

    def test_explicit_conversion_rate_type_is_used(self):
        from ofam_asset_xfer.fusion_ops import build_book_transfer_params

        params = build_book_transfer_params(
            state=_sample_state(),
            dest_book_type_code="JP CORP BOOK",
            effective_date="2026-05-01",
            overrides={"conversion_rate_type": "User"},
            request_id="REQ-1",
        )
        assert params["P_CONVERSION_RATE_TYPE"] == "User"

    def test_retirement_type_code_default(self):
        """Cross-book bookTransfer retires the asset on the source book;
        P_RETIREMENT_TYPE_CODE defaults to 'TRF TO NEW BOOK' (the value
        in Citadel's verified-working payload)."""
        from ofam_asset_xfer.fusion_ops import build_book_transfer_params

        params = build_book_transfer_params(
            state=_sample_state(),
            dest_book_type_code="JP CORP BOOK",
            effective_date="2026-05-01",
            overrides={},
            request_id="REQ-1",
        )
        assert params["P_RETIREMENT_TYPE_CODE"] == "TRF TO NEW BOOK"

    def test_retirement_type_code_override(self):
        from ofam_asset_xfer.fusion_ops import build_book_transfer_params

        params = build_book_transfer_params(
            state=_sample_state(),
            dest_book_type_code="JP CORP BOOK",
            effective_date="2026-05-01",
            overrides={"retirement_type_code": "INTERCOMPANY XFER"},
            request_id="REQ-1",
        )
        assert params["P_RETIREMENT_TYPE_CODE"] == "INTERCOMPANY XFER"

    def test_retirement_type_code_empty_override_drops_param(self):
        """An explicit empty retirement_type_code drops the parameter
        (build_parameter_list skips None)."""
        from ofam_asset_xfer.fusion_ops import build_book_transfer_params
        from ofam_asset_xfer.paramlist import build_parameter_list

        params = build_book_transfer_params(
            state=_sample_state(),
            dest_book_type_code="JP CORP BOOK",
            effective_date="2026-05-01",
            overrides={"retirement_type_code": ""},
            request_id="REQ-1",
        )
        assert params["P_RETIREMENT_TYPE_CODE"] is None
        assert "P_RETIREMENT_TYPE_CODE" not in build_parameter_list(params)

    def test_empty_string_override_is_treated_as_unset(self):
        """An explicit empty conversion_rate_type override is treated as unset.

        Oracle rejects empty strings for FX transfers, so we drop the
        parameter rather than send ``''``.
        """
        from ofam_asset_xfer.fusion_ops import build_book_transfer_params

        params = build_book_transfer_params(
            state=_sample_state(),
            dest_book_type_code="JP CORP BOOK",
            effective_date="2026-05-01",
            overrides={"conversion_rate_type": ""},
            request_id="REQ-1",
        )
        assert params["P_CONVERSION_RATE_TYPE"] is None

    def test_transfer_overrides_seeds_every_cross_book_call(self):
        """Run-wide transfer_overrides on FusionIUSync land in the
        bookTransfer payload for every asset processed in the BIP flow."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.side_effect = lambda params: (
            [_bip_row(book="US CORP BOOK", dff_entity="JP Entity")]
            if params.get("P_BOOK_TYPE_CODE") == "US CORP BOOK"
            else []
        )
        fusion.process_transaction.return_value = _get_asset_info_response()

        sync = FusionIUSync(
            fusion,
            _resolver(),
            bip,
            transfer_overrides={"conversion_rate_type": "Corporate"},
        )
        summary = sync.run_full_sync(
            books=["US CORP BOOK", "JP CORP BOOK"], dry_run=True
        )

        assert summary["counts"]["dry_run"] == 1
        sent = summary["results"][0]["fusion_response"]["planned_params"]
        assert sent["P_CONVERSION_RATE_TYPE"] == "Corporate"

    def test_per_row_target_overrides_still_win_over_run_wide_defaults(self):
        """Run-wide defaults seed the dict; per-row TARGET_LOCATION_ID and
        TARGET_EXPENSE_CCID from BIP overwrite their slots normally."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.side_effect = lambda params: (
            [
                _bip_row(
                    book="US CORP BOOK",
                    dff_entity="JP Entity",
                    target_location_id="300100999999",
                )
            ]
            if params.get("P_BOOK_TYPE_CODE") == "US CORP BOOK"
            else []
        )
        fusion.process_transaction.return_value = _get_asset_info_response()

        sync = FusionIUSync(
            fusion,
            _resolver(),
            bip,
            transfer_overrides={
                "conversion_rate_type": "Corporate",
                "location_ccid": "111-default",  # would lose to TARGET_LOCATION_ID
            },
        )
        summary = sync.run_full_sync(
            books=["US CORP BOOK", "JP CORP BOOK"], dry_run=True
        )

        sent = summary["results"][0]["fusion_response"]["planned_params"]
        # run-wide default lands
        assert sent["P_CONVERSION_RATE_TYPE"] == "Corporate"
        # per-row TARGET_LOCATION_ID still wins for the location slot
        assert "300100999999" in sent["P_LOCATION_CCID_TBL"]
        assert "111-default" not in sent["P_LOCATION_CCID_TBL"]


class TestAssignedToParam:
    """When every assigned_to slot is empty/-1, P_ASSIGNED_TO_TBL must be
    omitted from the params dict (set to None so build_parameter_list
    skips it).  Sending ``','`` or ``'-1,-1'`` causes Fusion to reject
    the transaction with malformed-rosetta errors."""

    def test_helper_returns_none_for_all_empty(self):
        from ofam_asset_xfer.fusion_ops import _assigned_to_param

        assert _assigned_to_param(["", "", ""]) is None

    def test_helper_returns_none_for_all_minus_one(self):
        from ofam_asset_xfer.fusion_ops import _assigned_to_param

        assert _assigned_to_param(["-1", "-1"]) is None

    def test_helper_returns_none_for_mixed_empty_sentinels(self):
        from ofam_asset_xfer.fusion_ops import _assigned_to_param

        assert _assigned_to_param(["", "-1", ""]) is None

    def test_helper_returns_list_when_any_real_value(self):
        from ofam_asset_xfer.fusion_ops import _assigned_to_param

        assert _assigned_to_param(["", "8801", ""]) == ["", "8801", ""]

    def test_same_book_omits_assigned_to_when_unassigned(self):
        from ofam_asset_xfer.fusion_ops import build_same_book_transfer_params

        # _sample_state has assigned_to=[""]
        state = _sample_state()
        params, is_noop = build_same_book_transfer_params(
            state=state,
            effective_date="2026-05-01",
            overrides={"location_ccid": "999000"},
            request_id="REQ-1",
        )
        assert not is_noop
        # Helper returned None -> build_parameter_list will skip it.
        assert params["P_ASSIGNED_TO_TBL"] is None

    def test_same_book_keeps_assigned_to_when_set(self):
        from ofam_asset_xfer.fusion_ops import build_same_book_transfer_params

        state = AssetState(
            asset_id="100",
            asset_number="142847",
            book_type_code="US CORP BOOK",
            distribution_ids=["1001"],
            units_assigned=["1"],
            assigned_to=["8801"],
            expense_ccids=["626955"],
            location_ccids=["789012"],
        )
        params, _ = build_same_book_transfer_params(
            state=state,
            effective_date="2026-05-01",
            overrides={"location_ccid": "999000"},
            request_id="REQ-1",
        )
        # Source leg + dest leg, both with the real person id.
        assert params["P_ASSIGNED_TO_TBL"] == ["8801", "8801"]

    def test_cross_book_omits_assigned_to_when_unassigned(self):
        from ofam_asset_xfer.fusion_ops import build_book_transfer_params

        params = build_book_transfer_params(
            state=_sample_state(),
            dest_book_type_code="JP CORP BOOK",
            effective_date="2026-05-01",
            overrides={},
            request_id="REQ-1",
        )
        assert params["P_ASSIGNED_TO_TBL"] is None

    def test_built_parameter_list_drops_none_assigned_to(self):
        from ofam_asset_xfer.fusion_ops import build_same_book_transfer_params
        from ofam_asset_xfer.paramlist import build_parameter_list

        params, _ = build_same_book_transfer_params(
            state=_sample_state(),
            effective_date="2026-05-01",
            overrides={"location_ccid": "999000"},
            request_id="REQ-1",
        )
        wire = build_parameter_list(params)
        # The serialized rosetta should not contain the empty-comma value.
        assert "P_ASSIGNED_TO_TBL" not in wire


class TestTransferOverridesByBook:
    """Per-target-book overrides should layer on top of run-wide
    transfer_overrides, so an AU ledger that does not publish 'Corporate'
    can use 'Spot' without changing the JP/UK default."""

    def _run_one(
        self,
        target_book: str,
        *,
        overrides_by_book: dict,
        transfer_overrides: dict | None = None,
    ):
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.side_effect = lambda params: (
            [_bip_row(book="US CORP BOOK", dff_entity="AU Entity")]
            if params.get("P_BOOK_TYPE_CODE") == "US CORP BOOK"
            else []
        )
        fusion.process_transaction.return_value = _get_asset_info_response()

        entity_map = {"US Entity": "US CORP BOOK", "AU Entity": target_book}
        sync = FusionIUSync(
            fusion,
            EntityBookResolver(entity_map),
            bip,
            transfer_overrides=transfer_overrides or {},
            transfer_overrides_by_book=overrides_by_book,
        )
        summary = sync.run_full_sync(books=["US CORP BOOK", target_book], dry_run=True)
        return summary["results"][0]["fusion_response"]["planned_params"]

    def test_au_overrides_conversion_rate_type(self):
        params = self._run_one(
            "AU CORP BOOK",
            overrides_by_book={"AU CORP BOOK": {"conversion_rate_type": "Spot"}},
        )
        assert params["P_CONVERSION_RATE_TYPE"] == "Spot"

    def test_jp_uses_its_own_per_book_override(self):
        """Per-book overrides are independent: setting AU does not bleed
        into JP, which gets its own rate type from its own per-book entry."""
        params = self._run_one(
            "JP CORP BOOK",
            overrides_by_book={
                "JP CORP BOOK": {"conversion_rate_type": "Corporate"},
                "AU CORP BOOK": {"conversion_rate_type": "Spot"},
            },
        )
        assert params["P_CONVERSION_RATE_TYPE"] == "Corporate"

    def test_book_with_no_override_omits_rate_type(self):
        """A book not listed in transfer_overrides_by_book and with no
        global default sends no P_CONVERSION_RATE_TYPE on the wire."""
        params = self._run_one(
            "JP CORP BOOK",
            overrides_by_book={"AU CORP BOOK": {"conversion_rate_type": "Spot"}},
        )
        assert params["P_CONVERSION_RATE_TYPE"] is None

    def test_book_lookup_is_case_insensitive(self):
        params = self._run_one(
            "AU CORP BOOK",
            overrides_by_book={"au corp book": {"conversion_rate_type": "User"}},
        )
        assert params["P_CONVERSION_RATE_TYPE"] == "User"

    def test_per_book_override_does_not_disturb_other_keys(self):
        params = self._run_one(
            "AU CORP BOOK",
            overrides_by_book={"AU CORP BOOK": {"conversion_rate_type": "Spot"}},
        )
        # Globals (copy_dff_flag etc.) keep their defaults; only the
        # explicitly-overridden key changes.
        assert params["P_COPY_DFF_FLAG"] == "Y"


class TestBumpUnderscoreSuffix:
    """Suffix-bump rule for the post-transfer DFF update."""

    def test_appends_underscore_one_when_no_suffix(self):
        from ofam_asset_xfer.fusion_ops import bump_underscore_suffix

        assert bump_underscore_suffix("ASSET-100") == "ASSET-100_1"

    def test_increments_existing_suffix(self):
        from ofam_asset_xfer.fusion_ops import bump_underscore_suffix

        assert bump_underscore_suffix("ASSET-100_1") == "ASSET-100_2"
        assert bump_underscore_suffix("ASSET-100_9") == "ASSET-100_10"
        assert bump_underscore_suffix("ASSET-100_99") == "ASSET-100_100"

    def test_none_and_empty(self):
        from ofam_asset_xfer.fusion_ops import bump_underscore_suffix

        assert bump_underscore_suffix(None) == "_1"
        assert bump_underscore_suffix("") == "_1"

    def test_only_trailing_digit_run_counts(self):
        """_5_3 → _5_4 (trailing _N wins; leading _5 left alone)."""
        from ofam_asset_xfer.fusion_ops import bump_underscore_suffix

        assert bump_underscore_suffix("A_5_3") == "A_5_4"

    def test_non_digit_suffix_not_treated_as_n(self):
        """_abc is not _N; append _1."""
        from ofam_asset_xfer.fusion_ops import bump_underscore_suffix

        assert bump_underscore_suffix("ASSET_abc") == "ASSET_abc_1"


class TestStripUnderscoreSuffix:
    """Removes any trailing _N from a tag — gives the canonical
    'original' tag that the destination of a cross-book transfer
    should carry."""

    def test_strips_existing_suffix(self):
        from ofam_asset_xfer.fusion_ops import strip_underscore_suffix

        assert strip_underscore_suffix("ASSET-100_1") == "ASSET-100"
        assert strip_underscore_suffix("ASSET-100_99") == "ASSET-100"

    def test_leaves_clean_value_alone(self):
        from ofam_asset_xfer.fusion_ops import strip_underscore_suffix

        assert strip_underscore_suffix("ASSET-100") == "ASSET-100"

    def test_only_trailing_run_stripped(self):
        """A_5_3 → A_5 (only the trailing _3 is removed)."""
        from ofam_asset_xfer.fusion_ops import strip_underscore_suffix

        assert strip_underscore_suffix("A_5_3") == "A_5"

    def test_none_and_empty(self):
        from ofam_asset_xfer.fusion_ops import strip_underscore_suffix

        assert strip_underscore_suffix(None) == ""
        assert strip_underscore_suffix("") == ""


class TestApplyUnderscoreSuffix:
    """Apply a specific N to a tag, replacing any existing _N suffix."""

    def test_applies_to_clean_value(self):
        from ofam_asset_xfer.fusion_ops import apply_underscore_suffix

        assert apply_underscore_suffix("TAG", 1) == "TAG_1"
        assert apply_underscore_suffix("TAG", 7) == "TAG_7"

    def test_replaces_existing_suffix(self):
        """If the input already has a _N, it's replaced with the new N
        (not stacked).  TAG_2 + 5 → TAG_5, not TAG_2_5."""
        from ofam_asset_xfer.fusion_ops import apply_underscore_suffix

        assert apply_underscore_suffix("TAG_2", 5) == "TAG_5"
        assert apply_underscore_suffix("TAG_10", 3) == "TAG_3"

    def test_none_and_empty(self):
        from ofam_asset_xfer.fusion_ops import apply_underscore_suffix

        assert apply_underscore_suffix(None, 3) == "_3"
        assert apply_underscore_suffix("", 3) == "_3"


class TestPostTransferAttributeUpdate:
    """End-to-end behavior of the opt-in post-bookTransfer DFF bump."""

    def _build_sync(self, fusion, *, enabled: bool, **cfg):
        bip = _mock_bip()
        bip.run_report.side_effect = lambda params: (
            [_bip_row(book="US CORP BOOK", dff_entity="UK Entity")]
            if params.get("P_BOOK_TYPE_CODE") == "US CORP BOOK"
            else []
        )
        return FusionIUSync(
            fusion,
            _resolver(),
            bip,
            post_transfer_attribute_update={"enabled": enabled, **cfg},
        )

    @staticmethod
    def _book_transfer_success_response(
        *, new_asset_id: str | None = None, new_asset_number: str | None = None
    ):
        pl: dict = {"X_RETURN_STATUS": "S"}
        if new_asset_id is not None:
            pl["X_NEW_ASSET_ID"] = new_asset_id
        if new_asset_number is not None:
            pl["X_NEW_ASSET_NUMBER"] = new_asset_number
        return ({"OperationName": "processTransaction-bookTransfer"}, pl)

    @staticmethod
    def _get_asset_info_with_attr10(value: str):
        return TestPostTransferAttributeUpdate._get_asset_info_with_attr(
            "attribute10", value
        )

    @staticmethod
    def _get_asset_info_with_tag(value: str):
        """Build a getAssetInformation response with X_TAG_NUMBER set.

        Default-path tests use this — the post-transfer bump now defaults
        to ``source_field=tag_number`` / ``fusion_parameter=P_TAG_NUMBER``
        (Oracle FA's canonical header Tag Number), not a DFF slot.
        """
        return TestPostTransferAttributeUpdate._get_asset_info_with_attr(
            "tag_number", value
        )

    @staticmethod
    def _get_asset_info_with_attr(slot: str, value: str):
        """Build a getAssetInformation response with any X_<slot> field set.

        ``slot`` is a lower-case name like ``attribute7`` / ``tag_number``;
        emitted on the response as ``X_ATTRIBUTE7`` / ``X_TAG_NUMBER``.
        """
        pl = {
            "X_RETURN_STATUS": "S",
            "X_ASSET_ID": "100",
            "X_ASSET_NUMBER": "142847",
            "X_BOOK_TYPE_CODE": "US CORP BOOK",
            "X_DISTRIBUTION_ID_TBL": "1001",
            "X_UNITS_ASSIGNED_TBL": "1",
            "X_ASSIGNED_TO_TBL": "",
            "X_EXPENSE_CCID_TBL": "626955",
            "X_LOCATION_CCID_TBL": "789012",
            f"X_{slot.upper()}": value,
        }
        return ({}, pl)

    def test_disabled_by_default_no_extra_calls(self):
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("ASSET-100"),  # discovery
            self._book_transfer_success_response(),  # bookTransfer
        ]
        sync = self._build_sync(fusion, enabled=False)
        summary = sync.run_full_sync(
            books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False
        )
        # exactly the two calls above, no post-update
        assert fusion.process_transaction.call_count == 2
        assert summary["results"][0]["status"] == "TRANSFERRED"

    def test_enabled_bumps_and_posts_update_with_new_asset_id_from_response(self):
        """Happy path: Oracle returns X_NEW_ASSET_ID on the bookTransfer
        response.  Two updateAssetDescriptiveDetails POSTs are issued:
        the retired source gets the suffixed tag, the new destination
        gets the clean canonical tag."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("ASSET-100"),  # discovery
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),  # source-side update
            ({}, {"X_RETURN_STATUS": "S"}),  # destination-side update
        ]
        sync = self._build_sync(fusion, enabled=True)
        summary = sync.run_full_sync(
            books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False
        )

        # 4 calls: discovery + bookTransfer + 2 updates
        assert fusion.process_transaction.call_count == 4
        src_op, src_params = fusion.process_transaction.call_args_list[-2][0]
        dst_op, dst_params = fusion.process_transaction.call_args_list[-1][0]
        assert src_op == "updateAssetDescriptiveDetails"
        assert dst_op == "updateAssetDescriptiveDetails"
        # Source: retired, gets the suffix.  asset_id=100 (discovery).
        assert src_params["P_ASSET_ID"] == "100"
        assert src_params["P_TAG_NUMBER"] == "ASSET-100_1"
        # Destination: new active, gets the clean canonical tag.
        assert dst_params["P_ASSET_ID"] == "555"
        assert dst_params["P_TAG_NUMBER"] == "ASSET-100"
        assert summary["results"][0]["status"] == "TRANSFERRED"
        assert summary["results"][0]["error"] is None

    def test_falls_back_to_dest_lookup_when_response_lacks_id_field(self):
        """Tenant doesn't echo X_NEW_ASSET_ID; resolver falls back to
        getAssetInformation against the target book using the source
        asset_number (when allow_same_asset_number_fallback=True)."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("ASSET-100"),  # discovery
            self._book_transfer_success_response(),  # bookTransfer with NO new-id fields
            self._get_asset_info_with_tag("ASSET-100"),  # fallback lookup
            ({}, {"X_RETURN_STATUS": "S"}),  # source update
            ({}, {"X_RETURN_STATUS": "S"}),  # destination update
        ]
        sync = self._build_sync(fusion, enabled=True)
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)
        # discovery + bookTransfer + fallback lookup + 2 updates = 5
        assert fusion.process_transaction.call_count == 5
        src_op, src_params = fusion.process_transaction.call_args_list[-2][0]
        dst_op, dst_params = fusion.process_transaction.call_args_list[-1][0]
        assert src_op == "updateAssetDescriptiveDetails"
        assert src_params["P_TAG_NUMBER"] == "ASSET-100_1"
        assert dst_params["P_TAG_NUMBER"] == "ASSET-100"

    def test_no_id_in_response_and_fallback_disabled_warns(self):
        """If neither the response carries an id/number nor the fallback
        is allowed, the post-update is skipped and a warning is recorded."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("ASSET-100"),
            self._book_transfer_success_response(),  # no new-id fields
        ]
        sync = self._build_sync(
            fusion, enabled=True, allow_same_asset_number_fallback=False
        )
        summary = sync.run_full_sync(
            books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False
        )
        # Only discovery + bookTransfer; no update was attempted.
        assert fusion.process_transaction.call_count == 2
        result = summary["results"][0]
        assert result["status"] == "TRANSFERRED"
        assert "could not resolve destination asset_id" in (result["error"] or "")

    def test_enabled_increments_existing_underscore_suffix(self):
        """Source already had ASSET-100_3 (third transfer in flight).
        Without chain BIP, fallback is bump_underscore_suffix -> _4.
        Destination gets the stripped canonical ASSET-100."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("ASSET-100_3"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),  # source update
            ({}, {"X_RETURN_STATUS": "S"}),  # destination update
        ]
        sync = self._build_sync(fusion, enabled=True)
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)

        _, src_params = fusion.process_transaction.call_args_list[-2][0]
        _, dst_params = fusion.process_transaction.call_args_list[-1][0]
        assert src_params["P_TAG_NUMBER"] == "ASSET-100_4"
        assert dst_params["P_TAG_NUMBER"] == "ASSET-100"

    DEST_LOOKUP_REPORT = "/Custom/Reports/Get_Transferred_Asset.xdo"

    def _bip_router(self, dest_lookup_return):
        """Build a side_effect that dispatches BIP calls by report_path.

        Discovery calls (no report_path override) return the IU row only
        when scanning US CORP BOOK.  The destination-lookup report
        returns the supplied value (list, exception, or callable).
        """

        def _run(params=None, report_path=None):
            if report_path == self.DEST_LOOKUP_REPORT:
                if isinstance(dest_lookup_return, BaseException):
                    raise dest_lookup_return
                return dest_lookup_return
            if (params or {}).get("P_BOOK_TYPE_CODE") == "US CORP BOOK":
                return [_bip_row(book="US CORP BOOK", dff_entity="UK Entity")]
            return []

        return _run

    def test_dest_lookup_bip_is_preferred_over_response_field(self):
        """When dest_lookup_bip is configured, it wins over the
        bookTransfer response field lookup."""
        fusion = _mock_fusion()
        # Response carries X_NEW_ASSET_ID=111 — but BIP should win.
        # (uses default tag_number / P_TAG_NUMBER target)
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("ASSET-100"),
            self._book_transfer_success_response(new_asset_id="111"),
            ({}, {"X_RETURN_STATUS": "S"}),  # source update
            ({}, {"X_RETURN_STATUS": "S"}),  # destination update
        ]
        bip = _mock_bip()
        bip.run_report.side_effect = self._bip_router(
            [{"NEW_ASSET_ID": "999", "NEW_ASSET_NUMBER": "AUS-12345"}]
        )
        sync = FusionIUSync(
            fusion,
            _resolver(),
            bip,
            post_transfer_attribute_update={
                "enabled": True,
                "dest_lookup_bip": {
                    "report_path": self.DEST_LOOKUP_REPORT,
                    "params": {
                        "P_SOURCE_ASSET_ID": "${source_asset_id}",
                        "P_TARGET_BOOK": "${target_book_type_code}",
                    },
                    "asset_id_column": "NEW_ASSET_ID",
                    "asset_number_column": "NEW_ASSET_NUMBER",
                },
            },
        )
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)

        # Destination update was POSTed against the BIP-resolved id (999), not 111.
        _, dst_params = fusion.process_transaction.call_args_list[-1][0]
        assert dst_params["P_ASSET_ID"] == "999"
        assert dst_params["P_TAG_NUMBER"] == "ASSET-100"  # clean canonical
        _, src_params = fusion.process_transaction.call_args_list[-2][0]
        assert src_params["P_TAG_NUMBER"] == "ASSET-100_1"  # suffixed

        # The BIP report was called with the interpolated params.
        dest_calls = [
            c
            for c in bip.run_report.call_args_list
            if (c.kwargs or {}).get("report_path") == self.DEST_LOOKUP_REPORT
        ]
        assert len(dest_calls) == 1
        sent = dest_calls[0].kwargs["params"]
        assert sent["P_SOURCE_ASSET_ID"] == "100"
        assert sent["P_TARGET_BOOK"] == "UK CORP BOOK"

    def test_dest_lookup_bip_empty_rows_falls_back_to_response(self):
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("ASSET-100"),
            self._book_transfer_success_response(new_asset_id="111"),
            ({}, {"X_RETURN_STATUS": "S"}),  # source update
            ({}, {"X_RETURN_STATUS": "S"}),  # destination update
        ]
        bip = _mock_bip()
        bip.run_report.side_effect = self._bip_router([])  # empty rows
        sync = FusionIUSync(
            fusion,
            _resolver(),
            bip,
            post_transfer_attribute_update={
                "enabled": True,
                "dest_lookup_bip": {
                    "report_path": self.DEST_LOOKUP_REPORT,
                    "params": {"P_SOURCE_ASSET_ID": "${source_asset_id}"},
                    "asset_id_column": "NEW_ASSET_ID",
                },
            },
        )
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)

        _, dst_params = fusion.process_transaction.call_args_list[-1][0]
        assert dst_params["P_ASSET_ID"] == "111"

    def test_dest_lookup_bip_failure_falls_back_to_response(self):
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("ASSET-100"),
            self._book_transfer_success_response(new_asset_id="111"),
            ({}, {"X_RETURN_STATUS": "S"}),  # source update
            ({}, {"X_RETURN_STATUS": "S"}),  # destination update
        ]
        bip = _mock_bip()
        bip.run_report.side_effect = self._bip_router(RuntimeError("BIP SOAP 500"))
        sync = FusionIUSync(
            fusion,
            _resolver(),
            bip,
            post_transfer_attribute_update={
                "enabled": True,
                "dest_lookup_bip": {
                    "report_path": self.DEST_LOOKUP_REPORT,
                    "params": {"P_SOURCE_ASSET_ID": "${source_asset_id}"},
                    "asset_id_column": "NEW_ASSET_ID",
                },
            },
        )
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)
        op_handle, params = fusion.process_transaction.call_args_list[-1][0]
        assert params["P_ASSET_ID"] == "111"

    def test_custom_dest_asset_id_field_name(self):
        """Tenants whose bookTransfer returns the new id under a custom
        field name (e.g. X_DESTINATION_ASSET_ID) can extend the lookup
        list via config."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("ASSET-100"),
            (
                {},
                {
                    "X_RETURN_STATUS": "S",
                    "X_CITADEL_NEW_ASSET_PK": "777",
                },
            ),
            ({}, {"X_RETURN_STATUS": "S"}),  # source update
            ({}, {"X_RETURN_STATUS": "S"}),  # destination update
        ]
        sync = self._build_sync(
            fusion,
            enabled=True,
            dest_asset_id_fields=["X_CITADEL_NEW_ASSET_PK"],
        )
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)
        _, dst_params = fusion.process_transaction.call_args_list[-1][0]
        assert dst_params["P_ASSET_ID"] == "777"

    def test_failed_bookTransfer_skips_post_update(self):
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("ASSET-100"),
            ({}, {"X_RETURN_STATUS": "E", "X_MSG_DATA": "boom"}),
        ]
        sync = self._build_sync(fusion, enabled=True)
        summary = sync.run_full_sync(
            books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False
        )
        # Only discovery + the failed bookTransfer.  Post-update must not fire.
        assert fusion.process_transaction.call_count == 2
        assert summary["results"][0]["status"] == "FAILED"

    def test_bumps_cat_attribute7_when_tenant_uses_category_dff(self):
        """Tenants that store the tag identifier in the category DFF
        (X_CAT_ATTRIBUTE7) must be addressable via source_field=
        cat_attribute7 + fusion_parameter=P_CAT_ATTRIBUTE7.  Source
        gets suffixed, destination gets clean canonical."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_attr("cat_attribute7", "TAG-100"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),  # source update
            ({}, {"X_RETURN_STATUS": "S"}),  # destination update
        ]
        sync = self._build_sync(
            fusion,
            enabled=True,
            source_field="cat_attribute7",
            fusion_parameter="P_CAT_ATTRIBUTE7",
        )
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)

        _, src_params = fusion.process_transaction.call_args_list[-2][0]
        _, dst_params = fusion.process_transaction.call_args_list[-1][0]
        assert src_params["P_CAT_ATTRIBUTE7"] == "TAG-100_1"
        assert dst_params["P_CAT_ATTRIBUTE7"] == "TAG-100"
        assert dst_params["P_ASSET_ID"] == "555"

    def test_bumps_attribute7_when_source_field_is_attribute7(self):
        """source_field=attribute7 reads X_ATTRIBUTE7; source-side POST
        sends P_ATTRIBUTE7=TAG-100_1, destination-side P_ATTRIBUTE7=TAG-100."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_attr("attribute7", "TAG-100"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),  # source update
            ({}, {"X_RETURN_STATUS": "S"}),  # destination update
        ]
        sync = self._build_sync(
            fusion,
            enabled=True,
            source_field="attribute7",
            fusion_parameter="P_ATTRIBUTE7",
        )
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)

        _, src_params = fusion.process_transaction.call_args_list[-2][0]
        _, dst_params = fusion.process_transaction.call_args_list[-1][0]
        assert src_params["P_ATTRIBUTE7"] == "TAG-100_1"
        assert dst_params["P_ATTRIBUTE7"] == "TAG-100"

    def test_attribute_source_field_is_case_insensitive(self):
        """Config that writes ``source_field: "ATTRIBUTE7"`` (upper-case)
        still resolves to the X_ATTRIBUTE7 value."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_attr("attribute7", "TAG-100"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),  # source update
            ({}, {"X_RETURN_STATUS": "S"}),  # destination update
        ]
        sync = self._build_sync(
            fusion,
            enabled=True,
            source_field="ATTRIBUTE7",
            fusion_parameter="P_ATTRIBUTE7",
        )
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)
        _, src_params = fusion.process_transaction.call_args_list[-2][0]
        _, dst_params = fusion.process_transaction.call_args_list[-1][0]
        assert src_params["P_ATTRIBUTE7"] == "TAG-100_1"
        assert dst_params["P_ATTRIBUTE7"] == "TAG-100"

    def test_skip_when_source_empty_and_no_dff_work(self):
        """If the configured source DFF is empty AND no populate/clear
        DFFs are configured, the whole follow-up is skipped (no
        updateAssetDescriptiveDetails call) — '_1' is never seeded."""
        fusion = _mock_fusion()
        # X_ATTRIBUTE7 missing entirely
        info_no_attr = (
            {},
            {
                "X_RETURN_STATUS": "S",
                "X_ASSET_ID": "100",
                "X_ASSET_NUMBER": "142847",
                "X_BOOK_TYPE_CODE": "US CORP BOOK",
                "X_DISTRIBUTION_ID_TBL": "1001",
                "X_UNITS_ASSIGNED_TBL": "1",
                "X_ASSIGNED_TO_TBL": "",
                "X_EXPENSE_CCID_TBL": "626955",
                "X_LOCATION_CCID_TBL": "789012",
            },
        )
        fusion.process_transaction.side_effect = [
            info_no_attr,  # discovery
            self._book_transfer_success_response(new_asset_id="555"),  # bookTransfer
        ]
        sync = self._build_sync(
            fusion,
            enabled=True,
            source_field="attribute7",
            fusion_parameter="P_ATTRIBUTE7",
        )
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)
        # discovery + bookTransfer only.  Update call must NOT fire.
        assert fusion.process_transaction.call_count == 2

    def test_empty_source_still_applies_dff_updates(self):
        """An untagged source no longer aborts the follow-up: the tag
        bump is skipped (no '_1' seeded, no tag param on the POST) but
        the destination POST still runs its populate/clear DFFs.  The
        source POST is omitted — no tag and no source-side DFFs."""
        fusion = _mock_fusion()
        info_no_attr = (
            {},
            {
                "X_RETURN_STATUS": "S",
                "X_ASSET_ID": "100",
                "X_ASSET_NUMBER": "142847",
                "X_BOOK_TYPE_CODE": "US CORP BOOK",
                "X_DISTRIBUTION_ID_TBL": "1001",
                "X_UNITS_ASSIGNED_TBL": "1",
                "X_ASSIGNED_TO_TBL": "",
                "X_EXPENSE_CCID_TBL": "626955",
                "X_LOCATION_CCID_TBL": "789012",
            },
        )
        fusion.process_transaction.side_effect = [
            info_no_attr,  # discovery
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),  # destination update
        ]
        sync = self._build_sync(
            fusion,
            enabled=True,
            source_field="attribute7",
            fusion_parameter="P_ATTRIBUTE7",
            populate_dest_dffs={"P_ATTRIBUTE13": "${source_book_type_code}"},
            clear_dest_dffs={"P_CAT_ATTRIBUTE9": ""},
        )
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)
        # discovery + bookTransfer + ONE destination POST (source POST
        # skipped: no tag, no source-side DFFs).
        assert fusion.process_transaction.call_count == 3
        op, params = fusion.process_transaction.call_args_list[-1][0]
        assert op == "updateAssetDescriptiveDetails"
        assert params["P_ASSET_ID"] == "555"
        # DFF updates applied...
        assert params["P_ATTRIBUTE13"] == "US CORP BOOK"
        assert params["P_CAT_ATTRIBUTE9"] == ""
        # ...but the tag param is left off entirely.
        assert "P_ATTRIBUTE7" not in params

    def test_post_update_failure_keeps_status_transferred_but_warns(self):
        """Source POST fails with E; destination POST still runs (per-side
        isolation).  Transfer stays TRANSFERRED with a warning."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("ASSET-100"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "E", "X_MSG_DATA": "permission denied"}),
            ({}, {"X_RETURN_STATUS": "S"}),  # destination still attempted
        ]
        sync = self._build_sync(fusion, enabled=True)
        summary = sync.run_full_sync(
            books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False
        )
        result = summary["results"][0]
        assert result["status"] == "TRANSFERRED"
        assert "post-transfer attribute update warning" in (result["error"] or "")
        assert "permission denied" in (result["error"] or "")

    def test_two_posts_per_transfer_source_suffixed_destination_clean(self):
        """Every transfer issues exactly two updateAssetDescriptiveDetails
        POSTs: source gets the suffixed tag, destination gets the clean
        canonical tag.  No optional flag — this is the contract."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("ASSET-100"),  # discovery
            self._book_transfer_success_response(new_asset_id="555"),  # bookTransfer
            ({}, {"X_RETURN_STATUS": "S"}),  # source-side update
            ({}, {"X_RETURN_STATUS": "S"}),  # destination-side update
        ]
        sync = self._build_sync(fusion, enabled=True)
        summary = sync.run_full_sync(
            books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False
        )

        assert summary["results"][0]["status"] == "TRANSFERRED"
        assert summary["results"][0]["error"] is None
        # 4 calls total: discovery + bookTransfer + source + destination
        assert fusion.process_transaction.call_count == 4

        # Source update — retired asset gets the suffix.
        src_op, src_params = fusion.process_transaction.call_args_list[-2][0]
        assert src_op == "updateAssetDescriptiveDetails"
        assert src_params["P_ASSET_ID"] == "100"
        assert src_params["P_TAG_NUMBER"] == "ASSET-100_1"

        # Destination update — new active asset gets the clean tag.
        dest_op, dest_params = fusion.process_transaction.call_args_list[-1][0]
        assert dest_op == "updateAssetDescriptiveDetails"
        assert dest_params["P_ASSET_ID"] == "555"
        assert dest_params["P_TAG_NUMBER"] == "ASSET-100"

    def test_clear_dest_dffs_applies_only_to_destination(self):
        """clear_dest_dffs blanks directive DFFs on the destination
        (not on the source-side update)."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("ASSET-100"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),  # source update
            ({}, {"X_RETURN_STATUS": "S"}),  # destination update
        ]
        sync = self._build_sync(
            fusion,
            enabled=True,
            clear_dest_dffs={"P_CAT_ATTRIBUTE9": ""},
        )
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)
        _, src_params = fusion.process_transaction.call_args_list[-2][0]
        _, dst_params = fusion.process_transaction.call_args_list[-1][0]
        assert src_params["P_TAG_NUMBER"] == "ASSET-100_1"
        assert "P_CAT_ATTRIBUTE9" not in src_params  # source untouched
        assert dst_params["P_TAG_NUMBER"] == "ASSET-100"
        assert dst_params["P_CAT_ATTRIBUTE9"] == ""

    def test_default_targets_canonical_tag_number_field(self):
        """With no source_field/fusion_parameter overrides, the post-update
        reads X_TAG_NUMBER from the source asset and POSTs P_TAG_NUMBER
        — Oracle FA's canonical header Tag Number.  Source gets suffixed,
        destination gets clean."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("HW-SERVER-42"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),  # source
            ({}, {"X_RETURN_STATUS": "S"}),  # destination
        ]
        sync = self._build_sync(fusion, enabled=True)
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)

        _, src_params = fusion.process_transaction.call_args_list[-2][0]
        _, dst_params = fusion.process_transaction.call_args_list[-1][0]
        assert src_params["P_TAG_NUMBER"] == "HW-SERVER-42_1"
        assert dst_params["P_TAG_NUMBER"] == "HW-SERVER-42"
        # DFF slots are NOT touched by default.
        assert "P_ATTRIBUTE10" not in dst_params
        assert "P_CAT_ATTRIBUTE7" not in dst_params

    def test_also_bump_params_mirrors_into_secondary_dff_on_destination(self):
        """also_bump_params=['P_CAT_ATTRIBUTE7'] mirrors the same _N
        value the canonical tag gets into the CMDB tag DFF, so the
        asset header and the CMDB record stay in lockstep."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("HW-SERVER-42"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),
        ]
        sync = self._build_sync(
            fusion,
            enabled=True,
            also_bump_params=["P_CAT_ATTRIBUTE7"],
        )
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)

        _, src_params = fusion.process_transaction.call_args_list[-2][0]
        _, dst_params = fusion.process_transaction.call_args_list[-1][0]
        # Source: both primary tag and mirrored DFF get the suffixed value.
        assert src_params["P_TAG_NUMBER"] == "HW-SERVER-42_1"
        assert src_params["P_CAT_ATTRIBUTE7"] == "HW-SERVER-42_1"
        # Destination: both get the clean canonical value.
        assert dst_params["P_TAG_NUMBER"] == "HW-SERVER-42"
        assert dst_params["P_CAT_ATTRIBUTE7"] == "HW-SERVER-42"

    def test_also_bump_params_mirrors_on_both_sides(self):
        """also_bump_params applies on both updates: source-side gets the
        suffixed value, destination-side gets the clean canonical value
        for every secondary param in the list."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("HW-SERVER-42"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),  # source update
            ({}, {"X_RETURN_STATUS": "S"}),  # destination update
        ]
        sync = self._build_sync(
            fusion,
            enabled=True,
            also_bump_params=["P_CAT_ATTRIBUTE7"],
        )
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)

        _, src_params = fusion.process_transaction.call_args_list[-2][0]
        _, dest_params = fusion.process_transaction.call_args_list[-1][0]
        assert src_params["P_ASSET_ID"] == "100"
        assert src_params["P_TAG_NUMBER"] == "HW-SERVER-42_1"
        assert src_params["P_CAT_ATTRIBUTE7"] == "HW-SERVER-42_1"
        assert dest_params["P_ASSET_ID"] == "555"
        assert dest_params["P_TAG_NUMBER"] == "HW-SERVER-42"
        assert dest_params["P_CAT_ATTRIBUTE7"] == "HW-SERVER-42"

    def test_empty_also_bump_params_no_extra_keys(self):
        """Empty / missing also_bump_params is a no-op — only the
        primary fusion_parameter is sent."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("HW-SERVER-42"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),  # source
            ({}, {"X_RETURN_STATUS": "S"}),  # destination
        ]
        sync = self._build_sync(fusion, enabled=True, also_bump_params=[])
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)
        _, src_params = fusion.process_transaction.call_args_list[-2][0]
        _, dst_params = fusion.process_transaction.call_args_list[-1][0]
        assert src_params["P_TAG_NUMBER"] == "HW-SERVER-42_1"
        assert dst_params["P_TAG_NUMBER"] == "HW-SERVER-42"

    def test_skip_when_tag_empty_and_no_dff_work(self):
        """Source asset has no X_TAG_NUMBER (untagged) and no
        populate/clear DFFs configured — nothing to do, so no update is
        posted; we never seed '_1' on a previously-untagged asset."""
        fusion = _mock_fusion()
        # X_TAG_NUMBER absent
        info_no_tag = (
            {},
            {
                "X_RETURN_STATUS": "S",
                "X_ASSET_ID": "100",
                "X_ASSET_NUMBER": "142847",
                "X_BOOK_TYPE_CODE": "US CORP BOOK",
                "X_DISTRIBUTION_ID_TBL": "1001",
                "X_UNITS_ASSIGNED_TBL": "1",
                "X_ASSIGNED_TO_TBL": "",
                "X_EXPENSE_CCID_TBL": "626955",
                "X_LOCATION_CCID_TBL": "789012",
            },
        )
        fusion.process_transaction.side_effect = [
            info_no_tag,
            self._book_transfer_success_response(new_asset_id="555"),
        ]
        sync = self._build_sync(fusion, enabled=True)
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)
        # Only discovery + bookTransfer — no update calls.
        assert fusion.process_transaction.call_count == 2

    def test_source_update_failure_kept_as_warning_alongside_dest_success(self):
        """If source update fails but dest succeeds, source failure is
        reported in the warning, transfer stays TRANSFERRED."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("ASSET-100"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "E", "X_MSG_DATA": "src failed"}),
            ({}, {"X_RETURN_STATUS": "S"}),
        ]
        sync = self._build_sync(fusion, enabled=True)
        summary = sync.run_full_sync(
            books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False
        )
        result = summary["results"][0]
        assert result["status"] == "TRANSFERRED"
        assert "source:" in (result["error"] or "")
        assert "src failed" in (result["error"] or "")


class TestPopulateDestDffs:
    """populate_dest_dffs sets templated DFF values on the destination
    asset — typically to fill in 'Source Asset Book' / 'Source Asset
    Number' DFFs so the new asset record points back at where it came
    from.  Values support ${source_*} / ${target_*} placeholders."""

    def _build_sync(self, fusion, **cfg):
        bip = _mock_bip()
        bip.run_report.side_effect = lambda params: (
            [_bip_row(book="US CORP BOOK", dff_entity="UK Entity")]
            if params.get("P_BOOK_TYPE_CODE") == "US CORP BOOK"
            else []
        )
        return FusionIUSync(
            fusion,
            _resolver(),
            bip,
            post_transfer_attribute_update={"enabled": True, **cfg},
        )

    @staticmethod
    def _get_asset_info_with_tag(value: str):
        return TestPostTransferAttributeUpdate._get_asset_info_with_tag(value)

    @staticmethod
    def _book_transfer_success_response(new_asset_id="555"):
        return TestPostTransferAttributeUpdate._book_transfer_success_response(
            new_asset_id=new_asset_id
        )

    def test_populates_source_book_and_source_number_on_destination(self):
        """The destination asset's Source Asset Book / Source Asset
        Number DFFs (tenant-specific P_ATTRIBUTE_* names) get the
        source asset's book + number."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("TAG-100"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),
        ]
        sync = self._build_sync(
            fusion,
            populate_dest_dffs={
                "P_ATTRIBUTE13": "${source_book_type_code}",
                "P_ATTRIBUTE14": "${source_asset_number}",
            },
        )
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)

        _, params = fusion.process_transaction.call_args_list[-1][0]
        # Source book + asset number from the in-flight transfer.
        assert params["P_ATTRIBUTE13"] == "US CORP BOOK"
        assert params["P_ATTRIBUTE14"] == "142847"

    def test_populate_and_clear_can_target_different_params_together(self):
        """populate_dest_dffs and clear_dest_dffs are independent —
        populate sets new values, clear blanks the directive DFFs that
        drove the transfer (Transfer Date / Transfer to Entity /
        Transfer to Location)."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("TAG-100"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),
        ]
        sync = self._build_sync(
            fusion,
            populate_dest_dffs={
                "P_ATTRIBUTE13": "${source_book_type_code}",
                "P_ATTRIBUTE14": "${source_asset_number}",
            },
            clear_dest_dffs={
                "P_CAT_ATTRIBUTE5": "",  # Transfer Date
                "P_CAT_ATTRIBUTE9": "",  # Transfer to Entity
                "P_CAT_ATTRIBUTE6": "",  # Transfer to Location
            },
        )
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)

        _, params = fusion.process_transaction.call_args_list[-1][0]
        assert params["P_ATTRIBUTE13"] == "US CORP BOOK"
        assert params["P_ATTRIBUTE14"] == "142847"
        assert params["P_CAT_ATTRIBUTE5"] == ""
        assert params["P_CAT_ATTRIBUTE9"] == ""
        assert params["P_CAT_ATTRIBUTE6"] == ""

    def test_clear_wins_when_same_param_in_both_populate_and_clear(self):
        """If misconfigured to both populate and clear the same param,
        clear wins — explicit-clear is the safer outcome."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("TAG-100"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),
        ]
        sync = self._build_sync(
            fusion,
            populate_dest_dffs={"P_ATTRIBUTE13": "${source_book_type_code}"},
            clear_dest_dffs={"P_ATTRIBUTE13": ""},
        )
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)
        _, params = fusion.process_transaction.call_args_list[-1][0]
        assert params["P_ATTRIBUTE13"] == ""

    def test_unknown_placeholders_left_in_place(self):
        """Misconfigured ${...} placeholders aren't silently dropped —
        they appear literally in the POST, so the bad config is
        visible in --debug dumps."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("TAG-100"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),
        ]
        sync = self._build_sync(
            fusion,
            populate_dest_dffs={"P_ATTRIBUTE13": "${not_a_real_key}"},
        )
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)
        _, params = fusion.process_transaction.call_args_list[-1][0]
        assert params["P_ATTRIBUTE13"] == "${not_a_real_key}"

    def test_populate_source_dffs_applies_to_source_asset_only(self):
        """populate_source_dffs lands on the source-side update only
        (not the destination).  Mirror of populate_dest_dffs."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("TAG-100"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),  # source
            ({}, {"X_RETURN_STATUS": "S"}),  # destination
        ]
        sync = self._build_sync(
            fusion,
            bump_source_too=True,
            populate_source_dffs={"P_ATTRIBUTE15": "${target_book_type_code}"},
            populate_dest_dffs={"P_ATTRIBUTE13": "${source_book_type_code}"},
        )
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)

        _, src_params = fusion.process_transaction.call_args_list[-2][0]
        _, dest_params = fusion.process_transaction.call_args_list[-1][0]
        # Source got source-DFF, NOT dest-DFF.
        assert src_params["P_ATTRIBUTE15"] == "UK CORP BOOK"
        assert "P_ATTRIBUTE13" not in src_params
        # Destination got dest-DFF, NOT source-DFF.
        assert dest_params["P_ATTRIBUTE13"] == "US CORP BOOK"
        assert "P_ATTRIBUTE15" not in dest_params

    def test_current_date_placeholder_resolves_to_today(self):
        """${current_date} interpolates to today's date (YYYY-MM-DD) —
        used to default a DATE DFF on the destination, since Oracle FA
        treats an empty string as 'no change' for DATE columns and so
        such a field cannot be blanked."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("TAG-100"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),  # source POST
            ({}, {"X_RETURN_STATUS": "S"}),  # destination POST
        ]
        sync = self._build_sync(
            fusion,
            populate_dest_dffs={"P_CAT_ATTRIBUTE_DATE1": "${current_date}"},
        )
        sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)
        _, params = fusion.process_transaction.call_args_list[-1][0]
        assert params["P_CAT_ATTRIBUTE_DATE1"] == date.today().isoformat()


class TestBumpChain:
    """bump_chain=true walks the full lineage chain returned by the
    lineage-lookup BIP report and POSTs the same bumped Tag Number
    against every link, so historical retired records stay in
    lockstep on the underscore-suffix scheme."""

    DEST_LOOKUP_REPORT = "/Custom/Reports/Get_Transferred_Asset.xdo"

    @staticmethod
    def _get_asset_info_with_tag(value: str):
        return TestPostTransferAttributeUpdate._get_asset_info_with_tag(value)

    @staticmethod
    def _book_transfer_success_response(new_asset_id="555"):
        return TestPostTransferAttributeUpdate._book_transfer_success_response(
            new_asset_id=new_asset_id
        )

    def _build_sync(self, fusion, chain_rows, **extra_cfg):
        """Build a FusionIUSync wired with a BIP that routes by report_path.

        Discovery calls (default report) return the IU row only when
        scanning US CORP BOOK.  The lineage-lookup report returns
        ``chain_rows``.
        """
        bip = _mock_bip()

        def _run(params=None, report_path=None):
            if report_path == self.DEST_LOOKUP_REPORT:
                return chain_rows
            if (params or {}).get("P_BOOK_TYPE_CODE") == "US CORP BOOK":
                return [_bip_row(book="US CORP BOOK", dff_entity="UK Entity")]
            return []

        bip.run_report.side_effect = _run

        cfg = {
            "enabled": True,
            "dest_lookup_bip": {
                "report_path": self.DEST_LOOKUP_REPORT,
                "params": {"P_SOURCE_ASSET_ID": "${source_asset_id}"},
                "asset_id_column": "ASSET_ID",
                "asset_number_column": "ASSET_NUMBER",
                "book_type_code_column": "BOOK_TYPE_CODE",
                "is_destination_column": "IS_DESTINATION",
                "chain_order_column": "CHAIN_ORDER",
            },
        }
        cfg.update(extra_cfg)
        return FusionIUSync(
            fusion, _resolver(), bip, post_transfer_attribute_update=cfg
        )

    @staticmethod
    def _link(asset_id, *, book, chain_order, asset_number=None, is_dest="N"):
        return {
            "ASSET_ID": asset_id,
            "ASSET_NUMBER": asset_number or f"ASSET-{asset_id}",
            "BOOK_TYPE_CODE": book,
            "CHAIN_ORDER": chain_order,
            "IS_DESTINATION": is_dest,
        }

    def test_third_transfer_uses_chain_order_3_for_source_suffix(self):
        """Asset with 2 prior transfers (chain order 1, 2 ancestors) +
        the current source at chain_order=3 + new destination at 4.
        Source must get _3 suffix (matches its chain_order), NOT _1 (a
        naive bump of its currently-clean tag) or _4 (chain length).
        Destination must get the clean canonical tag."""
        fusion = _mock_fusion()
        # Chain reflects: A (orig), B, C (current source), D (new dest).
        # Source C currently has tag "ASSET-100" (clean, because the
        # previous transfer left it as the active asset with the clean
        # tag).  Bumping naively would produce _1 — wrong.  Chain order
        # gives the correct _3.
        chain = [
            self._link("11", book="US CORP BOOK", chain_order=1),
            self._link("22", book="UK CORP BOOK", chain_order=2),
            self._link("100", book="UK CORP BOOK", chain_order=3),  # source
            self._link("555", book="SG CORP BOOK", chain_order=4, is_dest="Y"),
        ]
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("ASSET-100"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),  # source update
            ({}, {"X_RETURN_STATUS": "S"}),  # destination update
        ]
        sync = self._build_sync(fusion, chain)
        sync._entity_resolver._map["UK ENTITY"] = "SG CORP BOOK"  # type: ignore[attr-defined]
        summary = sync.run_full_sync(
            books=["US CORP BOOK", "UK CORP BOOK", "SG CORP BOOK"], dry_run=False
        )

        # Only 2 update calls — ancestors 11 and 22 are NOT touched.
        update_calls = [
            c
            for c in fusion.process_transaction.call_args_list
            if c[0][0] == "updateAssetDescriptiveDetails"
        ]
        assert len(update_calls) == 2
        assert summary["results"][0]["status"] == "TRANSFERRED"

        _, src_params = update_calls[0][0], update_calls[0][0][1]
        _, dst_params = update_calls[1][0], update_calls[1][0][1]
        # Source (asset 100) at chain_order=3 → suffix _3.
        assert src_params["P_ASSET_ID"] == "100"
        assert src_params["P_TAG_NUMBER"] == "ASSET-100_3"
        # Destination (asset 555) gets the clean tag.
        assert dst_params["P_ASSET_ID"] == "555"
        assert dst_params["P_TAG_NUMBER"] == "ASSET-100"

    def test_destination_gets_dest_dffs_source_gets_source_dffs(self):
        """clear_dest_dffs / populate_dest_dffs apply only to the
        destination update; clear_source_dffs / populate_source_dffs
        apply only to the source update."""
        fusion = _mock_fusion()
        chain = [
            self._link("100", book="UK CORP BOOK", chain_order=1),
            self._link("555", book="SG CORP BOOK", chain_order=2, is_dest="Y"),
        ]
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("ASSET-100"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),
            ({}, {"X_RETURN_STATUS": "S"}),
        ]
        sync = self._build_sync(
            fusion,
            chain,
            clear_dest_dffs={"P_CAT_ATTRIBUTE9": ""},
            clear_source_dffs={"P_CAT_ATTRIBUTE8": ""},
        )
        sync._entity_resolver._map["UK ENTITY"] = "SG CORP BOOK"  # type: ignore[attr-defined]
        sync.run_full_sync(
            books=["US CORP BOOK", "UK CORP BOOK", "SG CORP BOOK"], dry_run=False
        )
        update_calls = [
            c
            for c in fusion.process_transaction.call_args_list
            if c[0][0] == "updateAssetDescriptiveDetails"
        ]
        by_id = {c[0][1]["P_ASSET_ID"]: c[0][1] for c in update_calls}
        # Source gets clear_source_dffs, NOT clear_dest_dffs.
        assert by_id["100"]["P_CAT_ATTRIBUTE8"] == ""
        assert "P_CAT_ATTRIBUTE9" not in by_id["100"]
        # Destination gets clear_dest_dffs, NOT clear_source_dffs.
        assert by_id["555"]["P_CAT_ATTRIBUTE9"] == ""
        assert "P_CAT_ATTRIBUTE8" not in by_id["555"]

    def test_also_bump_params_per_side_mirrors_correct_value(self):
        """With chain, also_bump_params still applies per-side: source
        gets the suffixed value, destination gets the clean canonical."""
        fusion = _mock_fusion()
        chain = [
            self._link("11", book="US CORP BOOK", chain_order=1),
            self._link("100", book="UK CORP BOOK", chain_order=2),  # source
            self._link("555", book="SG CORP BOOK", chain_order=3, is_dest="Y"),
        ]
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("HW-42"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),
            ({}, {"X_RETURN_STATUS": "S"}),
        ]
        sync = self._build_sync(fusion, chain, also_bump_params=["P_CAT_ATTRIBUTE7"])
        sync._entity_resolver._map["UK ENTITY"] = "SG CORP BOOK"  # type: ignore[attr-defined]
        sync.run_full_sync(
            books=["US CORP BOOK", "UK CORP BOOK", "SG CORP BOOK"], dry_run=False
        )

        update_calls = [
            c
            for c in fusion.process_transaction.call_args_list
            if c[0][0] == "updateAssetDescriptiveDetails"
        ]
        by_id = {c[0][1]["P_ASSET_ID"]: c[0][1] for c in update_calls}
        # Source at chain_order=2 → _2.  Both P_TAG_NUMBER and the
        # mirrored P_CAT_ATTRIBUTE7 carry it.
        assert by_id["100"]["P_TAG_NUMBER"] == "HW-42_2"
        assert by_id["100"]["P_CAT_ATTRIBUTE7"] == "HW-42_2"
        # Destination gets the clean value in both.
        assert by_id["555"]["P_TAG_NUMBER"] == "HW-42"
        assert by_id["555"]["P_CAT_ATTRIBUTE7"] == "HW-42"

    def test_chain_only_used_for_counting_ancestors_not_touched(self):
        """The chain BIP determines the suffix number, but only the
        source and destination get POSTed.  Ancestors keep whatever
        they already had — we never rewrite their tags."""
        fusion = _mock_fusion()
        chain = [
            self._link("11", book="US CORP BOOK", chain_order=1),
            self._link("22", book="UK CORP BOOK", chain_order=2),
            self._link("100", book="UK CORP BOOK", chain_order=3),
            self._link("555", book="SG CORP BOOK", chain_order=4, is_dest="Y"),
        ]
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("ASSET-100"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),
            ({}, {"X_RETURN_STATUS": "S"}),
        ]
        sync = self._build_sync(fusion, chain)
        sync._entity_resolver._map["UK ENTITY"] = "SG CORP BOOK"  # type: ignore[attr-defined]
        sync.run_full_sync(
            books=["US CORP BOOK", "UK CORP BOOK", "SG CORP BOOK"], dry_run=False
        )

        update_calls = [
            c
            for c in fusion.process_transaction.call_args_list
            if c[0][0] == "updateAssetDescriptiveDetails"
        ]
        touched_assets = {c[0][1]["P_ASSET_ID"] for c in update_calls}
        # Only source (100) and destination (555).  Ancestors 11 / 22
        # are NOT touched.
        assert touched_assets == {"100", "555"}

    def test_empty_chain_falls_back_to_string_bumping(self):
        """When the lineage BIP returns 0 rows (or isn't configured),
        we fall back to bump_underscore_suffix on the source's current
        tag.  Two POSTs still happen (source + dest)."""
        fusion = _mock_fusion()
        fusion.process_transaction.side_effect = [
            self._get_asset_info_with_tag("ASSET-100"),
            self._book_transfer_success_response(new_asset_id="555"),
            ({}, {"X_RETURN_STATUS": "S"}),
            ({}, {"X_RETURN_STATUS": "S"}),
        ]
        sync = self._build_sync(fusion, chain_rows=[])  # empty chain
        summary = sync.run_full_sync(
            books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False
        )

        update_calls = [
            c
            for c in fusion.process_transaction.call_args_list
            if c[0][0] == "updateAssetDescriptiveDetails"
        ]
        assert len(update_calls) == 2
        by_id = {c[0][1]["P_ASSET_ID"]: c[0][1] for c in update_calls}
        # Without chain, source's suffix comes from string-bump → _1.
        assert by_id["100"]["P_TAG_NUMBER"] == "ASSET-100_1"
        assert by_id["555"]["P_TAG_NUMBER"] == "ASSET-100"
        assert summary["results"][0]["status"] == "TRANSFERRED"


class TestTargetSegmentResolution:
    """BIP report supplies TARGET_SEG* columns; pipeline looks them up
    (and optionally mints via SOAP)."""

    def _row_with_target_segs(self, target_segs, target_ledger_name=""):
        row = _bip_row(
            book="US CORP BOOK",
            dff_entity="US Entity",
            final_target_book="US CORP BOOK",  # same-book
            current_location_id="300100111111",
            target_location_id="300100111111",
            target_ledger_name=target_ledger_name,
        )
        for k, v in target_segs.items():
            row[k] = v
        return row

    def _setup_pipeline(self, *, target_segs, ac_client=None, ledger_map=None):
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.side_effect = lambda params: (
            [self._row_with_target_segs(target_segs)]
            if params.get("P_BOOK_TYPE_CODE") == "US CORP BOOK"
            else []
        )
        sync = FusionIUSync(
            fusion,
            _resolver(),
            bip,
            account_combination_client=ac_client,
            ledger_name_by_book=ledger_map or {},
        )
        return fusion, sync

    def test_pending_transfer_captures_target_segments(self):
        target_segs = {
            "TARGET_SEG1": "SG01",
            "TARGET_SEG2": "100",
            "TARGET_SEG3": "7000",
        }
        fusion, sync = self._setup_pipeline(target_segs=target_segs)
        fusion.process_transaction.return_value = _get_asset_info_response()

        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)
        assert len(pending) == 1
        # Columns parsed into Segment1..3 dict
        assert pending[0].target_segments == {
            "Segment1": "SG01",
            "Segment2": "100",
            "Segment3": "7000",
        }

    def test_missing_target_segs_columns_yields_none(self):
        # No TARGET_SEG* columns AND no target_location/expense → row skipped.
        fusion, sync = self._setup_pipeline(target_segs={})
        fusion.process_transaction.return_value = _get_asset_info_response()
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)
        assert pending == []

    def test_partial_target_segs_columns_parsed(self):
        # Only Segment1 and Segment5 set; gaps allowed.
        target_segs = {"TARGET_SEG1": "SG01", "TARGET_SEG5": "1008"}
        fusion, sync = self._setup_pipeline(target_segs=target_segs)
        fusion.process_transaction.return_value = _get_asset_info_response()
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)
        assert pending[0].target_segments == {"Segment1": "SG01", "Segment5": "1008"}

    def test_target_ledger_name_column_captured(self):
        """TARGET_LEDGER_NAME from the report lands on PendingTransfer."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.side_effect = lambda params: (
            [
                self._row_with_target_segs(
                    {"TARGET_SEG1": "SG01"},
                    target_ledger_name="Singapore Primary Ledger",
                )
            ]
            if params.get("P_BOOK_TYPE_CODE") == "US CORP BOOK"
            else []
        )
        fusion.process_transaction.return_value = _get_asset_info_response()
        sync = FusionIUSync(fusion, _resolver(), bip)
        pending = sync.find_pending_transfers(books=["US CORP BOOK"], limit=10)
        assert len(pending) == 1
        assert pending[0].target_ledger_name == "Singapore Primary Ledger"

    def test_report_ledger_name_overrides_config_map(self):
        """The per-row TARGET_LEDGER_NAME column wins over the static
        ledger_name_by_book config map."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.side_effect = lambda params: (
            [
                self._row_with_target_segs(
                    {"TARGET_SEG1": "SG01", "TARGET_SEG2": "100"},
                    target_ledger_name="Report-Supplied Ledger",
                )
            ]
            if params.get("P_BOOK_TYPE_CODE") == "US CORP BOOK"
            else []
        )

        captured_ledgers = []

        class _FakeAC:
            def validate_and_create(self, *, ledger_name, segments, **kw):
                captured_ledgers.append(ledger_name)
                from ofam_asset_xfer.account_combination_client import (
                    CreatedCombination,
                )

                return CreatedCombination(ccid=555000, status="New")

        # discovery getAssetInformation, then transferAsset response
        fusion.process_transaction.side_effect = [
            _get_asset_info_response(),  # discovery
            (
                {},
                {"X_RETURN_STATUS": "S"},
            ),  # transferAsset
        ]
        sync = FusionIUSync(
            fusion,
            _resolver(),
            bip,
            account_combination_client=_FakeAC(),
            # Config map says something else — must be overridden.
            ledger_name_by_book={"US CORP BOOK": "Config-Map Ledger"},
            ac_skip_lov_lookup=True,  # straight to SOAP creator
        )
        sync.run_full_sync(books=["US CORP BOOK"], dry_run=False)
        # The SOAP creator was called with the REPORT's ledger, not the map's.
        assert captured_ledgers == ["Report-Supplied Ledger"]


class TestAppliesToFlag:
    """Per-book overrides scoped by applies_to: S (source), T (target,
    default), B (both).  Source overrides apply first, target overrides
    apply second so target wins when both supply the same key."""

    def _run_with_source(
        self, source_book: str, target_book: str, *, overrides_by_book: dict
    ):
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.side_effect = lambda params: (
            [_bip_row(book=source_book, dff_entity="Tgt Entity")]
            if params.get("P_BOOK_TYPE_CODE") == source_book
            else []
        )
        fusion.process_transaction.return_value = _get_asset_info_response(
            book=source_book
        )
        entity_map = {"Src Entity": source_book, "Tgt Entity": target_book}
        sync = FusionIUSync(
            fusion,
            EntityBookResolver(entity_map),
            bip,
            transfer_overrides_by_book=overrides_by_book,
        )
        summary = sync.run_full_sync(books=[source_book, target_book], dry_run=True)
        return summary["results"][0]["fusion_response"]["planned_params"]

    def test_default_is_target_only(self):
        """No applies_to flag → entry applies only when book is target.
        SG-as-source with no flag should NOT contribute its overrides."""
        params = self._run_with_source(
            source_book="SG CORP BOOK",
            target_book="US CORP BOOK",
            overrides_by_book={
                "SG CORP BOOK": {"conversion_rate_type": "Spot"},
            },
        )
        # SG is source; default applies_to=T means SG entry is skipped.
        assert params["P_CONVERSION_RATE_TYPE"] is None

    def test_source_flag_applies_only_when_book_is_source(self):
        params = self._run_with_source(
            source_book="SG CORP BOOK",
            target_book="US CORP BOOK",
            overrides_by_book={
                "SG CORP BOOK": {"applies_to": "S", "conversion_rate_type": "User"},
            },
        )
        assert params["P_CONVERSION_RATE_TYPE"] == "User"

    def test_source_flag_does_not_apply_when_book_is_target(self):
        params = self._run_with_source(
            source_book="US CORP BOOK",
            target_book="SG CORP BOOK",
            overrides_by_book={
                "SG CORP BOOK": {"applies_to": "S", "conversion_rate_type": "User"},
            },
        )
        # SG is target now; applies_to=S means SG entry is skipped.
        assert params["P_CONVERSION_RATE_TYPE"] is None

    def test_target_wins_when_both_supply_same_key(self):
        params = self._run_with_source(
            source_book="SG CORP BOOK",
            target_book="JP CORP BOOK",
            overrides_by_book={
                "SG CORP BOOK": {"applies_to": "S", "conversion_rate_type": "User"},
                "JP CORP BOOK": {
                    "applies_to": "T",
                    "conversion_rate_type": "Corporate",
                },
            },
        )
        # Target's value wins.
        assert params["P_CONVERSION_RATE_TYPE"] == "Corporate"

    def test_both_flag_applies_in_either_role(self):
        # Once as source.
        p_src = self._run_with_source(
            source_book="SG CORP BOOK",
            target_book="US CORP BOOK",
            overrides_by_book={
                "SG CORP BOOK": {"applies_to": "B", "conversion_rate_type": "Spot"},
            },
        )
        assert p_src["P_CONVERSION_RATE_TYPE"] == "Spot"
        # And once as target.
        p_tgt = self._run_with_source(
            source_book="US CORP BOOK",
            target_book="SG CORP BOOK",
            overrides_by_book={
                "SG CORP BOOK": {"applies_to": "B", "conversion_rate_type": "Spot"},
            },
        )
        assert p_tgt["P_CONVERSION_RATE_TYPE"] == "Spot"

    def test_invalid_applies_to_value_falls_back_to_target(self):
        params = self._run_with_source(
            source_book="US CORP BOOK",
            target_book="JP CORP BOOK",
            overrides_by_book={
                "JP CORP BOOK": {
                    "applies_to": "garbage",
                    "conversion_rate_type": "Corporate",
                },
            },
        )
        assert params["P_CONVERSION_RATE_TYPE"] == "Corporate"

    def test_applies_to_key_not_leaked_into_payload(self):
        params = self._run_with_source(
            source_book="US CORP BOOK",
            target_book="JP CORP BOOK",
            overrides_by_book={
                "JP CORP BOOK": {
                    "applies_to": "T",
                    "conversion_rate_type": "Corporate",
                },
            },
        )
        # ``applies_to`` is metadata, not a Fusion parameter.
        assert "P_APPLIES_TO" not in params
        assert "applies_to" not in params
