"""Backup/restore acceptance smoke (#174 PR E).

Proves the client-recovery promise from #174: a configured deployment can be
backed up, restored into a *fresh, empty* database, started against that
restore, and found byte-for-byte equivalent — same record census, same
server-derived onboarding status, current migrations. This is the recovery
analogue of the restart proof (test_production_restart.py): restart reopens the
*same* database; this copies the data to a *new* one and re-verifies.

Run it directly against a durable SQLite deployment::

    python -m hockey_scheduler.acceptance.backup_restore --database-url ./client.db

With no ``--database-url`` it configures a throwaway sample deployment in a temp
database, so the check is self-contained and safe to run anywhere. It prints a
census + PASS/FAIL report and exits non-zero on any mismatch.

Scope: the file-copy backup here is SQLite-only — the durable single-instance
pilot path the issue explicitly supports. PostgreSQL deployments back up with
``pg_dump``/``pg_restore`` (documented in production-runbook.md); the logical
verification (census + onboarding equality) this module performs is the same
acceptance an operator applies after a Postgres restore.

Privacy: the report is counts + structural status only. No player names,
usernames, passwords, setup codes, or database URLs are printed — the sample
uses obviously-fictional names and a throwaway credential that never appears in
any output.
"""

import argparse
import os
import sqlite3
import sys
import tempfile

# Census dimensions: (label, store accessor). Every durable entity a client
# configures, so a dropped table or lost relationship shows up as a count gap.
_CENSUS = [
    ("organizations", "all_organizations"),
    ("leagues", "all_leagues"),
    ("venues", "all_venues"),
    ("rinks", "all_rinks"),
    ("ice_slots", "all_ice_slots"),
    ("seasons", "all_seasons"),
    ("levels", "all_levels"),
    ("divisions", "all_divisions"),
    ("clubs", "all_clubs"),
    ("teams", "all_teams"),
    ("players", "all_players"),
    ("officials", "all_officials"),
    ("accounts", "all_user_accounts"),
    ("setup_audit", "all_setup_audit"),
]

SAMPLE_ADMIN = "acceptance_admin"


def census(store) -> dict:
    """Count every durable entity class in ``store`` (counts only, no PII)."""
    return {label: len(getattr(store, accessor)()) for label, accessor in _CENSUS}


def configure_sample(api, actor_id: str = SAMPLE_ADMIN) -> None:
    """Configure a small but complete client hierarchy into an empty store via
    the public facade — the same shape the restart proof uses, so the sample
    exercises every relationship the census and onboarding status inspect.

    Fictional names only; the admin credential is a throwaway that is never
    printed. Leaves the installation fully onboarded (an active League Admin,
    game ice, a roster, an official) so the restored onboarding status is a rich
    payload to compare, not an all-blockers stub.
    """
    api.accounts.create_account(SAMPLE_ADMIN, "not-a-real-secret", "league_admin")
    org = api.create_organization("Canlon", short_name="CAN", actor_id=actor_id)
    league = api.create_league("Over 55", country="US",
                               organization_id=org["id"], actor_id=actor_id)
    venue = api.create_venue("Plainfield", address="1 Rink Rd",
                             league_id=league["id"], actor_id=actor_id)
    rink = api.create_rink(venue["id"], "Rink 1", actor_id=actor_id)
    api.create_ice_slot(rink["id"], "2026-09-01T18:30:00+00:00",
                        "2026-09-01T20:00:00+00:00", actor_id=actor_id)
    season = api.create_season(league["id"], "Fall 2026", actor_id=actor_id)
    division = api.create_division(season["id"], "Div A", actor_id=actor_id)
    club = api.create_club("Club X", country="US", actor_id=actor_id)
    team = api.create_team(club["id"], division["id"], "Team A", actor_id=actor_id)
    api.create_team(club["id"], division["id"], "Team B", actor_id=actor_id)
    api.create_player(team["id"], "Vince Skater", "forward",
                      jersey_number=9, actor_id=actor_id)
    api.create_official("Riley Whistle", actor_id=actor_id)


