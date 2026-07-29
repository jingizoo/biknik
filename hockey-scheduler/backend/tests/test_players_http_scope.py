"""GET /api/players IDOR regression — real authenticated HTTP, including
emails (#345 review blocker).

The prior coverage for the `list_players` Program/League ceiling asserted
`ApiService.list_players()` directly and, separately, that a rendered browser
page shows the right names. Neither exercised the real wire: `GET
/api/players?team_id=...` through the actual `Handler`/`authorize` chain, and
neither proved the email-bearing response (the real MANAGE_SETUP route sets
`include_email=True`) is empty for a scoped-out `team_id`. A future
route-wiring or query-parameter regression (e.g. dropping the `role`/`scope`
threading, or only checking `team_id is None`) would leak a junior player's
name AND email while every existing assertion stayed green.

Two halves, covering different things (#369 review guidance — do not fake
store coverage by parametrizing the HTTP layer, whose server binds a single
module-global `srv.STATE`):

* ``PlayersHttpScopeTest`` — REAL HTTP, over a REAL `ThreadingHTTPServer`
  running the real `Handler`, with real session cookies (login as each demo
  persona), against the DEFAULT in-memory store the server module boots with.
  This is the half that proves the *route wiring*: query-string parsing,
  `_resolve_role`, the `_operator_only` MANAGE_SETUP gate, and JSON
  serialization end to end — checking the RAW response text (not just parsed
  fields) so a leaked name/email anywhere in the payload is caught even if it
  rode in under an unexpected key.
* ``PlayersScopeFacadeContract`` (+ Memory/SQLite/PostgreSQL subclasses) —
  the SAME case matrix one layer down, calling `ApiService.list_players(...)`
  directly against each backend, proving the scoping query itself (not the
  HTTP dispatch) is correct on every store the app can run on. PostgreSQL
  only runs when `TEST_DATABASE_URL` is set, and isolates with a plain
  `SqlStore(url); store.clear_all_data()` (matching test_coach_scope_fail_
  closed.py / test_season_venue_access.py's convention as of this writing —
  another agent may later fold this into a shared helper).

Only `Role.LEAGUE_ADMIN` holds the `manage_setup` permission `/api/players`
is gated on (see `hockey_scheduler/domain/roles.py` — every other role's
permission set excludes `Permission.MANAGE_SETUP`), so League Admin is the
only role that can reach this route at all; that is asserted explicitly
below rather than assumed.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from urllib.parse import quote

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain import Role, Team
from hockey_scheduler.store import InMemoryStore, SqlStore

ACTOR = "user_players_scope_test"
POSITION = "forward"

# Unmistakable tokens: a leak shows up as a plain substring of the raw
# response body, no parsing required.
LEAGA_NAME, LEAGA_EMAIL = "LEAGA-Player", "leaga@example.test"
LEAGB_NAME, LEAGB_EMAIL = "LEAGB-Player", "leagb@example.test"
FOREIGN_NAME, FOREIGN_EMAIL = "FOREIGN-Player", "foreign@example.test"
UNBOUND_NAME, UNBOUND_EMAIL = "UNBOUND-Player", "unbound@example.test"

ALL_TOKENS = (LEAGA_NAME, LEAGA_EMAIL, LEAGB_NAME, LEAGB_EMAIL,
              FOREIGN_NAME, FOREIGN_EMAIL, UNBOUND_NAME, UNBOUND_EMAIL)


# =========================================================================== #
# HTTP half: real ThreadingHTTPServer, real Handler, real session cookies.    #
# Runs on the server's default (in-memory) store only — see module docstring. #
# =========================================================================== #
class PlayersHttpScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    # -- harness (copied idiom from test_v2_setup_contract.py) --------------
    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _admin(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        return c

    def _v2(self, c, entity, body):
        status, resp = self._req(c, "POST", f"/api/v2/setup/{entity}", body)
        self.assertEqual(status, 200, (entity, resp))
        self.assertNotIn("error", resp, (entity, resp))
        return resp

    def _players_raw(self, opener, team_id=None):
        """GET /api/players[?team_id=...], returning (status, raw_text, parsed).

        The raw text is what the leak-detection assertions check — a `name`
        or `email` value appearing ANYWHERE in the body, not just under the
        field the current implementation happens to put it in.
        """
        path = "/api/players"
        if team_id is not None:
            path += f"?team_id={quote(team_id)}"
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, method="GET")
        try:
            with opener.open(req) as r:
                raw = r.read()
                return r.status, raw.decode(), json.loads(raw or b"[]")
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, raw.decode(), json.loads(raw or b"{}")

    # -- fixture --------------------------------------------------------------
    def _build_fixture(self, c):
        """One Program, one Season, two Leagues (A, B), a Team per League with
        one distinctly-tokened Player+email each, plus the negative-case
        subjects: a Team in a different Program, a deleted Team, and an
        unbound (no permanent League) Team. Returns the ids needed to drive
        every case in the matrix.

        League B's registration is deliberately created THEN revoked
        (`.../remove`), so the same Team id also stands in for the "revoked
        Team id" case the repo owner named — proving revocation status (as
        opposed to the permanent `Team.league_id`) has no bearing on the
        scoping decision.
        """
        org = self._v2(c, "organization", {"name": "PS Org", "short_name": "PSO"})
        program = self._v2(c, "program",
                          {"name": "PS Program",
                           "operator_organization_id": org["id"]})
        season = self._v2(c, "season",
                         {"program_id": program["id"], "name": "PS Season"})
        league_a = self._v2(c, "league",
                           {"season_id": season["id"], "name": "PS League A",
                            "sort_order": 1})
        league_b = self._v2(c, "league",
                           {"season_id": season["id"], "name": "PS League B",
                            "sort_order": 2})
        club = self._v2(c, "club", {"name": "PS Club"})

        team_a = self._v2(c, "team",
                         {"league_id": league_a["id"], "club_id": club["id"],
                          "name": "PS Team A"})
        team_b = self._v2(c, "team",
                         {"league_id": league_b["id"], "club_id": club["id"],
                          "name": "PS Team B"})
        self._v2(c, "player", {"team_id": team_a["id"], "name": LEAGA_NAME,
                              "position": POSITION, "email": LEAGA_EMAIL})
        self._v2(c, "player", {"team_id": team_b["id"], "name": LEAGB_NAME,
                              "position": POSITION, "email": LEAGB_EMAIL})

        status, reg_a = self._req(
            c, "POST", f"/api/v2/setup/seasons/{season['id']}/team-registrations",
            {"team_id": team_a["id"], "league_id": league_a["id"]})
        self.assertEqual(status, 200, reg_a)
        status, reg_b = self._req(
            c, "POST", f"/api/v2/setup/seasons/{season['id']}/team-registrations",
            {"team_id": team_b["id"], "league_id": league_b["id"]})
        self.assertEqual(status, 200, reg_b)
        # Revoke League B's registration -- covers the owner's "revoked Team
        # id" case: the permanent Team.league_id (not registration state)
        # drives scoping, so this must stay excluded from League A exactly
        # like an actively-registered sibling-League team would.
        status, _ = self._req(
            c, "POST",
            f"/api/v2/setup/season-team-registration/{reg_b['id']}/remove", {})
        self.assertEqual(status, 200)

        # A Team that lives in an entirely different Program.
        org2 = self._v2(c, "organization", {"name": "PS Foreign Org",
                                           "short_name": "PFO"})
        program2 = self._v2(c, "program",
                           {"name": "PS Foreign Program",
                            "operator_organization_id": org2["id"]})
        season2 = self._v2(c, "season",
                          {"program_id": program2["id"], "name": "PS Foreign S"})
        league_f = self._v2(c, "league",
                           {"season_id": season2["id"], "name": "PS Foreign L"})
        club2 = self._v2(c, "club", {"name": "PS Foreign Club"})
        team_foreign = self._v2(c, "team",
                               {"league_id": league_f["id"], "club_id": club2["id"],
                                "name": "PS Foreign Team"})
        self._v2(c, "player", {"team_id": team_foreign["id"], "name": FOREIGN_NAME,
                              "position": POSITION, "email": FOREIGN_EMAIL})

        # A Team created, then deleted (no player attached, so the delete
        # itself is unblocked by dependents).
        team_deleted = self._v2(c, "team",
                               {"league_id": league_a["id"], "club_id": club["id"],
                                "name": "PS Deleted Team"})
        status, _ = self._req(
            c, "POST", f"/api/v2/setup/team/{team_deleted['id']}/delete", {})
        self.assertEqual(status, 200)

        # An unbound Team: real row, correct Program, but NO permanent League
        # (`Team.league_id is None`) -- the state `teams_without_league`
        # surfaces. The v2 create route always requires a resolvable League
        # (#283 Slice E), so this is injected directly into the shared store,
        # mirroring the "Loose" team technique in test_v2_setup_contract.py.
        store = self.srv.STATE.api.store
        unbound = Team(id=store.next_id("team"), name="PS Unbound Team",
                       club_id=club["id"], program_id=program["id"])
        store.add_team(unbound)
        self._v2(c, "player", {"team_id": unbound.id, "name": UNBOUND_NAME,
                              "position": POSITION, "email": UNBOUND_EMAIL})

        return {
            "program_id": program["id"], "season_id": season["id"],
            "league_a_id": league_a["id"], "league_b_id": league_b["id"],
            "team_a": team_a["id"], "team_b": team_b["id"],
            "team_foreign": team_foreign["id"],
            "team_deleted": team_deleted["id"], "team_unbound": unbound.id,
        }

    def _assert_no_leak(self, raw, allowed_tokens):
        for token in ALL_TOKENS:
            if token in allowed_tokens:
                continue
            self.assertNotIn(token, raw,
                             f"{token!r} leaked into the response: {raw}")

    # -- League A active: the full negative matrix ---------------------------
    def test_league_a_active_scoping_matrix(self):
        c = self._admin()
        ids = self._build_fixture(c)
        status, ctx = self._req(c, "POST", "/api/context",
                                {"program_id": ids["program_id"],
                                 "season_id": ids["season_id"],
                                 "league_id": ids["league_a_id"]})
        self.assertEqual(status, 200, ctx)

        # -- positives: no team_id, and explicitly League A's own Team -------
        status, raw, rows = self._players_raw(c)
        self.assertEqual(status, 200, raw)
        self.assertEqual({r["name"] for r in rows}, {LEAGA_NAME}, raw)
        self.assertEqual(rows[0]["email"], LEAGA_EMAIL, raw)
        self._assert_no_leak(raw, {LEAGA_NAME, LEAGA_EMAIL})

        status, raw, rows = self._players_raw(c, ids["team_a"])
        self.assertEqual(status, 200, raw)
        self.assertEqual({r["name"] for r in rows}, {LEAGA_NAME}, raw)
        self._assert_no_leak(raw, {LEAGA_NAME, LEAGA_EMAIL})

        # -- negatives: sibling-League(+revoked), foreign-Program,
        #    nonexistent, deleted, unbound -- ALL must return the identical
        #    empty shape, so none of them is distinguishable as "this one
        #    exists but is forbidden" vs. "this one doesn't exist" (no
        #    existence oracle).
        empty_cases = {
            "sibling_league_b_revoked": ids["team_b"],
            "foreign_program": ids["team_foreign"],
            "nonexistent": "team_totally_nonexistent_9999999",
            "deleted": ids["team_deleted"],
            "unbound": ids["team_unbound"],
        }
        baseline_raw = None
        for label, team_id in empty_cases.items():
            status, raw, rows = self._players_raw(c, team_id)
            self.assertEqual(status, 200, (label, raw))
            self.assertEqual(rows, [], (label, raw))
            self._assert_no_leak(raw, set())  # nothing at all may leak here
            if baseline_raw is None:
                baseline_raw = raw
            else:
                # Same shape across every negative -- not merely "empty" but
                # byte-identical, so a future variant (e.g. a 404 for
                # "nonexistent" vs 200+[] for "deleted") would fail here.
                self.assertEqual(raw, baseline_raw, label)

    # -- No League selected: Program-wide roster, repeated invariants --------
    def test_no_league_selected_is_program_wide(self):
        c = self._admin()
        ids = self._build_fixture(c)
        # Omitting league_id selects "No League" -- Program-wide is the
        # legitimate, INTENDED result here (contrast with League A active
        # above), so a future over-narrowing fix must not break this.
        status, ctx = self._req(c, "POST", "/api/context",
                                {"program_id": ids["program_id"],
                                 "season_id": ids["season_id"]})
        self.assertEqual(status, 200, ctx)
        self.assertIsNone(ctx["league_id"], ctx)

        status, raw, rows = self._players_raw(c)
        self.assertEqual(status, 200, raw)
        names = {r["name"] for r in rows}
        # Both Leagues' players AND the unbound Team's player (all three live
        # in this Program) -- but never the foreign-Program player.
        self.assertEqual(names, {LEAGA_NAME, LEAGB_NAME, UNBOUND_NAME}, raw)
        self._assert_no_leak(raw, {LEAGA_NAME, LEAGA_EMAIL,
                                   LEAGB_NAME, LEAGB_EMAIL,
                                   UNBOUND_NAME, UNBOUND_EMAIL})

        # The Program ceiling still applies regardless of League selection --
        # repeat the foreign-Program / nonexistent / deleted negatives here.
        for team_id in (ids["team_foreign"], "team_totally_nonexistent_9999999",
                        ids["team_deleted"]):
            status, raw, rows = self._players_raw(c, team_id)
            self.assertEqual(status, 200, raw)
            self.assertEqual(rows, [], (team_id, raw))
            self._assert_no_leak(raw, set())

        # Contrast check: under No League, League B's Team and the unbound
        # Team are legitimately answerable by their OWN explicit team_id --
        # proving the emptiness above is really about League A's selection,
        # not some other over-broad restriction.
        status, raw, rows = self._players_raw(c, ids["team_b"])
        self.assertEqual(status, 200, raw)
        self.assertEqual({r["name"] for r in rows}, {LEAGB_NAME}, raw)
        status, raw, rows = self._players_raw(c, ids["team_unbound"])
        self.assertEqual(status, 200, raw)
        self.assertEqual({r["name"] for r in rows}, {UNBOUND_NAME}, raw)

    # -- role coverage: only League Admin (manage_setup) reaches this route --
    def test_non_operator_role_is_refused(self):
        # hockey_scheduler/domain/roles.py: ROLE_PERMISSIONS grants
        # Permission.MANAGE_SETUP to Role.LEAGUE_ADMIN alone, and
        # `/api/players` is gated via `_operator_only("/api/setup/player")`
        # (required_permission → MANAGE_SETUP), so Coach -- which lacks it --
        # must be refused, never silently answering with an empty (or any)
        # player list.
        coach = self._client()
        self._req(coach, "POST", "/api/auth/login",
                 {"username": "coach", "password": "demo"})
        status, raw, body = self._players_raw(coach)
        self.assertEqual(status, 403, raw)
        self.assertEqual(body["error"]["details"]["required"], "manage_setup", raw)
        self._assert_no_leak(raw, set())

    def test_unauthenticated_is_refused(self):
        anon = self._client()
        status, raw, _ = self._players_raw(anon)
        self.assertIn(status, (401, 403), raw)
        self._assert_no_leak(raw, set())


# =========================================================================== #
# Facade half: ApiService.list_players(...) directly, across store backends. #
# =========================================================================== #
class PlayersScopeFacadeContract:
    """Store-agnostic version of the same case matrix, one layer below HTTP.
    Concrete subclasses below supply the backend via ``make_store``."""

    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()
        self.api = ApiService(self.store)
        self.user_id = ACTOR
        self.role = Role.LEAGUE_ADMIN
        self.scope = {}

    def _select(self, program_id, season_id, league_id=None):
        self.api.set_active_context(self.user_id, self.role, self.scope,
                                    program_id, season_id, league_id)

    def _players(self, team_id=None):
        return self.api.list_players(team_id, include_email=True,
                                     user_id=self.user_id, role=self.role,
                                     scope=self.scope)

    def _fixture(self):
        api = self.api
        org = api.create_organization("PS Org", actor_id=ACTOR)["id"]
        program = api.create_program(
            "PS Program", operator_organization_id=org, actor_id=ACTOR)["id"]
        season = api.create_season(program, "PS Season", actor_id=ACTOR)["id"]
        league_a = api.create_league(season, "PS League A", 1, ACTOR)["id"]
        league_b = api.create_league(season, "PS League B", 2, ACTOR)["id"]
        club = api.create_club("PS Club", actor_id=ACTOR)["id"]

        team_a = api.create_team(club, None, "PS Team A", ACTOR,
                                 league_id=league_a)["id"]
        team_b = api.create_team(club, None, "PS Team B", ACTOR,
                                 league_id=league_b)["id"]
        api.create_player(team_a, LEAGA_NAME, POSITION, email=LEAGA_EMAIL,
                          actor_id=ACTOR)
        api.create_player(team_b, LEAGB_NAME, POSITION, email=LEAGB_EMAIL,
                          actor_id=ACTOR)

        reg_a = api.register_team_for_season(season, team_a, actor_id=ACTOR,
                                             league_id=league_a)
        reg_b = api.register_team_for_season(season, team_b, actor_id=ACTOR,
                                             league_id=league_b)
        self.assertNotIn("error", reg_a, reg_a)
        self.assertNotIn("error", reg_b, reg_b)
        # Revoke League B's registration -- the "revoked Team id" case.
        api.unregister_team_from_season(reg_b["id"], actor_id=ACTOR)

        org2 = api.create_organization("PS Foreign Org", actor_id=ACTOR)["id"]
        program2 = api.create_program(
            "PS Foreign Program", operator_organization_id=org2,
            actor_id=ACTOR)["id"]
        season2 = api.create_season(program2, "PS Foreign S", actor_id=ACTOR)["id"]
        league_f = api.create_league(season2, "PS Foreign L", 1, ACTOR)["id"]
        club2 = api.create_club("PS Foreign Club", actor_id=ACTOR)["id"]
        team_foreign = api.create_team(club2, None, "PS Foreign Team", ACTOR,
                                       league_id=league_f)["id"]
        api.create_player(team_foreign, FOREIGN_NAME, POSITION,
                          email=FOREIGN_EMAIL, actor_id=ACTOR)

        team_deleted = api.create_team(club, None, "PS Deleted Team", ACTOR,
                                       league_id=league_a)["id"]
        deleted = api.delete_team(team_deleted, actor_id=ACTOR)
        self.assertNotIn("error", deleted, deleted)

        unbound_id = self.store.next_id("team")
        self.store.add_team(Team(id=unbound_id, name="PS Unbound Team",
                                 club_id=club, program_id=program))
        api.create_player(unbound_id, UNBOUND_NAME, POSITION,
                          email=UNBOUND_EMAIL, actor_id=ACTOR)

        return {
            "program_id": program, "season_id": season,
            "league_a_id": league_a, "league_b_id": league_b,
            "team_a": team_a, "team_b": team_b,
            "team_foreign": team_foreign, "team_deleted": team_deleted,
            "team_unbound": unbound_id,
        }

    def test_league_a_active_scoping_matrix(self):
        ids = self._fixture()
        self._select(ids["program_id"], ids["season_id"], ids["league_a_id"])

        rows = self._players()
        self.assertEqual({r["name"] for r in rows}, {LEAGA_NAME})
        self.assertEqual(rows[0]["email"], LEAGA_EMAIL)

        rows = self._players(ids["team_a"])
        self.assertEqual({r["name"] for r in rows}, {LEAGA_NAME})

        baseline = None
        for label, team_id in (
            ("sibling_league_b_revoked", ids["team_b"]),
            ("foreign_program", ids["team_foreign"]),
            ("nonexistent", "team_totally_nonexistent_9999999"),
            ("deleted", ids["team_deleted"]),
            ("unbound", ids["team_unbound"]),
        ):
            rows = self._players(team_id)
            self.assertEqual(rows, [], label)
            if baseline is None:
                baseline = rows
            else:
                self.assertEqual(rows, baseline, label)

    def test_no_league_selected_is_program_wide(self):
        ids = self._fixture()
        self._select(ids["program_id"], ids["season_id"], None)

        rows = self._players()
        names = {r["name"] for r in rows}
        self.assertEqual(names, {LEAGA_NAME, LEAGB_NAME, UNBOUND_NAME})

        for team_id in (ids["team_foreign"], "team_totally_nonexistent_9999999",
                        ids["team_deleted"]):
            self.assertEqual(self._players(team_id), [], team_id)

        self.assertEqual({r["name"] for r in self._players(ids["team_b"])},
                         {LEAGB_NAME})
        self.assertEqual({r["name"] for r in self._players(ids["team_unbound"])},
                         {UNBOUND_NAME})


class MemoryPlayersScopeTest(PlayersScopeFacadeContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class SqlitePlayersScopeTest(PlayersScopeFacadeContract, unittest.TestCase):
    def make_store(self):
        return SqlStore(":memory:")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresPlayersScopeTest(PlayersScopeFacadeContract, unittest.TestCase):
    def make_store(self):
        store = SqlStore(os.environ["TEST_DATABASE_URL"])
        store.clear_all_data()  # isolate from any prior run's rows
        return store


if __name__ == "__main__":
    unittest.main()
