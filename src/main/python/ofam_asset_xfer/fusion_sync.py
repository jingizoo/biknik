"""Fusion FA DFF-based IU transfer discovery and execution.

Replaces the CMDB-based sync approach.  Instead of querying ServiceNow CMDB
for asset locations, we read the asset-level Descriptive Flexfields (DFFs)
from an Oracle BI Publisher report (All_IUT_Transfers_Rpt):

  * **Book Type Code**        – P_BOOK_TYPE_CODE    – echoed report parameter at root level
  * **Transfer Date**         – TRANSFER_DATE       – when the IU transfer should happen
  * **Transfer to Entity**    – TRANSFER_TO_ENTITY   – the destination legal entity
  * **Transfer to Location**  – TRANSFER_TO_LOCATION – optional location hint

Architecture:
  1. Call BIP report via SOAP to get asset transfer candidates (DFF populated)
  2. For each candidate, call getAssetInformation to get full asset state
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

from .bip_client import BIPClient
from .entity_resolver import EntityBookResolver
from .exceptions import FusionApiError, ValidationError
from .fusion_ops import (
    AssetState,
    build_book_transfer_params,
    get_asset_information,
)
from .oracle_client import OracleErpIntegrationsClient

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DFF column configuration (maps BIP report column names to logical fields)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DFFConfig:
    """Maps logical DFF fields to BIP report XML column names.

    The column names depend on the SQL aliases used in the BIP report
    data model.  Override these if your report uses different names.
    """

    # Asset identity columns in the BIP report
    asset_id_col: str = "ASSET_ID"
    asset_number_col: str = "ASSET_NUMBER"
    book_type_code_col: str = "P_BOOK_TYPE_CODE"

    # All_IUT_Transfers_Rpt column aliases
    transfer_date_col: str = "TRANSFER_DATE"
    transfer_to_entity_col: str = "TRANSFER_TO_ENTITY"
    transfer_to_location_col: str = "TRANSFER_TO_LOCATION"


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
    book_type_code: str  # current book
    description: Optional[str]
    tag_number: Optional[str]
    cost: Optional[str]

    # From DFF
    transfer_date: str  # effective date from DFF
    transfer_to_entity: str  # destination entity from DFF
    transfer_to_location: Optional[str]  # optional location hint from DFF

    # Resolved
    target_book_type_code: str  # resolved from entity

    # Cached state for transfer execution
    fa_state: Optional[AssetState] = field(default=None, repr=False)


@dataclass
class TransferResult:
    """Result of a single IU transfer operation."""

    asset_number: str
    status: str  # TRANSFERRED, NOOP, DRY_RUN, FAILED
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
        bip_client: BIPClient,
        dff_config: Optional[DFFConfig] = None,
    ):
        self._client = fusion_client
        self._entity_resolver = entity_resolver
        self._bip = bip_client
        self._dff = dff_config or DEFAULT_DFF_CONFIG

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def find_pending_transfers(
        self,
        books: List[str],
        limit: int = 100,
        bip_params: Optional[Dict[str, str]] = None,
    ) -> List[PendingTransfer]:
        """Discover pending IU transfers from BIP report + getAssetInformation.

        1. Run the BIP report to get assets with Transfer-to-Entity DFF populated.
        2. For each candidate, call getAssetInformation to get full asset state.
        3. Resolve entity → target book and filter for actual pending transfers.

        Args:
            books: List of book_type_codes to scan.
            limit: Max pending transfers to collect.
            bip_params: Optional extra parameters to pass to the BIP report.

        Returns:
            List of PendingTransfer objects (at most ``limit``).
        """
        log.info("Scanning for pending IU transfers (books=%s, limit=%d)", books, limit)
        books_upper = {b.upper().strip() for b in books}

        # Step 1: Run BIP report to get transfer candidates
        try:
            rows = self._bip.run_report(params=bip_params)
        except FusionApiError as e:
            log.error("BIP report failed: %s", e)
            return []

        log.info("BIP report returned %d row(s)", len(rows))

        # Step 2: Filter and enrich each candidate
        pending: List[PendingTransfer] = []
        for row in rows:
            if len(pending) >= limit:
                break

            result = self._check_bip_row(row, books_upper)
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

        log.info("Scan complete: found %d pending transfer(s)", len(pending))
        return pending

    def _check_bip_row(
        self,
        row: Dict[str, str],
        books_upper: set,
    ) -> Optional[PendingTransfer]:
        """Check a single BIP report row for a pending transfer.

        Calls getAssetInformation to get full asset state for validated
        candidates.
        """
        dff = self._dff

        # --- Extract DFF fields from report row ---
        transfer_entity = row.get(dff.transfer_to_entity_col, "").strip()
        if not transfer_entity:
            return None

        asset_id = row.get(dff.asset_id_col, "").strip()
        asset_number = row.get(dff.asset_number_col, "").strip()
        book_type_code = row.get(dff.book_type_code_col, "").strip()
        if not asset_number or not book_type_code:
            return None

        # Check book is in the scan list
        if book_type_code.upper().strip() not in books_upper:
            return None

        transfer_date_raw = row.get(dff.transfer_date_col, "").strip()
        transfer_location = row.get(dff.transfer_to_location_col, "").strip() or None

        # --- Resolve target book from entity ---
        try:
            target_book = self._entity_resolver.resolve_target_book(transfer_entity)
        except ValidationError as e:
            log.debug("Skipping asset %s: %s", asset_number, e)
            return None

        if book_type_code.upper().strip() == target_book.upper().strip():
            return None  # Already in the right book

        # --- Call getAssetInformation to get full state ---
        try:
            _raw, _pl, state = get_asset_information(
                self._client,
                book_type_code,
                asset_number,
                asset_id=asset_id or None,
            )
        except FusionApiError as e:
            log.warning(
                "getAssetInformation failed for asset=%s book=%s: %s",
                asset_number,
                book_type_code,
                e,
            )
            return None

        return PendingTransfer(
            asset_id=state.asset_id,
            asset_number=state.asset_number,
            book_type_code=book_type_code,
            description=state.description,
            tag_number=state.tag_number,
            cost=state.cost,
            transfer_date=transfer_date_raw or date.today().isoformat(),
            transfer_to_entity=transfer_entity,
            transfer_to_location=transfer_location,
            target_book_type_code=target_book,
            fa_state=state,
        )

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

        # State should already be cached from discovery
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
                pending,
                state,
                request_id,
                effective_date,
                dry_run,
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
            log.info(
                "DRY-RUN: IU transfer payload built for asset=%s", pending.asset_number
            )
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
            error=None
            if status_code == "S"
            else f"Fusion X_RETURN_STATUS={status_code}",
        )

    # ------------------------------------------------------------------
    # Batch entry point
    # ------------------------------------------------------------------
    def run_full_sync(
        self,
        books: List[str],
        dry_run: bool = True,
        max_transfers: int = 500,
        bip_params: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Production entry point: discover pending IU transfers via BIP, execute.

        Returns:
            Summary dict with counts and per-asset results.
        """
        started = int(time.time())

        log.info(
            "=== Fusion IU Sync started (books=%s, dry_run=%s) ===",
            books,
            dry_run,
        )

        pending_list = self.find_pending_transfers(
            books=books,
            limit=max_transfers,
            bip_params=bip_params,
        )

        results: List[Dict[str, Any]] = []
        counts = {
            "total": len(pending_list),
            "transferred": 0,
            "failed": 0,
            "dry_run": 0,
        }

        for pt in pending_list:
            result = self.execute_transfer(pt, dry_run=dry_run)
            results.append(
                {
                    "asset_number": result.asset_number,
                    "status": result.status,
                    "source_book": result.source_book,
                    "target_book": result.target_book,
                    "transfer_to_entity": result.transfer_to_entity,
                    "transfer_date": result.transfer_date,
                    "error": result.error,
                }
            )
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
