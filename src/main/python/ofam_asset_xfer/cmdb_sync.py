"""CMDB → Oracle FA location sync engine.

Compares asset locations in CMDB (u_cit_location_code) against Oracle Fusion FA
and automatically triggers cross-book or same-book transfers when mismatches are found.

Architecture:
  1. Fetch assets from CMDB (via CMDBClient)
  2. For each asset, find the corresponding FA record (via tag_number lookup)
  3. Compare: CMDB location → target book vs FA current book
  4. If mismatch → build and execute transfer (cross-book or same-book)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

from .cmdb_client import CMDBClient
from .exceptions import (
    CMDBApiError,
    FusionApiError,
    LocationResolutionError,
    OFAMAssetXferError,
    ValidationError,
)
from .fusion_ops import (
    AssetState,
    build_book_transfer_params,
    build_same_book_transfer_params,
    get_asset_information,
)
from .location_resolver import LocationResolver
from .oracle_client import OracleErpIntegrationsClient

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class AssetMismatch:
    """Represents a detected location mismatch between CMDB and Oracle FA."""

    # CMDB side
    cmdb_tag: str
    cmdb_sys_id: str
    cmdb_display_name: str
    cmdb_location_code: str
    cmdb_location_country: str

    # FA side
    fa_asset_id: str
    fa_asset_number: str
    fa_book_type_code: str
    fa_description: Optional[str]
    fa_tag_number: Optional[str]

    # Resolution
    target_book_type_code: str
    mismatch_type: str          # "CROSS_BOOK" or "SAME_BOOK"

    # FA state (for building transfer)
    fa_state: Optional[AssetState] = field(default=None, repr=False)


@dataclass
class SyncResult:
    """Result of a single asset sync operation."""

    cmdb_tag: str
    status: str                 # "TRANSFERRED", "NOOP", "SKIPPED", "FAILED", "DRY_RUN"
    mismatch_type: Optional[str] = None
    fa_asset_number: Optional[str] = None
    source_book: Optional[str] = None
    target_book: Optional[str] = None
    error: Optional[str] = None
    fusion_response: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# FA asset lookup by tag_number
# ---------------------------------------------------------------------------
def find_fa_asset_by_tag(
    client: OracleErpIntegrationsClient,
    tag_number: str,
    search_books: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Find an FA asset by its tag_number using Fusion REST fixedAssets resource.

    Tries the fixedAssets REST resource first.  If unavailable, falls back to
    iterating through search_books and calling getAssetInformation with the
    tag as asset_number (some tenants use tag=asset_number convention).

    Returns:
        Dict with {asset_id, asset_number, book_type_code, tag_number} or None.
    """
    # Primary: query fixedAssets REST resource
    try:
        resp = client.get_resource(
            "fixedAssets",
            {
                "q": f"TagNumber={tag_number}",
                "onlyData": "true",
                "fields": "AssetId,AssetNumber,TagNumber,Description",
                "expand": "assetBooks",
                "limit": "5",
            },
        )
        items = resp.get("items") or []
        if items:
            row = items[0]
            asset_id = str(row.get("AssetId") or "").strip()
            asset_number = str(row.get("AssetNumber") or "").strip()
            tag = str(row.get("TagNumber") or "").strip()
            description = str(row.get("Description") or "").strip() or None

            # Get book from assetBooks child
            books = row.get("assetBooks") or []
            if isinstance(books, list) and books:
                book_code = str(books[0].get("BookTypeCode") or "").strip()
            else:
                book_code = ""

            if asset_id and asset_number:
                log.info(
                    "Found FA asset via fixedAssets: tag=%s → asset_id=%s, asset_number=%s, book=%s",
                    tag_number, asset_id, asset_number, book_code,
                )
                return {
                    "asset_id": asset_id,
                    "asset_number": asset_number,
                    "book_type_code": book_code,
                    "tag_number": tag,
                    "description": description,
                }
    except FusionApiError as e:
        log.warning("fixedAssets REST query failed (will try book search): %s", e)

    # Fallback: iterate through search_books
    if search_books:
        for book in search_books:
            try:
                _raw, pl, state = get_asset_information(client, book, tag_number)
                log.info(
                    "Found FA asset via getAssetInformation: tag=%s in book=%s → asset_number=%s",
                    tag_number, book, state.asset_number,
                )
                return {
                    "asset_id": state.asset_id,
                    "asset_number": state.asset_number,
                    "book_type_code": state.book_type_code,
                    "tag_number": state.tag_number,
                    "description": state.description,
                }
            except (FusionApiError, OFAMAssetXferError):
                continue

    log.warning("FA asset not found for tag_number=%s", tag_number)
    return None


