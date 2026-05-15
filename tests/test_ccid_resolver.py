"""Unit tests for ofam_asset_xfer.ccid_resolver (Option B CCID resolution).

All tests use a mock REST client — no network calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from ofam_asset_xfer.ccid_resolver import (
    build_target_segments,
    get_ccid_details,
    lookup_ccid_by_segments,
    resolve_target_ccid_from_segments,
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
            "Segment1": "US01",
            "Segment2": "100",
            "Segment3": "7710",
            "Segment4": "0000",
            "Segment5": "000",
            "Segment6": "000",
            "Segment7": "000",
        }
        result = lookup_ccid_by_segments(client, target_segments, chart_of_accounts_id=50388)

        assert result == 789012

        # Verify q filter and fields were built correctly
        actual_params = client.get_resource.call_args[0][1]
        assert "ChartOfAccountsId=50388" in actual_params["q"]
        # Segments should be numerically sorted in q filter
        assert actual_params["q"].startswith("Segment1=US01;Segment2=100;Segment3=7710")
        # fields should include segment fields for exact-match verification
        for seg in [
            "Segment1",
            "Segment2",
            "Segment3",
            "Segment4",
            "Segment5",
            "Segment6",
            "Segment7",
        ]:
            assert seg in actual_params["fields"]

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
            client,
            src_expense_ccid=626955,
            target_company="US01",
            company_segment_key="Segment1",
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
                client,
                src_expense_ccid=626955,
                target_company="101",
                company_segment_key="Segment1",
            )

    def test_missing_segment_key_raises(self):
        """When company_segment_key doesn't exist in source segments."""
        client = _mock_client(get_resource_return={"items": [SAMPLE_LOV_ROW]})

        with pytest.raises(ValidationError, match="not found"):
            resolve_target_expense_ccid(
                client,
                src_expense_ccid=626955,
                target_company="US01",
                company_segment_key="Segment99",
            )


class TestResolveTargetCcidFromSegments:
    """The BIP-report-supplies-TARGET_SEG* path.  No source-CCID dance —
    the report already worked out the destination segments and the
    pipeline just needs to look them up (or mint via SOAP)."""

    def test_lookup_returns_existing_ccid(self):
        client = _mock_client(
            get_resource_return={
                "items": [
                    {
                        "CodeCombinationId": 999000,
                        "Segment1": "SG01",
                        "Segment2": "100",
                        "Segment3": "7000",
                        "ConcatenatedSegments": "SG01-100-7000",
                    }
                ]
            }
        )
        ccid = resolve_target_ccid_from_segments(
            client,
            {"Segment1": "SG01", "Segment2": "100", "Segment3": "7000"},
        )
        assert ccid == 999000

    def test_creates_when_lookup_misses(self):
        client = _mock_client(get_resource_return={"items": []})

        captured = []

        def _creator(ledger, segments):
            captured.append((ledger, dict(segments)))
            return 1234567

        ccid = resolve_target_ccid_from_segments(
            client,
            {"Segment1": "SG01", "Segment2": "100"},
            creator=_creator,
            ledger_name="Singapore Primary Ledger",
        )
        assert ccid == 1234567
        assert captured == [("Singapore Primary Ledger", {"Segment1": "SG01", "Segment2": "100"})]

    def test_raises_without_creator_when_lookup_misses(self):
        client = _mock_client(get_resource_return={"items": []})

        with pytest.raises(ValidationError, match="No account combination found"):
            resolve_target_ccid_from_segments(
                client,
                {"Segment1": "SG01"},
            )

    def test_empty_segments_rejected(self):
        client = _mock_client(get_resource_return={"items": []})
        with pytest.raises(ValidationError, match="non-empty"):
            resolve_target_ccid_from_segments(client, {})

    def test_skip_lov_lookup_calls_creator_directly(self):
        """When skip_lov_lookup=True, the REST accountCombinationsLOV
        call is bypassed entirely and the resolver goes straight to the
        SOAP creator.  Oracle's validateAndCreateAccounts returns the
        existing CcId when the combination already exists, so the LOV
        round-trip is redundant."""
        client = MagicMock()
        # REST get_resource must NOT be called.
        client.get_resource.side_effect = AssertionError(
            "get_resource should not be called when skip_lov_lookup=True"
        )

        captured = []

        def _creator(ledger, segments):
            captured.append((ledger, dict(segments)))
            return 7777777

        ccid = resolve_target_ccid_from_segments(
            client,
            {"Segment1": "SG01", "Segment2": "100"},
            creator=_creator,
            ledger_name="Singapore Primary Ledger",
            skip_lov_lookup=True,
        )

        assert ccid == 7777777
        assert captured == [("Singapore Primary Ledger", {"Segment1": "SG01", "Segment2": "100"})]

    def test_skip_lov_lookup_requires_creator(self):
        client = MagicMock()
        with pytest.raises(ValidationError, match="creator and a ledger_name"):
            resolve_target_ccid_from_segments(
                client,
                {"Segment1": "X"},
                skip_lov_lookup=True,
            )

    def test_skip_lov_lookup_propagates_fusion_error(self):
        client = MagicMock()

        def _bad_creator(ledger, segments):
            raise FusionApiError("invalid segment value")

        with pytest.raises(FusionApiError, match="invalid segment value"):
            resolve_target_ccid_from_segments(
                client,
                {"Segment1": "X"},
                creator=_bad_creator,
                ledger_name="L",
                skip_lov_lookup=True,
            )
