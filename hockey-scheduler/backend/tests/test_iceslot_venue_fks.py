"""Referential integrity for the facility hierarchy (#201 Slice 3).

SCOPE: this is the FACILITY-HIERARCHY subset of #201's no-row-lock work — it adds
four foreign keys — rinks.venue_id → venues(id), ice_slots.rink_id → rinks(id),
games.ice_slot_id → ice_slots(id), and season_venue_access.venue_id → venues(id)
— as the DATABASE backstop for the reviewer-catalogued "no row lock" races
(create_rink vs delete_venue, create_ice_slot vs delete_rink, create_game vs
delete_ice_slot, grant_season_venue_access vs delete_venue). It ALSO adds the
store-boundary translation of migration 022's ux_games_active_ice_slot violation
into a stable ScheduleConflictError (ice_slot_taken) — the cross-Season slot
double-booking loser. It does NOT add a season-side FK to season_venue_access:
grant and delete_season both take the Season row lock, so that pair already
serialises.

It does NOT complete #201's no-row-lock scope: the Program/Organization structural
races (create_program vs delete_organization → programs.operator_organization_id →
organizations; create_season vs delete_program → seasons.program_id → programs)
remain and are tracked as a separate follow-up slice on #201.

All four foreign keys are NAMED (fk_rinks_venue, fk_ice_slots_rink,
fk_games_ice_slot, fk_sva_venue) with explicit ON DELETE NO ACTION — never
cascade, never data cleanup. The facility deletes take NO row lock (the FK-only
direction), so:
  * a child create that races behind a parent delete loses at the foreign key
    and surfaces the stable venue_not_found / rink_not_found / ice_slot_not_found
    conflict;
  * a parent delete that races behind a committed child blocks on the child's FK
    key-share lock, then surfaces the stable has-dependencies block (the incoming
    reference now exists) — never a cascade or orphan.
Neither direction lets a raw psycopg/sqlite exception escape, orphans a row, or
writes the losing operation's audit.

Coverage:
  * pre-migration orphan detection with counts + row ids, fail-closed with ZERO
    DDL/data mutation (SQLite + PostgreSQL);
  * clean upgrade re-adds all four named FKs; foreign_key_check is clean;
    enforcement survives restart; exact catalog assertions;
  * store-boundary translation into the stable *_not_found conflicts and the
    ice_slot_taken double-book conflict, insert and update paths;
  * service-level rejection of a missing parent and delete-dependency blocks on
    Memory + SQLite + PostgreSQL (sequential parity, both directions);
  * five PostgreSQL race families (create_rink vs delete_venue, create_ice_slot
    vs delete_rink, create_game vs delete_ice_slot, grant_season_venue_access vs
    delete_venue, cross-Season create_game vs create_game double-book), each
    proven by a forced parent-delete-wins (or double-book-loser) ordering, a
    forced child-commit-wins ordering with a per-PID lock-wait proof, and an open
    barrier asserting exactly one valid linearization.
"""

import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import IceSlotType, Position  # noqa: F401
from hockey_scheduler.domain.errors import (
    DomainError,
    HasDependenciesError,
    IntegrityConflictError,
    NotFoundError,
    ScheduleConflictError,
)
from hockey_scheduler.domain.models import Game
from hockey_scheduler.domain.setup_models import IceSlot, Rink, SeasonVenueAccess
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.store.integrity_checks import (
    MigrationDataError,
    assert_iceslot_venue_fks_ready,
    find_orphan_game_ice_slots,
    find_orphan_ice_slot_rinks,
    find_orphan_rink_venues,
    find_orphan_sva_venues,
)
from hockey_scheduler.store.sql_store import migrate

UTC = timezone.utc
_VERSION = "041_iceslot_venue_fks"


def _sql_backends():
    backends = [("sqlite", ":memory:")]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        backends.append(("postgres", url))
    return backends


def _fresh(url):
    store = SqlStore(url)
    if url != ":memory:":
        store.reset_schema()
    return store


# -- raw planting (bypasses the service so dangling refs can be created) ----
def _add_venue_raw(store, vid):
    store.conn.cursor().execute(
        store.dialect.sql("INSERT INTO venues (id, name) VALUES (?, ?)"),
        (vid, vid))


def _add_rink_raw(store, rid, venue_id=None):
    store.conn.cursor().execute(
        store.dialect.sql("INSERT INTO rinks (id, venue_id, name) "
                          "VALUES (?, ?, ?)"), (rid, venue_id, rid))


def _add_slot_raw(store, sid, rink_id=None):
    store.conn.cursor().execute(
        store.dialect.sql("INSERT INTO ice_slots (id, rink_id) VALUES (?, ?)"),
        (sid, rink_id))


def _add_game_raw(store, gid, ice_slot_id=None):
    store.conn.cursor().execute(
        store.dialect.sql(
            "INSERT INTO games (id, ice_slot_id, cancelled, game_type) "
            "VALUES (?, ?, 0, 'regular')"), (gid, ice_slot_id))


def _add_sva_raw(store, aid, venue_id, season_id="s"):
    store.conn.cursor().execute(
        store.dialect.sql(
            "INSERT INTO season_venue_access (id, season_id, venue_id, active) "
            "VALUES (?, ?, ?, 1)"), (aid, season_id, venue_id))


def _downgrade_041(store):
    """Reverse migration 041 to the prior schema: drop all four FKs and un-record
    it. Postgres drops the named constraints; SQLite rebuilds each table without
    its FK (recreating every index), mirroring 041's create-copy-drop-rename.
    Dangling rows can then be planted before it re-runs."""
    with store.transaction():
        cur = store.conn.cursor()
        if store.backend == "postgres":
            cur.execute("ALTER TABLE rinks DROP CONSTRAINT IF EXISTS fk_rinks_venue")
            cur.execute("ALTER TABLE ice_slots "
                        "DROP CONSTRAINT IF EXISTS fk_ice_slots_rink")
            cur.execute("ALTER TABLE games "
                        "DROP CONSTRAINT IF EXISTS fk_games_ice_slot")
            cur.execute("ALTER TABLE season_venue_access "
                        "DROP CONSTRAINT IF EXISTS fk_sva_venue")
        else:
            cur.execute("PRAGMA defer_foreign_keys = ON")
            # rinks (no FK)
            cur.execute("CREATE TABLE rinks_old (id TEXT PRIMARY KEY, "
                        "venue_id TEXT, name TEXT, external_ref TEXT)")
            cur.execute("INSERT INTO rinks_old SELECT id, venue_id, name, "
                        "external_ref FROM rinks")
            cur.execute("DROP TABLE rinks")
            cur.execute("ALTER TABLE rinks_old RENAME TO rinks")
            # ice_slots (no FK)
            cur.execute("CREATE TABLE ice_slots_old (id TEXT PRIMARY KEY, "
                        "rink_id TEXT, start_time TEXT, end_time TEXT, "
                        "slot_type TEXT, status TEXT)")
            cur.execute("INSERT INTO ice_slots_old SELECT id, rink_id, "
                        "start_time, end_time, slot_type, status FROM ice_slots")
            cur.execute("DROP TABLE ice_slots")
            cur.execute("ALTER TABLE ice_slots_old RENAME TO ice_slots")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_ice_slots_rink "
                        "ON ice_slots (rink_id, start_time)")
            # games (no FK) — every column preserved.
            cur.execute(
                "CREATE TABLE games_old (id TEXT PRIMARY KEY, home_team_id TEXT, "
                "start_time TEXT, target_goalies INTEGER, target_skaters INTEGER, "
                "max_skaters INTEGER, away_team_id TEXT, rink TEXT, end_time TEXT, "
                "roster_lock_time TEXT, locked INTEGER, cancelled INTEGER, "
                "published INTEGER, season_id TEXT, division_id TEXT, "
                "ice_slot_id TEXT, is_draft INTEGER, league_id TEXT, "
                "game_type TEXT NOT NULL DEFAULT 'regular', league_season_id TEXT)")
            cur.execute(
                "INSERT INTO games_old SELECT id, home_team_id, start_time, "
                "target_goalies, target_skaters, max_skaters, away_team_id, rink, "
                "end_time, roster_lock_time, locked, cancelled, published, "
                "season_id, division_id, ice_slot_id, is_draft, league_id, "
                "game_type, league_season_id FROM games")
            cur.execute("DROP TABLE games")
            cur.execute("ALTER TABLE games_old RENAME TO games")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_games_slot "
                        "ON games (ice_slot_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_games_teams "
                        "ON games (home_team_id, away_team_id)")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_games_active_ice_slot "
                        "ON games (ice_slot_id) "
                        "WHERE cancelled = 0 AND ice_slot_id IS NOT NULL")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_games_game_type "
                        "ON games (game_type)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_games_league_season "
                        "ON games (league_season_id)")
            # season_venue_access (no FK)
            cur.execute("CREATE TABLE season_venue_access_old (id TEXT PRIMARY KEY, "
                        "season_id TEXT NOT NULL, venue_id TEXT NOT NULL, "
                        "active INTEGER NOT NULL DEFAULT 1)")
            cur.execute("INSERT INTO season_venue_access_old SELECT id, season_id, "
                        "venue_id, active FROM season_venue_access")
            cur.execute("DROP TABLE season_venue_access")
            cur.execute("ALTER TABLE season_venue_access_old "
                        "RENAME TO season_venue_access")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS "
                        "ux_season_venue_access_active "
                        "ON season_venue_access (season_id, venue_id) "
                        "WHERE active = 1")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_season_venue_access_season "
                        "ON season_venue_access (season_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_season_venue_access_venue "
                        "ON season_venue_access (venue_id)")
        cur.execute(store.dialect.sql(
            "DELETE FROM schema_migrations WHERE version = ?"), (_VERSION,))


