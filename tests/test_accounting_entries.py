"""Tests for accounting entries builder."""

from decimal import Decimal

import pytest

from ofam_asset_xfer.accounting_entries import build_transfer_accounting_entries
from ofam_asset_xfer.exceptions import ValidationError


class TestBuildTransferAccountingEntries:
    def test_basic_entries(self):
        entry = build_transfer_accounting_entries(
            asset_number="ASSET001",
            transfer_date="2026-03-01",
            source_entity="US Entity",
            target_entity="UK Entity",
            cost="100000",
            accumulated_depreciation="30000",
            cost_account="1400100",
            accumulated_depreciation_account="1490100",
        )
        assert entry.cost == Decimal("100000")
        assert entry.accumulated_depreciation == Decimal("30000")
        assert len(entry.lines) == 4

        # Transfer FROM: DR Accum Depr
        from_dr = entry.lines[0]
        assert from_dr.side == "TRANSFER_FROM"
        assert from_dr.account_type == "ACCUMULATED_DEPRECIATION"
        assert from_dr.debit == Decimal("30000")

        # Transfer FROM: CR Cost
        from_cr = entry.lines[1]
        assert from_cr.side == "TRANSFER_FROM"
        assert from_cr.account_type == "COST"
        assert from_cr.credit == Decimal("100000")

        # Transfer TO: DR Cost
        to_dr = entry.lines[2]
        assert to_dr.side == "TRANSFER_TO"
        assert to_dr.account_type == "COST"
        assert to_dr.debit == Decimal("100000")

        # Transfer TO: CR Accum Depr
        to_cr = entry.lines[3]
        assert to_cr.side == "TRANSFER_TO"
        assert to_cr.account_type == "ACCUMULATED_DEPRECIATION"
        assert to_cr.credit == Decimal("30000")

    def test_negative_cost_raises(self):
        with pytest.raises(ValidationError, match="negative"):
            build_transfer_accounting_entries(
                asset_number="X",
                transfer_date="2026-01-01",
                source_entity="A",
                target_entity="B",
                cost="-100",
                accumulated_depreciation="0",
                cost_account="1400",
                accumulated_depreciation_account="1490",
            )

    def test_to_dict(self):
        entry = build_transfer_accounting_entries(
            asset_number="A1",
            transfer_date="2026-01-01",
            source_entity="US",
            target_entity="UK",
            cost="50000",
            accumulated_depreciation="10000",
            cost_account="1400",
            accumulated_depreciation_account="1490",
            functional_currency="GBP",
            request_id="REQ002",
        )
        d = entry.to_dict()
        assert d["asset_number"] == "A1"
        assert d["currency"] == "GBP"
        assert d["request_id"] == "REQ002"
        assert len(d["lines"]) == 4

    def test_fully_depreciated(self):
        entry = build_transfer_accounting_entries(
            asset_number="A2",
            transfer_date="2026-01-01",
            source_entity="US",
            target_entity="UK",
            cost="50000",
            accumulated_depreciation="50000",
            cost_account="1400",
            accumulated_depreciation_account="1490",
        )
        # Even fully depreciated assets get accounting entries (cost still moves)
        assert len(entry.lines) == 4
        assert entry.lines[0].debit == Decimal("50000")
        assert entry.lines[1].credit == Decimal("50000")
