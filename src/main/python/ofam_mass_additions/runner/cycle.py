from __future__ import annotations

import csv
from pathlib import Path
from typing import Callable

from ofam_mass_additions.audit.writer import write_audit_log, write_exceptions, write_proposed_updates
from ofam_mass_additions.cmdb.mock_lookup import lookup_cmdb_asset as _default_cmdb_lookup
from ofam_mass_additions.models import AuditEvent, CmdbAsset, ExceptionRecord, ProposedOracleUpdate
from ofam_mass_additions.oracle.payloads import build_operation_payload, parse_oracle_status
from ofam_mass_additions.rules.enrichment import apply_enrichment_rules
from ofam_mass_additions.models import MassAddition


CmdbLookup = Callable[[MassAddition], CmdbAsset | None]


class MassAdditionCycleRunner:
    def __init__(
        self,
        oracle_client: object,
        run_mode: str = "dry-run",
        pilot_book: str = "CORP_BOOK",
        pilot_region: str = "US",
        cmdb_lookup: CmdbLookup | None = None,
    ) -> None:
        self.oracle_client = oracle_client
        self.run_mode = run_mode
        self.pilot_book = pilot_book
        self.pilot_region = pilot_region
        self.cmdb_lookup: CmdbLookup = cmdb_lookup or _default_cmdb_lookup

    def run(self, output_dir: Path) -> dict[str, int]:
        output_dir.mkdir(parents=True, exist_ok=True)
        proposed: list[ProposedOracleUpdate] = []
        exceptions: list[ExceptionRecord] = []
        audit_events: list[AuditEvent] = []

        for mass_addition_id in self.oracle_client.list_new_mass_addition_ids(book_type_code=self.pilot_book):
            mass_addition = self.oracle_client.get_mass_addition(mass_addition_id)
            cmdb_asset = self.cmdb_lookup(mass_addition)
            decision = apply_enrichment_rules(mass_addition, cmdb_asset, self.pilot_book, self.pilot_region)

            if decision.action == "auto_update" and decision.proposed_update is not None:
                operation_payload = build_operation_payload("updateMassAddition", decision.proposed_update.params)
                proposed.append(decision.proposed_update)

                if self.run_mode != "live":
                    audit_events.append(
                        AuditEvent(
                            mass_addition_id,
                            "updateMassAddition",
                            "dry-run",
                            "proposed payload generated; live update skipped",
                        )
                    )
                    continue

                response_parameter_list = self.oracle_client.update_mass_addition(operation_payload)
                status, message = parse_oracle_status(response_parameter_list)
                if status == "S":
                    audit_events.append(AuditEvent(mass_addition_id, "updateMassAddition", "success", message or "updated"))
                else:
                    exceptions.append(ExceptionRecord(mass_addition_id, "Oracle validation failed", message or "unknown"))
                    audit_events.append(AuditEvent(mass_addition_id, "updateMassAddition", "exception", message or "unknown"))

            elif decision.action == "exception":
                exceptions.append(ExceptionRecord(mass_addition_id, decision.reason, "validation failed"))
                audit_events.append(AuditEvent(mass_addition_id, "rules", "exception", decision.reason))
            else:
                audit_events.append(AuditEvent(mass_addition_id, "rules", "skipped", decision.reason))

        write_proposed_updates(output_dir / "proposed_updates.csv", proposed)
        write_exceptions(output_dir / "exceptions.csv", exceptions)
        write_audit_log(output_dir / "audit.jsonl", audit_events)

        return {
            "total": len(audit_events),
            "auto_update": len(proposed),
            "exception": len(exceptions),
        }


def seed_oracle_rows_from_csv(input_csv: Path) -> dict[str, "MassAddition"]:
    from ofam_mass_additions.oracle.mock_get_mass_addition import get_mass_addition_from_oracle

    rows: dict[str, MassAddition] = {}
    with input_csv.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            mass_addition = get_mass_addition_from_oracle(row)
            rows[mass_addition.mass_addition_id] = mass_addition
    return rows
