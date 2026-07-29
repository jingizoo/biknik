"""Regression tests for ``get_standings``'s NEW Program-ownership check (#367).

Before #367, ``get_standings(division_id)`` computed a Division's standings
for ANY caller with no ownership check whatsoever — any ``division_id`` from
any Program was fully computable by anyone who could guess or enumerate an
id. The endpoint now optionally accepts ``(user_id, role, scope)``; when a
real ``role`` is supplied (the HTTP route always supplies one), the
Division's owning Program (via ``division.league_season_id -> LeagueSeason.
season_id -> Season.program_id``) must be a member of the caller's
``context_scope.authorized_program_ids`` set, or the response collapses to
the SAME generic empty shape (``{"division_id": ..., "standings": []}``) a
nonexistent ``division_id`` already returns — deliberately non-oracle, so an
attacker probing division ids can never distinguish "exists, wrong Program"
from "doesn't exist at all".

Called with no role at all (the historical 1-arg shape), no ownership check
runs — unchanged pre-#367 behavior, since dozens of existing direct/internal
call sites across the suite rely on the default.

Coverage:
  * the no-role legacy call performs no ownership check at all (sanity);
  * a Coach/Player-scoped caller (one Program only) is rejected for a
    foreign Program's division with the exact same shape a nonexistent id
    produces — proven by direct comparison, not just "no error";
  * that SAME caller's own Program's division still returns REAL computed
    standings (positive control — the check isn't rejecting everyone);
  * a global role (League Admin) reaches ANY Program's standings regardless
    of which Program is currently "active" in its context, contrasted with
    a scoped Coach being rejected for that same foreign Program — proving
    the gate is ``authorized_program_ids`` (the real authorization ceiling),
    not merely "the one currently active Program";
  * the real HTTP route: 401 signed-out (previously fully unauthenticated),
    200 real standings for an authorized session, 200 generic empty shape
    (never 403/404) for an unauthorized one.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.store import InMemoryStore


def _build_scored_division(api, tag, actor_id=None):
    """A minimal Program → Season → League → Division with two permanent
    Teams (each's real competition ``Team.league_id`` bound to that League,
    per the #367 vocabulary landmine — never the ``get_demo_overview`` team
    row's ``league_id``, which means Program there instead), registered into
    the Season under that League, with one FINAL (approved) result recorded
    so the Division's standings carry real, non-empty numbers (home wins
    4-2: home w=1/pts=2/gd=+2, away l=1/pts=0/gd=-2).

    Mirrors the quickest fixture shape in test_results.py (ResultsTest),
    built through the ApiService facade per #367's required fixture
    signatures rather than the lower-level SetupService that file uses
    directly.
    """
    program = api.create_program(f"Program {tag}", actor_id=actor_id)
    assert "error" not in program, program
    season = api.create_season(program["id"], "Season", actor_id=actor_id)
    assert "error" not in season, season
    league = api.create_league(season["id"], f"League {tag}", actor_id=actor_id)
    assert "error" not in league, league
    division = api.create_division(season["id"], f"Division {tag}",
                                   league_id=league["id"], actor_id=actor_id)
    assert "error" not in division, division
    club = api.create_club(f"Club {tag}", actor_id=actor_id)
    assert "error" not in club, club
    home = api.create_team(club["id"], None, f"{tag} Home", actor_id=actor_id,
                           program_id=program["id"], league_id=league["id"])
    assert "error" not in home, home
    away = api.create_team(club["id"], None, f"{tag} Away", actor_id=actor_id,
                           program_id=program["id"], league_id=league["id"])
    assert "error" not in away, away
    for team in (home, away):
        reg = api.register_team_for_season(season["id"], team["id"],
                                           division["id"], actor_id=actor_id,
                                           league_id=league["id"])
        assert "error" not in reg, reg
    venue = api.create_venue(f"Venue {tag}", league_id=program["id"],
                             actor_id=actor_id)
    assert "error" not in venue, venue
    rink = api.create_rink(venue["id"], f"Rink {tag}", actor_id=actor_id)
    assert "error" not in rink, rink
    access = api.grant_season_venue_access(season["id"], venue["id"],
                                           actor_id=actor_id)
    assert "error" not in access, access
    slot = api.create_ice_slot(rink["id"], "2026-09-01T18:00:00+00:00",
                               "2026-09-01T20:00:00+00:00", actor_id=actor_id)
    assert "error" not in slot, slot
    game = api.create_game(season["id"], division["id"], home["id"], away["id"],
                           slot["id"], league_id=league["id"], actor_id=actor_id)
    assert "error" not in game, game
    rec = api.record_result(game["id"], 4, 2, actor_id=actor_id)
    assert "error" not in rec, rec
    approved = api.approve_result(game["id"], actor_id=actor_id)
    assert "error" not in approved, approved
    return {"program": program, "season": season, "league": league,
            "division": division, "club": club, "home": home, "away": away,
            "game": game}


class StandingsAuthorizationComputationTest(unittest.TestCase):
    """Facade-level, Memory-backed — mirrors SetupProgressComputationTest's
    scope in test_setup_progress.py (a pure read composed from store methods
    that already carry their own Memory/SQLite/PostgreSQL parity coverage
    elsewhere, so one backend suffices for the authorization logic itself)."""

    def _api(self):
        return ApiService(InMemoryStore())

    def test_legacy_call_with_no_role_performs_no_ownership_check(self):
        """The historical 1-arg shape (``user_id``/``role``/``scope`` all
        omitted, defaulting to None) is exactly pre-#367 behavior: no
        ownership check runs at all. Many pre-existing direct call sites
        across the suite (e.g. test_results.py) rely on this default."""
        api = self._api()
        fixture = _build_scored_division(api, "B")

        result = api.get_standings(fixture["division"]["id"])

        self.assertNotIn("error", result, result)
        self.assertEqual(result["division_id"], fixture["division"]["id"])
        self.assertTrue(result["standings"], result)
        names = {row["team_name"] for row in result["standings"]}
        self.assertEqual(names, {fixture["home"]["name"], fixture["away"]["name"]})

    def test_foreign_program_division_is_indistinguishable_from_nonexistent(self):
        """The core #367 regression: a Coach scoped to Program A's own team
        requesting Program B's division must get the SAME generic empty
        shape a nonexistent division_id already returns — compared directly,
        not just checked for "no error", so a response-content-only
        attacker gains no signal distinguishing the two cases."""
        api = self._api()
        fixture_a = _build_scored_division(api, "A")
        fixture_b = _build_scored_division(api, "B")
        coach_scope = {"team_id": fixture_a["home"]["id"]}

        foreign = api.get_standings(fixture_b["division"]["id"], "coach-1",
                                    Role.COACH, coach_scope)
        nonexistent = api.get_standings("division_does_not_exist", "coach-1",
                                        Role.COACH, coach_scope)

        self.assertEqual(foreign, {"division_id": fixture_b["division"]["id"],
                                   "standings": []})
        self.assertEqual(nonexistent,
                         {"division_id": "division_does_not_exist",
                          "standings": []})
        # Identical shape -- the only difference is each echoing back its
        # OWN queried division_id, which is not itself a signal about
        # whether that id ever existed.
        self.assertEqual(set(foreign), set(nonexistent))
        self.assertEqual(foreign["standings"], nonexistent["standings"])

    def test_own_program_division_returns_real_standings_positive_control(self):
        """Positive control: the SAME Coach requesting THEIR OWN Program's
        division gets the REAL computed standings, not the empty shape --
        proving the check isn't accidentally rejecting everyone."""
        api = self._api()
        fixture_a = _build_scored_division(api, "A")
        coach_scope = {"team_id": fixture_a["home"]["id"]}

        own = api.get_standings(fixture_a["division"]["id"], "coach-1",
                                Role.COACH, coach_scope)

        self.assertNotIn("error", own, own)
        self.assertEqual(own["division_id"], fixture_a["division"]["id"])
        self.assertNotEqual(own["standings"], [],
                           "the caller's own Program must not be rejected")
        home_row = next(row for row in own["standings"]
                        if row["team_name"] == fixture_a["home"]["name"])
        self.assertEqual(home_row["w"], 1)
        self.assertEqual(home_row["pts"], 2)
        self.assertEqual(home_row["gd"], 2)
        away_row = next(row for row in own["standings"]
                        if row["team_name"] == fixture_a["away"]["name"])
        self.assertEqual(away_row["l"], 1)
        self.assertEqual(away_row["pts"], 0)

    def test_global_role_reaches_any_program_unlike_a_scoped_one(self):
        """League Admin is a "global" role (``context_scope._GLOBAL_ROLES``)
        — authorized for EVERY Program. Pinning the active context at
        Program A via ``set_active_context`` and then requesting Program
        B's standings must still succeed, proving the gate is
        ``authorized_program_ids`` (the caller's real authorization
        ceiling), not just "the one currently active Program". Contrast
        with a Coach scoped to Program A's own team, rejected for the same
        foreign Program B exactly as in the isolation test above."""
        api = self._api()
        fixture_a = _build_scored_division(api, "A")
        fixture_b = _build_scored_division(api, "B")

        api.set_active_context("admin-1", Role.LEAGUE_ADMIN, {},
                               fixture_a["program"]["id"],
                               fixture_a["season"]["id"])
        admin_b = api.get_standings(fixture_b["division"]["id"], "admin-1",
                                    Role.LEAGUE_ADMIN, {})
        self.assertNotIn("error", admin_b, admin_b)
        self.assertNotEqual(
            admin_b["standings"], [],
            "League Admin's active context is Program A, but League Admin "
            "is a global role and must still reach Program B's standings")
        away_row = next(row for row in admin_b["standings"]
                        if row["team_name"] == fixture_b["away"]["name"])
        self.assertEqual(away_row["l"], 1)

        coach_scope = {"team_id": fixture_a["home"]["id"]}
        coach_b = api.get_standings(fixture_b["division"]["id"], "coach-1",
                                    Role.COACH, coach_scope)
        self.assertEqual(coach_b, {"division_id": fixture_b["division"]["id"],
                                   "standings": []},
                        "a Coach scoped to Program A only must be rejected "
                        "for Program B exactly like the isolation test")


