"""Fusion FA DFF-based IU transfer discovery and execution.

Replaces the CMDB-based sync approach.  Instead of querying ServiceNow CMDB
for asset locations, we read the asset-level Descriptive Flexfields (DFFs)
from an Oracle BI Publisher report (All_IUT_Transfers_Rpt):

  * **Book Type Code**              – P_BOOK_TYPE_CODE              – echoed report parameter at root level
  * **Transfer Date**               – TRANSFER_DATE                 – when the IU transfer should happen
  * **Transfer to Entity**          – TRANSFER_TO_ENTITY            – the destination legal entity
  * **Transfer to Location**        – TRANSFER_TO_LOCATION          – optional location hint
  * **Final Target Book Type Code** – FINAL_TARGET_BOOK_TYPE_CODE   – pre-resolved target book from report
  * **Target Location ID**          – TARGET_LOCATION_ID            – target location ID from report

Architecture:
  1. Call BIP report via SOAP to get asset transfer candidates (DFF populated)
  2. For each candidate, call getAssetInformation to get full asset state
  3. Use FINAL_TARGET_BOOK_TYPE_CODE from report as target book
     (fallback: resolve entity → target book via EntityBookResolver)
  4. Compare current book vs target book
  5. If book mismatch → cross-book transfer via processTransaction-bookTransfer
  6. If same book but location differs → same-book transfer via processTransaction-transferAsset
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Set, Tuple


_PLACEHOLDER_RE = re.compile(r"\$\{([a-zA-Z0-9_]+)\}")


def _interpolate(template: str, substitutions: Dict[str, str]) -> str:
    """Replace ``${key}`` placeholders in ``template`` from ``substitutions``.

    Unknown placeholders are left in place so misconfigurations are
    visible in the resulting BIP parameter values rather than silently
    swallowed.
    """
    def _sub(m: "re.Match[str]") -> str:
        return substitutions.get(m.group(1), m.group(0))
    return _PLACEHOLDER_RE.sub(_sub, template)

from .bip_client import BIPClient
from .entity_resolver import EntityBookResolver
from .exceptions import FusionApiError, ValidationError
from .fusion_ops import (
    AssetState,
    build_book_transfer_params,
    build_same_book_transfer_params,
    bump_underscore_suffix,
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

    # Pre-resolved target fields from report (preferred over entity resolution)
    final_target_book_type_code_col: str = "FINAL_TARGET_BOOK_TYPE_CODE"
    target_location_id_col: str = "TARGET_LOCATION_ID"
    current_location_id_col: str = "CURRENT_LOCATION_ID"
    target_expense_ccid_col: str = "TARGET_EXPENSE_CCID"


DEFAULT_DFF_CONFIG = DFFConfig()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
class TransferClassification:
    """Transfer type classification constants.

    Interunit:  Source entity != target entity  → cross-book transfer
    Intraunit:  Same entity, same book          → location/expense/assignment change
    """

    INTERUNIT = "INTERUNIT"
    INTRAUNIT = "INTRAUNIT"

    @staticmethod
    def classify(
        source_book: str,
        target_book: str,
        source_entity: Optional[str] = None,
        target_entity: Optional[str] = None,
    ) -> str:
        """Determine whether a transfer is interunit or intraunit.

        Rules (from 2026-03-17 alignment):
          - Different books → always INTERUNIT (entity must differ)
          - Same book → INTRAUNIT (location/expense/cost-center change)
          - Same entity but different book is still INTERUNIT (config-driven)
        """
        src = source_book.upper().strip()
        tgt = target_book.upper().strip()
        if src != tgt:
            return TransferClassification.INTERUNIT
        return TransferClassification.INTRAUNIT


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

    # Resolved target
    target_book_type_code: str  # from FINAL_TARGET_BOOK_TYPE_CODE or entity resolver
    target_location_id: Optional[str] = None  # from TARGET_LOCATION_ID in report
    target_expense_ccid: Optional[str] = None  # from TARGET_EXPENSE_CCID in report
    is_cross_book: bool = True  # False = same-book (location-only) transfer

    # Classification: INTERUNIT or INTRAUNIT
    transfer_classification: str = ""

    # Cached state for transfer execution
    fa_state: Optional[AssetState] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.transfer_classification:
            self.transfer_classification = TransferClassification.classify(
                self.book_type_code,
                self.target_book_type_code,
            )


@dataclass
class TransferResult:
    """Result of a single IU transfer operation."""

    asset_number: str
    status: str  # TRANSFERRED, NOOP, DRY_RUN, FAILED
    source_book: Optional[str] = None
    target_book: Optional[str] = None
    transfer_to_entity: Optional[str] = None
    transfer_date: Optional[str] = None
    transfer_classification: str = ""  # INTERUNIT or INTRAUNIT
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
        default_transfer_date: Optional[str] = None,
        blocked_books: Optional[List[str]] = None,
        transfer_overrides: Optional[Dict[str, Any]] = None,
        transfer_overrides_by_book: Optional[Dict[str, Dict[str, Any]]] = None,
        post_transfer_attribute_update: Optional[Dict[str, Any]] = None,
    ):
        self._client = fusion_client
        self._entity_resolver = entity_resolver
        self._bip = bip_client
        self._dff = dff_config or DEFAULT_DFF_CONFIG
        self._default_date = default_transfer_date
        self._blocked_books: Set[str] = {
            b.upper().strip() for b in (blocked_books or []) if b and b.strip()
        }
        # Optional post-bookTransfer update that bumps a DFF on the new
        # asset.  Opt-in via config — see ``_run_post_transfer_attr_update``
        # for the full shape.  Default off so existing tenants are
        # unaffected.
        self._post_attr_cfg: Dict[str, Any] = dict(post_transfer_attribute_update or {})
        # Defaults for the Oracle FA bookTransfer / transferAsset payload
        # (conversion_rate_type, copy_dff_flag, etc.).  Per-row overrides
        # from the BIP report still take precedence — these are only used
        # when the row-level value is empty.
        self._transfer_overrides: Dict[str, Any] = dict(transfer_overrides or {})
        # Per-book overrides, keyed by book code (case-insensitive).  Each
        # entry may carry an ``applies_to`` flag:
        #   * ``"T"`` (default) — applies when the book is the *target*
        #     of the transfer.  Use for ledgers that publish their own
        #     rate type (e.g. JP=Corporate, AU=Spot).
        #   * ``"S"`` — applies when the book is the *source*.  Use for
        #     ledgers whose outbound rules differ from the rest (e.g. SG
        #     where Corporate as a rate type isn't valid for outbound
        #     conversions).
        #   * ``"B"`` — applies in either role.
        # Source overrides are layered first, target overrides second,
        # so when the same key is set on both, target wins.
        self._transfer_overrides_by_book: Dict[str, Dict[str, Any]] = {
            str(k).upper().strip(): dict(v or {})
            for k, v in (transfer_overrides_by_book or {}).items()
        }

    @staticmethod
    def _strip_applies_to(entry: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """Split an override entry into (applies_to, overrides)."""
        applies_to = str(entry.get("applies_to") or "T").upper().strip() or "T"
        if applies_to not in ("S", "T", "B"):
            applies_to = "T"
        return applies_to, {k: v for k, v in entry.items() if k != "applies_to"}

    def _book_overrides_for(
        self, source_book: Optional[str], target_book: Optional[str]
    ) -> Dict[str, Any]:
        """Resolve the per-book overrides that apply to this transfer.

        Source overrides are layered first; target overrides win when
        the same key is set on both sides.
        """
        out: Dict[str, Any] = {}
        src = (source_book or "").upper().strip()
        tgt = (target_book or "").upper().strip()

        src_entry = self._transfer_overrides_by_book.get(src) if src else None
        if src_entry:
            applies_to, ovr = self._strip_applies_to(src_entry)
            if applies_to in ("S", "B"):
                out.update(ovr)

        tgt_entry = self._transfer_overrides_by_book.get(tgt) if tgt else None
        if tgt_entry:
            applies_to, ovr = self._strip_applies_to(tgt_entry)
            if applies_to in ("T", "B"):
                out.update(ovr)
        return out

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
        books_upper: Set[str] = {b.upper().strip() for b in books}

        # Step 1: Run BIP report per book to get transfer candidates.
        # The BIP report requires P_BOOK_TYPE_CODE to filter assets.
        rows: List[Dict[str, str]] = []
        for book in books:
            report_params = dict(bip_params or {})
            report_params["P_BOOK_TYPE_CODE"] = book
            try:
                book_rows = self._bip.run_report(params=report_params)
                log.info("BIP report for book '%s' returned %d row(s)", book, len(book_rows))
                rows.extend(book_rows)
            except FusionApiError as e:
                log.error("BIP report failed for book '%s': %s", book, e)

        log.info("BIP report returned %d total row(s) across %d book(s)", len(rows), len(books))

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
        books_upper: set[str],
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

        # --- Resolve target book ---
        # Prefer FINAL_TARGET_BOOK_TYPE_CODE from the report (pre-resolved).
        # Fall back to entity resolver if not present.
        final_target_book = row.get(dff.final_target_book_type_code_col, "").strip()
        target_location_id = row.get(dff.target_location_id_col, "").strip() or None
        current_location_id = row.get(dff.current_location_id_col, "").strip() or None
        target_expense_ccid = row.get(dff.target_expense_ccid_col, "").strip() or None

        if final_target_book:
            target_book = final_target_book
            log.debug(
                "Using FINAL_TARGET_BOOK_TYPE_CODE='%s' for asset %s",
                target_book,
                asset_number,
            )
        else:
            try:
                target_book = self._entity_resolver.resolve_target_book(transfer_entity)
            except ValidationError as e:
                log.debug("Skipping asset %s: %s", asset_number, e)
                return None

        src_upper = book_type_code.upper().strip()
        tgt_upper = target_book.upper().strip()

        if self._blocked_books and (
            src_upper in self._blocked_books or tgt_upper in self._blocked_books
        ):
            log.info(
                "Skipping asset %s: blocked book (src=%s, tgt=%s, blocked=%s)",
                asset_number,
                book_type_code,
                target_book,
                sorted(self._blocked_books),
            )
            return None

        is_cross_book = src_upper != tgt_upper

        # Same book AND no target location/expense info → nothing to transfer
        if not is_cross_book and not target_location_id and not target_expense_ccid:
            return None

        # Same-book + asked for a location move that's already in place + no
        # CCID change → nothing left to do, skip.  Cross-book transfers are
        # NOT skipped on location-match: the entity/book is what's changing,
        # location may legitimately stay the same.  Same-book + matching
        # location + a TARGET_EXPENSE_CCID is also kept (CCID is the delta).
        if (
            not is_cross_book
            and current_location_id
            and target_location_id
            and current_location_id == target_location_id
            and not target_expense_ccid
        ):
            log.info(
                "Skipping asset %s: same-book, CURRENT_LOCATION_ID (%s) "
                "already equals TARGET_LOCATION_ID and no CCID change — "
                "nothing to do",
                asset_number,
                current_location_id,
            )
            return None

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
            transfer_date=transfer_date_raw or self._default_date or date.today().isoformat(),
            transfer_to_entity=transfer_entity,
            transfer_to_location=transfer_location,
            target_book_type_code=target_book,
            target_location_id=target_location_id,
            target_expense_ccid=target_expense_ccid,
            is_cross_book=is_cross_book,
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
        effective_date = pending.transfer_date or self._default_date or date.today().isoformat()
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

        classification = pending.transfer_classification

        try:
            if pending.is_cross_book:
                result = self._execute_cross_book(
                    pending, state, request_id, effective_date, dry_run,
                )
            else:
                result = self._execute_same_book(
                    pending, state, request_id, effective_date, dry_run,
                )
            result.transfer_classification = classification
            return result
        except Exception as e:
            log.exception("Transfer failed for asset=%s", pending.asset_number)
            return TransferResult(
                asset_number=pending.asset_number,
                status="FAILED",
                source_book=pending.book_type_code,
                target_book=pending.target_book_type_code,
                transfer_to_entity=pending.transfer_to_entity,
                transfer_date=effective_date,
                transfer_classification=classification,
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
            "Cross-book transfer: asset=%s, %s → %s (entity=%s)",
            pending.asset_number,
            pending.book_type_code,
            pending.target_book_type_code,
            pending.transfer_to_entity,
        )

        # Seed per-asset overrides with the run-wide defaults
        # (copy_dff_flag=Y, …) so they land in every Fusion call by
        # default.  Per-row values still win.
        overrides: Dict[str, Any] = dict(self._transfer_overrides)
        # Layer per-book overrides scoped by ``applies_to`` (S/T/B).
        # Source first, target second — target wins when both supply the
        # same key.
        overrides.update(
            self._book_overrides_for(
                source_book=pending.book_type_code,
                target_book=pending.target_book_type_code,
            )
        )
        # Prefer TARGET_LOCATION_ID from report; fall back to TRANSFER_TO_LOCATION DFF
        location_override = pending.target_location_id or pending.transfer_to_location
        if location_override:
            overrides["location_ccid"] = location_override
        if pending.target_expense_ccid:
            overrides["expense_ccid"] = pending.target_expense_ccid

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

        raw, pl = self._client.process_transaction("bookTransfer", params)
        status_code = str(pl.get("X_RETURN_STATUS") or "").strip()
        return_msg = str(pl.get("X_MSG_DATA") or pl.get("X_RETURN_MESSAGE") or "").strip()

        post_update_error: Optional[str] = None
        if status_code == "S":
            post_update_error = self._run_post_transfer_attr_update(
                pending=pending,
                source_state=state,
                request_id=request_id,
                book_transfer_response=pl,
            )

        result_error: Optional[str] = None
        if status_code != "S":
            result_error = f"Fusion X_RETURN_STATUS={status_code}: {return_msg}"
        elif post_update_error:
            # Transfer itself succeeded; surface the post-update problem
            # without flipping the row to FAILED.
            result_error = f"post-transfer attribute update warning: {post_update_error}"

        return TransferResult(
            asset_number=pending.asset_number,
            status="TRANSFERRED" if status_code == "S" else "FAILED",
            source_book=pending.book_type_code,
            target_book=pending.target_book_type_code,
            transfer_to_entity=pending.transfer_to_entity,
            transfer_date=effective_date,
            fusion_response=pl,
            error=result_error,
        )

    # Field names Oracle FA *may* return on the bookTransfer response
    # for the freshly-created destination asset.  Names vary by tenant /
    # patch level, so we try several and let config extend the list.
    _DEFAULT_DEST_ASSET_ID_FIELDS = (
        "X_NEW_ASSET_ID",
        "X_DEST_ASSET_ID",
        "X_DESTINATION_ASSET_ID",
        "X_TARGET_ASSET_ID",
        "X_ASSET_ID",  # last resort: same-id case (CREATE_NEW_ASSET_FLAG=N)
    )
    _DEFAULT_DEST_ASSET_NUMBER_FIELDS = (
        "X_NEW_ASSET_NUMBER",
        "X_DEST_ASSET_NUMBER",
        "X_DESTINATION_ASSET_NUMBER",
        "X_TARGET_ASSET_NUMBER",
        "X_ASSET_NUMBER",
    )

    def _run_post_transfer_attr_update(
        self,
        *,
        pending: PendingTransfer,
        source_state: AssetState,
        request_id: str,
        book_transfer_response: Dict[str, Any],
    ) -> Optional[str]:
        """Post-bookTransfer DFF bump on the newly-created destination asset.

        Returns ``None`` on success (or when the feature is disabled),
        or an error string when the follow-up call fails.  The error is
        attached to the transfer result as a warning — the asset *was*
        transferred, only the DFF bump failed.

        Destination resolution order (most reliable → fallback):
          1. ``dest_lookup_bip`` — a tenant-owned BI Publisher report
             that joins on source_asset_id + target_book and returns
             the new asset_id.  Most reliable in production because
             tenants control the report schema.
          2. ``X_NEW_ASSET_ID`` etc. on the ``bookTransfer`` response
             (field names are tenant-specific; ``dest_asset_id_fields``
             can extend the lookup list).
          3. ``X_NEW_ASSET_NUMBER`` etc. + getAssetInformation against
             the target book to look up the asset_id.
          4. Source asset_number against target book — only valid when
             ``P_CREATE_NEW_ASSET_FLAG=N`` (asset shared across books).

        Config shape (``post_transfer_attribute_update`` on FusionIUSync)::

            {
              "enabled": true,
              "source_field": "attribute10",
              "fusion_parameter": "P_ATTRIBUTE10",
              "operation_handle": "updateAssetDescriptiveDetails",
              "request_id_param": "P_TRX_ATTRIBUTE1",
              "trace_param": "P_TRX_ATTRIBUTE2",
              "trace_value": "OFAM_POST_XBOOK_ATTR_BUMP",
              "dest_lookup_bip": {
                "report_path": "/Custom/.../Get_Transferred_Asset.xdo",
                "params": {
                  "P_SOURCE_ASSET_ID": "${source_asset_id}",
                  "P_TARGET_BOOK_TYPE_CODE": "${target_book_type_code}"
                },
                "asset_id_column": "NEW_ASSET_ID",
                "asset_number_column": "NEW_ASSET_NUMBER"
              },
              "dest_asset_id_fields": ["X_NEW_ASSET_ID", ...],
              "dest_asset_number_fields": ["X_NEW_ASSET_NUMBER", ...],
              "allow_same_asset_number_fallback": true
            }
        """
        cfg = self._post_attr_cfg
        if not cfg or not cfg.get("enabled"):
            return None

        source_field = str(cfg.get("source_field") or "attribute10")
        fusion_param = str(cfg.get("fusion_parameter") or "P_ATTRIBUTE10")
        op_handle = str(cfg.get("operation_handle") or "updateAssetDescriptiveDetails")
        rid_param = str(cfg.get("request_id_param") or "P_TRX_ATTRIBUTE1")
        trace_param = str(cfg.get("trace_param") or "P_TRX_ATTRIBUTE2")
        trace_value = str(cfg.get("trace_value") or "OFAM_POST_XBOOK_ATTR_BUMP")
        allow_fallback = bool(cfg.get("allow_same_asset_number_fallback", True))
        dest_id_fields = tuple(
            cfg.get("dest_asset_id_fields") or self._DEFAULT_DEST_ASSET_ID_FIELDS
        )
        dest_num_fields = tuple(
            cfg.get("dest_asset_number_fields") or self._DEFAULT_DEST_ASSET_NUMBER_FIELDS
        )

        current_value = getattr(source_state, source_field, None)
        bumped = bump_underscore_suffix(current_value)

        dest_asset_id, dest_asset_number, resolution = self._resolve_destination_asset(
            book_transfer_response=book_transfer_response,
            pending=pending,
            source_state=source_state,
            dest_id_fields=dest_id_fields,
            dest_num_fields=dest_num_fields,
            allow_same_asset_number_fallback=allow_fallback,
            dest_lookup_bip=cfg.get("dest_lookup_bip") or {},
        )
        if not dest_asset_id:
            return (
                "could not resolve destination asset_id from bookTransfer "
                f"response (tried fields={list(dest_id_fields)}); "
                "set 'dest_lookup_bip' (recommended) or "
                "'dest_asset_id_fields' in post_transfer_attribute_update, "
                "or enable 'allow_same_asset_number_fallback'"
            )

        log.info(
            "Post-transfer bump asset=%s %s '%s' -> '%s' "
            "(dest_asset_id=%s, dest_asset_number=%s, resolved_via=%s)",
            pending.asset_number,
            fusion_param,
            current_value,
            bumped,
            dest_asset_id,
            dest_asset_number,
            resolution,
        )

        params: Dict[str, Any] = {
            "P_ASSET_ID": dest_asset_id,
            "P_BOOK_TYPE_CODE": pending.target_book_type_code,
            fusion_param: bumped,
            rid_param: f"{request_id}_ATTR_BUMP",
            trace_param: trace_value,
        }

        try:
            _raw, pl = self._client.process_transaction(op_handle, params)
        except FusionApiError as e:
            log.warning(
                "Post-transfer attribute bump failed for asset=%s: %s",
                pending.asset_number,
                e,
            )
            return f"updateAssetDescriptiveDetails call failed: {e}"

        status_code = str(pl.get("X_RETURN_STATUS") or "").strip()
        if status_code != "S":
            msg = str(pl.get("X_MSG_DATA") or pl.get("X_RETURN_MESSAGE") or "").strip()
            log.warning(
                "Post-transfer attribute bump returned %s for asset=%s: %s",
                status_code,
                pending.asset_number,
                msg,
            )
            return f"updateAssetDescriptiveDetails X_RETURN_STATUS={status_code}: {msg}"

        return None

    def _resolve_destination_asset(
        self,
        *,
        book_transfer_response: Dict[str, Any],
        pending: PendingTransfer,
        source_state: AssetState,
        dest_id_fields: Tuple[str, ...],
        dest_num_fields: Tuple[str, ...],
        allow_same_asset_number_fallback: bool,
        dest_lookup_bip: Dict[str, Any],
    ) -> Tuple[Optional[str], Optional[str], str]:
        """Return (asset_id, asset_number, resolution_label).

        Resolution order (most reliable → fallback):
          1. Custom BIP report (``dest_lookup_bip`` block) — when
             configured, this is the canonical way to find the new
             asset.  Tenant builds a small report that joins on
             source_asset_id + target_book and returns the new asset's
             id (and optionally number).
          2. ``X_NEW_ASSET_ID`` etc. directly on the bookTransfer
             response (fragile — field names vary).
          3. ``X_NEW_ASSET_NUMBER`` etc. + getAssetInformation against
             the target book.
          4. Source asset_number against target book — only valid when
             ``P_CREATE_NEW_ASSET_FLAG=N`` (asset shared across books).
        """
        # 1. Custom BIP report — recommended for production.
        if dest_lookup_bip.get("report_path"):
            asset_id, asset_number = self._lookup_dest_via_bip(
                dest_lookup_bip,
                pending=pending,
                source_state=source_state,
            )
            if asset_id:
                return asset_id, asset_number, "bip_report"

        # 2. Direct asset_id from the bookTransfer response.
        for fname in dest_id_fields:
            v = str(book_transfer_response.get(fname) or "").strip()
            if v and v != source_state.asset_id:
                number = self._first_nonempty(book_transfer_response, dest_num_fields)
                return v, number, f"response.{fname}"

        # 3. asset_number from response → getAssetInformation on dest book.
        for fname in dest_num_fields:
            v = str(book_transfer_response.get(fname) or "").strip()
            if v and v != source_state.asset_number:
                try:
                    _r, _pl, dest_state = get_asset_information(
                        self._client, pending.target_book_type_code, v
                    )
                except FusionApiError as e:
                    log.warning(
                        "Post-transfer lookup via response.%s=%s failed: %s",
                        fname, v, e,
                    )
                    continue
                return dest_state.asset_id, v, f"response.{fname}+getAssetInformation"

        # 4. Same-id case: X_ASSET_ID echoed back means CREATE_NEW_ASSET_FLAG=N.
        for fname in dest_id_fields:
            v = str(book_transfer_response.get(fname) or "").strip()
            if v and v == source_state.asset_id:
                return v, source_state.asset_number, f"response.{fname}(same_id)"

        # 5. Last-resort fallback: source asset_number on target book.
        if allow_same_asset_number_fallback:
            try:
                _r, _pl, dest_state = get_asset_information(
                    self._client,
                    pending.target_book_type_code,
                    pending.asset_number,
                )
                return (
                    dest_state.asset_id,
                    pending.asset_number,
                    "fallback.source_asset_number_on_target_book",
                )
            except FusionApiError as e:
                log.warning(
                    "Post-transfer fallback lookup (%s/%s) failed: %s",
                    pending.target_book_type_code,
                    pending.asset_number,
                    e,
                )

        return None, None, "unresolved"

    def _lookup_dest_via_bip(
        self,
        cfg: Dict[str, Any],
        *,
        pending: PendingTransfer,
        source_state: AssetState,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Call the destination-lookup BIP report and parse the first row.

        Config shape::

            {
              "report_path": "/Custom/.../Get_Transferred_Asset.xdo",
              "params": {
                "P_SOURCE_ASSET_ID": "${source_asset_id}",
                "P_TARGET_BOOK_TYPE_CODE": "${target_book_type_code}"
              },
              "asset_id_column": "NEW_ASSET_ID",
              "asset_number_column": "NEW_ASSET_NUMBER"
            }

        Param values support ``${...}`` placeholders that interpolate
        fields from the source asset and the pending transfer:
        ``source_asset_id``, ``source_asset_number``, ``source_book_type_code``,
        ``target_book_type_code``, ``transfer_to_entity``.
        """
        report_path = str(cfg.get("report_path") or "").strip()
        if not report_path:
            return None, None

        substitutions: Dict[str, str] = {
            "source_asset_id": source_state.asset_id or "",
            "source_asset_number": source_state.asset_number or "",
            "source_book_type_code": pending.book_type_code or "",
            "target_book_type_code": pending.target_book_type_code or "",
            "transfer_to_entity": pending.transfer_to_entity or "",
        }
        raw_params = cfg.get("params") or {}
        params: Dict[str, str] = {}
        for k, v in raw_params.items():
            params[str(k)] = _interpolate(str(v), substitutions)

        id_col = str(cfg.get("asset_id_column") or "NEW_ASSET_ID")
        num_col = str(cfg.get("asset_number_column") or "NEW_ASSET_NUMBER")

        try:
            rows = self._bip.run_report(params=params, report_path=report_path)
        except Exception as e:
            log.warning(
                "Destination-lookup BIP report failed (path=%s, params=%s): %s",
                report_path, params, e,
            )
            return None, None

        if not rows:
            log.warning(
                "Destination-lookup BIP returned 0 rows (path=%s, params=%s)",
                report_path, params,
            )
            return None, None

        row = rows[0]
        asset_id = str(row.get(id_col) or "").strip() or None
        asset_number = str(row.get(num_col) or "").strip() or None
        if not asset_id:
            log.warning(
                "Destination-lookup BIP row missing %s column (have: %s)",
                id_col, sorted(row.keys()),
            )
        return asset_id, asset_number

    @staticmethod
    def _first_nonempty(
        d: Dict[str, Any], keys: Tuple[str, ...]
    ) -> Optional[str]:
        for k in keys:
            v = str(d.get(k) or "").strip()
            if v:
                return v
        return None

    def _execute_same_book(
        self,
        pending: PendingTransfer,
        state: AssetState,
        request_id: str,
        effective_date: str,
        dry_run: bool,
    ) -> TransferResult:
        """Execute a same-book transfer (location/distribution change)."""
        log.info(
            "Same-book transfer: asset=%s, book=%s, target_location=%s (entity=%s)",
            pending.asset_number,
            pending.book_type_code,
            pending.target_location_id,
            pending.transfer_to_entity,
        )

        overrides: Dict[str, Any] = {}
        location_override = pending.target_location_id or pending.transfer_to_location
        if location_override:
            overrides["location_ccid"] = location_override
        if pending.target_expense_ccid:
            overrides["expense_ccid"] = pending.target_expense_ccid

        params, is_noop = build_same_book_transfer_params(
            state=state,
            effective_date=effective_date,
            overrides=overrides,
            request_id=request_id,
        )

        if is_noop:
            log.info(
                "NOOP: no distribution changes for asset=%s", pending.asset_number
            )
            return TransferResult(
                asset_number=pending.asset_number,
                status="NOOP",
                source_book=pending.book_type_code,
                target_book=pending.target_book_type_code,
                transfer_to_entity=pending.transfer_to_entity,
                transfer_date=effective_date,
            )

        if dry_run:
            log.info(
                "DRY-RUN: same-book transfer payload built for asset=%s",
                pending.asset_number,
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
        return_msg = str(pl.get("X_MSG_DATA") or pl.get("X_RETURN_MESSAGE") or "").strip()

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
            else f"Fusion X_RETURN_STATUS={status_code}: {return_msg}",
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
                    "transfer_classification": result.transfer_classification,
                    "error": result.error,
                    "fusion_response": result.fusion_response,
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
