"""Referential integrity for reassignment writes (#201 Slice 2).

SCOPE: this slice adds ONLY two foreign keys — players.team_id → teams(id) and
the nullable teams.club_id → clubs(id) — as the DATABASE backstop for the
reviewer-flagged reassignment races handed to #201. It does NOT complete the
wider #201 relationship-integrity scope (IceSlot / cross-Season double-booking /
no-row-lock work lands separately).

Both foreign keys are NAMED (fk_players_team, fk_teams_club) with the default
NO ACTION delete behavior — never ON DELETE CASCADE, never any data cleanup. The
service layer already validates the destination parent up front, and
delete_team/delete_club block on dependents (each row-locking its subject), so:
  * a child reassignment/create that races behind a parent delete loses at the
    foreign key and surfaces the stable team_not_found / club_not_found conflict;
  * a parent delete that races behind a committed child serializes on the subject
    row lock and surfaces the stable has-dependencies block.
Neither direction ever lets a raw psycopg/sqlite exception escape, orphans a row,
or writes the losing operation's audit.

Coverage:
  * pre-migration orphan detection with counts + row ids, fail-closed with ZERO
    DDL/data mutation (SQLite + PostgreSQL);
  * clean upgrade re-adds both named FKs; foreign_key_check is clean; enforcement
    survives restart; exact catalog assertions (foreign_key_list / pg_constraint);
  * store-boundary translation into the stable team_not_found / club_not_found
    conflict, insert and update paths (SQLite + PostgreSQL);
  * service-level rejection of a missing parent and delete-dependency blocks on
    Memory + SQLite + PostgreSQL (sequential parity, both directions);
  * four PostgreSQL race families (add_player vs delete_team, assign_player_team
    vs delete_team, create_team vs delete_club, assign_team_club vs delete_club),
    each proven both by a forced parent-delete-wins ordering and by an
    open barrier asserting exactly one valid linearization.
"""

import os
import tempfile
import threading
import time
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Player, Position, Team
from hockey_scheduler.domain.errors import (
    DomainError,
    HasDependenciesError,
    IntegrityConflictError,
    NotFoundError,
)
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.store.integrity_checks import (
    MigrationDataError,
    assert_reassignment_fks_ready,
    find_orphan_player_teams,
    find_orphan_team_clubs,
)
from hockey_scheduler.store.sql_store import migrate

_VERSION = "040_reassignment_fks"


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


def _add_club_raw(store, cid):
    store.conn.cursor().execute(
        store.dialect.sql("INSERT INTO clubs (id, name) VALUES (?, ?)"),
        (cid, cid))


def _add_team_raw(store, tid, club_id=None):
    store.conn.cursor().execute(
        store.dialect.sql("INSERT INTO teams (id, name, club_id) "
                          "VALUES (?, ?, ?)"), (tid, tid, club_id))


def _add_player_raw(store, pid, team_id=None):
    store.conn.cursor().execute(
        store.dialect.sql("INSERT INTO players (id, team_id, name, position) "
                          "VALUES (?, ?, ?, ?)"), (pid, team_id, pid, "forward"))


def _downgrade_040(store):
    """Reverse migration 040 to the prior schema: drop both FKs and un-record it.

    Postgres drops the named constraints; SQLite rebuilds both tables without the
    FKs (recreating every index), mirroring the create-copy-drop-rename in 040.
    Dangling rows can then be planted before it re-runs — the upgrade/adoption
    path exercised by the tests below.
    """
    with store.transaction():
        cur = store.conn.cursor()
        if store.backend == "postgres":
            cur.execute("ALTER TABLE players "
                        "DROP CONSTRAINT IF EXISTS fk_players_team")
            cur.execute("ALTER TABLE teams "
                        "DROP CONSTRAINT IF EXISTS fk_teams_club")
        else:
            cur.execute("PRAGMA defer_foreign_keys = ON")
            # players first (it references teams), then teams.
            cur.execute(
                "CREATE TABLE players_old (id TEXT PRIMARY KEY, team_id TEXT, "
                "name TEXT, position TEXT, shoots TEXT, jersey_number INTEGER, "
                "is_active INTEGER, guardian_person_id TEXT, external_ref TEXT)")
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
            cur.execute(
                "CREATE TABLE teams_old (id TEXT PRIMARY KEY, name TEXT, "
                "division TEXT, club_id TEXT, division_id TEXT, external_ref TEXT, "
                "program_id TEXT, league_id TEXT)")
            cur.execute(
                "INSERT INTO teams_old SELECT id, name, division, club_id, "
                "division_id, external_ref, program_id, league_id FROM teams")
            cur.execute("DROP TABLE teams")
            cur.execute("ALTER TABLE teams_old RENAME TO teams")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_teams_program "
                        "ON teams (program_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_teams_league "
                        "ON teams (league_id)")
            # The rebuild above recreates `players` at its pre-051 shape (the
            # hardcoded pre-#273 column list), so 051's identity columns are
            # physically gone while still recorded in the ledger. Un-record
            # 051 too: the re-migrate then replays 040 AND 051 in order,
            # exactly like a genuine pre-040 database upgrading through both.
            # SQLite-branch only — the Postgres branch drops just the named
            # constraints and keeps every column, so re-running 051's plain
            # ADD COLUMN there would fail on the existing columns.
            cur.execute(store.dialect.sql(
                "DELETE FROM schema_migrations WHERE version = ?"),
                ("051_athlete_identity",))
        cur.execute(store.dialect.sql(
            "DELETE FROM schema_migrations WHERE version = ?"), (_VERSION,))


