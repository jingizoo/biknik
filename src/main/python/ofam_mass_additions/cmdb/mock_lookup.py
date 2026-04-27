from __future__ import annotations

from ofam_mass_additions.models import CmdbAsset, MassAddition


MOCK_CMDB: dict[str, CmdbAsset] = {
    "TAG-100": CmdbAsset("tag_number", "TAG-100", location_id=501, expense_ccid=12001, employee_id=8801),
    "SN-101": CmdbAsset("serial_number", "SN-101", location_id=502, expense_ccid=12002, employee_id=8802),
    "PO-102": CmdbAsset("po_number", "PO-102", location_id=503, expense_ccid=None, employee_id=8803),
}


LOOKUP_ORDER = ("tag_number", "serial_number", "po_number", "invoice_number")


def lookup_cmdb_asset(mass_addition: MassAddition) -> CmdbAsset | None:
    for field_name in LOOKUP_ORDER:
        value = getattr(mass_addition, field_name)
        if value and value in MOCK_CMDB:
            return MOCK_CMDB[value]
    return None
