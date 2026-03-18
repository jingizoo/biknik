"""End-to-end simulation test for OFAM Asset Transfer pipeline.

Simulates realistic transactions through the full pipeline using mock Oracle
Fusion and BIP clients.  Covers:

  1. Interunit transfers (cross-book: US→UK, US→JP, UK→US)
  2. Intraunit transfers (same-book location/expense changes)
  3. Multi-distribution assets
  4. Failure scenarios (API errors, missing entity, closed period)
  5. NOOP scenarios (no changes needed)
  6. Mixed batch with all transfer types
  7. Dry-run vs execute modes
  8. Cloud-friendly reporting output (NDJSON, summary, audit)

All tests use mocks — no network calls.  The simulation creates made-up
transactions that exercise every code path in the pipeline.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

from ofam_asset_xfer.fusion_sync import (
    DFFConfig,
    FusionIUSync,
    PendingTransfer,
    TransferClassification,
    TransferResult,
)
from ofam_asset_xfer.entity_resolver import EntityBookResolver
from ofam_asset_xfer.fusion_ops import AssetState
from ofam_asset_xfer.exceptions import FusionApiError
from ofam_asset_xfer.gcs_publisher import flatten_results, to_ndjson
from ofam_asset_xfer.local_publisher import LocalResultPublisher


# ============================================================================
# Simulated enterprise: entities, books, assets
# ============================================================================

ENTITY_BOOK_MAP = {
    "US Entity": "US CORP BOOK",
    "UK Entity": "UK CORP BOOK",
    "JP Entity": "JP CORP BOOK",
    "DE Entity": "DE CORP BOOK",
    "IN Entity": "IN CORP BOOK",
}

ALL_BOOKS = list(ENTITY_BOOK_MAP.values())


# ---------------------------------------------------------------------------
# Made-up transaction catalogue  (realistic asset data)
# ---------------------------------------------------------------------------

SIMULATED_ASSETS = {
    # ---- INTERUNIT: US → UK ----
    "ASSET_US_UK_001": {
        "asset_id": "300100501",
        "asset_number": "ASSET_US_UK_001",
        "book": "US CORP BOOK",
        "target_entity": "UK Entity",
        "target_book": "UK CORP BOOK",
        "transfer_date": "2026-03-15",
        "cost": "125000.00",
        "description": "Dell PowerEdge R750 Server",
        "tag_number": "SRV-US-2024-001",
        "distribution_ids": ["5001"],
        "units": ["1"],
        "expense_ccids": ["626955"],
        "location_ccids": ["789012"],
        "target_location_id": "300100888001",
        "target_expense_ccid": "627564",
        "category_id": "501",
        "dpis": "2024-06-15",
    },
    # ---- INTERUNIT: US → JP ----
    "ASSET_US_JP_002": {
        "asset_id": "300100502",
        "asset_number": "ASSET_US_JP_002",
        "book": "US CORP BOOK",
        "target_entity": "JP Entity",
        "target_book": "JP CORP BOOK",
        "transfer_date": "2026-03-18",
        "cost": "87500.50",
        "description": "Arista 7280R3 Network Switch",
        "tag_number": "NET-US-2024-042",
        "distribution_ids": ["5002"],
        "units": ["1"],
        "expense_ccids": ["626960"],
        "location_ccids": ["789020"],
        "target_location_id": "300100888002",
        "target_expense_ccid": "629100",
        "category_id": "502",
        "dpis": "2024-01-20",
    },
    # ---- INTERUNIT: UK → US ----
    "ASSET_UK_US_003": {
        "asset_id": "300100503",
        "asset_number": "ASSET_UK_US_003",
        "book": "UK CORP BOOK",
        "target_entity": "US Entity",
        "target_book": "US CORP BOOK",
        "transfer_date": "2026-03-20",
        "cost": "45000.00",
        "description": "Cisco Catalyst 9300 Switch",
        "tag_number": "NET-UK-2023-115",
        "distribution_ids": ["6001"],
        "units": ["1"],
        "expense_ccids": ["700100"],
        "location_ccids": ["800200"],
        "target_location_id": "300100888003",
        "target_expense_ccid": "626980",
        "category_id": "502",
        "dpis": "2023-09-01",
    },
    # ---- INTRAUNIT: US same-book location change ----
    "ASSET_US_LOC_004": {
        "asset_id": "300100504",
        "asset_number": "ASSET_US_LOC_004",
        "book": "US CORP BOOK",
        "target_entity": "US Entity",
        "target_book": "US CORP BOOK",
        "transfer_date": "2026-03-10",
        "cost": "32000.00",
        "description": "HP ProLiant DL380 Gen10",
        "tag_number": "SRV-US-2023-088",
        "distribution_ids": ["5010"],
        "units": ["1"],
        "expense_ccids": ["626955"],
        "location_ccids": ["789012"],
        "target_location_id": "300100999001",
        "target_expense_ccid": "",
        "category_id": "501",
        "dpis": "2023-03-15",
    },
    # ---- INTRAUNIT: US same-book expense change ----
    "ASSET_US_EXP_005": {
        "asset_id": "300100505",
        "asset_number": "ASSET_US_EXP_005",
        "book": "US CORP BOOK",
        "target_entity": "US Entity",
        "target_book": "US CORP BOOK",
        "transfer_date": "2026-03-12",
        "cost": "18500.00",
        "description": "Juniper EX4300 Switch",
        "tag_number": "NET-US-2024-067",
        "distribution_ids": ["5011"],
        "units": ["1"],
        "expense_ccids": ["626955"],
        "location_ccids": ["789012"],
        "target_location_id": "",
        "target_expense_ccid": "627100",
        "category_id": "502",
        "dpis": "2024-02-28",
    },
    # ---- NOOP: US same-book, no changes ----
    "ASSET_US_NOOP_006": {
        "asset_id": "300100506",
        "asset_number": "ASSET_US_NOOP_006",
        "book": "US CORP BOOK",
        "target_entity": "US Entity",
        "target_book": "US CORP BOOK",
        "transfer_date": "2026-03-05",
        "cost": "9800.00",
        "description": "Ubiquiti EdgeSwitch 48",
        "tag_number": "NET-US-2024-099",
        "distribution_ids": ["5012"],
        "units": ["1"],
        "expense_ccids": ["626955"],
        "location_ccids": ["789012"],
        "target_location_id": "789012",  # same as current → NOOP
        "target_expense_ccid": "",
        "category_id": "503",
        "dpis": "2024-04-01",
    },
    # ---- MULTI-DISTRIBUTION: US → DE (2 distributions) ----
    "ASSET_US_DE_007": {
        "asset_id": "300100507",
        "asset_number": "ASSET_US_DE_007",
        "book": "US CORP BOOK",
        "target_entity": "DE Entity",
        "target_book": "DE CORP BOOK",
        "transfer_date": "2026-03-22",
        "cost": "250000.00",
        "description": "NetApp AFF A400 Storage Array",
        "tag_number": "STG-US-2024-003",
        "distribution_ids": ["5020", "5021"],
        "units": ["0.6", "0.4"],
        "expense_ccids": ["626955", "626960"],
        "location_ccids": ["789012", "789015"],
        "target_location_id": "300100888007",
        "target_expense_ccid": "630200",
        "category_id": "504",
        "dpis": "2024-08-01",
    },
    # ---- FAILURE: Unknown entity ----
    "ASSET_FAIL_ENT_008": {
        "asset_id": "300100508",
        "asset_number": "ASSET_FAIL_ENT_008",
        "book": "US CORP BOOK",
        "target_entity": "MARS Entity",  # doesn't exist
        "target_book": "",
        "transfer_date": "2026-03-25",
        "cost": "5000.00",
        "description": "Raspberry Pi Cluster",
        "tag_number": "IOT-US-2024-001",
        "distribution_ids": ["5030"],
        "units": ["1"],
        "expense_ccids": ["626955"],
        "location_ccids": ["789012"],
        "target_location_id": "",
        "target_expense_ccid": "",
        "category_id": "505",
        "dpis": "2024-11-01",
    },
    # ---- INTERUNIT: US → IN ----
    "ASSET_US_IN_009": {
        "asset_id": "300100509",
        "asset_number": "ASSET_US_IN_009",
        "book": "US CORP BOOK",
        "target_entity": "IN Entity",
        "target_book": "IN CORP BOOK",
        "transfer_date": "2026-04-01",
        "cost": "175000.00",
        "description": "Palo Alto PA-5260 Firewall",
        "tag_number": "FW-US-2024-012",
        "distribution_ids": ["5040"],
        "units": ["1"],
        "expense_ccids": ["626970"],
        "location_ccids": ["789030"],
        "target_location_id": "300100888009",
        "target_expense_ccid": "631500",
        "category_id": "506",
        "dpis": "2024-07-15",
    },
    # ---- FAILURE: API error during transfer ----
    "ASSET_FAIL_API_010": {
        "asset_id": "300100510",
        "asset_number": "ASSET_FAIL_API_010",
        "book": "UK CORP BOOK",
        "target_entity": "JP Entity",
        "target_book": "JP CORP BOOK",
        "transfer_date": "2026-03-28",
        "cost": "62000.00",
        "description": "F5 BIG-IP i5800 Load Balancer",
        "tag_number": "LB-UK-2024-005",
        "distribution_ids": ["6010"],
        "units": ["1"],
        "expense_ccids": ["700110"],
        "location_ccids": ["800210"],
        "target_location_id": "300100888010",
        "target_expense_ccid": "629200",
        "category_id": "507",
        "dpis": "2024-05-20",
    },
}


# ============================================================================
# Mock factories
# ============================================================================


def _build_asset_state(asset_cfg: Dict[str, Any]) -> AssetState:
    """Build an AssetState from simulated asset config."""
    return AssetState(
        asset_id=asset_cfg["asset_id"],
        asset_number=asset_cfg["asset_number"],
        book_type_code=asset_cfg["book"],
        distribution_ids=asset_cfg["distribution_ids"],
        units_assigned=asset_cfg["units"],
        assigned_to=[""] * len(asset_cfg["distribution_ids"]),
        expense_ccids=asset_cfg["expense_ccids"],
        location_ccids=asset_cfg["location_ccids"],
        category_id=asset_cfg.get("category_id"),
        date_placed_in_service=asset_cfg.get("dpis"),
        cost=asset_cfg["cost"],
        description=asset_cfg["description"],
        tag_number=asset_cfg.get("tag_number"),
    )


def _build_get_asset_info_response(
    asset_cfg: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build a getAssetInformation response from simulated asset config."""
    pl = {
        "X_RETURN_STATUS": "S",
        "X_ASSET_ID": asset_cfg["asset_id"],
        "X_ASSET_NUMBER": asset_cfg["asset_number"],
        "X_BOOK_TYPE_CODE": asset_cfg["book"],
        "X_DISTRIBUTION_ID_TBL": ",".join(asset_cfg["distribution_ids"]),
        "X_UNITS_ASSIGNED_TBL": ",".join(asset_cfg["units"]),
        "X_ASSIGNED_TO_TBL": ",".join([""] * len(asset_cfg["distribution_ids"])),
        "X_EXPENSE_CCID_TBL": ",".join(asset_cfg["expense_ccids"]),
        "X_LOCATION_CCID_TBL": ",".join(asset_cfg["location_ccids"]),
        "X_CATEGORY_ID": asset_cfg.get("category_id", ""),
        "X_DATE_PLACED_IN_SERVICE": asset_cfg.get("dpis", ""),
        "X_COST": asset_cfg["cost"],
        "X_DESCRIPTION": asset_cfg["description"],
        "X_TAG_NUMBER": asset_cfg.get("tag_number", ""),
    }
    return ({}, pl)