class StandingsAuthorizationHttpTest(unittest.TestCase):
    """Route/authz contract over real HTTP — mirrors test_setup_progress.py's
    SetupProgressHttpTest harness. The demo seed (STATE.reset()) provisions
    the standard admin/arena/coach accounts this reuses; "coach" is scoped
    to a team in the demo's OWN seeded Program, distinct from any fresh
    Program built per-test via ``_build_scored_division``."""

    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def setUp(self):
        # Per-test reset (not just once per class): a clean full demo seed
        # (including the admin/arena/coach accounts) so no test's fixture
        # writes leak into the next one.
        self.srv.STATE.reset()

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

    def _login(self, username):
        c = self._client()
        status, body = self._req(c, "POST", "/api/auth/login",
                                 {"username": username, "password": "demo"})
        self.assertEqual(status, 200, body)
        return c

    def test_requires_signed_in_session(self):
        """This route was previously completely unauthenticated -- a bare
        GET with no session must now be rejected, not silently computed."""
        fixture = _build_scored_division(self.srv.STATE.api, "HTTP")
        c = self._client()
        status, _ = self._req(c, "GET",
                              f"/api/standings/{fixture['division']['id']}")
        self.assertEqual(status, 401)

    def test_authorized_session_returns_real_standings(self):
        """League Admin is a global role, authorized for every Program
        including one freshly built mid-test."""
        fixture = _build_scored_division(self.srv.STATE.api, "HTTP")
        admin = self._login("admin")

        status, resp = self._req(
            admin, "GET", f"/api/standings/{fixture['division']['id']}")

        self.assertEqual(status, 200, resp)
        self.assertNotEqual(resp["standings"], [])
        names = {row["team_name"] for row in resp["standings"]}
        self.assertEqual(names, {fixture["home"]["name"], fixture["away"]["name"]})

    def test_unauthorized_session_returns_generic_empty_shape_not_403_or_404(self):
        """A real session for a role NOT authorized for this Program (the
        seeded "coach" persona, scoped to a team in the demo's own separate
        Program) must get 200 with the SAME generic empty-standings shape a
        nonexistent division_id returns -- never 403/404, which would leak
        "this division exists, you're just not allowed" to an unauthorized
        caller. Compared directly against the nonexistent-id response too,
        the same non-oracle proof as the facade-level test, over HTTP."""
        fixture = _build_scored_division(self.srv.STATE.api, "HTTP")
        coach = self._login("coach")

        status, resp = self._req(
            coach, "GET", f"/api/standings/{fixture['division']['id']}")
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp, {"division_id": fixture["division"]["id"],
                               "standings": []}, resp)

        status_missing, resp_missing = self._req(
            coach, "GET", "/api/standings/division_does_not_exist")
        self.assertEqual(status_missing, 200, resp_missing)
        self.assertEqual(set(resp), set(resp_missing))
        self.assertEqual(resp["standings"], resp_missing["standings"])


if __name__ == "__main__":
    unittest.main()
