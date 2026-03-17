"""Tests for transfer restrictions framework."""

import pytest

from ofam_asset_xfer.transfer_restrictions import (
    TransferRestrictionConfig,
    TransferRestrictionEngine,
)


@pytest.fixture
def config():
    return TransferRestrictionConfig(
        blocked_books={"KL CORP BOOK"},
        approval_required_books={
            "IN CORP BOOK": "IN Controllers",
            "CN CORP BOOK": "CN Controllers",
        },
        blocked_jurisdictions={"KL"},
        location_entity_rules=[
            {
                "location_pattern": "AMSTERDAM",
                "cost_centre": "OFFICE",
                "entity": "CSNL",
            },
            {
                "location_pattern": "AMSTERDAM",
                "asset_type": "SERVER",
                "entity": "CETS",
            },
            {
                "location_pattern": "ZURICH",
                "cost_centre": "OFFICE",
                "entity": "CSZH",
            },
            {
                "location_pattern": "ZURICH",
                "asset_type": "SERVER",
                "entity": "CETS",
            },
        ],
    )


@pytest.fixture
def engine(config):
    return TransferRestrictionEngine(config)


class TestTransferRestrictions:
    def test_allowed_transfer(self, engine):
        result = engine.check_transfer("US CORP BOOK", "UK CORP BOOK")
        assert result.allowed is True
        assert result.requires_approval is False

    def test_blocked_source_book(self, engine):
        result = engine.check_transfer("KL CORP BOOK", "US CORP BOOK")
        assert result.allowed is False
        assert result.restriction_type == "BLOCKED"
        assert "KL CORP BOOK" in result.reason

    def test_blocked_target_book(self, engine):
        result = engine.check_transfer("US CORP BOOK", "KL CORP BOOK")
        assert result.allowed is False
        assert result.restriction_type == "BLOCKED"

    def test_india_approval_required(self, engine):
        result = engine.check_transfer("US CORP BOOK", "IN CORP BOOK")
        assert result.allowed is True
        assert result.requires_approval is True
        assert result.approval_group == "IN Controllers"
        assert result.restriction_type == "APPROVAL_REQUIRED"

    def test_china_approval_required(self, engine):
        result = engine.check_transfer("CN CORP BOOK", "US CORP BOOK")
        assert result.allowed is True
        assert result.requires_approval is True
        assert result.approval_group == "CN Controllers"

    def test_tax_hold_jurisdiction(self, engine):
        result = engine.check_transfer(
            "US CORP BOOK", "UK CORP BOOK",
            source_jurisdiction="KL",
        )
        assert result.allowed is False
        assert result.restriction_type == "TAX_HOLD"

    def test_case_insensitive(self, engine):
        result = engine.check_transfer("kl corp book", "us corp book")
        assert result.allowed is False


class TestLocationEntityResolution:
    def test_amsterdam_office(self, engine):
        entity = engine.resolve_entity_by_location(
            location="Amsterdam Office", cost_centre="OFFICE"
        )
        assert entity == "CSNL"

    def test_amsterdam_server(self, engine):
        entity = engine.resolve_entity_by_location(
            location="Amsterdam DC", asset_type="SERVER"
        )
        assert entity == "CETS"

    def test_zurich_office(self, engine):
        entity = engine.resolve_entity_by_location(
            location="Zurich Office", cost_centre="OFFICE"
        )
        assert entity == "CSZH"

    def test_zurich_server(self, engine):
        entity = engine.resolve_entity_by_location(
            location="Zurich DC", asset_type="SERVER"
        )
        assert entity == "CETS"

    def test_unknown_location(self, engine):
        entity = engine.resolve_entity_by_location(location="Tokyo")
        assert entity is None


class TestFromDict:
    def test_from_dict(self):
        d = {
            "blocked_books": ["KL CORP BOOK"],
            "approval_required_books": {"IN CORP BOOK": "IN Controllers"},
            "blocked_jurisdictions": ["KL", "MY"],
            "location_entity_rules": [
                {"location_pattern": "LONDON", "entity": "UKCO"},
            ],
        }
        config = TransferRestrictionConfig.from_dict(d)
        assert "KL CORP BOOK" in config.blocked_books
        assert config.approval_required_books["IN CORP BOOK"] == "IN Controllers"
        assert "MY" in config.blocked_jurisdictions
        assert len(config.location_entity_rules) == 1
