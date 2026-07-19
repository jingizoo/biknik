"""Jersey-number range + active-team uniqueness (#269).

Two invariants, one source of truth (``domain.jersey``):

* range — an integer 1..98 inclusive, or ``None``; a bool/non-integer/out-of-
  range value is a field-level ``validation_error``;
* active-team uniqueness — at most one ACTIVE player per (team, jersey), a
  service-layer check backed by a partial DB unique index (migration 038), so a
  cross-process race can't bypass it. The same jersey is free on another team
  and reusable once the prior holder is inactive.

Covers the pure rule, every write path (manual create, reassignment, import
upsert), the HTTP envelope, both import previews, and the migration
preflight/constraint/race on SQLite + PostgreSQL.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import timedelta, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Division, LeagueSeason, Player, Position, Team)
from hockey_scheduler.domain.errors import (
    IntegrityConflictError, ValidationError)
from hockey_scheduler.domain.jersey import (
    MAX_JERSEY_NUMBER, MIN_JERSEY_NUMBER, jersey_number_error, parse_jersey_cell)
from hockey_scheduler.services.hierarchy_import import (
    commit_hierarchy_import, validate_hierarchy_import)
from hockey_scheduler.services.import_validator import validate_import
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.store.integrity_checks import (
    MigrationDataError,
    assert_no_duplicate_active_team_jerseys,
    assert_player_jersey_constraints_ready,
    find_duplicate_active_team_jerseys,
    find_invalid_player_jersey_numbers,
)
from hockey_scheduler.store.sql_store import migrate
from hockey_scheduler.web import server as srv

UTC = timezone.utc
_VERSION = "038_active_team_jersey_unique"
_INDEX = "ux_players_active_team_jersey"


# --------------------------------------------------------------------------- #
# Pure range rule                                                             #
# --------------------------------------------------------------------------- #
class JerseyNumberRuleTest(unittest.TestCase):
    def test_none_is_valid(self):
        self.assertIsNone(jersey_number_error(None))

    def test_boundaries_1_and_98_are_valid(self):
        self.assertIsNone(jersey_number_error(MIN_JERSEY_NUMBER))
        self.assertIsNone(jersey_number_error(MAX_JERSEY_NUMBER))
        self.assertEqual((MIN_JERSEY_NUMBER, MAX_JERSEY_NUMBER), (1, 98))

    def test_below_min_and_above_max_are_out_of_range(self):
        self.assertEqual(jersey_number_error(0), "out_of_range")
        self.assertEqual(jersey_number_error(99), "out_of_range")
        self.assertEqual(jersey_number_error(-1), "out_of_range")

    def test_bool_is_rejected_even_though_bool_is_an_int(self):
        self.assertEqual(jersey_number_error(True), "not_an_integer")
        self.assertEqual(jersey_number_error(False), "not_an_integer")

    def test_non_integers_are_rejected(self):
        self.assertEqual(jersey_number_error(3.5), "not_an_integer")
        self.assertEqual(jersey_number_error("7"), "not_an_integer")

    def test_parse_cell_blank_is_absent(self):
        self.assertEqual(parse_jersey_cell(""), (None, None))
        self.assertEqual(parse_jersey_cell("  "), (None, None))
        self.assertEqual(parse_jersey_cell(None), (None, None))

    def test_parse_cell_range_and_format(self):
        self.assertEqual(parse_jersey_cell("1"), (1, None))
        self.assertEqual(parse_jersey_cell("98"), (98, None))
        self.assertEqual(parse_jersey_cell("0"), (0, "out_of_range"))
        self.assertEqual(parse_jersey_cell("99"), (99, "out_of_range"))
        self.assertEqual(parse_jersey_cell("x"), (None, "not_an_integer"))
        self.assertEqual(parse_jersey_cell("3.5"), (None, "not_an_integer"))


# --------------------------------------------------------------------------- #
# Service write paths, on every backend                                       #
# --------------------------------------------------------------------------- #
def _service_backends():
    """(label, store) pairs: InMemory, SQLite, and PostgreSQL when configured."""
    stores = [("memory", InMemoryStore()), ("sqlite", SqlStore(":memory:"))]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        pg = SqlStore(url)
        pg.reset_schema()
        stores.append(("postgres", pg))
    return stores


def _seed_two_teams(store):
    store.add_league_season(LeagueSeason(id="ls", league_id="l", season_id="se"))
    store.add_division(Division(id="d", league_season_id="ls", name="D1"))
    store.add_team(Team(id="home", name="Home", division="D1", division_id="d"))
    store.add_team(Team(id="away", name="Away", division="D1", division_id="d"))
    return ApiService(store)


class ServiceJerseyTest(unittest.TestCase):
    """The write paths (create, reassign, import upsert) enforce both rules on
    Memory, SQLite, and PostgreSQL identically."""

    def _each(self):
        for label, store in _service_backends():
            with self.subTest(backend=label):
                api = _seed_two_teams(store)
                try:
                    yield label, store, api, api.setup
                finally:
                    if isinstance(store, SqlStore):
                        store.close()

    # -- range --------------------------------------------------------------
    def test_create_accepts_boundaries_1_and_98(self):
        for label, store, api, setup in self._each():
            p1 = setup.add_player("home", "One", Position.FORWARD, jersey_number=1)
            p98 = setup.add_player("away", "Nn", Position.FORWARD, jersey_number=98)
            self.assertEqual(p1.jersey_number, 1, label)
            self.assertEqual(p98.jersey_number, 98, label)

    def test_create_accepts_none(self):
        for label, store, api, setup in self._each():
            p = setup.add_player("home", "NoNum", Position.GOALIE)
            self.assertIsNone(p.jersey_number, label)

    def test_create_rejects_zero_and_99_as_field_error(self):
        for label, store, api, setup in self._each():
            for bad in (0, 99):
                with self.assertRaises(ValidationError, msg=label) as ctx:
                    setup.add_player("home", "Bad", Position.FORWARD,
                                     jersey_number=bad)
                self.assertEqual(ctx.exception.details.get("reason"),
                                 "invalid_jersey_number", label)
                self.assertEqual(ctx.exception.details.get("field"),
                                 "jersey_number", label)
            self.assertEqual(store.players_for_team("home"), [], label)

    def test_create_rejects_bool_and_non_integer(self):
        for label, store, api, setup in self._each():
            for bad in (True, 3.5):
                with self.assertRaises(ValidationError, msg=label):
                    setup.add_player("home", "Bad", Position.FORWARD,
                                     jersey_number=bad)

    # -- active-team uniqueness --------------------------------------------
    def test_duplicate_active_jersey_same_team_is_rejected_zero_writes(self):
        for label, store, api, setup in self._each():
            setup.add_player("home", "First", Position.FORWARD, jersey_number=7)
            with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                setup.add_player("home", "Second", Position.FORWARD,
                                 jersey_number=7)
            self.assertEqual(ctx.exception.details.get("reason"),
                             "duplicate_jersey_number", label)
            self.assertEqual(ctx.exception.details.get("jersey_number"), 7, label)
            self.assertIn("team_id", ctx.exception.details, label)
            # Only the first player was ever written.
            sevens = [p for p in store.players_for_team("home")
                      if p.jersey_number == 7]
            self.assertEqual(len(sevens), 1, label)

    def test_same_jersey_on_different_teams_is_allowed(self):
        for label, store, api, setup in self._each():
            setup.add_player("home", "H7", Position.FORWARD, jersey_number=7)
            other = setup.add_player("away", "A7", Position.FORWARD,
                                     jersey_number=7)
            self.assertEqual(other.jersey_number, 7, label)

    def test_jersey_reused_after_prior_player_inactive(self):
        for label, store, api, setup in self._each():
            # An inactive player parks #9 without reserving it; a new active
            # player may take it, and a second active one may not.
            setup.add_player("home", "Retired", Position.FORWARD,
                             jersey_number=9, is_active=False)
            active = setup.add_player("home", "Rookie", Position.FORWARD,
                                      jersey_number=9)
            self.assertEqual(active.jersey_number, 9, label)
            with self.assertRaises(IntegrityConflictError, msg=label):
                setup.add_player("home", "TooLate", Position.FORWARD,
                                 jersey_number=9)

    def test_multiple_players_without_a_jersey_are_allowed(self):
        for label, store, api, setup in self._each():
            setup.add_player("home", "A", Position.FORWARD)
            setup.add_player("home", "B", Position.FORWARD)
            self.assertEqual(len(store.players_for_team("home")), 2, label)

    # -- reassignment -------------------------------------------------------
    def test_reassignment_collision_rejected_zero_mutation(self):
        for label, store, api, setup in self._each():
            keep = setup.add_player("home", "Home11", Position.FORWARD,
                                    jersey_number=11)
            mover = setup.add_player("away", "Away11", Position.FORWARD,
                                     jersey_number=11)
            with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                setup.assign_player_team(mover.id, "home")
            self.assertEqual(ctx.exception.details.get("reason"),
                             "duplicate_jersey_number", label)
            # The mover never left its team; no audit/mutation happened.
            self.assertEqual(store.get_player(mover.id).team_id, "away", label)
            self.assertEqual(store.get_player(keep.id).team_id, "home", label)

    def test_reassignment_to_team_with_free_number_succeeds(self):
        for label, store, api, setup in self._each():
            mover = setup.add_player("away", "Away12", Position.FORWARD,
                                     jersey_number=12)
            moved = setup.assign_player_team(mover.id, "home")
            self.assertEqual(moved.team_id, "home", label)

    def test_reassignment_of_numberless_player_is_unconstrained(self):
        for label, store, api, setup in self._each():
            setup.add_player("home", "Home5", Position.FORWARD, jersey_number=5)
            mover = setup.add_player("away", "AwayNone", Position.GOALIE)
            moved = setup.assign_player_team(mover.id, "home")
            self.assertEqual(moved.team_id, "home", label)

    # -- import upsert hook -------------------------------------------------
    def test_import_upsert_rejects_duplicate_active_jersey(self):
        for label, store, api, setup in self._each():
            setup.upsert_imported_player("P1", "First", "home",
                                         Position.FORWARD, 21, None)
            with self.assertRaises(IntegrityConflictError, msg=label):
                setup.upsert_imported_player("P2", "Second", "home",
                                             Position.FORWARD, 21, None)

    def test_import_upsert_reimport_same_row_is_not_a_self_conflict(self):
        for label, store, api, setup in self._each():
            setup.upsert_imported_player("P1", "First", "home",
                                         Position.FORWARD, 21, None,
                                         existing=None)
            existing = next(p for p in store.all_players()
                            if p.external_ref == "P1")
            # Re-importing the same code keeps #21 — it must not collide with
            # itself.
            obj, created, _ = setup.upsert_imported_player(
                "P1", "First Renamed", "home", Position.FORWARD, 21, None,
                existing=existing)
            self.assertFalse(created, label)
            self.assertEqual(obj.jersey_number, 21, label)


# --------------------------------------------------------------------------- #
# HTTP envelope                                                               #
# --------------------------------------------------------------------------- #
class HttpJerseyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _admin(self):
        c = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        self._req(c, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        self._req(c, "POST", "/api/demo/load", {})
        return c

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _a_team_id(self, c):
        _s, teams = self._req(c, "GET", "/api/v2/setup/overview")
        return teams["teams"][0]["id"]

    def test_out_of_range_create_is_400(self):
        c = self._admin()
        team = self._a_team_id(c)
        status, body = self._req(c, "POST", "/api/setup/player", {
            "team_id": team, "name": "Ranger", "position": "forward",
            "jersey_number": 131})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertEqual(body["error"]["details"]["reason"],
                         "invalid_jersey_number")

    def test_duplicate_active_jersey_create_is_409(self):
        c = self._admin()
        team = self._a_team_id(c)
        s1, _ = self._req(c, "POST", "/api/setup/player", {
            "team_id": team, "name": "One", "position": "forward",
            "jersey_number": 77})
        self.assertEqual(s1, 200)
        s2, body = self._req(c, "POST", "/api/setup/player", {
            "team_id": team, "name": "Two", "position": "forward",
            "jersey_number": 77})
        self.assertEqual(s2, 409)
        self.assertEqual(body["error"]["code"], "conflict")
        self.assertEqual(body["error"]["details"]["reason"],
                         "duplicate_jersey_number")


# --------------------------------------------------------------------------- #
# Import previews report conflicts; a failed preview commits nothing          #
# --------------------------------------------------------------------------- #
class LegacyImportPreviewTest(unittest.TestCase):
    def _players(self, rows, store=None):
        return validate_import({"teams": [{"team_code": "T1", "team_name": "T"}],
                                "players": rows}, store=store)

    def test_out_of_range_jersey_is_reported(self):
        r = self._players([{"player_code": "P1", "first_name": "A",
                            "last_name": "B", "team_code": "T1",
                            "jersey_number": "131"}])
        self.assertFalse(r["ok"])
        self.assertTrue(any(e["field"] == "jersey_number" for e in r["errors"]))

    def test_duplicate_jersey_same_team_within_upload_is_reported(self):
        r = self._players([
            {"player_code": "P1", "first_name": "A", "last_name": "B",
             "team_code": "T1", "jersey_number": "9"},
            {"player_code": "P2", "first_name": "C", "last_name": "D",
             "team_code": "T1", "jersey_number": "9"},
            {"player_code": "P3", "first_name": "E", "last_name": "F",
             "team_code": "T1", "jersey_number": "9"}])
        self.assertFalse(r["ok"])
        dupes = [e for e in r["errors"]
                 if e["field"] == "jersey_number" and "Duplicate" in e["message"]]
        self.assertEqual({e["row"] for e in dupes}, {1, 2, 3})

    def test_existing_active_player_collision_is_reported(self):
        store = InMemoryStore()
        store.add_team(Team(id="team_t1", name="T", external_ref="T1"))
        store.add_player(Player(
            id="existing", team_id="team_t1", name="Existing",
            position=Position.FORWARD, jersey_number=9, external_ref="PX"))
        # Exercise the live legacy preview facade, not only the validator, so a
        # regression that stops passing the store cannot silently return.
        result = ApiService(store).get_import_dry_run({
            "teams_csv": "team_code,team_name\nT1,T\n",
            "players_csv": (
                "player_code,first_name,last_name,team_code,jersey_number\n"
                "P1,A,B,T1,9\n")})
        conflicts = [e for e in result["errors"]
                     if e.get("field") == "jersey_number"]
        self.assertEqual([e["row"] for e in conflicts], [1])
        self.assertIn("Existing", conflicts[0]["message"])
        self.assertIn("existing", conflicts[0]["message"])

    def test_same_jersey_different_teams_within_upload_is_ok(self):
        r = validate_import({
            "teams": [{"team_code": "T1", "team_name": "One"},
                      {"team_code": "T2", "team_name": "Two"}],
            "players": [
                {"player_code": "P1", "first_name": "A", "last_name": "B",
                 "team_code": "T1", "jersey_number": "9"},
                {"player_code": "P2", "first_name": "C", "last_name": "D",
                 "team_code": "T2", "jersey_number": "9"}]})
        self.assertTrue(r["ok"], r["errors"])


def _hier_sheets(players, teams=None):
    return {
        "organizations": [{"organization_code": "O1", "organization_name": "Org"}],
        "programs": [{"program_code": "PR1", "program_name": "Prog",
                     "organization_code": "O1"}],
        "venues_rinks": [],
        "competition": [{"program_code": "PR1", "season_code": "S1",
                        "season_name": "Winter", "league_code": "L1",
                        "league_name": "Lg"}],
        "clubs": [],
        "permanent_teams": teams if teams is not None else [
            {"team_code": "T1", "team_name": "Lions", "program_code": "PR1",
             "league_code": "L1"}],
        "players": players,
        "registrations": [],
        "season_venue_access": [],
    }


class HierarchyImportPreviewTest(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()

    def _prow(self, code, jersey, team="T1"):
        return {"player_code": code, "first_name": code, "last_name": "X",
                "team_code": team, "jersey_number": str(jersey),
                "position": "forward"}

    def test_within_upload_duplicate_is_reported_with_context(self):
        sheets = _hier_sheets([
            self._prow("P1", 9), self._prow("P2", 9), self._prow("P3", 9)])
        result = validate_hierarchy_import(sheets, self.store)
        self.assertFalse(result["ok"])
        dupes = [e for e in result["errors"]
                 if e.get("code") == "duplicate_jersey_number"]
        self.assertEqual({e["row"] for e in dupes}, {1, 2, 3})
        self.assertIn("T1", dupes[0]["message"])
        self.assertIn("9", dupes[0]["message"])

    def test_out_of_range_reported(self):
        sheets = _hier_sheets([self._prow("P1", 131)])
        result = validate_hierarchy_import(sheets, self.store)
        self.assertFalse(result["ok"])
        self.assertTrue(any(e.get("code") == "invalid_jersey_number"
                            for e in result["errors"]))

    def test_conflict_against_existing_active_player_reported(self):
        # An existing active player on T1 wears #5; importing another #5 onto
        # the same (existing) team is a conflict.
        self.store.add_team(Team(id="team_t1", name="Lions"))
        # Give the team the external_ref the upload references so it resolves.
        team = self.store.get_team("team_t1")
        team.external_ref = "T1"
        self.store.save_team(team)
        self.store.add_player(Player(id="ex1", team_id="team_t1", name="Vet",
                                     position=Position.FORWARD, jersey_number=5))
        sheets = _hier_sheets(
            [self._prow("P1", 5)],
            teams=[])  # no new team; player references the existing T1
        result = validate_hierarchy_import(sheets, self.store)
        self.assertFalse(result["ok"])
        self.assertTrue(any(e.get("code") == "duplicate_jersey_number"
                            for e in result["errors"]))

    def test_failed_preview_commits_zero_rows(self):
        api = ApiService(self.store)
        sheets = _hier_sheets([self._prow("P1", 9), self._prow("P2", 9)])
        result = commit_hierarchy_import(api.setup, sheets)
        self.assertFalse(result["committed"])
        self.assertEqual(self.store.all_players(), [])


# --------------------------------------------------------------------------- #
# Migration preflight, constraint, restart, race                             #
# --------------------------------------------------------------------------- #
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


def _downgrade_038(store):
    """Simulate a pre-038 database: drop the unique index and un-record it."""
    with store.transaction():
        cur = store.conn.cursor()
        cur.execute(f"DROP INDEX IF EXISTS {_INDEX}")
        cur.execute(store.dialect.sql(
            "DELETE FROM schema_migrations WHERE version = ?"), (_VERSION,))


def _player(pid, team, jersey, active=True):
    return Player(id=pid, team_id=team, name=pid, position=Position.FORWARD,
                  jersey_number=jersey, is_active=active)


def _index_present(store):
    cur = store.conn.cursor()
    if store.backend == "sqlite":
        cur.execute("PRAGMA index_list('players')")
        return any(row["name"] == _INDEX and row["unique"] == 1
                   and row["partial"] == 1 for row in cur.fetchall())
    cur.execute("SELECT indexname, indexdef FROM pg_indexes "
                "WHERE tablename = 'players'")
    return any(row["indexname"] == _INDEX
               and "UNIQUE" in row["indexdef"].upper()
               and "WHERE" in row["indexdef"].upper()
               for row in cur.fetchall())


class JerseyPreMigrationTest(unittest.TestCase):
    def _cleanup(self, store):
        if store.backend != "sqlite":
            store.reset_schema()
        store.close()

    def test_existing_active_duplicates_detected_and_migrate_aborts(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            _downgrade_038(store)
            try:
                with store.transaction():
                    store.add_player(_player("a", "t1", 7))
                    store.add_player(_player("b", "t1", 7))   # dup active
                    store.add_player(_player("c", "t1", 8))
                self.assertEqual(find_duplicate_active_team_jerseys(store.conn),
                                 [("t1", 7)], label)
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    assert_no_duplicate_active_team_jerseys(store.conn)
                self.assertIn("team t1/#7", str(ctx.exception))
                with self.assertRaises(MigrationDataError, msg=label):
                    migrate(store.conn, store.dialect)  # aborts on dirty data
            finally:
                self._cleanup(store)

    def test_inactive_and_null_duplicates_are_not_flagged(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            _downgrade_038(store)
            try:
                with store.transaction():
                    # two inactive #7, and two active NULL — neither reserves.
                    store.add_player(_player("a", "t1", 7, active=False))
                    store.add_player(_player("b", "t1", 7, active=False))
                    store.add_player(_player("c", "t1", None))
                    store.add_player(_player("d", "t1", None))
                self.assertEqual(find_duplicate_active_team_jerseys(store.conn),
                                 [], label)
                assert_no_duplicate_active_team_jerseys(store.conn)  # no raise
                migrate(store.conn, store.dialect)  # succeeds; index created
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
            finally:
                self._cleanup(store)

    def test_out_of_range_existing_rows_abort_with_player_context(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            _downgrade_038(store)
            try:
                with store.transaction():
                    store.add_player(_player("bad_0", "t1", 0))
                    store.add_player(_player("bad_99", "t1", 99,
                                             active=False))
                    store.add_player(_player("bad_131", "t2", 131))
                    store.add_player(_player("valid_1", "t1", 1))
                    store.add_player(_player("valid_98", "t1", 98))
                    store.add_player(_player("inactive_valid", "t1", 7,
                                             active=False))
                    store.add_player(_player("null_ok", "t1", None))
                invalid = find_invalid_player_jersey_numbers(store.conn)
                self.assertEqual(
                    [(row[0], row[2]) for row in invalid],
                    [("bad_0", 0), ("bad_131", 131), ("bad_99", 99)], label)
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    assert_player_jersey_constraints_ready(store.conn)
                message = str(ctx.exception)
                for token in ("bad_0", "#0", "bad_99", "#99",
                              "bad_131", "#131"):
                    self.assertIn(token, message, label)
                with self.assertRaises(MigrationDataError, msg=label):
                    migrate(store.conn, store.dialect)
                self.assertNotIn(_VERSION, store.migration_status()["applied"])
            finally:
                self._cleanup(store)

    def test_clean_upgrade_applies(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            _downgrade_038(store)
            try:
                with store.transaction():
                    store.add_player(_player("a", "t1", 7))
                    store.add_player(_player("b", "t1", 8))
                    store.add_player(_player("c", "t2", 7))  # other team ok
                migrate(store.conn, store.dialect)
                self.assertTrue(_index_present(store), label)
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
            finally:
                self._cleanup(store)


class JerseyConstraintEnforcementTest(unittest.TestCase):
    def test_second_active_same_team_jersey_is_translated_conflict(self):
        # The DB-enforced path carries the SAME conflict contract the service
        # pre-check does, now including the conflicting player (#292): a lost
        # race must not return thinner detail than the pre-check.
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                with store.transaction():
                    store.add_player(_player("a", "t1", 7))
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_player(_player("b", "t1", 7))
                d = ctx.exception.details
                self.assertEqual(d["reason"], "duplicate_jersey_number", label)
                self.assertEqual(d["team_id"], "t1", label)
                self.assertEqual(d["jersey_number"], 7, label)
                # Enriched post-rollback with the winning holder (id + name),
                # never any contact/private field.
                self.assertEqual(d["conflicting_player_id"], "a", label)
                self.assertEqual(d["conflicting_player_name"], "a", label)
                self.assertNotIn("email", d, label)
            finally:
                store.close()

    def test_same_number_on_other_team_and_inactive_and_null_are_allowed(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                with store.transaction():
                    store.add_player(_player("a", "t1", 7))
                    store.add_player(_player("b", "t2", 7))         # other team
                    store.add_player(_player("c", "t1", 7, active=False))  # inactive
                    store.add_player(_player("d", "t1", None))      # null
                    store.add_player(_player("e", "t1", None))      # null again
                cur = store.conn.cursor()
                cur.execute("SELECT COUNT(*) AS n FROM players")
                self.assertEqual(cur.fetchone()["n"], 5, label)
            finally:
                store.close()

    def test_conflict_makes_zero_writes(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                with store.transaction():
                    store.add_player(_player("a", "t1", 7))
                with self.assertRaises(IntegrityConflictError, msg=label):
                    with store.transaction():
                        store.add_player(_player("ok", "t1", 8))   # fine
                        store.add_player(_player("b", "t1", 7))    # violates
                self.assertIsNone(store.get_player("ok"), label)   # rolled back
            finally:
                store.close()


class JerseyMigrationLedgerTest(unittest.TestCase):
    def test_recorded_and_enforced_after_reopen(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            first = SqlStore(path)
            self.assertIn(_VERSION, first.migration_status()["applied"])
            self.assertTrue(_index_present(first))
            with first.transaction():
                first.add_player(_player("a", "t1", 7))
            first.close()
            second = SqlStore(path)
            self.assertTrue(second.migration_status()["current"])
            self.assertTrue(_index_present(second))
            with self.assertRaises(IntegrityConflictError):
                with second.transaction():
                    second.add_player(_player("b", "t1", 7))  # still enforced
            second.close()
        finally:
            os.remove(path)


class JerseyRaceTest(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "cross-connection race needs PostgreSQL")
    def test_only_one_of_two_racing_active_creates_wins(self):
        url = os.environ["TEST_DATABASE_URL"]
        seed = SqlStore(url)
        seed.reset_schema()
        seed.add_team(Team(id="race_t", name="Race"))
        seed.close()
        barrier = threading.Barrier(2)
        results = {}

        def attempt(pid):
            store = SqlStore(url)
            api = ApiService(store)
            try:
                # Force both service checks to observe the number as free before
                # either insert begins; the DB index must decide the race.
                original = api.setup._assert_jersey_available

                def checked_then_wait(*args, **kwargs):
                    original(*args, **kwargs)
                    barrier.wait(timeout=5)

                api.setup._assert_jersey_available = checked_then_wait
                res = api.create_player("race_t", pid, "forward",
                                        jersey_number=7)
                results[pid] = res
            except Exception as exc:  # pragma: no cover
                results[pid] = f"error:{exc!r}"
            finally:
                store.close()

        threads = [threading.Thread(target=attempt, args=(p,))
                   for p in ("p_a", "p_b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(results), 2, results)
        winners = [res for res in results.values() if "error" not in res]
        losers = [res for res in results.values() if "error" in res]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(losers), 1, results)
        self.assertEqual(losers[0]["error"]["code"], "conflict")
        # The lost race must carry the SAME context the service pre-check does,
        # including the winning holder (#292) — enriched post-rollback, never
        # any contact/private field.
        d = losers[0]["error"]["details"]
        self.assertEqual(d["reason"], "duplicate_jersey_number")
        self.assertEqual(d["team_id"], "race_t")
        self.assertEqual(d["jersey_number"], 7)
        winner_id = [res for res in results.values()
                     if "error" not in res][0]["id"]
        self.assertEqual(d["conflicting_player_id"], winner_id)
        self.assertIn("conflicting_player_name", d)
        self.assertNotIn("email", d)
        checker = SqlStore(url)
        try:
            cur = checker.conn.cursor()
            cur.execute(checker.dialect.sql(
                "SELECT COUNT(*) AS n FROM players "
                "WHERE team_id = ? AND jersey_number = ? AND is_active = 1"),
                ("race_t", 7))
            self.assertEqual(cur.fetchone()["n"], 1)
        finally:
            checker.close()


if __name__ == "__main__":
    unittest.main()
