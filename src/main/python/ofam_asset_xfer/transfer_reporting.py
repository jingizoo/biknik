"""Transfer reporting classification for asset rollforward reports.

Per the BRD:
- Transfer activity must be properly reflected in the asset Rollforward report.
- Categories should show "Transfer" not "Addition" or "Retirement".
- Reports should link which entity the asset was transferred from/to.
- Reporting should layer in depreciation reserve at transfer points.
- Asset records should maintain a reporting trail for movements.

This module produces structured reporting records that downstream systems
(Rollforward report, Tableau, OAC) can consume to correctly classify
transfer activity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# Rollforward categories per BRD
CATEGORY_TRANSFER_OUT = "TRANSFER_OUT"
CATEGORY_TRANSFER_IN = "TRANSFER_IN"
CATEGORY_ADDITION = "ADDITION"
CATEGORY_RETIREMENT = "RETIREMENT"


@dataclass
class RollforwardEntry:
    """A single entry for the asset rollforward report.

    This replaces generic "Addition" / "Retirement" classifications with
    proper "Transfer" categories per the BRD.
    """

    asset_number: str
    entity: str
    book_type_code: str
    category: str  # TRANSFER_OUT, TRANSFER_IN, ADDITION, RETIREMENT
    transfer_date: str
    cost: Decimal
    accumulated_depreciation: Decimal
    nbv: Decimal

    # Traceability: link source and target
    counterpart_entity: Optional[str] = None
    counterpart_book: Optional[str] = None
    counterpart_asset_number: Optional[str] = None

    # Depreciation reserve layering
    depreciation_reserve_at_transfer: Optional[Decimal] = None

    request_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "asset_number": self.asset_number,
            "entity": self.entity,
            "book_type_code": self.book_type_code,
            "category": self.category,
            "transfer_date": self.transfer_date,
            "cost": str(self.cost),
            "accumulated_depreciation": str(self.accumulated_depreciation),
            "nbv": str(self.nbv),
            "request_id": self.request_id,
        }
        if self.counterpart_entity:
            d["counterpart_entity"] = self.counterpart_entity
        if self.counterpart_book:
            d["counterpart_book"] = self.counterpart_book
        if self.counterpart_asset_number:
            d["counterpart_asset_number"] = self.counterpart_asset_number
        if self.depreciation_reserve_at_transfer is not None:
            d["depreciation_reserve_at_transfer"] = str(
                self.depreciation_reserve_at_transfer
            )
        return d


@dataclass
class TransferTraceRecord:
    """Cross-reference record linking source and target assets.

    Per BRD: traceability of the assets transferred — should be able to tell
    which asset in the new book is linked to which asset in the old book.
    """

    source_asset_number: str
    source_book: str
    source_entity: str
    target_asset_number: str
    target_book: str
    target_entity: str
    transfer_date: str
    cost_transferred: Decimal
    accumulated_depreciation_transferred: Decimal
    nbv_transferred: Decimal
    tag_number: Optional[str] = None
    serial_number: Optional[str] = None
    request_id: str = ""
    transfer_count: int = 1  # supports multiple transfers per BRD

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "source_asset_number": self.source_asset_number,
            "source_book": self.source_book,
            "source_entity": self.source_entity,
            "target_asset_number": self.target_asset_number,
            "target_book": self.target_book,
            "target_entity": self.target_entity,
            "transfer_date": self.transfer_date,
            "cost_transferred": str(self.cost_transferred),
            "accumulated_depreciation_transferred": str(
                self.accumulated_depreciation_transferred
            ),
            "nbv_transferred": str(self.nbv_transferred),
            "transfer_count": self.transfer_count,
            "request_id": self.request_id,
        }
        if self.tag_number:
            d["tag_number"] = self.tag_number
        if self.serial_number:
            d["serial_number"] = self.serial_number
        return d


def build_rollforward_entries(
    source_asset_number: str,
    source_entity: str,
    source_book: str,
    target_asset_number: str,
    target_entity: str,
    target_book: str,
    transfer_date: str,
    cost: str,
    accumulated_depreciation: str,
    request_id: str = "",
) -> List[RollforwardEntry]:
    """Build rollforward report entries for a cross-book transfer.

    Creates paired entries:
    1. TRANSFER_OUT on the source entity/book
    2. TRANSFER_IN on the target entity/book

    These replace the misleading "Addition" / "Retirement" that Oracle
    would otherwise show when using add+retire orchestration.
    """
    cost_val = Decimal(cost.replace(",", ""))
    accum_val = Decimal(accumulated_depreciation.replace(",", ""))
    nbv_val = cost_val - accum_val

    entries = [
        RollforwardEntry(
            asset_number=source_asset_number,
            entity=source_entity,
            book_type_code=source_book,
            category=CATEGORY_TRANSFER_OUT,
            transfer_date=transfer_date,
            cost=cost_val,
            accumulated_depreciation=accum_val,
            nbv=nbv_val,
            counterpart_entity=target_entity,
            counterpart_book=target_book,
            counterpart_asset_number=target_asset_number,
            depreciation_reserve_at_transfer=accum_val,
            request_id=request_id,
        ),
        RollforwardEntry(
            asset_number=target_asset_number,
            entity=target_entity,
            book_type_code=target_book,
            category=CATEGORY_TRANSFER_IN,
            transfer_date=transfer_date,
            cost=cost_val,
            accumulated_depreciation=accum_val,
            nbv=nbv_val,
            counterpart_entity=source_entity,
            counterpart_book=source_book,
            counterpart_asset_number=source_asset_number,
            depreciation_reserve_at_transfer=accum_val,
            request_id=request_id,
        ),
    ]

    log.info(
        "Rollforward entries built: %s/%s (TRANSFER_OUT) → %s/%s (TRANSFER_IN), "
        "cost=%s, accum_depr=%s, NBV=%s",
        source_entity,
        source_asset_number,
        target_entity,
        target_asset_number,
        cost_val,
        accum_val,
        nbv_val,
    )
    return entries


def build_trace_record(
    source_asset_number: str,
    source_book: str,
    source_entity: str,
    target_asset_number: str,
    target_book: str,
    target_entity: str,
    transfer_date: str,
    cost: str,
    accumulated_depreciation: str,
    tag_number: Optional[str] = None,
    serial_number: Optional[str] = None,
    request_id: str = "",
    transfer_count: int = 1,
) -> TransferTraceRecord:
    """Build a traceability record linking source and target assets.

    Per BRD: asset tags and serials need to be transferred across books,
    and we need to be able to tell which asset in the new book is linked
    to which asset in the old book.
    """
    cost_val = Decimal(cost.replace(",", ""))
    accum_val = Decimal(accumulated_depreciation.replace(",", ""))
    nbv_val = cost_val - accum_val

    record = TransferTraceRecord(
        source_asset_number=source_asset_number,
        source_book=source_book,
        source_entity=source_entity,
        target_asset_number=target_asset_number,
        target_book=target_book,
        target_entity=target_entity,
        transfer_date=transfer_date,
        cost_transferred=cost_val,
        accumulated_depreciation_transferred=accum_val,
        nbv_transferred=nbv_val,
        tag_number=tag_number,
        serial_number=serial_number,
        request_id=request_id,
        transfer_count=transfer_count,
    )

    log.info(
        "Trace record: %s/%s → %s/%s (tag=%s, transfer #%d)",
        source_book,
        source_asset_number,
        target_book,
        target_asset_number,
        tag_number,
        transfer_count,
    )
    return record