# -- schema-catalog inspection (exact named constraints) ------------------
def _fks(store, table):
    cur = store.conn.cursor()
    if store.backend == "sqlite":
        cur.execute(f"PRAGMA foreign_key_list('{table}')")
        return {(r["from"], r["table"], r["to"], None,
                 (r["on_delete"] or "NO ACTION").upper())
                for r in cur.fetchall()}
    cur.execute(
        "SELECT c.conname AS name, a.attname AS col, "
        "       confrelid::regclass::text AS ref, af.attname AS refcol, "
        "       c.confdeltype AS del "
        "FROM pg_constraint c "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid "
        "  AND a.attnum = ANY(c.conkey) "
        "JOIN pg_attribute af ON af.attrelid = c.confrelid "
        "  AND af.attnum = ANY(c.confkey) "
        "WHERE c.contype = 'f' AND c.conrelid = %s::regclass",
        (table,))
    delmap = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE",
              "n": "SET NULL", "d": "SET DEFAULT"}
    return {(r["col"], r["ref"], r["refcol"], r["name"],
             delmap.get(r["del"], r["del"])) for r in cur.fetchall()}


def _name(store, pg_name):
    return pg_name if store.backend == "postgres" else None


def _foreign_key_check_clean(store):
    if store.backend != "sqlite":
        return True
    cur = store.conn.cursor()
    cur.execute("PRAGMA foreign_key_check")
    return cur.fetchall() == []


def _indexes(store, table):
    """Set of secondary index names on ``table`` (excludes auto/PK indexes)."""
    cur = store.conn.cursor()
    if store.backend == "sqlite":
        cur.execute(f"PRAGMA index_list('{table}')")
        return {r["name"] for r in cur.fetchall()
                if not r["name"].startswith("sqlite_autoindex")}
    cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = %s", (table,))
    return {r["indexname"] for r in cur.fetchall()
            if not r["indexname"].endswith("_pkey")}


def _is_partial_unique(store, table, index_name):
    """True if ``index_name`` on ``table`` is a partial (WHERE-clause) UNIQUE
    index — the shape of ux_games_active_ice_slot that must survive the rebuild."""
    cur = store.conn.cursor()
    if store.backend == "sqlite":
        cur.execute(f"SELECT name, [unique], partial FROM pragma_index_list('{table}')")
        for r in cur.fetchall():
            if r["name"] == index_name:
                return bool(r["unique"]) and bool(r["partial"])
        return False
    cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname = %s", (index_name,))
    row = cur.fetchone()
    return bool(row) and "UNIQUE" in row["indexdef"] and "WHERE" in row["indexdef"]


class PreMigrationValidationTest(unittest.TestCase):
    def _pre041(self, url):
        store = _fresh(url)
        _downgrade_041(store)
        return store

    def _cleanup(self, store):
        if store.backend != "sqlite":
            store.reset_schema()
        store.close()

    def test_orphans_reported_with_counts_and_ids_zero_mutation(self):
        for label, url in _sql_backends():
            store = self._pre041(url)
            try:
                _add_venue_raw(store, "v1")
                _add_rink_raw(store, "r1", "v1")     # valid rink → venue
                with store.transaction():
                    _add_rink_raw(store, "r_bad", "ghost_venue")   # dangling
                    _add_rink_raw(store, "r_null", None)           # NULL — allowed
                    _add_slot_raw(store, "s_ok", "r1")             # valid slot
                    _add_slot_raw(store, "s_bad", "ghost_rink")    # dangling
                    _add_slot_raw(store, "s_null", None)           # NULL — allowed
                    _add_game_raw(store, "g_ok", "s_ok")           # valid game
                    _add_game_raw(store, "g_bad", "ghost_slot")    # dangling
                    _add_game_raw(store, "g_null", None)           # NULL — allowed
                    _add_sva_raw(store, "a_ok", "v1")              # valid access
                    _add_sva_raw(store, "a_bad", "ghost_venue")    # dangling
                self.assertEqual(find_orphan_rink_venues(store.conn),
                                 ["r_bad"], label)
                self.assertEqual(find_orphan_ice_slot_rinks(store.conn),
                                 ["s_bad"], label)
                self.assertEqual(find_orphan_game_ice_slots(store.conn),
                                 ["g_bad"], label)
                self.assertEqual(find_orphan_sva_venues(store.conn),
                                 ["a_bad"], label)
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    assert_iceslot_venue_fks_ready(store.conn)
                msg = str(ctx.exception)
                for token in ("1 rink(s)", "r_bad", "1 ice slot(s)", "s_bad",
                              "1 game(s)", "g_bad", "a_bad"):
                    self.assertIn(token, msg, label)
                with self.assertRaises(MigrationDataError, msg=label):
                    migrate(store.conn, store.dialect)
                # FAIL-CLOSED: zero mutation — no FK added, ledger not advanced.
                self.assertEqual(_fks(store, "rinks"), set(), label)
                self.assertEqual(_fks(store, "games"), set(), label)
                self.assertNotIn(_VERSION, store.migration_status()["applied"],
                                 label)
            finally:
                self._cleanup(store)

    def test_valid_and_null_references_upgrade_cleanly(self):
        for label, url in _sql_backends():
            store = self._pre041(url)
            try:
                _add_venue_raw(store, "v1")
                with store.transaction():
                    _add_rink_raw(store, "r1", "v1")   # valid
                    _add_rink_raw(store, "r2", None)   # NULL — allowed
                    _add_slot_raw(store, "s1", "r1")   # valid
                    _add_slot_raw(store, "s2", None)   # NULL — allowed
                    _add_game_raw(store, "g1", "s1")   # valid
                    _add_game_raw(store, "g2", None)   # NULL — allowed
                    _add_sva_raw(store, "a1", "v1")    # valid
                self.assertEqual(find_orphan_rink_venues(store.conn), [], label)
                self.assertEqual(find_orphan_ice_slot_rinks(store.conn), [], label)
                self.assertEqual(find_orphan_game_ice_slots(store.conn), [], label)
                self.assertEqual(find_orphan_sva_venues(store.conn), [], label)
                assert_iceslot_venue_fks_ready(store.conn)  # must not raise
                migrate(store.conn, store.dialect)          # succeeds; FKs added
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
                self.assertEqual(
                    _fks(store, "rinks"),
                    {("venue_id", "venues", "id", _name(store, "fk_rinks_venue"),
                      "NO ACTION")}, label)
                self.assertEqual(
                    _fks(store, "ice_slots"),
                    {("rink_id", "rinks", "id",
                      _name(store, "fk_ice_slots_rink"), "NO ACTION")}, label)
                self.assertEqual(
                    _fks(store, "games"),
                    {("ice_slot_id", "ice_slots", "id",
                      _name(store, "fk_games_ice_slot"), "NO ACTION")}, label)
                self.assertEqual(
                    _fks(store, "season_venue_access"),
                    {("venue_id", "venues", "id", _name(store, "fk_sva_venue"),
                      "NO ACTION")}, label)
                self.assertTrue(_foreign_key_check_clean(store), label)
            finally:
                self._cleanup(store)