# -- schema-catalog inspection (exact named constraints) ------------------
def _fks(store, table):
    """Every declared FK on ``table`` as {(from_col, ref_table, to_col, name,
    on_delete)} — for asserting the EXACT constraints, columns, and NO ACTION
    delete behavior on both dialects."""
    cur = store.conn.cursor()
    if store.backend == "sqlite":
        cur.execute(f"PRAGMA foreign_key_list('{table}')")
        # SQLite gives no constraint name; on_delete is a keyword string.
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
    # confdeltype 'a' = NO ACTION (never 'c' CASCADE).
    delmap = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE",
              "n": "SET NULL", "d": "SET DEFAULT"}
    return {(r["col"], r["ref"], r["refcol"], r["name"],
             delmap.get(r["del"], r["del"])) for r in cur.fetchall()}


def _foreign_key_check_clean(store):
    """True if SQLite reports no FK violations anywhere (no-op on Postgres,
    whose deferred constraints are validated at COMMIT)."""
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
    index — the shape of ux_players_active_team_jersey that must survive the
    players rebuild."""
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
    def _pre040(self, url):
        store = _fresh(url)
        _downgrade_040(store)
        return store

    def _cleanup(self, store):
        if store.backend != "sqlite":
            store.reset_schema()  # leave the shared PostgreSQL DB pristine
        store.close()

    def test_orphans_reported_with_counts_and_ids_zero_mutation(self):
        for label, url in _sql_backends():
            store = self._pre040(url)
            try:
                _add_club_raw(store, "c1")
                _add_team_raw(store, "t1", "c1")           # valid team → club
                with store.transaction():
                    _add_team_raw(store, "t_badc", "ghost_club")  # missing club
                    _add_team_raw(store, "t_null", None)          # NULL — allowed
                    _add_player_raw(store, "p_ok", "t1")          # valid
                    _add_player_raw(store, "p_badt", "ghost_team")  # missing team
                    _add_player_raw(store, "p_null", None)          # NULL — allowed
                self.assertEqual(find_orphan_player_teams(store.conn),
                                 ["p_badt"], label)
                self.assertEqual(find_orphan_team_clubs(store.conn),
                                 ["t_badc"], label)
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    assert_reassignment_fks_ready(store.conn)
                msg = str(ctx.exception)
                # Counts AND actionable row identifiers are named.
                self.assertIn("1 player(s)", msg)
                self.assertIn("p_badt", msg)
                self.assertIn("1 team(s)", msg)
                self.assertIn("t_badc", msg)
                with self.assertRaises(MigrationDataError, msg=label):
                    migrate(store.conn, store.dialect)  # aborts on dangling data
                # FAIL-CLOSED: zero mutation — the FKs were NOT added and the
                # ledger did not advance.
                self.assertEqual(_fks(store, "players"), set(), label)
                self.assertEqual(_fks(store, "teams"), set(), label)
                self.assertNotIn(_VERSION, store.migration_status()["applied"],
                                 label)
            finally:
                self._cleanup(store)

    def test_valid_and_null_references_upgrade_cleanly(self):
        for label, url in _sql_backends():
            store = self._pre040(url)
            try:
                _add_club_raw(store, "c1")
                with store.transaction():
                    _add_team_raw(store, "t1", "c1")   # valid club link
                    _add_team_raw(store, "t2", None)   # NULL club — allowed
                    _add_player_raw(store, "p1", "t1")  # valid team link
                    _add_player_raw(store, "p2", None)  # NULL team — allowed
                self.assertEqual(find_orphan_player_teams(store.conn), [], label)
                self.assertEqual(find_orphan_team_clubs(store.conn), [], label)
                assert_reassignment_fks_ready(store.conn)  # must not raise
                migrate(store.conn, store.dialect)         # succeeds; FKs added
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
                # Exact named constraints, columns/refs, NO ACTION delete.
                self.assertEqual(
                    _fks(store, "teams"),
                    {("club_id", "clubs", "id", _name(store, "fk_teams_club"),
                      "NO ACTION")}, label)
                self.assertEqual(
                    _fks(store, "players"),
                    {("team_id", "teams", "id", _name(store, "fk_players_team"),
                      "NO ACTION")}, label)
                self.assertTrue(_foreign_key_check_clean(store), label)
            finally:
                self._cleanup(store)


def _name(store, pg_name):
    """The expected constraint name in the catalog set — the real name on
    PostgreSQL, ``None`` on SQLite (which stores no FK name)."""
    return pg_name if store.backend == "postgres" else None


class StoreBoundaryTranslationTest(unittest.TestCase):
    """A racing write onto a missing parent surfaces as the stable domain
    conflict, not a raw driver error — at the store boundary, on both SQL
    backends. Memory has no FK and relies on the service check (below)."""

    def test_inserting_player_onto_missing_team_is_team_not_found(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_player(Player(id="p1", team_id="ghost",
                                                name="A", position=Position.FORWARD))
                self.assertEqual(ctx.exception.details["reason"],
                                 "team_not_found", label)
                self.assertEqual(ctx.exception.details["team_id"], "ghost", label)
                self.assertIsNone(store.get_player("p1"), label)
            finally:
                store.close()

    def test_saving_player_onto_missing_team_is_team_not_found(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _add_team_raw(store, "t1", None)
                with store.transaction():
                    store.add_player(Player(id="p1", team_id="t1", name="A",
                                            position=Position.FORWARD))
                moved = store.get_player("p1")
                moved.team_id = "ghost"
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.save_player(moved)
                self.assertEqual(ctx.exception.details["reason"],
                                 "team_not_found", label)
                self.assertEqual(store.get_player("p1").team_id, "t1", label)
            finally:
                store.close()

    def test_inserting_team_onto_missing_club_is_club_not_found(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_team(Team(id="t1", name="A", club_id="ghost"))
                self.assertEqual(ctx.exception.details["reason"],
                                 "club_not_found", label)
                self.assertEqual(ctx.exception.details["club_id"], "ghost", label)
                self.assertIsNone(store.get_team("t1"), label)
            finally:
                store.close()

    def test_saving_team_onto_missing_club_is_club_not_found(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _add_club_raw(store, "c1")
                with store.transaction():
                    store.add_team(Team(id="t1", name="A", club_id="c1"))
                moved = store.get_team("t1")
                moved.club_id = "ghost"
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.save_team(moved)
                self.assertEqual(ctx.exception.details["reason"],
                                 "club_not_found", label)
                self.assertEqual(store.get_team("t1").club_id, "c1", label)
            finally:
                store.close()

    def test_null_parents_are_allowed(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                with store.transaction():
                    store.add_team(Team(id="t1", name="A", club_id=None))
                    store.add_player(Player(id="p1", team_id=None, name="A",
                                            position=Position.FORWARD))
                self.assertIsNotNone(store.get_team("t1"), label)
                self.assertIsNotNone(store.get_player("p1"), label)
            finally:
                store.close()


class MigrationLedgerTest(unittest.TestCase):
    def test_fks_recorded_and_enforced_after_reopen(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            first = SqlStore(path)  # fresh database
            self.assertIn(_VERSION, first.migration_status()["applied"])
            self.assertEqual(
                _fks(first, "teams"),
                {("club_id", "clubs", "id", None, "NO ACTION")})
            self.assertEqual(
                _fks(first, "players"),
                {("team_id", "teams", "id", None, "NO ACTION")})
            first.close()
            second = SqlStore(path)  # reopened — enforcement survives restart
            self.assertTrue(second.migration_status()["current"])
            self.assertEqual(
                _fks(second, "players"),
                {("team_id", "teams", "id", None, "NO ACTION")})
            self.assertTrue(_foreign_key_check_clean(second))
            with self.assertRaises(IntegrityConflictError):  # still enforced
                with second.transaction():
                    second.add_player(Player(id="p1", team_id="ghost", name="A",
                                             position=Position.FORWARD))
            second.close()
        finally:
            os.remove(path)


# =====================================================================
# Service-level parity: Memory + SQLite + PostgreSQL
# =====================================================================
def _seed(store):
    """Program → Season → League → Division → two Clubs → two Teams, one with a
    player. Returns (api, club_id, club2_id, team_id, empty_team_id, player_id)."""
    api = ApiService(store)
    pid = api.create_program("Prog", "US", "UTC")["id"]
    sid = api.create_season(pid, "S1")["id"]
    lid = api.create_league(sid, "Gold")["id"]
    did = api.create_division(sid, "D1", league_id=lid)["id"]
    club = api.create_club("Club")["id"]
    club2 = api.create_club("Club2")["id"]
    team = api.create_team(club_id=club, name="Alpha", league_id=lid,
                           division_id=did)["id"]
    empty = api.create_team(club_id=club2, name="Beta", league_id=lid,
                            division_id=did)["id"]
    player = api.create_player(team, "Vince", "forward")["id"]
    return api, club, club2, team, empty, player


class ServiceParityTest(unittest.TestCase):
    def _backends(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")
        # #201 Slice 2 review: the gate requires Memory/SQLite/PostgreSQL parity
        # for these service-level outcomes, not only the store-boundary tests.
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            SqlStore(url).clear_all_data()
            yield "postgres", SqlStore(url)

    def test_reassign_and_add_onto_missing_parent_rejected(self):
        for label, store in self._backends():
            with self.subTest(backend=label):
                api, club, _c2, team, empty, player = _seed(store)
                with self.assertRaises(NotFoundError, msg=label):
                    api.setup.assign_player_team(player, "team_ghost")
                self.assertEqual(store.get_player(player).team_id, team, label)
                with self.assertRaises(NotFoundError, msg=label):
                    api.setup.assign_team_club(team, "club_ghost")
                self.assertEqual(store.get_team(team).club_id, club, label)
                before = len(store.all_players())
                with self.assertRaises(NotFoundError, msg=label):
                    api.setup.add_player("team_ghost", "New", Position.FORWARD)
                self.assertEqual(len(store.all_players()), before, label)
                # A valid reassignment still works.
                api.setup.assign_player_team(player, empty)
                self.assertEqual(store.get_player(player).team_id, empty, label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_delete_blocked_by_dependents_both_directions(self):
        for label, store in self._backends():
            with self.subTest(backend=label):
                api, club, _c2, team, _empty, _player = _seed(store)
                # Parent delete after a committed child → the existing
                # has-dependencies block, on every backend.
                with self.assertRaises(HasDependenciesError, msg=label):
                    api.setup.delete_club(club)
                self.assertIsNotNone(store.get_club(club), label)
                with self.assertRaises(HasDependenciesError, msg=label):
                    api.setup.delete_team(team)
                self.assertIsNotNone(store.get_team(team), label)
                if isinstance(store, SqlStore):
                    store.close()


# =====================================================================
# PostgreSQL forced barrier races (real row locks + the DB foreign key)
# =====================================================================
def _audit_actions(store, action):
    return sum(1 for a in store.all_setup_audit() if a.action == action)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class ReassignmentRaceTest(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    # -- runners -----------------------------------------------------------
    def _record(self, results, key, fn):
        try:
            fn(); results[key] = "ok"
        except HasDependenciesError:
            results[key] = "blocked"
        except NotFoundError:
            results[key] = "not_found"
        except DomainError as exc:
            results[key] = exc.details.get("reason") or exc.code
        except Exception as exc:  # a raw driver error would land here → fail
            results[key] = f"ERR:{exc}"

    def _forced_delete_wins(self, victim_store, locator_method, victim_fn,
                            racer_fn):
        """Force the parent-delete-wins ordering: pause the victim (the
        child create/reassign) at its parent-locator read, run the racer (the
        delete) to completion on another connection, then resume so the child's
        write races the now-committed delete and must lose at the foreign key."""
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
        """Open barrier race — both start together; the row locks pick one valid
        linearization. Returns {child, delete} outcomes. This is optional extra
        stress; the two orderings are each proven deterministically by
        _forced_delete_wins and _forced_child_wins."""
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
        """Force the child-commit-wins ordering deterministically: pause the child
        AFTER its foreign-key write (its transaction still open, so it holds the
        FK key-share lock on the parent row), start the delete and CONFIRM its
        exact backend is waiting on the parent's FOR UPDATE lock, then release the
        child to commit.
        The delete then observes the committed child and must lose with the
        has-dependencies block — never a cascade, orphan, deadlock, or delete
        audit. Complements _forced_delete_wins (the other ordering)."""
        orig = getattr(child_store, write_method)
        written = threading.Event()
        release = threading.Event()
        calls = [0]

        def instrumented(*a, **k):
            result = orig(*a, **k)   # the INSERT/UPDATE takes the FK key-share lock
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
        # Capture the delete connection's own backend PID BEFORE it runs, so the
        # lock-wait proof targets exactly that connection (an unrelated waiter can
        # never satisfy it). Its SqlStore uses one connection, so the PID is
        # stable across the delete it is about to run.
        delete_pid = self._backend_pid(delete_store)
        tc.start()
        if not written.wait(15):
            release.set(); tc.join(15)
            self.fail("child never reached its foreign-key write")
        td.start()
        # The child holds the parent's FK key-share lock; the delete's
        # get_*_for_update conflicts and must block. Prove THAT EXACT backend is
        # blocked by polling PostgreSQL's pg_stat_activity for delete_pid waiting
        # on a heavyweight Lock (not a fixed sleep, not any waiter).
        self.assertTrue(self._wait_until_blocked_on_lock(delete_pid),
                        "delete backend never registered as waiting on a row lock")
        self.assertIsNone(results.get("delete"),
                          "delete completed without blocking on the child's lock")
        release.set()               # child commits → delete unblocks → sees child
        tc.join(25); td.join(25)
        return results

    def _backend_pid(self, store):
        """The server-side backend PID of a store's PostgreSQL connection."""
        with store.conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid() AS pid")
            return cur.fetchone()["pid"]

    def _wait_until_blocked_on_lock(self, backend_pid, timeout=10.0):
        """Poll PostgreSQL's pg_stat_activity until the DELETE'S OWN backend
        (``backend_pid``) is ACTIVELY waiting on a heavyweight lock
        (``wait_event_type = 'Lock'``) — a deterministic proof that *that exact
        connection* is blocked on the parent row lock, so an unrelated waiter can
        never satisfy it. No reliance on a fixed sleep: the short poll interval
        only bounds busy-spin; correctness comes from the per-PID lock state."""
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

    # -- seeds -------------------------------------------------------------
    def _seed_team(self, api, extra_team=False):
        pid = api.create_program("Prog", "US", "UTC")["id"]
        sid = api.create_season(pid, "S1")["id"]
        lid = api.create_league(sid, "Gold")["id"]
        club = api.create_club("Club")["id"]
        return pid, sid, lid, club

    # -- add_player vs delete_team ----------------------------------------
    def test_add_player_vs_delete_team_forced(self):
        api0 = ApiService(SqlStore(self.url))
        _p, _s, lid, club = self._seed_team(api0)
        team_t = api0.create_team(club_id=club, name="Target", league_id=lid)["id"]
        victim_store = SqlStore(self.url)
        api_v = ApiService(victim_store)
        api_r = ApiService(SqlStore(self.url))
        results = self._forced_delete_wins(
            victim_store, "get_team",
            victim_fn=lambda: api_v.setup.add_player(
                team_t, "Newbie", Position.FORWARD, actor_id="add"),
            racer_fn=lambda: api_r.setup.delete_team(team_t, actor_id="del"))
        self.assertEqual(results, {"child": "team_not_found", "delete": "ok"},
                         results)
        check = SqlStore(self.url)
        self._no_orphan_players(check)
        self.assertEqual(check.all_players(), [], results)
        self.assertIsNone(check.get_team(team_t), results)
        self.assertEqual(_audit_actions(check, "player_added"), 0, results)

    def test_add_player_vs_delete_team_barrier(self):
        api0 = ApiService(SqlStore(self.url))
        _p, _s, lid, club = self._seed_team(api0)
        team_t = api0.create_team(club_id=club, name="Target", league_id=lid)["id"]
        api_c = ApiService(SqlStore(self.url))
        api_d = ApiService(SqlStore(self.url))
        results = self._barrier(
            child_fn=lambda: api_c.setup.add_player(
                team_t, "Newbie", Position.FORWARD, actor_id="add"),
            delete_fn=lambda: api_d.setup.delete_team(team_t, actor_id="del"))
        check = SqlStore(self.url)
        self._no_orphan_players(check)
        if results["child"] == "ok":
            self.assertEqual(results["delete"], "blocked", results)
            self.assertEqual(len(check.players_for_team(team_t)), 1, results)
            self.assertIsNotNone(check.get_team(team_t), results)
            self.assertEqual(_audit_actions(check, "team_deleted"), 0, results)
        else:
            self.assertEqual(results, {"child": "team_not_found", "delete": "ok"},
                             results)
            self.assertEqual(check.all_players(), [], results)
            self.assertIsNone(check.get_team(team_t), results)
            self.assertEqual(_audit_actions(check, "player_added"), 0, results)

    # -- assign_player_team vs delete_team --------------------------------
    def _seed_move(self, api0):
        _p, _s, lid, club = self._seed_team(api0)
        team_a = api0.create_team(club_id=club, name="Alpha", league_id=lid)["id"]
        team_t = api0.create_team(club_id=club, name="Target", league_id=lid)["id"]
        player = api0.create_player(team_a, "Vince", "forward")["id"]
        return team_a, team_t, player

    def test_assign_player_team_vs_delete_team_forced(self):
        api0 = ApiService(SqlStore(self.url))
        team_a, team_t, player = self._seed_move(api0)
        victim_store = SqlStore(self.url)
        api_v = ApiService(victim_store)
        api_r = ApiService(SqlStore(self.url))
        results = self._forced_delete_wins(
            victim_store, "get_team",
            victim_fn=lambda: api_v.setup.assign_player_team(
                player, team_t, actor_id="assign"),
            racer_fn=lambda: api_r.setup.delete_team(team_t, actor_id="del"))
        self.assertEqual(results, {"child": "team_not_found", "delete": "ok"},
                         results)
        check = SqlStore(self.url)
        self._no_orphan_players(check)
        self.assertEqual(check.get_player(player).team_id, team_a, results)
        self.assertIsNone(check.get_team(team_t), results)
        self.assertEqual(_audit_actions(check, "player_team_assigned"), 0, results)

    def test_assign_player_team_vs_delete_team_barrier(self):
        api0 = ApiService(SqlStore(self.url))
        team_a, team_t, player = self._seed_move(api0)
        api_c = ApiService(SqlStore(self.url))
        api_d = ApiService(SqlStore(self.url))
        results = self._barrier(
            child_fn=lambda: api_c.setup.assign_player_team(
                player, team_t, actor_id="assign"),
            delete_fn=lambda: api_d.setup.delete_team(team_t, actor_id="del"))
        check = SqlStore(self.url)
        self._no_orphan_players(check)
        if results["child"] == "ok":
            self.assertEqual(results["delete"], "blocked", results)
            self.assertEqual(check.get_player(player).team_id, team_t, results)
            self.assertIsNotNone(check.get_team(team_t), results)
        else:
            self.assertEqual(results, {"child": "team_not_found", "delete": "ok"},
                             results)
            self.assertEqual(check.get_player(player).team_id, team_a, results)
            self.assertIsNone(check.get_team(team_t), results)
            self.assertEqual(_audit_actions(check, "player_team_assigned"), 0,
                             results)

    # -- create_team vs delete_club ---------------------------------------
    def test_create_team_vs_delete_club_forced(self):
        api0 = ApiService(SqlStore(self.url))
        _p, _s, lid, club = self._seed_team(api0)
        victim_store = SqlStore(self.url)
        api_v = ApiService(victim_store)
        api_r = ApiService(SqlStore(self.url))
        results = self._forced_delete_wins(
            victim_store, "get_club",
            victim_fn=lambda: api_v.setup.create_team(
                club_id=club, name="New", league_id=lid, actor_id="create"),
            racer_fn=lambda: api_r.setup.delete_club(club, actor_id="del"))
        self.assertEqual(results, {"child": "club_not_found", "delete": "ok"},
                         results)
        check = SqlStore(self.url)
        self._no_orphan_team_clubs(check)
        self.assertEqual([t for t in check.all_teams() if t.club_id == club], [],
                         results)
        self.assertIsNone(check.get_club(club), results)
        self.assertEqual(_audit_actions(check, "team_created"), 0, results)

    def test_create_team_vs_delete_club_barrier(self):
        api0 = ApiService(SqlStore(self.url))
        _p, _s, lid, club = self._seed_team(api0)
        api_c = ApiService(SqlStore(self.url))
        api_d = ApiService(SqlStore(self.url))
        results = self._barrier(
            child_fn=lambda: api_c.setup.create_team(
                club_id=club, name="New", league_id=lid, actor_id="create"),
            delete_fn=lambda: api_d.setup.delete_club(club, actor_id="del"))
        check = SqlStore(self.url)
        self._no_orphan_team_clubs(check)
        teams_on_club = [t for t in check.all_teams() if t.club_id == club]
        if results["child"] == "ok":
            self.assertEqual(results["delete"], "blocked", results)
            self.assertEqual(len(teams_on_club), 1, results)
            self.assertIsNotNone(check.get_club(club), results)
            self.assertEqual(_audit_actions(check, "club_deleted"), 0, results)
        else:
            self.assertEqual(results, {"child": "club_not_found", "delete": "ok"},
                             results)
            self.assertEqual(teams_on_club, [], results)
            self.assertIsNone(check.get_club(club), results)
            self.assertEqual(_audit_actions(check, "team_created"), 0, results)

    # -- assign_team_club vs delete_club ----------------------------------
    def _seed_rebind(self, api0):
        _p, _s, lid, club_old = self._seed_team(api0)
        club_c = api0.create_club("Target")["id"]  # club to delete
        team_t = api0.create_team(club_id=club_old, name="Alpha",
                                  league_id=lid)["id"]
        return club_old, club_c, team_t

    def test_assign_team_club_vs_delete_club_forced(self):
        api0 = ApiService(SqlStore(self.url))
        club_old, club_c, team_t = self._seed_rebind(api0)
        victim_store = SqlStore(self.url)
        api_v = ApiService(victim_store)
        api_r = ApiService(SqlStore(self.url))
        results = self._forced_delete_wins(
            victim_store, "get_club",
            victim_fn=lambda: api_v.setup.assign_team_club(
                team_t, club_c, actor_id="assign"),
            racer_fn=lambda: api_r.setup.delete_club(club_c, actor_id="del"))
        self.assertEqual(results, {"child": "club_not_found", "delete": "ok"},
                         results)
        check = SqlStore(self.url)
        self._no_orphan_team_clubs(check)
        self.assertEqual(check.get_team(team_t).club_id, club_old, results)
        self.assertIsNone(check.get_club(club_c), results)
        self.assertEqual(_audit_actions(check, "team_club_assigned"), 0, results)

    def test_assign_team_club_vs_delete_club_barrier(self):
        api0 = ApiService(SqlStore(self.url))
        club_old, club_c, team_t = self._seed_rebind(api0)
        api_c = ApiService(SqlStore(self.url))
        api_d = ApiService(SqlStore(self.url))
        results = self._barrier(
            child_fn=lambda: api_c.setup.assign_team_club(
                team_t, club_c, actor_id="assign"),
            delete_fn=lambda: api_d.setup.delete_club(club_c, actor_id="del"))
        check = SqlStore(self.url)
        self._no_orphan_team_clubs(check)
        if results["child"] == "ok":
            self.assertEqual(results["delete"], "blocked", results)
            self.assertEqual(check.get_team(team_t).club_id, club_c, results)
            self.assertIsNotNone(check.get_club(club_c), results)
            self.assertEqual(_audit_actions(check, "club_deleted"), 0, results)
        else:
            self.assertEqual(results, {"child": "club_not_found", "delete": "ok"},
                             results)
            self.assertEqual(check.get_team(team_t).club_id, club_old, results)
            self.assertIsNone(check.get_club(club_c), results)
            self.assertEqual(_audit_actions(check, "team_club_assigned"), 0,
                             results)

    # -- child-commit-wins (delete loses with has_dependencies) -----------
    def test_add_player_vs_delete_team_child_wins(self):
        api0 = ApiService(SqlStore(self.url))
        _p, _s, lid, club = self._seed_team(api0)
        team_t = api0.create_team(club_id=club, name="Target", league_id=lid)["id"]
        child_store = SqlStore(self.url)
        api_c = ApiService(child_store)
        delete_store = SqlStore(self.url)
        api_d = ApiService(delete_store)
        results = self._forced_child_wins(
            child_store, "add_player",
            child_fn=lambda: api_c.setup.add_player(
                team_t, "Newbie", Position.FORWARD, actor_id="add"),
            delete_store=delete_store,
            delete_fn=lambda: api_d.setup.delete_team(team_t, actor_id="del"))
        self.assertEqual(results, {"child": "ok", "delete": "blocked"}, results)
        check = SqlStore(self.url)
        self._no_orphan_players(check)
        self.assertEqual(len(check.players_for_team(team_t)), 1, results)
        self.assertIsNotNone(check.get_team(team_t), results)
        self.assertEqual(_audit_actions(check, "team_deleted"), 0, results)

    def test_assign_player_team_vs_delete_team_child_wins(self):
        api0 = ApiService(SqlStore(self.url))
        team_a, team_t, player = self._seed_move(api0)
        child_store = SqlStore(self.url)
        api_c = ApiService(child_store)
        delete_store = SqlStore(self.url)
        api_d = ApiService(delete_store)
        results = self._forced_child_wins(
            child_store, "save_player",
            child_fn=lambda: api_c.setup.assign_player_team(
                player, team_t, actor_id="assign"),
            delete_store=delete_store,
            delete_fn=lambda: api_d.setup.delete_team(team_t, actor_id="del"))
        self.assertEqual(results, {"child": "ok", "delete": "blocked"}, results)
        check = SqlStore(self.url)
        self._no_orphan_players(check)
        self.assertEqual(check.get_player(player).team_id, team_t, results)
        self.assertIsNotNone(check.get_team(team_t), results)
        self.assertEqual(_audit_actions(check, "team_deleted"), 0, results)

    def test_create_team_vs_delete_club_child_wins(self):
        api0 = ApiService(SqlStore(self.url))
        _p, _s, lid, club = self._seed_team(api0)
        child_store = SqlStore(self.url)
        api_c = ApiService(child_store)
        delete_store = SqlStore(self.url)
        api_d = ApiService(delete_store)
        results = self._forced_child_wins(
            child_store, "add_team",
            child_fn=lambda: api_c.setup.create_team(
                club_id=club, name="New", league_id=lid, actor_id="create"),
            delete_store=delete_store,
            delete_fn=lambda: api_d.setup.delete_club(club, actor_id="del"))
        self.assertEqual(results, {"child": "ok", "delete": "blocked"}, results)
        check = SqlStore(self.url)
        self._no_orphan_team_clubs(check)
        self.assertEqual(
            len([t for t in check.all_teams() if t.club_id == club]), 1, results)
        self.assertIsNotNone(check.get_club(club), results)
        self.assertEqual(_audit_actions(check, "club_deleted"), 0, results)

    def test_assign_team_club_vs_delete_club_child_wins(self):
        api0 = ApiService(SqlStore(self.url))
        club_old, club_c, team_t = self._seed_rebind(api0)
        child_store = SqlStore(self.url)
        api_c = ApiService(child_store)
        delete_store = SqlStore(self.url)
        api_d = ApiService(delete_store)
        results = self._forced_child_wins(
            child_store, "save_team",
            child_fn=lambda: api_c.setup.assign_team_club(
                team_t, club_c, actor_id="assign"),
            delete_store=delete_store,
            delete_fn=lambda: api_d.setup.delete_club(club_c, actor_id="del"))
        self.assertEqual(results, {"child": "ok", "delete": "blocked"}, results)
        check = SqlStore(self.url)
        self._no_orphan_team_clubs(check)
        self.assertEqual(check.get_team(team_t).club_id, club_c, results)
        self.assertIsNotNone(check.get_club(club_c), results)
        self.assertEqual(_audit_actions(check, "club_deleted"), 0, results)

    # -- invariants --------------------------------------------------------
    def _no_orphan_players(self, store):
        orphans = [p.id for p in store.all_players()
                   if p.team_id is not None and store.get_team(p.team_id) is None]
        self.assertEqual(orphans, [], "player stranded on a deleted team")

    def _no_orphan_team_clubs(self, store):
        orphans = [t.id for t in store.all_teams()
                   if t.club_id is not None and store.get_club(t.club_id) is None]
        self.assertEqual(orphans, [], "team stranded on a deleted club")


