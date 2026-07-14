"""The canonical v2 /api/v2/setup contract (#233 Slice C2).

Where the v1 surface (`test_v1_setup_contract.py`) proves the legacy keys are
preserved and the canonical ones are hidden, this proves the *opposite* for v2:
every v2 route returns CANONICAL keys directly (program_id /
operator_organization_id / competition league_id), with NO legacy aliases
(organization_id-on-program, league_id-on-season, level_id-on-division,
league_id-on-team). All over real HTTP against the running server, so the v2
dispatch + authz + facade wiring is exercised end to end.

Also covers the canonical validation rules that only v2 enforces: League is
REQUIRED on division / registration / game; Division is OPTIONAL; and a
League from a different Season is rejected.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

# Canonical v2 JSON key sets (contrast with the legacy sets in the v1 contract).
PROGRAM_KEYS = {"id", "name", "country", "timezone", "operator_organization_id",
                "external_ref"}
SEASON_KEYS = {"id", "program_id", "name", "start_date", "end_date",
               "external_ref"}
LEAGUE_KEYS = {"id", "season_id", "name", "sort_order", "external_ref"}
DIVISION_KEYS = {"id", "season_id", "name", "age_group", "league_id",
                 "external_ref"}
TEAM_KEYS = {"id", "name", "division", "club_id", "division_id", "external_ref",
             "program_id"}
# Canonical Venue is Organization-owned only: the legacy league_id is stripped
# from every v2 Venue response (#233 Slice C2).
VENUE_KEYS = {"id", "name", "address", "timezone", "organization_id",
              "external_ref"}
REGISTRATION_KEYS = {"id", "season_id", "team_id", "division_id", "league_id",
                     "active"}
GAME_KEYS = {"id", "home_team_id", "start_time", "target_goalies",
             "target_skaters", "max_skaters", "away_team_id", "rink", "end_time",
             "roster_lock_time", "locked", "cancelled", "published", "season_id",
             "division_id", "ice_slot_id", "is_draft", "league_id"}


class V2SetupContractTest(unittest.TestCase):
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

    def _v2(self, c, entity, body):
        status, resp = self._req(c, "POST", f"/api/v2/setup/{entity}", body)
        self.assertEqual(status, 200, (entity, resp))
        self.assertNotIn("error", resp, (entity, resp))
        return resp

    # -- exact canonical key sets, end to end -------------------------------
    def test_v2_end_to_end_canonical_keys(self):
        c = self._admin()

        org = self._v2(c, "organization", {"name": "Twin Rinks", "short_name": "TR"})

        # program: operator_organization_id, NOT organization_id.
        program = self._v2(c, "program",
                           {"name": "Adult Men", "country": "US",
                            "operator_organization_id": org["id"]})
        self.assertEqual(set(program), PROGRAM_KEYS, program)
        self.assertEqual(program["operator_organization_id"], org["id"])
        self.assertNotIn("organization_id", program)

        # season: program_id, NOT league_id.
        season = self._v2(c, "season",
                         {"program_id": program["id"], "name": "Fall 2026"})
        self.assertEqual(set(season), SEASON_KEYS, season)
        self.assertEqual(season["program_id"], program["id"])
        self.assertNotIn("league_id", season)

        # league (the grouping): season_id.
        league = self._v2(c, "league",
                         {"season_id": season["id"], "name": "Diamond",
                          "sort_order": 1})
        self.assertEqual(set(league), LEAGUE_KEYS, league)
        self.assertEqual(league["season_id"], season["id"])
        self.assertEqual(league["sort_order"], 1)

        # division: parented by league_id (canonical), NOT level_id; season
        # derived from the league.
        division = self._v2(c, "division",
                           {"league_id": league["id"], "name": "Div A",
                            "age_group": "adult"})
        self.assertEqual(set(division), DIVISION_KEYS, division)
        self.assertEqual(division["league_id"], league["id"])
        self.assertEqual(division["season_id"], season["id"])
        self.assertNotIn("level_id", division)

        club = self._v2(c, "club", {"name": "Host Club"})

        # team: program_id, NOT league_id. (Club stays required until Slice D
        # makes it truly optional; the v2 body accepts the key but C2 still
        # validates a real club.)
        team_a = self._v2(c, "team",
                         {"program_id": program["id"], "club_id": club["id"],
                          "name": "Rangers"})
        self.assertEqual(set(team_a), TEAM_KEYS, team_a)
        self.assertEqual(team_a["program_id"], program["id"])
        self.assertNotIn("league_id", team_a)

        team_b = self._v2(c, "team",
                         {"program_id": program["id"], "club_id": club["id"],
                          "name": "Kings"})
        self.assertEqual(set(team_b), TEAM_KEYS, team_b)
        self.assertEqual(team_b["club_id"], club["id"])

        # venue: v2 never carries league_id in the request OR the response —
        # canonical Venue is org-owned only; league_id is stripped entirely.
        venue = self._v2(c, "venue",
                        {"name": "Main Arena", "organization_id": org["id"]})
        self.assertEqual(set(venue), VENUE_KEYS, venue)
        self.assertEqual(venue["organization_id"], org["id"])
        self.assertNotIn("league_id", venue)

        # For the game's schedulable ice, the venue must be linked to the program
        # (the league-ice isolation guard). That temporary v1 venue↔program link
        # is v1-only (removed in Slice E), so use the v1 venue route for it.
        status, game_venue = self._req(c, "POST", "/api/setup/venue",
                                       {"name": "Game Arena",
                                        "league_id": program["id"],
                                        "organization_id": org["id"]})
        self.assertEqual(status, 200, game_venue)
        rink = self._v2(c, "rink", {"venue_id": game_venue["id"], "name": "Rink 1"})
        slot = self._v2(c, "ice-slot",
                       {"rink_id": rink["id"],
                        "start_time": "2026-09-01T18:30:00+00:00",
                        "end_time": "2026-09-01T20:00:00+00:00",
                        "slot_type": "game"})

        # register both teams WITH league_id (canonical registration keeps it).
        regs = {}
        for tm in (team_a, team_b):
            status, reg = self._req(
                c, "POST",
                f"/api/v2/setup/seasons/{season['id']}/team-registrations",
                {"team_id": tm["id"], "league_id": league["id"],
                 "division_id": division["id"]})
            self.assertEqual(status, 200, reg)
            self.assertEqual(set(reg), REGISTRATION_KEYS, reg)
            self.assertEqual(reg["league_id"], league["id"])
            regs[tm["id"]] = reg

        # GET registrations list also keeps league_id on every row.
        status, listing = self._req(
            c, "GET", f"/api/v2/setup/seasons/{season['id']}/team-registrations")
        self.assertEqual(status, 200, listing)
        self.assertEqual(set(listing), {"registrations"}, listing)
        self.assertEqual(set(listing["registrations"][0]), REGISTRATION_KEYS)
        self.assertTrue(all("league_id" in r for r in listing["registrations"]))

        # game: league_id REQUIRED and present in the canonical response.
        status, game = self._req(c, "POST", "/api/v2/setup/game",
                                 {"season_id": season["id"],
                                  "league_id": league["id"],
                                  "division_id": division["id"],
                                  "home_team_id": team_a["id"],
                                  "away_team_id": team_b["id"],
                                  "ice_slot_id": slot["id"]})
        self.assertEqual(status, 200, game)
        self.assertEqual(set(game), GAME_KEYS, game)
        self.assertEqual(game["league_id"], league["id"])

    # -- hierarchy shape ----------------------------------------------------
    def test_v2_hierarchy_is_canonical(self):
        c = self._admin()
        org = self._v2(c, "organization", {"name": "H Org", "short_name": "HO"})
        program = self._v2(c, "program",
                          {"name": "H Program",
                           "operator_organization_id": org["id"]})
        season = self._v2(c, "season",
                        {"program_id": program["id"], "name": "S"})
        league = self._v2(c, "league", {"season_id": season["id"], "name": "L"})
        self._v2(c, "division", {"league_id": league["id"], "name": "D"})

        status, body = self._req(c, "GET", "/api/v2/setup/hierarchy")
        self.assertEqual(status, 200, body)
        self.assertIn("programs", body)
        # Locate our program in the canonical tree.
        prog = next(p for p in body["programs"] if p["id"] == program["id"])
        self.assertIn("operator_organization_id", prog)
        self.assertEqual(prog["operator_organization_id"], org["id"])
        s = next(x for x in prog["seasons"] if x["id"] == season["id"])
        lg = next(x for x in s["leagues"] if x["id"] == league["id"])
        self.assertEqual(len(lg["divisions"]), 1)
        # Canonical vocabulary only — no legacy key names anywhere in the tree.
        blob = json.dumps(body)
        self.assertNotIn("level_id", blob)
        # program_id appears (canonical) but the legacy season-level "league_id"
        # meaning-of-program is gone — the season carries program_id.
        self.assertNotIn('"level"', blob)

    # -- assign-league + assign-division ------------------------------------
    def test_v2_assign_league_and_division(self):
        c = self._admin()
        org = self._v2(c, "organization", {"name": "A Org", "short_name": "AO"})
        program = self._v2(c, "program",
                          {"name": "A Prog", "operator_organization_id": org["id"]})
        season = self._v2(c, "season",
                        {"program_id": program["id"], "name": "S"})
        league1 = self._v2(c, "league", {"season_id": season["id"], "name": "L1"})
        league2 = self._v2(c, "league", {"season_id": season["id"], "name": "L2"})
        div1 = self._v2(c, "division", {"league_id": league1["id"], "name": "D1"})
        club = self._v2(c, "club", {"name": "C"})
        team = self._v2(c, "team",
                       {"program_id": program["id"], "club_id": club["id"],
                        "name": "T"})

        status, reg = self._req(
            c, "POST",
            f"/api/v2/setup/seasons/{season['id']}/team-registrations",
            {"team_id": team["id"], "league_id": league1["id"]})
        self.assertEqual(status, 200, reg)
        self.assertEqual(reg["league_id"], league1["id"])
        self.assertIsNone(reg["division_id"])  # division optional

        # reassign the registration to another league (no division constraint).
        status, moved = self._req(
            c, "POST",
            f"/api/v2/setup/season-team-registration/{reg['id']}/assign-league",
            {"league_id": league2["id"]})
        self.assertEqual(status, 200, moved)
        self.assertEqual(set(moved), REGISTRATION_KEYS, moved)
        self.assertEqual(moved["league_id"], league2["id"])

        # assign a division that belongs to league1 → move back is consistent.
        status, back = self._req(
            c, "POST",
            f"/api/v2/setup/season-team-registration/{reg['id']}/assign-league",
            {"league_id": league1["id"]})
        self.assertEqual(status, 200, back)
        status, divd = self._req(
            c, "POST",
            f"/api/v2/setup/season-team-registration/{reg['id']}/assign-division",
            {"division_id": div1["id"]})
        self.assertEqual(status, 200, divd)
        self.assertEqual(divd["division_id"], div1["id"])
        self.assertEqual(divd["league_id"], league1["id"])

        # division reassign under the new tree: division→league.
        status, dmoved = self._req(
            c, "POST", f"/api/v2/setup/division/{div1['id']}/assign-league",
            {"league_id": league1["id"]})
        self.assertEqual(status, 200, dmoved)
        self.assertEqual(set(dmoved), DIVISION_KEYS, dmoved)
        self.assertNotIn("level_id", dmoved)

    # -- program operator reassignment (#233 Slice C2 review) ---------------
    def test_v2_program_assign_organization(self):
        c = self._admin()
        org1 = self._v2(c, "organization", {"name": "Org1", "short_name": "O1"})
        org2 = self._v2(c, "organization", {"name": "Org2", "short_name": "O2"})
        program = self._v2(c, "program",
                          {"name": "P", "operator_organization_id": org1["id"]})
        self.assertEqual(program["operator_organization_id"], org1["id"])

        # Canonical operator move via the new v2 route → canonical Program back.
        status, moved = self._req(
            c, "POST",
            f"/api/v2/setup/program/{program['id']}/assign-organization",
            {"operator_organization_id": org2["id"]})
        self.assertEqual(status, 200, moved)
        self.assertEqual(set(moved), PROGRAM_KEYS, moved)
        self.assertEqual(moved["operator_organization_id"], org2["id"])
        self.assertNotIn("organization_id", moved)  # canonical, not legacy v1

        # The temporary Venue-link safety rule is preserved: attach a venue to
        # the program (v1-only link) and the operator can no longer change.
        status, _venue = self._req(c, "POST", "/api/setup/venue",
                                   {"name": "Arena", "league_id": program["id"],
                                    "organization_id": org2["id"]})
        self.assertEqual(status, 200, _venue)
        status, blocked = self._req(
            c, "POST",
            f"/api/v2/setup/program/{program['id']}/assign-organization",
            {"operator_organization_id": org1["id"]})
        self.assertEqual(blocked["error"]["code"], "validation_error", blocked)
        # Zero mutation — the operator is still org2.
        status, prog2 = self._req(c, "GET", "/api/v2/setup/hierarchy")
        p = next(x for x in prog2["programs"] if x["id"] == program["id"])
        self.assertEqual(p["operator_organization_id"], org2["id"])

    # -- deletes ------------------------------------------------------------
    def test_v2_deletes_canonical(self):
        c = self._admin()
        org = self._v2(c, "organization", {"name": "D Org", "short_name": "DO"})
        program = self._v2(c, "program",
                          {"name": "D Prog", "operator_organization_id": org["id"]})
        season = self._v2(c, "season",
                        {"program_id": program["id"], "name": "S"})
        league = self._v2(c, "league", {"season_id": season["id"], "name": "L"})
        division = self._v2(c, "division",
                          {"league_id": league["id"], "name": "D"})

        # league-delete = the grouping League (canonical shape returned).
        status, deleted = self._req(
            c, "POST", f"/api/v2/setup/division/{division['id']}/delete", {})
        self.assertEqual(status, 200, deleted)
        self.assertEqual(set(deleted), DIVISION_KEYS, deleted)

        status, dleague = self._req(
            c, "POST", f"/api/v2/setup/league/{league['id']}/delete", {})
        self.assertEqual(status, 200, dleague)
        self.assertEqual(set(dleague), LEAGUE_KEYS, dleague)

        # program-delete = the umbrella (canonical program shape).
        status, dprog = self._req(
            c, "POST", f"/api/v2/setup/season/{season['id']}/delete", {})
        self.assertEqual(status, 200, dprog)
        status, dprogram = self._req(
            c, "POST", f"/api/v2/setup/program/{program['id']}/delete", {})
        self.assertEqual(status, 200, dprogram)
        self.assertEqual(set(dprogram), PROGRAM_KEYS, dprogram)
        self.assertNotIn("organization_id", dprogram)

    # -- canonical validation: League required, Division optional -----------
    def test_v2_division_requires_league(self):
        c = self._admin()
        status, resp = self._req(c, "POST", "/api/v2/setup/division",
                                 {"name": "No League"})
        self.assertEqual(status, 400, resp)
        self.assertEqual(resp["error"]["code"], "validation_error", resp)

    def test_v2_registration_requires_league(self):
        c = self._admin()
        org = self._v2(c, "organization", {"name": "R Org", "short_name": "RO"})
        program = self._v2(c, "program",
                          {"name": "R Prog", "operator_organization_id": org["id"]})
        season = self._v2(c, "season",
                        {"program_id": program["id"], "name": "S"})
        club = self._v2(c, "club", {"name": "C"})
        team = self._v2(c, "team",
                       {"program_id": program["id"], "club_id": club["id"],
                        "name": "T"})
        status, resp = self._req(
            c, "POST",
            f"/api/v2/setup/seasons/{season['id']}/team-registrations",
            {"team_id": team["id"]})
        self.assertEqual(status, 400, resp)
        self.assertEqual(resp["error"]["code"], "validation_error", resp)

    def test_v2_game_requires_league(self):
        c = self._admin()
        env = self._playable(c)
        status, resp = self._req(c, "POST", "/api/v2/setup/game",
                                 {"season_id": env["season"]["id"],
                                  "division_id": env["division"]["id"],
                                  "home_team_id": env["team_a"]["id"],
                                  "away_team_id": env["team_b"]["id"],
                                  "ice_slot_id": env["slot"]["id"]})
        self.assertEqual(status, 400, resp)
        self.assertEqual(resp["error"]["code"], "validation_error", resp)

    def test_v2_division_optional_on_registration_and_game(self):
        c = self._admin()
        env = self._playable(c)
        # Register both teams with league but NO division.
        for tm in (env["team_a"], env["team_b"]):
            status, reg = self._req(
                c, "POST",
                f"/api/v2/setup/seasons/{env['season']['id']}/team-registrations",
                {"team_id": tm["id"], "league_id": env["league"]["id"]})
            self.assertEqual(status, 200, reg)
            self.assertIsNone(reg["division_id"])

        # Game with league but NO division — allowed in v2.
        status, game = self._req(c, "POST", "/api/v2/setup/game",
                                 {"season_id": env["season"]["id"],
                                  "league_id": env["league"]["id"],
                                  "home_team_id": env["team_a"]["id"],
                                  "away_team_id": env["team_b"]["id"],
                                  "ice_slot_id": env["slot"]["id"]})
        self.assertEqual(status, 200, game)
        self.assertIsNone(game["division_id"])
        self.assertEqual(game["league_id"], env["league"]["id"])

    def test_v2_cross_season_league_rejected(self):
        c = self._admin()
        env = self._playable(c)
        # A league under a DIFFERENT season of the same program.
        other_season = self._v2(c, "season",
                               {"program_id": env["program"]["id"], "name": "S2"})
        other_league = self._v2(c, "league",
                              {"season_id": other_season["id"], "name": "OL"})
        status, resp = self._req(
            c, "POST",
            f"/api/v2/setup/seasons/{env['season']['id']}/team-registrations",
            {"team_id": env["team_a"]["id"], "league_id": other_league["id"]})
        self.assertEqual(status, 400, resp)
        self.assertEqual(resp["error"]["code"], "validation_error", resp)

    def _playable(self, c):
        """A full canonical hierarchy + a bookable game ice slot (no regs yet)."""
        org = self._v2(c, "organization", {"name": "P Org", "short_name": "PO"})
        program = self._v2(c, "program",
                          {"name": "P Prog", "operator_organization_id": org["id"]})
        season = self._v2(c, "season",
                        {"program_id": program["id"], "name": "S"})
        league = self._v2(c, "league", {"season_id": season["id"], "name": "L"})
        division = self._v2(c, "division",
                          {"league_id": league["id"], "name": "D"})
        club = self._v2(c, "club", {"name": "C"})
        team_a = self._v2(c, "team",
                        {"program_id": program["id"], "club_id": club["id"],
                         "name": "A"})
        team_b = self._v2(c, "team",
                        {"program_id": program["id"], "club_id": club["id"],
                         "name": "B"})
        # Venue must be linked to the program for the game ice to be schedulable
        # (the league-ice isolation guard). The v2 venue route omits league_id,
        # so use the v1 venue create for the owner+program link this test needs.
        status, venue = self._req(c, "POST", "/api/setup/venue",
                                  {"name": "V", "league_id": program["id"],
                                   "organization_id": org["id"]})
        self.assertEqual(status, 200, venue)
        rink = self._v2(c, "rink", {"venue_id": venue["id"], "name": "R"})
        slot = self._v2(c, "ice-slot",
                       {"rink_id": rink["id"],
                        "start_time": "2026-09-01T18:30:00+00:00",
                        "end_time": "2026-09-01T20:00:00+00:00",
                        "slot_type": "game"})
        return {"org": org, "program": program, "season": season,
                "league": league, "division": division, "team_a": team_a,
                "team_b": team_b, "slot": slot}


if __name__ == "__main__":
    unittest.main()