def _build_bip_row(asset_cfg: Dict[str, Any]) -> Dict[str, str]:
    """Build a BIP report row from simulated asset config."""
    return {
        "ASSET_ID": asset_cfg["asset_id"],
        "ASSET_NUMBER": asset_cfg["asset_number"],
        "P_BOOK_TYPE_CODE": asset_cfg["book"],
        "TRANSFER_TO_ENTITY": asset_cfg["target_entity"],
        "TRANSFER_DATE": asset_cfg["transfer_date"],
        "TRANSFER_TO_LOCATION": "",
        "FINAL_TARGET_BOOK_TYPE_CODE": asset_cfg.get("target_book", ""),
        "TARGET_LOCATION_ID": asset_cfg.get("target_location_id", ""),
        "CURRENT_LOCATION_ID": asset_cfg["location_ccids"][0],
        "TARGET_EXPENSE_CCID": asset_cfg.get("target_expense_ccid", ""),
    }


# ============================================================================
# TransferClassification unit tests
# ============================================================================


class TestTransferClassification:
    """Test the interunit/intraunit classification logic."""

    def test_different_books_is_interunit(self):
        assert (
            TransferClassification.classify("US CORP BOOK", "UK CORP BOOK")
            == "INTERUNIT"
        )

    def test_same_book_is_intraunit(self):
        assert (
            TransferClassification.classify("US CORP BOOK", "US CORP BOOK")
            == "INTRAUNIT"
        )

    def test_case_insensitive(self):
        assert (
            TransferClassification.classify("us corp book", "US CORP BOOK")
            == "INTRAUNIT"
        )

    def test_with_whitespace(self):
        assert (
            TransferClassification.classify("  US CORP BOOK  ", "US CORP BOOK")
            == "INTRAUNIT"
        )

    def test_cross_region_always_interunit(self):
        for src, tgt in [
            ("US CORP BOOK", "JP CORP BOOK"),
            ("UK CORP BOOK", "DE CORP BOOK"),
            ("JP CORP BOOK", "IN CORP BOOK"),
            ("DE CORP BOOK", "US CORP BOOK"),
        ]:
            assert TransferClassification.classify(src, tgt) == "INTERUNIT"

    def test_pending_transfer_auto_classifies(self):
        """PendingTransfer should auto-classify via __post_init__."""
        pt = PendingTransfer(
            asset_id="100",
            asset_number="TEST",
            book_type_code="US CORP BOOK",
            description="Test",
            tag_number="T1",
            cost="1000",
            transfer_date="2026-03-01",
            transfer_to_entity="UK Entity",
            transfer_to_location=None,
            target_book_type_code="UK CORP BOOK",
        )
        assert pt.transfer_classification == "INTERUNIT"

    def test_pending_transfer_intraunit(self):
        pt = PendingTransfer(
            asset_id="100",
            asset_number="TEST",
            book_type_code="US CORP BOOK",
            description="Test",
            tag_number="T1",
            cost="1000",
            transfer_date="2026-03-01",
            transfer_to_entity="US Entity",
            transfer_to_location=None,
            target_book_type_code="US CORP BOOK",
            is_cross_book=False,
            target_location_id="999",
        )
        assert pt.transfer_classification == "INTRAUNIT"


