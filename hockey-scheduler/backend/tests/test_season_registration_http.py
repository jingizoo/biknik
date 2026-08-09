"""HTTP surface for season team registrations (#180 PR B).

Exposes the PR A registration lifecycle over the setup API: register a permanent
league team for a season, reassign its division, remove it, and list a season's
registrations / a league's teams. All League-Admin-only (MANAGE_SETUP, via the
/api/setup/ authz catch-all) with the actor resolved from the session.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import Role
from hockey_scheduler.web.server import STATE, Handler


class SeasonRegistrationHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def _req(self, method, path, body=None, role=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        if role is not None:
            req.add_header("X-Demo-Role", role)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _session_req(self, opener, method, path, body=None):
        """Same request, issued through a REAL signed-in session."""
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def _admin_session(self, program_id=None, season_id=None):
        """A real League-Admin SESSION with the fixture's tuple EXPLICITLY
        selected (#409).

        The registration reassign/remove routes are guarded setup mutations,
        and a guarded mutation now requires an explicitly PERSISTED
        Program/Season — a resolved tuple is not a chosen one. An
        ``X-Demo-Role`` caller has no backing account, so it has nowhere to
        persist a selection and can no longer drive those two routes at all;
        the operator the product actually ships is a signed-in one, which is
        what this returns. Every unguarded call in this class keeps using the
        header path unchanged.
        """
        if STATE.api.store.get_user_account_by_username("reg_admin") is None:
            STATE.api.accounts.create_account(
                "reg_admin", "demo", Role.LEAGUE_ADMIN, scope={},
                actor_id="test_seed")
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        status, body = self._session_req(
            opener, "POST", "/api/auth/login",
            {"username": "reg_admin", "password": "demo"})
        self.assertEqual(status, 200, body)
        if program_id is not None:
            status, body = self._session_req(
                opener, "POST", "/api/context",
                {"program_id": program_id, "season_id": season_id})
            self.assertEqual(status, 200, body)
        return opener

    def _admin_setup(self):
        """Build a fresh league/season/division/club/team as League Admin over
        HTTP and return the created ids.

        Clean slate first (#369). The CREATE calls here are identity-less
        ``X-Demo-Role`` calls, which the context resolver serves from its
        deterministic fallback: the first authorized Program by id. Resetting
        to an empty store makes the Program this fixture creates the one that
        fallback lands on, regardless of test order.

        The guarded reassign/remove routes below can no longer be driven that
        way (#409 — a mutation needs an explicitly PERSISTED tuple, and a
        header caller has no account to persist one against), so those two
        calls go through ``_admin_session`` instead."""
        STATE.reset(seed=False)
        # #409 extends that same reasoning to the CREATES: a Season create is
        # PROGRAM-AXIS and a division/team create is judged against the axes
        # its parents carry, so all of them now need a persisted selection an
        # ``X-Demo-Role`` caller has nowhere to keep. The whole fixture is
        # therefore built by the signed-in operator the product actually
        # ships, selecting each axis the moment it exists — which is what an
        # operator does. The GET listings below keep the header path unchanged.
        operator = self._admin_session()
        self._operator = operator
        _, league = self._session_req(operator, "POST", "/api/setup/league",
                                      {"name": "Reg League"})
        self._session_req(operator, "POST", "/api/context",
                          {"program_id": league["id"]})
        _, season = self._session_req(
            operator, "POST", "/api/setup/season",
            {"league_id": league["id"], "name": "Fall 2026"})
        self._session_req(operator, "POST", "/api/context",
                          {"program_id": league["id"],
                           "season_id": season["id"]})
        _, division = self._session_req(
            operator, "POST", "/api/setup/division",
            {"season_id": season["id"], "name": "Div A"})
        _, club = self._session_req(operator, "POST", "/api/setup/club",
                                    {"name": "Club X"})
        _, team = self._session_req(
            operator, "POST", "/api/setup/team",
            {"club_id": club["id"], "division_id": division["id"],
             "name": "Lions"})
        return league, season, division, team

    def test_admin_can_register_reassign_and_remove(self):
        league, season, division, team = self._admin_setup()
        operator = self._operator
        # Register the team for the season (#409: a guarded CREATE, so it runs
        # through the same signed-in operator that built the fixture, with this
        # fixture's own Program/Season already selected).
        status, reg = self._session_req(
            operator, "POST",
            f"/api/setup/seasons/{season['id']}/team-registrations",
            {"team_id": team["id"], "division_id": division["id"]})
        self.assertEqual(status, 200, reg)
        self.assertEqual(reg["team_id"], team["id"])
        # It shows up in the season's registration list.
        status, listing = self._req(
            "GET", f"/api/setup/seasons/{season['id']}/team-registrations",
            role="league_admin")
        self.assertEqual(status, 200)
        self.assertIn(reg["id"], [r["id"] for r in listing["registrations"]])
        # Reassign to a second division in the same season.
        _, div_b = self._session_req(operator, "POST", "/api/setup/division",
                                     {"season_id": season["id"],
                                      "name": "Div B"})
        status, moved = self._session_req(
            operator, "POST",
            f"/api/setup/season-team-registration/{reg['id']}/assign-division",
            {"division_id": div_b["id"]})
        self.assertEqual(status, 200, moved)
        self.assertEqual(moved["division_id"], div_b["id"])
        # Remove from the season (deactivates; Team preserved).
        status, removed = self._session_req(
            operator, "POST",
            f"/api/setup/season-team-registration/{reg['id']}/remove", {})
        self.assertEqual(status, 200, removed)
        self.assertFalse(removed["active"])
        status, teams = self._req(
            "GET", f"/api/setup/leagues/{league['id']}/teams", role="league_admin")
        self.assertEqual(status, 200)
        self.assertIn(team["id"], [t["id"] for t in teams["teams"]])

    def test_duplicate_registration_returns_structured_error(self):
        _, season, division, team = self._admin_setup()
        operator = self._operator
        path = f"/api/setup/seasons/{season['id']}/team-registrations"
        body = {"team_id": team["id"], "division_id": division["id"]}
        self._session_req(operator, "POST", path, body)
        status, dup = self._session_req(operator, "POST", path, body)
        self.assertEqual(status, 400)
        self.assertEqual(dup["error"]["code"], "validation_error")

    def test_v2_register_rejects_active_stray_elsewhere_over_real_http(self):
        """#331 review round 20 finding 1: register_team_for_season's new
        season-wide conflict guard, over the actual v2 HTTP boundary the
        real Season participation UI posts to -- not just the ApiService
        facade called directly, which every other regression for this fix
        uses. The reviewer's "real HTTP paths" bar, met once."""
        from hockey_scheduler.domain import SeasonTeamRegistration
        api = STATE.api
        program = api.create_program("P", actor_id="admin")
        season = api.create_season(program["id"], "S", actor_id="admin")
        l1 = api.create_league(season["id"], "L1", actor_id="admin")
        l2 = api.create_league(season["id"], "L2", actor_id="admin")
        club = api.create_club("C", actor_id="admin")
        team = api.create_team(club["id"], None, "T", actor_id="admin",
                               league_id=l1["id"])
        ls2 = api.store.league_season_for(l2["id"], season["id"])
        stray = SeasonTeamRegistration(
            id=api.store.next_id("streg"), league_season_id=ls2.id,
            team_id=team["id"], division_id=None, active=True)
        api.store.add_season_team_registration(stray)

        # #409: the registration CREATE is guarded, so it runs through a real
        # signed-in operator with this fixture's own Program/Season selected —
        # the same tuple the header caller was resolving by fallback.
        operator = self._admin_session(program["id"], season["id"])
        status, body = self._session_req(
            operator, "POST",
            f"/api/v2/setup/seasons/{season['id']}/team-registrations",
            {"team_id": team["id"], "league_id": l1["id"], "division_id": None})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"],
                         "team_registration_conflict", body)
        self.assertEqual(body["error"]["details"]["affected_registration_ids"],
                         [stray.id])
        # Zero mutation: only the stray still exists for this Team.
        self.assertEqual(
            len(api.store.registrations_for_season(season["id"])), 1)

    def test_viewer_cannot_register(self):
        status, body = self._req(
            "POST", "/api/setup/seasons/season_x/team-registrations",
            {"team_id": "team_x"}, role="viewer")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["details"]["required"], "manage_setup")

    def test_arena_manager_cannot_register_or_list(self):
        # MANAGE_SETUP is League-Admin-only; an Arena Manager is forbidden.
        status, _ = self._req(
            "POST", "/api/setup/seasons/season_x/team-registrations",
            {"team_id": "team_x"}, role="arena_manager")
        self.assertEqual(status, 403)
        status, body = self._req(
            "GET", "/api/setup/seasons/season_x/team-registrations",
            role="arena_manager")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["details"]["required"], "manage_setup")

    def test_viewer_cannot_list_league_teams(self):
        status, body = self._req("GET", "/api/setup/leagues/league_x/teams",
                                 role="viewer")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["details"]["required"], "manage_setup")


if __name__ == "__main__":
    unittest.main()
