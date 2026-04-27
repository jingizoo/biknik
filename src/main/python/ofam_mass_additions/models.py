from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class MassAddition:
    mass_addition_id: str
    book_type_code: str
    region: str
    queue_name: str = "NEW"
    posting_status: str = "NEW"
    tag_number: str | None = None
    serial_number: str | None = None
    po_number: str | None = None
    invoice_number: str | None = None


@dataclass(slots=True)
class CmdbAsset:
    source_key: str
    source_value: str
    location_id: int | None
    expense_ccid: int | None
    employee_id: int | None
    category_id: int | None = None
    ccid_active: bool = True


@dataclass(slots=True)
class ProposedOracleUpdate:
    mass_addition_id: str
    params: dict[str, Any]


@dataclass(slots=True)
class EnrichmentDecision:
    action: str
    reason: str
    proposed_update: ProposedOracleUpdate | None = None


@dataclass(slots=True)
class ExceptionRecord:
    mass_addition_id: str
    reason: str
    detail: str


@dataclass(slots=True)
class AuditEvent:
    mass_addition_id: str
    stage: str
    status: str
    message: str
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