# ---------------------------------------------------------------------------
# CMDBFASync — core engine
# ---------------------------------------------------------------------------
class CMDBFASync:
    """Orchestrates CMDB → FA location comparison and automated transfers."""

    def __init__(
        self,
        cmdb_client: CMDBClient,
        fusion_client: OracleErpIntegrationsClient,
        location_resolver: LocationResolver,
        search_books: Optional[List[str]] = None,
    ):
        self._cmdb = cmdb_client
        self._fusion = fusion_client
        self._resolver = location_resolver
        # Books to search when fixedAssets REST is unavailable
        self._search_books = search_books or list(location_resolver.country_book_map.values())

    def find_mismatches(
        self,
        limit: int = 100,
        cmdb_query: Optional[str] = None,
    ) -> List[AssetMismatch]:
        """Scan CMDB assets and identify those whose FA book doesn't match CMDB location.

        Args:
            limit: Max mismatches to collect.
            cmdb_query: Additional ServiceNow query filter.

        Returns:
            List of AssetMismatch objects (at most `limit`).
        """
        log.info("Starting mismatch scan (limit=%d)", limit)
        cmdb_assets = self._cmdb.get_assets_with_location(
            additional_query=cmdb_query,
            limit=limit * 5,  # Fetch more since not all will be mismatches
        )
        log.info("Fetched %d CMDB assets with location codes", len(cmdb_assets))

        mismatches: List[AssetMismatch] = []

        for asset in cmdb_assets:
            if len(mismatches) >= limit:
                break

            cmdb_tag = str(asset.get("asset_tag") or "").strip()
            cmdb_loc = str(asset.get("u_cit_location_code") or "").strip()
            cmdb_name = str(asset.get("display_name") or "").strip()
            cmdb_sys_id = str(asset.get("sys_id") or "").strip()

            if not cmdb_tag or not cmdb_loc:
                continue

            try:
                mismatch = self._check_single_asset(
                    cmdb_tag=cmdb_tag,
                    cmdb_sys_id=cmdb_sys_id,
                    cmdb_display_name=cmdb_name,
                    cmdb_location_code=cmdb_loc,
                )
                if mismatch:
                    mismatches.append(mismatch)
                    log.info(
                        "Mismatch #%d: tag=%s, CMDB_loc=%s → target_book=%s (current=%s)",
                        len(mismatches), cmdb_tag, cmdb_loc,
                        mismatch.target_book_type_code, mismatch.fa_book_type_code,
                    )
            except (LocationResolutionError, FusionApiError, OFAMAssetXferError) as e:
                log.warning("Skipping tag=%s: %s", cmdb_tag, e)
                continue

        log.info("Mismatch scan complete: found %d mismatches", len(mismatches))
        return mismatches

    def _check_single_asset(
        self,
        cmdb_tag: str,
        cmdb_sys_id: str,
        cmdb_display_name: str,
        cmdb_location_code: str,
    ) -> Optional[AssetMismatch]:
        """Check if a single CMDB asset has a location mismatch with FA.

        Returns AssetMismatch if mismatch found, None if locations align.
        """
        # Step 1: Resolve CMDB location → target book
        target_book = self._resolver.resolve_target_book(cmdb_location_code)
        fa_loc = self._resolver.get_fa_location(cmdb_location_code)
        cmdb_country = fa_loc.country

        # Step 2: Find the asset in FA
        fa_asset = find_fa_asset_by_tag(
            self._fusion, cmdb_tag, self._search_books,
        )
        if not fa_asset:
            log.debug("Asset tag=%s not found in FA, skipping", cmdb_tag)
            return None

        current_book = fa_asset["book_type_code"]

        # Step 3: Compare books
        if current_book.upper().strip() == target_book.upper().strip():
            log.debug(
                "Asset tag=%s: books match (%s), no transfer needed",
                cmdb_tag, current_book,
            )
            return None

        # Step 4: Get full asset state for transfer
        fa_state = None
        try:
            _raw, _pl, fa_state = get_asset_information(
                self._fusion, current_book, fa_asset["asset_number"],
            )
        except FusionApiError as e:
            log.warning("Could not get full state for tag=%s: %s", cmdb_tag, e)

        mismatch_type = "CROSS_BOOK" if current_book != target_book else "SAME_BOOK"

        return AssetMismatch(
            cmdb_tag=cmdb_tag,
            cmdb_sys_id=cmdb_sys_id,
            cmdb_display_name=cmdb_display_name,
            cmdb_location_code=cmdb_location_code,
            cmdb_location_country=cmdb_country,
            fa_asset_id=fa_asset["asset_id"],
            fa_asset_number=fa_asset["asset_number"],
            fa_book_type_code=current_book,
            fa_description=fa_asset.get("description"),
            fa_tag_number=fa_asset.get("tag_number"),
            target_book_type_code=target_book,
            mismatch_type=mismatch_type,
            fa_state=fa_state,
        )

    def execute_transfer(
        self,
        mismatch: AssetMismatch,
        dry_run: bool = True,
        effective_date: Optional[str] = None,
    ) -> SyncResult:
        """Execute a transfer for a detected mismatch.

        Args:
            mismatch: The detected mismatch to resolve.
            dry_run: If True, build payload but don't POST to Fusion.
            effective_date: Transfer date (defaults to today).

        Returns:
            SyncResult with transfer outcome.
        """
        if not effective_date:
            effective_date = date.today().isoformat()

        request_id = f"CMDB_SYNC_{mismatch.cmdb_tag}_{int(time.time())}"

        # Ensure we have full FA state
        state = mismatch.fa_state
        if not state:
            try:
                _raw, _pl, state = get_asset_information(
                    self._fusion,
                    mismatch.fa_book_type_code,
                    mismatch.fa_asset_number,
                )
            except FusionApiError as e:
                return SyncResult(
                    cmdb_tag=mismatch.cmdb_tag,
                    status="FAILED",
                    mismatch_type=mismatch.mismatch_type,
                    fa_asset_number=mismatch.fa_asset_number,
                    source_book=mismatch.fa_book_type_code,
                    target_book=mismatch.target_book_type_code,
                    error=f"Failed to get asset state: {e}",
                )

        try:
            if mismatch.mismatch_type == "CROSS_BOOK":
                return self._execute_cross_book(
                    mismatch, state, request_id, effective_date, dry_run,
                )
            else:
                return self._execute_same_book(
                    mismatch, state, request_id, effective_date, dry_run,
                )
        except Exception as e:
            log.exception("Transfer failed for tag=%s", mismatch.cmdb_tag)
            return SyncResult(
                cmdb_tag=mismatch.cmdb_tag,
                status="FAILED",
                mismatch_type=mismatch.mismatch_type,
                fa_asset_number=mismatch.fa_asset_number,
                source_book=mismatch.fa_book_type_code,
                target_book=mismatch.target_book_type_code,
                error=str(e),
            )

    def _execute_cross_book(
        self,
        mismatch: AssetMismatch,
        state: AssetState,
        request_id: str,
        effective_date: str,
        dry_run: bool,
    ) -> SyncResult:
        """Execute a cross-book transfer (different country/book)."""
        log.info(
            "Cross-book transfer: tag=%s, %s → %s",
            mismatch.cmdb_tag, mismatch.fa_book_type_code, mismatch.target_book_type_code,
        )

        params = build_book_transfer_params(
            state=state,
            dest_book_type_code=mismatch.target_book_type_code,
            effective_date=effective_date,
            overrides={},
            request_id=request_id,
        )

        if dry_run:
            log.info("DRY-RUN: cross-book transfer payload built for tag=%s", mismatch.cmdb_tag)
            return SyncResult(
                cmdb_tag=mismatch.cmdb_tag,
                status="DRY_RUN",
                mismatch_type="CROSS_BOOK",
                fa_asset_number=mismatch.fa_asset_number,
                source_book=mismatch.fa_book_type_code,
                target_book=mismatch.target_book_type_code,
                fusion_response={"planned_params": params},
            )

        raw, pl = self._fusion.process_transaction("transferAsset", params)
        status_code = str(pl.get("X_RETURN_STATUS") or "").strip()

        return SyncResult(
            cmdb_tag=mismatch.cmdb_tag,
            status="TRANSFERRED" if status_code == "S" else "FAILED",
            mismatch_type="CROSS_BOOK",
            fa_asset_number=mismatch.fa_asset_number,
            source_book=mismatch.fa_book_type_code,
            target_book=mismatch.target_book_type_code,
            fusion_response=pl,
            error=None if status_code == "S" else f"Fusion X_RETURN_STATUS={status_code}",
        )

    def _execute_same_book(
        self,
        mismatch: AssetMismatch,
        state: AssetState,
        request_id: str,
        effective_date: str,
        dry_run: bool,
    ) -> SyncResult:
        """Execute a same-book transfer (location change within same book)."""
        log.info(
            "Same-book transfer: tag=%s in book=%s",
            mismatch.cmdb_tag, mismatch.fa_book_type_code,
        )

        params, is_noop = build_same_book_transfer_params(
            state=state,
            effective_date=effective_date,
            overrides={},
            request_id=request_id,
        )

        if is_noop:
            return SyncResult(
                cmdb_tag=mismatch.cmdb_tag,
                status="NOOP",
                mismatch_type="SAME_BOOK",
                fa_asset_number=mismatch.fa_asset_number,
                source_book=mismatch.fa_book_type_code,
                target_book=mismatch.target_book_type_code,
            )

        if dry_run:
            return SyncResult(
                cmdb_tag=mismatch.cmdb_tag,
                status="DRY_RUN",
                mismatch_type="SAME_BOOK",
                fa_asset_number=mismatch.fa_asset_number,
                source_book=mismatch.fa_book_type_code,
                target_book=mismatch.target_book_type_code,
                fusion_response={"planned_params": params},
            )

        raw, pl = self._fusion.process_transaction("transferAsset", params)
        status_code = str(pl.get("X_RETURN_STATUS") or "").strip()

        return SyncResult(
            cmdb_tag=mismatch.cmdb_tag,
            status="TRANSFERRED" if status_code == "S" else "FAILED",
            mismatch_type="SAME_BOOK",
            fa_asset_number=mismatch.fa_asset_number,
            source_book=mismatch.fa_book_type_code,
            target_book=mismatch.target_book_type_code,
            fusion_response=pl,
            error=None if status_code == "S" else f"Fusion X_RETURN_STATUS={status_code}",
        )

    def run_full_sync(
        self,
        dry_run: bool = True,
        effective_date: Optional[str] = None,
        cmdb_query: Optional[str] = None,
        max_transfers: int = 500,
    ) -> Dict[str, Any]:
        """Production entry point: scan all CMDB assets, find mismatches, execute transfers.

        Returns:
            Summary dict with counts and per-asset results.
        """
        started = int(time.time())
        if not effective_date:
            effective_date = date.today().isoformat()

        log.info("=== CMDB→FA Full Sync started (dry_run=%s, date=%s) ===", dry_run, effective_date)

        mismatches = self.find_mismatches(limit=max_transfers, cmdb_query=cmdb_query)

        results: List[Dict[str, Any]] = []
        counts = {"total": len(mismatches), "transferred": 0, "failed": 0, "noop": 0, "dry_run": 0, "skipped": 0}

        for mm in mismatches:
            result = self.execute_transfer(mm, dry_run=dry_run, effective_date=effective_date)
            results.append({
                "cmdb_tag": result.cmdb_tag,
                "status": result.status,
                "mismatch_type": result.mismatch_type,
                "fa_asset_number": result.fa_asset_number,
                "source_book": result.source_book,
                "target_book": result.target_book,
                "error": result.error,
            })

            status_key = result.status.lower()
            if status_key in counts:
                counts[status_key] += 1

        finished = int(time.time())

        summary = {
            "started_ts": started,
            "finished_ts": finished,
            "duration_seconds": finished - started,
            "dry_run": dry_run,
            "effective_date": effective_date,
            "counts": counts,
            "results": results,
        }

        log.info("=== CMDB→FA Sync complete: %s ===", counts)
        return summary
