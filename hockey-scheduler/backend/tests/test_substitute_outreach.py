"""Substitute outreach queue: ranked offers, expiry, accept/decline (#112).

Builds on #107/#110. The offer/accept/decline SERVICES already existed; this
covers the #112 additions: the coach candidate queue (get_substitute_candidates
/ list_substitute_candidates), the OFFERED branch of the player detail view
(can_accept_offer / can_decline_offer), the signed-in-player scoped
accept-offer/decline-offer routes, the coach-gated candidate-queue route, and
the feed notifications the offer/accept/decline transitions now emit.
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
    """Home team with a goalie already rostered so exactly one skater slot is
    open, plus two enrolled skater substitutes for the outreach queue."""
    s = InMemoryStore()
    s.add_division(Division(id="d", season_id="se", name="D1"))
    s.add_rink(Rink(id="r1", venue_id="v", name="Main"))
    s.add_team(Team(id="home", name="Home", division="D1", division_id="d"))
    s.add_team(Team(id="away", name="Away", division="D1", division_id="d"))
    s.add_player(Player(id="g0", team_id="home", name="G0", position=Position.GOALIE))
    s.add_player(Player(id="sk0", team_id="home", name="Sk0", position=Position.FORWARD))
    s.add_player(Player(id="sub_b", team_id="home", name="Bravo", position=Position.FORWARD))
    s.add_player(Player(id="sub_a", team_id="home", name="Alpha", position=Position.FORWARD))
    s.add_player(Player(id="away_sk", team_id="away", name="Away Sk", position=Position.FORWARD))
    api = ApiService(s)
    api.roster.clock = FakeClock()
    base = api.roster.clock()
    g = Game(id="g1", home_team_id="home", away_team_id="away",
             start_time=base + timedelta(days=1),
             end_time=base + timedelta(days=1, hours=1),
             division_id="d", published=True, rink="Rink 1",
             target_goalies=1, target_skaters=2)
    s.add_game(g)
    api.select_roster("g1", ["g0", "sk0"])  # one open skater slot
    api.enroll_substitute("g1", "sub_b")
    api.enroll_substitute("g1", "sub_a")
    return api, s, base


class CandidateQueueTest(unittest.TestCase):
    def setUp(self):
        self.api, self.store, self.base = _seeded_api()

    def test_queue_lists_enrolled_candidates_ordered_by_name(self):
        q = self.api.get_substitute_candidates("g1", "home")
        self.assertEqual(q["open_skater_slots"], 1)
        names = [c["name"] for c in q["candidates"]]
        # Both enrolled; stable sort by name → Alpha before Bravo.
        self.assertEqual(names, ["Alpha", "Bravo"])
        self.assertTrue(all(c["can_offer"] for c in q["candidates"]))

    def test_queue_excludes_other_teams_substitutes(self):
        self.api.enroll_substitute("g1", "away_sk")  # away team's own vacancy
        ids = [c["player_id"]
               for c in self.api.get_substitute_candidates("g1", "home")["candidates"]]
        self.assertNotIn("away_sk", ids)

    def test_offered_candidate_ranks_after_enrolled_and_cannot_reoffer(self):
        self.api.offer_substitute("g1", "sub_a")
        q = self.api.get_substitute_candidates("g1", "home")
        # sub_a is now OFFERED → ranks after the still-enrolled sub_b.
        self.assertEqual([c["player_id"] for c in q["candidates"]], ["sub_b", "sub_a"])
        offered = next(c for c in q["candidates"] if c["player_id"] == "sub_a")
        self.assertEqual(offered["status"], "offered")
        self.assertFalse(offered["can_offer"])

    def test_can_offer_false_when_no_open_slot(self):
        # Fill the open skater slot by accepting sub_b, then the queue for the
        # remaining candidate can no longer be offered.
        self.api.offer_substitute("g1", "sub_b")
        self.api.accept_substitute("g1", "sub_b")
        q = self.api.get_substitute_candidates("g1", "home")
        remaining = next(c for c in q["candidates"] if c["player_id"] == "sub_a")
        self.assertEqual(q["open_skater_slots"], 0)
        self.assertFalse(remaining["can_offer"])

    def test_can_offer_false_when_locked(self):
        self.api.lock_roster("g1")
        q = self.api.get_substitute_candidates("g1", "home")
        self.assertTrue(q["locked"])
        self.assertFalse(any(c["can_offer"] for c in q["candidates"]))

    def test_unknown_game_is_not_found(self):
        result = self.api.get_substitute_candidates("nope", "home")
        self.assertIn("error", result)
        self.assertEqual(result["error"]["code"], "not_found")


class OfferedPlayerDetailTest(unittest.TestCase):
    def setUp(self):
        self.api, self.store, self.base = _seeded_api()

    def test_offered_player_sees_accept_and_decline(self):
        self.api.offer_substitute("g1", "sub_a")
        d = self.api.get_substitute_opportunity("sub_a", "g1")
        self.assertEqual(d["enrollment_status"], "offered")
        self.assertTrue(d["can_accept_offer"])
        self.assertTrue(d["can_decline_offer"])
        # Not the self-service enroll/withdraw path.
        self.assertFalse(d["can_accept"])
        self.assertFalse(d["can_withdraw"])

    def test_locked_offer_blocks_both_actions(self):
        self.api.offer_substitute("g1", "sub_a")
        self.api.lock_roster("g1")
        d = self.api.get_substitute_opportunity("sub_a", "g1")
        self.assertFalse(d["can_accept_offer"])
        self.assertFalse(d["can_decline_offer"])
        self.assertIn("locked", d["blocked_reason"])

    def test_expired_offer_cannot_be_accepted_but_can_be_declined(self):
        # An offer whose expiry has already passed: Accept is disabled with a
        # reason, but the player can still Decline to clear it.
        self.api.roster.offer_substitute(
            "g1", "sub_a", offer_expires_at=self.base - timedelta(hours=1))
        d = self.api.get_substitute_opportunity("sub_a", "g1")
        self.assertFalse(d["can_accept_offer"])
        self.assertTrue(d["can_decline_offer"])
        self.assertIn("expired", d["blocked_reason"])

    def test_offered_slot_surfaces_on_player_home(self):
        # An offered slot is excluded from the self-enrol opportunities list
        # but must appear under substitute_offers so the player can respond.
        self.api.offer_substitute("g1", "sub_a")
        home = self.api.get_player_home("sub_a")
        offer_ids = [o["game_id"] for o in home["substitute_offers"]]
        opp_ids = [o["game_id"] for o in home["substitute_opportunities"]]
        self.assertIn("g1", offer_ids)
        self.assertNotIn("g1", opp_ids)

    def test_past_game_offer_cannot_be_accepted(self):
        # A no-expiry offer on a game whose start has passed must not stay
        # acceptable — both the detail flag and the service reject it.
        self.api.offer_substitute("g1", "sub_a")  # offer_expires_at=None
        # Freeze the clock past the game's start (base + 1 day).
        future = self.base + timedelta(days=2)
        self.api.roster.clock = lambda: future
        d = self.api.get_substitute_opportunity("sub_a", "g1")
        self.assertFalse(d["can_accept_offer"])
        self.assertIn("upcoming", d["blocked_reason"])
        res = self.api.accept_substitute("g1", "sub_a")
        self.assertIn("error", res)

    def test_offer_on_past_or_cancelled_game_absent_from_home(self):
        self.api.offer_substitute("g1", "sub_a")
        self.api.cancel_game("g1")
        home = self.api.get_player_home("sub_a")
        self.assertNotIn("g1", [o["game_id"] for o in home["substitute_offers"]])

    def test_accept_offer_fills_roster(self):
        self.api.offer_substitute("g1", "sub_a")
        self.api.accept_substitute("g1", "sub_a")
        entry = self.store.roster_entry_for_player("g1", "sub_a")
        self.assertIsNotNone(entry)
        self.assertTrue(entry.status.occupies_slot)

    def test_decline_offer_does_not_fill_roster(self):
        self.api.offer_substitute("g1", "sub_a")
        self.api.decline_substitute("g1", "sub_a")
        self.assertIsNone(self.store.roster_entry_for_player("g1", "sub_a"))
        sub = self.store.substitute_for_player("g1", "sub_a")
        self.assertEqual(sub.status.value, "declined")


class OutreachNotificationTest(unittest.TestCase):
    def setUp(self):
        self.api, self.store, self.base = _seeded_api()

    def test_offer_notifies_player_feed(self):
        before = self.api.get_notifications("player", {"player_id": "sub_a"})["unread"]
        self.api.offer_substitute("g1", "sub_a")
        after = self.api.get_notifications("player", {"player_id": "sub_a"})["unread"]
        self.assertEqual(after, before + 1)

    def test_accept_notifies_coach_feed(self):
        self.api.offer_substitute("g1", "sub_a")
        before = self.api.get_notifications("coach", {"team_id": "home"})["unread"]
        self.api.accept_substitute("g1", "sub_a")
        after = self.api.get_notifications("coach", {"team_id": "home"})["unread"]
        self.assertEqual(after, before + 1)

    def test_decline_notifies_coach_feed(self):
        self.api.offer_substitute("g1", "sub_a")
        before = self.api.get_notifications("coach", {"team_id": "home"})["unread"]
        self.api.decline_substitute("g1", "sub_a")
        after = self.api.get_notifications("coach", {"team_id": "home"})["unread"]
        self.assertEqual(after, before + 1)

    def test_transitions_are_audited(self):
        self.api.offer_substitute("g1", "sub_a", actor_id="user_coach")
        self.api.decline_substitute("g1", "sub_a", actor_id="user_sub_a")
        actions = {a.action for a in self.store.audit_for_game("g1")
                   if a.subject_player_id == "sub_a"}
        self.assertIn("substitute_offered", actions)
        self.assertIn("substitute_declined", actions)


class OutreachHttpTest(unittest.TestCase):
    """Route scoping: the candidate queue is coach/operator-only; the player
    accept-offer/decline-offer routes resolve identity from the session."""

    def setUp(self):
        srv.STATE.reset()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.store = srv.STATE.api.store
        self.pid = srv.STATE.ids["selected_player_id"]
        self.team_id = self.store.get_player(self.pid).team_id
        self.gid = self.store.next_id("game")
        self.store.add_game(Game(
            id=self.gid, home_team_id=self.team_id, away_team_id="opp112",
            start_time=datetime(2030, 1, 1, 18, tzinfo=UTC),
            end_time=datetime(2030, 1, 1, 19, tzinfo=UTC),
            division_id=self.store.get_team(self.team_id).division_id,
            published=True, target_goalies=1, target_skaters=2))

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()

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

    def test_coach_can_read_candidate_queue(self):
        c = self._login("coach")
        status, q = self._get(c, f"/api/games/{self.gid}/substitute-candidates")
        self.assertEqual(status, 200)
        self.assertIn("candidates", q)

    def test_player_forbidden_from_candidate_queue(self):
        c = self._login("player")
        status, _ = self._get(c, f"/api/games/{self.gid}/substitute-candidates")
        self.assertEqual(status, 403)

    def test_full_offer_accept_flow_over_http(self):
        # Player self-enrolls, coach offers, player accepts — all via routes.
        player = self._login("player")
        self._post(player, f"/api/me/substitute-opportunities/{self.gid}/enroll", {})
        coach = self._login("coach")
        status, _ = self._post(
            coach, f"/api/games/{self.gid}/substitutes/{self.pid}/offer", {})
        self.assertEqual(status, 200)
        # The offered player now sees accept/decline in their detail.
        _, detail = self._get(
            player, f"/api/me/substitute-opportunities/{self.gid}")
        self.assertTrue(detail["can_accept_offer"])
        status, _ = self._post(
            player, f"/api/me/substitute-opportunities/{self.gid}/accept-offer", {})
        self.assertEqual(status, 200)
        self.assertIsNotNone(self.store.roster_entry_for_player(self.gid, self.pid))

    def test_accept_offer_requires_player_session(self):
        anon = urllib.request.build_opener()
        status, _ = self._post(
            anon, f"/api/me/substitute-opportunities/{self.gid}/accept-offer", {})
        self.assertEqual(status, 401)

    def test_accept_offer_forbidden_for_coach_role(self):
        c = self._login("coach")
        status, _ = self._post(
            c, f"/api/me/substitute-opportunities/{self.gid}/accept-offer", {})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
