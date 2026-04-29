"""Live runner: real ServiceNow CMDB + caller-supplied Oracle FA client.

Mirrors ``runner.dry_run.run_dry_run`` but swaps the mock CMDB lookup
for a live ServiceNow client.  The Oracle FA client is still injected
(callers can pass either ``MockOracleFaClient`` for offline testing or
``FusionFaClient`` for real Fusion).
"""

from __future__ import annotations

from pathlib import Path

from ofam_mass_additions.cmdb.servicenow_client import (
    ServiceNowConfig,
    make_servicenow_lookup,
)
from ofam_mass_additions.runner.cycle import MassAdditionCycleRunner
from ofam_mass_additions.rules.enrichment import DEFAULT_CAPITALIZE_THRESHOLD


def run_live(
    *,
    oracle_client: object,
    servicenow_config: ServiceNowConfig,
    output_dir: Path,
    run_mode: str = "dry-run",
    pilot_book: str = "CORP_BOOK",
    pilot_region: str = "US",
    capitalize_threshold: float = DEFAULT_CAPITALIZE_THRESHOLD,
) -> dict[str, int]:
    """Run a mass-additions cycle against real ServiceNow CMDB.

    ``run_mode='dry-run'`` (default) skips Oracle writes; pass
    ``run_mode='live'`` to call ``oracle_client.update_mass_addition``.
    """
    runner = MassAdditionCycleRunner(
        oracle_client=oracle_client,
        run_mode=run_mode,
        pilot_book=pilot_book,
        pilot_region=pilot_region,
        cmdb_lookup=make_servicenow_lookup(servicenow_config),
        capitalize_threshold=capitalize_threshold,
    )
    return runner.run(output_dir=output_dir)
