"""Intercompany (IC) journal entry builder for cross-book asset transfers.

Per the BRD, whenever an asset with remaining NBV (original cost > accumulated
depreciation) is transferred to a new entity, intercompany accounting entries
are required:

Transfer Entry:
  - DR 1050100  Entity: Transfer From,  Intercompany: Transfer To,   Amount: NBV
  - CR 2050100  Entity: Transfer To,    Intercompany: Transfer From,  Amount: NBV

Cross-currency (non-USD ledger) transfers:
  FA currency follows functional currency, but the IC leg must be posted in USD.
  The module converts the IC amount using the provided exchange rate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from .exceptions import ValidationError

log = logging.getLogger(__name__)

# Default GL accounts per BRD
IC_RECEIVABLE_ACCOUNT = "1050100"
IC_PAYABLE_ACCOUNT = "2050100"


@dataclass(frozen=True)
class ICConfig:
    """Configuration for intercompany journal generation."""

    ic_receivable_account: str = IC_RECEIVABLE_ACCOUNT
    ic_payable_account: str = IC_PAYABLE_ACCOUNT
    ic_currency: str = "USD"
    default_rate_type: str = "Corporate"

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ICConfig":
        return ICConfig(
            ic_receivable_account=str(
                d.get("ic_receivable_account", IC_RECEIVABLE_ACCOUNT)
            ),
            ic_payable_account=str(
                d.get("ic_payable_account", IC_PAYABLE_ACCOUNT)
            ),
            ic_currency=str(d.get("ic_currency", "USD")),
            default_rate_type=str(d.get("default_rate_type", "Corporate")),
        )


@dataclass
class ICJournalLine:
    """A single line of an intercompany journal entry."""

    entity: str
    account: str
    intercompany: str
    debit: Optional[Decimal] = None
    credit: Optional[Decimal] = None
    currency: str = "USD"
    functional_amount: Optional[Decimal] = None
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "entity": self.entity,
            "account": self.account,
            "intercompany": self.intercompany,
            "currency": self.currency,
            "description": self.description,
        }
        if self.debit is not None:
            d["debit"] = str(self.debit)
        if self.credit is not None:
            d["credit"] = str(self.credit)
        if self.functional_amount is not None:
            d["functional_amount"] = str(self.functional_amount)
        return d


@dataclass
class ICJournalEntry:
    """A complete intercompany journal entry for a cross-book transfer."""

    source_entity: str
    target_entity: str
    asset_number: str
    transfer_date: str
    nbv: Decimal
    lines: List[ICJournalLine] = field(default_factory=list)
    source_currency: str = "USD"
    ic_currency: str = "USD"
    exchange_rate: Optional[Decimal] = None
    request_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_entity": self.source_entity,
            "target_entity": self.target_entity,
            "asset_number": self.asset_number,
            "transfer_date": self.transfer_date,
            "nbv": str(self.nbv),
            "source_currency": self.source_currency,
            "ic_currency": self.ic_currency,
            "exchange_rate": str(self.exchange_rate) if self.exchange_rate else None,
            "request_id": self.request_id,
            "lines": [line.to_dict() for line in self.lines],
        }


def compute_nbv(cost: str, accumulated_depreciation: str) -> Decimal:
    """Compute Net Book Value = Cost - Accumulated Depreciation.

    Both values should be positive numbers as stored in Oracle.
    """
    c = Decimal(cost.replace(",", ""))
    ad = Decimal(accumulated_depreciation.replace(",", ""))
    nbv = c - ad
    if nbv < 0:
        raise ValidationError(
            f"NBV is negative (cost={cost}, accum_depr={accumulated_depreciation}). "
            f"Asset may be fully depreciated or data is inconsistent."
        )
    return nbv


def convert_to_ic_currency(
    amount: Decimal,
    exchange_rate: Decimal,
) -> Decimal:
    """Convert a functional currency amount to IC currency (USD).

    For non-USD ledgers, the IC leg must be posted in USD.
    exchange_rate is functional_currency_per_USD (e.g., 150 JPY = 1 USD → rate=150).
    """
    if exchange_rate <= 0:
        raise ValidationError(f"Exchange rate must be positive, got {exchange_rate}")
    return (amount / exchange_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def build_ic_journal_entry(
    source_entity: str,
    target_entity: str,
    asset_number: str,
    transfer_date: str,
    cost: str,
    accumulated_depreciation: str,
    source_currency: str = "USD",
    exchange_rate: Optional[Decimal] = None,
    config: Optional[ICConfig] = None,
    request_id: str = "",
) -> Optional[ICJournalEntry]:
    """Build intercompany journal entries for a cross-book transfer.

    Returns None if NBV is zero (fully depreciated asset, no IC booking needed).

    Per BRD:
    - DR 1050100 Entity: Transfer From, IC: Transfer To,  Amount: NBV
    - CR 2050100 Entity: Transfer To,   IC: Transfer From, Amount: NBV

    For non-USD functional currencies, the FA entry follows functional currency
    but the IC leg amount is converted to USD.
    """
    cfg = config or ICConfig()

    nbv = compute_nbv(cost, accumulated_depreciation)
    if nbv == 0:
        log.info(
            "Asset %s is fully depreciated (NBV=0); no IC booking required",
            asset_number,
        )
        return None

    ic_currency = cfg.ic_currency
    ic_amount = nbv

    # Cross-currency conversion: if source is non-USD, convert IC amount to USD
    if source_currency.upper() != ic_currency.upper():
        if exchange_rate is None:
            raise ValidationError(
                f"Cross-currency IC transfer requires exchange_rate for "
                f"{source_currency} → {ic_currency}. Asset: {asset_number}"
            )
        ic_amount = convert_to_ic_currency(nbv, exchange_rate)
        log.info(
            "Cross-currency IC: %s %s → %s %s (rate=%s) for asset %s",
            nbv,
            source_currency,
            ic_amount,
            ic_currency,
            exchange_rate,
            asset_number,
        )

    lines = [
        # DR: Transfer From entity receives receivable from Transfer To
        ICJournalLine(
            entity=source_entity,
            account=cfg.ic_receivable_account,
            intercompany=target_entity,
            debit=ic_amount,
            currency=ic_currency,
            functional_amount=nbv if source_currency.upper() != ic_currency.upper() else None,
            description=f"IC Receivable - Asset {asset_number} transfer to {target_entity}",
        ),
        # CR: Transfer To entity records payable to Transfer From
        ICJournalLine(
            entity=target_entity,
            account=cfg.ic_payable_account,
            intercompany=source_entity,
            credit=ic_amount,
            currency=ic_currency,
            functional_amount=nbv if source_currency.upper() != ic_currency.upper() else None,
            description=f"IC Payable - Asset {asset_number} transfer from {source_entity}",
        ),
    ]

    entry = ICJournalEntry(
        source_entity=source_entity,
        target_entity=target_entity,
        asset_number=asset_number,
        transfer_date=transfer_date,
        nbv=nbv,
        lines=lines,
        source_currency=source_currency,
        ic_currency=ic_currency,
        exchange_rate=exchange_rate,
        request_id=request_id,
    )

    log.info(
        "IC journal entry built: %s → %s, asset=%s, NBV=%s %s, IC amount=%s %s",
        source_entity,
        target_entity,
        asset_number,
        nbv,
        source_currency,
        ic_amount,
        ic_currency,
    )
    return entry
