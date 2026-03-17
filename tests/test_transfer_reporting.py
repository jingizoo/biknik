"""Tests for transfer reporting classification."""

from decimal import Decimal

from ofam_asset_xfer.transfer_reporting import (
    build_rollforward_entries,
    build_trace_record,
    CATEGORY_TRANSFER_OUT,
    CATEGORY_TRANSFER_IN,
)


class TestBuildRollforwardEntries:
    def test_basic_pair(self):
        entries = build_rollforward_entries(
            source_asset_number="ASSET001",
            source_entity="US Entity",
            source_book="US CORP BOOK",
            target_asset_number="ASSET001_T",
            target_entity="UK Entity",
            target_book="UK CORP BOOK",
            transfer_date="2026-03-01",
            cost="100000",
            accumulated_depreciation="30000",
            request_id="REQ001",
        )
        assert len(entries) == 2

        # Transfer OUT
        out_entry = entries[0]
        assert out_entry.category == CATEGORY_TRANSFER_OUT
        assert out_entry.entity == "US Entity"
        assert out_entry.cost == Decimal("100000")
        assert out_entry.accumulated_depreciation == Decimal("30000")
        assert out_entry.nbv == Decimal("70000")
        assert out_entry.counterpart_entity == "UK Entity"
        assert out_entry.counterpart_asset_number == "ASSET001_T"
        assert out_entry.depreciation_reserve_at_transfer == Decimal("30000")

        # Transfer IN
        in_entry = entries[1]
        assert in_entry.category == CATEGORY_TRANSFER_IN
        assert in_entry.entity == "UK Entity"
        assert in_entry.counterpart_entity == "US Entity"
        assert in_entry.counterpart_asset_number == "ASSET001"

    def test_to_dict(self):
        entries = build_rollforward_entries(
            source_asset_number="A1",
            source_entity="US",
            source_book="US_BOOK",
            target_asset_number="A1_T",
            target_entity="UK",
            target_book="UK_BOOK",
            transfer_date="2026-01-01",
            cost="50000",
            accumulated_depreciation="10000",
        )
        for e in entries:
            d = e.to_dict()
            assert "category" in d
            assert "depreciation_reserve_at_transfer" in d


class TestBuildTraceRecord:
    def test_basic(self):
        trace = build_trace_record(
            source_asset_number="ASSET001",
            source_book="US CORP BOOK",
            source_entity="US Entity",
            target_asset_number="ASSET001_T",
            target_book="UK CORP BOOK",
            target_entity="UK Entity",
            transfer_date="2026-03-01",
            cost="100000",
            accumulated_depreciation="30000",
            tag_number="TAG-001",
            serial_number="SN-12345",
            request_id="REQ001",
            transfer_count=1,
        )
        assert trace.source_asset_number == "ASSET001"
        assert trace.target_asset_number == "ASSET001_T"
        assert trace.cost_transferred == Decimal("100000")
        assert trace.nbv_transferred == Decimal("70000")
        assert trace.tag_number == "TAG-001"
        assert trace.serial_number == "SN-12345"
        assert trace.transfer_count == 1

    def test_multiple_transfers(self):
        trace = build_trace_record(
            source_asset_number="A1",
            source_book="B1",
            source_entity="E1",
            target_asset_number="A2",
            target_book="B2",
            target_entity="E2",
            transfer_date="2026-06-01",
            cost="50000",
            accumulated_depreciation="20000",
            transfer_count=3,
        )
        assert trace.transfer_count == 3

    def test_to_dict(self):
        trace = build_trace_record(
            source_asset_number="A1",
            source_book="B1",
            source_entity="E1",
            target_asset_number="A2",
            target_book="B2",
            target_entity="E2",
            transfer_date="2026-01-01",
            cost="10000",
            accumulated_depreciation="5000",
            tag_number="T1",
        )
        d = trace.to_dict()
        assert d["tag_number"] == "T1"
        assert d["nbv_transferred"] == "5000"
        assert d["transfer_count"] == 1