# ============================================================================
# End-to-end simulation: full batch discovery + execution
# ============================================================================


class TestE2EInterunitTransfers:
    """Simulate interunit (cross-book) transfers end-to-end."""

    def _setup_sync(self, bip_rows, asset_responses, transfer_responses=None):
        """Wire up mocks for a full sync run."""
        fusion = MagicMock()
        bip = MagicMock()
        resolver = EntityBookResolver(ENTITY_BOOK_MAP)

        # BIP returns all rows for any book call
        def _bip_side_effect(params):
            book = params.get("P_BOOK_TYPE_CODE", "")
            return [r for r in bip_rows if r["P_BOOK_TYPE_CODE"] == book]

        bip.run_report.side_effect = _bip_side_effect

        # Stack responses: first N are getAssetInformation, then transfers
        all_responses = list(asset_responses)
        if transfer_responses:
            all_responses.extend(transfer_responses)
        fusion.process_transaction.side_effect = all_responses

        sync = FusionIUSync(fusion, resolver, bip)
        return sync, fusion, bip

    def test_us_to_uk_interunit(self):
        """US → UK cross-book transfer classified as INTERUNIT."""
        cfg = SIMULATED_ASSETS["ASSET_US_UK_001"]
        bip_rows = [_build_bip_row(cfg)]
        asset_resp = [_build_get_asset_info_response(cfg)]
        transfer_resp = [({}, {"X_RETURN_STATUS": "S", "X_EVENT_ID": "90001"})]

        sync, fusion, _ = self._setup_sync(bip_rows, asset_resp, transfer_resp)
        summary = sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)

        assert summary["counts"]["total"] == 1
        assert summary["counts"]["transferred"] == 1
        r = summary["results"][0]
        assert r["status"] == "TRANSFERRED"
        assert r["transfer_classification"] == "INTERUNIT"
        assert r["source_book"] == "US CORP BOOK"
        assert r["target_book"] == "UK CORP BOOK"

    def test_us_to_jp_interunit(self):
        """US → JP cross-book transfer classified as INTERUNIT."""
        cfg = SIMULATED_ASSETS["ASSET_US_JP_002"]
        bip_rows = [_build_bip_row(cfg)]
        asset_resp = [_build_get_asset_info_response(cfg)]
        transfer_resp = [({}, {"X_RETURN_STATUS": "S", "X_EVENT_ID": "90002"})]

        sync, _, _ = self._setup_sync(bip_rows, asset_resp, transfer_resp)
        summary = sync.run_full_sync(books=["US CORP BOOK", "JP CORP BOOK"], dry_run=False)

        r = summary["results"][0]
        assert r["transfer_classification"] == "INTERUNIT"
        assert r["target_book"] == "JP CORP BOOK"

    def test_uk_to_us_interunit(self):
        """UK → US cross-book transfer classified as INTERUNIT."""
        cfg = SIMULATED_ASSETS["ASSET_UK_US_003"]
        bip_rows = [_build_bip_row(cfg)]
        asset_resp = [_build_get_asset_info_response(cfg)]
        transfer_resp = [({}, {"X_RETURN_STATUS": "S", "X_EVENT_ID": "90003"})]

        sync, _, _ = self._setup_sync(bip_rows, asset_resp, transfer_resp)
        summary = sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False)

        r = summary["results"][0]
        assert r["transfer_classification"] == "INTERUNIT"
        assert r["source_book"] == "UK CORP BOOK"
        assert r["target_book"] == "US CORP BOOK"

    def test_us_to_in_interunit(self):
        """US → IN cross-book transfer (India book)."""
        cfg = SIMULATED_ASSETS["ASSET_US_IN_009"]
        bip_rows = [_build_bip_row(cfg)]
        asset_resp = [_build_get_asset_info_response(cfg)]
        transfer_resp = [({}, {"X_RETURN_STATUS": "S", "X_EVENT_ID": "90009"})]

        sync, _, _ = self._setup_sync(bip_rows, asset_resp, transfer_resp)
        summary = sync.run_full_sync(books=["US CORP BOOK", "IN CORP BOOK"], dry_run=False)

        r = summary["results"][0]
        assert r["transfer_classification"] == "INTERUNIT"
        assert r["target_book"] == "IN CORP BOOK"

    def test_multi_distribution_interunit(self):
        """Multi-distribution asset transfer US → DE."""
        cfg = SIMULATED_ASSETS["ASSET_US_DE_007"]
        bip_rows = [_build_bip_row(cfg)]
        asset_resp = [_build_get_asset_info_response(cfg)]
        transfer_resp = [({}, {"X_RETURN_STATUS": "S", "X_EVENT_ID": "90007"})]

        sync, fusion, _ = self._setup_sync(bip_rows, asset_resp, transfer_resp)
        summary = sync.run_full_sync(books=["US CORP BOOK", "DE CORP BOOK"], dry_run=False)

        r = summary["results"][0]
        assert r["transfer_classification"] == "INTERUNIT"
        # Verify dual-leg rosetta tables have 4 entries (2 dists × 2 legs)
        call_params = fusion.process_transaction.call_args_list[-1][0][1]
        assert len(call_params["P_TRANSACTION_UNITS_TBL"]) == 4

    def test_interunit_dry_run(self):
        """Dry-run should classify correctly without executing."""
        cfg = SIMULATED_ASSETS["ASSET_US_UK_001"]
        bip_rows = [_build_bip_row(cfg)]
        asset_resp = [_build_get_asset_info_response(cfg)]

        sync, fusion, _ = self._setup_sync(bip_rows, asset_resp)
        summary = sync.run_full_sync(books=["US CORP BOOK", "UK CORP BOOK"], dry_run=True)

        r = summary["results"][0]
        assert r["status"] == "DRY_RUN"
        assert r["transfer_classification"] == "INTERUNIT"
        # Only getAssetInformation should have been called (no transfer POST)
        assert fusion.process_transaction.call_count == 1


