"""Program/Organization + Venue-owner referential integrity (#201 Slice 4).

Migration 042 adds five ON DELETE NO ACTION foreign keys backstopping the
create-child-vs-delete-parent races that no row lock covers:

  programs.operator_organization_id → organizations   (create_program vs delete_organization)
  venues.organization_id            → organizations   (create_venue   vs delete_organization)
  seasons.program_id                → programs         (create_season  vs delete_program)
  leagues.program_id                → programs         (create_league  vs delete_program)
  venues.league_id (legacy owner)   → programs         (create_venue   vs delete_program)

Mirrors test_iceslot_venue_fks (Slice 3): a forward-only pre-migration gate, a
store-boundary translation of racing writes into stable domain conflicts, the
itemised has-dependencies contract on the child-commits-wins delete race
(re-resolved after the outermost rollback so nested transactions stay safe), and
forced-PostgreSQL races with a per-PID lock-wait proof. Runs on SQLite and (when
TEST_DATABASE_URL is set) PostgreSQL.

Two Slice-4 specifics:
  * venues carries TWO outgoing foreign keys, so the store disambiguates which
    parent went missing (PostgreSQL by constraint name, SQLite by re-reading).
  * leagues.program_id is exercised at the store boundary only: create_league
    requires a Season, whose presence blocks delete_program, so the forced
    *service* race for that family is naturally serialised.
"""

import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain.errors import (
    ConcurrencyConflictError,
    DomainError,
    HasDependenciesError,
    IntegrityConflictError,
    NotFoundError,
)
from hockey_scheduler.domain.setup_models import (
    League, Organization, Program, Season, Venue)
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.store.integrity_checks import (
    MigrationDataError,
    assert_program_org_fks_ready,
    find_orphan_league_programs,
    find_orphan_program_orgs,
    find_orphan_season_programs,
    find_orphan_venue_orgs,
    find_orphan_venue_programs,
)
from hockey_scheduler.store.sql_store import migrate

UTC = timezone.utc
_VERSION = "042_program_org_fks"


def _sql_backends():
    yield "sqlite", ":memory:"
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        SqlStore(url).clear_all_data()
        yield "postgres", url


def _fresh(url):
    store = SqlStore(url)
    if url != ":memory:":
        store.reset_schema()
    return store


def _downgrade_042(store):
    """Reverse migration 042 to the prior schema: drop all five FKs and un-record
    it. Postgres drops the named constraints; SQLite rebuilds each table without
    its FK (recreating every index), mirroring 042's create-copy-drop-rename.
    Dangling rows can then be planted before it re-runs."""
    with store.transaction():
        cur = store.conn.cursor()
        if store.backend == "postgres":
            cur.execute("ALTER TABLE programs "
                        "DROP CONSTRAINT IF EXISTS fk_programs_operator_org")
            cur.execute("ALTER TABLE venues "
                        "DROP CONSTRAINT IF EXISTS fk_venues_organization")
            cur.execute("ALTER TABLE seasons "
                        "DROP CONSTRAINT IF EXISTS fk_seasons_program")
            cur.execute("ALTER TABLE leagues "
                        "DROP CONSTRAINT IF EXISTS fk_leagues_program")
            cur.execute("ALTER TABLE venues "
                        "DROP CONSTRAINT IF EXISTS fk_venues_program")
        else:
            cur.execute("PRAGMA defer_foreign_keys = ON")
            cur.execute("CREATE TABLE programs_old (id TEXT PRIMARY KEY, name TEXT, "
                        "country TEXT, timezone TEXT, operator_organization_id TEXT, "
                        "external_ref TEXT, "
                        # #277 policy columns (migration 046) so the full-SPEC
                        # add_program insert still fits this pre-042 rebuild.
                        "turnover_minutes INTEGER, curfew_local TEXT, "
                        "warmup_minutes INTEGER, resurface_minutes INTEGER)")
            cur.execute("INSERT INTO programs_old SELECT id, name, country, "
                        "timezone, operator_organization_id, external_ref, "
                        "turnover_minutes, curfew_local, warmup_minutes, "
                        "resurface_minutes FROM programs")
            cur.execute("DROP TABLE programs")
            cur.execute("ALTER TABLE programs_old RENAME TO programs")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_programs_operator_organization "
                        "ON programs (operator_organization_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_programs_external_ref "
                        "ON programs (external_ref)")
            cur.execute("CREATE TABLE seasons_old (id TEXT PRIMARY KEY, "
                        "program_id TEXT, name TEXT, start_date TEXT, end_date TEXT, "
                        "external_ref TEXT, status TEXT NOT NULL DEFAULT 'active', "
                        "archived_at TEXT)")
            cur.execute("INSERT INTO seasons_old SELECT id, program_id, name, "
                        "start_date, end_date, external_ref, status, archived_at "
                        "FROM seasons")
            cur.execute("DROP TABLE seasons")
            cur.execute("ALTER TABLE seasons_old RENAME TO seasons")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_seasons_external_ref "
                        "ON seasons (external_ref)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_seasons_program "
                        "ON seasons (program_id)")
            cur.execute("CREATE TABLE leagues_old (id TEXT PRIMARY KEY, name TEXT, "
                        "sort_order INTEGER, external_ref TEXT, program_id TEXT)")
            cur.execute("INSERT INTO leagues_old SELECT id, name, sort_order, "
                        "external_ref, program_id FROM leagues")
            cur.execute("DROP TABLE leagues")
            cur.execute("ALTER TABLE leagues_old RENAME TO leagues")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_leagues_external_ref "
                        "ON leagues (external_ref)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_leagues_program "
                        "ON leagues (program_id)")
            cur.execute("CREATE TABLE venues_old (id TEXT PRIMARY KEY, name TEXT, "
                        "address TEXT, timezone TEXT, organization_id TEXT, "
                        "league_id TEXT, external_ref TEXT)")
            cur.execute("INSERT INTO venues_old SELECT id, name, address, timezone, "
                        "organization_id, league_id, external_ref FROM venues")
            cur.execute("DROP TABLE venues")
            cur.execute("ALTER TABLE venues_old RENAME TO venues")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_venues_league "
                        "ON venues (league_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_venues_external_ref "
                        "ON venues (external_ref)")
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
    cur = store.conn.cursor()
    if store.backend == "sqlite":
        cur.execute(f"PRAGMA index_list('{table}')")
        return {r["name"] for r in cur.fetchall()
                if not r["name"].startswith("sqlite_autoindex")}
    cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = %s", (table,))
    return {r["indexname"] for r in cur.fetchall()
            if not r["indexname"].endswith("_pkey")}


