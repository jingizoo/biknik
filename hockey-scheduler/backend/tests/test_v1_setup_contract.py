"""v1 /api/setup contract stays byte-compatible after the C1b rename (#233).

The domain + facade are canonical (Program/League with program_id /
operator_organization_id / competition league_id), but the v1 HTTP API must be
unchanged. The v1_setup_adapter maps canonical results back to the legacy v1
keys. These tests drive the real v1 setup + reassign routes and assert the JSON
response carries the LEGACY keys (organization_id; season league_id; division
level_id; team league_id; venue league_id + organization_id) and never the
canonical ones — i.e. the pre-C1b contract.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)


class V1SetupContractTest(unittest.TestCase):
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

    def _create(self, c, entity, body):
        status, resp = self._req(c, "POST", f"/api/setup/{entity}", body)
        self.assertEqual(status, 200, (entity, resp))
        self.assertNotIn("error", resp, (entity, resp))
        return resp

    def test_v1_setup_responses_use_legacy_keys(self):
        c = self._admin()

        org = self._create(c, "organization", {"name": "Canlon", "short_name": "CAN"})

        # /api/setup/league create → v1 "league": organization_id, NOT
        # operator_organization_id.
        league = self._create(c, "league",
                              {"name": "Over 55", "country": "US",
                               "organization_id": org["id"]})
        self.assertIn("organization_id", league)
        self.assertEqual(league["organization_id"], org["id"])
        self.assertNotIn("operator_organization_id", league)

        # season body league_id → response league_id, NOT program_id.
        season = self._create(c, "season",
                              {"league_id": league["id"], "name": "Fall 2026"})
        self.assertIn("league_id", season)
        self.assertEqual(season["league_id"], league["id"])
        self.assertNotIn("program_id", season)

        # /api/setup/level create → the new League presented as a v1 "level".
        level = self._create(c, "level",
                             {"season_id": season["id"], "name": "Level 1",
                              "sort_order": 1})
        self.assertEqual(level["season_id"], season["id"])
        self.assertEqual(level["sort_order"], 1)

        # division body level_id → response level_id, NOT league_id.
        division = self._create(c, "division",
                                {"season_id": season["id"], "name": "Div A",
                                 "level_id": level["id"]})
        self.assertIn("level_id", division)
        self.assertEqual(division["level_id"], level["id"])
        self.assertNotIn("league_id", division)

        club = self._create(c, "club", {"name": "Club X"})

        # team body league_id → response league_id, NOT program_id.
        team = self._create(c, "team",
                            {"club_id": club["id"], "name": "Team X",
                             "league_id": league["id"]})
        self.assertIn("league_id", team)
        self.assertEqual(team["league_id"], league["id"])
        self.assertNotIn("program_id", team)

        # venue keeps BOTH league_id and organization_id this slice (unchanged).
        venue = self._create(c, "venue",
                             {"name": "Plainfield", "league_id": league["id"],
                              "organization_id": org["id"]})
        self.assertEqual(venue["league_id"], league["id"])
        self.assertEqual(venue["organization_id"], org["id"])

        # Reassign routes stay v1: division/assign-level → level_id;
        # league/assign-organization → organization_id.
        status, moved = self._req(
            c, "POST", f"/api/setup/division/{division['id']}/assign-level",
            {"level_id": level["id"]})
        self.assertEqual(status, 200, moved)
        self.assertIn("level_id", moved)
        self.assertNotIn("league_id", moved)

        org2 = self._create(c, "organization", {"name": "Other", "short_name": "OTH"})
        # (assigning a new operator while a venue is attached is refused by the
        # invariant; unassign the venue first so the reassign succeeds cleanly.)
        self._req(c, "POST", f"/api/setup/venue/{venue['id']}/assign-league",
                  {"league_id": None})
        status, reorg = self._req(
            c, "POST", f"/api/setup/league/{league['id']}/assign-organization",
            {"organization_id": org2["id"]})
        self.assertEqual(status, 200, reorg)
        self.assertIn("organization_id", reorg)
        self.assertEqual(reorg["organization_id"], org2["id"])
        self.assertNotIn("operator_organization_id", reorg)

    def test_v1_program_teams_read_route_uses_legacy_key(self):
        # GET /api/setup/leagues/{id}/teams (permanent program teams) must return
        # each team under its legacy v1 key league_id, never the canonical
        # program_id — this read route backs the frontend permanent-team panel.
        c = self._admin()
        org = self._create(c, "organization", {"name": "Owner", "short_name": "OWN"})
        league = self._create(c, "league",
                              {"name": "Read Program", "organization_id": org["id"]})
        club = self._create(c, "club", {"name": "Read Club"})
        self._create(c, "team",
                     {"club_id": club["id"], "name": "Read Team",
                      "league_id": league["id"]})
        status, body = self._req(
            c, "GET", f"/api/setup/leagues/{league['id']}/teams")
        self.assertEqual(status, 200, body)
        rows = body["teams"]
        self.assertEqual(len(rows), 1, body)
        self.assertEqual(rows[0]["league_id"], league["id"])
        self.assertNotIn("program_id", rows[0])


if __name__ == "__main__":
    unittest.main()
