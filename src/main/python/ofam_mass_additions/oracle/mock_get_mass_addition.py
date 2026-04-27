from __future__ import annotations

from ofam_mass_additions.models import MassAddition


def get_mass_addition_from_oracle(row: dict[str, str]) -> MassAddition:
    return MassAddition(
        mass_addition_id=row["mass_addition_id"],
        book_type_code=row["book_type_code"],
        region=row["region"],
        tag_number=row.get("tag_number") or None,
        serial_number=row.get("serial_number") or None,
        po_number=row.get("po_number") or None,
        invoice_number=row.get("invoice_number") or None,
    )
