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
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .bip_client import BIPClient
from .entity_resolver import EntityBookResolver
from .exceptions import FusionApiError, ValidationError
from .fusion_ops import (
    AssetState,
    apply_underscore_suffix,
    build_book_transfer_params,
    build_same_book_transfer_params,
    bump_underscore_suffix,
    get_asset_information,
    strip_underscore_suffix,
)
from .oracle_client import OracleErpIntegrationsClient

log = logging.getLogger(__name__)


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


def build_account_combination_client_from_config(
    cfg_block: Optional[Dict[str, Any]],
    token_provider: Optional[Callable[[], str]] = None,
) -> Tuple[Optional[Any], Dict[str, str], bool]:
    """Build an ``AccountCombinationServiceClient`` + ledger map from config.

    Returns ``(client, ledger_name_by_book, skip_lov_lookup)``.  Client
    is ``None`` when the config block is missing or ``enabled`` is
    false.  Safe to call even when the SOAP module is not used: the
    import is lazy so deployments that don't enable Option B never pay
    for it.

    ``skip_lov_lookup`` (config key ``skip_lov_lookup``, default
    ``false``) bypasses the REST ``accountCombinationsLOV`` round-trip
    and goes straight to SOAP ``validateAndCreateAccounts``, which
    returns the existing CcId when the combination already exists and
    creates it otherwise.  Enable when the LOV endpoint is flaky on
    your pod.
    """
    if not cfg_block or not cfg_block.get("enabled"):
        return None, {}, False

    from .account_combination_client import (
        AccountCombinationServiceClient,
        AccountCombinationServiceConfig,
    )

    ac_cfg = AccountCombinationServiceConfig.from_dict(cfg_block)
    ac_client = AccountCombinationServiceClient(ac_cfg, token_provider=token_provider)

    ledger_map = {
        str(k): str(v)
        for k, v in (cfg_block.get("ledger_name_by_book") or {}).items()
        if str(k).strip() and str(v).strip()
    }
    skip_lov = bool(cfg_block.get("skip_lov_lookup", False))
    return ac_client, ledger_map, skip_lov


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
    # Target Company segment value (e.g. "SG01") for Option B same-book
    # transfers.  When this column is non-empty and TARGET_EXPENSE_CCID
    # is empty, the pipeline derives the destination CCID by swapping
    # the company segment on the source CCID, looking up the matching
    # combination in the target ledger, and optionally minting a new
    # one via AccountCombinationService.validateAndCreateAccounts.
    target_company_col: str = "TARGET_COMPANY"
    # Prefix for per-segment target columns on the BIP report.  The
    # pipeline scans for ``<prefix>1``, ``<prefix>2``, ... up to
    # ``target_segment_col_max`` and builds a ``{Segment1: ..., ...}``
    # dict.  When non-empty, this skips the company-segment-swap math
    # entirely — the report has already worked out the destination
    # segments, so we just look them up (and optionally auto-create).
    target_segment_col_prefix: str = "TARGET_SEG"
    target_segment_col_max: int = 30
    # GL ledger that owns the destination chart of accounts.  Used by
    # AccountCombinationService.validateAndCreateAccounts.  Sourced
    # per-row from the BIP report; the static ``ledger_name_by_book``
    # config map is only a fallback for rows where this column is blank.
    target_ledger_name_col: str = "TARGET_LEDGER_NAME"


DEFAULT_DFF_CONFIG = DFFConfig()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class TransferControl:
    """Per-classification operational toggle.

    Lets the runner kill-switch any of the four transfer classifications,
    or selected behaviors within them, without a code change.  All flags
    default to ``True`` so an empty / missing ``transfer_controls``
    config preserves the original pre-control behavior.

    Resolution order: global default for the classification → per-book
    source-side block (``applies_to`` in ``S``/``B``) → per-book
    target-side block (``applies_to`` in ``T``/``B``).  Target wins
    on conflict.
    """

    enabled: bool = True
    post_transfer_bump: bool = True
    pre_bip_dff_updates: bool = True

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "TransferControl":
        return TransferControl(
            enabled=bool(d.get("enabled", True)),
            post_transfer_bump=bool(d.get("post_transfer_bump", True)),
            pre_bip_dff_updates=bool(d.get("pre_bip_dff_updates", True)),
        )

    def overlay(self, other: "TransferControl") -> "TransferControl":
        """Return a new TransferControl with ``other``'s values winning."""
        return TransferControl(
            enabled=other.enabled,
            post_transfer_bump=other.post_transfer_bump,
            pre_bip_dff_updates=other.pre_bip_dff_updates,
        )


