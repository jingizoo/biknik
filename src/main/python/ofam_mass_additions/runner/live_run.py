"""Live runner: real ServiceNow CMDB + caller-supplied Oracle FA client.

Mirrors ``runner.dry_run.run_dry_run`` but swaps the mock CMDB lookup
for a live ServiceNow client.  The Oracle FA client is still injected
(callers can pass either ``MockOracleFaClient`` for offline testing or
``FusionFaClient`` for real Fusion).

Returns a :class:`LiveRunResult` carrying the counts dict, the in-memory
proposed/exceptions/audit lists, and run metadata.  Publishers
(Slack / GCS / PagerDuty) consume this result without re-parsing the
on-disk artefacts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ofam_mass_additions.cmdb.servicenow_client import (
    ServiceNowConfig,
    make_servicenow_lookup,
)
from ofam_mass_additions.models import AuditEvent, ExceptionRecord, ProposedOracleUpdate
from ofam_mass_additions.rules.enrichment import DEFAULT_CAPITALIZE_THRESHOLD
from ofam_mass_additions.runner.cycle import MassAdditionCycleRunner


@dataclass
class LiveRunResult:
    counts: dict[str, int]
    proposed: list[ProposedOracleUpdate] = field(default_factory=list)
    exceptions: list[ExceptionRecord] = field(default_factory=list)
    audit_events: list[AuditEvent] = field(default_factory=list)
    run_date: str = ""
    run_ts: str = ""
    dry_run: bool = True
    pilot_book: str = ""
    pilot_region: str = ""
    out_dir: Path = field(default_factory=Path)


def run_live(
    *,
    oracle_client: object,
    servicenow_config: ServiceNowConfig,
    output_dir: Path,
    run_mode: str = "dry-run",
    pilot_book: str = "CORP_BOOK",
    pilot_region: str = "US",
    capitalize_threshold: float = DEFAULT_CAPITALIZE_THRESHOLD,
    debug_dump: bool = False,
) -> LiveRunResult:
    """Run a mass-additions cycle against real ServiceNow CMDB."""
    runner = MassAdditionCycleRunner(
        oracle_client=oracle_client,
        run_mode=run_mode,
        pilot_book=pilot_book,
        pilot_region=pilot_region,
        cmdb_lookup=make_servicenow_lookup(servicenow_config),
        capitalize_threshold=capitalize_threshold,
        debug_dump=debug_dump,
    )
    counts = runner.run(output_dir=output_dir)

    now = datetime.now(timezone.utc)
    return LiveRunResult(
        counts=counts,
        proposed=list(runner.last_proposed),
        exceptions=list(runner.last_exceptions),
        audit_events=list(runner.last_audit_events),
        run_date=now.date().isoformat(),
        run_ts=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        dry_run=(run_mode != "live"),
        pilot_book=pilot_book,
        pilot_region=pilot_region,
        out_dir=output_dir,
    )
