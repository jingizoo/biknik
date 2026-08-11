"""The v1 /api/setup contract after the C1b rename (#233).

The domain + facade are canonical (Program/League with program_id /
operator_organization_id / competition league_id), but the v1 HTTP API must be
unchanged. The v1_setup_adapter maps canonical results back to the legacy v1
keys, and drops the competition league_id the v1 API never exposed.

This is a real before/after contract: each v1 setup response is asserted to have
EXACTLY the legacy JSON key set (and stable values), proving the legacy keys are
present and the canonical ones (operator_organization_id / program_id, and the
competition league_id on registration and game) are absent. Where raw byte order
isn't guaranteed the assertions use the parsed JSON, so they pin the same JSON
keys/shape and values rather than a byte sequence.

Two levels are covered:
 - the ApiService FACADE is canonical — a direct call returns registration/game
   dicts WITH league_id;
 - the v1 HTTP boundary strips it — the same routes over HTTP omit league_id.
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
from hockey_scheduler.domain import Team
from hockey_scheduler.store import InMemoryStore

# The exact legacy v1 JSON key set for each setup response (the frozen contract).
ORG_KEYS = {"id", "name", "short_name", "external_ref"}
PROGRAM_KEYS = {"id", "name", "country", "timezone", "organization_id",
                "external_ref"}
SEASON_KEYS = {"id", "league_id", "name", "start_date", "end_date", "external_ref"}
LEVEL_KEYS = {"id", "season_id", "name", "sort_order", "external_ref"}
DIVISION_KEYS = {"id", "season_id", "name", "age_group", "level_id", "external_ref"}
CLUB_KEYS = {"id", "name", "country", "external_ref"}  # #260 Slice F
TEAM_KEYS = {"id", "name", "division", "club_id", "division_id", "external_ref",
             "league_id"}
VENUE_KEYS = {"id", "name", "address", "timezone", "organization_id", "league_id",
              "external_ref"}
REGISTRATION_KEYS = {"id", "season_id", "team_id", "division_id", "active"}
GAME_KEYS = {"id", "home_team_id", "start_time", "target_goalies",
             "target_skaters", "max_skaters", "away_team_id", "rink", "end_time",
             "roster_lock_time", "locked", "cancelled", "published", "season_id",
             "division_id", "ice_slot_id", "is_draft"}


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
        cls.httpd.server_close()

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

    def _select(self, c, program_id, season_id=None):
        """Make ``program_id`` (and optionally ``season_id``) this caller's
        PERSISTED active context (#369).

        A setup mutation that names an EXISTING record is authorized against the
        active context, not merely against the caller's role. A test that builds
        a fresh Program and then reassigns, edits or deletes records under it
        has to say which Program it is working in; otherwise the admin is still
        sitting in whichever Program resolved first and every one of those
        mutations is correctly refused as not-found.

        The same is true one axis down (#369 re-review): a SEASON-BOUND target
        — a Division, a registration, a venue-access grant — is judged against
        the ACTIVE SEASON as well, and a Program-only context fails closed
        against it. Passing the Season is how this file states which Season the
        v1 contract legs below are working in.
        """
        body = {"program_id": program_id}
        if season_id is not None:
            body["season_id"] = season_id
        status, ctx = self._req(c, "POST", "/api/context", body)
        self.assertEqual(status, 200, ctx)
        if season_id is not None:
            self.assertEqual((ctx.get("season") or {}).get("id"), season_id,
                             ctx)
        return ctx

    def _program(self, c, body):
        """Create a Program via the v1 "league" route and select it (#369).

        v1 spells today's Program "league" — the pre-#233 vocabulary this whole
        file exists to pin — but ``/api/context`` is canonical, so the same id
        goes in as ``program_id``.
        """
        program = self._create(c, "league", body)
        self._select(c, program["id"])
        return program

    # -- exact legacy key sets on every create response ---------------------
    def test_v1_create_responses_have_exact_legacy_key_sets(self):
        c = self._admin()

        org = self._create(c, "organization", {"name": "Canlon", "short_name": "CAN"})
        self.assertEqual(set(org), ORG_KEYS, org)
        self.assertEqual(org["name"], "Canlon")
        self.assertEqual(org["short_name"], "CAN")

        # v1 "league" is the umbrella Program: organization_id, NOT
        # operator_organization_id.
        league = self._create(c, "league",
                              {"name": "Over 55", "country": "US",
                               "organization_id": org["id"]})
        self.assertEqual(set(league), PROGRAM_KEYS, league)
        self.assertEqual(league["organization_id"], org["id"])
        self.assertNotIn("operator_organization_id", league)

        # season: response league_id (the Program id), NOT program_id.
        # #409: a Season CREATE is PROGRAM-AXIS, so select the Program this
        # test just built before creating into it; a League/Division create is
        # SEASON-OWNED, so select the Season the moment it exists. Both are the
        # selections this file already makes further down, one step earlier.
        self._select(c, league["id"])
        season = self._create(c, "season",
                              {"league_id": league["id"], "name": "Fall 2026"})
        self._select(c, league["id"], season["id"])
        self.assertEqual(set(season), SEASON_KEYS, season)
        self.assertEqual(season["league_id"], league["id"])
        self.assertNotIn("program_id", season)

        # v1 "level" is the new grouping League — every legacy field unchanged.
        level = self._create(c, "level",
                             {"season_id": season["id"], "name": "Level 1",
                              "sort_order": 1})
        self.assertEqual(set(level), LEVEL_KEYS, level)
        self.assertEqual(level["season_id"], season["id"])
        self.assertEqual(level["sort_order"], 1)

        # division: response level_id, NOT the canonical league_id.
        division = self._create(c, "division",
                                {"season_id": season["id"], "name": "Div A",
                                 "level_id": level["id"]})
        self.assertEqual(set(division), DIVISION_KEYS, division)
        self.assertEqual(division["level_id"], level["id"])
        self.assertNotIn("league_id", division)

        club = self._create(c, "club", {"name": "Club X"})
        self.assertEqual(set(club), CLUB_KEYS, club)

        # team: response league_id (the Program id), NOT program_id.
        team = self._create(c, "team",
                            {"club_id": club["id"], "name": "Team X",
                             "league_id": league["id"]})
        self.assertEqual(set(team), TEAM_KEYS, team)
        self.assertEqual(team["league_id"], league["id"])
        self.assertNotIn("program_id", team)

        # venue keeps BOTH league_id and organization_id this slice (unchanged).
        venue = self._create(c, "venue",
                             {"name": "Plainfield", "league_id": league["id"],
                              "organization_id": org["id"]})
        self.assertEqual(set(venue), VENUE_KEYS, venue)
        self.assertEqual(venue["league_id"], league["id"])
        self.assertEqual(venue["organization_id"], org["id"])

    # -- exact legacy key sets on reassign responses ------------------------
    def test_v1_reassign_responses_have_exact_legacy_key_sets(self):
        c = self._admin()
        org = self._create(c, "organization", {"name": "Reorg", "short_name": "REO"})
        league = self._program(c,
                               {"name": "Reassign", "organization_id": org["id"]})
        season = self._create(c, "season",
                              {"league_id": league["id"], "name": "S"})
        self._select(c, league["id"], season["id"])
        level = self._create(c, "level", {"season_id": season["id"], "name": "L"})
        division = self._create(c, "division",
                                {"season_id": season["id"], "name": "D",
                                 "level_id": level["id"]})

        # division/assign-level → level_id, never the canonical league_id.
        status, moved = self._req(
            c, "POST", f"/api/setup/division/{division['id']}/assign-level",
            {"level_id": level["id"]})
        self.assertEqual(status, 200, moved)
        self.assertEqual(set(moved), DIVISION_KEYS, moved)
        self.assertIn("level_id", moved)
        self.assertNotIn("league_id", moved)

        # league/assign-organization → organization_id, never
        # operator_organization_id.
        org2 = self._create(c, "organization", {"name": "Other", "short_name": "OTH"})
        status, reorg = self._req(
            c, "POST", f"/api/setup/league/{league['id']}/assign-organization",
            {"organization_id": org2["id"]})
        self.assertEqual(status, 200, reorg)
        self.assertEqual(set(reorg), PROGRAM_KEYS, reorg)
        self.assertEqual(reorg["organization_id"], org2["id"])
        self.assertNotIn("operator_organization_id", reorg)

    # -- registration + game: v1 omits the competition league_id ------------
    def _build_playable(self, c):
        """Full hierarchy with two registered teams + a valid game ice slot."""
        org = self._create(c, "organization", {"name": "Game Org", "short_name": "GMO"})
        league = self._program(c,
                               {"name": "Game Prog", "organization_id": org["id"]})
        season = self._create(c, "season",
                              {"league_id": league["id"], "name": "Fall"})
        self._select(c, league["id"], season["id"])
        division = self._create(c, "division",
                                {"season_id": season["id"], "name": "Div A"})
        club = self._create(c, "club", {"name": "Game Club"})
        team_a = self._create(c, "team",
                              {"club_id": club["id"], "name": "A",
                               "league_id": league["id"]})
        team_b = self._create(c, "team",
                              {"club_id": club["id"], "name": "B",
                               "league_id": league["id"]})
        venue = self._create(c, "venue",
                             {"name": "V", "league_id": league["id"],
                              "organization_id": org["id"]})
        rink = self._create(c, "rink", {"venue_id": venue["id"], "name": "R"})
        slot = self._create(c, "ice-slot",
                            {"rink_id": rink["id"],
                             "start_time": "2026-09-01T18:30:00+00:00",
                             "end_time": "2026-09-01T20:00:00+00:00",
                             "slot_type": "game"})
        # Game ice eligibility (#233 Slice E) is season-based; grant the
        # venue access via the v2-only route (no v1 equivalent exists).
        self._req(c, "POST", f"/api/v2/setup/seasons/{season['id']}/venue-access",
                  {"venue_id": venue["id"]})
        return league, season, division, team_a, team_b, slot

    def test_v1_registration_response_omits_league_id(self):
        c = self._admin()
        league, season, division, team_a, team_b, _ = self._build_playable(c)

        status, reg = self._req(
            c, "POST", f"/api/setup/seasons/{season['id']}/team-registrations",
            {"team_id": team_a["id"], "division_id": division["id"]})
        self.assertEqual(status, 200, reg)
        self.assertEqual(set(reg), REGISTRATION_KEYS, reg)
        self.assertNotIn("league_id", reg)  # competition league_id stripped at v1
        self.assertEqual(reg["team_id"], team_a["id"])
        self.assertTrue(reg["active"])

        # assign-division stays a registration shape (no league_id).
        status, moved = self._req(
            c, "POST",
            f"/api/setup/season-team-registration/{reg['id']}/assign-division",
            {"division_id": division["id"]})
        self.assertEqual(status, 200, moved)
        self.assertEqual(set(moved), REGISTRATION_KEYS, moved)

        # The season's registration list also omits league_id on every row.
        status, listing = self._req(
            c, "GET", f"/api/setup/seasons/{season['id']}/team-registrations")
        self.assertEqual(status, 200, listing)
        self.assertEqual(set(listing), {"registrations"}, listing)
        self.assertEqual(set(listing["registrations"][0]), REGISTRATION_KEYS)

        # remove → still a registration shape, deactivated, no league_id.
        status, removed = self._req(
            c, "POST",
            f"/api/setup/season-team-registration/{reg['id']}/remove", {})
        self.assertEqual(status, 200, removed)
        self.assertEqual(set(removed), REGISTRATION_KEYS, removed)
        self.assertFalse(removed["active"])

    def test_v1_game_response_omits_league_id(self):
        c = self._admin()
        league, season, division, team_a, team_b, slot = self._build_playable(c)
        for tm in (team_a, team_b):
            self._req(
                c, "POST", f"/api/setup/seasons/{season['id']}/team-registrations",
                {"team_id": tm["id"], "division_id": division["id"]})

        status, game = self._req(c, "POST", "/api/setup/game",
                                 {"season_id": season["id"],
                                  "division_id": division["id"],
                                  "home_team_id": team_a["id"],
                                  "away_team_id": team_b["id"],
                                  "ice_slot_id": slot["id"]})
        self.assertEqual(status, 200, game)
        self.assertEqual(set(game), GAME_KEYS, game)
        self.assertNotIn("league_id", game)  # competition league_id stripped at v1

        # delete → still a game shape, no league_id. Only a DRAFT game is
        # deletable (a scheduled one must be cancelled), so mark it draft in the
        # in-process store — same shortcut test_setup_deletion uses — to exercise
        # the delete route's v1 mapping.
        store = self.srv.STATE.api.store
        g = store.get_game(game["id"])
        g.is_draft = True
        store.save_game(g)
        status, deleted = self._req(
            c, "POST", f"/api/setup/game/{game['id']}/delete", {})
        self.assertEqual(status, 200, deleted)
        self.assertEqual(set(deleted), GAME_KEYS, deleted)
        self.assertNotIn("league_id", deleted)

    def test_v1_program_teams_read_route_exact_shape(self):
        c = self._admin()
        org = self._create(c, "organization", {"name": "Owner", "short_name": "OWN"})
        league = self._create(c, "league",
                              {"name": "Read Program", "organization_id": org["id"]})
        club = self._create(c, "club", {"name": "Read Club"})
        # #283 Slice E: create_team now requires a resolvable permanent League,
        # which the v1 team route can't supply (its "league_id" body key means
        # Program, and this program has no competition League). This read-shape
        # test just needs a team under the program, so inject it directly into
        # the shared store, bypassing the service guard (the v1 read route still
        # derives the legacy league_id from the team's program_id).
        store = self.srv.STATE.api.store
        store.add_team(Team(id=store.next_id("team"), name="Read Team",
                            club_id=club["id"], program_id=league["id"]))
        status, body = self._req(
            c, "GET", f"/api/setup/leagues/{league['id']}/teams")
        self.assertEqual(status, 200, body)
        self.assertEqual(set(body), {"teams"}, body)
        rows = body["teams"]
        self.assertEqual(len(rows), 1, body)
        self.assertEqual(set(rows[0]), TEAM_KEYS, rows[0])
        self.assertEqual(rows[0]["league_id"], league["id"])
        self.assertNotIn("program_id", rows[0])

    def test_v1_hierarchy_never_leaks_canonical_keys(self):
        c = self._admin()
        org = self._create(c, "organization", {"name": "H Org", "short_name": "HOG"})
        league = self._create(c, "league",
                              {"name": "H Prog", "organization_id": org["id"]})
        self._select(c, league["id"])
        season = self._create(c, "season",
                              {"league_id": league["id"], "name": "H S"})
        self._select(c, league["id"], season["id"])
        level = self._create(c, "level", {"season_id": season["id"], "name": "H L"})
        self._create(c, "division",
                     {"season_id": season["id"], "level_id": level["id"],
                      "name": "H D"})
        status, body = self._req(c, "GET", "/api/setup/hierarchy")
        self.assertEqual(status, 200, body)
        # The nested hierarchy uses its own explicit UI keys; the canonical field
        # names must never surface anywhere in the serialized tree.
        blob = json.dumps(body)
        self.assertNotIn("operator_organization_id", blob)
        self.assertNotIn("program_id", blob)
        # #233: the v1 level-node key set is FROZEN — the #159 unbind's
        # league_season_id lives on the v2 hierarchy only, never v1.
        self.assertNotIn("league_season_id", blob)
        lg = next(x for x in body["leagues"] if x["id"] == league["id"])
        lv = lg["seasons"][0]["levels"][0]
        self.assertEqual(set(lv), {"id", "name", "sort_order", "divisions"}, lv)

    def test_v1_error_envelope_shape_and_status(self):
        c = self._admin()
        # A season under a non-existent program → structured error envelope.
        status, resp = self._req(c, "POST", "/api/setup/season",
                                 {"league_id": "ghost", "name": "X"})
        self.assertIn(status, (400, 404), resp)
        self.assertEqual(set(resp), {"error"}, resp)
        self.assertLessEqual({"code", "message"}, set(resp["error"]), resp)
        self.assertIsInstance(resp["error"]["code"], str)


class V1FacadeIsCanonicalTest(unittest.TestCase):
    """The ApiService facade is purely canonical: a DIRECT call (no v1 boundary)
    returns registration and game dicts that DO carry the competition
    league_id — it is only the v1 HTTP boundary that strips it (proven above)."""

    def _playable(self):
        api = ApiService(InMemoryStore())
        org = api.create_organization("Org", short_name="O", actor_id="admin")
        prog = api.create_program("Prog", operator_organization_id=org["id"],
                                  actor_id="admin")
        season = api.create_season(prog["id"], "Fall", actor_id="admin")
        # A grouping League (the canonical competition league) so the derived
        # registration/game league_id has a concrete, assertable value.
        self.grouping = api.create_league(season["id"], "L1", actor_id="admin")
        division = api.create_division(season["id"], "Div",
                                       league_id=self.grouping["id"],
                                       actor_id="admin")
        club = api.create_club("Club", actor_id="admin")
        team_a = api.create_team(club["id"], division["id"], "A", actor_id="admin",
                                 program_id=prog["id"])
        team_b = api.create_team(club["id"], division["id"], "B", actor_id="admin",
                                 program_id=prog["id"])
        venue = api.create_venue("V", league_id=prog["id"], actor_id="admin")
        api.grant_season_venue_access(season["id"], venue["id"], actor_id="admin")
        rink = api.create_rink(venue["id"], "R", actor_id="admin")
        slot = api.create_ice_slot(rink["id"], "2026-09-01T18:30:00+00:00",
                                   "2026-09-01T20:00:00+00:00", actor_id="admin")
        return api, prog, season, division, team_a, team_b, slot

    def test_facade_register_team_returns_league_id(self):
        api, prog, season, division, team_a, team_b, _ = self._playable()
        reg = api.register_team_for_season(
            season["id"], team_a["id"], division["id"], actor_id="admin")
        self.assertNotIn("error", reg, reg)
        # Canonical: the facade exposes the competition league_id (the grouping
        # League derived from the division), never stripped.
        self.assertIn("league_id", reg)
        self.assertEqual(reg["league_id"], self.grouping["id"])

    def test_facade_create_game_returns_league_id(self):
        api, prog, season, division, team_a, team_b, slot = self._playable()
        for tm in (team_a, team_b):
            api.register_team_for_season(
                season["id"], tm["id"], division["id"], actor_id="admin")
        game = api.create_game(
            season["id"], division["id"], team_a["id"], team_b["id"], slot["id"],
            actor_id="admin")
        self.assertNotIn("error", game, game)
        # Canonical: the facade exposes the competition league_id (the grouping
        # League derived from the division), never stripped.
        self.assertIn("league_id", game)
        self.assertEqual(game["league_id"], self.grouping["id"])


if __name__ == "__main__":
    unittest.main()
