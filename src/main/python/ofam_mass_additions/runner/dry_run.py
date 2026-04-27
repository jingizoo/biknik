from __future__ import annotations

import csv
from pathlib import Path

from ofam_mass_additions.audit.writer import write_audit_log, write_exceptions, write_proposed_updates
from ofam_mass_additions.cmdb.mock_lookup import lookup_cmdb_asset
from ofam_mass_additions.models import AuditEvent, ExceptionRecord, ProposedOracleUpdate
from ofam_mass_additions.oracle.mock_get_mass_addition import get_mass_addition_from_oracle
from ofam_mass_additions.rules.enrichment import apply_enrichment_rules


def run_dry_run(
    input_csv: Path,
    output_dir: Path,
    pilot_book: str = "CORP_BOOK",
    pilot_region: str = "US",
) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    proposed: list[ProposedOracleUpdate] = []
    exceptions: list[ExceptionRecord] = []
    audit_events: list[AuditEvent] = []

    with input_csv.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            mass_addition = get_mass_addition_from_oracle(row)
            cmdb_asset = lookup_cmdb_asset(mass_addition)
            decision = apply_enrichment_rules(mass_addition, cmdb_asset, pilot_book=pilot_book, pilot_region=pilot_region)

            if decision.action == "auto_update" and decision.proposed_update is not None:
                proposed.append(decision.proposed_update)
                audit_events.append(AuditEvent(mass_addition.mass_addition_id, "rules", "success", decision.reason))
            elif decision.action == "exception":
                exceptions.append(
                    ExceptionRecord(
                        mass_addition_id=mass_addition.mass_addition_id,
                        reason=decision.reason,
                        detail="dry-run validation failure",
                    )
                )
                audit_events.append(AuditEvent(mass_addition.mass_addition_id, "rules", "exception", decision.reason))
            else:
                audit_events.append(AuditEvent(mass_addition.mass_addition_id, "rules", "skipped", decision.reason))

    write_proposed_updates(output_dir / "proposed_updates.csv", proposed)
    write_exceptions(output_dir / "exceptions.csv", exceptions)
    write_audit_log(output_dir / "audit.jsonl", audit_events)

    return {
        "auto_update": len(proposed),
        "exception": len(exceptions),
        "total": len(audit_events),
    }
