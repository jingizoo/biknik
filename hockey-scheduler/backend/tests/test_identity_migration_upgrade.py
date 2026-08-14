"""Migration 051 upgrade safety (#273 AC[5]/AC[6]).

Builds a REAL pre-051 database — the physical players table at its pre-#273
shape (guardian_person_id column included, carrying data), a verified
GuardianLink, roster history — then reopens the store so ``migrate()``
replays 051, and proves:

* existing players load unchanged with every new identity field honestly
  None (no first/last split is ever guessed from the display name);
* the GuardianLink relationship survives untouched (AC[5]);
* the dead ``guardian_person_id`` COLUMN and its data survive physically
  (migrations never drop or rewrite data) while the field stays OUT of the
  model and every facade payload — deprecated from the contract;
* roster history rows still reference the same player ids (AC[6]);
* the new columns/table exist and post-upgrade writes round-trip.

Runs on file-backed SQLite (the reopen is real) and PostgreSQL when
TEST_DATABASE_URL is set.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.store import SqlStore

UTC = timezone.utc

_MIGRATION = "051_athlete_identity"

# The post-040 / pre-051 players table, verbatim from
# 040_reassignment_fks.sqlite.sql — the exact shape a real database had
# before this slice.
_PRE_051_PLAYERS_SQLITE = (
    "CREATE TABLE players_old (id TEXT PRIMARY KEY, "
    "team_id TEXT REFERENCES teams (id) ON DELETE NO ACTION, name TEXT, "
    "position TEXT, shoots TEXT, jersey_number INTEGER, is_active INTEGER, "
    "guardian_person_id TEXT, external_ref TEXT)")

_NEW_COLUMNS = ("first_name", "last_name", "preferred_name", "birthdate",
                "registration_number", "skill_rating")


def _downgrade_051(store):
    """Physically revert 051 and un-record it, exactly reversing its DDL.

    SQLite uses the same create-copy-drop-rename dance as migration 040
    itself: renaming the LIVE table first would rewrite every incoming
    ``REFERENCES players`` clause (game_roster_entries etc.) to follow the
    rename, whereas dropping it leaves them dangling until the rebuilt
    table takes the ``players`` name again.
    """
    cur = store.conn.cursor()
    if store.backend == "postgres":
        for column in _NEW_COLUMNS:
            cur.execute(f"ALTER TABLE players DROP COLUMN {column}")
    else:
        cur.execute("PRAGMA foreign_keys = OFF")
        cur.execute(_PRE_051_PLAYERS_SQLITE)
        cur.execute(
            "INSERT INTO players_old SELECT id, team_id, name, position, "
            "shoots, jersey_number, is_active, guardian_person_id, "
            "external_ref FROM players")
        cur.execute("DROP TABLE players")
        cur.execute("ALTER TABLE players_old RENAME TO players")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_players_team "
                    "ON players (team_id)")
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_players_active_team_jersey "
            "ON players (team_id, jersey_number) "
            "WHERE is_active = 1 AND jersey_number IS NOT NULL")
        cur.execute("PRAGMA foreign_keys = ON")
    cur.execute("DROP INDEX IF EXISTS ix_players_registration_number")
    # #273 review round 2 finding 2: the partial unique (team_id,
    # registration_number) index added alongside the plain one above —
    # drop it too so this pre-051 simulation is a complete reversal on
    # BOTH backends, regardless of whether a Postgres column drop already
    # cascaded to it.
    cur.execute("DROP INDEX IF EXISTS ux_players_team_registration_number")
    cur.execute("DROP TABLE IF EXISTS age_eligibility_rules")
    cur.execute(store.dialect.sql(
        "DELETE FROM schema_migrations WHERE version = ?"), (_MIGRATION,))
    store.conn.commit()


def _table_columns(store, table):
    cur = store.conn.cursor()
    if store.backend == "sqlite":
        cur.execute(f"PRAGMA table_info('{table}')")
        return {row["name"] for row in cur.fetchall()}
    cur.execute(
        "SELECT column_name AS name FROM information_schema.columns "
        "WHERE table_name = %s", (table,))
    return {row["name"] for row in cur.fetchall()}


class IdentityMigrationUpgradeTest(unittest.TestCase):
    def _locations(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            yield "sqlite", path
        finally:
            os.remove(path)
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            yield "postgres", url

    def test_pre_051_database_with_guardian_data_upgrades_intact(self):
        for label, url in self._locations():
            with self.subTest(backend=label):
                store = SqlStore(url)
                if url != ":memory:" and label == "postgres":
                    store.reset_schema()
                _downgrade_051(store)
                self.assertNotIn("first_name",
                                 _table_columns(store, "players"), label)

                # Plant a legacy world through raw SQL only — exactly what a
                # real pre-#273 database holds, guardian column populated.
                sql = store.dialect.sql
                cur = store.conn.cursor()
                cur.execute(sql(
                    "INSERT INTO teams (id, name) VALUES (?, ?)"),
                    ("t1", "Team One"))
                cur.execute(sql(
                    "INSERT INTO players (id, team_id, name, position, "
                    "shoots, jersey_number, is_active, guardian_person_id, "
                    "external_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"),
                    ("p1", "t1", "Junior Player", "forward", "L", 7, 1,
                     "legacy-guardian-ref", "EXT-P1"))
                cur.execute(sql(
                    "INSERT INTO guardian_links (id, guardian_user_id, "
                    "player_id, created_at, verified, consent_method, "
                    "consented_at) VALUES (?, ?, ?, ?, ?, ?, ?)"),
                    ("gl1", "guardian-user", "p1",
                     "2026-01-01T00:00:00+00:00", 1, "signed_form",
                     "2026-01-01T00:00:00+00:00"))
                cur.execute(sql(
                    "INSERT INTO games (id, home_team_id, start_time) "
                    "VALUES (?, ?, ?)"),
                    ("g1", "t1", "2026-01-02T00:00:00+00:00"))
                cur.execute(sql(
                    "INSERT INTO game_roster_entries (id, game_id, "
                    "player_id, roster_role, selection_source, status, "
                    "selected_at, updated_at, selected_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"),
                    ("re1", "g1", "p1", "selected", "coach_selected",
                     "selected", "2026-01-02T00:00:00+00:00",
                     "2026-01-02T00:00:00+00:00", "op"))
                store.conn.commit()
                store.close()

                # THE UPGRADE: reopen -> migrate() replays 051.
                upgraded = SqlStore(url)
                try:
                    self.assertLessEqual(
                        set(_NEW_COLUMNS),
                        _table_columns(upgraded, "players"), label)

                    player = upgraded.get_player("p1")
                    self.assertEqual(player.name, "Junior Player", label)
                    self.assertEqual(player.jersey_number, 7, label)
                    for field in _NEW_COLUMNS:
                        self.assertIsNone(getattr(player, field),
                                          f"{label}: {field}")
                    # The model no longer carries the dead field at all.
                    self.assertFalse(hasattr(player, "guardian_person_id"),
                                     label)

                    # AC[5]: the real guardian relationship is intact.
                    links = upgraded.guardian_links_for_player("p1")
                    self.assertEqual(
                        [(l.id, l.guardian_user_id, l.verified)
                         for l in links],
                        [("gl1", "guardian-user", True)], label)

                    # AC[6]: roster history still references the player.
                    self.assertEqual(
                        [e.id for e in
                         upgraded.roster_entries_for_player("p1")],
                        ["re1"], label)

                    # Deprecated-not-destroyed: the raw column kept its data,
                    # and a model-level save leaves it untouched.
                    player.skill_rating = 3
                    upgraded.save_player(player)
                    cur = upgraded.conn.cursor()
                    cur.execute(upgraded.dialect.sql(
                        "SELECT guardian_person_id FROM players "
                        "WHERE id = ?"), ("p1",))
                    self.assertEqual(cur.fetchone()["guardian_person_id"],
                                     "legacy-guardian-ref", label)
                    self.assertEqual(
                        upgraded.get_player("p1").skill_rating, 3, label)

                    # The contract dropped the field: no facade payload
                    # carries it (or the private fields) for legacy rows.
                    api = ApiService(upgraded)
                    row = api.list_players(team_id="t1")[0]
                    self.assertNotIn("guardian_person_id", row, label)
                    self.assertNotIn("birthdate", row, label)
                    self.assertIsNone(row["first_name"], label)

                    # Post-upgrade writes round-trip through the new columns.
                    api.update_player("p1", first_name="Junior",
                                      last_name="Player",
                                      birthdate="2015-06-06")
                    got = upgraded.get_player("p1")
                    self.assertEqual(got.birthdate, "2015-06-06", label)

                    # And the rules table exists and accepts rows.
                    self.assertEqual(
                        upgraded.age_eligibility_rules_for_league_season(
                            "none"), [], label)
                finally:
                    upgraded.close()

    def test_051_is_recorded_exactly_once_and_reopen_is_idempotent(self):
        for label, url in self._locations():
            with self.subTest(backend=label):
                store = SqlStore(url)
                if label == "postgres":
                    store.reset_schema()
                cur = store.conn.cursor()
                cur.execute(store.dialect.sql(
                    "SELECT COUNT(*) AS n FROM schema_migrations "
                    "WHERE version = ?"), (_MIGRATION,))
                self.assertEqual(cur.fetchone()["n"], 1, label)
                store.close()
                reopened = SqlStore(url)  # replaying nothing must not fail
                try:
                    self.assertIn("first_name",
                                  _table_columns(reopened, "players"), label)
                finally:
                    reopened.close()


if __name__ == "__main__":
    unittest.main()
