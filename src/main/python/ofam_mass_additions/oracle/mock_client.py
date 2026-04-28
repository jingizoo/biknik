from __future__ import annotations

from dataclasses import dataclass

from ofam_mass_additions.models import MassAddition


@dataclass
class MockOracleFaClient:
    rows: dict[str, MassAddition]

    def list_new_mass_addition_ids(self, *, book_type_code: str) -> list[str]:
        return [
            row.mass_addition_id
            for row in self.rows.values()
            if row.book_type_code == book_type_code and row.posting_status == "NEW"
        ]

    def get_mass_addition(self, mass_addition_id: str) -> MassAddition:
        return self.rows[mass_addition_id]

    def update_mass_addition(self, _payload: dict[str, str]) -> str:
        return '{"X_RETURN_STATUS":"S","X_MSG_DATA":"updated"}'
