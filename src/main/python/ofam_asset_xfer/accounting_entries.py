"""Accounting entries for asset transfers.

Per the BRD, accounting entries are required for ALL transfers to move cost
and accumulated depreciation between entities:

Transfer From Side:
  - DR Asset Accumulated Depreciation Account
  - CR Asset Cost Account

Transfer To Side:
  - DR Asset Cost Account
  - CR Asset Accumulated Depreciation Account

FA currency follows functional currency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from .exceptions import ValidationError

log = logging.getLogger(__name__)


@dataclass
class AccountingLine:
    """A single accounting entry line."""

    side: str  # "TRANSFER_FROM" or "TRANSFER_TO"
    account_type: str  # "COST" or "ACCUMULATED_DEPRECIATION"
    account: str
    debit: Optional[Decimal] = None
    credit: Optional[Decimal] = None
    currency: str = "USD"
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "side": self.side,
            "account_type": self.account_type,
            "account": self.account,
            "currency": self.currency,
            "description": self.description,
        }
        if self.debit is not None:
            d["debit"] = str(self.debit)
        if self.credit is not None:
            d["credit"] = str(self.credit)
        return d


@dataclass
class TransferAccountingEntry:
    """Complete set of accounting entries for an asset transfer."""

    asset_number: str
    transfer_date: str
    source_entity: str
    target_entity: str
    cost: Decimal
    accumulated_depreciation: Decimal
    currency: str
    lines: List[AccountingLine] = field(default_factory=list)
    request_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "asset_number": self.asset_number,
            "transfer_date": self.transfer_date,
            "source_entity": self.source_entity,
            "target_entity": self.target_entity,
            "cost": str(self.cost),
            "accumulated_depreciation": str(self.accumulated_depreciation),
            "currency": self.currency,
            "request_id": self.request_id,
            "lines": [line.to_dict() for line in self.lines],
        }


def build_transfer_accounting_entries(
    asset_number: str,
    transfer_date: str,
    source_entity: str,
    target_entity: str,
    cost: str,
    accumulated_depreciation: str,
    cost_account: str,
    accumulated_depreciation_account: str,
    functional_currency: str = "USD",
    request_id: str = "",
) -> TransferAccountingEntry:
    """Build the cost/depreciation transfer accounting entries.

    Per BRD:
    Transfer From Side:
      - DR Accumulated Depreciation Account (remove accum depr from old entity)
      - CR Cost Account (remove cost from old entity)

    Transfer To Side:
      - DR Cost Account (add cost to new entity)
      - CR Accumulated Depreciation Account (add accum depr to new entity)
    """
    cost_val = Decimal(cost.replace(",", ""))
    accum_depr_val = Decimal(accumulated_depreciation.replace(",", ""))

    if cost_val < 0:
        raise ValidationError(f"Cost cannot be negative: {cost}")

    lines = [
        # Transfer FROM side: remove asset from source books
        AccountingLine(
            side="TRANSFER_FROM",
            account_type="ACCUMULATED_DEPRECIATION",
            account=accumulated_depreciation_account,
            debit=accum_depr_val,
            currency=functional_currency,
            description=f"Remove accum depreciation - Asset {asset_number} transfer out",
        ),
        AccountingLine(
            side="TRANSFER_FROM",
            account_type="COST",
            account=cost_account,
            credit=cost_val,
            currency=functional_currency,
            description=f"Remove cost - Asset {asset_number} transfer out",
        ),
        # Transfer TO side: add asset to target books
        AccountingLine(
            side="TRANSFER_TO",
            account_type="COST",
            account=cost_account,
            debit=cost_val,
            currency=functional_currency,
            description=f"Add cost - Asset {asset_number} transfer in",
        ),
        AccountingLine(
            side="TRANSFER_TO",
            account_type="ACCUMULATED_DEPRECIATION",
            account=accumulated_depreciation_account,
            credit=accum_depr_val,
            currency=functional_currency,
            description=f"Add accum depreciation - Asset {asset_number} transfer in",
        ),
    ]

    entry = TransferAccountingEntry(
        asset_number=asset_number,
        transfer_date=transfer_date,
        source_entity=source_entity,
        target_entity=target_entity,
        cost=cost_val,
        accumulated_depreciation=accum_depr_val,
        currency=functional_currency,
        lines=lines,
        request_id=request_id,
    )

    log.info(
        "Accounting entries built: asset=%s, cost=%s, accum_depr=%s, %s → %s",
        asset_number,
        cost_val,
        accum_depr_val,
        source_entity,
        target_entity,
    )
    return entry