# -- raw planters (used after _downgrade_042, so no FK rejects the dangling id) --
def _add_org(store, oid):
    store.add_organization(Organization(id=oid, name=oid))


def _add_program(store, pid, org=None):
    store.add_program(Program(id=pid, name=pid, operator_organization_id=org))


def _add_season(store, sid, program=None):
    store.add_season(Season(id=sid, program_id=program, name=sid))


def _add_league(store, lid, program=None):
    store.add_league(League(id=lid, program_id=program, name=lid))


def _add_venue(store, vid, org=None, league=None):
    store.add_venue(Venue(id=vid, name=vid, organization_id=org, league_id=league))


class PreMigrationValidationTest(unittest.TestCase):
    def _pre042(self, url):
        store = _fresh(url)
        _downgrade_042(store)
        return store

    def _cleanup(self, store):
        if store.backend != "sqlite":
            store.reset_schema()
        store.close()

    def test_orphans_reported_with_counts_and_ids_zero_mutation(self):
        for label, url in _sql_backends():
            store = self._pre042(url)
            try:
                with store.transaction():
                    _add_org(store, "o1")
                    _add_program(store, "p_ok", "o1")        # valid
                    _add_program(store, "p_bad", "ghost_org")  # dangling → org
                    _add_program(store, "p_null", None)        # NULL — allowed
                    _add_venue(store, "v_ok_o", "o1")          # valid org
                    _add_venue(store, "v_bad_o", "ghost_org")  # dangling → org
                    _add_season(store, "s_ok", "p_ok")         # valid program
                    _add_season(store, "s_bad", "ghost_prog")  # dangling → program
                    _add_league(store, "l_ok", "p_ok")         # valid program
                    _add_league(store, "l_bad", "ghost_prog")  # dangling → program
                    _add_venue(store, "v_bad_p", league="ghost_prog")  # dangling → prog
                self.assertEqual(find_orphan_program_orgs(store.conn),
                                 ["p_bad"], label)
                self.assertEqual(find_orphan_venue_orgs(store.conn),
                                 ["v_bad_o"], label)
                self.assertEqual(find_orphan_season_programs(store.conn),
                                 ["s_bad"], label)
                self.assertEqual(find_orphan_league_programs(store.conn),
                                 ["l_bad"], label)
                self.assertEqual(find_orphan_venue_programs(store.conn),
                                 ["v_bad_p"], label)
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    assert_program_org_fks_ready(store.conn)
                msg = str(ctx.exception)
                for token in ("1 program(s)", "p_bad", "1 venue(s)", "v_bad_o",
                              "1 season(s)", "s_bad", "1 league(s)", "l_bad",
                              "v_bad_p"):
                    self.assertIn(token, msg, label)
                with self.assertRaises(MigrationDataError, msg=label):
                    migrate(store.conn, store.dialect)
                # FAIL-CLOSED: zero mutation — no FK added, ledger not advanced.
                self.assertEqual(_fks(store, "programs"), set(), label)
                self.assertEqual(_fks(store, "venues"), set(), label)
                self.assertNotIn(_VERSION, store.migration_status()["applied"],
                                 label)
            finally:
                self._cleanup(store)

    def test_valid_and_null_references_upgrade_cleanly(self):
        for label, url in _sql_backends():
            store = self._pre042(url)
            try:
                with store.transaction():
                    _add_org(store, "o1")
                    _add_program(store, "p1", "o1")   # valid
                    _add_program(store, "p2", None)   # NULL — allowed
                    _add_venue(store, "v1", "o1", "p1")  # valid org + program
                    _add_venue(store, "v2", None, None)  # NULL — allowed
                    _add_season(store, "s1", "p1")    # valid
                    _add_league(store, "l1", "p1")    # valid
                for finder in (find_orphan_program_orgs, find_orphan_venue_orgs,
                               find_orphan_season_programs, find_orphan_league_programs,
                               find_orphan_venue_programs):
                    self.assertEqual(finder(store.conn), [], label)
                assert_program_org_fks_ready(store.conn)  # must not raise
                migrate(store.conn, store.dialect)        # succeeds; FKs added
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
                self.assertEqual(
                    _fks(store, "programs"),
                    {("operator_organization_id", "organizations", "id",
                      _name(store, "fk_programs_operator_org"), "NO ACTION")}, label)
                self.assertEqual(
                    _fks(store, "seasons"),
                    {("program_id", "programs", "id",
                      _name(store, "fk_seasons_program"), "NO ACTION")}, label)
                self.assertEqual(
                    _fks(store, "leagues"),
                    {("program_id", "programs", "id",
                      _name(store, "fk_leagues_program"), "NO ACTION")}, label)
                self.assertEqual(
                    _fks(store, "venues"),
                    {("organization_id", "organizations", "id",
                      _name(store, "fk_venues_organization"), "NO ACTION"),
                     ("league_id", "programs", "id",
                      _name(store, "fk_venues_program"), "NO ACTION")}, label)
                self.assertTrue(_foreign_key_check_clean(store), label)
            finally:
                self._cleanup(store)