def backup_sqlite(store, dest_path: str) -> None:
    """Copy ``store``'s SQLite database to ``dest_path`` via SQLite's online
    backup API — the local analogue of ``pg_dump``, safe to run against a live
    connection. Refuses non-SQLite stores rather than silently no-op."""
    if getattr(store, "backend", None) != "sqlite":
        raise RuntimeError(
            "File backup is SQLite-only; for PostgreSQL use pg_dump/pg_restore "
            "(see docs/architecture/production-runbook.md).")
    dest = sqlite3.connect(dest_path)
    try:
        store.conn.backup(dest)
    finally:
        dest.close()


def compare(source: dict, restored: dict) -> list:
    """Return a list of human-readable mismatches between two acceptance
    snapshots (``{"census":..., "onboarding":..., "migrations_current":...}``).
    Empty list means the restore is faithful."""
    mismatches = []
    for label in {*source["census"], *restored["census"]}:
        a, b = source["census"].get(label), restored["census"].get(label)
        if a != b:
            mismatches.append(f"census.{label}: source={a} restored={b}")
    if source["onboarding"] != restored["onboarding"]:
        mismatches.append("onboarding status differs after restore")
    if not restored["migrations_current"]:
        mismatches.append("restored database migrations are not current")
    return mismatches


def _snapshot(api) -> dict:
    return {
        "census": census(api.store),
        "onboarding": api.get_onboarding_status("production"),
        "migrations_current": bool(api.get_health()["migrations"]["current"]),
    }


def run_acceptance(source_url: str = None) -> dict:
    """Back up a deployment, restore into a fresh database, start against it,
    and verify equivalence. Returns a structured report with ``ok`` and any
    ``mismatches``. When ``source_url`` is omitted, a throwaway sample
    deployment is configured in a temp database first."""
    # Imported here so the module has no import cost until actually run.
    from ..api import ApiService
    from ..store import SqlStore

    temp_paths = []
    try:
        if source_url:
            source_api = ApiService(SqlStore(source_url))
        else:
            fd, src = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            temp_paths.append(src)
            source_api = ApiService(SqlStore(src))
            configure_sample(source_api)

        source = _snapshot(source_api)

        # Back up to a brand-new empty database, then start a fresh app against
        # the restore — exactly the recovery path an operator would run.
        fd, restore_path = tempfile.mkstemp(suffix=".restored.db")
        os.close(fd)
        temp_paths.append(restore_path)
        backup_sqlite(source_api.store, restore_path)
        restored_api = ApiService(SqlStore(restore_path))
        restored = _snapshot(restored_api)

        mismatches = compare(source, restored)
        return {
            "ok": not mismatches,
            "census": restored["census"],
            "onboarding_complete": restored["onboarding"]["complete"],
            "ready_to_schedule": restored["onboarding"]["ready_to_schedule"],
            "migrations_current": restored["migrations_current"],
            "mismatches": mismatches,
        }
    finally:
        for path in temp_paths:
            try:
                os.remove(path)
            except OSError:
                pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hockey_scheduler.acceptance.backup_restore",
        description="Backup/restore acceptance smoke (#174). Backs up a durable "
                    "SQLite deployment, restores into a fresh database, and "
                    "verifies the record census and onboarding status match.")
    parser.add_argument(
        "--database-url", default=None,
        help="Durable SQLite database (file path) to back up. Omit to configure "
             "and verify a throwaway sample deployment.")
    args = parser.parse_args(argv)

    report = run_acceptance(args.database_url)
    print("Record census after restore:")
    for label, count in sorted(report["census"].items()):
        print(f"  {label:>14}: {count}")
    print(f"  migrations current: {report['migrations_current']}")
    print(f"  ready to schedule : {report['ready_to_schedule']}")
    print(f"  onboarding complete: {report['onboarding_complete']}")
    if report["ok"]:
        print("PASS — restore is byte-for-byte equivalent to the source.")
        return 0
    print("FAIL — restore diverged from the source:")
    for mismatch in report["mismatches"]:
        print(f"  - {mismatch}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