class TestE2EIntraunitTransfers:
    """Simulate intraunit (same-book) transfers end-to-end."""

    def _setup_sync(self, bip_rows, asset_responses, transfer_responses=None):
        fusion = MagicMock()
        bip = MagicMock()
        resolver = EntityBookResolver(ENTITY_BOOK_MAP)

        def _bip_side_effect(params):
            book = params.get("P_BOOK_TYPE_CODE", "")
            return [r for r in bip_rows if r["P_BOOK_TYPE_CODE"] == book]

        bip.run_report.side_effect = _bip_side_effect
        all_responses = list(asset_responses)
        if transfer_responses:
            all_responses.extend(transfer_responses)
        fusion.process_transaction.side_effect = all_responses
        sync = FusionIUSync(fusion, resolver, bip)
        return sync, fusion, bip

    def test_location_change_intraunit(self):
        """Same-book location change classified as INTRAUNIT."""
        cfg = SIMULATED_ASSETS["ASSET_US_LOC_004"]
        bip_rows = [_build_bip_row(cfg)]
        asset_resp = [_build_get_asset_info_response(cfg)]
        transfer_resp = [({}, {"X_RETURN_STATUS": "S", "X_EVENT_ID": "90004"})]

        sync, fusion, _ = self._setup_sync(bip_rows, asset_resp, transfer_resp)
        summary = sync.run_full_sync(books=["US CORP BOOK"], dry_run=False)

        r = summary["results"][0]
        assert r["status"] == "TRANSFERRED"
        assert r["transfer_classification"] == "INTRAUNIT"
        assert r["source_book"] == "US CORP BOOK"
        assert r["target_book"] == "US CORP BOOK"
        # Should have called transferAsset (not bookTransfer)
        call_handle = fusion.process_transaction.call_args_list[-1][0][0]
        assert call_handle == "transferAsset"

    def test_expense_change_intraunit(self):
        """Same-book expense CCID change classified as INTRAUNIT."""
        cfg = SIMULATED_ASSETS["ASSET_US_EXP_005"]
        bip_rows = [_build_bip_row(cfg)]
        asset_resp = [_build_get_asset_info_response(cfg)]
        transfer_resp = [({}, {"X_RETURN_STATUS": "S", "X_EVENT_ID": "90005"})]

        sync, fusion, _ = self._setup_sync(bip_rows, asset_resp, transfer_resp)
        summary = sync.run_full_sync(books=["US CORP BOOK"], dry_run=False)

        r = summary["results"][0]
        assert r["status"] == "TRANSFERRED"
        assert r["transfer_classification"] == "INTRAUNIT"

    def test_noop_when_no_changes(self):
        """Same-book, location already matches → NOOP."""
        cfg = SIMULATED_ASSETS["ASSET_US_NOOP_006"]
        bip_rows = [_build_bip_row(cfg)]
        # CURRENT_LOCATION_ID == TARGET_LOCATION_ID → skipped in discovery
        sync, fusion, _ = self._setup_sync(bip_rows, [])
        summary = sync.run_full_sync(books=["US CORP BOOK"], dry_run=False)

        # Should be filtered out during discovery
        assert summary["counts"]["total"] == 0


