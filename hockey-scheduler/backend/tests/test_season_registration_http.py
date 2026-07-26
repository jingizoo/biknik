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
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

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

    def _admin_setup(self):
        """Build a fresh league/season/division/club/team as League Admin over
        HTTP and return the created ids."""
        _, league = self._req("POST", "/api/setup/league",
                               {"name": "Reg League"}, role="league_admin")
        _, season = self._req("POST", "/api/setup/season",
                              {"league_id": league["id"], "name": "Fall 2026"},
                              role="league_admin")
        _, division = self._req("POST", "/api/setup/division",
                               {"season_id": season["id"], "name": "Div A"},
                               role="league_admin")
        _, club = self._req("POST", "/api/setup/club", {"name": "Club X"},
                            role="league_admin")
        _, team = self._req("POST", "/api/setup/team",
                           {"club_id": club["id"], "division_id": division["id"],
                            "name": "Lions"}, role="league_admin")
        return league, season, division, team

    def test_admin_can_register_reassign_and_remove(self):
        league, season, division, team = self._admin_setup()
        # Register the team for the season.
        status, reg = self._req(
            "POST", f"/api/setup/seasons/{season['id']}/team-registrations",
            {"team_id": team["id"], "division_id": division["id"]},
            role="league_admin")
        self.assertEqual(status, 200)
        self.assertEqual(reg["team_id"], team["id"])
        # It shows up in the season's registration list.
        status, listing = self._req(
            "GET", f"/api/setup/seasons/{season['id']}/team-registrations",
            role="league_admin")
        self.assertEqual(status, 200)
        self.assertIn(reg["id"], [r["id"] for r in listing["registrations"]])
        # Reassign to a second division in the same season.
        _, div_b = self._req("POST", "/api/setup/division",
                            {"season_id": season["id"], "name": "Div B"},
                            role="league_admin")
        status, moved = self._req(
            "POST",
            f"/api/setup/season-team-registration/{reg['id']}/assign-division",
            {"division_id": div_b["id"]}, role="league_admin")
        self.assertEqual(status, 200)
        self.assertEqual(moved["division_id"], div_b["id"])
        # Remove from the season (deactivates; Team preserved).
        status, removed = self._req(
            "POST", f"/api/setup/season-team-registration/{reg['id']}/remove",
            {}, role="league_admin")
        self.assertEqual(status, 200)
        self.assertFalse(removed["active"])
        status, teams = self._req(
            "GET", f"/api/setup/leagues/{league['id']}/teams", role="league_admin")
        self.assertEqual(status, 200)
        self.assertIn(team["id"], [t["id"] for t in teams["teams"]])

    def test_duplicate_registration_returns_structured_error(self):
        _, season, division, team = self._admin_setup()
        path = f"/api/setup/seasons/{season['id']}/team-registrations"
        body = {"team_id": team["id"], "division_id": division["id"]}
        self._req("POST", path, body, role="league_admin")
        status, dup = self._req("POST", path, body, role="league_admin")
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

        status, body = self._req(
            "POST", f"/api/v2/setup/seasons/{season['id']}/team-registrations",
            {"team_id": team["id"], "league_id": l1["id"], "division_id": None},
            role="league_admin")
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
