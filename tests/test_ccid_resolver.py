"""Unit tests for ofam_asset_xfer.ccid_resolver (Option B CCID resolution).

All tests use a mock REST client — no network calls.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from ofam_asset_xfer.ccid_resolver import (
    get_ccid_details,
    build_target_segments,
    lookup_ccid_by_segments,
    resolve_target_expense_ccid,
)
from ofam_asset_xfer.exceptions import FusionApiError, ValidationError


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _mock_client(get_resource_side_effect=None, get_resource_return=None):
    client = MagicMock()
    if get_resource_side_effect is not None:
        client.get_resource.side_effect = get_resource_side_effect
    elif get_resource_return is not None:
        client.get_resource.return_value = get_resource_return
    return client


SAMPLE_LOV_ROW = {
    "CodeCombinationId": 626955,
    "ChartOfAccountsId": 50388,
    "ConcatenatedSegments": "101-100-7710-0000-000-000-000",
    "Segment1": "101",
    "Segment2": "100",
    "Segment3": "7710",
    "Segment4": "0000",
    "Segment5": "000",
    "Segment6": "000",
    "Segment7": "000",
}

SAMPLE_TARGET_ROW = {
    "CodeCombinationId": 789012,
    "ConcatenatedSegments": "US01-100-7710-0000-000-000-000",
    "Segment1": "US01",
    "Segment2": "100",
    "Segment3": "7710",
    "Segment4": "0000",
    "Segment5": "000",
    "Segment6": "000",
    "Segment7": "000",
}


# ===================================================================
# get_ccid_details
# ===================================================================

class TestGetCcidDetails:
    def test_success(self):
        client = _mock_client(get_resource_return={"items": [SAMPLE_LOV_ROW]})

        result = get_ccid_details(client, 626955)

        assert result["ccid"] == 626955
        assert result["chart_of_accounts_id"] == 50388
        assert result["concatenated_segments"] == "101-100-7710-0000-000-000-000"
        assert result["segments"]["Segment1"] == "101"
        assert result["segments"]["Segment3"] == "7710"
        assert len(result["segments"]) == 7

        # Verify correct REST call
        client.get_resource.assert_called_once_with(
            "accountCombinationsLOV",
            {"finder": "PrimaryKey;_CODE_COMBINATION_ID=626955", "onlyData": "true"},
        )

    def test_no_items_raises(self):
        client = _mock_client(get_resource_return={"items": []})

        with pytest.raises(FusionApiError, match="no items"):
            get_ccid_details(client, 999999)

    def test_no_segment_fields_raises(self):
        row = {"CodeCombinationId": 1, "SomeOtherField": "value"}
        client = _mock_client(get_resource_return={"items": [row]})

        with pytest.raises(FusionApiError, match="No Segment"):
            get_ccid_details(client, 1)

    def test_coa_id_none_when_missing(self):
        row = {"CodeCombinationId": 1, "Segment1": "100"}
        client = _mock_client(get_resource_return={"items": [row]})

        result = get_ccid_details(client, 1)
        assert result["chart_of_accounts_id"] is None

    def test_camel_case_coa_id(self):
        """Oracle REST may return either ChartOfAccountsId or chartOfAccountsId."""
        row = {"CodeCombinationId": 1, "chartOfAccountsId": 99, "Segment1": "X"}
        client = _mock_client(get_resource_return={"items": [row]})

        result = get_ccid_details(client, 1)
        assert result["chart_of_accounts_id"] == 99


# ===================================================================
# build_target_segments
# ===================================================================

class TestBuildTargetSegments:
    def test_replaces_company_segment(self):
        src = {"Segment1": "101", "Segment2": "100", "Segment3": "7710"}
        target = build_target_segments(src, "US01", "Segment1")

        assert target["Segment1"] == "US01"
        assert target["Segment2"] == "100"  # unchanged
        assert target["Segment3"] == "7710"  # unchanged

    def test_does_not_mutate_source(self):
        src = {"Segment1": "101", "Segment2": "100"}
        build_target_segments(src, "US01", "Segment1")

        assert src["Segment1"] == "101"  # original unchanged

    def test_custom_segment_key(self):
        src = {"Segment1": "101", "Segment2": "100", "Segment3": "7710"}
        target = build_target_segments(src, "NEW_CO", "Segment3")

        assert target["Segment3"] == "NEW_CO"
        assert target["Segment1"] == "101"  # unchanged

    def test_missing_segment_key_raises(self):
        src = {"Segment1": "101", "Segment2": "100"}

        with pytest.raises(ValidationError, match="not found"):
            build_target_segments(src, "US01", "Segment99")


# ===================================================================
# lookup_ccid_by_segments
# ===================================================================

class TestLookupCcidBySegments:
    def test_exact_match(self):
        client = _mock_client(get_resource_return={"items": [SAMPLE_TARGET_ROW]})

        target_segments = {
            "Segment1": "US01", "Segment2": "100", "Segment3": "7710",
            "Segment4": "0000", "Segment5": "000", "Segment6": "000", "Segment7": "000",
        }
        result = lookup_ccid_by_segments(client, target_segments, chart_of_accounts_id=50388)

        assert result == 789012

        # Verify q filter was built correctly
        call_args = client.get_resource.call_args
        q_param = call_args[0][1]["q"] if len(call_args[0]) > 1 else call_args[1].get("query_params", {}).get("q")
        # It's passed as positional or keyword — check the actual call
        actual_params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1]["query_params"]
        assert "ChartOfAccountsId=50388" in actual_params["q"]

    def test_no_match_raises(self):
        client = _mock_client(get_resource_return={"items": []})

        with pytest.raises(ValidationError, match="No account combination"):
            lookup_ccid_by_segments(client, {"Segment1": "NOPE"})

    def test_no_coa_id(self):
        """When coa_id is None, it should not be in the q filter."""
        client = _mock_client(get_resource_return={"items": [{"CodeCombinationId": 42}]})

        result = lookup_ccid_by_segments(client, {"Segment1": "US01"}, chart_of_accounts_id=None)

        assert result == 42
        actual_params = client.get_resource.call_args[0][1]
        assert "ChartOfAccountsId" not in actual_params["q"]

    def test_multiple_items_picks_exact(self):
        """When multiple results, pick the one with exact segment match."""
        wrong_row = {"CodeCombinationId": 111, "Segment1": "XX", "Segment2": "100"}
        right_row = {"CodeCombinationId": 222, "Segment1": "US01", "Segment2": "100"}
        client = _mock_client(get_resource_return={"items": [wrong_row, right_row]})

        result = lookup_ccid_by_segments(client, {"Segment1": "US01", "Segment2": "100"})
        assert result == 222


# ===================================================================
# resolve_target_expense_ccid (end-to-end)
# ===================================================================

class TestResolveTargetExpenseCcid:
    def test_success_end_to_end(self):
        """Full flow: lookup source segments → swap company → find target CCID."""
        call_count = [0]

        def mock_get_resource(resource_path, query_params=None):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: get source CCID details
                return {"items": [SAMPLE_LOV_ROW]}
            else:
                # Second call: lookup target CCID
                return {"items": [SAMPLE_TARGET_ROW]}

        client = _mock_client(get_resource_side_effect=mock_get_resource)

        target_ccid = resolve_target_expense_ccid(
            client, src_expense_ccid=626955, target_company="US01", company_segment_key="Segment1",
        )

        assert target_ccid == 789012
        assert client.get_resource.call_count == 2

    def test_same_ccid_raises(self):
        """When target resolves to the same CCID as source, should raise."""
        same_row = dict(SAMPLE_LOV_ROW)  # CCID=626955, Segment1=101

        # Source lookup returns 626955 with segments
        # Target lookup ALSO returns 626955 (same combination)
        def mock_get_resource(resource_path, query_params=None):
            return {"items": [same_row]}

        client = _mock_client(get_resource_side_effect=mock_get_resource)

        with pytest.raises(ValidationError, match="equals source"):
            resolve_target_expense_ccid(
                client, src_expense_ccid=626955, target_company="101", company_segment_key="Segment1",
            )

    def test_missing_segment_key_raises(self):
        """When company_segment_key doesn't exist in source segments."""
        client = _mock_client(get_resource_return={"items": [SAMPLE_LOV_ROW]})

        with pytest.raises(ValidationError, match="not found"):
            resolve_target_expense_ccid(
                client, src_expense_ccid=626955, target_company="US01", company_segment_key="Segment99",
            )