class TestE2EFailureScenarios:
    """Simulate failure scenarios end-to-end."""

    def _setup_sync(self, bip_rows, asset_responses, transfer_responses=None):
        fusion = MagicMock()
        bip = MagicMock()
        resolver = EntityBookResolver(ENTITY_BOOK_MAP)

        def _bip_side_effect(params):
            book = params.get("P_BOOK_TYPE_CODE", "")
            return [r for r in bip_rows if r["P_BOOK_TYPE_CODE"] == book]

        bip.run_report.side_effect = _bip_side_effect
        all_responses = list(asset_responses)
        if transfer_responses:
            all_responses.extend(transfer_responses)
        fusion.process_transaction.side_effect = all_responses
        sync = FusionIUSync(fusion, resolver, bip)
        return sync, fusion, bip

    def test_unknown_entity_filtered_out(self):
        """Asset with unknown entity is skipped during discovery."""
        cfg = SIMULATED_ASSETS["ASSET_FAIL_ENT_008"]
        bip_rows = [_build_bip_row(cfg)]

        sync, fusion, _ = self._setup_sync(bip_rows, [])
        summary = sync.run_full_sync(books=["US CORP BOOK"], dry_run=False)

        # Unknown entity → filtered at discovery, not a transfer failure
        assert summary["counts"]["total"] == 0

    def test_api_failure_returns_failed(self):
        """Fusion API failure during transfer → FAILED status."""
        cfg = SIMULATED_ASSETS["ASSET_FAIL_API_010"]
        bip_rows = [_build_bip_row(cfg)]
        asset_resp = [_build_get_asset_info_response(cfg)]
        # Transfer returns failure
        transfer_resp = [
            ({}, {"X_RETURN_STATUS": "F", "X_MSG_DATA": "Period is closed"})
        ]

        sync, _, _ = self._setup_sync(bip_rows, asset_resp, transfer_resp)
        summary = sync.run_full_sync(
            books=["UK CORP BOOK", "JP CORP BOOK"], dry_run=False
        )

        assert summary["counts"]["total"] == 1
        assert summary["counts"]["failed"] == 1
        r = summary["results"][0]
        assert r["status"] == "FAILED"
        assert r["transfer_classification"] == "INTERUNIT"
        assert "X_RETURN_STATUS=F" in r["error"]

    def test_get_asset_info_failure_skips_asset(self):
        """getAssetInformation timeout → asset skipped in discovery."""
        cfg = SIMULATED_ASSETS["ASSET_US_UK_001"]
        bip_rows = [_build_bip_row(cfg)]

        fusion = MagicMock()
        bip = MagicMock()
        resolver = EntityBookResolver(ENTITY_BOOK_MAP)
        bip.run_report.side_effect = lambda params: (
            bip_rows if params.get("P_BOOK_TYPE_CODE") == "US CORP BOOK" else []
        )
        fusion.process_transaction.side_effect = FusionApiError("Connection timeout")

        sync = FusionIUSync(fusion, resolver, bip)
        summary = sync.run_full_sync(books=["US CORP BOOK"], dry_run=False)

        assert summary["counts"]["total"] == 0

    def test_bip_report_failure_returns_empty(self):
        """BIP report failure → no transfers discovered."""
        fusion = MagicMock()
        bip = MagicMock()
        resolver = EntityBookResolver(ENTITY_BOOK_MAP)
        bip.run_report.side_effect = FusionApiError("SOAP fault")

        sync = FusionIUSync(fusion, resolver, bip)
        summary = sync.run_full_sync(books=["US CORP BOOK"], dry_run=False)

        assert summary["counts"]["total"] == 0


