"""Unit tests for ofam_asset_xfer.location_resolver.

All tests use a mock REST client — no network calls.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from ofam_asset_xfer.location_resolver import (
    LocationResolver,
    FALocation,
    DEFAULT_COUNTRY_BOOK_MAP,
    build_resolver_from_config,
)
from ofam_asset_xfer.exceptions import LocationResolutionError


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _mock_client(get_resource_return=None, get_resource_side_effect=None):
    client = MagicMock()
    if get_resource_side_effect is not None:
        client.get_resource.side_effect = get_resource_side_effect
    elif get_resource_return is not None:
        client.get_resource.return_value = get_resource_return
    return client


SAMPLE_FA_LOCATION_US = {
    "LocationId": 12345,
    "Segment1": "US-NYC-01",
    "Segment2": "US",
    "Segment3": "NYC",
    "Description": "New York Data Center 01",
}

SAMPLE_FA_LOCATION_UK = {
    "LocationId": 67890,
    "Segment1": "UK-LON-01",
    "Segment2": "UK",
    "Segment3": "LON",
    "Description": "London Office 01",
}

SAMPLE_FA_LOCATION_AU = {
    "LocationId": 11111,
    "Segment1": "AU-SYD-01",
    "Segment2": "AU",
    "Segment3": "SYD",
    "Description": "Sydney Office 01",
}


# ===================================================================
# LocationResolver.get_fa_location
# ===================================================================

class TestGetFaLocation:
    def test_success_us_location(self):
        client = _mock_client(get_resource_return={"items": [SAMPLE_FA_LOCATION_US]})
        resolver = LocationResolver(client)

        loc = resolver.get_fa_location("US-NYC-01")

        assert loc.location_id == 12345
        assert loc.segment1 == "US-NYC-01"
        assert loc.country == "US"
        assert loc.description == "New York Data Center 01"

    def test_success_uk_location(self):
        client = _mock_client(get_resource_return={"items": [SAMPLE_FA_LOCATION_UK]})
        resolver = LocationResolver(client)

        loc = resolver.get_fa_location("UK-LON-01")

        assert loc.location_id == 67890
        assert loc.country == "UK"

    def test_caching(self):
        client = _mock_client(get_resource_return={"items": [SAMPLE_FA_LOCATION_US]})
        resolver = LocationResolver(client)

        loc1 = resolver.get_fa_location("US-NYC-01")
        loc2 = resolver.get_fa_location("US-NYC-01")

        assert loc1 is loc2
        assert client.get_resource.call_count == 1  # Only one API call

    def test_not_found_raises(self):
        client = _mock_client(get_resource_return={"items": []})
        resolver = LocationResolver(client)

        with pytest.raises(LocationResolutionError, match="No FA location"):
            resolver.get_fa_location("NONEXISTENT-LOC")

    def test_empty_code_raises(self):
        client = _mock_client()
        resolver = LocationResolver(client)

        with pytest.raises(LocationResolutionError, match="Empty"):
            resolver.get_fa_location("")

    def test_query_params_correct(self):
        client = _mock_client(get_resource_return={"items": [SAMPLE_FA_LOCATION_US]})
        resolver = LocationResolver(client, location_resource="customLocations")

        resolver.get_fa_location("US-NYC-01")

        client.get_resource.assert_called_once()
        args = client.get_resource.call_args
        assert args[0][0] == "customLocations"
        assert args[0][1]["q"] == "Segment1=US-NYC-01"


# ===================================================================
# LocationResolver.resolve_target_book
# ===================================================================

class TestResolveTargetBook:
    def test_us_location_to_us_book(self):
        client = _mock_client(get_resource_return={"items": [SAMPLE_FA_LOCATION_US]})
        resolver = LocationResolver(client)

        book = resolver.resolve_target_book("US-NYC-01")
        assert book == "US CORP BOOK"

    def test_uk_location_to_uk_book(self):
        client = _mock_client(get_resource_return={"items": [SAMPLE_FA_LOCATION_UK]})
        resolver = LocationResolver(client)

        book = resolver.resolve_target_book("UK-LON-01")
        assert book == "UK CORP BOOK"

    def test_au_location_to_au_book(self):
        client = _mock_client(get_resource_return={"items": [SAMPLE_FA_LOCATION_AU]})
        resolver = LocationResolver(client)

        book = resolver.resolve_target_book("AU-SYD-01")
        assert book == "AU CORP BOOK"

    def test_custom_country_book_map(self):
        client = _mock_client(get_resource_return={"items": [SAMPLE_FA_LOCATION_US]})
        resolver = LocationResolver(client, country_book_map={"US": "CUSTOM US BOOK"})

        book = resolver.resolve_target_book("US-NYC-01")
        assert book == "CUSTOM US BOOK"

    def test_unknown_country_raises(self):
        # Location with unknown country
        loc_row = {"LocationId": 1, "Segment1": "ZZ-XXX-01", "Segment2": "ZZ"}
        client = _mock_client(get_resource_return={"items": [loc_row]})
        resolver = LocationResolver(client, country_book_map={"US": "US CORP BOOK"})

        with pytest.raises(LocationResolutionError, match="No book mapping"):
            resolver.resolve_target_book("ZZ-XXX-01")


# ===================================================================
# Country extraction logic
# ===================================================================

class TestCountryExtraction:
    def test_from_segment2_two_letter(self):
        """Segment2 with 2-letter alpha value is used as country."""
        row = {"LocationId": 1, "Segment1": "LOC-001", "Segment2": "JP"}
        client = _mock_client(get_resource_return={"items": [row]})
        resolver = LocationResolver(client)

        loc = resolver.get_fa_location("LOC-001")
        assert loc.country == "JP"

    def test_from_code_pattern_dash(self):
        """Extract country from 'XX-...' pattern when no segment2 country."""
        row = {"LocationId": 1, "Segment1": "AU-MEL-01", "Segment2": "Melbourne"}
        client = _mock_client(get_resource_return={"items": [row]})
        resolver = LocationResolver(client)

        loc = resolver.get_fa_location("AU-MEL-01")
        assert loc.country == "AU"

    def test_from_code_pattern_dot(self):
        """Extract country from 'XX.xxx.xxx' pattern."""
        row = {"LocationId": 1, "Segment1": "UK.LON.DC1", "Segment2": "London"}
        client = _mock_client(get_resource_return={"items": [row]})
        resolver = LocationResolver(client)

        loc = resolver.get_fa_location("UK.LON.DC1")
        assert loc.country == "UK"

    def test_from_code_pattern_underscore(self):
        """Extract country from 'XX_xxx' pattern."""
        row = {"LocationId": 1, "Segment1": "SG_CBD_01", "Segment2": "CBD"}
        client = _mock_client(get_resource_return={"items": [row]})
        resolver = LocationResolver(client)

        loc = resolver.get_fa_location("SG_CBD_01")
        assert loc.country == "SG"

    def test_from_code_pattern_numeric(self):
        """Extract country from 'XX01' pattern (letters followed by digits)."""
        row = {"LocationId": 1, "Segment1": "US01", "Segment2": "001"}
        client = _mock_client(get_resource_return={"items": [row]})
        resolver = LocationResolver(client)

        loc = resolver.get_fa_location("US01")
        assert loc.country == "US"

    def test_fallback_first_two_alpha(self):
        """Fallback: use first 2 alpha characters."""
        row = {"LocationId": 1, "Segment1": "INxyz", "Segment2": "999"}
        client = _mock_client(get_resource_return={"items": [row]})
        resolver = LocationResolver(client)

        loc = resolver.get_fa_location("INxyz")
        assert loc.country == "IN"


# ===================================================================
# build_resolver_from_config
# ===================================================================

class TestBuildResolverFromConfig:
    def test_with_custom_map(self):
        client = _mock_client()
        resolver = build_resolver_from_config(client, {
            "country_book_map": {"ZZ": "ZZ CORP BOOK"},
        })

        # Should merge with defaults
        assert resolver.country_book_map["US"] == "US CORP BOOK"
        assert resolver.country_book_map["ZZ"] == "ZZ CORP BOOK"

    def test_with_custom_resource(self):
        client = _mock_client(get_resource_return={"items": [SAMPLE_FA_LOCATION_US]})
        resolver = build_resolver_from_config(client, {
            "location_resource": "myCustomLocations",
        })

        resolver.get_fa_location("US-NYC-01")
        assert client.get_resource.call_args[0][0] == "myCustomLocations"

    def test_empty_config(self):
        client = _mock_client()
        resolver = build_resolver_from_config(client, {})

        assert resolver.country_book_map == DEFAULT_COUNTRY_BOOK_MAP
