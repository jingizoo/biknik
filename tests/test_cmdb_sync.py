"""Unit tests for ofam_asset_xfer.cmdb_sync (CMDB→FA mismatch detection & transfer).

All tests use mock clients — no network calls.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch, call
from dataclasses import dataclass

from ofam_asset_xfer.cmdb_sync import (
    CMDBFASync,
    AssetMismatch,
    SyncResult,
    find_fa_asset_by_tag,
)
from ofam_asset_xfer.fusion_ops import AssetState
from ofam_asset_xfer.location_resolver import LocationResolver, FALocation
from ofam_asset_xfer.exceptions import FusionApiError, LocationResolutionError


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _mock_cmdb_client(assets=None):
    client = MagicMock()
    client.get_assets_with_location.return_value = assets or []
    return client


def _mock_fusion_client():
    client = MagicMock()
    return client


def _mock_resolver(locations=None, books=None):
    """Build a mock LocationResolver.

    Args:
        locations: Dict mapping location_code → FALocation
        books: Dict mapping location_code → book_type_code
    """
    resolver = MagicMock(spec=LocationResolver)

    locations = locations or {}
    books = books or {}

    def get_fa_location(code):
        if code in locations:
            return locations[code]
        raise LocationResolutionError(f"No FA location for {code}")

    def resolve_target_book(code):
        if code in books:
            return books[code]
        raise LocationResolutionError(f"No book for {code}")

    resolver.get_fa_location.side_effect = get_fa_location
    resolver.resolve_target_book.side_effect = resolve_target_book
    resolver.country_book_map = {"US": "US CORP BOOK", "UK": "UK CORP BOOK"}

    return resolver


def _sample_fa_state(asset_id="100", asset_number="10026", book="UK CORP BOOK"):
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
        cost="50000",
        description="Test Asset",
        tag_number="TAG-001",
    )


CMDB_ASSET_US = {
    "sys_id": "aaa",
    "asset_tag": "TAG-001",
    "display_name": "Dell Server",
    "u_cit_location_code": "US-NYC-01",
    "install_status": "1",
}

CMDB_ASSET_UK = {
    "sys_id": "bbb",
    "asset_tag": "TAG-002",
    "display_name": "HP Laptop",
    "u_cit_location_code": "UK-LON-01",
    "install_status": "1",
}

FA_LOC_US = FALocation(location_id=111, segment1="US-NYC-01", country="US", description="NYC DC")
FA_LOC_UK = FALocation(location_id=222, segment1="UK-LON-01", country="UK", description="London")


# ===================================================================
# find_fa_asset_by_tag
# ===================================================================

class TestFindFaAssetByTag:
    def test_found_via_rest_resource(self):
        client = _mock_fusion_client()
        client.get_resource.return_value = {
            "items": [{
                "AssetId": "100",
                "AssetNumber": "10026",
                "TagNumber": "TAG-001",
                "Description": "Dell Server",
                "assetBooks": [{"BookTypeCode": "UK CORP BOOK"}],
            }]
        }

        result = find_fa_asset_by_tag(client, "TAG-001")

        assert result is not None
        assert result["asset_id"] == "100"
        assert result["asset_number"] == "10026"
        assert result["book_type_code"] == "UK CORP BOOK"
        assert result["tag_number"] == "TAG-001"

    def test_not_found_returns_none(self):
        client = _mock_fusion_client()
        client.get_resource.return_value = {"items": []}

        result = find_fa_asset_by_tag(client, "NONEXISTENT")
        assert result is None

    def test_fallback_to_book_search(self):
        """When fixedAssets REST fails, iterate through search_books."""
        client = _mock_fusion_client()
        client.get_resource.side_effect = FusionApiError("404 Not Found")

        state = _sample_fa_state()
        client.process_transaction.return_value = (
            {},
            {
                "X_RETURN_STATUS": "S",
                "X_ASSET_ID": "100",
                "X_ASSET_NUMBER": "10026",
                "X_BOOK_TYPE_CODE": "UK CORP BOOK",
                "X_DISTRIBUTION_ID_TBL": "1001",
                "X_UNITS_ASSIGNED_TBL": "1",
                "X_ASSIGNED_TO_TBL": "",
                "X_EXPENSE_CCID_TBL": "626955",
                "X_LOCATION_CCID_TBL": "789012",
            },
        )

        result = find_fa_asset_by_tag(
            client, "TAG-001",
            search_books=["US CORP BOOK", "UK CORP BOOK"],
        )

        assert result is not None
        assert result["asset_number"] == "10026"

    def test_rest_no_books_child(self):
        """When assetBooks not in response, book_type_code should be empty."""
        client = _mock_fusion_client()
        client.get_resource.return_value = {
            "items": [{
                "AssetId": "100",
                "AssetNumber": "10026",
                "TagNumber": "TAG-001",
                "Description": "Test",
            }]
        }

        result = find_fa_asset_by_tag(client, "TAG-001")
        assert result is not None
        assert result["book_type_code"] == ""


# ===================================================================
# CMDBFASync.find_mismatches
# ===================================================================

class TestFindMismatches:
    def test_detects_cross_book_mismatch(self):
        """Asset in UK book but CMDB says US location → CROSS_BOOK mismatch."""
        cmdb = _mock_cmdb_client([CMDB_ASSET_US])
        fusion = _mock_fusion_client()

        # fixedAssets returns asset in UK book
        fusion.get_resource.return_value = {
            "items": [{
                "AssetId": "100",
                "AssetNumber": "10026",
                "TagNumber": "TAG-001",
                "Description": "Dell Server",
                "assetBooks": [{"BookTypeCode": "UK CORP BOOK"}],
            }]
        }

        # getAssetInformation for full state
        fusion.process_transaction.return_value = (
            {},
            {
                "X_RETURN_STATUS": "S",
                "X_ASSET_ID": "100",
                "X_ASSET_NUMBER": "10026",
                "X_BOOK_TYPE_CODE": "UK CORP BOOK",
                "X_DISTRIBUTION_ID_TBL": "1001",
                "X_UNITS_ASSIGNED_TBL": "1",
                "X_ASSIGNED_TO_TBL": "",
                "X_EXPENSE_CCID_TBL": "626955",
                "X_LOCATION_CCID_TBL": "789012",
            },
        )

        resolver = _mock_resolver(
            locations={"US-NYC-01": FA_LOC_US},
            books={"US-NYC-01": "US CORP BOOK"},
        )

        sync = CMDBFASync(cmdb, fusion, resolver)
        mismatches = sync.find_mismatches(limit=10)

        assert len(mismatches) == 1
        mm = mismatches[0]
        assert mm.cmdb_tag == "TAG-001"
        assert mm.cmdb_location_code == "US-NYC-01"
        assert mm.cmdb_location_country == "US"
        assert mm.fa_book_type_code == "UK CORP BOOK"
        assert mm.target_book_type_code == "US CORP BOOK"
        assert mm.mismatch_type == "CROSS_BOOK"

    def test_no_mismatch_when_books_match(self):
        """Asset in US book and CMDB says US location → no mismatch."""
        cmdb = _mock_cmdb_client([CMDB_ASSET_US])
        fusion = _mock_fusion_client()

        fusion.get_resource.return_value = {
            "items": [{
                "AssetId": "100",
                "AssetNumber": "10026",
                "TagNumber": "TAG-001",
                "Description": "Dell Server",
                "assetBooks": [{"BookTypeCode": "US CORP BOOK"}],
            }]
        }

        resolver = _mock_resolver(
            locations={"US-NYC-01": FA_LOC_US},
            books={"US-NYC-01": "US CORP BOOK"},
        )

        sync = CMDBFASync(cmdb, fusion, resolver)
        mismatches = sync.find_mismatches(limit=10)

        assert len(mismatches) == 0

    def test_skips_asset_not_in_fa(self):
        """CMDB asset with no FA match should be skipped."""
        cmdb = _mock_cmdb_client([CMDB_ASSET_US])
        fusion = _mock_fusion_client()
        # fixedAssets REST returns nothing
        fusion.get_resource.return_value = {"items": []}
        # Fallback book search also fails
        fusion.process_transaction.side_effect = FusionApiError("Asset not found")

        resolver = _mock_resolver(
            locations={"US-NYC-01": FA_LOC_US},
            books={"US-NYC-01": "US CORP BOOK"},
        )

        sync = CMDBFASync(cmdb, fusion, resolver)
        mismatches = sync.find_mismatches(limit=10)

        assert len(mismatches) == 0

    def test_skips_asset_with_no_tag(self):
        """CMDB asset without asset_tag should be skipped."""
        asset_no_tag = dict(CMDB_ASSET_US, asset_tag="")
        cmdb = _mock_cmdb_client([asset_no_tag])
        fusion = _mock_fusion_client()

        resolver = _mock_resolver(
            locations={"US-NYC-01": FA_LOC_US},
            books={"US-NYC-01": "US CORP BOOK"},
        )

        sync = CMDBFASync(cmdb, fusion, resolver)
        mismatches = sync.find_mismatches(limit=10)

        assert len(mismatches) == 0

    def test_respects_limit(self):
        """Should stop collecting after reaching limit."""
        assets = [
            {**CMDB_ASSET_US, "sys_id": f"id{i}", "asset_tag": f"TAG-{i:03d}"}
            for i in range(20)
        ]
        cmdb = _mock_cmdb_client(assets)
        fusion = _mock_fusion_client()

        # All assets in UK book → all mismatch with US location
        fusion.get_resource.return_value = {
            "items": [{
                "AssetId": "100",
                "AssetNumber": "10026",
                "TagNumber": "TAG-001",
                "assetBooks": [{"BookTypeCode": "UK CORP BOOK"}],
            }]
        }
        fusion.process_transaction.return_value = (
            {},
            {
                "X_RETURN_STATUS": "S",
                "X_ASSET_ID": "100",
                "X_ASSET_NUMBER": "10026",
                "X_BOOK_TYPE_CODE": "UK CORP BOOK",
                "X_DISTRIBUTION_ID_TBL": "1001",
                "X_UNITS_ASSIGNED_TBL": "1",
                "X_ASSIGNED_TO_TBL": "",
                "X_EXPENSE_CCID_TBL": "626955",
                "X_LOCATION_CCID_TBL": "789012",
            },
        )

        resolver = _mock_resolver(
            locations={"US-NYC-01": FA_LOC_US},
            books={"US-NYC-01": "US CORP BOOK"},
        )

        sync = CMDBFASync(cmdb, fusion, resolver)
        mismatches = sync.find_mismatches(limit=5)

        assert len(mismatches) == 5

    def test_skips_on_location_resolution_error(self):
        """Assets with unresolvable locations should be skipped, not crash."""
        cmdb = _mock_cmdb_client([CMDB_ASSET_US])
        fusion = _mock_fusion_client()

        resolver = _mock_resolver(locations={}, books={})  # No mappings

        sync = CMDBFASync(cmdb, fusion, resolver)
        mismatches = sync.find_mismatches(limit=10)

        assert len(mismatches) == 0


# ===================================================================
# CMDBFASync.execute_transfer
# ===================================================================

class TestExecuteTransfer:
    def _make_mismatch(self, state=None):
        return AssetMismatch(
            cmdb_tag="TAG-001",
            cmdb_sys_id="aaa",
            cmdb_display_name="Dell Server",
            cmdb_location_code="US-NYC-01",
            cmdb_location_country="US",
            fa_asset_id="100",
            fa_asset_number="10026",
            fa_book_type_code="UK CORP BOOK",
            fa_description="Dell Server",
            fa_tag_number="TAG-001",
            target_book_type_code="US CORP BOOK",
            mismatch_type="CROSS_BOOK",
            fa_state=state,
        )

    def test_dry_run_cross_book(self):
        cmdb = _mock_cmdb_client()
        fusion = _mock_fusion_client()
        resolver = _mock_resolver()

        state = _sample_fa_state()
        mismatch = self._make_mismatch(state=state)

        sync = CMDBFASync(cmdb, fusion, resolver)
        result = sync.execute_transfer(mismatch, dry_run=True)

        assert result.status == "DRY_RUN"
        assert result.mismatch_type == "CROSS_BOOK"
        assert result.source_book == "UK CORP BOOK"
        assert result.target_book == "US CORP BOOK"
        assert result.error is None
        # Should NOT have called process_transaction
        fusion.process_transaction.assert_not_called()

    def test_execute_cross_book_success(self):
        cmdb = _mock_cmdb_client()
        fusion = _mock_fusion_client()
        fusion.process_transaction.return_value = (
            {},
            {"X_RETURN_STATUS": "S", "X_EVENT_ID": "12345"},
        )
        resolver = _mock_resolver()

        state = _sample_fa_state()
        mismatch = self._make_mismatch(state=state)

        sync = CMDBFASync(cmdb, fusion, resolver)
        result = sync.execute_transfer(mismatch, dry_run=False)

        assert result.status == "TRANSFERRED"
        assert result.error is None
        fusion.process_transaction.assert_called_once()
        call_args = fusion.process_transaction.call_args
        assert call_args[0][0] == "transferAsset"

    def test_execute_cross_book_failure(self):
        cmdb = _mock_cmdb_client()
        fusion = _mock_fusion_client()
        fusion.process_transaction.return_value = (
            {},
            {"X_RETURN_STATUS": "F", "X_MSG_DATA": "Transfer failed"},
        )
        resolver = _mock_resolver()

        state = _sample_fa_state()
        mismatch = self._make_mismatch(state=state)

        sync = CMDBFASync(cmdb, fusion, resolver)
        result = sync.execute_transfer(mismatch, dry_run=False)

        assert result.status == "FAILED"
        assert "X_RETURN_STATUS=F" in result.error

    def test_fetches_state_if_missing(self):
        """When fa_state is None on the mismatch, it should fetch from FA."""
        cmdb = _mock_cmdb_client()
        fusion = _mock_fusion_client()

        # getAssetInformation call
        fusion.process_transaction.return_value = (
            {},
            {
                "X_RETURN_STATUS": "S",
                "X_ASSET_ID": "100",
                "X_ASSET_NUMBER": "10026",
                "X_BOOK_TYPE_CODE": "UK CORP BOOK",
                "X_DISTRIBUTION_ID_TBL": "1001",
                "X_UNITS_ASSIGNED_TBL": "1",
                "X_ASSIGNED_TO_TBL": "",
                "X_EXPENSE_CCID_TBL": "626955",
                "X_LOCATION_CCID_TBL": "789012",
                "X_CATEGORY_ID": "501",
                "X_DATE_PLACED_IN_SERVICE": "2023-01-15",
                "X_COST": "50000",
            },
        )
        resolver = _mock_resolver()

        mismatch = self._make_mismatch(state=None)  # No pre-fetched state

        sync = CMDBFASync(cmdb, fusion, resolver)
        result = sync.execute_transfer(mismatch, dry_run=True)

        assert result.status == "DRY_RUN"
        # Should have called getAssetInformation
        assert fusion.process_transaction.call_count == 1

    def test_failed_state_fetch_returns_failed(self):
        cmdb = _mock_cmdb_client()
        fusion = _mock_fusion_client()
        fusion.process_transaction.side_effect = FusionApiError("Connection timeout")
        resolver = _mock_resolver()

        mismatch = self._make_mismatch(state=None)

        sync = CMDBFASync(cmdb, fusion, resolver)
        result = sync.execute_transfer(mismatch, dry_run=False)

        assert result.status == "FAILED"
        assert "Connection timeout" in result.error


# ===================================================================
# CMDBFASync.run_full_sync
# ===================================================================

class TestRunFullSync:
    def test_dry_run_summary(self):
        cmdb = _mock_cmdb_client([CMDB_ASSET_US])
        fusion = _mock_fusion_client()

        fusion.get_resource.return_value = {
            "items": [{
                "AssetId": "100",
                "AssetNumber": "10026",
                "TagNumber": "TAG-001",
                "assetBooks": [{"BookTypeCode": "UK CORP BOOK"}],
            }]
        }
        fusion.process_transaction.return_value = (
            {},
            {
                "X_RETURN_STATUS": "S",
                "X_ASSET_ID": "100",
                "X_ASSET_NUMBER": "10026",
                "X_BOOK_TYPE_CODE": "UK CORP BOOK",
                "X_DISTRIBUTION_ID_TBL": "1001",
                "X_UNITS_ASSIGNED_TBL": "1",
                "X_ASSIGNED_TO_TBL": "",
                "X_EXPENSE_CCID_TBL": "626955",
                "X_LOCATION_CCID_TBL": "789012",
                "X_CATEGORY_ID": "501",
                "X_DATE_PLACED_IN_SERVICE": "2023-01-15",
                "X_COST": "50000",
            },
        )

        resolver = _mock_resolver(
            locations={"US-NYC-01": FA_LOC_US},
            books={"US-NYC-01": "US CORP BOOK"},
        )

        sync = CMDBFASync(cmdb, fusion, resolver)
        summary = sync.run_full_sync(dry_run=True)

        assert summary["dry_run"] is True
        assert summary["counts"]["total"] == 1
        assert summary["counts"]["dry_run"] == 1
        assert len(summary["results"]) == 1
        assert summary["results"][0]["status"] == "DRY_RUN"

    def test_empty_cmdb_returns_empty_summary(self):
        cmdb = _mock_cmdb_client([])
        fusion = _mock_fusion_client()
        resolver = _mock_resolver()

        sync = CMDBFASync(cmdb, fusion, resolver)
        summary = sync.run_full_sync(dry_run=True)

        assert summary["counts"]["total"] == 0
        assert summary["results"] == []
