from __future__ import annotations

from pathlib import Path

from ofam_mass_additions.oracle.mock_client import MockOracleFaClient
from ofam_mass_additions.runner.cycle import MassAdditionCycleRunner, seed_oracle_rows_from_csv


def run_dry_run(
    input_csv: Path,
    output_dir: Path,
    pilot_book: str = "CORP_BOOK",
    pilot_region: str = "US",
) -> dict[str, int]:
    rows = seed_oracle_rows_from_csv(input_csv)
    runner = MassAdditionCycleRunner(
        oracle_client=MockOracleFaClient(rows=rows),
        run_mode="dry-run",
        pilot_book=pilot_book,
        pilot_region=pilot_region,
    )
    return runner.run(output_dir=output_dir)