class TestE2EMixedBatch:
    """Simulate a realistic mixed batch with all transfer types."""

    def test_mixed_batch_all_scenarios(self):
        """Run a batch with interunit, intraunit, and failure scenarios."""
        # Pick a mix of assets
        selected = [
            "ASSET_US_UK_001",    # INTERUNIT: US → UK
            "ASSET_US_LOC_004",   # INTRAUNIT: US same-book location change
            "ASSET_US_EXP_005",   # INTRAUNIT: US same-book expense change
            "ASSET_UK_US_003",    # INTERUNIT: UK → US
        ]

        bip_rows = []
        asset_resps = []
        transfer_resps = []

        for name in selected:
            cfg = SIMULATED_ASSETS[name]
            bip_rows.append(_build_bip_row(cfg))
            asset_resps.append(_build_get_asset_info_response(cfg))
            transfer_resps.append(
                ({}, {"X_RETURN_STATUS": "S", "X_EVENT_ID": f"E_{name}"})
            )

        fusion = MagicMock()
        bip = MagicMock()
        resolver = EntityBookResolver(ENTITY_BOOK_MAP)

        def _bip_side_effect(params):
            book = params.get("P_BOOK_TYPE_CODE", "")
            return [r for r in bip_rows if r["P_BOOK_TYPE_CODE"] == book]

        bip.run_report.side_effect = _bip_side_effect
        fusion.process_transaction.side_effect = asset_resps + transfer_resps
        sync = FusionIUSync(fusion, resolver, bip)

        summary = sync.run_full_sync(
            books=["US CORP BOOK", "UK CORP BOOK"], dry_run=False
        )

        assert summary["counts"]["total"] == 4
        assert summary["counts"]["transferred"] == 4

        # Verify classification breakdown
        classifications = [r["transfer_classification"] for r in summary["results"]]
        assert classifications.count("INTERUNIT") == 2
        assert classifications.count("INTRAUNIT") == 2

    def test_mixed_batch_with_failures(self):
        """Batch where some succeed and some fail — error doesn't stop batch."""
        # US → UK (success) + UK → JP (failure)
        cfg_ok = SIMULATED_ASSETS["ASSET_US_UK_001"]
        cfg_fail = SIMULATED_ASSETS["ASSET_FAIL_API_010"]

        bip_rows = [_build_bip_row(cfg_ok), _build_bip_row(cfg_fail)]

        fusion = MagicMock()
        bip = MagicMock()
        resolver = EntityBookResolver(ENTITY_BOOK_MAP)

        def _bip_side_effect(params):
            book = params.get("P_BOOK_TYPE_CODE", "")
            return [r for r in bip_rows if r["P_BOOK_TYPE_CODE"] == book]

        bip.run_report.side_effect = _bip_side_effect

        # getAssetInfo for both, then transfer success for first, failure for second
        fusion.process_transaction.side_effect = [
            _build_get_asset_info_response(cfg_ok),
            _build_get_asset_info_response(cfg_fail),
            ({}, {"X_RETURN_STATUS": "S", "X_EVENT_ID": "90001"}),
            ({}, {"X_RETURN_STATUS": "F", "X_MSG_DATA": "Period closed"}),
        ]

        sync = FusionIUSync(fusion, resolver, bip)
        summary = sync.run_full_sync(
            books=["US CORP BOOK", "UK CORP BOOK", "JP CORP BOOK"], dry_run=False
        )

        assert summary["counts"]["total"] == 2
        assert summary["counts"]["transferred"] == 1
        assert summary["counts"]["failed"] == 1

        # Both should have classification
        for r in summary["results"]:
            assert r["transfer_classification"] == "INTERUNIT"


# ============================================================================
# Reporting: cloud-friendly NDJSON output
# ============================================================================


