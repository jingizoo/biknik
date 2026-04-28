from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

from ofam_mass_additions.models import AuditEvent, ExceptionRecord, ProposedOracleUpdate


def write_proposed_updates(path: Path, updates: list[ProposedOracleUpdate]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["mass_addition_id", "payload"])
        writer.writeheader()
        for update in updates:
            writer.writerow(
                {
                    "mass_addition_id": update.mass_addition_id,
                    "payload": json.dumps(update.params, sort_keys=True),
                }
            )


def write_exceptions(path: Path, exceptions: list[ExceptionRecord]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["mass_addition_id", "reason", "detail"])
        writer.writeheader()
        for record in exceptions:
            writer.writerow(asdict(record))


def write_audit_log(path: Path, events: list[AuditEvent]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(asdict(event), sort_keys=True) + "\n")
