"""Resolves CMDB location codes to Oracle Fusion FA locations, books, and CCIDs.

Flow:
  1. CMDB u_cit_location_code  →  FA fa_locations.segment1 match
  2. FA location record        →  country extraction (from location segments or configured map)
  3. Country                   →  book_type_code (via COUNTRY_BOOK_MAP)
  4. FA location               →  location CCID (for distribution-level transfer)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

from .exceptions import LocationResolutionError

log = logging.getLogger(__name__)


# Default country → book mapping.  Override via config.
DEFAULT_COUNTRY_BOOK_MAP: Dict[str, str] = {
    "US": "US CORP BOOK",
    "UK": "UK CORP BOOK",
    "GB": "UK CORP BOOK",
    "AU": "AU CORP BOOK",
    "JP": "JP CORP BOOK",
    "HK": "HK CORP BOOK",
    "SG": "SG CORP BOOK",
    "IN": "IN CORP BOOK",
    "DE": "DE CORP BOOK",
    "FR": "FR CORP BOOK",
    "CH": "CH CORP BOOK",
    "CA": "CA CORP BOOK",
}


# ---------------------------------------------------------------------------
# Protocol for the Fusion REST client (allows mocking)
# ---------------------------------------------------------------------------
class _RestClient(Protocol):
    def get_resource(
        self, resource_path: str, query_params: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]: ...


# ---------------------------------------------------------------------------
# FA Location record
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FALocation:
    """Represents a resolved Oracle Fusion FA location record."""

    location_id: int
    segment1: str           # Maps to u_cit_location_code
    country: str            # Derived from location data or code pattern
    description: Optional[str] = None
    location_ccid: Optional[int] = None  # CodeCombinationId if available


# ---------------------------------------------------------------------------
# LocationResolver
# ---------------------------------------------------------------------------
class LocationResolver:
    """Maps CMDB location codes to FA locations and target books.

    The resolver queries Oracle Fusion REST to find FA location records
    matching the CMDB u_cit_location_code (via fa_locations.segment1),
    then determines the target book from the location's country.
    """

    def __init__(
        self,
        fusion_client: _RestClient,
        country_book_map: Optional[Dict[str, str]] = None,
        location_resource: str = "fixedAssetLocations",
    ):
        self._client = fusion_client
        self._country_book_map = country_book_map or dict(DEFAULT_COUNTRY_BOOK_MAP)
        self._location_resource = location_resource
        # Cache: location_code → FALocation
        self._cache: Dict[str, FALocation] = {}

    @property
    def country_book_map(self) -> Dict[str, str]:
        return dict(self._country_book_map)

    def get_fa_location(self, location_code: str) -> FALocation:
        """Look up an FA location by its segment1 (= CMDB u_cit_location_code).

        Queries the Fusion fixedAssetLocations REST resource.

        Returns:
            FALocation with location_id, segment1, and derived country.

        Raises:
            LocationResolutionError if no matching FA location is found.
        """
        location_code = location_code.strip()
        if not location_code:
            raise LocationResolutionError("Empty location_code provided")

        if location_code in self._cache:
            return self._cache[location_code]

        log.info("Querying FA location for segment1=%s", location_code)

        resp = self._client.get_resource(
            self._location_resource,
            {
                "q": f"Segment1={location_code}",
                "onlyData": "true",
                "fields": "LocationId,Segment1,Segment2,Segment3,Segment4,Segment5,Segment6,Segment7,Description",
                "limit": "5",
            },
        )

        items = resp.get("items") or []
        if not items:
            raise LocationResolutionError(
                f"No FA location found for segment1='{location_code}'. "
                f"The location code may need to be created in Oracle FA first."
            )

        row = items[0]
        location_id = int(row.get("LocationId") or row.get("locationId") or 0)
        seg1 = str(row.get("Segment1") or row.get("segment1") or "").strip()
        description = str(row.get("Description") or row.get("description") or "").strip() or None

        # Derive country from segments or location code pattern
        country = self._extract_country(row, location_code)

        fa_loc = FALocation(
            location_id=location_id,
            segment1=seg1,
            country=country,
            description=description,
        )

        self._cache[location_code] = fa_loc
        log.info(
            "Resolved FA location: code=%s → location_id=%s, country=%s, desc=%s",
            location_code, location_id, country, description,
        )
        return fa_loc

    def resolve_target_book(self, cmdb_location_code: str) -> str:
        """Determine the target FA book_type_code for a CMDB location code.

        Flow: location_code → FA location → country → book_type_code

        Raises:
            LocationResolutionError if country cannot be mapped to a book.
        """
        fa_loc = self.get_fa_location(cmdb_location_code)
        country = fa_loc.country.upper()

        book = self._country_book_map.get(country)
        if not book:
            raise LocationResolutionError(
                f"No book mapping for country='{country}' (from location_code='{cmdb_location_code}'). "
                f"Add it to the country_book_map configuration. "
                f"Available mappings: {self._country_book_map}"
            )

        log.info(
            "Target book resolved: location=%s → country=%s → book=%s",
            cmdb_location_code, country, book,
        )
        return book

    def get_location_id(self, location_code: str) -> int:
        """Get the FA LocationId for a CMDB location code."""
        fa_loc = self.get_fa_location(location_code)
        return fa_loc.location_id

    def _extract_country(self, row: Dict[str, Any], location_code: str) -> str:
        """Extract country code from FA location segments or location code pattern.

        Strategy (in priority order):
        1. Check Segment2 or a 'Country' field if present in the response
        2. Parse country prefix from location code pattern (e.g., 'US-NYC-01' → 'US')
        3. Use first 2 characters of location code as country hint

        Override this method for custom extraction logic.
        """
        # Strategy 1: Look for a country field or well-known segment
        for field_name in ("Country", "country", "Segment2", "segment2"):
            val = str(row.get(field_name) or "").strip()
            if val and len(val) == 2 and val.isalpha():
                return val.upper()

        # Strategy 2: Parse location code pattern
        # Common patterns: "US-NYC-01", "UK.LON.DC1", "AU_SYD_001", "US01"
        m = re.match(r"^([A-Z]{2})[\-._\d]", location_code.upper())
        if m:
            return m.group(1)

        # Strategy 3: First 2 alpha characters
        alpha = re.match(r"^([A-Za-z]{2})", location_code)
        if alpha:
            return alpha.group(1).upper()

        raise LocationResolutionError(
            f"Cannot extract country from location_code='{location_code}' "
            f"and no country field in FA location response. Row keys: {list(row.keys())}"
        )


def build_resolver_from_config(
    fusion_client: _RestClient,
    config: Dict[str, Any],
) -> LocationResolver:
    """Factory: build a LocationResolver from a config dict.

    Expected config shape:
        {
            "country_book_map": {"US": "US CORP BOOK", ...},
            "location_resource": "fixedAssetLocations"
        }
    """
    country_map = config.get("country_book_map")
    if isinstance(country_map, dict) and country_map:
        merged = dict(DEFAULT_COUNTRY_BOOK_MAP)
        merged.update(country_map)
    else:
        merged = None

    resource = str(config.get("location_resource", "fixedAssetLocations")).strip()

    return LocationResolver(
        fusion_client=fusion_client,
        country_book_map=merged,
        location_resource=resource,
    )