class TestE2EReporting:
    """Test that the reporting output is cloud-friendly and super readable."""

    def _make_summary_with_results(self):
        """Build a realistic summary with mixed results for reporting tests."""
        return {
            "started_ts": 1710720000,
            "finished_ts": 1710720045,
            "duration_seconds": 45,
            "dry_run": False,
            "books": ["US CORP BOOK", "UK CORP BOOK", "JP CORP BOOK"],
            "counts": {
                "total": 6,
                "transferred": 3,
                "failed": 1,
                "dry_run": 0,
            },
            "results": [
                {
                    "asset_number": "ASSET_US_UK_001",
                    "status": "TRANSFERRED",
                    "source_book": "US CORP BOOK",
                    "target_book": "UK CORP BOOK",
                    "transfer_to_entity": "UK Entity",
                    "transfer_date": "2026-03-15",
                    "transfer_classification": "INTERUNIT",
                    "error": None,
                    "fusion_response": {"X_RETURN_STATUS": "S"},
                },
                {
                    "asset_number": "ASSET_US_LOC_004",
                    "status": "TRANSFERRED",
                    "source_book": "US CORP BOOK",
                    "target_book": "US CORP BOOK",
                    "transfer_to_entity": "US Entity",
                    "transfer_date": "2026-03-10",
                    "transfer_classification": "INTRAUNIT",
                    "error": None,
                    "fusion_response": {"X_RETURN_STATUS": "S"},
                },
                {
                    "asset_number": "ASSET_UK_US_003",
                    "status": "TRANSFERRED",
                    "source_book": "UK CORP BOOK",
                    "target_book": "US CORP BOOK",
                    "transfer_to_entity": "US Entity",
                    "transfer_date": "2026-03-20",
                    "transfer_classification": "INTERUNIT",
                    "error": None,
                    "fusion_response": {"X_RETURN_STATUS": "S"},
                },
                {
                    "asset_number": "ASSET_FAIL_API_010",
                    "status": "FAILED",
                    "source_book": "UK CORP BOOK",
                    "target_book": "JP CORP BOOK",
                    "transfer_to_entity": "JP Entity",
                    "transfer_date": "2026-03-28",
                    "transfer_classification": "INTERUNIT",
                    "error": "Fusion X_RETURN_STATUS=F",
                    "fusion_response": {"X_RETURN_STATUS": "F"},
                },
                {
                    "asset_number": "ASSET_US_EXP_005",
                    "status": "TRANSFERRED",
                    "source_book": "US CORP BOOK",
                    "target_book": "US CORP BOOK",
                    "transfer_to_entity": "US Entity",
                    "transfer_date": "2026-03-12",
                    "transfer_classification": "INTRAUNIT",
                    "error": None,
                    "fusion_response": {"X_RETURN_STATUS": "S"},
                },
                {
                    "asset_number": "ASSET_US_NOOP_006",
                    "status": "NOOP",
                    "source_book": "US CORP BOOK",
                    "target_book": "US CORP BOOK",
                    "transfer_to_entity": "US Entity",
                    "transfer_date": "2026-03-05",
                    "transfer_classification": "INTRAUNIT",
                    "error": None,
                    "fusion_response": None,
                },
            ],
        }

    def test_flatten_includes_classification(self):
        """Flattened NDJSON rows should include transfer_classification."""
        summary = self._make_summary_with_results()
        flat = flatten_results(
            summary["results"], "2026-03-18", 1710720000, False
        )

        assert len(flat) == 6
        classifications = [r["transfer_classification"] for r in flat]
        assert "INTERUNIT" in classifications
        assert "INTRAUNIT" in classifications
        # Every row should have the field
        for r in flat:
            assert "transfer_classification" in r

    def test_ndjson_format_valid(self):
        """NDJSON output should be valid (one JSON object per line)."""
        summary = self._make_summary_with_results()
        flat = flatten_results(
            summary["results"], "2026-03-18", 1710720000, False
        )
        ndjson = to_ndjson(flat)

        lines = ndjson.strip().split("\n")
        assert len(lines) == 6
        for line in lines:
            parsed = json.loads(line)
            assert "asset_number" in parsed
            assert "transfer_classification" in parsed

    def test_ndjson_classification_filtering(self):
        """Downstream consumers can filter NDJSON by classification."""
        summary = self._make_summary_with_results()
        flat = flatten_results(
            summary["results"], "2026-03-18", 1710720000, False
        )
        ndjson = to_ndjson(flat)

        lines = ndjson.strip().split("\n")
        interunit = [json.loads(l) for l in lines if "INTERUNIT" in l]
        intraunit = [json.loads(l) for l in lines if "INTRAUNIT" in l]

        assert len(interunit) == 3  # 2 transferred + 1 failed
        assert len(intraunit) == 3  # 2 transferred + 1 NOOP

    def test_local_publisher_creates_ndjson_with_classification(self):
        """LocalResultPublisher writes NDJSON with classification field."""
        tmpdir = tempfile.mkdtemp(prefix="ofam_e2e_")
        try:
            summary = self._make_summary_with_results()
            pub = LocalResultPublisher(tmpdir, retention_days=365)
            pub.publish(summary, summary["results"])

            today = date.today().isoformat()
            results_path = Path(tmpdir) / today / "results.ndjson"
            assert results_path.exists()

            lines = results_path.read_text().strip().split("\n")
            assert len(lines) == 6
            for line in lines:
                row = json.loads(line)
                assert "transfer_classification" in row

            # Errors file should exist (we have a FAILED entry)
            errors_path = Path(tmpdir) / today / "errors.ndjson"
            assert errors_path.exists()
            error_lines = errors_path.read_text().strip().split("\n")
            assert len(error_lines) == 1
            err = json.loads(error_lines[0])
            assert err["status"] == "FAILED"
            assert err["transfer_classification"] == "INTERUNIT"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_reporting_metadata_stamps(self):
        """Every NDJSON row should have run_date, run_ts, dry_run metadata."""
        summary = self._make_summary_with_results()
        flat = flatten_results(
            summary["results"], "2026-03-18", 1710720000, False
        )

        for r in flat:
            assert r["run_date"] == "2026-03-18"
            assert r["run_ts"] == 1710720000
            assert r["dry_run"] is False


