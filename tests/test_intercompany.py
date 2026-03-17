"""Tests for intercompany journal entry builder."""

from decimal import Decimal

import pytest

from ofam_asset_xfer.intercompany import (
    ICConfig,
    build_ic_journal_entry,
    compute_nbv,
    convert_to_ic_currency,
)
from ofam_asset_xfer.exceptions import ValidationError


class TestComputeNBV:
    def test_basic(self):
        assert compute_nbv("100000", "30000") == Decimal("70000")

    def test_fully_depreciated(self):
        assert compute_nbv("50000", "50000") == Decimal("0")

    def test_negative_raises(self):
        with pytest.raises(ValidationError, match="NBV is negative"):
            compute_nbv("10000", "20000")

    def test_with_commas(self):
        assert compute_nbv("1,000,000", "250,000") == Decimal("750000")


class TestConvertToICCurrency:
    def test_jpy_to_usd(self):
        # 150 JPY per USD
        result = convert_to_ic_currency(Decimal("1500000"), Decimal("150"))
        assert result == Decimal("10000.00")

    def test_zero_rate_raises(self):
        with pytest.raises(ValidationError, match="positive"):
            convert_to_ic_currency(Decimal("1000"), Decimal("0"))

    def test_negative_rate_raises(self):
        with pytest.raises(ValidationError, match="positive"):
            convert_to_ic_currency(Decimal("1000"), Decimal("-1"))

    def test_rounding(self):
        result = convert_to_ic_currency(Decimal("1000"), Decimal("3"))
        assert result == Decimal("333.33")


class TestBuildICJournalEntry:
    def test_basic_usd_transfer(self):
        entry = build_ic_journal_entry(
            source_entity="US Entity",
            target_entity="UK Entity",
            asset_number="ASSET001",
            transfer_date="2026-03-01",
            cost="100000",
            accumulated_depreciation="30000",
            source_currency="USD",
        )
        assert entry is not None
        assert entry.nbv == Decimal("70000")
        assert len(entry.lines) == 2

        # DR line (source entity receivable)
        dr_line = entry.lines[0]
        assert dr_line.entity == "US Entity"
        assert dr_line.account == "1050100"
        assert dr_line.intercompany == "UK Entity"
        assert dr_line.debit == Decimal("70000")
        assert dr_line.credit is None

        # CR line (target entity payable)
        cr_line = entry.lines[1]
        assert cr_line.entity == "UK Entity"
        assert cr_line.account == "2050100"
        assert cr_line.intercompany == "US Entity"
        assert cr_line.credit == Decimal("70000")
        assert cr_line.debit is None

    def test_fully_depreciated_returns_none(self):
        entry = build_ic_journal_entry(
            source_entity="US Entity",
            target_entity="UK Entity",
            asset_number="ASSET002",
            transfer_date="2026-03-01",
            cost="50000",
            accumulated_depreciation="50000",
        )
        assert entry is None

    def test_cross_currency_jpy(self):
        entry = build_ic_journal_entry(
            source_entity="JP Entity",
            target_entity="US Entity",
            asset_number="ASSET003",
            transfer_date="2026-03-01",
            cost="15000000",  # 15M JPY
            accumulated_depreciation="5000000",  # 5M JPY
            source_currency="JPY",
            exchange_rate=Decimal("150"),  # 150 JPY = 1 USD
        )
        assert entry is not None
        # NBV = 10M JPY, IC amount = 10M / 150 = 66666.67 USD
        assert entry.nbv == Decimal("10000000")
        assert entry.lines[0].currency == "USD"
        assert entry.lines[0].debit == Decimal("66666.67")

    def test_cross_currency_no_rate_raises(self):
        with pytest.raises(ValidationError, match="exchange_rate"):
            build_ic_journal_entry(
                source_entity="JP Entity",
                target_entity="US Entity",
                asset_number="ASSET004",
                transfer_date="2026-03-01",
                cost="1000000",
                accumulated_depreciation="0",
                source_currency="JPY",
            )

    def test_custom_accounts(self):
        cfg = ICConfig(
            ic_receivable_account="1060100",
            ic_payable_account="2060100",
        )
        entry = build_ic_journal_entry(
            source_entity="A",
            target_entity="B",
            asset_number="X",
            transfer_date="2026-01-01",
            cost="10000",
            accumulated_depreciation="0",
            config=cfg,
        )
        assert entry is not None
        assert entry.lines[0].account == "1060100"
        assert entry.lines[1].account == "2060100"

    def test_to_dict(self):
        entry = build_ic_journal_entry(
            source_entity="US",
            target_entity="UK",
            asset_number="A1",
            transfer_date="2026-01-01",
            cost="10000",
            accumulated_depreciation="2000",
            request_id="REQ001",
        )
        d = entry.to_dict()
        assert d["source_entity"] == "US"
        assert d["target_entity"] == "UK"
        assert d["nbv"] == "8000"
        assert d["request_id"] == "REQ001"
        assert len(d["lines"]) == 2
