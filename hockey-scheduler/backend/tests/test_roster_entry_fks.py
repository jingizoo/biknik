"""Referential integrity: a roster entry references a real game + player
(#201 Slice 3F).

Migration 027 adds game_roster_entries.game_id → games(id) and
.player_id → players(id) foreign keys. Continues #201's table-by-table
relationship work after the result table (3D/3E). Both columns stay nullable — a
nullable FK still permits NULL — so only a concrete, dangling reference is
rejected; requiring the columns (NOT NULL) is a later slice.

Covers pre-migration orphan detection/report + upgrade abort, both foreign keys
enforced as translated conflicts on SQLite + PostgreSQL, NULL still allowed, the
SQLite rebuild preserving rows + indexes, and enforcement surviving a restart.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import (
    GameRosterEntry,
    RosterEntryStatus,
    RosterRole,
    SelectionSource,
)
from hockey_scheduler.domain.errors import IntegrityConflictError
from hockey_scheduler.store import SqlStore
from hockey_scheduler.store.integrity_checks import (
    MigrationDataError,
    assert_roster_refs_exist,
    find_orphan_roster_refs,
)
from hockey_scheduler.store.sql_store import migrate

UTC = timezone.utc
_VERSION = "027_roster_entry_fks"

# Migrations AFTER 027 that ALTER game_roster_entries, and the columns they
# add. ``_downgrade_027`` must take these down WITH 027 (see its docstring):
# 027's SQLite half rebuilds the table from a column list frozen at 027, so
# replaying it on top of a later ALTER drops that ALTER's columns for good.
# Keep the two tuples in step, and extend both the next time this table
# gains a column.
_POST_027_MIGRATIONS = ("061_roster_entry_durable_attribution",)
_POST_027_COLUMNS = ("team_side", "seated_position")


def _entry(eid, game="g1", player="p1"):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return GameRosterEntry(
        id=eid, game_id=game, player_id=player, roster_role=RosterRole.SELECTED,
        selection_source=SelectionSource.COACH_SELECTED,
        status=RosterEntryStatus.SELECTED, selected_at=now, updated_at=now,
        selected_by="actor")


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


def _add_game(store, gid):
    store.conn.cursor().execute(
        store.dialect.sql("INSERT INTO games (id) VALUES (?)"), (gid,))


def _add_player(store, pid):
    store.conn.cursor().execute(
        store.dialect.sql("INSERT INTO players (id) VALUES (?)"), (pid,))


def _add_roster_raw(store, eid, game_id, player_id):
    """Insert a roster row bypassing the FK, to plant a pre-027 dangling row."""
    now = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    store.conn.cursor().execute(store.dialect.sql(
        "INSERT INTO game_roster_entries (id, game_id, player_id, roster_role, "
        "selection_source, status, selected_at, updated_at, selected_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"),
        (eid, game_id, player_id, "selected", "coach_selected", "selected",
         now, now, "actor"))


def _downgrade_027(store):
    """Simulate a pre-027 database: drop both foreign keys and un-record it.

    Postgres drops the named constraints; SQLite rebuilds the table without them
    (recreating the Slice 3B indexes). 027 changed nothing but the FKs, so this is
    a single-step downgrade — dangling rows can then be planted before it re-runs.

    IT IS NOT A DOWNGRADE OF 027 ALONE, and cannot be. 027's SQLite half
    rebuilds ``game_roster_entries`` from a hand-written CREATE carrying the
    column list AS OF 027 — correct on a real upgrade path, where 027 runs
    before anything that adds a column, and never replayed in production
    because ``schema_migrations`` is authoritative. Replaying it here, on
    top of a database that has since had columns ADDED, silently drops
    them; and because the later migration is still recorded it never
    re-runs to put them back, so the next ``_insert`` fails with "no column
    named …".

    So every later migration that ALTERs this same table is downgraded
    alongside 027 (columns dropped, version un-recorded) and the subsequent
    ``migrate()`` replays the whole chain in FORWARD ORDER — 027's rebuild,
    then their ALTERs — which is the only order any of them is correct in.
    ``_POST_027_COLUMNS`` is that list; #205 blocker 5's migration 061
    (``team_side``/``seated_position``) is the first entry, and the next
    ALTER on this table belongs there too.
    """
    with store.transaction():
        cur = store.conn.cursor()
        if store.backend == "postgres":
            cur.execute("ALTER TABLE game_roster_entries "
                        "DROP CONSTRAINT IF EXISTS fk_roster_game")
            cur.execute("ALTER TABLE game_roster_entries "
                        "DROP CONSTRAINT IF EXISTS fk_roster_player")
            for col in _POST_027_COLUMNS:
                cur.execute("ALTER TABLE game_roster_entries "
                            f"DROP COLUMN IF EXISTS {col}")
        else:
            cur.execute("PRAGMA defer_foreign_keys = ON")
            cur.execute(
                "CREATE TABLE game_roster_entries_old (id TEXT PRIMARY KEY, "
                "game_id TEXT, player_id TEXT, roster_role TEXT, "
                "selection_source TEXT, status TEXT, selected_at TEXT, "
                "updated_at TEXT, selected_by TEXT)")
            cur.execute(
                "INSERT INTO game_roster_entries_old SELECT id, game_id, "
                "player_id, roster_role, selection_source, status, selected_at, "
                "updated_at, selected_by FROM game_roster_entries")
            cur.execute("DROP TABLE game_roster_entries")
            cur.execute("ALTER TABLE game_roster_entries_old "
                        "RENAME TO game_roster_entries")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_roster_game "
                        "ON game_roster_entries (game_id, player_id)")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_roster_game_player "
                        "ON game_roster_entries (game_id, player_id) "
                        "WHERE game_id IS NOT NULL AND player_id IS NOT NULL")
        cur.execute(store.dialect.sql(
            "DELETE FROM schema_migrations WHERE version = ?"), (_VERSION,))
        # ...and every later migration that ALTERs this same table, so the
        # subsequent migrate() replays the whole chain in forward order.
        for follower in _POST_027_MIGRATIONS:
            cur.execute(store.dialect.sql(
                "DELETE FROM schema_migrations WHERE version = ?"),
                (follower,))


def _fk_names(store):
    """Declared foreign keys on game_roster_entries: set of referenced tables."""
    cur = store.conn.cursor()
    if store.backend == "sqlite":
        cur.execute("PRAGMA foreign_key_list('game_roster_entries')")
        return {row["table"] for row in cur.fetchall()}
    cur.execute(
        "SELECT ccu.table_name AS ref FROM information_schema.table_constraints tc "
        "JOIN information_schema.constraint_column_usage ccu "
        "  ON ccu.constraint_name = tc.constraint_name "
        "WHERE tc.table_name = 'game_roster_entries' "
        "  AND tc.constraint_type = 'FOREIGN KEY'")
    return {row["ref"] for row in cur.fetchall()}


class PreMigrationValidationTest(unittest.TestCase):
    def _pre027(self, url):
        store = _fresh(url)
        _downgrade_027(store)
        return store

    def _cleanup(self, store):
        if store.backend != "sqlite":
            store.reset_schema()  # leave the shared PostgreSQL DB pristine
        store.close()

    def test_orphan_references_detected_and_migrate_aborts(self):
        for label, url in _sql_backends():
            store = self._pre027(url)
            try:
                _add_game(store, "g1")
                _add_player(store, "p1")
                with store.transaction():
                    _add_roster_raw(store, "e_ok", "g1", "p1")      # valid
                    _add_roster_raw(store, "e_badg", "ghost", "p1")  # missing game
                    _add_roster_raw(store, "e_badp", "g1", "ghost")  # missing player
                    _add_roster_raw(store, "e_null", None, "p1")     # NULL — allowed
                self.assertEqual(find_orphan_roster_refs(store.conn),
                                 ["e_badg", "e_badp"], label)
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    assert_roster_refs_exist(store.conn)
                self.assertIn("e_badg", str(ctx.exception))
                self.assertIn("e_badp", str(ctx.exception))
                with self.assertRaises(MigrationDataError, msg=label):
                    migrate(store.conn, store.dialect)  # aborts on dangling data
            finally:
                self._cleanup(store)

    def test_valid_and_null_references_upgrade_cleanly(self):
        for label, url in _sql_backends():
            store = self._pre027(url)
            try:
                _add_game(store, "g1")
                _add_player(store, "p1")
                with store.transaction():
                    _add_roster_raw(store, "e1", "g1", "p1")   # valid
                    _add_roster_raw(store, "e2", None, "p1")   # NULL game — allowed
                    _add_roster_raw(store, "e3", "g1", None)   # NULL player — allowed
                self.assertEqual(find_orphan_roster_refs(store.conn), [], label)
                assert_roster_refs_exist(store.conn)  # must not raise
                migrate(store.conn, store.dialect)    # succeeds; FKs added
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
                self.assertEqual(_fk_names(store), {"games", "players"}, label)
            finally:
                self._cleanup(store)


class ForeignKeyEnforcementTest(unittest.TestCase):
    def test_missing_game_is_a_translated_conflict(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _add_player(store, "p1")
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_roster_entry(_entry("e1", "ghost", "p1"))
                self.assertEqual(ctx.exception.details["reason"],
                                 "foreign_key_violation", label)
                self.assertIsNone(store.roster_entry_for_player("ghost", "p1"), label)
            finally:
                store.close()

    def test_missing_player_is_a_translated_conflict(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _add_game(store, "g1")
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_roster_entry(_entry("e1", "g1", "ghost"))
                self.assertEqual(ctx.exception.details["reason"],
                                 "foreign_key_violation", label)
            finally:
                store.close()

    def test_valid_references_are_allowed(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _add_game(store, "g1")
                _add_player(store, "p1")
                with store.transaction():
                    store.add_roster_entry(_entry("e1", "g1", "p1"))
                self.assertIsNotNone(store.roster_entry_for_player("g1", "p1"), label)
            finally:
                store.close()

    def test_null_references_are_still_allowed(self):
        # Both FKs are nullable, so a roster row not yet tied to a game/player
        # (e.g. an intentionally-open slot) is fine.
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _add_game(store, "g1")
                _add_player(store, "p1")
                with store.transaction():
                    store.add_roster_entry(_entry("e1", None, "p1"))
                    store.add_roster_entry(_entry("e2", "g1", None))
                cur = store.conn.cursor()
                cur.execute("SELECT COUNT(*) AS n FROM game_roster_entries")
                self.assertEqual(cur.fetchone()["n"], 2, label)
            finally:
                store.close()


class RebuildPreservesDataTest(unittest.TestCase):
    def test_rows_and_indexes_survive_the_migration(self):
        for label, url in _sql_backends():
            store = self._downgraded_with_rows(url)
            try:
                migrate(store.conn, store.dialect)  # apply 027 over real rows
                self.assertEqual(_fk_names(store), {"games", "players"}, label)
                cur = store.conn.cursor()
                cur.execute("SELECT COUNT(*) AS n FROM game_roster_entries")
                self.assertEqual(cur.fetchone()["n"], 2, label)
                # The Slice 3B uniqueness index still enforces one row per pair.
                with self.assertRaises(IntegrityConflictError, msg=label):
                    with store.transaction():
                        store.add_roster_entry(_entry("e3", "g1", "p1"))
            finally:
                if store.backend != "sqlite":
                    store.reset_schema()
                store.close()

    def _downgraded_with_rows(self, url):
        store = _fresh(url)
        _downgrade_027(store)
        _add_game(store, "g1")
        _add_player(store, "p1")
        _add_player(store, "p2")
        with store.transaction():
            _add_roster_raw(store, "e1", "g1", "p1")
            _add_roster_raw(store, "e2", "g1", "p2")
        return store


class MigrationLedgerTest(unittest.TestCase):
    def test_fks_recorded_and_enforced_after_reopen(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            first = SqlStore(path)
            self.assertIn(_VERSION, first.migration_status()["applied"])
            self.assertEqual(_fk_names(first), {"games", "players"})
            _add_game(first, "g1")
            _add_player(first, "p1")
            first.close()
            second = SqlStore(path)
            self.assertTrue(second.migration_status()["current"])
            self.assertEqual(_fk_names(second), {"games", "players"})
            with self.assertRaises(IntegrityConflictError):  # still enforced
                with second.transaction():
                    second.add_roster_entry(_entry("e1", "ghost", "p1"))
            with second.transaction():
                second.add_roster_entry(_entry("e2", "g1", "p1"))  # valid works
            second.close()
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
