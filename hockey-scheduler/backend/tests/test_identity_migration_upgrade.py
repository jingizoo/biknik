"""Migration 058 upgrade safety (#273 AC[5]/AC[6]).

Builds a REAL pre-058 database — the physical players table at its pre-#273
shape (guardian_person_id column included, carrying data), a verified
GuardianLink, roster history — then reopens the store so ``migrate()``
replays 058, and proves:

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

Also covers the #424 owner review's migration-renumbering finding: this
file's own migration was originally 051_athlete_identity, which shared a
leading number (though not a filename) with main's own
051_active_context_generation once main advanced through 057 in parallel —
see 058_athlete_identity.sql's own NUMBERING NOTE for the full history.
``Migration058UpgradeOrderingParityTest`` below is the review's explicit,
additional required coverage: it builds the pre-058 state by REPLAYING THE
REAL migration files main actually ships (through 057, with
058_athlete_identity.sql absent) rather than hand-reversing 058's own
forward DDL the way ``_downgrade_058`` below does, so it genuinely
exercises the real production upgrade path the review described, and
proves parity against a fresh 001-through-058 database.
"""

import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.store import SqlStore
from hockey_scheduler.store import sql_store as _sql_store_module

UTC = timezone.utc

_MIGRATION = "058_athlete_identity"

# The post-040 / pre-058 players table, verbatim from
# 040_reassignment_fks.sqlite.sql — the exact shape a real database had
# before this slice.
_PRE_058_PLAYERS_SQLITE = (
    "CREATE TABLE players_old (id TEXT PRIMARY KEY, "
    "team_id TEXT REFERENCES teams (id) ON DELETE NO ACTION, name TEXT, "
    "position TEXT, shoots TEXT, jersey_number INTEGER, is_active INTEGER, "
    "guardian_person_id TEXT, external_ref TEXT)")

_NEW_COLUMNS = ("first_name", "last_name", "preferred_name", "birthdate",
                "registration_number", "skill_rating")