class TransferClassification:
    """Transfer type classification constants.

    Four classifications across two axes:
      * Entity: same (``INTRAUNIT``) vs different (``INTERUNIT``)
      * Book:   same (``INTRABOOK``) vs different (``INTERBOOK``)

    Backward-compat aliases (``INTERUNIT`` / ``INTRAUNIT``) point at the
    cross-book and same-book quadrants respectively, so existing callers
    keep working.

    Note: until upstream populates ``source_entity`` (TODO — likely a
    ``SOURCE_ENTITY`` column on the BIP report or a reverse lookup of
    ``entity_book_map``), classify() degrades to a 2-way book-only
    split.  Only ``INTRAUNIT_INTERBOOK`` and ``INTRAUNIT_INTRABOOK`` are
    reachable in that mode; the ``INTERUNIT_*`` quadrants light up once
    source_entity is wired through.
    """

    INTERUNIT_INTERBOOK = "INTERUNIT_INTERBOOK"
    INTERUNIT_INTRABOOK = "INTERUNIT_INTRABOOK"
    INTRAUNIT_INTERBOOK = "INTRAUNIT_INTERBOOK"
    INTRAUNIT_INTRABOOK = "INTRAUNIT_INTRABOOK"

    # Backward-compat aliases — existing callers reading
    # ``TransferClassification.INTERUNIT`` / ``.INTRAUNIT`` still work.
    INTERUNIT = INTERUNIT_INTERBOOK
    INTRAUNIT = INTRAUNIT_INTRABOOK

    @staticmethod
    def classify(
        source_book: str,
        target_book: str,
        source_entity: Optional[str] = None,
        target_entity: Optional[str] = None,
    ) -> str:
        """Classify a transfer across the entity x book axes."""
        is_cross_book = (
            source_book.upper().strip() != target_book.upper().strip()
        )
        is_cross_entity = (
            bool(source_entity and target_entity)
            and source_entity.upper().strip() != target_entity.upper().strip()
        )
        if is_cross_entity and is_cross_book:
            return TransferClassification.INTERUNIT_INTERBOOK
        if is_cross_entity and not is_cross_book:
            return TransferClassification.INTERUNIT_INTRABOOK
        if not is_cross_entity and is_cross_book:
            return TransferClassification.INTRAUNIT_INTERBOOK
        return TransferClassification.INTRAUNIT_INTRABOOK


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
    target_company: Optional[str] = None  # from TARGET_COMPANY in report (Option B)
    # From TARGET_SEG1..TARGET_SEGN columns on the report.  Pre-built
    # target combination; pipeline looks it up directly (and creates
    # via SOAP when ``account_combination_service.enabled``).
    target_segments: Optional[Dict[str, str]] = None
    # GL ledger for the destination CoA, from TARGET_LEDGER_NAME column.
    target_ledger_name: Optional[str] = None
    is_cross_book: bool = True  # False = same-book (location-only) transfer

    # Classification: INTERUNIT or INTRAUNIT
    transfer_classification: str = ""

    # Cached state for transfer execution
    fa_state: Optional[AssetState] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.transfer_classification:
            # TODO: derive ``source_entity`` (BIP ``SOURCE_ENTITY`` column,
            # or reverse-lookup of ``entity_book_map``).  Until then
            # ``classify()`` degrades to a 2-way book-only split.
            self.transfer_classification = TransferClassification.classify(
                self.book_type_code,
                self.target_book_type_code,
                source_entity=None,
                target_entity=self.transfer_to_entity,
            )


