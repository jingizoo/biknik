"""Manual player add and coach-driven substitute candidate creation (#114).

A coach/operator needs a real-world path — new player joins → add player →
make them a substitute candidate → send offer — that the app didn't offer
before: Setup had no Player card, and the outreach queue only ever listed
players who already had a SubstituteEnrollment row (no way to originate one).

Reuses the existing substitute lifecycle rather than inventing a parallel one:
add_substitute_candidate is enroll_substitute (the same method the player's
own self-enroll flow calls) preceded by the full eligibility gate
substitute_block_reason already encodes for the player-side opportunity list
— so a coach adding an arbitrary roster member gets the SAME rules (open
slot, published/non-draft, upcoming, not locked/cancelled, own team, active,
not already rostered) a player would hit via their own pre-filtered list.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND, FakeClock  # noqa: F401  (BACKEND sets up sys.path)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Division, Game, Player, Position, Rink, Team
from hockey_scheduler.store import InMemoryStore
from hockey_scheduler.web import server as srv

UTC = timezone.utc


def _seeded_api():
    s = InMemoryStore()
    s.add_division(Division(id="d", season_id="se", name="D1"))
    s.add_rink(Rink(id="r1", venue_id="v", name="Main"))
    s.add_team(Team(id="home", name="Home", division="D1", division_id="d"))
    s.add_team(Team(id="away", name="Away", division="D1", division_id="d"))
    s.add_team(Team(id="other", name="Other", division="D1", division_id="d"))
    api = ApiService(s)
    api.roster.clock = FakeClock()
    base = api.roster.clock()
    g = Game(id="g1", home_team_id="home", away_team_id="away",
             start_time=base + timedelta(days=1),
             end_time=base + timedelta(days=1, hours=1),
             division_id="d", published=True,
             target_goalies=1, target_skaters=1)
    s.add_game(g)
    return api, s, base


class ManualPlayerCreateTest(unittest.TestCase):
    def setUp(self):
        self.api, self.store, self.base = _seeded_api()

    def test_create_player_writes_the_same_model_import_uses(self):
        p = self.api.create_player("home", "Jordan Lee", "forward",
                                   jersey_number=17, email="jordan@example.com")
        self.assertNotIn("error", p)
        self.assertEqual(p["team_id"], "home")
        self.assertEqual(p["name"], "Jordan Lee")
        self.assertEqual(p["position"], "forward")
        self.assertEqual(p["jersey_number"], 17)
        self.assertTrue(p["is_active"])
        stored = self.store.get_player(p["id"])
        self.assertIsNotNone(stored)

    def test_create_player_requires_a_name(self):
        r = self.api.create_player("home", "  ", "forward")
        self.assertEqual(r["error"]["code"], "validation_error")

    def test_create_player_requires_a_real_team(self):
        r = self.api.create_player("no_such_team", "Jordan Lee", "forward")
        self.assertEqual(r["error"]["code"], "not_found")

    def test_create_player_rejects_non_positive_jersey_number(self):
        r = self.api.create_player("home", "Jordan Lee", "forward", jersey_number=0)
        self.assertEqual(r["error"]["code"], "validation_error")

    def test_create_player_rejects_malformed_email(self):
        r = self.api.create_player("home", "Jordan Lee", "forward",
                                   email="not-an-email")
        self.assertEqual(r["error"]["code"], "validation_error")

    def test_create_player_without_jersey_or_email_is_fine(self):
        p = self.api.create_player("home", "No Extras", "goalie")
        self.assertNotIn("error", p)
        self.assertIsNone(p["jersey_number"])

    def test_list_players_scoped_by_team(self):
        self.api.create_player("home", "Home Kid", "forward")
        self.api.create_player("away", "Away Kid", "forward")
        home_only = self.api.list_players("home")
        self.assertEqual([p["name"] for p in home_only], ["Home Kid"])
        everyone = self.api.list_players()
        self.assertEqual(len(everyone), 2)


class AddSubstituteCandidateTest(unittest.TestCase):
    """add_substitute_candidate reuses enroll_substitute, gated by the exact
    eligibility rules #114 lists — each proven via the actual failure
    scenario, not just that an exception type is raised."""

    def setUp(self):
        self.api, self.store, self.base = _seeded_api()
        self.store.add_player(Player(id="new1", team_id="home", name="New Kid",
                                     position=Position.FORWARD))

    def test_coach_adds_a_newly_created_player_as_a_candidate(self):
        sub = self.api.add_substitute_candidate("g1", "new1", actor_id="coach1")
        self.assertNotIn("error", sub)
        self.assertEqual(sub["status"], "enrolled")
        self.assertEqual(sub["player_id"], "new1")
        # Now visible in the ordinary outreach queue, just like a
        # self-enrolled player would be.
        q = self.api.get_substitute_candidates("g1", "home")
        self.assertIn("new1", [c["player_id"] for c in q["candidates"]])

    def test_blocks_an_inactive_player(self):
        self.store.add_player(Player(id="inactive1", team_id="home", name="Out",
                                     position=Position.FORWARD, is_active=False))
        r = self.api.add_substitute_candidate("g1", "inactive1")
        self.assertEqual(r["error"]["code"], "not_eligible")

    def test_blocks_a_player_on_neither_side_of_this_game(self):
        self.store.add_player(Player(id="other1", team_id="other", name="Other",
                                     position=Position.FORWARD))
        r = self.api.add_substitute_candidate("g1", "other1")
        self.assertEqual(r["error"]["code"], "not_eligible")

    def test_allows_the_away_sides_own_player(self):
        # Home vs away within the SAME game is not "wrong team" — only a
        # player from neither side is blocked.
        self.store.add_player(Player(id="away1", team_id="away", name="Away One",
                                     position=Position.FORWARD))
        sub = self.api.add_substitute_candidate("g1", "away1")
        self.assertNotIn("error", sub)

    def test_blocks_a_player_already_on_the_roster(self):
        self.api.select_roster("g1", ["new1"])
        r = self.api.add_substitute_candidate("g1", "new1")
        self.assertEqual(r["error"]["code"], "not_eligible")

    def test_blocks_when_no_open_slot_for_position(self):
        goalie = Player(id="g0", team_id="home", name="G0", position=Position.GOALIE)
        self.store.add_player(goalie)
        self.api.select_roster("g1", ["g0"])  # fills the only goalie slot
        self.store.add_player(Player(id="g_extra", team_id="home", name="G Extra",
                                     position=Position.GOALIE))
        r = self.api.add_substitute_candidate("g1", "g_extra")
        self.assertEqual(r["error"]["code"], "not_eligible")
        self.assertIn("no open slot", r["error"]["message"])

    def test_blocks_a_draft_unpublished_game(self):
        g = Game(id="gdraft", home_team_id="home", away_team_id="away",
                 start_time=self.base + timedelta(days=1),
                 end_time=self.base + timedelta(days=1, hours=1),
                 division_id="d", published=False, is_draft=True,
                 target_goalies=1, target_skaters=1)
        self.store.add_game(g)
        r = self.api.add_substitute_candidate("gdraft", "new1")
        self.assertEqual(r["error"]["code"], "not_eligible")

    def test_blocks_a_past_game(self):
        past = self.base - timedelta(days=1)
        g = Game(id="gpast", home_team_id="home", away_team_id="away",
                 start_time=past, end_time=past + timedelta(hours=1),
                 division_id="d", published=True,
                 target_goalies=1, target_skaters=1)
        self.store.add_game(g)
        r = self.api.add_substitute_candidate("gpast", "new1")
        self.assertEqual(r["error"]["code"], "not_eligible")

    def test_blocks_a_locked_game(self):
        # substitute_block_reason surfaces this itself (rather than letting
        # enroll_substitute's _guard_mutable raise a dead end), so it comes
        # back as "not_eligible" like the other pre-filter reasons above.
        self.api.lock_roster("g1")
        r = self.api.add_substitute_candidate("g1", "new1")
        self.assertEqual(r["error"]["code"], "not_eligible")

    def test_blocks_a_cancelled_game(self):
        self.api.cancel_game("g1")
        r = self.api.add_substitute_candidate("g1", "new1")
        self.assertEqual(r["error"]["code"], "not_eligible")

    def test_blocks_a_player_already_enrolled(self):
        self.api.enroll_substitute("g1", "new1")
        r = self.api.add_substitute_candidate("g1", "new1")
        self.assertEqual(r["error"]["code"], "validation_error")

    def test_audit_attributes_the_acting_coach(self):
        self.api.add_substitute_candidate("g1", "new1", actor_id="coach1")
        entries = self.store.audit_for_game("g1")
        entry = next(a for a in entries if a.subject_player_id == "new1"
                    and a.action.value == "substitute_enrolled")
        self.assertEqual(entry.actor_id, "coach1")

    def test_full_flow_add_then_offer_then_accept(self):
        # #114 acceptance criterion 4: candidate can then receive Send Offer,
        # reusing the existing offer/accept lifecycle unchanged.
        self.api.add_substitute_candidate("g1", "new1", actor_id="coach1")
        offer = self.api.offer_substitute("g1", "new1", actor_id="coach1")
        self.assertEqual(offer["status"], "offered")
        accept = self.api.accept_substitute("g1", "new1", actor_id="new1")
        self.assertEqual(accept["status"], "accepted")
        self.assertIsNotNone(self.store.roster_entry_for_player("g1", "new1"))


class AddablePlayersListTest(unittest.TestCase):
    def setUp(self):
        self.api, self.store, self.base = _seeded_api()

    def test_lists_eligible_active_team_players(self):
        self.store.add_player(Player(id="p1", team_id="home", name="P1",
                                     position=Position.FORWARD))
        addable = self.api.get_addable_substitutes("g1", "home")
        self.assertEqual([p["player_id"] for p in addable["addable"]], ["p1"])

    def test_excludes_already_enrolled_players(self):
        self.store.add_player(Player(id="p1", team_id="home", name="P1",
                                     position=Position.FORWARD))
        self.api.enroll_substitute("g1", "p1")
        addable = self.api.get_addable_substitutes("g1", "home")
        self.assertEqual(addable["addable"], [])

    def test_excludes_inactive_players(self):
        self.store.add_player(Player(id="p1", team_id="home", name="P1",
                                     position=Position.FORWARD, is_active=False))
        addable = self.api.get_addable_substitutes("g1", "home")
        self.assertEqual(addable["addable"], [])

    def test_excludes_players_already_rostered(self):
        self.store.add_player(Player(id="p1", team_id="home", name="P1",
                                     position=Position.FORWARD))
        self.api.select_roster("g1", ["p1"])
        addable = self.api.get_addable_substitutes("g1", "home")
        self.assertEqual(addable["addable"], [])


class ManualPlayerAndCandidateHttpTest(unittest.TestCase):
    """Route-level permission/scope gating on the demo store, plus the full
    add-player -> add-as-candidate -> offer -> accept flow over real HTTP —
    #114 acceptance criteria 1-4 driven end to end through the actual app."""

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
        cls.httpd.server_close()

    def _login(self, username):
        c = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        self._post(c, "/api/auth/login", {"username": username, "password": "demo"})
        return c

    def _post(self, opener, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _get(self, opener, path):
        try:
            with opener.open(f"http://127.0.0.1:{self.port}{path}") as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    # -- manual player creation (criterion 1-2) -----------------------------
    def test_league_admin_can_create_a_player_via_setup(self):
        c = self._login("admin")
        team_id = srv.STATE.ids["home_team_id"]
        status, p = self._post(c, "/api/setup/player", {
            "team_id": team_id, "name": "New Arrival", "position": "forward"})
        self.assertEqual(status, 200)
        self.assertEqual(p["team_id"], team_id)

    def test_coach_cannot_create_a_player(self):
        c = self._login("coach")
        team_id = srv.STATE.ids["home_team_id"]
        status, _ = self._post(c, "/api/setup/player", {
            "team_id": team_id, "name": "New Arrival", "position": "forward"})
        self.assertEqual(status, 403)

    def test_players_list_requires_operator_session(self):
        anon = urllib.request.build_opener()
        status, _ = self._get(anon, "/api/players")
        self.assertEqual(status, 401)
        c = self._login("coach")
        status, _ = self._get(c, "/api/players")
        self.assertEqual(status, 403)
        admin = self._login("admin")
        status, body = self._get(admin, "/api/players")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, list)

    # -- coach add-to-substitute-candidate (criterion 3-4) -------------------
    def test_coach_can_add_and_the_full_flow_reaches_accepted(self):
        admin = self._login("admin")
        team_id = srv.STATE.ids["home_team_id"]
        gid = srv.STATE.ids["next_game_id"]
        # next_game_id is seeded as an unpublished draft (so the roster demo
        # has an empty game to fill) — substitute eligibility requires a
        # published game, so publish it first.
        status, _ = self._post(admin, f"/api/games/{gid}/publish", {})
        self.assertEqual(status, 200)
        _, p = self._post(admin, "/api/setup/player", {
            "team_id": team_id, "name": "Flow Kid", "position": "forward"})
        pid = p["id"]

        coach = self._login("coach")
        status, addable = self._get(
            coach, f"/api/games/{gid}/substitute-addable?team_id={team_id}")
        self.assertEqual(status, 200)
        self.assertIn(pid, [a["player_id"] for a in addable["addable"]])

        status, sub = self._post(
            coach, f"/api/games/{gid}/substitutes/add-candidate", {"player_id": pid})
        self.assertEqual(status, 200)
        self.assertEqual(sub["status"], "enrolled")

        status, _ = self._post(
            coach, f"/api/games/{gid}/substitutes/{pid}/offer", {})
        self.assertEqual(status, 200)

        status, acc = self._post(
            coach, f"/api/games/{gid}/substitutes/{pid}/accept", {})
        self.assertEqual(status, 200)
        self.assertEqual(acc["status"], "accepted")

    def test_player_role_cannot_add_a_candidate(self):
        gid = srv.STATE.ids["next_game_id"]
        pid = srv.STATE.ids["substitute_player_id"]
        c = self._login("player")
        status, _ = self._post(
            c, f"/api/games/{gid}/substitutes/add-candidate", {"player_id": pid})
        self.assertEqual(status, 403)

    def test_coach_scoped_to_own_team_cannot_add_a_stranger_teams_player(self):
        # The demo coach persona is bound to home_team_id; away_team_id's
        # roster is a different team's business.
        admin = self._login("admin")
        away_team = srv.STATE.ids["away_team_id"]
        gid = srv.STATE.ids["next_game_id"]
        _, p = self._post(admin, "/api/setup/player", {
            "team_id": away_team, "name": "Away Kid", "position": "forward"})
        coach = self._login("coach")
        status, _ = self._post(
            coach, f"/api/games/{gid}/substitutes/add-candidate",
            {"player_id": p["id"]})
        self.assertEqual(status, 403)

    def test_substitute_addable_requires_manage_roster(self):
        gid = srv.STATE.ids["next_game_id"]
        c = self._login("player")
        status, _ = self._get(c, f"/api/games/{gid}/substitute-addable")
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
