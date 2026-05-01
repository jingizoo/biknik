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
    ship_to_location: str | None = None
    delivery_location: str | None = None
    invoice_quantity: int | None = None
    received_quantity: int | None = None
    cost: float | None = None
    source_system: str = "OTBI"


@dataclass(slots=True)
class CmdbAsset:
    source_key: str
    source_value: str
    # Oracle FA expects numeric IDs (P_LOCATION_ID_TBL = 300000004974106).
    # CMDB stores codes ("LC000238") or sys_ids — these flow through as
    # strings; the cycle runner translates them to Oracle FA IDs via
    # ``oracle_translations`` (or replaces them via ``cmdb_overrides``)
    # before the payload is built.
    location_id: int | str | None
    expense_ccid: int | str | None
    employee_id: int | str | None
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
    warnings: list[str] = field(default_factory=list)


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
