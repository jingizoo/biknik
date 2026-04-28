from __future__ import annotations

from ofam_mass_additions.models import MassAddition


def get_mass_addition_from_oracle(row: dict[str, str]) -> MassAddition:
    return MassAddition(
        mass_addition_id=row["mass_addition_id"],
        book_type_code=row["book_type_code"],
        region=row["region"],
        queue_name=row.get("queue_name") or "NEW",
        posting_status=row.get("posting_status") or "NEW",
        tag_number=row.get("tag_number") or None,
        serial_number=row.get("serial_number") or None,
        po_number=row.get("po_number") or None,
        invoice_number=row.get("invoice_number") or None,
        ship_to_location=row.get("ship_to_location") or None,
        delivery_location=row.get("delivery_location") or None,
        invoice_quantity=_to_int(row.get("invoice_quantity")),
        received_quantity=_to_int(row.get("received_quantity")),
        source_system=row.get("source_system") or "OTBI",
    )


def _to_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    return int(value)