class MigrationRebuildPreservationTest(unittest.TestCase):
    """Migration 040 rebuilds `teams` and `players` (create-copy-drop-rename),
    and `players` is REFERENCED by game_roster_entries (migration 027). This is
    the durable regression proof that a POPULATED upgrade through 040 preserves —
    across a close/reopen — every team/player row value, the incoming
    game_roster_entries → players reference, the new FKs, and every recreated
    index (including the partial unique ux_players_active_team_jersey). It guards
    the shared migration-runner fix (SQLite foreign_keys = OFF around the rebuild
    + foreign_key_check gate) that #201 Slice 3 introduced: without it, dropping
    the still-referenced `players` with an existing roster row fails at COMMIT.
    Runs on a file-backed SQLite database (so the reopen is real) and on
    PostgreSQL when available."""

    _TEAM_COLS = ("id", "name", "division", "club_id", "division_id",
                  "external_ref", "program_id", "league_id")
    _TEAM = ("t1", "Team One", "U16", "c1", "div1", "EXT-T1", "prog1", "lg1")
    _PLAYER_COLS = ("id", "team_id", "name", "position", "shoots",
                    "jersey_number", "is_active", "guardian_person_id",
                    "external_ref")
    _PLAYER = ("p1", "t1", "Player One", "forward", "left", 17, 1, "guard1",
               "EXT-P1")

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
        q = store.dialect.sql
        cur = store.conn.cursor()

        def ins(sql, params):
            cur.execute(q(sql), params)

        ins("INSERT INTO clubs (id, name) VALUES (?, ?)", ("c1", "Club One"))
        ins("INSERT INTO teams (" + ", ".join(self._TEAM_COLS) + ") VALUES ("
            + ", ".join("?" for _ in self._TEAM_COLS) + ")", self._TEAM)
        ins("INSERT INTO players (" + ", ".join(self._PLAYER_COLS) + ") VALUES ("
            + ", ".join("?" for _ in self._PLAYER_COLS) + ")", self._PLAYER)
        # A Game + roster row so players carries an INCOMING reference during the
        # 040 rebuild (game_roster_entries.player_id → players, migration 027).
        ins("INSERT INTO games (id, ice_slot_id, cancelled, game_type) "
            "VALUES (?, ?, 0, 'regular')", ("g1", None))
        ins("INSERT INTO game_roster_entries (id, game_id, player_id) "
            "VALUES (?, ?, ?)", ("re1", "g1", "p1"))

    def _row_values(self, store, cols, sql, params):
        # sqlite3.Row supports tuple(row) → values, but psycopg's dict_row makes
        # tuple(row) → keys; index by column name so the comparison is portable.
        cur = store.conn.cursor()
        cur.execute(store.dialect.sql(sql), params)
        r = cur.fetchone()
        return tuple(r[c] for c in cols)

    def _assert_preserved(self, store, label):
        self.assertEqual(
            self._row_values(store, self._TEAM_COLS,
                             "SELECT " + ", ".join(self._TEAM_COLS)
                             + " FROM teams WHERE id = ?", ("t1",)),
            self._TEAM, label)
        self.assertEqual(
            self._row_values(store, self._PLAYER_COLS,
                             "SELECT " + ", ".join(self._PLAYER_COLS)
                             + " FROM players WHERE id = ?", ("p1",)),
            self._PLAYER, label)
        # Incoming roster reference survived the players rebuild.
        self.assertEqual(
            self._row_values(store, ("game_id", "player_id"),
                             "SELECT game_id, player_id FROM game_roster_entries "
                             "WHERE id = ?", ("re1",)),
            ("g1", "p1"), label)
        # Named FKs — outgoing on the rebuilt tables and the incoming roster ones.
        self.assertEqual(
            _fks(store, "teams"),
            {("club_id", "clubs", "id", _name(store, "fk_teams_club"),
              "NO ACTION")}, label)
        self.assertEqual(
            _fks(store, "players"),
            {("team_id", "teams", "id", _name(store, "fk_players_team"),
              "NO ACTION")}, label)
        roster_targets = {ref for (_c, ref, _t, _n, _d)
                          in _fks(store, "game_roster_entries")}
        self.assertIn("players", roster_targets, label)
        self.assertIn("games", roster_targets, label)
        # Every recreated index, including the partial unique active-jersey one.
        self.assertIn("ix_players_team", _indexes(store, "players"), label)
        self.assertTrue(
            _is_partial_unique(store, "players", "ux_players_active_team_jersey"),
            label)
        team_idx = _indexes(store, "teams")
        self.assertIn("ix_teams_program", team_idx, label)
        self.assertIn("ix_teams_league", team_idx, label)
        self.assertTrue(_foreign_key_check_clean(store), label)

    def test_populated_upgrade_through_040_preserves_teams_players_and_roster(self):
        for label, loc in self._locations():
            store = SqlStore(loc)
            try:
                if store.backend == "postgres":
                    store.reset_schema()
                _downgrade_040(store)          # back to the pre-040 schema
                with store.transaction():
                    self._seed_rich(store)     # now teams/players carry data + a
                                               # live incoming roster reference
                migrate(store.conn, store.dialect)   # re-apply 040 (the rebuild)
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
                store.close()
                store = SqlStore(loc)          # a real close/reopen cycle
                self._assert_preserved(store, label)
            finally:
                if store.backend == "postgres":
                    store.reset_schema()
                store.close()


if __name__ == "__main__":
    unittest.main()