class StoreBoundaryTranslationTest(unittest.TestCase):
    """A racing write onto a missing parent surfaces as the stable domain
    conflict; a duplicate active slot surfaces as ice_slot_taken."""

    def test_inserting_rink_onto_missing_venue_is_venue_not_found(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_rink(Rink(id="r1", venue_id="ghost", name="R"))
                self.assertEqual(ctx.exception.details["reason"],
                                 "venue_not_found", label)
                self.assertEqual(ctx.exception.details["venue_id"], "ghost", label)
                self.assertIsNone(store.get_rink("r1"), label)
            finally:
                store.close()

    def test_saving_rink_onto_missing_venue_is_venue_not_found(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _add_venue_raw(store, "v1")
                with store.transaction():
                    store.add_rink(Rink(id="r1", venue_id="v1", name="R"))
                moved = store.get_rink("r1")
                moved.venue_id = "ghost"
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.save_rink(moved)
                self.assertEqual(ctx.exception.details["reason"],
                                 "venue_not_found", label)
                self.assertEqual(store.get_rink("r1").venue_id, "v1", label)
            finally:
                store.close()

    def test_inserting_slot_onto_missing_rink_is_rink_not_found(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_ice_slot(IceSlot(
                            id="s1", rink_id="ghost",
                            start_time=datetime(2027, 1, 1, tzinfo=UTC),
                            end_time=datetime(2027, 1, 1, 1, tzinfo=UTC)))
                self.assertEqual(ctx.exception.details["reason"],
                                 "rink_not_found", label)
                self.assertEqual(ctx.exception.details["rink_id"], "ghost", label)
                self.assertIsNone(store.get_ice_slot("s1"), label)
            finally:
                store.close()

    def test_inserting_game_onto_missing_slot_is_ice_slot_not_found(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_game(Game(
                            id="g1", home_team_id="h", away_team_id="a",
                            start_time=None, ice_slot_id="ghost"))
                self.assertEqual(ctx.exception.details["reason"],
                                 "ice_slot_not_found", label)
                self.assertEqual(ctx.exception.details["ice_slot_id"], "ghost",
                                 label)
                self.assertIsNone(store.get_game("g1"), label)
            finally:
                store.close()

    def test_inserting_access_onto_missing_venue_is_venue_not_found(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_season_venue_access(SeasonVenueAccess(
                            id="a1", season_id="s", venue_id="ghost", active=True))
                self.assertEqual(ctx.exception.details["reason"],
                                 "venue_not_found", label)
                self.assertEqual(ctx.exception.details["venue_id"], "ghost", label)
                self.assertIsNone(store.get_season_venue_access("a1"), label)
            finally:
                store.close()

    def test_second_active_game_on_slot_is_ice_slot_taken(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _add_slot_raw(store, "slot1")   # NULL rink is fine
                with store.transaction():
                    store.add_game(Game(id="g1", home_team_id="h", away_team_id="a",
                                        start_time=None, ice_slot_id="slot1",
                                        cancelled=False))
                with self.assertRaises(ScheduleConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_game(Game(id="g2", home_team_id="x",
                                            away_team_id="y", start_time=None,
                                            ice_slot_id="slot1", cancelled=False))
                self.assertEqual(ctx.exception.details["reason"],
                                 "ice_slot_taken", label)
                self.assertEqual(ctx.exception.details["ice_slot_id"], "slot1",
                                 label)
                self.assertIsNone(store.get_game("g2"), label)
            finally:
                store.close()

    def test_saving_slot_onto_missing_rink_is_rink_not_found(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _add_rink_raw(store, "r1", None)
                with store.transaction():
                    store.add_ice_slot(IceSlot(
                        id="s1", rink_id="r1",
                        start_time=datetime(2027, 1, 1, tzinfo=UTC),
                        end_time=datetime(2027, 1, 1, 1, tzinfo=UTC)))
                moved = store.get_ice_slot("s1")
                moved.rink_id = "ghost"
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.save_ice_slot(moved)
                self.assertEqual(ctx.exception.details["reason"],
                                 "rink_not_found", label)
                self.assertEqual(store.get_ice_slot("s1").rink_id, "r1", label)
            finally:
                store.close()

    def test_saving_game_onto_missing_slot_is_ice_slot_not_found(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _add_slot_raw(store, "s1")
                with store.transaction():
                    store.add_game(Game(id="g1", home_team_id="h", away_team_id="a",
                                        start_time=None, ice_slot_id="s1",
                                        cancelled=False))
                moved = store.get_game("g1")
                moved.ice_slot_id = "ghost"
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.save_game(moved)
                self.assertEqual(ctx.exception.details["reason"],
                                 "ice_slot_not_found", label)
                self.assertEqual(store.get_game("g1").ice_slot_id, "s1", label)
            finally:
                store.close()

    def test_saving_game_onto_booked_slot_is_ice_slot_taken(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _add_slot_raw(store, "s1")
                _add_slot_raw(store, "s2")
                with store.transaction():
                    store.add_game(Game(id="g1", home_team_id="h", away_team_id="a",
                                        start_time=None, ice_slot_id="s1",
                                        cancelled=False))
                    store.add_game(Game(id="g2", home_team_id="x", away_team_id="y",
                                        start_time=None, ice_slot_id="s2",
                                        cancelled=False))
                moved = store.get_game("g2")
                moved.ice_slot_id = "s1"   # collides with g1's active booking
                with self.assertRaises(ScheduleConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.save_game(moved)
                self.assertEqual(ctx.exception.details["reason"],
                                 "ice_slot_taken", label)
                self.assertEqual(store.get_game("g2").ice_slot_id, "s2", label)
            finally:
                store.close()

    def test_saving_access_onto_missing_venue_is_venue_not_found(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _add_venue_raw(store, "v1")
                with store.transaction():
                    store.add_season_venue_access(SeasonVenueAccess(
                        id="a1", season_id="s", venue_id="v1", active=True))
                moved = store.get_season_venue_access("a1")
                moved.venue_id = "ghost"
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.save_season_venue_access(moved)
                self.assertEqual(ctx.exception.details["reason"],
                                 "venue_not_found", label)
                self.assertEqual(store.get_season_venue_access("a1").venue_id,
                                 "v1", label)
            finally:
                store.close()

    def test_null_parents_are_allowed(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                with store.transaction():
                    store.add_rink(Rink(id="r1", venue_id=None, name="R"))
                    store.add_ice_slot(IceSlot(
                        id="s1", rink_id=None,
                        start_time=datetime(2027, 1, 1, tzinfo=UTC),
                        end_time=datetime(2027, 1, 1, 1, tzinfo=UTC)))
                    store.add_game(Game(id="g1", home_team_id="h", away_team_id="a",
                                        start_time=None, ice_slot_id=None))
                self.assertIsNotNone(store.get_rink("r1"), label)
                self.assertIsNotNone(store.get_ice_slot("s1"), label)
                self.assertIsNotNone(store.get_game("g1"), label)
            finally:
                store.close()


class MigrationLedgerTest(unittest.TestCase):
    def test_fks_recorded_and_enforced_after_reopen(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            first = SqlStore(path)
            self.assertIn(_VERSION, first.migration_status()["applied"])
            self.assertEqual(
                _fks(first, "games"),
                {("ice_slot_id", "ice_slots", "id", None, "NO ACTION")})
            self.assertEqual(
                _fks(first, "rinks"),
                {("venue_id", "venues", "id", None, "NO ACTION")})
            first.close()
            second = SqlStore(path)   # reopened — enforcement survives restart
            self.assertTrue(second.migration_status()["current"])
            self.assertTrue(_foreign_key_check_clean(second))
            with self.assertRaises(IntegrityConflictError):
                with second.transaction():
                    second.add_rink(Rink(id="r1", venue_id="ghost", name="R"))
            second.close()
        finally:
            os.remove(path)


class MigrationRebuildPreservationTest(unittest.TestCase):
    """The SQLite create-copy-drop-rename of rinks/ice_slots/games/
    season_venue_access is a physical table rewrite. This is the durable
    regression proof that migration 041 preserves — across a close/reopen — every
    row value, every recreated index (including the partial unique
    ux_games_active_ice_slot), the new outgoing FKs, AND the incoming
    game_results → games / game_roster_entries → games references the migration
    documentation promises to keep. Runs upgrade-from-040 on a file-backed SQLite
    database (so the reopen is real) and on PostgreSQL when available."""

    # Non-default values in EVERY column of each rebuilt table — a column drop,
    # reorder, or truncation in the rebuild would change one of these.
    _VENUE = ("v1", "Ice Palace", "1 Rink Rd", "America/New_York")
    _RINK = ("r1", "v1", "North Rink", "EXT-R1")
    _SLOT = ("s1", "r1", "2027-03-01T18:00:00+00:00",
             "2027-03-01T19:30:00+00:00", "game", "allocated")
    _SVA = ("a1", "seas1", "v1", 1)
    _GAME = ("g1", "team_home", "2027-03-01T18:00:00+00:00", 2, 17, 21,
             "team_away", "North Rink", "2027-03-01T19:30:00+00:00",
             "2027-03-01T17:00:00+00:00", 1, 0, 1, "seas1", "div1", "s1", 1,
             "lg1", "exhibition", "ls1")
    _GAME_COLS = ("id", "home_team_id", "start_time", "target_goalies",
                  "target_skaters", "max_skaters", "away_team_id", "rink",
                  "end_time", "roster_lock_time", "locked", "cancelled",
                  "published", "season_id", "division_id", "ice_slot_id",
                  "is_draft", "league_id", "game_type", "league_season_id")

    def _locations(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._tmp = path
        yield "sqlite", path
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            yield "postgres", url

    def tearDown(self):
        if getattr(self, "_tmp", None) and os.path.exists(self._tmp):
            os.remove(self._tmp)

    def _seed_rich(self, store):
        cur = store.conn.cursor()
        q = store.dialect.sql

        def ins(sql, params):
            cur.execute(q(sql), params)

        # Parents for the incoming-FK dependents (teams→clubs, players→teams).
        ins("INSERT INTO clubs (id, name) VALUES (?, ?)", ("c1", "Club One"))
        ins("INSERT INTO teams (id, name, club_id) VALUES (?, ?, ?)",
            ("t1", "Team One", "c1"))
        ins("INSERT INTO players (id, team_id, name, position) VALUES (?, ?, ?, ?)",
            ("p1", "t1", "Player One", "forward"))
        # The four rebuilt tables, every column set to a non-default value.
        ins("INSERT INTO venues (id, name, address, timezone) VALUES (?, ?, ?, ?)",
            self._VENUE)
        ins("INSERT INTO rinks (id, venue_id, name, external_ref) "
            "VALUES (?, ?, ?, ?)", self._RINK)
        ins("INSERT INTO ice_slots (id, rink_id, start_time, end_time, slot_type, "
            "status) VALUES (?, ?, ?, ?, ?, ?)", self._SLOT)
        ins("INSERT INTO season_venue_access (id, season_id, venue_id, active) "
            "VALUES (?, ?, ?, ?)", self._SVA)
        ins("INSERT INTO games (" + ", ".join(self._GAME_COLS) + ") VALUES ("
            + ", ".join("?" for _ in self._GAME_COLS) + ")", self._GAME)
        # Both incoming Game dependents (game_results + game_roster_entries).
        ins("INSERT INTO game_results (id, game_id, home_score, away_score) "
            "VALUES (?, ?, ?, ?)", ("gr1", "g1", 3, 2))
        ins("INSERT INTO game_roster_entries (id, game_id, player_id) "
            "VALUES (?, ?, ?)", ("re1", "g1", "p1"))

    def _row(self, store, sql, params=()):
        cur = store.conn.cursor()
        cur.execute(store.dialect.sql(sql), params)
        return cur.fetchone()

    def _row_values(self, store, cols, sql, params):
        # sqlite3.Row supports tuple(row) → values, but psycopg's dict_row makes
        # tuple(row) → keys; index by column name so the comparison is portable.
        r = self._row(store, sql, params)
        return tuple(r[c] for c in cols)

    def _assert_preserved(self, store, label):
        # 1) Exact row-for-row values in every rebuilt table.
        venue_cols = ("id", "name", "address", "timezone")
        self.assertEqual(
            self._row_values(store, venue_cols,
                             "SELECT id, name, address, timezone FROM venues "
                             "WHERE id = ?", ("v1",)), self._VENUE, label)
        rink_cols = ("id", "venue_id", "name", "external_ref")
        self.assertEqual(
            self._row_values(store, rink_cols,
                             "SELECT id, venue_id, name, external_ref FROM rinks "
                             "WHERE id = ?", ("r1",)), self._RINK, label)
        slot_cols = ("id", "rink_id", "start_time", "end_time", "slot_type",
                     "status")
        self.assertEqual(
            self._row_values(store, slot_cols,
                             "SELECT id, rink_id, start_time, end_time, "
                             "slot_type, status FROM ice_slots WHERE id = ?",
                             ("s1",)), self._SLOT, label)
        sva_cols = ("id", "season_id", "venue_id", "active")
        self.assertEqual(
            self._row_values(store, sva_cols,
                             "SELECT id, season_id, venue_id, active FROM "
                             "season_venue_access WHERE id = ?", ("a1",)),
            self._SVA, label)
        self.assertEqual(
            self._row_values(store, self._GAME_COLS,
                             "SELECT " + ", ".join(self._GAME_COLS)
                             + " FROM games WHERE id = ?", ("g1",)),
            self._GAME, label)
        # 2) Incoming Game references survived the games rebuild.
        r = self._row(store, "SELECT game_id FROM game_results WHERE id = ?",
                      ("gr1",))
        self.assertEqual(r["game_id"], "g1", label)
        r = self._row(store, "SELECT game_id, player_id FROM game_roster_entries "
                             "WHERE id = ?", ("re1",))
        self.assertEqual((r["game_id"], r["player_id"]), ("g1", "p1"), label)
        # 3) Outgoing + incoming FK catalogs.
        self.assertEqual(
            _fks(store, "games"),
            {("ice_slot_id", "ice_slots", "id",
              _name(store, "fk_games_ice_slot"), "NO ACTION")}, label)
        self.assertEqual(
            _fks(store, "rinks"),
            {("venue_id", "venues", "id", _name(store, "fk_rinks_venue"),
              "NO ACTION")}, label)
        result_fk_targets = {ref for (_c, ref, _t, _n, _d)
                             in _fks(store, "game_results")}
        self.assertIn("games", result_fk_targets, label)
        roster_fk_targets = {ref for (_c, ref, _t, _n, _d)
                             in _fks(store, "game_roster_entries")}
        self.assertIn("games", roster_fk_targets, label)
        self.assertIn("players", roster_fk_targets, label)
        # 4) Every recreated index, including the partial unique one.
        game_idx = _indexes(store, "games")
        for name in ("ix_games_slot", "ix_games_teams", "ux_games_active_ice_slot",
                     "ix_games_game_type", "ix_games_league_season"):
            self.assertIn(name, game_idx, f"{label}: games index {name}")
        self.assertTrue(
            _is_partial_unique(store, "games", "ux_games_active_ice_slot"), label)
        self.assertIn("ix_ice_slots_rink", _indexes(store, "ice_slots"), label)
        sva_idx = _indexes(store, "season_venue_access")
        for name in ("ux_season_venue_access_active",
                     "ix_season_venue_access_season", "ix_season_venue_access_venue"):
            self.assertIn(name, sva_idx, f"{label}: sva index {name}")
        self.assertTrue(
            _is_partial_unique(store, "season_venue_access",
                               "ux_season_venue_access_active"), label)
        # 5) A clean structural check (SQLite).
        self.assertTrue(_foreign_key_check_clean(store), label)

    def test_upgrade_from_040_preserves_values_indexes_and_incoming_refs(self):
        for label, loc in self._locations():
            store = SqlStore(loc)
            try:
                if store.backend == "postgres":
                    store.reset_schema()
                _downgrade_041(store)          # back to the pre-041 (post-040) schema
                with store.transaction():
                    self._seed_rich(store)
                migrate(store.conn, store.dialect)   # apply 041 (the rebuild)
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
                store.close()
                store = SqlStore(loc)          # reopen — a real close/reopen cycle
                self._assert_preserved(store, label)
            finally:
                if store.backend == "postgres":
                    store.reset_schema()       # leave the shared DB pristine
                store.close()


# =====================================================================
# Service-level parity: Memory + SQLite + PostgreSQL
# =====================================================================
def _seed_arena(api):
    """Program → active Season → League → Division → two registered Teams →
    Venue (+access) → Rink. Returns a dict of ids. No slot/game yet."""
    pid = api.create_program("Prog", "US", "UTC")["id"]
    sid = api.create_season(pid, "S1")["id"]
    lid = api.create_league(sid, "Gold")["id"]
    did = api.create_division(sid, "D1", league_id=lid)["id"]
    club = api.create_club("Club")["id"]
    home = api.create_team(club_id=club, name="Alpha", league_id=lid,
                           division_id=did)["id"]
    away = api.create_team(club_id=club, name="Beta", league_id=lid,
                           division_id=did)["id"]
    api.setup.register_team_for_season(sid, home, did)
    api.setup.register_team_for_season(sid, away, did)
    venue = api.setup.create_venue("Arena")
    api.setup.grant_season_venue_access(sid, venue.id)
    rink = api.setup.create_rink(venue.id, "Rink 1")
    return {"pid": pid, "sid": sid, "lid": lid, "did": did, "club": club,
            "home": home, "away": away, "venue": venue.id, "rink": rink.id}


def _future_slot(api, rink_id, hour=18):
    slot = api.setup.create_ice_slot(
        rink_id, datetime(2027, 1, 1, hour, tzinfo=UTC),
        datetime(2027, 1, 1, hour + 1, tzinfo=UTC), IceSlotType.GAME)
    return slot.id


class ServiceParityTest(unittest.TestCase):
    def _backends(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            SqlStore(url).clear_all_data()
            yield "postgres", SqlStore(url)

    def test_create_onto_missing_parent_rejected(self):
        for label, store in self._backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                ctx = _seed_arena(api)
                with self.assertRaises(NotFoundError, msg=label):
                    api.setup.create_rink("venue_ghost", "R")
                with self.assertRaises(NotFoundError, msg=label):
                    api.setup.create_ice_slot(
                        "rink_ghost", datetime(2027, 1, 1, 9, tzinfo=UTC),
                        datetime(2027, 1, 1, 10, tzinfo=UTC), IceSlotType.GAME)
                with self.assertRaises(NotFoundError, msg=label):
                    api.setup.create_game(ctx["sid"], ctx["did"], ctx["home"],
                                          ctx["away"], "slot_ghost")
                with self.assertRaises(NotFoundError, msg=label):
                    api.setup.grant_season_venue_access(ctx["sid"], "venue_ghost")
                if isinstance(store, SqlStore):
                    store.close()

    def test_double_book_rejected(self):
        for label, store in self._backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                ctx = _seed_arena(api)
                slot = _future_slot(api, ctx["rink"])
                api.setup.create_game(ctx["sid"], ctx["did"], ctx["home"],
                                      ctx["away"], slot)
                with self.assertRaises(ScheduleConflictError, msg=label):
                    api.setup.create_game(ctx["sid"], ctx["did"], ctx["home"],
                                          ctx["away"], slot)
                if isinstance(store, SqlStore):
                    store.close()

    def _assert_block_group(self, cm, entity_type, entity_id, group_type,
                            count, label):
        d = cm.exception.details
        self.assertEqual(d.get("entity_type"), entity_type, label)
        self.assertEqual(d.get("entity_id"), entity_id, label)
        groups = {g["type"]: g for g in d.get("dependencies", [])}
        self.assertIn(group_type, groups, label)
        self.assertEqual(groups[group_type]["count"], count, label)
        self.assertEqual(len(groups[group_type]["items"]), count, label)

    def test_delete_blocked_by_dependents(self):
        # Sequential Memory/SQLite/PostgreSQL parity — the same itemised
        # dependency contract the child-wins race loser must reproduce.
        for label, store in self._backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                ctx = _seed_arena(api)
                slot = _future_slot(api, ctx["rink"])
                api.setup.create_game(ctx["sid"], ctx["did"], ctx["home"],
                                      ctx["away"], slot)
                # venue has a rink + access; rink has a slot; slot has a game.
                with self.assertRaises(HasDependenciesError, msg=label) as cm:
                    api.setup.delete_venue(ctx["venue"])
                self._assert_block_group(cm, "venue", ctx["venue"], "rink", 1,
                                         label)
                with self.assertRaises(HasDependenciesError, msg=label) as cm:
                    api.setup.delete_rink(ctx["rink"])
                self._assert_block_group(cm, "rink", ctx["rink"], "ice slot", 1,
                                         label)
                with self.assertRaises(HasDependenciesError, msg=label) as cm:
                    api.setup.delete_ice_slot(slot)
                self._assert_block_group(cm, "ice slot", slot, "game", 1, label)
                self.assertIsNotNone(store.get_venue(ctx["venue"]), label)
                if isinstance(store, SqlStore):
                    store.close()


# =====================================================================
# PostgreSQL forced barrier races (real row locks + the DB foreign key)
# =====================================================================
def _audit_actions(store, action):
    return sum(1 for a in store.all_setup_audit() if a.action == action)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class IceSlotRaceTest(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    # -- runners -----------------------------------------------------------
    def _record(self, results, key, fn):
        try:
            fn(); results[key] = "ok"
        except HasDependenciesError as exc:
            results[key] = "blocked"
            # Capture the itemised payload so the child-wins races can assert the
            # race loser carries the SAME dependency groups/counts/ids as the
            # non-race pre-check (reviewer requirement).
            results[key + "_details"] = exc.details
        except NotFoundError:
            results[key] = "not_found"
        except DomainError as exc:
            results[key] = exc.details.get("reason") or exc.code
        except Exception as exc:  # a raw driver error would land here → fail
            results[key] = f"ERR:{exc}"

    def _reference_block_details(self, delete_call):
        """The itemised HasDependenciesError.details the NON-race pre-check
        produces for the same delete against a store where the dependent already
        exists — the ground truth the child-wins race loser must match exactly."""
        try:
            delete_call()
        except HasDependenciesError as exc:
            return exc.details
        raise AssertionError("expected the reference delete to be blocked")

    def _forced_delete_wins(self, victim_store, locator_method, victim_fn,
                            racer_fn):
        """Pause the child (create) at its parent-locator read, run the delete to
        completion on another connection, then resume so the child's write races
        the committed delete and must lose at the foreign key."""
        orig = getattr(victim_store, locator_method)
        paused = threading.Event()
        resume = threading.Event()
        calls = [0]

        def instrumented(*a, **k):
            result = orig(*a, **k)
            calls[0] += 1
            if calls[0] == 1:
                paused.set()
                resume.wait(15)
            return result

        setattr(victim_store, locator_method, instrumented)
        results = {}

        def run_victim():
            self._record(results, "child", victim_fn)

        def run_racer():
            if not paused.wait(15):
                results["delete"] = "ERR:victim-never-paused"
                return
            try:
                self._record(results, "delete", racer_fn)
            finally:
                resume.set()

        tv = threading.Thread(target=run_victim)
        trc = threading.Thread(target=run_racer)
        tv.start(); trc.start()
        trc.join(25); resume.set(); tv.join(25)
        return results

    def _barrier(self, child_fn, delete_fn):
        barrier = threading.Barrier(2)
        results = {}

        def run(key, fn):
            barrier.wait()
            self._record(results, key, fn)

        tc = threading.Thread(target=run, args=("child", child_fn))
        td = threading.Thread(target=run, args=("delete", delete_fn))
        tc.start(); td.start(); tc.join(20); td.join(20)
        return results

    def _forced_child_wins(self, child_store, write_method, child_fn,
                           delete_store, delete_fn):
        """Pause the child AFTER its foreign-key write (transaction open, holding
        the FK key-share lock on the parent row), start the delete and CONFIRM its
        exact backend is waiting on a heavyweight lock, then release the child.
        The delete then observes the committed child and must lose with the
        has-dependencies block — never a cascade, orphan, deadlock, or audit."""
        orig = getattr(child_store, write_method)
        written = threading.Event()
        release = threading.Event()
        calls = [0]

        def instrumented(*a, **k):
            result = orig(*a, **k)   # the INSERT takes the FK key-share lock
            calls[0] += 1
            if calls[0] == 1:
                written.set()
                release.wait(15)
            return result

        setattr(child_store, write_method, instrumented)
        results = {}

        tc = threading.Thread(target=lambda: self._record(results, "child",
                                                          child_fn))
        td = threading.Thread(target=lambda: self._record(results, "delete",
                                                          delete_fn))
        delete_pid = self._backend_pid(delete_store)
        tc.start()
        if not written.wait(15):
            release.set(); tc.join(15)
            self.fail("child never reached its foreign-key write")
        td.start()
        self.assertTrue(self._wait_until_blocked_on_lock(delete_pid),
                        "delete backend never registered as waiting on a row lock")
        self.assertIsNone(results.get("delete"),
                          "delete completed without blocking on the child's lock")
        release.set()
        tc.join(25); td.join(25)
        return results

    def _forced_double_book(self, first_store, first_fn, second_store, second_fn):
        """Two creates target the same slot. Pause the first AFTER its game insert
        (holding the ux_games_active_ice_slot index entry, transaction open), start
        the second and CONFIRM its exact backend is blocked on the index, then
        release the first to commit. The second then loses with ice_slot_taken —
        never a raw error, and never a second active game on the slot."""
        orig = first_store.add_game
        written = threading.Event()
        release = threading.Event()
        calls = [0]

        def instrumented(*a, **k):
            result = orig(*a, **k)
            calls[0] += 1
            if calls[0] == 1:
                written.set()
                release.wait(15)
            return result

        first_store.add_game = instrumented
        results = {}
        tf = threading.Thread(target=lambda: self._record(results, "first",
                                                          first_fn))
        ts = threading.Thread(target=lambda: self._record(results, "second",
                                                          second_fn))
        second_pid = self._backend_pid(second_store)
        tf.start()
        if not written.wait(15):
            release.set(); tf.join(15)
            self.fail("first create never reached its game insert")
        ts.start()
        self.assertTrue(self._wait_until_blocked_on_lock(second_pid),
                        "second create never blocked on the ice-slot unique index")
        self.assertIsNone(results.get("second"),
                          "second create completed without blocking on the index")
        release.set()
        tf.join(25); ts.join(25)
        return results

    def _backend_pid(self, store):
        with store.conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid() AS pid")
            return cur.fetchone()["pid"]

    def _wait_until_blocked_on_lock(self, backend_pid, timeout=10.0):
        import psycopg
        deadline = time.monotonic() + timeout
        with psycopg.connect(self.url, autocommit=True) as mon:
            while time.monotonic() < deadline:
                with mon.cursor() as cur:
                    cur.execute(
                        "SELECT state, wait_event_type FROM pg_stat_activity "
                        "WHERE pid = %s", (backend_pid,))
                    row = cur.fetchone()
                    if (row is not None and row[0] == "active"
                            and row[1] == "Lock"):
                        return True
                time.sleep(0.02)
        return False

    # -- create_rink vs delete_venue --------------------------------------
    def test_create_rink_vs_delete_venue_forced(self):
        api0 = ApiService(SqlStore(self.url))
        venue = api0.setup.create_venue("Arena").id
        victim_store = SqlStore(self.url)
        api_v = ApiService(victim_store)
        api_r = ApiService(SqlStore(self.url))
        results = self._forced_delete_wins(
            victim_store, "get_venue",
            victim_fn=lambda: api_v.setup.create_rink(venue, "New", actor_id="c"),
            racer_fn=lambda: api_r.setup.delete_venue(venue, actor_id="d"))
        self.assertEqual(results, {"child": "venue_not_found", "delete": "ok"},
                         results)
        check = SqlStore(self.url)
        self._no_orphan_rinks(check)
        self.assertEqual(check.all_rinks(), [], results)
        self.assertIsNone(check.get_venue(venue), results)
        self.assertEqual(_audit_actions(check, "rink_created"), 0, results)

    def test_create_rink_vs_delete_venue_child_wins(self):
        api0 = ApiService(SqlStore(self.url))
        venue = api0.setup.create_venue("Arena").id
        child_store = SqlStore(self.url)
        api_c = ApiService(child_store)
        delete_store = SqlStore(self.url)
        api_d = ApiService(delete_store)
        results = self._forced_child_wins(
            child_store, "add_rink",
            child_fn=lambda: api_c.setup.create_rink(venue, "New", actor_id="c"),
            delete_store=delete_store,
            delete_fn=lambda: api_d.setup.delete_venue(venue, actor_id="d"))
        self.assertEqual(results["child"], "ok", results)
        self.assertEqual(results["delete"], "blocked", results)
        check = SqlStore(self.url)
        self._no_orphan_rinks(check)
        self.assertEqual(len([r for r in check.all_rinks()
                              if r.venue_id == venue]), 1, results)
        self.assertIsNotNone(check.get_venue(venue), results)
        self.assertEqual(_audit_actions(check, "venue_deleted"), 0, results)
        # The race loser carries the SAME itemised dependency payload the non-race
        # pre-check produces against the now-committed rink — identical groups,
        # counts, and ids (reviewer requirement), not a thin error.
        reference = self._reference_block_details(
            lambda: ApiService(SqlStore(self.url)).setup.delete_venue(venue))
        self.assertEqual(results["delete_details"], reference, results)
        self._assert_itemised(results["delete_details"], "venue", venue,
                              "rink", 1)

    def test_create_rink_vs_delete_venue_barrier(self):
        api0 = ApiService(SqlStore(self.url))
        venue = api0.setup.create_venue("Arena").id
        api_c = ApiService(SqlStore(self.url))
        api_d = ApiService(SqlStore(self.url))
        results = self._barrier(
            child_fn=lambda: api_c.setup.create_rink(venue, "New", actor_id="c"),
            delete_fn=lambda: api_d.setup.delete_venue(venue, actor_id="d"))
        check = SqlStore(self.url)
        self._no_orphan_rinks(check)
        if results["child"] == "ok":
            self.assertEqual(results["delete"], "blocked", results)
            self.assertIsNotNone(check.get_venue(venue), results)
        else:
            self.assertEqual(results, {"child": "venue_not_found", "delete": "ok"},
                             results)
            self.assertIsNone(check.get_venue(venue), results)

    # -- create_ice_slot vs delete_rink -----------------------------------
    def _seed_venue_rink(self, api0):
        venue = api0.setup.create_venue("Arena").id
        rink = api0.setup.create_rink(venue, "Rink 1").id
        return venue, rink

    def test_create_ice_slot_vs_delete_rink_forced(self):
        api0 = ApiService(SqlStore(self.url))
        _venue, rink = self._seed_venue_rink(api0)
        victim_store = SqlStore(self.url)
        api_v = ApiService(victim_store)
        api_r = ApiService(SqlStore(self.url))
        results = self._forced_delete_wins(
            victim_store, "get_rink",
            victim_fn=lambda: api_v.setup.create_ice_slot(
                rink, datetime(2027, 1, 1, 18, tzinfo=UTC),
                datetime(2027, 1, 1, 19, tzinfo=UTC), IceSlotType.GAME,
                actor_id="c"),
            racer_fn=lambda: api_r.setup.delete_rink(rink, actor_id="d"))
        self.assertEqual(results, {"child": "rink_not_found", "delete": "ok"},
                         results)
        check = SqlStore(self.url)
        self._no_orphan_slots(check)
        self.assertEqual(check.all_ice_slots(), [], results)
        self.assertIsNone(check.get_rink(rink), results)
        self.assertEqual(_audit_actions(check, "ice_slot_created"), 0, results)

    def test_create_ice_slot_vs_delete_rink_child_wins(self):
        api0 = ApiService(SqlStore(self.url))
        _venue, rink = self._seed_venue_rink(api0)
        child_store = SqlStore(self.url)
        api_c = ApiService(child_store)
        delete_store = SqlStore(self.url)
        api_d = ApiService(delete_store)
        results = self._forced_child_wins(
            child_store, "add_ice_slot",
            child_fn=lambda: api_c.setup.create_ice_slot(
                rink, datetime(2027, 1, 1, 18, tzinfo=UTC),
                datetime(2027, 1, 1, 19, tzinfo=UTC), IceSlotType.GAME,
                actor_id="c"),
            delete_store=delete_store,
            delete_fn=lambda: api_d.setup.delete_rink(rink, actor_id="d"))
        self.assertEqual(results["child"], "ok", results)
        self.assertEqual(results["delete"], "blocked", results)
        check = SqlStore(self.url)
        self._no_orphan_slots(check)
        self.assertEqual(len([s for s in check.all_ice_slots()
                              if s.rink_id == rink]), 1, results)
        self.assertIsNotNone(check.get_rink(rink), results)
        self.assertEqual(_audit_actions(check, "rink_deleted"), 0, results)
        reference = self._reference_block_details(
            lambda: ApiService(SqlStore(self.url)).setup.delete_rink(rink))
        self.assertEqual(results["delete_details"], reference, results)
        self._assert_itemised(results["delete_details"], "rink", rink,
                              "ice slot", 1)

    def test_create_ice_slot_vs_delete_rink_barrier(self):
        api0 = ApiService(SqlStore(self.url))
        _venue, rink = self._seed_venue_rink(api0)
        api_c = ApiService(SqlStore(self.url))
        api_d = ApiService(SqlStore(self.url))
        results = self._barrier(
            child_fn=lambda: api_c.setup.create_ice_slot(
                rink, datetime(2027, 1, 1, 18, tzinfo=UTC),
                datetime(2027, 1, 1, 19, tzinfo=UTC), IceSlotType.GAME,
                actor_id="c"),
            delete_fn=lambda: api_d.setup.delete_rink(rink, actor_id="d"))
        check = SqlStore(self.url)
        self._no_orphan_slots(check)
        if results["child"] == "ok":
            self.assertEqual(results["delete"], "blocked", results)
            self.assertIsNotNone(check.get_rink(rink), results)
        else:
            self.assertEqual(results, {"child": "rink_not_found", "delete": "ok"},
                             results)
            self.assertIsNone(check.get_rink(rink), results)

    # -- create_game vs delete_ice_slot -----------------------------------
    def test_create_game_vs_delete_ice_slot_forced(self):
        api0 = ApiService(SqlStore(self.url))
        ctx = _seed_arena(api0)
        slot = _future_slot(api0, ctx["rink"])
        victim_store = SqlStore(self.url)
        api_v = ApiService(victim_store)
        api_r = ApiService(SqlStore(self.url))
        results = self._forced_delete_wins(
            victim_store, "get_ice_slot",
            victim_fn=lambda: api_v.setup.create_game(
                ctx["sid"], ctx["did"], ctx["home"], ctx["away"], slot,
                actor_id="c"),
            racer_fn=lambda: api_r.setup.delete_ice_slot(slot, actor_id="d"))
        # The delete wins; the child loses with a stable, non-orphaning domain
        # error. create_game re-reads the slot after its first (paused) read, so
        # the SERVICE catches the deletion with a clean NotFoundError; the
        # games.ice_slot_id → ice_slots foreign key is the backstop for the
        # residual read-to-insert window (proven directly by the store-boundary
        # and child-wins tests). Either stable loss is acceptable — never a raw
        # error or an orphan.
        self.assertEqual(results["delete"], "ok", results)
        self.assertIn(results["child"], ("not_found", "ice_slot_not_found"),
                      results)
        check = SqlStore(self.url)
        self._no_orphan_games(check)
        self.assertEqual(check.all_games(), [], results)
        self.assertIsNone(check.get_ice_slot(slot), results)
        self.assertEqual(_audit_actions(check, "game_created"), 0, results)

    def test_create_game_vs_delete_ice_slot_child_wins(self):
        api0 = ApiService(SqlStore(self.url))
        ctx = _seed_arena(api0)
        slot = _future_slot(api0, ctx["rink"])
        child_store = SqlStore(self.url)
        api_c = ApiService(child_store)
        delete_store = SqlStore(self.url)
        api_d = ApiService(delete_store)
        results = self._forced_child_wins(
            child_store, "add_game",
            child_fn=lambda: api_c.setup.create_game(
                ctx["sid"], ctx["did"], ctx["home"], ctx["away"], slot,
                actor_id="c"),
            delete_store=delete_store,
            delete_fn=lambda: api_d.setup.delete_ice_slot(slot, actor_id="d"))
        self.assertEqual(results["child"], "ok", results)
        self.assertEqual(results["delete"], "blocked", results)
        check = SqlStore(self.url)
        self._no_orphan_games(check)
        self.assertEqual(len([g for g in check.all_games()
                              if g.ice_slot_id == slot]), 1, results)
        self.assertIsNotNone(check.get_ice_slot(slot), results)
        self.assertEqual(_audit_actions(check, "ice_slot_deleted"), 0, results)
        reference = self._reference_block_details(
            lambda: ApiService(SqlStore(self.url)).setup.delete_ice_slot(slot))
        self.assertEqual(results["delete_details"], reference, results)
        self._assert_itemised(results["delete_details"], "ice slot", slot,
                              "game", 1)

    def test_create_game_vs_delete_ice_slot_barrier(self):
        api0 = ApiService(SqlStore(self.url))
        ctx = _seed_arena(api0)
        slot = _future_slot(api0, ctx["rink"])
        api_c = ApiService(SqlStore(self.url))
        api_d = ApiService(SqlStore(self.url))
        results = self._barrier(
            child_fn=lambda: api_c.setup.create_game(
                ctx["sid"], ctx["did"], ctx["home"], ctx["away"], slot,
                actor_id="c"),
            delete_fn=lambda: api_d.setup.delete_ice_slot(slot, actor_id="d"))
        check = SqlStore(self.url)
        self._no_orphan_games(check)
        if results["child"] == "ok":
            self.assertEqual(results["delete"], "blocked", results)
            self.assertIsNotNone(check.get_ice_slot(slot), results)
        else:
            self.assertEqual(results["delete"], "ok", results)
            self.assertIn(results["child"], ("not_found", "ice_slot_not_found"),
                          results)
            self.assertIsNone(check.get_ice_slot(slot), results)

    # -- grant_season_venue_access vs delete_venue ------------------------
    def _seed_season_venue(self, api0):
        pid = api0.create_program("Prog", "US", "UTC")["id"]
        sid = api0.create_season(pid, "S1")["id"]
        venue = api0.setup.create_venue("Arena").id
        return sid, venue

    def test_grant_access_vs_delete_venue_forced(self):
        api0 = ApiService(SqlStore(self.url))
        sid, venue = self._seed_season_venue(api0)
        victim_store = SqlStore(self.url)
        api_v = ApiService(victim_store)
        api_r = ApiService(SqlStore(self.url))
        results = self._forced_delete_wins(
            victim_store, "get_venue",
            victim_fn=lambda: api_v.setup.grant_season_venue_access(
                sid, venue, actor_id="c"),
            racer_fn=lambda: api_r.setup.delete_venue(venue, actor_id="d"))
        self.assertEqual(results, {"child": "venue_not_found", "delete": "ok"},
                         results)
        check = SqlStore(self.url)
        self._no_orphan_access(check)
        self.assertEqual(check.all_season_venue_access(), [], results)
        self.assertIsNone(check.get_venue(venue), results)
        self.assertEqual(_audit_actions(check, "season_venue_access_granted"), 0,
                         results)

    def test_grant_access_vs_delete_venue_child_wins(self):
        api0 = ApiService(SqlStore(self.url))
        sid, venue = self._seed_season_venue(api0)
        child_store = SqlStore(self.url)
        api_c = ApiService(child_store)
        delete_store = SqlStore(self.url)
        api_d = ApiService(delete_store)
        results = self._forced_child_wins(
            child_store, "add_season_venue_access",
            child_fn=lambda: api_c.setup.grant_season_venue_access(
                sid, venue, actor_id="c"),
            delete_store=delete_store,
            delete_fn=lambda: api_d.setup.delete_venue(venue, actor_id="d"))
        self.assertEqual(results["child"], "ok", results)
        self.assertEqual(results["delete"], "blocked", results)
        check = SqlStore(self.url)
        self._no_orphan_access(check)
        self.assertEqual(len([a for a in check.all_season_venue_access()
                              if a.venue_id == venue]), 1, results)
        self.assertIsNotNone(check.get_venue(venue), results)
        self.assertEqual(_audit_actions(check, "venue_deleted"), 0, results)
        reference = self._reference_block_details(
            lambda: ApiService(SqlStore(self.url)).setup.delete_venue(venue))
        self.assertEqual(results["delete_details"], reference, results)
        self._assert_itemised(results["delete_details"], "venue", venue,
                              "venue access", 1)

    def test_grant_access_vs_delete_venue_barrier(self):
        api0 = ApiService(SqlStore(self.url))
        sid, venue = self._seed_season_venue(api0)
        api_c = ApiService(SqlStore(self.url))
        api_d = ApiService(SqlStore(self.url))
        results = self._barrier(
            child_fn=lambda: api_c.setup.grant_season_venue_access(
                sid, venue, actor_id="c"),
            delete_fn=lambda: api_d.setup.delete_venue(venue, actor_id="d"))
        check = SqlStore(self.url)
        self._no_orphan_access(check)
        if results["child"] == "ok":
            self.assertEqual(results["delete"], "blocked", results)
            self.assertIsNotNone(check.get_venue(venue), results)
        else:
            self.assertEqual(results, {"child": "venue_not_found", "delete": "ok"},
                             results)
            self.assertIsNone(check.get_venue(venue), results)

    # -- cross-Season create_game vs create_game (double-book) ------------
    def _seed_two_season_slot(self, api0):
        """Two Seasons under one Program, each with a Division + two registered
        Teams, sharing one Venue/Rink/GAME slot. Returns the two (sid, did, home,
        away) tuples and the shared slot id."""
        pid = api0.create_program("Prog", "US", "UTC")["id"]
        venue = api0.setup.create_venue("Arena").id
        rink = api0.setup.create_rink(venue, "Rink 1").id
        seasons = []
        for n in (1, 2):
            sid = api0.create_season(pid, f"S{n}")["id"]
            lid = api0.create_league(sid, "Gold")["id"]
            did = api0.create_division(sid, "D1", league_id=lid)["id"]
            club = api0.create_club(f"Club{n}")["id"]
            home = api0.create_team(club_id=club, name=f"Alpha{n}", league_id=lid,
                                    division_id=did)["id"]
            away = api0.create_team(club_id=club, name=f"Beta{n}", league_id=lid,
                                    division_id=did)["id"]
            api0.setup.register_team_for_season(sid, home, did)
            api0.setup.register_team_for_season(sid, away, did)
            api0.setup.grant_season_venue_access(sid, venue)
            seasons.append((sid, did, home, away))
        slot = _future_slot(api0, rink)
        return seasons, slot

    def test_cross_season_double_book_forced(self):
        api0 = ApiService(SqlStore(self.url))
        (s1, s2), slot = self._seed_two_season_slot(api0)
        first_store = SqlStore(self.url)
        api_1 = ApiService(first_store)
        second_store = SqlStore(self.url)
        api_2 = ApiService(second_store)
        results = self._forced_double_book(
            first_store,
            first_fn=lambda: api_1.setup.create_game(
                s1[0], s1[1], s1[2], s1[3], slot, actor_id="g1"),
            second_store=second_store,
            second_fn=lambda: api_2.setup.create_game(
                s2[0], s2[1], s2[2], s2[3], slot, actor_id="g2"))
        self.assertEqual(results, {"first": "ok", "second": "ice_slot_taken"},
                         results)
        check = SqlStore(self.url)
        active = [g for g in check.all_games()
                  if g.ice_slot_id == slot and not g.cancelled]
        self.assertEqual(len(active), 1, results)   # never double-booked
        self.assertEqual(_audit_actions(check, "game_created"), 1, results)

    def test_cross_season_double_book_barrier(self):
        api0 = ApiService(SqlStore(self.url))
        (s1, s2), slot = self._seed_two_season_slot(api0)
        api_1 = ApiService(SqlStore(self.url))
        api_2 = ApiService(SqlStore(self.url))
        barrier = threading.Barrier(2)
        results = {}

        def run(key, api, s):
            barrier.wait()
            self._record(results, key,
                         lambda: api.setup.create_game(s[0], s[1], s[2], s[3],
                                                       slot, actor_id=key))

        t1 = threading.Thread(target=run, args=("first", api_1, s1))
        t2 = threading.Thread(target=run, args=("second", api_2, s2))
        t1.start(); t2.start(); t1.join(20); t2.join(20)
        check = SqlStore(self.url)
        active = [g for g in check.all_games()
                  if g.ice_slot_id == slot and not g.cancelled]
        self.assertEqual(len(active), 1, results)   # exactly one wins the slot
        self.assertEqual(sorted(results.values()), ["ice_slot_taken", "ok"],
                         results)
        self.assertEqual(_audit_actions(check, "game_created"), 1, results)

    # -- itemised has-dependencies payload assertion -----------------------
    def _assert_itemised(self, details, entity_type, entity_id, group_type,
                         count):
        """The race-loser block carries the full itemised dependency contract:
        the deleted entity, and a dependency group of ``group_type`` with the
        expected count and per-row {id, name} items (secret-free)."""
        self.assertEqual(details.get("entity_type"), entity_type, details)
        self.assertEqual(details.get("entity_id"), entity_id, details)
        groups = {g["type"]: g for g in details.get("dependencies", [])}
        self.assertIn(group_type, groups, details)
        g = groups[group_type]
        self.assertEqual(g["count"], count, details)
        self.assertEqual(len(g["items"]), count, details)
        for item in g["items"]:
            self.assertIn("id", item, details)
            self.assertIn("name", item, details)

    # -- invariants --------------------------------------------------------
    def _no_orphan_rinks(self, store):
        orphans = [r.id for r in store.all_rinks()
                   if r.venue_id is not None and store.get_venue(r.venue_id) is None]
        self.assertEqual(orphans, [], "rink stranded on a deleted venue")

    def _no_orphan_slots(self, store):
        orphans = [s.id for s in store.all_ice_slots()
                   if s.rink_id is not None and store.get_rink(s.rink_id) is None]
        self.assertEqual(orphans, [], "ice slot stranded on a deleted rink")

    def _no_orphan_games(self, store):
        orphans = [g.id for g in store.all_games()
                   if g.ice_slot_id is not None
                   and store.get_ice_slot(g.ice_slot_id) is None]
        self.assertEqual(orphans, [], "game stranded on a deleted ice slot")

    def _no_orphan_access(self, store):
        orphans = [a.id for a in store.all_season_venue_access()
                   if a.venue_id is not None and store.get_venue(a.venue_id) is None]
        self.assertEqual(orphans, [], "venue access stranded on a deleted venue")


if __name__ == "__main__":
    unittest.main()