@dataclass
class TransferResult:
    """Result of a single IU transfer operation."""

    asset_number: str
    status: str  # TRANSFERRED, NOOP, DRY_RUN, FAILED, SKIPPED
    source_book: Optional[str] = None
    target_book: Optional[str] = None
    transfer_to_entity: Optional[str] = None
    transfer_date: Optional[str] = None
    transfer_classification: str = ""  # one of TransferClassification.* values
    error: Optional[str] = None
    fusion_response: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class ChainLink:
    """One asset_id in a transferred asset's lineage chain.

    Populated from the lineage-lookup BIP report, which walks a merged
    edge set — TRUE bookTransfer rows (FA_BOOK_TRANSFER_DISTS) plus
    historical addition+retirement hops recovered from the "Source
    Asset Number" DFF.  Used to resolve the just-created destination
    asset and to read the source link's ``chain_order`` so the retired
    source's Tag Number gets the right ``_N`` suffix.
    """

    asset_id: str
    asset_number: Optional[str]
    book_type_code: Optional[str]
    chain_order: int  # 1 = earliest by creation_date, N = latest
    is_destination: bool  # True for the asset just created by this transfer


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
        account_combination_client: Optional[Any] = None,
        ledger_name_by_book: Optional[Dict[str, str]] = None,
        company_segment_key: str = "Segment1",
        ac_skip_lov_lookup: bool = False,
        transfer_controls: Optional[Dict[str, Any]] = None,
        transfer_controls_by_book: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        self._client = fusion_client
        self._entity_resolver = entity_resolver
        self._bip = bip_client
        self._dff = dff_config or DEFAULT_DFF_CONFIG
        self._default_date = default_transfer_date
        self._blocked_books: Set[str] = {
            b.upper().strip() for b in (blocked_books or []) if b and b.strip()
        }
        # Optional Option B plumbing.  When the BIP report includes a
        # TARGET_COMPANY column, same-book rows are routed through the
        # company-segment-swap path.  When ``account_combination_client``
        # is provided, missing combinations in the destination ledger
        # are auto-created via SOAP validateAndCreateAccounts.
        self._ac_client = account_combination_client
        self._ledger_name_by_book: Dict[str, str] = {
            str(k).upper().strip(): str(v)
            for k, v in (ledger_name_by_book or {}).items()
            if str(k).strip() and str(v).strip()
        }
        self._company_segment_key = company_segment_key or "Segment1"
        # When True, the resolver skips the REST accountCombinationsLOV
        # call and goes straight to SOAP validateAndCreateAccounts —
        # which returns the existing CcId if the combination exists, or
        # creates it.  Useful on pods where the LOV endpoint is flaky.
        self._ac_skip_lov_lookup = bool(ac_skip_lov_lookup)
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
        # Global default controls per classification.  Each value is a
        # dict shaped like ``{"enabled": bool, "post_transfer_bump":
        # bool, "pre_bip_dff_updates": bool}``; missing keys default to
        # True via ``TransferControl.from_dict``.  An empty / missing
        # config preserves the original pre-control behavior.
        self._transfer_controls: Dict[str, TransferControl] = {
            str(k): TransferControl.from_dict(v)
            for k, v in (transfer_controls or {}).items()
            if isinstance(v, dict) and k != "applies_to"
        }
        # Per-book overrides for transfer controls, keyed by book code
        # (case-insensitive).  Each book entry may carry an
        # ``applies_to`` flag (``S`` / ``T`` / ``B``) plus per-
        # classification blocks.  Resolution mirrors
        # ``transfer_overrides_by_book``: source first, target second
        # (target wins on conflict).
        self._transfer_controls_by_book: Dict[str, Dict[str, Any]] = {
            str(k).upper().strip(): dict(v or {})
            for k, v in (transfer_controls_by_book or {}).items()
        }

    @staticmethod
    def _strip_applies_to(entry: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """Split an override entry into (applies_to, overrides)."""
        applies_to = str(entry.get("applies_to") or "T").upper().strip() or "T"
        if applies_to not in ("S", "T", "B"):
            applies_to = "T"
        return applies_to, {k: v for k, v in entry.items() if k != "applies_to"}

    def _make_account_combination_creator(self) -> Callable[[str, Dict[str, str]], int]:
        """Adapt the SOAP client into the resolver's creator signature.

        ``ccid_resolver`` expects ``(ledger_name, segments) -> int``.
        The SOAP client returns a ``CreatedCombination`` with status +
        error info; we translate ``Status=E`` into ``FusionApiError`` so
        the transfer fails with a clear message instead of silently
        returning ``None``.
        """
        ac_client = self._ac_client
        if ac_client is None:
            raise RuntimeError(  # pragma: no cover — guarded by callers
                "AccountCombinationServiceClient not configured"
            )

        def _creator(ledger_name: str, segments: Dict[str, str]) -> int:
            result = ac_client.validate_and_create(
                ledger_name=ledger_name, segments=segments
            )
            # Oracle's Status is "Valid" / "New" / "Invalid" — both
            # Valid and New return a CcId; Invalid does not.  Gate on
            # the CcId (via is_success), NOT on a literal status code.
            if not result.is_success:
                raise FusionApiError(
                    f"AccountCombinationService rejected segments {segments} "
                    f"(ledger={ledger_name}): status={result.status} "
                    f"code={result.error_code} msg={result.error_message}"
                )
            log.info(
                "AccountCombinationService %s CCID=%s for ledger=%s segments=%s",
                result.status or "(no status)",
                result.ccid,
                ledger_name,
                segments,
            )
            return int(result.ccid)

        return _creator

    def _resolve_expense_ccid_from_segments(
        self, pending: "PendingTransfer"
    ) -> Optional[str]:
        """Resolve the destination expense CCID from TARGET_SEG* columns.

        Used by BOTH the cross-book and same-book paths.  Returns the
        CCID as a string when the report supplied ``TARGET_SEG*``
        columns and no explicit ``TARGET_EXPENSE_CCID`` override, or
        ``None`` when there's nothing to resolve (caller then keeps the
        source / report-supplied CCID).

        When ``account_combination_service`` is wired, a missing
        combination is minted via SOAP ``validateAndCreateAccounts``.
        """
        if not pending.target_segments or pending.target_expense_ccid:
            return None

        from .ccid_resolver import resolve_target_ccid_from_segments

        # Ledger: prefer the per-row TARGET_LEDGER_NAME column; fall back
        # to the static ledger_name_by_book config map.
        ledger_name = pending.target_ledger_name or self._ledger_name_by_book.get(
            (pending.target_book_type_code or "").upper().strip()
        )
        creator = None
        if self._ac_client is not None and ledger_name:
            creator = self._make_account_combination_creator()

        log.info(
            "Resolving target CCID from report TARGET_SEG* columns: "
            "asset=%s segments=%s ledger=%s auto_create=%s",
            pending.asset_number,
            pending.target_segments,
            ledger_name or "(none)",
            "yes" if creator is not None else "no",
        )
        target_ccid = resolve_target_ccid_from_segments(
            self._client,
            pending.target_segments,
            creator=creator,
            ledger_name=ledger_name,
            skip_lov_lookup=self._ac_skip_lov_lookup,
        )
        return str(target_ccid)

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

    def _resolve_transfer_control(
        self,
        classification: str,
        source_book: Optional[str],
        target_book: Optional[str],
    ) -> TransferControl:
        """Resolve the effective :class:`TransferControl` for a transfer.

        Resolution order: global default for the classification →
        per-book source-side block (``applies_to`` in ``S`` / ``B``) →
        per-book target-side block (``applies_to`` in ``T`` / ``B``).
        Target wins on conflict.
        """
        base = self._transfer_controls.get(classification, TransferControl())

        src = (source_book or "").upper().strip()
        tgt = (target_book or "").upper().strip()

        src_entry = self._transfer_controls_by_book.get(src) if src else None
        if src_entry:
            applies_to = (
                str(src_entry.get("applies_to", "T")).upper().strip() or "T"
            )
            if applies_to in ("S", "B"):
                cls_block = src_entry.get(classification)
                if isinstance(cls_block, dict):
                    base = TransferControl(
                        enabled=bool(cls_block.get("enabled", base.enabled)),
                        post_transfer_bump=bool(
                            cls_block.get(
                                "post_transfer_bump", base.post_transfer_bump
                            )
                        ),
                        pre_bip_dff_updates=bool(
                            cls_block.get(
                                "pre_bip_dff_updates", base.pre_bip_dff_updates
                            )
                        ),
                    )

        tgt_entry = self._transfer_controls_by_book.get(tgt) if tgt else None
        if tgt_entry:
            applies_to = (
                str(tgt_entry.get("applies_to", "T")).upper().strip() or "T"
            )
            if applies_to in ("T", "B"):
                cls_block = tgt_entry.get(classification)
                if isinstance(cls_block, dict):
                    base = TransferControl(
                        enabled=bool(cls_block.get("enabled", base.enabled)),
                        post_transfer_bump=bool(
                            cls_block.get(
                                "post_transfer_bump", base.post_transfer_bump
                            )
                        ),
                        pre_bip_dff_updates=bool(
                            cls_block.get(
                                "pre_bip_dff_updates", base.pre_bip_dff_updates
                            )
                        ),
                    )

        return base

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
                log.info(
                    "BIP report for book '%s' returned %d row(s)", book, len(book_rows)
                )
                rows.extend(book_rows)
            except FusionApiError as e:
                log.error("BIP report failed for book '%s': %s", book, e)

        log.info(
            "BIP report returned %d total row(s) across %d book(s)",
            len(rows),
            len(books),
        )

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
        target_company = row.get(dff.target_company_col, "").strip() or None
        target_ledger_name = row.get(dff.target_ledger_name_col, "").strip() or None

        # Scan TARGET_SEG1..TARGET_SEGn into a {SegmentN: value} dict.
        # The BIP report ships these once it has resolved each row's
        # destination segments (per the screenshot the user shared).
        target_segments: Dict[str, str] = {}
        for n in range(1, dff.target_segment_col_max + 1):
            col = f"{dff.target_segment_col_prefix}{n}"
            val = str(row.get(col, "") or "").strip()
            if val:
                target_segments[f"Segment{n}"] = val
        target_segments_or_none = target_segments or None

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

        # Same book AND no target location/expense/company/segments info → nothing to transfer
        if (
            not is_cross_book
            and not target_location_id
            and not target_expense_ccid
            and not target_company
            and not target_segments_or_none
        ):
            return None

        # Same-book + asked for a location move that's already in place + no
        # CCID change + no Company swap → nothing left to do, skip.  Cross-book
        # transfers are NOT skipped on location-match: the entity/book is what's
        # changing, location may legitimately stay the same.  Same-book +
        # matching location + a TARGET_EXPENSE_CCID or TARGET_COMPANY is also
        # kept (CCID is the delta).
        if (
            not is_cross_book
            and current_location_id
            and target_location_id
            and current_location_id == target_location_id
            and not target_expense_ccid
            and not target_company
            and not target_segments_or_none
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
            transfer_date=transfer_date_raw
            or self._default_date
            or date.today().isoformat(),
            transfer_to_entity=transfer_entity,
            transfer_to_location=transfer_location,
            target_book_type_code=target_book,
            target_location_id=target_location_id,
            target_expense_ccid=target_expense_ccid,
            target_company=target_company,
            target_segments=target_segments_or_none,
            target_ledger_name=target_ledger_name,
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
        effective_date = (
            pending.transfer_date or self._default_date or date.today().isoformat()
        )
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

        control = self._resolve_transfer_control(
            classification,
            pending.book_type_code,
            pending.target_book_type_code,
        )
        if not control.enabled:
            log.info(
                "SKIPPED asset=%s: transfer_controls disabled for %s "
                "(src=%s, tgt=%s)",
                pending.asset_number,
                classification,
                pending.book_type_code,
                pending.target_book_type_code,
            )
            return TransferResult(
                asset_number=pending.asset_number,
                status="SKIPPED",
                source_book=pending.book_type_code,
                target_book=pending.target_book_type_code,
                transfer_to_entity=pending.transfer_to_entity,
                transfer_date=effective_date,
                transfer_classification=classification,
            )

        try:
            if pending.is_cross_book:
                result = self._execute_cross_book(
                    pending,
                    state,
                    request_id,
                    effective_date,
                    dry_run,
                )
            else:
                result = self._execute_same_book(
                    pending,
                    state,
                    request_id,
                    effective_date,
                    dry_run,
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
        else:
            # No explicit TARGET_EXPENSE_CCID — when the report supplies
            # TARGET_SEG* columns, resolve the destination CCID (minting
            # it via AccountCombinationService when missing).  Without
            # this the cross-book payload would carry the SOURCE ledger's
            # CCID, which Oracle rejects ("balancing segment value not
            # valid for ledger ...").
            resolved_ccid = self._resolve_expense_ccid_from_segments(pending)
            if resolved_ccid:
                overrides["expense_ccid"] = resolved_ccid

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
        return_msg = str(
            pl.get("X_MSG_DATA") or pl.get("X_RETURN_MESSAGE") or ""
        ).strip()

        # Loud diagnostic so an operator can always tell from the log
        # whether the follow-up update was even reachable.  Three cases:
        #   1. feature disabled in config        -> CONFIG_DISABLED
        #   2. enabled but bookTransfer != S     -> BOOKTRANSFER_FAILED
        #   3. enabled and bookTransfer == S     -> ATTEMPTING (helper takes over)
        control = self._resolve_transfer_control(
            pending.transfer_classification,
            pending.book_type_code,
            pending.target_book_type_code,
        )
        post_attr_enabled = (
            bool(self._post_attr_cfg.get("enabled"))
            and control.post_transfer_bump
        )
        if not post_attr_enabled:
            log.info(
                "Post-transfer attr-bump CONFIG_DISABLED asset=%s "
                "(set post_transfer_attribute_update.enabled=true and "
                "transfer_controls[%s].post_transfer_bump=true to enable)",
                pending.asset_number,
                pending.transfer_classification,
            )
        elif status_code != "S":
            log.info(
                "Post-transfer attr-bump SKIPPED asset=%s: bookTransfer "
                "X_RETURN_STATUS=%s (must be 'S'); msg=%s",
                pending.asset_number,
                status_code or "(empty)",
                return_msg or "(none)",
            )

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
            result_error = (
                f"post-transfer attribute update warning: {post_update_error}"
            )

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
        """Post-bookTransfer tag bump on the newly-created destination asset.

        Returns ``None`` on success (or when the feature is disabled),
        or an error string when the follow-up call fails.  The error is
        attached to the transfer result as a warning — the asset *was*
        transferred, only the tag bump failed.

        **Bump direction.**  Oracle FA enforces tag uniqueness across
        active assets.  After a cross-book transfer, both the retired
        source and the newly-created destination would otherwise carry
        the same canonical tag — a collision.  We resolve it by
        suffixing the **retired source** with ``_<N>`` (where N is its
        position in the lineage chain) and giving the **destination**
        the clean canonical tag.

        Per the expected-behaviour contract::

            Transfer 1:  A (retired) -> A_1   ;  B (new active) -> A
            Transfer 2:  B (retired) -> A_2   ;  C (new active) -> A
            Transfer 3:  C (retired) -> A_3   ;  D (new active) -> A

        Each transfer issues exactly two ``updateAssetDescriptiveDetails``
        calls — one against the retired source and one against the new
        destination.  Earlier retired ancestors (A, B in transfer 3
        above) are NOT touched; they already carry their own historical
        suffix from when they were the source of an earlier transfer.

        The suffix number ``N`` comes from the source's ``chain_order``
        in the lineage BIP report.  When the chain BIP is unavailable
        the code falls back to ``bump_underscore_suffix(source_tag)`` —
        less accurate, but adequate for first-time transfers.

        **Empty source tag.**  When the source asset has no tag at all,
        the Tag Number bump is skipped — no ``_N`` on the source, no tag
        POSTed to the destination — so a previously-untagged asset is
        never seeded with ``_1``.  The configured ``populate_*_dffs`` /
        ``clear_*_dffs`` updates still run; the whole follow-up is
        skipped only when there is also no DFF work configured.

        Tenants that *also* maintain a CMDB tag identifier in a DFF
        (e.g. ``X_CAT_ATTRIBUTE7`` at Citadel) can mirror the same
        per-side values into that DFF via ``also_bump_params`` (the
        source-side update gets the suffixed value, the destination-side
        update gets the clean value).

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
              "source_field": "tag_number",
              "fusion_parameter": "P_TAG_NUMBER",
              "also_bump_params": ["P_CAT_ATTRIBUTE7"],
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
            # The wrapper in _execute_cross_book already logs
            # CONFIG_DISABLED at INFO; nothing to add here.
            return None

        # Defaults target the canonical Oracle FA header Tag Number —
        # the field with uniqueness behaviour that feeds the FA asset
        # reports.  Bumping a DFF instead leaves the header Tag Number
        # blank on the destination (Fusion's bookTransfer doesn't copy
        # it), which is why tenants saw the tag "lost" pre-fix.
        source_field = str(cfg.get("source_field") or "tag_number")
        fusion_param = str(cfg.get("fusion_parameter") or "P_TAG_NUMBER")
        op_handle = str(cfg.get("operation_handle") or "updateAssetDescriptiveDetails")
        rid_param = str(cfg.get("request_id_param") or "P_TRX_ATTRIBUTE1")
        trace_param = str(cfg.get("trace_param") or "P_TRX_ATTRIBUTE2")
        trace_value = str(cfg.get("trace_value") or "OFAM_POST_XBOOK_ATTR_BUMP")
        allow_fallback = bool(cfg.get("allow_same_asset_number_fallback", True))
        dest_id_fields = tuple(
            cfg.get("dest_asset_id_fields") or self._DEFAULT_DEST_ASSET_ID_FIELDS
        )
        dest_num_fields = tuple(
            cfg.get("dest_asset_number_fields")
            or self._DEFAULT_DEST_ASSET_NUMBER_FIELDS
        )

        log.info(
            "Post-transfer attr-bump START asset=%s source_field=%s "
            "fusion_parameter=%s op_handle=%s",
            pending.asset_number,
            source_field,
            fusion_param,
            op_handle,
        )

        # Read the source value.  Try direct attribute first — this
        # covers the canonical ``tag_number`` (X_TAG_NUMBER) field as
        # well as the back-compat ``attribute10`` shortcut.  Then fall
        # back to the generic ``attributes`` dict so configs can target
        # ATTRIBUTE7, CAT_ATTRIBUTE7, etc. without code changes.
        current_value = getattr(source_state, source_field, None)
        if not current_value:
            current_value = (source_state.attributes or {}).get(source_field)
        if not current_value:
            # Also accept upper-cased form, in case someone writes
            # ``source_field: "ATTRIBUTE7"`` in their config.
            current_value = (source_state.attributes or {}).get(source_field.lower())

        # An empty source tag no longer aborts the whole follow-up.  The
        # destination still needs its Source Asset Book/Number DFFs
        # populated and its directive DFFs cleared — only the Tag Number
        # bump is skipped (the source keeps no _N suffix, the
        # destination is POSTed no tag at all, so a previously-untagged
        # asset is never seeded with "_1").
        has_tag = bool(current_value)

        # The one true "nothing to do" case: no tag to bump AND no
        # populate/clear DFFs configured on either side.
        has_dff_work = bool(
            cfg.get("populate_dest_dffs")
            or cfg.get("clear_dest_dffs")
            or cfg.get("populate_source_dffs")
            or cfg.get("clear_source_dffs")
        )
        if not has_tag and not has_dff_work:
            log.info(
                "Post-transfer attr-bump SKIPPED asset=%s: source %s is empty "
                "and no populate/clear DFFs are configured — nothing to do "
                "(available DFFs on source: %s).",
                pending.asset_number,
                source_field,
                sorted((source_state.attributes or {}).keys()) or "(none)",
            )
            return None
        if not has_tag:
            log.info(
                "Post-transfer attr-bump asset=%s: source %s is empty — "
                "leaving Tag Number untouched, still applying the configured "
                "populate/clear DFF updates.",
                pending.asset_number,
                source_field,
            )

        # Resolve destination + lineage chain via the BIP report.  The
        # chain (when present) gives us each link's chain_order so we
        # can assign deterministic suffixes by position rather than
        # naive string-bumping.
        dest_asset_id, dest_asset_number, resolution, chain = (
            self._resolve_destination_asset(
                book_transfer_response=book_transfer_response,
                pending=pending,
                source_state=source_state,
                dest_id_fields=dest_id_fields,
                dest_num_fields=dest_num_fields,
                allow_same_asset_number_fallback=allow_fallback,
                dest_lookup_bip=cfg.get("dest_lookup_bip") or {},
            )
        )
        if not dest_asset_id:
            log.warning(
                "Post-transfer attr-bump UNRESOLVED asset=%s: no destination "
                "asset_id found (tried response.fields=%s).  Configure "
                "'dest_lookup_bip' (recommended), extend "
                "'dest_asset_id_fields' to your tenant's field name, or "
                "enable 'allow_same_asset_number_fallback'.",
                pending.asset_number,
                list(dest_id_fields),
            )
            return (
                "could not resolve destination asset_id from bookTransfer "
                f"response (tried fields={list(dest_id_fields)}); "
                "set 'dest_lookup_bip' (recommended) or "
                "'dest_asset_id_fields' in post_transfer_attribute_update, "
                "or enable 'allow_same_asset_number_fallback'"
            )

        log.info(
            "Post-transfer attr-bump DEST asset=%s dest_asset_id=%s "
            "dest_asset_number=%s resolved_via=%s",
            pending.asset_number,
            dest_asset_id,
            dest_asset_number,
            resolution,
        )

        # ---- Read config knobs ----
        clear_dest_dffs: Dict[str, str] = {
            str(k): str(v) for k, v in (cfg.get("clear_dest_dffs") or {}).items()
        }
        clear_source_dffs: Dict[str, str] = {
            str(k): str(v) for k, v in (cfg.get("clear_source_dffs") or {}).items()
        }
        # ``also_bump_params`` are secondary fusion parameters that
        # receive the *same* tag value as the primary (P_TAG_NUMBER).
        # Typical use: mirror into the CMDB tag DFF ("P_CAT_ATTRIBUTE7")
        # so the asset header and CMDB record stay in lockstep.
        also_bump_params: List[str] = [
            str(p) for p in (cfg.get("also_bump_params") or []) if str(p).strip()
        ]
        # ---- Compute the canonical original tag + per-side values ----
        #
        # Contract (per agreed expected-behaviour table):
        #
        #   Transfer N: source asset gets <original>_<chain_order>;
        #               destination asset gets <original> (clean).
        #
        # The retired source vacates the canonical tag (FA tag-uniqueness)
        # so the new active destination can hold it unsuffixed.  Each
        # retired ancestor keeps its own historical suffix from when it
        # was the source of an earlier transfer; we don't touch them.
        #
        # All three stay empty when the source has no tag (see has_tag):
        # the tag params are then left off both POSTs entirely.
        original_tag = ""
        source_new_tag = ""
        destination_new_tag = ""
        if has_tag:
            original_tag = strip_underscore_suffix(current_value)

            # Find the source link in the chain to read its chain_order.
            # When chain BIP is unavailable, fall back to bumping the
            # source's existing suffix by 1 (less accurate — can't
            # detect a "clean tag after previous transfer" case).
            source_chain_order: Optional[int] = None
            for link in chain:
                if str(link.asset_id) == str(source_state.asset_id):
                    source_chain_order = link.chain_order
                    break

            if source_chain_order is not None:
                source_new_tag = apply_underscore_suffix(
                    original_tag, source_chain_order
                )
                suffix_resolution = f"chain_order={source_chain_order}"
            else:
                source_new_tag = bump_underscore_suffix(current_value)
                suffix_resolution = "string-bumped (chain unavailable)"

            destination_new_tag = original_tag

            log.info(
                "Post-transfer attr-bump VALUES asset=%s original='%s' "
                "source_new='%s' dest_new='%s' (%s)",
                pending.asset_number,
                original_tag,
                source_new_tag,
                destination_new_tag,
                suffix_resolution,
            )

        # ``populate_dest_dffs`` / ``populate_source_dffs`` are dicts of
        # extra DFF params to set on the update calls.  Values support
        # ``${...}`` templating — typically used to fill "Source Asset
        # Book" / "Source Asset Number" DFFs on the destination so the
        # new asset record points back at where it came from.
        #
        # ``${current_date}`` resolves to today as YYYY-MM-DD.  Use it to
        # stamp a DATE-typed DFF on the destination: Oracle FA treats an
        # empty string as "no change" for DATE columns, so such a field
        # can't be blanked — defaulting it to the current date is the
        # workable alternative.
        substitutions: Dict[str, str] = {
            "source_asset_id": source_state.asset_id or "",
            "source_asset_number": source_state.asset_number or "",
            "source_book_type_code": pending.book_type_code or "",
            "target_book_type_code": pending.target_book_type_code or "",
            "target_asset_id": str(dest_asset_id) or "",
            "target_asset_number": str(dest_asset_number or "") or "",
            "transfer_to_entity": pending.transfer_to_entity or "",
            "transfer_date": pending.transfer_date or "",
            "current_date": date.today().isoformat(),
            "original_tag": original_tag,
            "source_new_tag": source_new_tag,
            "destination_new_tag": destination_new_tag,
        }
        populate_dest_dffs: Dict[str, str] = {
            str(k): _interpolate(str(v), substitutions)
            for k, v in (cfg.get("populate_dest_dffs") or {}).items()
        }
        populate_source_dffs: Dict[str, str] = {
            str(k): _interpolate(str(v), substitutions)
            for k, v in (cfg.get("populate_source_dffs") or {}).items()
        }

        # ---- Build the two POSTs: source (suffixed) + destination (clean) ----
        #
        # Retired ancestors in the chain are NOT touched.  They already
        # carry their own historical suffixes from when they were the
        # source of an earlier transfer; rewriting them would lose audit
        # state and risks creating new uniqueness collisions.
        errors: List[str] = []

        # Source POST — retired asset gets the suffixed tag.  The tag
        # params (primary + also_bump_params) are included only when the
        # source actually had a tag; the POST is skipped entirely when
        # there's neither a tag nor any source-side DFF, since what's
        # left would be only request-id/trace bookkeeping.
        source_params: Dict[str, Any] = {
            "P_ASSET_ID": source_state.asset_id,
            "P_BOOK_TYPE_CODE": pending.book_type_code,
            rid_param: f"{request_id}_ATTR_BUMP_SRC",
            trace_param: trace_value,
        }
        if has_tag:
            source_params[fusion_param] = source_new_tag
            for p in also_bump_params:
                source_params[p] = source_new_tag
        source_params.update(populate_source_dffs)
        source_params.update(clear_source_dffs)
        if has_tag or populate_source_dffs or clear_source_dffs:
            src_err = self._post_attr_update_call(
                pending=pending,
                params=source_params,
                op_handle=op_handle,
                side="source",
                asset_id=source_state.asset_id,
            )
            if src_err:
                errors.append(f"source: {src_err}")

        # Destination POST — new asset gets the clean canonical tag +
        # populated source-info DFFs + cleared directive DFFs.  The tag
        # params are likewise included only when the source had a tag;
        # populate/clear DFF updates run regardless.
        dest_params: Dict[str, Any] = {
            "P_ASSET_ID": dest_asset_id,
            "P_BOOK_TYPE_CODE": pending.target_book_type_code,
            rid_param: f"{request_id}_ATTR_BUMP_DST",
            trace_param: trace_value,
        }
        if has_tag:
            dest_params[fusion_param] = destination_new_tag
            for p in also_bump_params:
                dest_params[p] = destination_new_tag
        # Populate-then-clear ordering: if a misconfig lists the same
        # param in both blocks, clear wins (safer outcome).
        dest_params.update(populate_dest_dffs)
        dest_params.update(clear_dest_dffs)
        if has_tag or populate_dest_dffs or clear_dest_dffs:
            dest_err = self._post_attr_update_call(
                pending=pending,
                params=dest_params,
                op_handle=op_handle,
                side="destination",
                asset_id=dest_asset_id,
            )
            if dest_err:
                errors.append(f"destination: {dest_err}")

        if errors:
            return "; ".join(errors)
        return None

    def _post_attr_update_call(
        self,
        *,
        pending: PendingTransfer,
        params: Dict[str, Any],
        op_handle: str,
        side: str,
        asset_id: str,
    ) -> Optional[str]:
        """POST one updateAssetDescriptiveDetails call.

        ``side`` is "source" or "destination" — used only for log lines.
        Returns ``None`` on success, an error string on failure.  Never
        raises — failures are bubbled up as warning strings so the
        outer transfer status stays TRANSFERRED.
        """
        log.info(
            "Post-transfer attr-bump POST asset=%s side=%s asset_id=%s op=%s "
            "params=%s (captured in --debug dumps as a NNNN-POST-%s.json file)",
            pending.asset_number,
            side,
            asset_id,
            op_handle,
            {k: v for k, v in params.items() if k != "P_TRX_ATTRIBUTE1"},
            op_handle,
        )

        try:
            _raw, pl = self._client.process_transaction(op_handle, params)
        except FusionApiError as e:
            log.warning(
                "Post-transfer attr-bump CALL FAILED asset=%s side=%s: %s",
                pending.asset_number,
                side,
                e,
            )
            return f"updateAssetDescriptiveDetails call failed: {e}"

        status_code = str(pl.get("X_RETURN_STATUS") or "").strip()
        if status_code != "S":
            msg = str(pl.get("X_MSG_DATA") or pl.get("X_RETURN_MESSAGE") or "").strip()
            log.warning(
                "Post-transfer attr-bump RETURN=%s asset=%s side=%s msg=%s",
                status_code,
                pending.asset_number,
                side,
                msg,
            )
            return f"updateAssetDescriptiveDetails X_RETURN_STATUS={status_code}: {msg}"

        log.info(
            "Post-transfer attr-bump SUCCESS asset=%s side=%s asset_id=%s",
            pending.asset_number,
            side,
            asset_id,
        )
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
    ) -> Tuple[Optional[str], Optional[str], str, List[ChainLink]]:
        """Return (asset_id, asset_number, resolution_label, chain).

        ``chain`` is the full lineage chain returned by the BIP report
        (empty list when BIP isn't used or returned 0 rows).  Consumers
        that want chain-wide tag bumps read it directly; destination-only
        consumers can ignore it.

        Resolution order (most reliable → fallback):
          1. Custom BIP report (``dest_lookup_bip`` block) — when
             configured, this is the canonical way to find the new
             asset.  The report returns the full lineage chain
             (FA_BOOK_TRANSFER_DISTS recursive walk); we pick the row
             whose BOOK_TYPE_CODE matches the in-flight target book.
          2. ``X_NEW_ASSET_ID`` etc. directly on the bookTransfer
             response (fragile — field names vary).
          3. ``X_NEW_ASSET_NUMBER`` etc. + getAssetInformation against
             the target book.
          4. Source asset_number against target book — only valid when
             ``P_CREATE_NEW_ASSET_FLAG=N`` (asset shared across books).
        """
        # 1. Custom BIP report — recommended for production.  Returns
        #    the full chain in one call; destination is the link whose
        #    book matches the in-flight transfer's target book.
        chain: List[ChainLink] = []
        if dest_lookup_bip.get("report_path"):
            chain = self._fetch_lineage_chain(
                dest_lookup_bip,
                pending=pending,
                source_state=source_state,
            )
            dest_link = self._pick_destination_from_chain(
                chain, target_book=pending.target_book_type_code
            )
            if dest_link is not None:
                return (
                    dest_link.asset_id,
                    dest_link.asset_number,
                    "bip_report",
                    chain,
                )

        # 2. Direct asset_id from the bookTransfer response.
        for fname in dest_id_fields:
            v = str(book_transfer_response.get(fname) or "").strip()
            if v and v != source_state.asset_id:
                number = self._first_nonempty(book_transfer_response, dest_num_fields)
                return v, number, f"response.{fname}", chain

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
                        fname,
                        v,
                        e,
                    )
                    continue
                return (
                    dest_state.asset_id,
                    v,
                    f"response.{fname}+getAssetInformation",
                    chain,
                )

        # 4. Same-id case: X_ASSET_ID echoed back means CREATE_NEW_ASSET_FLAG=N.
        for fname in dest_id_fields:
            v = str(book_transfer_response.get(fname) or "").strip()
            if v and v == source_state.asset_id:
                return v, source_state.asset_number, f"response.{fname}(same_id)", chain

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
                    chain,
                )
            except FusionApiError as e:
                log.warning(
                    "Post-transfer fallback lookup (%s/%s) failed: %s",
                    pending.target_book_type_code,
                    pending.asset_number,
                    e,
                )

        return None, None, "unresolved", chain

    @staticmethod
    def _pick_destination_from_chain(
        chain: List[ChainLink],
        *,
        target_book: Optional[str],
    ) -> Optional[ChainLink]:
        """Pick the destination link from a lineage chain.

        Preference order:
          1. ``is_destination=True`` AND ``book_type_code == target_book``
          2. Any link with ``book_type_code == target_book``, picking
             the latest by ``chain_order``.
          3. The link with the highest ``chain_order`` whose
             ``is_destination=True`` (book column missing).
          4. Back-compat: when no row carries ``book_type_code`` or
             ``is_destination``, treat the highest-``chain_order`` row
             as the destination.  This matches the old single-row
             destination-only report shape.
        """
        if not chain:
            return None
        tgt = (target_book or "").upper().strip()
        if tgt:
            matches = [
                link
                for link in chain
                if (link.book_type_code or "").upper().strip() == tgt
            ]
            destination_matches = [m for m in matches if m.is_destination]
            if destination_matches:
                return max(destination_matches, key=lambda m: m.chain_order)
            if matches:
                return max(matches, key=lambda m: m.chain_order)
        dest_flagged = [link for link in chain if link.is_destination]
        if dest_flagged:
            return max(dest_flagged, key=lambda m: m.chain_order)
        # Old report shape: no book / is_destination columns at all.
        any_book = any(link.book_type_code for link in chain)
        any_flag = any(link.is_destination for link in chain)
        if not any_book and not any_flag:
            return max(chain, key=lambda m: m.chain_order)
        return None

    def _fetch_lineage_chain(
        self,
        cfg: Dict[str, Any],
        *,
        pending: PendingTransfer,
        source_state: AssetState,
    ) -> List[ChainLink]:
        """Call the lineage-lookup BIP report and parse all rows into a chain.

        One report serves two purposes: pick the destination asset of
        the just-completed transfer, and (when ``bump_chain`` is
        enabled) enumerate every link in the lineage so the post-
        transfer Tag Number bump can hit them all.

        Config shape::

            {
              "report_path": "/Custom/.../Get_Transferred_Asset.xdo",
              "params": {
                "P_SOURCE_ASSET_ID": "${source_asset_id}"
              },
              "asset_id_column": "NEW_ASSET_ID",
              "asset_number_column": "NEW_ASSET_NUMBER",
              "book_type_code_column": "BOOK_TYPE_CODE",
              "is_destination_column": "IS_DESTINATION",
              "chain_order_column": "CHAIN_ORDER"
            }

        ``book_type_code_column`` is required for ``bump_chain`` —
        each link in the chain may live in a different book, and the
        ``updateAssetDescriptiveDetails`` call needs the right
        ``P_BOOK_TYPE_CODE`` per row.

        Param values support ``${...}`` placeholders that interpolate
        fields from the source asset and the pending transfer:
        ``source_asset_id``, ``source_asset_number``,
        ``source_book_type_code``, ``target_book_type_code``,
        ``transfer_to_entity``.

        Returns an empty list when the report is unconfigured, fails,
        or returns 0 rows.  Callers treat empty chain as "fall back to
        legacy destination-resolution path".
        """
        report_path = str(cfg.get("report_path") or "").strip()
        if not report_path:
            return []

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
        book_col = str(cfg.get("book_type_code_column") or "BOOK_TYPE_CODE")
        is_dest_col = str(cfg.get("is_destination_column") or "IS_DESTINATION")
        chain_order_col = str(cfg.get("chain_order_column") or "CHAIN_ORDER")

        try:
            rows = self._bip.run_report(params=params, report_path=report_path)
        except Exception as e:
            log.warning(
                "Lineage-lookup BIP report failed (path=%s, params=%s): %s",
                report_path,
                params,
                e,
            )
            return []

        if not rows:
            log.warning(
                "Lineage-lookup BIP returned 0 rows (path=%s, params=%s)",
                report_path,
                params,
            )
            return []

        chain: List[ChainLink] = []
        for idx, row in enumerate(rows, start=1):
            asset_id = str(row.get(id_col) or "").strip()
            if not asset_id:
                log.warning(
                    "Lineage-lookup BIP row missing %s column (have: %s); skipping",
                    id_col,
                    sorted(row.keys()),
                )
                continue
            asset_number = str(row.get(num_col) or "").strip() or None
            book_type_code = str(row.get(book_col) or "").strip() or None
            is_destination = str(row.get(is_dest_col) or "").strip().upper() in (
                "Y",
                "YES",
                "TRUE",
                "1",
            )
            chain_order_raw = str(row.get(chain_order_col) or "").strip()
            try:
                chain_order = int(chain_order_raw) if chain_order_raw else idx
            except ValueError:
                chain_order = idx
            chain.append(
                ChainLink(
                    asset_id=asset_id,
                    asset_number=asset_number,
                    book_type_code=book_type_code,
                    chain_order=chain_order,
                    is_destination=is_destination,
                )
            )

        log.info(
            "Lineage-lookup BIP returned %d link(s) for source_asset_id=%s: %s",
            len(chain),
            source_state.asset_id,
            [(link.chain_order, link.asset_id, link.book_type_code) for link in chain],
        )
        return chain

    @staticmethod
    def _first_nonempty(d: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[str]:
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

        # Pre-built TARGET_SEG* columns (preferred when the BIP report
        # supplies them): resolve — and optionally mint — the destination
        # CCID, then send a plain transferAsset with that expense_ccid.
        resolved_ccid = self._resolve_expense_ccid_from_segments(pending)
        if resolved_ccid:
            overrides["expense_ccid"] = resolved_ccid
            params, is_noop = build_same_book_transfer_params(
                state=state,
                effective_date=effective_date,
                overrides=overrides,
                request_id=request_id,
            )
        elif pending.target_company and not pending.target_expense_ccid:
            from .fusion_ops import build_same_book_transfer_params_option_b

            # Ledger: prefer the per-row TARGET_LEDGER_NAME column;
            # fall back to the static ledger_name_by_book config map.
            ledger_name = pending.target_ledger_name or self._ledger_name_by_book.get(
                (pending.book_type_code or "").upper().strip()
            )
            creator = None
            if self._ac_client is not None and ledger_name:
                creator = self._make_account_combination_creator()

            log.info(
                "Option B: asset=%s target_company=%s segment_key=%s ledger=%s "
                "auto_create=%s",
                pending.asset_number,
                pending.target_company,
                self._company_segment_key,
                ledger_name or "(none)",
                "yes" if creator is not None else "no",
            )

            params, is_noop = build_same_book_transfer_params_option_b(
                self._client,
                state=state,
                effective_date=effective_date,
                target_company=pending.target_company,
                request_id=request_id,
                company_segment_key=self._company_segment_key,
                account_combination_creator=creator,
                target_ledger_name=ledger_name,
            )
        else:
            params, is_noop = build_same_book_transfer_params(
                state=state,
                effective_date=effective_date,
                overrides=overrides,
                request_id=request_id,
            )

        if is_noop:
            log.info("NOOP: no distribution changes for asset=%s", pending.asset_number)
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
        return_msg = str(
            pl.get("X_MSG_DATA") or pl.get("X_RETURN_MESSAGE") or ""
        ).strip()

        return TransferResult(
            asset_number=pending.asset_number,
            status="TRANSFERRED" if status_code == "S" else "FAILED",
            source_book=pending.book_type_code,
            target_book=pending.target_book_type_code,
            transfer_to_entity=pending.transfer_to_entity,
            transfer_date=effective_date,
            fusion_response=pl,
            error=(
                None
                if status_code == "S"
                else f"Fusion X_RETURN_STATUS={status_code}: {return_msg}"
            ),
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
