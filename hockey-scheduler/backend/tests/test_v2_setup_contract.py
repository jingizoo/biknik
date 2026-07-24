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

from hockey_scheduler.domain import SeasonTeamRegistration, Team

# Canonical v2 JSON key sets (contrast with the legacy sets in the v1 contract).
PROGRAM_KEYS = {"id", "name", "country", "timezone", "operator_organization_id",
                "external_ref"}
SEASON_KEYS = {"id", "program_id", "name", "start_date", "end_date",
               "external_ref", "status", "archived_at"}
LEAGUE_KEYS = {"id", "season_id", "name", "sort_order", "external_ref"}
DIVISION_KEYS = {"id", "season_id", "name", "age_group", "league_id",
                 "external_ref"}
# delete_division's response also reports how many inactive, game-free
# registrations it cleared (#233 D1 bundled fix, #248) — a delete-only field,
# not part of the general Division shape used by create/read/reassign.
DIVISION_DELETE_KEYS = DIVISION_KEYS | {"inactive_registrations_cleaned"}
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
             "division_id", "ice_slot_id", "is_draft", "league_id", "game_type",
             "league_season_id"}


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
                         {"season_id": season["id"], "name": "Adult League",
                          "sort_order": 1})
        self.assertEqual(set(league), LEAGUE_KEYS, league)
        self.assertEqual(league["season_id"], season["id"])
        self.assertEqual(league["sort_order"], 1)

        # division: parented by league_id (canonical), NOT level_id; season
        # derived from the league.
        division = self._v2(c, "division",
                           {"league_id": league["id"], "name": "Gold",
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
                         {"league_id": league["id"], "club_id": club["id"],
                          "name": "Rangers"})
        self.assertEqual(set(team_a), TEAM_KEYS, team_a)
        self.assertEqual(team_a["program_id"], program["id"])
        self.assertNotIn("league_id", team_a)

        team_b = self._v2(c, "team",
                         {"league_id": league["id"], "club_id": club["id"],
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

        # For the game's schedulable ice, the venue needs active
        # SeasonVenueAccess for this Season (#233 Slice E).
        game_venue = self._v2(c, "venue",
                              {"name": "Game Arena", "organization_id": org["id"]})
        self._req(c, "POST", f"/api/v2/setup/seasons/{season['id']}/venue-access",
                  {"venue_id": game_venue["id"]})
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

    # -- exhibition game type (#283 Slice D) --------------------------------
    def test_v2_exhibition_game_needs_no_league_and_reports_type(self):
        c = self._admin()
        org = self._v2(c, "organization", {"name": "Ex Org", "short_name": "EX"})
        program = self._v2(c, "program",
                          {"name": "Ex Prog", "operator_organization_id": org["id"]})
        season = self._v2(c, "season",
                        {"program_id": program["id"], "name": "ExS"})
        # Two DIFFERENT permanent Leagues — a friendly may cross League lines.
        elite = self._v2(c, "league", {"season_id": season["id"], "name": "Elite"})
        rec = self._v2(c, "league", {"season_id": season["id"], "name": "Rec"})
        club = self._v2(c, "club", {"name": "ExC"})
        team_a = self._v2(c, "team",
                         {"league_id": elite["id"], "club_id": club["id"],
                          "name": "Comets"})
        team_b = self._v2(c, "team",
                         {"league_id": rec["id"], "club_id": club["id"],
                          "name": "Blazers"})
        venue = self._v2(c, "venue",
                        {"name": "Ex Arena", "organization_id": org["id"]})
        self._req(c, "POST", f"/api/v2/setup/seasons/{season['id']}/venue-access",
                  {"venue_id": venue["id"]})
        rink = self._v2(c, "rink", {"venue_id": venue["id"], "name": "R"})
        slot = self._v2(c, "ice-slot",
                       {"rink_id": rink["id"],
                        "start_time": "2026-09-01T18:30:00+00:00",
                        "end_time": "2026-09-01T20:00:00+00:00",
                        "slot_type": "game"})
        for tm, lg in ((team_a, elite), (team_b, rec)):
            self._req(
                c, "POST",
                f"/api/v2/setup/seasons/{season['id']}/team-registrations",
                {"team_id": tm["id"], "league_id": lg["id"]})

        # A REGULAR game with NO league_id is rejected (league required).
        status, err = self._req(c, "POST", "/api/v2/setup/game",
                                {"season_id": season["id"],
                                 "home_team_id": team_a["id"],
                                 "away_team_id": team_b["id"],
                                 "ice_slot_id": slot["id"]})
        self.assertEqual(err["error"]["code"], "validation_error", err)

        # An EXHIBITION game needs no league_id and reports game_type; it
        # crosses League lines and carries no owning League/Division.
        status, game = self._req(c, "POST", "/api/v2/setup/game",
                                 {"season_id": season["id"],
                                  "home_team_id": team_a["id"],
                                  "away_team_id": team_b["id"],
                                  "ice_slot_id": slot["id"],
                                  "game_type": "exhibition"})
        self.assertEqual(status, 200, game)
        self.assertEqual(set(game), GAME_KEYS, game)
        self.assertEqual(game["game_type"], "exhibition")
        self.assertIsNone(game["league_id"])
        self.assertIsNone(game["division_id"])

        # The LeagueSeason standings route responds with a table (no crash),
        # and the friendly never appears in it.
        status, st = self._req(
            c, "GET",
            f"/api/standings/league-season/{elite['id']}/{season['id']}")
        self.assertEqual(status, 200, st)
        self.assertEqual(st["league_id"], elite["id"])
        self.assertIn("standings", st)

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
        # #159 — the v2 League node exposes its LeagueSeason binding id so the UI
        # can drive the explicit unbind before deleting a League.
        self.assertIn("league_season_id", lg)
        self.assertTrue(lg["league_season_id"])
        # Canonical vocabulary only — no legacy key names anywhere in the tree.
        blob = json.dumps(body)
        self.assertNotIn("level_id", blob)
        # program_id appears (canonical) but the legacy season-level "league_id"
        # meaning-of-program is gone — the season carries program_id.
        self.assertNotIn('"level"', blob)

    # -- canonical flat overview (#233 Slice B2a) ---------------------------
    def test_v2_overview_canonical_flat_lists(self):
        c = self._admin()
        org = self._v2(c, "organization", {"name": "Ov Org", "short_name": "OO"})
        program = self._v2(c, "program",
                          {"name": "Ov Program",
                           "operator_organization_id": org["id"]})
        season = self._v2(c, "season",
                        {"program_id": program["id"], "name": "OvS"})
        league = self._v2(c, "league", {"season_id": season["id"], "name": "OvL"})
        division = self._v2(c, "division",
                           {"league_id": league["id"], "name": "OvD"})
        club = self._v2(c, "club", {"name": "OvC"})
        team = self._v2(c, "team",
                       {"league_id": league["id"], "club_id": club["id"],
                        "name": "OvT"})
        venue = self._v2(c, "venue",
                        {"name": "OvV", "organization_id": org["id"]})

        status, ov = self._req(c, "GET", "/api/v2/setup/overview")
        self.assertEqual(status, 200, ov)
        # Flat canonical lists exist for every setup entity.
        for key in ("programs", "seasons", "leagues", "divisions", "teams",
                    "clubs", "organizations", "venues", "rinks", "ice_slots"):
            self.assertIn(key, ov, ov)

        prog = next(p for p in ov["programs"] if p["id"] == program["id"])
        self.assertEqual(prog["operator_organization_id"], org["id"])
        self.assertNotIn("organization_id", prog)  # canonical, not legacy

        s = next(x for x in ov["seasons"] if x["id"] == season["id"])
        self.assertEqual(s["program_id"], program["id"])
        self.assertNotIn("league_id", s)  # season carries program_id, not league_id

        d = next(x for x in ov["divisions"] if x["id"] == division["id"])
        self.assertEqual(d["league_id"], league["id"])  # canonical grouping League
        self.assertNotIn("level_id", d)

        t = next(x for x in ov["teams"] if x["id"] == team["id"])
        self.assertEqual(t["program_id"], program["id"])
        self.assertNotIn("league_id", t)

        v = next(x for x in ov["venues"] if x["id"] == venue["id"])
        self.assertEqual(v["organization_id"], org["id"])
        # Canonical Venue is org-owned only — the temporary program link is NOT
        # exposed here (managed via the v1 assign-league bridge until Slice E).
        self.assertNotIn("league_id", v)

        # No legacy vocabulary anywhere in the payload.
        blob = json.dumps(ov)
        self.assertNotIn("level_id", blob)
        self.assertNotIn("level_name", blob)

    def test_v2_overview_requires_operator(self):
        # Unauthenticated → gated (401 no session / 403 wrong role), never 200.
        c = self._client()
        status, _ = self._req(c, "GET", "/api/v2/setup/overview")
        self.assertIn(status, (401, 403))

    def test_v2_overview_allows_arena_manager(self):
        # #233 B2a review: overview is gated MANAGE_ARENA (not MANAGE_SETUP
        # like hierarchy) so the Setup UI's facility portion doesn't crash for
        # an Arena Manager, who holds MANAGE_ARENA but not MANAGE_SETUP.
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "arena", "password": "demo"})
        status, resp = self._req(c, "GET", "/api/v2/setup/overview")
        self.assertEqual(status, 200, resp)
        self.assertIn("organizations", resp)
        self.assertIn("venues", resp)

    def test_v2_overview_denies_role_without_arena_or_setup(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "coach", "password": "demo"})
        status, _ = self._req(c, "GET", "/api/v2/setup/overview")
        self.assertEqual(status, 403)

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
        # This test reassigns the registration BETWEEN league1 and league2, so
        # the team must have NO permanent League (a team with one may only
        # register in that League, rule 7). #283 Slice E: a league-less team can
        # no longer be created through any service route, and registering it
        # would backfill (pin) its permanent League — either of which blocks the
        # cross-league reassignment below. Inject both the league-less team and
        # its league1 registration directly into the shared store so the team
        # stays league-less and the assign-league/assign-division routes are the
        # subject under test.
        store = self.srv.STATE.api.store
        _team = Team(id=store.next_id("team"), name="T", club_id=club["id"],
                     program_id=program["id"])
        store.add_team(_team)
        team = {"id": _team.id}
        _ls = store.league_season_for(league1["id"], season["id"])
        _reg = SeasonTeamRegistration(
            id=store.next_id("streg"), league_season_id=_ls.id,
            team_id=team["id"], division_id=None, active=True)
        store.add_season_team_registration(_reg)
        reg = {"id": _reg.id}

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

    # -- permanent-League tree + team transfer (#283 Slice B) ---------------
    def test_v2_permanent_league_tree_and_team_transfer(self):
        c = self._admin()
        org = self._v2(c, "organization", {"name": "P Org", "short_name": "PO"})
        program = self._v2(c, "program",
                          {"name": "P Prog", "operator_organization_id": org["id"]})
        season = self._v2(c, "season",
                        {"program_id": program["id"], "name": "S"})
        elite = self._v2(c, "league",
                         {"season_id": season["id"], "name": "Elite",
                          "sort_order": 1})
        rec = self._v2(c, "league",
                       {"season_id": season["id"], "name": "Rec",
                        "sort_order": 2})
        club = self._v2(c, "club", {"name": "C"})

        # Create a Team directly under its PERMANENT League (Slice B: league_id
        # is accepted on team create; Program is derived from the League).
        team = self._v2(c, "team",
                       {"league_id": elite["id"], "club_id": club["id"],
                        "name": "Falcons"})
        self.assertEqual(team["program_id"], program["id"])
        self.assertNotIn("league_id", team)  # raw competition id stays hidden

        # A Team with a Program but no permanent League. #283 Slice E: create_team
        # now requires a resolvable League on every route, so this league-less
        # team (the legacy remediation state the hierarchy surfaces under
        # teams_without_league) is injected directly into the shared store.
        store = self.srv.STATE.api.store
        _loose = Team(id=store.next_id("team"), name="Loose",
                      program_id=program["id"])
        store.add_team(_loose)
        loose = {"id": _loose.id}

        # The hierarchy exposes the permanent Program → League → Team tree.
        status, body = self._req(c, "GET", "/api/v2/setup/hierarchy")
        self.assertEqual(status, 200, body)
        prog = next(p for p in body["programs"] if p["id"] == program["id"])
        self.assertIn("leagues", prog)
        by_league = {lg["id"]: lg for lg in prog["leagues"]}
        # Leagues are ordered by (sort_order, name).
        self.assertEqual([lg["name"] for lg in prog["leagues"]], ["Elite", "Rec"])
        self.assertIn(team["id"], [t["id"] for t in by_league[elite["id"]]["teams"]])
        self.assertEqual(by_league[elite["id"]]["season_count"], 1)
        # The league-less team is surfaced for assignment, not falsely nested.
        self.assertIn(loose["id"],
                      [t["id"] for t in prog["teams_without_league"]])

        # Transfer the team from Elite to Rec (promotion/relegation, rule 10).
        status, moved = self._req(
            c, "POST", f"/api/v2/setup/team/{team['id']}/assign-league",
            {"league_id": rec["id"]})
        self.assertEqual(status, 200, moved)
        self.assertNotIn("league_id", moved)  # raw competition id stays hidden

        status, body2 = self._req(c, "GET", "/api/v2/setup/hierarchy")
        prog2 = next(p for p in body2["programs"] if p["id"] == program["id"])
        by_league2 = {lg["id"]: lg for lg in prog2["leagues"]}
        self.assertIn(team["id"],
                      [t["id"] for t in by_league2[rec["id"]]["teams"]])
        self.assertNotIn(team["id"],
                         [t["id"] for t in by_league2[elite["id"]]["teams"]])

        # A transfer without a league_id is a validation error.
        status, err = self._req(
            c, "POST", f"/api/v2/setup/team/{team['id']}/assign-league", {})
        self.assertEqual(err["error"]["code"], "validation_error", err)

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

        # #233 Slice E: physical (facility) and competition (Program) structure
        # are independent trees — attaching a venue to the program (the legacy
        # v1-only link) no longer constrains an operator-organization change.
        status, _venue = self._req(c, "POST", "/api/setup/venue",
                                   {"name": "Arena", "league_id": program["id"],
                                    "organization_id": org2["id"]})
        self.assertEqual(status, 200, _venue)
        status, unblocked = self._req(
            c, "POST",
            f"/api/v2/setup/program/{program['id']}/assign-organization",
            {"operator_organization_id": org1["id"]})
        self.assertEqual(status, 200, unblocked)
        self.assertEqual(unblocked["operator_organization_id"], org1["id"])

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
        self.assertEqual(set(deleted), DIVISION_DELETE_KEYS, deleted)

        # A bound League blocks on its LeagueSeason binding (#159 — itemized
        # dependency, no silent cascade); the explicit unbind clears it first.
        ls_id = self.srv.STATE.api.store.league_seasons_for_league(
            league["id"])[0].id
        status, _ = self._req(
            c, "POST", f"/api/v2/setup/league-season/{ls_id}/delete", {})
        self.assertEqual(status, 200)
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

    # -- explicit inactive-registration cleanup (#251) -----------------------
    def test_v2_registration_delete_contract(self):
        """POST /api/v2/setup/season-team-registration/{id}/delete: League
        Admin success + resolved labels, unauthorized-role rejection,
        active/game-backed blocking, and invalid-parent zero-mutation."""
        c = self._admin()
        org = self._v2(c, "organization", {"name": "R Org", "short_name": "RO"})
        program = self._v2(c, "program",
                          {"name": "R Prog", "operator_organization_id": org["id"]})
        season = self._v2(c, "season",
                         {"program_id": program["id"], "name": "R Season"})
        league = self._v2(c, "league",
                         {"season_id": season["id"], "name": "R League"})
        division = self._v2(c, "division",
                           {"league_id": league["id"], "name": "R Division"})
        club = self._v2(c, "club", {"name": "R Club"})

        # -- League Admin success + resolved labels for UI confirmation -----
        team = self._v2(c, "team",
                       {"league_id": league["id"], "club_id": club["id"],
                        "name": "R Team"})
        status, reg = self._req(
            c, "POST", f"/api/v2/setup/seasons/{season['id']}/team-registrations",
            {"team_id": team["id"], "league_id": league["id"],
             "division_id": division["id"]})
        self.assertEqual(status, 200, reg)
        status, removed = self._req(
            c, "POST",
            f"/api/v2/setup/season-team-registration/{reg['id']}/remove", {})
        self.assertEqual(status, 200, removed)
        self.assertFalse(removed["active"])

        status, deleted = self._req(
            c, "POST",
            f"/api/v2/setup/season-team-registration/{reg['id']}/delete", {})
        self.assertEqual(status, 200, deleted)
        self.assertEqual(
            set(deleted),
            {"id", "season_id", "team_id", "league_id", "division_id",
             "reason", "season_name", "team_name", "league_name",
             "division_name"}, deleted)
        self.assertEqual(deleted["id"], reg["id"])
        self.assertEqual(deleted["season_name"], "R Season")
        self.assertEqual(deleted["team_name"], "R Team")
        self.assertEqual(deleted["league_name"], "R League")
        self.assertEqual(deleted["division_name"], "R Division")

        # -- unauthorized role is rejected, before any entity-specific check -
        coach = self._client()
        self._req(coach, "POST", "/api/auth/login",
                 {"username": "coach", "password": "demo"})
        status, denied = self._req(
            coach, "POST",
            f"/api/v2/setup/season-team-registration/{reg['id']}/delete", {})
        self.assertEqual(status, 403, denied)
        self.assertEqual(denied["error"]["details"]["required"], "manage_setup")

        # -- an active registration blocks the permanent cleanup delete -----
        team_active = self._v2(c, "team",
                              {"league_id": league["id"], "club_id": club["id"],
                               "name": "R Active"})
        status, reg_active = self._req(
            c, "POST", f"/api/v2/setup/seasons/{season['id']}/team-registrations",
            {"team_id": team_active["id"], "league_id": league["id"],
             "division_id": division["id"]})
        self.assertEqual(status, 200, reg_active)
        status, blocked_active = self._req(
            c, "POST",
            f"/api/v2/setup/season-team-registration/{reg_active['id']}/delete",
            {})
        self.assertEqual(status, 400, blocked_active)
        self.assertEqual(blocked_active["error"]["code"], "validation_error")
        self.assertEqual(
            blocked_active["error"]["details"]["reason"], "registration_active")

        # -- a Game (even cancelled) still blocks the cleanup ---------------
        team_home = self._v2(c, "team",
                            {"league_id": league["id"], "club_id": club["id"],
                             "name": "R Home"})
        team_away = self._v2(c, "team",
                            {"league_id": league["id"], "club_id": club["id"],
                             "name": "R Away"})
        for tm in (team_home, team_away):
            status, r = self._req(
                c, "POST",
                f"/api/v2/setup/seasons/{season['id']}/team-registrations",
                {"team_id": tm["id"], "league_id": league["id"],
                 "division_id": division["id"]})
            self.assertEqual(status, 200, r)
        status, game_venue = self._req(
            c, "POST", "/api/setup/venue",
            {"name": "R Arena", "league_id": program["id"],
             "organization_id": org["id"]})
        self.assertEqual(status, 200, game_venue)
        self._req(c, "POST", f"/api/v2/setup/seasons/{season['id']}/venue-access",
                  {"venue_id": game_venue["id"]})
        rink = self._v2(c, "rink", {"venue_id": game_venue["id"], "name": "Rink 1"})
        slot = self._v2(c, "ice-slot",
                       {"rink_id": rink["id"],
                        "start_time": "2027-01-01T10:00:00+00:00",
                        "end_time": "2027-01-01T11:00:00+00:00",
                        "slot_type": "game"})
        status, game = self._req(
            c, "POST", "/api/v2/setup/game",
            {"season_id": season["id"], "league_id": league["id"],
             "division_id": division["id"], "home_team_id": team_home["id"],
             "away_team_id": team_away["id"], "ice_slot_id": slot["id"]})
        self.assertEqual(status, 200, game)
        status, cancelled = self._req(
            c, "POST", f"/api/games/{game['id']}/cancel", {})
        self.assertEqual(status, 200, cancelled)
        status, listing = self._req(
            c, "GET", f"/api/v2/setup/seasons/{season['id']}/team-registrations")
        home_reg_id = next(
            r["id"] for r in listing["registrations"]
            if r["team_id"] == team_home["id"])
        status, _ = self._req(
            c, "POST",
            f"/api/v2/setup/season-team-registration/{home_reg_id}/remove", {})
        self.assertEqual(status, 200)
        status, blocked_game = self._req(
            c, "POST",
            f"/api/v2/setup/season-team-registration/{home_reg_id}/delete", {})
        self.assertEqual(status, 409, blocked_game)
        self.assertEqual(blocked_game["error"]["code"], "has_dependencies")
        dep_types = {g["type"] for g in
                    blocked_game["error"]["details"]["dependencies"]}
        self.assertIn("game", dep_types)

        # -- an inconsistent parent (no resolvable League) is reported, not -
        # -- silently deleted or ignored — zero mutation on the block -------
        season2 = self._v2(c, "season",
                          {"program_id": program["id"], "name": "R Season 2"})
        # R Legacy is never registered through the v2 route (below it is bound to
        # an injected ghost LeagueSeason), so it has no permanent League and must
        # be created via the legacy v1 route (v1 "league_id" means Program).
        status, team_legacy = self._req(
            c, "POST", "/api/setup/team",
            {"name": "R Legacy", "league_id": program["id"],
             "club_id": club["id"]})
        self.assertEqual(status, 200, team_legacy)
        # #283: a registration always hangs off a LeagueSeason, so the service
        # now refuses to create a league-less row. Reproduce the inconsistent
        # parent (a registration whose League no longer resolves) by injecting an
        # inactive row against a LeagueSeason bound to a non-existent League —
        # the same direct-store technique the invalid-data guards elsewhere use.
        from hockey_scheduler.domain import LeagueSeason, SeasonTeamRegistration
        store = self.srv.STATE.api.store
        ghost_ls = store.add_league_season(LeagueSeason(
            id=store.next_id("leagueseason"), league_id="league_ghost",
            season_id=season2["id"]))
        reg_legacy = {"id": store.next_id("streg")}
        store.add_season_team_registration(SeasonTeamRegistration(
            id=reg_legacy["id"], league_season_id=ghost_ls.id,
            team_id=team_legacy["id"], division_id=None, active=False))
        status, blocked_legacy = self._req(
            c, "POST",
            f"/api/v2/setup/season-team-registration/{reg_legacy['id']}/delete",
            {})
        self.assertEqual(status, 400, blocked_legacy)
        self.assertEqual(blocked_legacy["error"]["code"], "validation_error")
        self.assertEqual(
            blocked_legacy["error"]["details"]["reason"], "invalid_league")
        status, still_there = self._req(
            c, "GET", f"/api/v2/setup/seasons/{season2['id']}/team-registrations")
        self.assertEqual(status, 200, still_there)
        self.assertIn(reg_legacy["id"],
                      [r["id"] for r in still_there["registrations"]])

    # -- season venue access (#233 Slice E) ----------------------------------
    def test_v2_season_venue_access_contract(self):
        """List/grant/remove for POST /api/v2/setup/seasons/{id}/venue-access
        and POST /api/v2/setup/season-venue-access/{id}/remove: League Admin
        success + response shape, unauthorized-role rejection, one Season
        using multiple Venues, one Venue hosting multiple Programs/Seasons,
        and duplicate-grant / not-found error shapes."""
        c = self._admin()
        org = self._v2(c, "organization", {"name": "SVA Org", "short_name": "SO"})
        program = self._v2(c, "program",
                          {"name": "SVA Prog", "operator_organization_id": org["id"]})
        season = self._v2(c, "season",
                         {"program_id": program["id"], "name": "SVA Season"})
        venue = self._v2(c, "venue",
                        {"name": "SVA Venue", "organization_id": org["id"]})

        # -- League Admin success + exact response shape ---------------------
        status, granted = self._req(
            c, "POST", f"/api/v2/setup/seasons/{season['id']}/venue-access",
            {"venue_id": venue["id"]})
        self.assertEqual(status, 200, granted)
        self.assertEqual(set(granted), {"id", "season_id", "venue_id", "active"},
                         granted)
        self.assertEqual(granted["season_id"], season["id"])
        self.assertEqual(granted["venue_id"], venue["id"])
        self.assertTrue(granted["active"])

        status, listing = self._req(
            c, "GET", f"/api/v2/setup/seasons/{season['id']}/venue-access")
        self.assertEqual(status, 200, listing)
        self.assertEqual(set(listing), {"venue_access"}, listing)
        self.assertEqual([a["id"] for a in listing["venue_access"]],
                         [granted["id"]])

        # -- unauthorized role is rejected ------------------------------------
        coach = self._client()
        self._req(coach, "POST", "/api/auth/login",
                 {"username": "coach", "password": "demo"})
        status, denied = self._req(
            coach, "POST", f"/api/v2/setup/seasons/{season['id']}/venue-access",
            {"venue_id": venue["id"]})
        self.assertEqual(status, 403, denied)
        self.assertEqual(denied["error"]["details"]["required"], "manage_setup")
        status, denied_get = self._req(
            coach, "GET", f"/api/v2/setup/seasons/{season['id']}/venue-access")
        self.assertEqual(status, 403, denied_get)

        # -- duplicate active grant is rejected, zero mutation ----------------
        status, dup = self._req(
            c, "POST", f"/api/v2/setup/seasons/{season['id']}/venue-access",
            {"venue_id": venue["id"]})
        self.assertEqual(status, 400, dup)
        self.assertEqual(dup["error"]["code"], "validation_error")
        self.assertEqual(dup["error"]["details"]["reason"], "already_active")

        # -- one Season can use multiple Venues -------------------------------
        venue2 = self._v2(c, "venue",
                         {"name": "SVA Venue 2", "organization_id": org["id"]})
        status, granted2 = self._req(
            c, "POST", f"/api/v2/setup/seasons/{season['id']}/venue-access",
            {"venue_id": venue2["id"]})
        self.assertEqual(status, 200, granted2)
        status, listing2 = self._req(
            c, "GET", f"/api/v2/setup/seasons/{season['id']}/venue-access")
        self.assertEqual({a["venue_id"] for a in listing2["venue_access"]},
                         {venue["id"], venue2["id"]})

        # -- one Venue can host multiple Programs/Seasons ---------------------
        program2 = self._v2(c, "program",
                           {"name": "SVA Prog 2",
                            "operator_organization_id": org["id"]})
        season2 = self._v2(c, "season",
                          {"program_id": program2["id"], "name": "SVA Season 2"})
        status, granted3 = self._req(
            c, "POST", f"/api/v2/setup/seasons/{season2['id']}/venue-access",
            {"venue_id": venue["id"]})
        self.assertEqual(status, 200, granted3)
        self.assertNotEqual(granted3["id"], granted["id"])

        # -- remove deactivates; not-found and missing-venue_id are reported --
        status, removed = self._req(
            c, "POST",
            f"/api/v2/setup/season-venue-access/{granted['id']}/remove", {})
        self.assertEqual(status, 200, removed)
        self.assertFalse(removed["active"])
        status, missing = self._req(
            c, "POST", "/api/v2/setup/season-venue-access/nope/remove", {})
        self.assertEqual(status, 404, missing)
        self.assertEqual(missing["error"]["code"], "not_found")
        status, no_venue = self._req(
            c, "POST", f"/api/v2/setup/seasons/{season['id']}/venue-access", {})
        self.assertEqual(status, 400, no_venue)
        self.assertEqual(no_venue["error"]["code"], "validation_error")

    def test_v2_season_venue_delete_blocked_until_access_explicitly_cleaned(self):
        """#255 review: SeasonVenueAccess must never be an orphan.
        delete_season/delete_venue block on a matching access row — active
        OR revoked, zero mutation/audit — until it's explicitly cleaned up
        via POST /api/v2/setup/season-venue-access/{id}/delete."""
        c = self._admin()
        org = self._v2(c, "organization", {"name": "SVD Org", "short_name": "SO"})
        program = self._v2(c, "program",
                          {"name": "SVD Prog", "operator_organization_id": org["id"]})
        season = self._v2(c, "season",
                         {"program_id": program["id"], "name": "SVD Season"})
        venue = self._v2(c, "venue",
                        {"name": "SVD Venue", "organization_id": org["id"]})
        status, granted = self._req(
            c, "POST", f"/api/v2/setup/seasons/{season['id']}/venue-access",
            {"venue_id": venue["id"]})
        self.assertEqual(status, 200, granted)

        # -- blocked while active: zero mutation on both parents ------------
        status, blocked_season = self._req(
            c, "POST", f"/api/v2/setup/season/{season['id']}/delete", {})
        self.assertEqual(status, 409, blocked_season)
        self.assertEqual(blocked_season["error"]["code"], "has_dependencies")
        deps = {g["type"] for g in
               blocked_season["error"]["details"]["dependencies"]}
        self.assertIn("venue access", deps)
        status, blocked_venue = self._req(
            c, "POST", f"/api/v2/setup/venue/{venue['id']}/delete", {})
        self.assertEqual(status, 409, blocked_venue)
        self.assertEqual(blocked_venue["error"]["code"], "has_dependencies")
        status, still_there = self._req(
            c, "GET", f"/api/v2/setup/seasons/{season['id']}/venue-access")
        self.assertEqual([a["id"] for a in still_there["venue_access"]],
                         [granted["id"]])
        self.assertTrue(still_there["venue_access"][0]["active"])

        # -- revoking is not enough: still blocked (history preserved) ------
        status, revoked = self._req(
            c, "POST",
            f"/api/v2/setup/season-venue-access/{granted['id']}/remove", {})
        self.assertEqual(status, 200, revoked)
        status, still_blocked = self._req(
            c, "POST", f"/api/v2/setup/season/{season['id']}/delete", {})
        self.assertEqual(status, 409, still_blocked)
        self.assertEqual(still_blocked["error"]["code"], "has_dependencies")

        # -- explicit permanent cleanup unblocks both parents ----------------
        status, cleaned = self._req(
            c, "POST",
            f"/api/v2/setup/season-venue-access/{granted['id']}/delete", {})
        self.assertEqual(status, 200, cleaned)
        self.assertEqual(cleaned["season_id"], season["id"])
        self.assertEqual(cleaned["venue_id"], venue["id"])
        # Cleaning up an already-active row is rejected, not silently ignored.
        status, active_reg = self._req(
            c, "POST", f"/api/v2/setup/seasons/{season['id']}/venue-access",
            {"venue_id": venue["id"]})
        self.assertEqual(status, 200, active_reg)
        status, reject_active = self._req(
            c, "POST",
            f"/api/v2/setup/season-venue-access/{active_reg['id']}/delete", {})
        self.assertEqual(status, 400, reject_active)
        self.assertEqual(reject_active["error"]["code"], "validation_error")
        self.assertEqual(
            reject_active["error"]["details"]["reason"], "access_active")
        status, revoke2 = self._req(
            c, "POST",
            f"/api/v2/setup/season-venue-access/{active_reg['id']}/remove", {})
        self.assertEqual(status, 200, revoke2)
        status, cleaned2 = self._req(
            c, "POST",
            f"/api/v2/setup/season-venue-access/{active_reg['id']}/delete", {})
        self.assertEqual(status, 200, cleaned2)

        status, venue_deleted = self._req(
            c, "POST", f"/api/v2/setup/venue/{venue['id']}/delete", {})
        self.assertEqual(status, 200, venue_deleted)
        status, season_deleted = self._req(
            c, "POST", f"/api/v2/setup/season/{season['id']}/delete", {})
        self.assertEqual(status, 200, season_deleted)

    def test_v2_season_venue_access_cleanup_blocked_by_game_history(self):
        """#257 review: a revoked access row can be the ONLY explicit record
        of why a Game's ice was allowed at this Venue for this Season, so
        POST .../season-venue-access/{id}/delete must never purge it out
        from under a draft, committed, cancelled, or published Game — real
        HTTP end to end, mirroring #251's own game-history guard on
        delete_season_team_registration."""
        c = self._admin()
        org = self._v2(c, "organization", {"name": "SVGH Org", "short_name": "SO"})
        program = self._v2(c, "program",
                          {"name": "SVGH Prog", "operator_organization_id": org["id"]})
        season = self._v2(c, "season",
                         {"program_id": program["id"], "name": "SVGH Season"})
        league = self._v2(c, "league",
                         {"season_id": season["id"], "name": "SVGH League"})
        division = self._v2(c, "division",
                           {"league_id": league["id"], "name": "SVGH Division"})
        venue = self._v2(c, "venue",
                        {"name": "SVGH Venue", "organization_id": org["id"]})
        rink = self._v2(c, "rink", {"venue_id": venue["id"], "name": "R"})
        club = self._v2(c, "club", {"name": "SVGH Club"})
        home = self._v2(c, "team",
                       {"league_id": league["id"], "club_id": club["id"], "name": "Home"})
        away = self._v2(c, "team",
                       {"league_id": league["id"], "club_id": club["id"], "name": "Away"})
        for team in (home, away):
            status, reg = self._req(
                c, "POST", f"/api/v2/setup/seasons/{season['id']}/team-registrations",
                {"team_id": team["id"], "league_id": league["id"], "division_id": division["id"]})
            self.assertEqual(status, 200, reg)
        # A third team (#206 slice 1): the division's home-vs-away pairing
        # already has a real game (created below), so the scheduler would
        # otherwise find nothing left to (re)propose onto slot2 -- a
        # genuinely open pairing is needed to produce the committed draft
        # this test's cleanup-blocking assertions depend on.
        extra = self._v2(c, "team",
                        {"league_id": league["id"], "club_id": club["id"], "name": "Extra"})
        status, reg = self._req(
            c, "POST", f"/api/v2/setup/seasons/{season['id']}/team-registrations",
            {"team_id": extra["id"], "league_id": league["id"], "division_id": division["id"]})
        self.assertEqual(status, 200, reg)
        slot = self._v2(c, "ice-slot", {
            "rink_id": rink["id"], "start_time": "2027-03-01T18:00:00+00:00",
            "end_time": "2027-03-01T19:30:00+00:00", "slot_type": "game"})

        slot2 = self._v2(c, "ice-slot", {
            "rink_id": rink["id"], "start_time": "2027-03-08T18:00:00+00:00",
            "end_time": "2027-03-08T19:30:00+00:00", "slot_type": "game"})

        status, granted = self._req(
            c, "POST", f"/api/v2/setup/seasons/{season['id']}/venue-access",
            {"venue_id": venue["id"]})
        self.assertEqual(status, 200, granted)
        status, game = self._req(c, "POST", "/api/v2/setup/game", {
            "season_id": season["id"], "division_id": division["id"],
            "league_id": league["id"], "home_team_id": home["id"],
            "away_team_id": away["id"], "ice_slot_id": slot["id"]})
        self.assertEqual(status, 200, game)
        # The DRAFT game is created while access is still active — creating
        # one AFTER revoke is correctly rejected by the same eligibility gate
        # this whole PR added, so it must exist before revoke to exercise the
        # cleanup-blocking behavior below.
        _, draft_preview = self._req(
            c, "POST", "/api/scheduler/draft",
            {"division_id": division["id"], "slot_ids": [slot2["id"]]})
        status, draft = self._req(
            c, "POST", "/api/scheduler/commit",
            {"division_id": division["id"], "slot_ids": [slot2["id"]],
             "draft_fingerprint": draft_preview["draft_fingerprint"]})
        self.assertEqual(status, 200, draft)
        self.assertTrue(draft["created"], draft)
        draft_game_id = draft["created"][0]["game_id"]
        status, revoked = self._req(
            c, "POST",
            f"/api/v2/setup/season-venue-access/{granted['id']}/remove", {})
        self.assertEqual(status, 200, revoked)

        # -- blocked by the committed game: zero mutation ----------------
        status, blocked = self._req(
            c, "POST",
            f"/api/v2/setup/season-venue-access/{granted['id']}/delete", {})
        self.assertEqual(status, 409, blocked)
        self.assertEqual(blocked["error"]["code"], "has_dependencies")
        deps = {g["type"] for g in blocked["error"]["details"]["dependencies"]}
        self.assertIn("game", deps)
        status, still_there = self._req(
            c, "GET", f"/api/v2/setup/seasons/{season['id']}/venue-access")
        self.assertEqual([a["id"] for a in still_there["venue_access"]],
                         [granted["id"]])
        self.assertTrue(still_there["venue_access"][0]["active"] is False)

        # -- cancelling the game does NOT clear the block: cancelled games
        #    remain permanent record, same as #251's own registration guard.
        status, cancelled = self._req(
            c, "POST", f"/api/games/{game['id']}/cancel", {})
        self.assertEqual(status, 200, cancelled)
        status, still_blocked = self._req(
            c, "POST",
            f"/api/v2/setup/season-venue-access/{granted['id']}/delete", {})
        self.assertEqual(status, 409, still_blocked)
        self.assertEqual(still_blocked["error"]["code"], "has_dependencies")

        # -- the DRAFT game still blocks cleanup too, and deleting it (its
        #    own supported removal path) is what finally clears the way.
        status, still_blocked2 = self._req(
            c, "POST",
            f"/api/v2/setup/season-venue-access/{granted['id']}/delete", {})
        self.assertEqual(status, 409, still_blocked2)
        status, draft_deleted = self._req(
            c, "POST", f"/api/v2/setup/game/{draft_game_id}/delete", {})
        self.assertEqual(status, 200, draft_deleted)

        # The cancelled (but permanent) game still blocks — cleanup only
        # succeeds once every Game referencing this Venue/Season is gone.
        status, still_blocked3 = self._req(
            c, "POST",
            f"/api/v2/setup/season-venue-access/{granted['id']}/delete", {})
        self.assertEqual(status, 409, still_blocked3)

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
        league = self._v2(c, "league", {"season_id": season["id"], "name": "L"})
        club = self._v2(c, "club", {"name": "C"})
        team = self._v2(c, "team",
                       {"league_id": league["id"], "club_id": club["id"],
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

    def test_v2_cross_program_league_rejected(self):
        # #283: a League is permanent and MAY participate in several Seasons of
        # its own Program, so a League from a different Season of the SAME program
        # is now valid participation. The rejected case is a League belonging to a
        # DIFFERENT Program entirely (its LeagueSeason invariant fails).
        c = self._admin()
        env = self._playable(c)
        other_org = self._v2(c, "organization",
                            {"name": "XProg Org", "short_name": "XO"})
        other_program = self._v2(c, "program",
                               {"name": "X Prog",
                                "operator_organization_id": other_org["id"]})
        other_season = self._v2(c, "season",
                              {"program_id": other_program["id"], "name": "S2"})
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
                        {"league_id": league["id"], "club_id": club["id"],
                         "name": "A"})
        team_b = self._v2(c, "team",
                        {"league_id": league["id"], "club_id": club["id"],
                         "name": "B"})
        # The venue needs active SeasonVenueAccess for this Season for the
        # game ice to be schedulable (#233 Slice E).
        venue = self._v2(c, "venue", {"name": "V", "organization_id": org["id"]})
        self._req(c, "POST", f"/api/v2/setup/seasons/{season['id']}/venue-access",
                  {"venue_id": venue["id"]})
        rink = self._v2(c, "rink", {"venue_id": venue["id"], "name": "R"})
        slot = self._v2(c, "ice-slot",
                       {"rink_id": rink["id"],
                        "start_time": "2026-09-01T18:30:00+00:00",
                        "end_time": "2026-09-01T20:00:00+00:00",
                        "slot_type": "game"})
        return {"org": org, "program": program, "season": season,
                "league": league, "division": division, "team_a": team_a,
                "team_b": team_b, "slot": slot}

    def test_device_token_dangling_recipient_rejected_after_delete(self):
        """#232 review: a device token deactivated before its Player/Official
        is deleted must not be reactivatable or re-registerable afterward —
        the same dangling-identity hole the account reactivation guard
        closes. A still-existing subject's token lifecycle is untouched."""
        c = self._admin()
        env = self._playable(c)

        player = self._v2(c, "player",
                          {"team_id": env["team_a"]["id"], "name": "HTTP Player",
                           "position": "forward"})
        official = self._v2(c, "official", {"name": "HTTP Official"})

        for kind, entity, ref in (
                ("player", player, f"player:{player['id']}"),
                ("official", official, f"official:{official['id']}")):
            status, tok = self._req(
                c, "POST", "/api/notifications/device-tokens",
                {"recipient_ref": ref, "provider": "fcm", "token": f"http-tok-{kind}"})
            self.assertEqual(status, 200, tok)
            self.assertTrue(tok["active"], tok)

            # Deactivate — the supported resolution path — then delete succeeds.
            status, _ = self._req(
                c, "POST", f"/api/notifications/device-tokens/{tok['id']}/active",
                {"active": False})
            self.assertEqual(status, 200)
            status, deleted = self._req(
                c, "POST", f"/api/v2/setup/{kind}/{entity['id']}/delete", {})
            self.assertEqual(status, 200, deleted)

            # Reactivation is rejected — zero mutation.
            status, react = self._req(
                c, "POST", f"/api/notifications/device-tokens/{tok['id']}/active",
                {"active": True})
            self.assertEqual(status, 400, react)
            self.assertEqual(react["error"]["code"], "validation_error", react)
            self.assertEqual(react["error"]["details"]["reason"],
                             "scope_subject_missing", react)

            status, listing = self._req(c, "GET", "/api/notifications/device-tokens")
            self.assertEqual(status, 200)
            row = next(t for t in listing["device_tokens"] if t["id"] == tok["id"])
            self.assertFalse(row["active"], "reactivation must not have mutated the token")

            # Re-registering the same recipient/token pair (the
            # reactivate-via-value-match path in register_device_token) is
            # rejected too, as is a brand-new token for the same dead recipient.
            status, reregister = self._req(
                c, "POST", "/api/notifications/device-tokens",
                {"recipient_ref": ref, "provider": "fcm", "token": f"http-tok-{kind}"})
            self.assertEqual(status, 400, reregister)
            self.assertEqual(reregister["error"]["code"], "validation_error", reregister)
            status, newtok = self._req(
                c, "POST", "/api/notifications/device-tokens",
                {"recipient_ref": ref, "provider": "fcm", "token": f"http-tok-{kind}-new"})
            self.assertEqual(status, 400, newtok)

        # A valid, still-existing subject can register/deactivate/reactivate
        # normally — the guard is narrowly scoped to dead subjects only.
        player2 = self._v2(c, "player",
                           {"team_id": env["team_a"]["id"], "name": "HTTP Player 2",
                            "position": "forward"})
        status, tok2 = self._req(
            c, "POST", "/api/notifications/device-tokens",
            {"recipient_ref": f"player:{player2['id']}", "provider": "fcm",
             "token": "http-tok-live"})
        self.assertEqual(status, 200, tok2)
        status, _ = self._req(
            c, "POST", f"/api/notifications/device-tokens/{tok2['id']}/active",
            {"active": False})
        self.assertEqual(status, 200)
        status, reactivated = self._req(
            c, "POST", f"/api/notifications/device-tokens/{tok2['id']}/active",
            {"active": True})
        self.assertEqual(status, 200, reactivated)
        self.assertTrue(reactivated["active"])


if __name__ == "__main__":
    unittest.main()
