"""Per-user active Program/Season context — the selection backend (#159).

A VIEW selection only: on every resolve/set it is filtered through the caller's
REAL role + account scope (the same #211/#266/#202 rules the rest of the app
enforces), so a scoped account can neither select nor enumerate a Program/Season
outside its scope. It never grants authority. Supports Program-only selection;
honors an archived Season as a read-only historical context; and a deleted or
no-longer-authorized saved selection is ignored (fallback), the row not rewritten.

Coverage: resolve/set behavior; the full authorization MATRIX (League Admin,
Arena Manager, Viewer, Coach, Player, Official, Guardian, unknown role) on
Memory/SQLite/PostgreSQL; a subject-resolution contract proving the context
selector and the web scope guards resolve the SAME caller identity; concurrent
last-write-wins; migration-044 durability; and the strict authenticated HTTP
contract (including a genuinely scoped identity).
"""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain import (
    ActiveContext, GuardianLink, OfficialRole, Role, SeasonStatus)
from hockey_scheduler.domain.models import Game
from hockey_scheduler.domain.setup_models import OfficialAssignment
from hockey_scheduler.services import context_scope
from hockey_scheduler.services.subject_scope import own_team_id
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.store.sql_store import migrate
from hockey_scheduler.web.server import STATE, Handler

_VERSION = "044_active_context"
ADMIN = (Role.LEAGUE_ADMIN, {})       # (role, scope) for a global operator


def _backends():
    yield "memory", InMemoryStore()
    yield "sqlite", SqlStore(":memory:")
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        SqlStore(url).clear_all_data()
        yield "postgres", SqlStore(url)


def _close(store):
    if isinstance(store, SqlStore):
        store.close()


# -- scenario builders ---------------------------------------------------
def _program_season(api, pname="P1", sname="S1"):
    pid = api.create_program(pname, "US", "UTC")["id"]
    sid = api.create_season(pid, sname)["id"]
    return pid, sid


def _team_registered(api, sid, tname="Alpha"):
    """A League+Division+Club+Team registered in Season ``sid``. Returns team_id."""
    lid = api.create_league(sid, "Gold")["id"]
    did = api.create_division(sid, "D1", league_id=lid)["id"]
    club = api.create_club("Club")["id"]
    team = api.create_team(club_id=club, name=tname, league_id=lid,
                           division_id=did)["id"]
    api.setup.register_team_for_season(sid, team, did)
    return team


def _official_assigned(api, sid, team_id):
    """An Official assigned to a Game in Season ``sid``. Returns (official_id, game_id)."""
    oid = api.create_official("Ref")["id"]
    store = api.store
    with store.transaction():
        gid = store.next_id("game")
        store.add_game(Game(id=gid, home_team_id=team_id, away_team_id=team_id,
                            start_time=None, season_id=sid))
        store.add_official_assignment(OfficialAssignment(
            id=store.next_id("assign"), game_id=gid, official_id=oid,
            role=OfficialRole.REFEREE))
    return oid, gid


def _archive(store, season_id):
    s = store.get_season(season_id)
    s.status = SeasonStatus.ARCHIVED
    store.save_season(s)