class StoreBoundaryTranslationTest(unittest.TestCase):
    """A racing write onto a missing parent surfaces as the stable domain
    conflict (organization_not_found / program_not_found), not a raw driver
    error — on both the insert and the update path, for all five FKs, with the
    venue two-FK case disambiguated."""

    def _reason(self, store, fn):
        with self.assertRaises(IntegrityConflictError) as cm:
            with store.transaction():
                fn(store)
        return cm.exception.details

    def _run(self, plant, expect_reason, expect_ctx):
        for label, url in _sql_backends():
            store = SqlStore(url)
            details = self._reason(store, plant)
            self.assertEqual(details.get("reason"), expect_reason, label)
            for k, v in expect_ctx.items():
                self.assertEqual(details.get(k), v, label)
            store.close()

    def test_insert_program_onto_missing_org(self):
        self._run(lambda s: _add_program(s, "p1", "ghost"),
                  "organization_not_found", {"organization_id": "ghost"})

    def test_insert_venue_onto_missing_org(self):
        self._run(lambda s: _add_venue(s, "v1", org="ghost"),
                  "organization_not_found", {"organization_id": "ghost"})

    def test_insert_season_onto_missing_program(self):
        self._run(lambda s: _add_season(s, "s1", "ghost"),
                  "program_not_found", {"program_id": "ghost"})

    def test_insert_league_onto_missing_program(self):
        self._run(lambda s: _add_league(s, "l1", "ghost"),
                  "program_not_found", {"program_id": "ghost"})

    def test_insert_venue_onto_missing_program(self):
        self._run(lambda s: _add_venue(s, "v1", league="ghost"),
                  "program_not_found", {"program_id": "ghost"})

    def test_venue_both_parents_missing_reports_organization_first(self):
        self._run(lambda s: _add_venue(s, "v1", org="gh_o", league="gh_p"),
                  "organization_not_found", {"organization_id": "gh_o"})

    def test_save_program_onto_missing_org(self):
        def plant(store):
            _add_org(store, "o1")
            _add_program(store, "p1", "o1")
            p = store.get_program("p1")
            p.operator_organization_id = "ghost"
            store.save_program(p)
        self._run(plant, "organization_not_found", {"organization_id": "ghost"})

    def test_save_season_onto_missing_program(self):
        def plant(store):
            _add_program(store, "p1")
            _add_season(store, "s1", "p1")
            s = store.get_season("s1")
            s.program_id = "ghost"
            store.save_season(s)
        self._run(plant, "program_not_found", {"program_id": "ghost"})

    def test_save_league_onto_missing_program(self):
        def plant(store):
            _add_program(store, "p1")
            _add_league(store, "l1", "p1")
            lg = store.get_league("l1")
            lg.program_id = "ghost"
            store.save_league(lg)
        self._run(plant, "program_not_found", {"program_id": "ghost"})

    def test_save_venue_onto_missing_org(self):
        def plant(store):
            _add_org(store, "o1")
            _add_venue(store, "v1", org="o1")
            v = store.get_venue("v1")
            v.organization_id = "ghost"
            store.save_venue(v)
        self._run(plant, "organization_not_found", {"organization_id": "ghost"})

    def test_save_venue_onto_missing_program(self):
        def plant(store):
            _add_venue(store, "v1")
            v = store.get_venue("v1")
            v.league_id = "ghost"
            store.save_venue(v)
        self._run(plant, "program_not_found", {"program_id": "ghost"})

    def test_null_parents_are_allowed(self):
        for label, url in _sql_backends():
            store = SqlStore(url)
            with store.transaction():
                _add_program(store, "p1", None)
                _add_season(store, "s1", None)
                _add_league(store, "l1", None)
                _add_venue(store, "v1", None, None)
            self.assertIsNotNone(store.get_program("p1"), label)
            self.assertIsNotNone(store.get_venue("v1"), label)
            store.close()


