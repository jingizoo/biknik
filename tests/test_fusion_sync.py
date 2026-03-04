"""Unit tests for ofam_asset_xfer.fusion_sync (BIP + getAssetInformation discovery).

All tests use mock clients — no network calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from ofam_asset_xfer.fusion_sync import (
    FusionIUSync,
    DFFConfig,
    PendingTransfer,
)
from ofam_asset_xfer.entity_resolver import EntityBookResolver
from ofam_asset_xfer.fusion_ops import AssetState
from ofam_asset_xfer.exceptions import FusionApiError


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
    asset_number="142847",
    book="US CORP BOOK",
    dff_entity="UK Entity",
    dff_date="2026-03-01",
    dff_location="",
):
    """Build a BIP report row dict (as returned by BIPClient.run_report)."""
    return {
        "ASSET_NUMBER": asset_number,
        "BOOK_TYPE_CODE": book,
        "ATTRIBUTE9": dff_entity,
        "ATTRIBUTE_DATE1": dff_date,
        "ATTRIBUTE10": dff_location,
    }


def _get_asset_info_response(
    asset_id="100",
    asset_number="142847",
    book="US CORP BOOK",
    cost="36129.54",
):
    """Build a getAssetInformation processTransaction response."""
    return (
        {},  # raw
        {
            "X_RETURN_STATUS": "S",
            "X_ASSET_ID": asset_id,
            "X_ASSET_NUMBER": asset_number,
            "X_BOOK_TYPE_CODE": book,
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
        },
    )


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
        bip.run_report.return_value = [
            _bip_row(book="US CORP BOOK", dff_entity="UK Entity")
        ]
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
                "ASSET_NUM": "142847",
                "BOOK": "US CORP BOOK",
                "XFER_ENTITY": "UK Entity",
                "XFER_DATE": "2026-06-01",
                "XFER_LOC": "",
            }
        ]
        fusion.process_transaction.return_value = _get_asset_info_response()

        dff = DFFConfig(
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
        """bip_params should be forwarded to BIPClient.run_report."""
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = []

        sync = FusionIUSync(fusion, _resolver(), bip)
        sync.find_pending_transfers(
            books=["US CORP BOOK"],
            limit=10,
            bip_params={"P_BOOK_TYPE_CODE": "US CORP BOOK"},
        )

        bip.run_report.assert_called_once_with(
            params={"P_BOOK_TYPE_CODE": "US CORP BOOK"},
        )


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
        assert call_args[0][0] == "transferAsset"

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


# ===================================================================
# FusionIUSync.run_full_sync
# ===================================================================


class TestRunFullSync:
    def test_dry_run_summary(self):
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = [_bip_row(dff_entity="UK Entity")]
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

    def test_empty_report_returns_empty(self):
        fusion = _mock_fusion()
        bip = _mock_bip()
        bip.run_report.return_value = []

        sync = FusionIUSync(fusion, _resolver(), bip)
        summary = sync.run_full_sync(books=["US CORP BOOK"], dry_run=True)

        assert summary["counts"]["total"] == 0
        assert summary["results"] == []