class ContextResolveSetTest(unittest.TestCase):
    """resolve/set behavior for a global operator (role/scope threaded)."""

    def test_empty_world_is_empty_context(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                self.assertEqual(
                    api.get_active_context("u1", *ADMIN),
                    {"program_id": None, "season_id": None, "read_only": False,
                     "program": None, "season": None}, label)
                _close(store)

    def test_fallback_prefers_latest_active_season_semantically(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid = api.create_program("P", "US", "UTC")["id"]
                # Insert out of date order; a null-date season must NOT win over a
                # dated one, and the LATEST date wins (not string/insertion order).
                s_mid = api.create_season(pid, "mid", start_date="2027-06-01")["id"]
                s_late = api.create_season(pid, "late", start_date="2027-09-01")["id"]
                api.create_season(pid, "nodate")           # null start_date
                api.create_season(pid, "early", start_date="2027-01-01")
                c = api.get_active_context("u1", *ADMIN)
                self.assertEqual(c["season_id"], s_late, (label, c))
                self.assertFalse(c["read_only"], label)
                self.assertNotEqual(c["season_id"], s_mid, label)
                _close(store)

    def test_program_only_when_no_active_season(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid = api.create_program("P", "US", "UTC")["id"]   # no Season yet
                c = api.get_active_context("u1", *ADMIN)
                self.assertEqual(c["program_id"], pid, label)      # Program-only
                self.assertIsNone(c["season_id"], label)
                # Explicit Program-only set round-trips.
                c = api.set_active_context("u1", *ADMIN, pid, None)
                self.assertEqual((c["program_id"], c["season_id"]), (pid, None),
                                 label)
                self.assertFalse(c["read_only"], label)
                _close(store)

    def test_archived_selection_is_read_only_history_not_replaced(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                p2, s2 = _program_season(api, "P2", "S2")
                _archive(store, s2)
                # Setting an archived Season is allowed — a read-only historical
                # context — NOT rejected, NOT swapped for p1's active Season.
                c = api.set_active_context("u1", *ADMIN, p2, s2)
                self.assertEqual(c["season_id"], s2, (label, c))
                self.assertTrue(c["read_only"], (label, c))
                self.assertEqual(api.get_active_context("u1", *ADMIN)["season_id"],
                                 s2, label)                        # honored on resolve
                _close(store)

    def test_deleted_season_and_deleted_program_fall_back(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                p2, s2 = _program_season(api, "P2", "S2")
                # deleted Season → fallback
                api.set_active_context("u1", *ADMIN, p2, s2)
                store.delete_season(s2)
                self.assertNotEqual(
                    api.get_active_context("u1", *ADMIN)["season_id"], s2, label)
                # deleted Program → fallback (never a dangling program_id)
                api.set_active_context("u2", *ADMIN, p1, s1)
                with store.transaction():
                    store.delete_season(s1)          # clear the dependent first
                    store.delete_program(p1)
                c = api.get_active_context("u2", *ADMIN)
                self.assertNotEqual(c["program_id"], p1, (label, c))
                _close(store)

    def test_orphan_row_and_per_user_isolation(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                p2, s2 = _program_season(api, "P2", "S2")
                api.set_active_context("u1", *ADMIN, p2, s2)
                # u2 has no saved row ⇒ deterministic fallback, not u1's choice.
                self.assertEqual(
                    api.get_active_context("u2", *ADMIN)["program_id"], p1, label)
                # An orphan row (user has no account) is inert: resolving it just
                # applies the passed role/scope; no crash.
                store.set_active_context(ActiveContext(
                    "ghost_user", p2, s2, datetime(2027, 1, 1, tzinfo=timezone.utc)))
                self.assertEqual(
                    api.get_active_context("ghost_user", *ADMIN)["season_id"], s2,
                    label)
                _close(store)


class ContextAuthorizationMatrixTest(unittest.TestCase):
    """resolve/set are filtered through the caller's REAL role + scope."""

    def test_global_roles_see_all_programs(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                for role in (Role.LEAGUE_ADMIN, Role.ARENA_MANAGER, Role.VIEWER):
                    c = api.get_active_context(f"u_{role.value}", role, {})
                    self.assertEqual(c["program_id"], p1, (label, role))
                    ok = api.set_active_context(f"u_{role.value}", role, {}, p1, s1)
                    self.assertEqual(ok["season_id"], s1, (label, role))
                _close(store)

    def test_unknown_or_unbound_role_fails_closed(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                # A Coach with no team_id, an Official with no official_id: empty
                # authorized set ⇒ empty context, and a set is a non-oracle 404.
                for role, scope in ((Role.COACH, {}), (Role.OFFICIAL, {}),
                                    (Role.GUARDIAN, {})):
                    self.assertIsNone(
                        api.get_active_context("x", role, scope)["program_id"],
                        (label, role))
                    r = api.set_active_context("x", role, scope, p1, s1)
                    self.assertEqual(r["error"]["code"], "not_found", (label, role))
                _close(store)

    def test_coach_scoped_to_own_program_only(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                team = _team_registered(api, s1)
                p2, s2 = _program_season(api, "P2", "S2")   # unrelated
                coach = (Role.COACH, {"team_id": team})
                c = api.get_active_context("c1", *coach)
                self.assertEqual(c["program_id"], p1, (label, c))   # only own program
                self.assertEqual(c["season_id"], s1, (label, c))
                # Selecting the unrelated Program is a non-oracle not_found.
                r = api.set_active_context("c1", *coach, p2, s2)
                self.assertEqual(r["error"]["code"], "not_found", label)
                self.assertEqual(r["error"]["details"]["reason"],
                                 "program_not_accessible", label)
                # Its own is allowed.
                self.assertEqual(
                    api.set_active_context("c1", *coach, p1, s1)["season_id"], s1,
                    label)
                _close(store)

    def test_player_live_team_transfer_deactivate_teamless(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                team1 = _team_registered(api, s1, "T1")
                p2, s2 = _program_season(api, "P2", "S2")
                team2 = _team_registered(api, s2, "T2")
                player = api.create_player(team1, "Pat", "forward")["id"]
                pl = (Role.PLAYER, {"player_id": player})
                # Live resolution: player sees team1's Program only.
                self.assertEqual(api.get_active_context("pl", *pl)["program_id"],
                                 p1, label)
                # Transfer to team2 (different Program) → former Program rejected,
                # new one granted — resolved LIVE from player_id.
                pobj = store.get_player(player)
                pobj.team_id = team2
                store.save_player(pobj)
                self.assertEqual(api.get_active_context("pl", *pl)["program_id"],
                                 p2, label)
                self.assertEqual(api.set_active_context("pl", *pl, p1, s1)
                                 ["error"]["code"], "not_found", label)  # former
                # Deactivated player → fails closed (empty).
                pobj = store.get_player(player)
                pobj.is_active = False
                store.save_player(pobj)
                self.assertIsNone(api.get_active_context("pl", *pl)["program_id"],
                                  label)
                _close(store)

    def test_official_sees_only_assigned_season_and_loses_it_on_unassign(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                team = _team_registered(api, s1)
                p2, s2 = _program_season(api, "P2", "S2")     # unassigned → unseen
                oid, gid = _official_assigned(api, s1, team)
                off = (Role.OFFICIAL, {"official_id": oid})
                c = api.get_active_context("o1", *off)
                self.assertEqual(c["program_id"], p1, (label, c))  # assigned only
                self.assertEqual(api.set_active_context("o1", *off, p2, s2)
                                 ["error"]["code"], "not_found", label)  # unrelated
                # Removing the assignment removes access immediately.
                with store.transaction():
                    store.remove_official_assignment(
                        store.assignments_for_official(oid)[0].id)
                self.assertIsNone(api.get_active_context("o1", *off)["program_id"],
                                  label)
                _close(store)

    def test_guardian_verified_link_only(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                team = _team_registered(api, s1)
                junior = api.create_player(team, "Junior", "forward")["id"]
                ts = datetime(2027, 1, 1, tzinfo=timezone.utc)
                with store.transaction():   # link rows directly (context reads them)
                    store.add_guardian_link(GuardianLink(
                        id="gl_unv", guardian_user_id="g_unv", player_id=junior,
                        created_at=ts, verified=False))
                    store.add_guardian_link(GuardianLink(
                        id="gl_ver", guardian_user_id="g_ver", player_id=junior,
                        created_at=ts, verified=True))
                # Unverified link grants nothing; a verified link grants the
                # junior's authorized Program/Season.
                self.assertIsNone(
                    api.get_active_context("g_unv", Role.GUARDIAN, {})["program_id"],
                    label)
                self.assertEqual(
                    api.get_active_context("g_ver", Role.GUARDIAN, {})["program_id"],
                    p1, label)
                _close(store)


class ContextSubjectContractTest(unittest.TestCase):
    """The context selector and the web scope guards resolve the SAME caller
    identity (one shared resolver), after transfer and deactivate — proving the
    de-duplication holds, not just today's happy path."""

    def test_context_and_web_agree_on_team_after_transfer_and_deactivate(self):
        store = InMemoryStore()
        api = ApiService(store)
        p1, s1 = _program_season(api, "P1", "S1")
        team1 = _team_registered(api, s1, "T1")
        p2, s2 = _program_season(api, "P2", "S2")
        team2 = _team_registered(api, s2, "T2")
        player = api.create_player(team1, "Pat", "forward")["id"]
        scope = {"player_id": player}
        # Both gates resolve the same live team, and context's Program set matches.
        self.assertEqual(own_team_id(Role.PLAYER, scope, store), team1)
        self.assertEqual(
            context_scope.authorized_program_ids(store, Role.PLAYER, scope, "pl"),
            {p1})
        # After a transfer, both still agree (same shared resolver).
        pobj = store.get_player(player); pobj.team_id = team2; store.save_player(pobj)
        self.assertEqual(own_team_id(Role.PLAYER, scope, store), team2)
        self.assertEqual(
            context_scope.authorized_program_ids(store, Role.PLAYER, scope, "pl"),
            {p2})
        # After deactivate, both fail closed identically.
        pobj = store.get_player(player); pobj.is_active = False; store.save_player(pobj)
        self.assertIsNone(own_team_id(Role.PLAYER, scope, store))
        self.assertEqual(
            context_scope.authorized_program_ids(store, Role.PLAYER, scope, "pl"),
            set())

    def test_stale_saved_selection_is_ignored_not_rewritten(self):
        # A saved selection whose Program the caller is no longer authorized for
        # is IGNORED (fallback) but NOT rewritten, so restoring authorization
        # restores the choice.
        store = InMemoryStore()
        api = ApiService(store)
        p1, s1 = _program_season(api, "P1", "S1")
        team = _team_registered(api, s1)
        p2, s2 = _program_season(api, "P2", "S2")
        api.set_active_context("u", *ADMIN, p2, s2)          # admin saves p2
        coach = (Role.COACH, {"team_id": team})              # coach can't see p2
        self.assertEqual(api.get_active_context("u", *coach)["program_id"], p1)
        self.assertEqual(store.get_active_context("u").program_id, p2)  # not rewritten
        self.assertEqual(api.get_active_context("u", *ADMIN)["season_id"], s2)  # reappears


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class ContextConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()
        api = ApiService(SqlStore(self.url))
        self.p1, self.s1 = _program_season(api, "P1", "S1")
        self.p2, self.s2 = _program_season(api, "P2", "S2")

    def _barrier_set(self, sels):
        barrier = threading.Barrier(len(sels))
        results = {}

        def run(key, program_id, season_id):
            store = SqlStore(self.url)
            api = ApiService(store)
            barrier.wait()
            try:
                results[key] = api.set_active_context(
                    "same_user", Role.LEAGUE_ADMIN, {}, program_id, season_id)
            except Exception as exc:            # a raw integrity error/500 → fail
                results[key] = f"ERR:{exc}"
            store.close()

        threads = [threading.Thread(target=run, args=(k, p, s))
                   for k, (p, s) in sels.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        return results

    def test_concurrent_first_writes_both_succeed_one_row_last_wins(self):
        results = self._barrier_set({"a": (self.p1, self.s1),
                                     "b": (self.p2, self.s2)})
        for key, res in results.items():
            self.assertNotIn("ERR", str(res), (key, res))      # no 500
            self.assertNotIn("error", res, (key, res))         # both 200
        check = SqlStore(self.url)
        with check.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM user_active_context "
                        "WHERE id = 'same_user'")
            self.assertEqual(cur.fetchone()["c"], 1)           # exactly one row
        final = check.get_active_context("same_user")
        self.assertIn((final.program_id, final.season_id),
                      [(self.p1, self.s1), (self.p2, self.s2)])  # last-committed wins
        check.close()


class ActiveContextMigrationTest(unittest.TestCase):
    """Migration 044 applies to an ADOPTED (pre-044) database and a stored
    selection survives a real close/reopen — file-backed SQLite + PostgreSQL."""

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

    def _downgrade(self, store):
        with store.transaction():
            cur = store.conn.cursor()
            cur.execute("DROP TABLE IF EXISTS user_active_context")
            cur.execute(store.dialect.sql(
                "DELETE FROM schema_migrations WHERE version = ?"), (_VERSION,))

    def test_upgrade_from_043_and_reopen_preserves_selection(self):
        stamp = datetime(2027, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
        for label, loc in self._locations():
            store = SqlStore(loc)
            try:
                if store.backend == "postgres":
                    store.reset_schema()
                self._downgrade(store)
                self.assertNotIn(_VERSION, store.migration_status()["applied"],
                                 label)
                migrate(store.conn, store.dialect)
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
                store.set_active_context(ActiveContext(
                    id="user_x", program_id="prog_x", season_id="seas_x",
                    updated_at=stamp))
                store.close()
                store = SqlStore(loc)
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
                rec = store.get_active_context("user_x")
                self.assertEqual((rec.program_id, rec.season_id, rec.updated_at),
                                 ("prog_x", "seas_x", stamp), label)
                # A Program-only (null Season) selection round-trips durably too.
                store.set_active_context(ActiveContext(
                    id="user_y", program_id="prog_y", season_id=None,
                    updated_at=stamp))
                store.close()
                store = SqlStore(loc)
                self.assertIsNone(store.get_active_context("user_y").season_id,
                                  label)
            finally:
                if store.backend == "postgres":
                    store.reset_schema()
                store.close()


class ActiveContextHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("DATABASE_URL", None)   # the default in-memory demo store
        STATE.reset()                          # seeds demo Program/Season + accounts
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _req(self, method, path, body=None, opener=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        op = opener or urllib.request.build_opener()
        try:
            with op.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _login(self, username):
        op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        self._req("POST", "/api/auth/login",
                  {"username": username, "password": "demo"}, opener=op)
        return op

    def test_roundtrip_and_no_500_on_repeat(self):
        admin = self._login("admin")
        s, ctx = self._req("GET", "/api/context", opener=admin)
        self.assertEqual(s, 200, ctx)
        self.assertIsNotNone(ctx["program_id"], ctx)
        body = {"program_id": ctx["program_id"], "season_id": ctx["season_id"]}
        s1, a = self._req("POST", "/api/context", body, opener=admin)
        s2, b = self._req("POST", "/api/context", body, opener=admin)   # idempotent
        self.assertEqual((s1, s2), (200, 200), (a, b))
        self.assertEqual(a["season_id"], b["season_id"])

    def test_unauthenticated_is_rejected(self):
        self.assertEqual(self._req("GET", "/api/context")[0], 401)
        self.assertEqual(self._req("POST", "/api/context",
                                   {"program_id": "p"})[0], 401)

    def test_strict_schema(self):
        admin = self._login("admin")
        # unknown field
        s, b = self._req("POST", "/api/context",
                         {"program_id": "p", "extra": 1}, opener=admin)
        self.assertEqual(s, 400, b)
        self.assertEqual(b["error"]["details"]["reason"], "unknown_field")
        # missing required program_id
        s, b = self._req("POST", "/api/context", {"season_id": "s"}, opener=admin)
        self.assertEqual(s, 400, b)
        self.assertEqual(b["error"]["details"]["reason"], "field_required")
        # wrong type
        s, b = self._req("POST", "/api/context", {"program_id": 5}, opener=admin)
        self.assertEqual(s, 400, b)
        self.assertEqual(b["error"]["details"]["reason"], "wrong_type")

    def test_scoped_identity_through_the_real_boundary(self):
        # The seeded "coach" account is scoped to the demo home team. Its context
        # resolves to that team's Program, proving session role/scope threading —
        # and setting context still grants it no operator authority.
        coach = self._login("coach")
        s, ctx = self._req("GET", "/api/context", opener=coach)
        self.assertEqual(s, 200, ctx)
        self.assertIsNotNone(ctx["program_id"], ctx)
        s, _ = self._req("POST", "/api/context",
                         {"program_id": ctx["program_id"],
                          "season_id": ctx["season_id"]}, opener=coach)
        self.assertEqual(s, 200)
        # ...yet an operator write stays forbidden.
        s, denied = self._req("POST", "/api/setup/venue", {"name": "V"},
                              opener=coach)
        self.assertEqual(s, 403, denied)


if __name__ == "__main__":
    unittest.main()
