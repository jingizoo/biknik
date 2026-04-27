from pathlib import Path

from ofam_mass_additions.runner.dry_run import run_dry_run


if __name__ == "__main__":
    stats = run_dry_run(
        input_csv=Path("data/samples/mass_additions_pilot.csv"),
        output_dir=Path("output"),
    )
    print(stats)