class MigrationLedgerTest(unittest.TestCase):
    def test_fks_recorded_and_enforced_after_reopen(self):
        for label, url in _sql_backends():
            if url == ":memory:":
                continue  # a reopen needs a durable database
            SqlStore(url).reset_schema()
            first = SqlStore(url)
            self.assertIn(_VERSION, first.migration_status()["applied"], label)
            first.close()
            store = SqlStore(url)
            try:
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
                with self.assertRaises(IntegrityConflictError, msg=label):
                    with store.transaction():
                        _add_season(store, "s_bad", "ghost_prog")
            finally:
                store.reset_schema()
                store.close()


class MigrationRebuildPreservationTest(unittest.TestCase):
    """A *populated* upgrade-from-041 preserves every rebuilt row value, every
    recreated index, the five new outgoing FKs, AND the incoming
    rinks → venues / season_venue_access → venues references (migration 041) that
    the venues rebuild must keep. This is the regression that would catch a
    column drop/reorder or a botched populated rebuild — the failure mode the
    runner foreign_keys=OFF + foreign_key_check mechanism exists for. Runs on a
    file-backed SQLite database (so the reopen is real) and on PostgreSQL when
    available."""

    _ORG = ("o1", "Org One", "OO", "EXT-O1")
    _PROGRAM = ("p1", "Program One", "CA", "America/Toronto", "o1", "EXT-P1")
    _SEASON = ("s1", "p1", "Winter", "2027-01-01", "2027-03-31", "EXT-S1",
               "archived", "2027-04-01T00:00:00+00:00")
    _LEAGUE = ("l1", "Gold", 7, "EXT-L1", "p1")
    _VENUE = ("v1", "Ice Palace", "1 Rink Rd", "America/New_York", "o1", "p1",
              "EXT-V1")
    _RINK = ("r1", "v1", "North Rink", "EXT-R1")     # incoming (041 FK)
    _SVA = ("a1", "s1", "v1", 1)                      # incoming (041 FK)

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

        # Owners first, then venue, then the incoming (041-FK) dependents.
        ins("INSERT INTO organizations (id, name, short_name, external_ref) "
            "VALUES (?, ?, ?, ?)", self._ORG)
        ins("INSERT INTO programs (id, name, country, timezone, "
            "operator_organization_id, external_ref) VALUES (?, ?, ?, ?, ?, ?)",
            self._PROGRAM)
        ins("INSERT INTO seasons (id, program_id, name, start_date, end_date, "
            "external_ref, status, archived_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            self._SEASON)
        ins("INSERT INTO leagues (id, name, sort_order, external_ref, program_id) "
            "VALUES (?, ?, ?, ?, ?)", self._LEAGUE)
        ins("INSERT INTO venues (id, name, address, timezone, organization_id, "
            "league_id, external_ref) VALUES (?, ?, ?, ?, ?, ?, ?)", self._VENUE)
        ins("INSERT INTO rinks (id, venue_id, name, external_ref) "
            "VALUES (?, ?, ?, ?)", self._RINK)
        ins("INSERT INTO season_venue_access (id, season_id, venue_id, active) "
            "VALUES (?, ?, ?, ?)", self._SVA)

    def _row(self, store, sql, params=()):
        cur = store.conn.cursor()
        cur.execute(store.dialect.sql(sql), params)
        return cur.fetchone()

    def _row_values(self, store, cols, sql, params):
        # psycopg dict_row makes tuple(row) yield keys, so index by name.
        r = self._row(store, sql, params)
        return tuple(r[c] for c in cols)

    def _assert_preserved(self, store, label):
        prog_cols = ("id", "name", "country", "timezone",
                     "operator_organization_id", "external_ref")
        self.assertEqual(
            self._row_values(store, prog_cols,
                             "SELECT " + ", ".join(prog_cols)
                             + " FROM programs WHERE id = ?", ("p1",)),
            self._PROGRAM, label)
        season_cols = ("id", "program_id", "name", "start_date", "end_date",
                       "external_ref", "status", "archived_at")
        self.assertEqual(
            self._row_values(store, season_cols,
                             "SELECT " + ", ".join(season_cols)
                             + " FROM seasons WHERE id = ?", ("s1",)),
            self._SEASON, label)
        league_cols = ("id", "name", "sort_order", "external_ref", "program_id")
        self.assertEqual(
            self._row_values(store, league_cols,
                             "SELECT " + ", ".join(league_cols)
                             + " FROM leagues WHERE id = ?", ("l1",)),
            self._LEAGUE, label)
        venue_cols = ("id", "name", "address", "timezone", "organization_id",
                      "league_id", "external_ref")
        self.assertEqual(
            self._row_values(store, venue_cols,
                             "SELECT " + ", ".join(venue_cols)
                             + " FROM venues WHERE id = ?", ("v1",)),
            self._VENUE, label)
        # Incoming references survived the venues rebuild.
        self.assertEqual(
            self._row(store, "SELECT venue_id FROM rinks WHERE id = ?",
                      ("r1",))["venue_id"], "v1", label)
        self.assertEqual(
            self._row(store, "SELECT venue_id FROM season_venue_access "
                             "WHERE id = ?", ("a1",))["venue_id"], "v1", label)
        # Outgoing FK catalogs (all five) + the incoming rinks/sva → venues.
        self.assertEqual(
            _fks(store, "programs"),
            {("operator_organization_id", "organizations", "id",
              _name(store, "fk_programs_operator_org"), "NO ACTION")}, label)
        self.assertEqual(
            _fks(store, "seasons"),
            {("program_id", "programs", "id",
              _name(store, "fk_seasons_program"), "NO ACTION")}, label)
        self.assertEqual(
            _fks(store, "leagues"),
            {("program_id", "programs", "id",
              _name(store, "fk_leagues_program"), "NO ACTION")}, label)
        self.assertEqual(
            _fks(store, "venues"),
            {("organization_id", "organizations", "id",
              _name(store, "fk_venues_organization"), "NO ACTION"),
             ("league_id", "programs", "id",
              _name(store, "fk_venues_program"), "NO ACTION")}, label)
        rink_fk_targets = {ref for (_c, ref, _t, _n, _d) in _fks(store, "rinks")}
        self.assertIn("venues", rink_fk_targets, label)
        sva_fk_targets = {ref for (_c, ref, _t, _n, _d)
                          in _fks(store, "season_venue_access")}
        self.assertIn("venues", sva_fk_targets, label)
        # Every recreated index.
        for table, names in (
                ("programs", ("ix_programs_operator_organization",
                              "ix_programs_external_ref")),
                ("seasons", ("ix_seasons_external_ref", "ix_seasons_program")),
                ("leagues", ("ix_leagues_external_ref", "ix_leagues_program")),
                ("venues", ("ix_venues_league", "ix_venues_external_ref"))):
            idx = _indexes(store, table)
            for name in names:
                self.assertIn(name, idx, f"{label}: {table} index {name}")
        self.assertTrue(_foreign_key_check_clean(store), label)

    def test_populated_upgrade_from_041_preserves_values_indexes_incoming_refs(self):
        for label, loc in self._locations():
            store = SqlStore(loc)
            try:
                if store.backend == "postgres":
                    store.reset_schema()
                _downgrade_042(store)         # back to the pre-042 schema
                with store.transaction():
                    self._seed_rich(store)
                migrate(store.conn, store.dialect)   # apply 042 (the rebuild)
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
                store.close()
                store = SqlStore(loc)         # a real close/reopen cycle
                self._assert_preserved(store, label)
            finally:
                if store.backend == "postgres":
                    store.reset_schema()
                store.close()


def _seed_owned_arena(api):
    """Organization → Program (operated by it) → Season + League + two Venues
    (one owned by the org, one owned by the program). Returns ids."""
    org = api.setup.create_organization("Org").id
    pid = api.create_program("Prog", "US", "UTC",
                             operator_organization_id=org)["id"]
    sid = api.create_season(pid, "S1")["id"]
    lid = api.create_league(sid, "Gold")["id"]
    v_org = api.setup.create_venue("OrgVenue", organization_id=org).id
    v_prog = api.setup.create_venue("ProgVenue", league_id=pid).id
    return {"org": org, "program": pid, "season": sid, "league": lid,
            "v_org": v_org, "v_prog": v_prog}


class ServiceParityTest(unittest.TestCase):
    def _backends(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            SqlStore(url).clear_all_data()
            yield "postgres", SqlStore(url)

    def test_create_onto_missing_parent_rejected(self):
        # The facade returns a structured error (not an exception across the
        # boundary) when a create names a parent that does not exist.
        for label, store in self._backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                self.assertIn("error",
                              api.create_program("P", "US", "UTC",
                                                 operator_organization_id="ghost"))
                self.assertIn("error", api.create_season("ghost_prog", "S"))
                self.assertIn("error", api.create_venue("V", organization_id="ghost"))
                self.assertIn("error", api.create_venue("V2", league_id="ghost"))
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
                ctx = _seed_owned_arena(api)
                with self.assertRaises(HasDependenciesError, msg=label) as cm:
                    api.setup.delete_organization(ctx["org"])
                self._assert_block_group(cm, "organization", ctx["org"],
                                         "league", 1, label)  # program group
                with self.assertRaises(HasDependenciesError, msg=label) as cm:
                    api.setup.delete_program(ctx["program"])
                self._assert_block_group(cm, "league", ctx["program"],
                                         "season", 1, label)
                self._assert_block_group(cm, "league", ctx["program"],
                                         "level", 1, label)  # the League
                self.assertIsNotNone(store.get_organization(ctx["org"]), label)
                self.assertIsNotNone(store.get_program(ctx["program"]), label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_delete_inside_outer_transaction_blocks_and_rolls_back(self):
        # Nested-transaction parity: the delete nested in a caller's
        # transaction() still raises the itemised error, rolls the whole unit
        # back (a sentinel write before it is undone), and leaves the store
        # usable — never InFailedSqlTransaction.
        for label, store in self._backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                ctx = _seed_owned_arena(api)
                for entity_type, entity_id, group_type, delete_call in (
                        ("organization", ctx["org"], "league",
                         lambda: api.setup.delete_organization(ctx["org"])),
                        ("league", ctx["program"], "season",
                         lambda: api.setup.delete_program(ctx["program"]))):
                    sentinel = f"SENTINEL-{entity_id}"
                    with self.assertRaises(HasDependenciesError, msg=label) as cm:
                        with store.transaction():
                            store.add_organization(
                                Organization(id=sentinel, name=sentinel))
                            delete_call()
                    self._assert_block_group(cm, entity_type, entity_id,
                                             group_type, 1, label)
                    self.assertIsNone(store.get_organization(sentinel),
                                      f"{label}: sentinel survived rollback")
                self.assertIsNotNone(store.get_organization(ctx["org"]), label)
                self.assertIsNotNone(store.get_program(ctx["program"]), label)
                if isinstance(store, SqlStore):
                    store.close()


# =====================================================================
# PostgreSQL forced races (real row locks + the DB foreign key)
# =====================================================================
def _audit_actions(store, action):
    return sum(1 for a in store.all_setup_audit() if a.action == action)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class ProgramOrgRaceTest(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    # -- runners (identical to the Slice 3 harness) -----------------------
    def _record(self, results, key, fn):
        try:
            fn(); results[key] = "ok"
        except HasDependenciesError as exc:
            results[key] = "blocked"
            results[key + "_details"] = exc.details
        except NotFoundError:
            results[key] = "not_found"
        except DomainError as exc:
            results[key] = exc.details.get("reason") or exc.code
        except Exception as exc:
            results[key] = f"ERR:{exc}"

    def _reference_block_details(self, delete_call):
        try:
            delete_call()
        except HasDependenciesError as exc:
            return exc.details
        raise AssertionError("expected the reference delete to be blocked")

    def _forced_delete_wins(self, victim_store, locator_method, victim_fn,
                            racer_fn):
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

        def run_racer():
            if not paused.wait(15):
                results["delete"] = "ERR:victim-never-paused"
                return
            try:
                self._record(results, "delete", racer_fn)
            finally:
                resume.set()

        tv = threading.Thread(target=lambda: self._record(results, "child", victim_fn))
        trc = threading.Thread(target=run_racer)
        tv.start(); trc.start()
        trc.join(25); resume.set(); tv.join(25)
        return results

    def _forced_child_wins(self, child_store, write_method, child_fn,
                           delete_store, delete_fn):
        orig = getattr(child_store, write_method)
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

        setattr(child_store, write_method, instrumented)
        results = {}
        tc = threading.Thread(target=lambda: self._record(results, "child", child_fn))
        td = threading.Thread(target=lambda: self._record(results, "delete", delete_fn))
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

    @staticmethod
    def _in_txn(store, *thunks):
        def run():
            with store.transaction():
                for thunk in thunks:
                    thunk()
        return run

    def _assert_itemised(self, details, entity_type, entity_id, group_type, count):
        self.assertEqual(details.get("entity_type"), entity_type)
        self.assertEqual(details.get("entity_id"), entity_id)
        groups = {g["type"]: g for g in details.get("dependencies", [])}
        self.assertIn(group_type, groups)
        self.assertEqual(groups[group_type]["count"], count)
        self.assertEqual(len(groups[group_type]["items"]), count)

    # -- the four raceable families ---------------------------------------
    # (create_league vs delete_program is store-boundary only: create_league
    # needs a Season, whose presence blocks delete_program — no service race.)
    def _families(self):
        def seed_org():
            api0 = ApiService(SqlStore(self.url))
            return api0.setup.create_organization("Org").id

        def seed_prog():
            api0 = ApiService(SqlStore(self.url))
            oid = api0.setup.create_organization("Org").id
            return api0.create_program("Prog", "US", "UTC",
                                       operator_organization_id=oid)["id"]

        return {
            "program_vs_org": dict(
                parent=seed_org, write="add_program", locator="get_organization",
                child=lambda api, pid: (lambda: api.setup.create_program(
                    "New", "US", "UTC", operator_organization_id=pid)),
                delete=lambda api, pid: (lambda: api.setup.delete_organization(
                    pid, actor_id="d")),
                child_lost="organization_not_found", delete_audit="organization_deleted",
                entity_type="organization", group="league", delete_kind="delete_organization"),
            "venue_org_vs_org": dict(
                parent=seed_org, write="add_venue", locator="get_organization",
                child=lambda api, pid: (lambda: api.setup.create_venue(
                    "V", organization_id=pid, actor_id="c")),
                delete=lambda api, pid: (lambda: api.setup.delete_organization(
                    pid, actor_id="d")),
                child_lost="organization_not_found", delete_audit="organization_deleted",
                entity_type="organization", group="venue", delete_kind="delete_organization"),
            "season_vs_program": dict(
                parent=seed_prog, write="add_season", locator="get_program",
                child=lambda api, pid: (lambda: api.setup.create_season(pid, "S")),
                delete=lambda api, pid: (lambda: api.setup.delete_program(
                    pid, actor_id="d")),
                child_lost="program_not_found", delete_audit="league_deleted",
                entity_type="league", group="season", delete_kind="delete_program"),
            "venue_prog_vs_program": dict(
                parent=seed_prog, write="add_venue", locator="get_program",
                child=lambda api, pid: (lambda: api.setup.create_venue(
                    "V", league_id=pid, actor_id="c")),
                delete=lambda api, pid: (lambda: api.setup.delete_program(
                    pid, actor_id="d")),
                child_lost="program_not_found", delete_audit="league_deleted",
                entity_type="league", group="venue", delete_kind="delete_program"),
        }

    def test_child_wins_preserves_itemised_contract(self):
        for name, fam in self._families().items():
            with self.subTest(family=name):
                SqlStore(self.url).clear_all_data()
                parent = fam["parent"]()
                child_store = SqlStore(self.url)
                api_c = ApiService(child_store)
                delete_store = SqlStore(self.url)
                api_d = ApiService(delete_store)
                results = self._forced_child_wins(
                    child_store, fam["write"],
                    child_fn=fam["child"](api_c, parent),
                    delete_store=delete_store,
                    delete_fn=fam["delete"](api_d, parent))
                self.assertEqual(results["child"], "ok", results)
                self.assertEqual(results["delete"], "blocked", results)
                reference = self._reference_block_details(
                    fam["delete"](ApiService(SqlStore(self.url)), parent))
                self.assertEqual(results["delete_details"], reference, results)
                self._assert_itemised(results["delete_details"],
                                      fam["entity_type"], parent, fam["group"], 1)
                check = SqlStore(self.url)
                self.assertEqual(_audit_actions(check, fam["delete_audit"]), 0,
                                 results)

    def test_child_wins_nested_transaction_full_rollback(self):
        for name, fam in self._families().items():
            with self.subTest(family=name):
                SqlStore(self.url).clear_all_data()
                parent = fam["parent"]()
                child_store = SqlStore(self.url)
                api_c = ApiService(child_store)
                delete_store = SqlStore(self.url)
                api_d = ApiService(delete_store)
                sentinel = f"venue_sentinel_{name}"
                results = self._forced_child_wins(
                    child_store, fam["write"],
                    child_fn=fam["child"](api_c, parent),
                    delete_store=delete_store,
                    delete_fn=self._in_txn(
                        delete_store,
                        lambda: delete_store.add_venue(
                            Venue(id=sentinel, name=sentinel)),
                        fam["delete"](api_d, parent)))
                self.assertEqual(results["child"], "ok", results)
                self.assertEqual(results["delete"], "blocked", results)
                self._assert_itemised(results["delete_details"],
                                      fam["entity_type"], parent, fam["group"], 1)
                check = SqlStore(self.url)
                # The whole outer unit rolled back: sentinel gone, no delete audit.
                self.assertIsNone(check.get_venue(sentinel), results)
                self.assertEqual(_audit_actions(check, fam["delete_audit"]), 0,
                                 results)

    def test_delete_wins_child_loses_stably(self):
        for name, fam in self._families().items():
            with self.subTest(family=name):
                SqlStore(self.url).clear_all_data()
                parent = fam["parent"]()
                victim_store = SqlStore(self.url)
                api_v = ApiService(victim_store)
                api_r = ApiService(SqlStore(self.url))
                results = self._forced_delete_wins(
                    victim_store, fam["locator"],
                    victim_fn=fam["child"](api_v, parent),
                    racer_fn=fam["delete"](api_r, parent))
                self.assertEqual(results.get("delete"), "ok", results)
                self.assertEqual(results.get("child"), fam["child_lost"], results)
                check = SqlStore(self.url)
                # Parent gone, no orphan child committed.
                if fam["entity_type"] == "organization":
                    self.assertIsNone(check.get_organization(parent), results)
                else:
                    self.assertIsNone(check.get_program(parent), results)

    def test_barrier_never_orphans(self):
        for name, fam in self._families().items():
            with self.subTest(family=name):
                SqlStore(self.url).clear_all_data()
                parent = fam["parent"]()
                api_c = ApiService(SqlStore(self.url))
                api_d = ApiService(SqlStore(self.url))
                results = self._barrier(
                    child_fn=fam["child"](api_c, parent),
                    delete_fn=fam["delete"](api_d, parent))
                check = SqlStore(self.url)
                # Whatever the interleaving: never a raw driver error, never a
                # committed orphan (parent present ⟺ child may exist).
                self.assertNotIn("ERR", str(results.get("child")), results)
                self.assertNotIn("ERR", str(results.get("delete")), results)
                parent_alive = (check.get_organization(parent) is not None
                                if fam["entity_type"] == "organization"
                                else check.get_program(parent) is not None)
                if results.get("delete") == "ok":
                    self.assertFalse(parent_alive, results)
                    self.assertIn(results.get("child"),
                                  (fam["child_lost"], "not_found"), results)


if __name__ == "__main__":
    unittest.main()