# ============================================================================
# Comprehensive end-to-end: discovery → execute → report → verify
# ============================================================================


class TestE2EFullPipeline:
    """Full pipeline simulation: BIP discovery → transfer → local reporting."""

    def test_full_pipeline_with_reporting(self):
        """Simulate complete pipeline: discover, transfer, write report, verify."""
        tmpdir = tempfile.mkdtemp(prefix="ofam_e2e_full_")
        try:
            # Setup: 3 interunit + 2 intraunit transfers
            selected = [
                "ASSET_US_UK_001",   # INTERUNIT
                "ASSET_US_JP_002",   # INTERUNIT
                "ASSET_US_LOC_004",  # INTRAUNIT
                "ASSET_US_EXP_005",  # INTRAUNIT
                "ASSET_US_IN_009",   # INTERUNIT
            ]

            bip_rows = [_build_bip_row(SIMULATED_ASSETS[n]) for n in selected]
            asset_resps = [
                _build_get_asset_info_response(SIMULATED_ASSETS[n]) for n in selected
            ]
            transfer_resps = [
                ({}, {"X_RETURN_STATUS": "S", "X_EVENT_ID": f"E_{i}"})
                for i in range(len(selected))
            ]

            fusion = MagicMock()
            bip = MagicMock()
            resolver = EntityBookResolver(ENTITY_BOOK_MAP)

            def _bip_side_effect(params):
                book = params.get("P_BOOK_TYPE_CODE", "")
                return [r for r in bip_rows if r["P_BOOK_TYPE_CODE"] == book]

            bip.run_report.side_effect = _bip_side_effect
            fusion.process_transaction.side_effect = asset_resps + transfer_resps

            sync = FusionIUSync(fusion, resolver, bip)

            # Run the sync
            summary = sync.run_full_sync(
                books=["US CORP BOOK", "UK CORP BOOK", "JP CORP BOOK", "IN CORP BOOK"],
                dry_run=False,
            )

            # Verify summary counts
            assert summary["counts"]["total"] == 5
            assert summary["counts"]["transferred"] == 5
            assert summary["counts"]["failed"] == 0

            # Verify classification breakdown
            results = summary["results"]
            interunit_count = sum(
                1 for r in results if r["transfer_classification"] == "INTERUNIT"
            )
            intraunit_count = sum(
                1 for r in results if r["transfer_classification"] == "INTRAUNIT"
            )
            assert interunit_count == 3
            assert intraunit_count == 2

            # Publish to local disk and verify
            pub = LocalResultPublisher(tmpdir, retention_days=365)
            pub.publish(summary, results)

            today = date.today().isoformat()
            results_path = Path(tmpdir) / today / "results.ndjson"
            assert results_path.exists()

            lines = results_path.read_text().strip().split("\n")
            assert len(lines) == 5

            # Parse and verify each row
            for line in lines:
                row = json.loads(line)
                assert row["run_date"] == today
                assert row["dry_run"] is False
                assert row["status"] == "TRANSFERRED"
                assert row["transfer_classification"] in ("INTERUNIT", "INTRAUNIT")

            # No errors file (all succeeded)
            errors_path = Path(tmpdir) / today / "errors.ndjson"
            assert not errors_path.exists()

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_full_pipeline_dry_run_with_reporting(self):
        """Dry-run pipeline: discover, classify, write report — no Oracle POST."""
        tmpdir = tempfile.mkdtemp(prefix="ofam_e2e_dry_")
        try:
            selected = ["ASSET_US_UK_001", "ASSET_US_LOC_004"]
            bip_rows = [_build_bip_row(SIMULATED_ASSETS[n]) for n in selected]
            asset_resps = [
                _build_get_asset_info_response(SIMULATED_ASSETS[n]) for n in selected
            ]

            fusion = MagicMock()
            bip = MagicMock()
            resolver = EntityBookResolver(ENTITY_BOOK_MAP)

            def _bip_side_effect(params):
                book = params.get("P_BOOK_TYPE_CODE", "")
                return [r for r in bip_rows if r["P_BOOK_TYPE_CODE"] == book]

            bip.run_report.side_effect = _bip_side_effect
            fusion.process_transaction.side_effect = asset_resps

            sync = FusionIUSync(fusion, resolver, bip)
            summary = sync.run_full_sync(
                books=["US CORP BOOK", "UK CORP BOOK"], dry_run=True
            )

            assert summary["dry_run"] is True
            assert summary["counts"]["dry_run"] == 2
            for r in summary["results"]:
                assert r["status"] == "DRY_RUN"
                assert r["transfer_classification"] in ("INTERUNIT", "INTRAUNIT")

            # Publish and verify dry_run flag in NDJSON
            pub = LocalResultPublisher(tmpdir)
            pub.publish(summary, summary["results"])

            today = date.today().isoformat()
            results_path = Path(tmpdir) / today / "results.ndjson"
            for line in results_path.read_text().strip().split("\n"):
                row = json.loads(line)
                assert row["dry_run"] is True

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
