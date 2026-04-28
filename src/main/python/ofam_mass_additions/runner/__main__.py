from pathlib import Path

from ofam_mass_additions.oracle.mock_client import MockOracleFaClient
from ofam_mass_additions.runner.cycle import MassAdditionCycleRunner, seed_oracle_rows_from_csv


if __name__ == "__main__":
    rows = seed_oracle_rows_from_csv(Path("data/samples/mass_additions_pilot.csv"))
    runner = MassAdditionCycleRunner(oracle_client=MockOracleFaClient(rows=rows), run_mode="dry-run")
    print(runner.run(output_dir=Path("output")))