def _downgrade_058(store):
    """Physically revert 058 and un-record it, exactly reversing its DDL.

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
        cur.execute(_PRE_058_PLAYERS_SQLITE)
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
    # drop it too so this pre-058 simulation is a complete reversal on
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

    def test_pre_058_database_with_guardian_data_upgrades_intact(self):
        for label, url in self._locations():
            with self.subTest(backend=label):
                store = SqlStore(url)
                if url != ":memory:" and label == "postgres":
                    store.reset_schema()
                _downgrade_058(store)
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

                # THE UPGRADE: reopen -> migrate() replays 058.
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

    def test_058_is_recorded_exactly_once_and_reopen_is_idempotent(self):
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


# -- #424 owner review required coverage: real upgrade-from-057 vs fresh ----
#
# The migration-renumbering bug the review reported (and this suite's
# demonstrate-first step reproduced against the real, then-unmodified
# migrate()/_load_migrations()): a database already migrated through main's
# 057 BEFORE this branch's own athlete-identity file ever existed applies
# that file CHRONOLOGICALLY LAST once it lands (never previously recorded,
# so it runs whenever migrate()'s loop first reaches it) — while a FRESH
# database sorts and applies the same file BEFORE 052-057. Two different
# real DDL-execution orders for the one file, purely a function of install
# history. The class below is the review's own required regression: it
# proves the RENUMBERED file (058, sorting after 052-057 on every install
# path) no longer has that property, by genuinely replaying main's real
# migration files rather than hand-reversing forward DDL.

_BRANCH_MIGRATIONS_DIR = _sql_store_module._MIGRATIONS_DIR


def _copy_migrations_dir(exclude=frozenset()):
    """A temp copy of the branch's real migrations/ directory, optionally
    omitting some exact filenames — used to make migrate() REPLAY the real
    historical migration files main actually ships, in the real sorted
    order, rather than hand-reversing a forward migration's DDL the way
    ``_downgrade_058`` above does. Caller owns ``shutil.rmtree`` cleanup."""
    tmp = tempfile.mkdtemp(prefix="mig058_parity_")
    for fname in os.listdir(_BRANCH_MIGRATIONS_DIR):
        if not fname.endswith(".sql") or fname in exclude:
            continue
        shutil.copy(os.path.join(_BRANCH_MIGRATIONS_DIR, fname),
                    os.path.join(tmp, fname))
    return tmp


def _ledger_versions(store):
    """Every version ever recorded in schema_migrations, as a set — the
    full upgrade history, order-independent."""
    cur = store.conn.cursor()
    cur.execute("SELECT version FROM schema_migrations")
    return {row["version"] for row in cur.fetchall()}


class Migration058UpgradeOrderingParityTest(unittest.TestCase):
    """#424 owner review: 058_athlete_identity (renumbered from 051) must
    apply EXACTLY ONCE whether a database is fresh (001 through 058 in one
    ``migrate()`` pass) or an existing installation that already migrated
    through main's 057 BEFORE this file ever existed on disk — and the two
    paths must converge on the identical final schema shape.

    Unlike ``IdentityMigrationUpgradeTest`` above (which hand-reverses 058's
    own forward DDL to fake a pre-058 shape), this builds the pre-058 state
    by pointing ``sql_store._MIGRATIONS_DIR`` at a temp copy of the REAL
    migrations/ directory with 058_athlete_identity.sql removed, so
    ``migrate()`` genuinely replays main's own shipped files (through 057)
    — the actual production upgrade path the review's finding described.
    Every database this test touches is a fresh, uniquely-named one it
    creates and tears down itself; nothing here reuses TEST_DATABASE_URL's
    own database or any state another test may have left behind.
    """

    def setUp(self):
        # Belt and suspenders: the test body always restores
        # _MIGRATIONS_DIR in a `finally` right after each use, but this
        # guarantees it regardless, so a bug here can never leak a
        # temp-directory pointer into a later test in the same process
        # (run_parallel.py shards multiple test MODULES per subprocess).
        self.addCleanup(setattr, _sql_store_module, "_MIGRATIONS_DIR",
                         _BRANCH_MIGRATIONS_DIR)

    def _locations(self):
        yield "sqlite", None
        base = os.environ.get("TEST_DATABASE_URL")
        if base:
            yield "postgres", base

    def _new_sqlite_url(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def _new_postgres_url(self, base_url, tag):
        import psycopg
        admin_url = base_url.rsplit("/", 1)[0] + "/postgres"
        name = (base_url.rsplit("/", 1)[-1]
                 + f"_058parity_{int(time.time() * 1000)}_{tag}")

        def _admin(sql_text):
            conn = psycopg.connect(admin_url, autocommit=True)
            try:
                conn.execute(sql_text)
            finally:
                conn.close()

        _admin(f'DROP DATABASE IF EXISTS "{name}"')
        _admin(f'CREATE DATABASE "{name}"')
        self.addCleanup(_admin, f'DROP DATABASE IF EXISTS "{name}"')
        return f"{base_url.rsplit('/', 1)[0]}/{name}"

    def _new_url(self, label, base, tag):
        return (self._new_sqlite_url() if label == "sqlite"
                else self._new_postgres_url(base, tag))

    def _seed_pre_058_data(self, store):
        """Plant real team/player/guardian/roster rows through raw SQL —
        the exact pre-#273 shape (guardian_person_id populated, no identity
        columns) — mirroring IdentityMigrationUpgradeTest's own planting so
        both tests exercise the SAME real-world legacy row shape."""
        sql = store.dialect.sql
        cur = store.conn.cursor()
        cur.execute(sql("INSERT INTO teams (id, name) VALUES (?, ?)"),
                    ("t1", "Team One"))
        cur.execute(sql(
            "INSERT INTO players (id, team_id, name, position, shoots, "
            "jersey_number, is_active, guardian_person_id, external_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"),
            ("p1", "t1", "Junior Player", "forward", "L", 7, 1,
             "legacy-guardian-ref", "EXT-P1"))
        cur.execute(sql(
            "INSERT INTO guardian_links (id, guardian_user_id, player_id, "
            "created_at, verified, consent_method, consented_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)"),
            ("gl1", "guardian-user", "p1", "2026-01-01T00:00:00+00:00", 1,
             "signed_form", "2026-01-01T00:00:00+00:00"))
        cur.execute(sql(
            "INSERT INTO games (id, home_team_id, start_time) "
            "VALUES (?, ?, ?)"), ("g1", "t1", "2026-01-02T00:00:00+00:00"))
        cur.execute(sql(
            "INSERT INTO game_roster_entries (id, game_id, player_id, "
            "roster_role, selection_source, status, selected_at, "
            "updated_at, selected_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"),
            ("re1", "g1", "p1", "selected", "coach_selected", "selected",
             "2026-01-02T00:00:00+00:00", "2026-01-02T00:00:00+00:00", "op"))
        store.conn.commit()

    def _assert_seed_data_intact(self, store, label):
        player = store.get_player("p1")
        self.assertEqual(player.name, "Junior Player", label)
        self.assertEqual(player.jersey_number, 7, label)
        for field in _NEW_COLUMNS:
            self.assertIsNone(getattr(player, field), f"{label}: {field}")
        links = store.guardian_links_for_player("p1")
        self.assertEqual(
            [(l.id, l.guardian_user_id, l.verified) for l in links],
            [("gl1", "guardian-user", True)], label)
        self.assertEqual(
            [e.id for e in store.roster_entries_for_player("p1")],
            ["re1"], label)
        cur = store.conn.cursor()
        cur.execute(store.dialect.sql(
            "SELECT guardian_person_id FROM players WHERE id = ?"), ("p1",))
        self.assertEqual(cur.fetchone()["guardian_person_id"],
                         "legacy-guardian-ref", label)

    def test_upgrade_from_main_057_applies_058_once_and_matches_fresh(self):
        for label, base in self._locations():
            with self.subTest(backend=label):
                url = self._new_url(label, base, "up")
                main_only_dir = _copy_migrations_dir(
                    exclude={"058_athlete_identity.sql"})
                self.addCleanup(shutil.rmtree, main_only_dir,
                                ignore_errors=True)

                # (1) Build EXACTLY current-main's schema/ledger state:
                # migrated through 057 by REPLAYING the real main files,
                # 058_athlete_identity never applied — simulating a real
                # existing production installation about to receive this
                # PR's deploy.
                _sql_store_module._MIGRATIONS_DIR = main_only_dir
                try:
                    store = SqlStore(url)
                finally:
                    _sql_store_module._MIGRATIONS_DIR = _BRANCH_MIGRATIONS_DIR

                self.assertNotIn("first_name",
                                 _table_columns(store, "players"), label)
                cur = store.conn.cursor()
                cur.execute(store.dialect.sql(
                    "SELECT COUNT(*) AS n FROM schema_migrations "
                    "WHERE version = ?"), (_MIGRATION,))
                self.assertEqual(
                    cur.fetchone()["n"], 0,
                    f"{label}: 058_athlete_identity must not be applied "
                    "yet on the pre-deploy replay")
                cur.execute(store.dialect.sql(
                    "SELECT COUNT(*) AS n FROM schema_migrations "
                    "WHERE version = ?"), ("057_device_token_unique_key",))
                self.assertEqual(
                    cur.fetchone()["n"], 1,
                    f"{label}: sanity, the replay really did reach "
                    "main's 057")

                self._seed_pre_058_data(store)
                store.close()

                # (2) THE DEPLOY: this branch's migrations/ directory (058
                # included) lands; the app restarts and migrate() runs
                # again against the SAME, already-existing database.
                upgraded = SqlStore(url)
                try:
                    cur = upgraded.conn.cursor()
                    cur.execute(upgraded.dialect.sql(
                        "SELECT COUNT(*) AS n FROM schema_migrations "
                        "WHERE version = ?"), (_MIGRATION,))
                    self.assertEqual(
                        cur.fetchone()["n"], 1,
                        f"{label}: 058_athlete_identity must apply "
                        "exactly once")
                    self.assertLessEqual(
                        set(_NEW_COLUMNS),
                        _table_columns(upgraded, "players"), label)

                    # (4) existing identity/guardian/roster data survives
                    # the upgrade unchanged.
                    self._assert_seed_data_intact(upgraded, label)

                    upgraded_player_cols = _table_columns(upgraded, "players")
                    upgraded_rule_cols = _table_columns(
                        upgraded, "age_eligibility_rules")
                    upgraded_ledger = _ledger_versions(upgraded)
                finally:
                    upgraded.close()

                # (3) idempotent reopen: migrate() runs again with nothing
                # left to apply — no error, still recorded exactly once.
                reopened = SqlStore(url)
                try:
                    cur = reopened.conn.cursor()
                    cur.execute(reopened.dialect.sql(
                        "SELECT COUNT(*) AS n FROM schema_migrations "
                        "WHERE version = ?"), (_MIGRATION,))
                    self.assertEqual(
                        cur.fetchone()["n"], 1,
                        f"{label}: idempotent reopen must not "
                        "re-apply or duplicate the row")
                    self.assertEqual(
                        _table_columns(reopened, "players"),
                        upgraded_player_cols, label)
                    self.assertEqual(
                        _ledger_versions(reopened), upgraded_ledger,
                        f"{label}: idempotent reopen must not change "
                        "the recorded ledger set")
                finally:
                    reopened.close()

                # (5) parity: a FRESH database, migrated 001 through 058 in
                # ONE pass, ends up at the identical final schema AND the
                # identical recorded ledger set as the upgraded-from-057
                # path above.
                fresh_url = self._new_url(label, base, "fresh")
                fresh = SqlStore(fresh_url)
                try:
                    self.assertEqual(
                        upgraded_player_cols,
                        _table_columns(fresh, "players"),
                        f"{label}: fresh-install players columns must "
                        "match the upgraded-from-057 path exactly")
                    self.assertEqual(
                        upgraded_rule_cols,
                        _table_columns(fresh, "age_eligibility_rules"),
                        f"{label}: fresh-install age_eligibility_rules "
                        "columns must match the upgraded-from-057 path "
                        "exactly")
                    self.assertEqual(
                        upgraded_ledger, _ledger_versions(fresh),
                        f"{label}: fresh-install and upgraded-from-057 "
                        "must record the identical set of applied "
                        "migrations")
                finally:
                    fresh.close()


if __name__ == "__main__":
    unittest.main()
