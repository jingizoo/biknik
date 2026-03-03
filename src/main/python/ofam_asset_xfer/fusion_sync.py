"""Fusion FA DFF-based IU transfer discovery and execution.

Replaces the CMDB-based sync approach.  Instead of querying ServiceNow CMDB
for asset locations, we read the asset-level Descriptive Flexfields (DFFs)
directly from Oracle Fusion FA:

  * **Transfer Date**       – when the IU transfer should happen
  * **Transfer to Entity**  – the destination legal entity

If "Transfer to Entity" is populated and maps to a different book than the
asset's current book, the asset is a pending IU transfer.

Architecture:
  1. Query fixedAssets REST with assetBooks + assetDFF expansion
  2. Filter for assets whose Transfer-to-Entity DFF is populated
  3. Resolve entity → target book via EntityBookResolver
  4. Compare current book vs target book
  5. If mismatch → pending transfer (cross-book)
  6. Execute via processTransaction-transferAsset
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

from .entity_resolver import EntityBookResolver
from .exceptions import FusionApiError, OFAMAssetXferError, ValidationError
from .fusion_ops import (
    AssetState,
    build_book_transfer_params,
    build_same_book_transfer_params,
    get_asset_information,
)
from .oracle_client import OracleErpIntegrationsClient

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DFF configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DFFConfig:
    """Maps logical DFF fields to the actual REST attribute names.

    The REST field names depend on how the DFF segments were registered in
    Oracle Fusion.  Override these if your environment uses different names.
    """
    transfer_date: str = "transferDate"
    transfer_to_entity: str = "transferToEntity"
    transfer_to_location: str = "transferToLocation"


DEFAULT_DFF_CONFIG = DFFConfig()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class PendingTransfer:
    """An FA asset whose DFF indicates a pending IU transfer."""

    # FA identity
    asset_id: str
    asset_number: str
    book_type_code: str           # current book
    description: Optional[str]
    tag_number: Optional[str]
    cost: Optional[str]

    # From DFF
    transfer_date: str            # effective date from DFF
    transfer_to_entity: str       # destination entity from DFF
    transfer_to_location: Optional[str]  # optional location hint from DFF

    # Resolved
    target_book_type_code: str    # resolved from entity

    # Cached state for transfer execution
    fa_state: Optional[AssetState] = field(default=None, repr=False)


@dataclass
class TransferResult:
    """Result of a single IU transfer operation."""

    asset_number: str
    status: str                   # TRANSFERRED, NOOP, DRY_RUN, FAILED
    source_book: Optional[str] = None
    target_book: Optional[str] = None
    transfer_to_entity: Optional[str] = None
    transfer_date: Optional[str] = None
    error: Optional[str] = None
    fusion_response: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# FusionIUSync — core engine
# ---------------------------------------------------------------------------
class FusionIUSync:
    """Discovers and executes IU transfers using FA asset DFF fields."""

    def __init__(
        self,
        fusion_client: OracleErpIntegrationsClient,
        entity_resolver: EntityBookResolver,
        dff_config: Optional[DFFConfig] = None,
    ):
        self._client = fusion_client
        self._entity_resolver = entity_resolver
        self._dff = dff_config or DEFAULT_DFF_CONFIG

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def find_pending_transfers(
        self,
        books: List[str],
        limit: int = 100,
    ) -> List[PendingTransfer]:
        """Scan FA assets for those with Transfer-to-Entity DFF populated.

        Queries the fixedAssets REST resource with assetBooks and assetDFF
        expansion, then filters client-side for assets where:
          - the asset is in one of the specified books
          - the Transfer-to-Entity DFF is populated
          - the resolved target book differs from the current book

        Args:
            books: List of book_type_codes to scan.
            limit: Max pending transfers to collect.

        Returns:
            List of PendingTransfer objects (at most ``limit``).
        """
        log.info("Scanning for pending IU transfers (books=%s, limit=%d)", books, limit)

        books_upper = {b.upper().strip() for b in books}
        pending: List[PendingTransfer] = []
        offset = 0
        batch_size = 200

        while len(pending) < limit:
            try:
                resp = self._client.get_resource("fixedAssets", {
                    "expand": "assetBooks,assetDFF",
                    "onlyData": "true",
                    "limit": str(batch_size),
                    "offset": str(offset),
                })
            except FusionApiError as e:
                log.error("fixedAssets query failed at offset=%d: %s", offset, e)
                break

            items = resp.get("items") or []
            if not items:
                break

            for item in items:
                if len(pending) >= limit:
                    break

                result = self._check_asset(item, books_upper)
                if result:
                    pending.append(result)
                    log.info(
                        "Pending #%d: asset=%s, %s → %s (entity=%s, date=%s)",
                        len(pending),
                        result.asset_number,
                        result.book_type_code,
                        result.target_book_type_code,
                        result.transfer_to_entity,
                        result.transfer_date,
                    )

            offset += batch_size
            if len(items) < batch_size:
                break  # No more pages

        log.info("Scan complete: found %d pending transfer(s)", len(pending))
        return pending

    def _check_asset(
        self,
        item: Dict[str, Any],
        books_upper: set,
    ) -> Optional[PendingTransfer]:
        """Check a single fixedAssets REST row for a pending transfer."""

        # --- Extract DFF ---
        dff_list = item.get("assetDFF") or []
        if not dff_list:
            return None
        dff = dff_list[0] if isinstance(dff_list, list) else dff_list

        transfer_entity = str(dff.get(self._dff.transfer_to_entity) or "").strip()
        if not transfer_entity:
            return None

        transfer_date_raw = str(dff.get(self._dff.transfer_date) or "").strip()
        transfer_location = str(dff.get(self._dff.transfer_to_location) or "").strip() or None

        # --- Current book from assetBooks ---
        current_book = self._extract_current_book(item, books_upper)
        if not current_book:
            return None

        # --- Resolve target book from entity ---
        try:
            target_book = self._entity_resolver.resolve_target_book(transfer_entity)
        except ValidationError as e:
            log.debug("Skipping asset %s: %s", item.get("AssetNumber"), e)
            return None

        if current_book.upper().strip() == target_book.upper().strip():
            return None  # Already in the right book

        # --- Build pending transfer ---
        asset_id = str(item.get("AssetId") or "").strip()
        asset_number = str(item.get("AssetNumber") or "").strip()

        if not asset_id or not asset_number:
            return None

        # Extract cost from assetBooks if available
        cost = None
        for ab in (item.get("assetBooks") or []):
            if str(ab.get("BookTypeCode") or "").upper().strip() == current_book.upper().strip():
                cost = str(ab.get("Cost") or "").strip() or None
                break

        return PendingTransfer(
            asset_id=asset_id,
            asset_number=asset_number,
            book_type_code=current_book,
            description=str(item.get("Description") or "").strip() or None,
            tag_number=str(item.get("TagNumber") or "").strip() or None,
            cost=cost,
            transfer_date=transfer_date_raw or date.today().isoformat(),
            transfer_to_entity=transfer_entity,
            transfer_to_location=transfer_location,
            target_book_type_code=target_book,
        )

    @staticmethod
    def _extract_current_book(item: Dict[str, Any], books_upper: set) -> Optional[str]:
        """Get the current book_type_code from assetBooks child."""
        for ab in (item.get("assetBooks") or []):
            btc = str(ab.get("BookTypeCode") or "").strip()
            if btc.upper().strip() in books_upper:
                return btc
        return None

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def execute_transfer(
        self,
        pending: PendingTransfer,
        dry_run: bool = True,
    ) -> TransferResult:
        """Execute an IU transfer for a pending asset.

        Uses the DFF Transfer Date as the effective date.

        Args:
            pending: The pending transfer to execute.
            dry_run: If True, build payload but don't POST.

        Returns:
            TransferResult with transfer outcome.
        """
        effective_date = pending.transfer_date or date.today().isoformat()
        request_id = f"IU_XFER_{pending.asset_number}_{int(time.time())}"

        # Fetch full asset state
        state = pending.fa_state
        if not state:
            try:
                _raw, _pl, state = get_asset_information(
                    self._client,
                    pending.book_type_code,
                    pending.asset_number,
                )
            except FusionApiError as e:
                return TransferResult(
                    asset_number=pending.asset_number,
                    status="FAILED",
                    source_book=pending.book_type_code,
                    target_book=pending.target_book_type_code,
                    transfer_to_entity=pending.transfer_to_entity,
                    transfer_date=effective_date,
                    error=f"Failed to get asset state: {e}",
                )

        try:
            return self._execute_cross_book(
                pending, state, request_id, effective_date, dry_run,
            )
        except Exception as e:
            log.exception("Transfer failed for asset=%s", pending.asset_number)
            return TransferResult(
                asset_number=pending.asset_number,
                status="FAILED",
                source_book=pending.book_type_code,
                target_book=pending.target_book_type_code,
                transfer_to_entity=pending.transfer_to_entity,
                transfer_date=effective_date,
                error=str(e),
            )

    def _execute_cross_book(
        self,
        pending: PendingTransfer,
        state: AssetState,
        request_id: str,
        effective_date: str,
        dry_run: bool,
    ) -> TransferResult:
        """Execute a cross-book IU transfer."""
        log.info(
            "IU transfer: asset=%s, %s → %s (entity=%s)",
            pending.asset_number,
            pending.book_type_code,
            pending.target_book_type_code,
            pending.transfer_to_entity,
        )

        overrides: Dict[str, Any] = {}
        if pending.transfer_to_location:
            overrides["location_ccid"] = pending.transfer_to_location

        params = build_book_transfer_params(
            state=state,
            dest_book_type_code=pending.target_book_type_code,
            effective_date=effective_date,
            overrides=overrides,
            request_id=request_id,
        )

        if dry_run:
            log.info("DRY-RUN: IU transfer payload built for asset=%s", pending.asset_number)
            return TransferResult(
                asset_number=pending.asset_number,
                status="DRY_RUN",
                source_book=pending.book_type_code,
                target_book=pending.target_book_type_code,
                transfer_to_entity=pending.transfer_to_entity,
                transfer_date=effective_date,
                fusion_response={"planned_params": params},
            )

        raw, pl = self._client.process_transaction("transferAsset", params)
        status_code = str(pl.get("X_RETURN_STATUS") or "").strip()

        return TransferResult(
            asset_number=pending.asset_number,
            status="TRANSFERRED" if status_code == "S" else "FAILED",
            source_book=pending.book_type_code,
            target_book=pending.target_book_type_code,
            transfer_to_entity=pending.transfer_to_entity,
            transfer_date=effective_date,
            fusion_response=pl,
            error=None if status_code == "S" else f"Fusion X_RETURN_STATUS={status_code}",
        )

    # ------------------------------------------------------------------
    # Batch entry point
    # ------------------------------------------------------------------
    def run_full_sync(
        self,
        books: List[str],
        dry_run: bool = True,
        max_transfers: int = 500,
    ) -> Dict[str, Any]:
        """Production entry point: scan FA assets, find pending IU transfers, execute.

        Returns:
            Summary dict with counts and per-asset results.
        """
        started = int(time.time())

        log.info(
            "=== Fusion IU Sync started (books=%s, dry_run=%s) ===",
            books, dry_run,
        )

        pending_list = self.find_pending_transfers(books=books, limit=max_transfers)

        results: List[Dict[str, Any]] = []
        counts = {
            "total": len(pending_list),
            "transferred": 0,
            "failed": 0,
            "dry_run": 0,
        }

        for pt in pending_list:
            result = self.execute_transfer(pt, dry_run=dry_run)
            results.append({
                "asset_number": result.asset_number,
                "status": result.status,
                "source_book": result.source_book,
                "target_book": result.target_book,
                "transfer_to_entity": result.transfer_to_entity,
                "transfer_date": result.transfer_date,
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
            "books": books,
            "counts": counts,
            "results": results,
        }

        log.info("=== Fusion IU Sync complete: %s ===", counts)
        return summary
