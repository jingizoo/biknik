"""Substitute opportunity detail + response flow (#110).

Builds on #107's Player Home substitute opportunities. This file covers the new
detail view (``get_substitute_opportunity``) and its eligibility/blocked-reason
matrix, plus the signed-in-player scoped ``/api/me/substitute-opportunities``
routes (detail + accept/decline) — no ``player_id`` is passed from the browser,
identity comes from the session. The accept/decline actions reuse the existing
audited enroll/withdraw workflow rather than a parallel one.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import timedelta, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.parse import quote

from helpers import BACKEND, FakeClock  # noqa: F401  (BACKEND sets up sys.path)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Division, Game, LeagueSeason, Player, Position, Rink, Team)
from hockey_scheduler.store import InMemoryStore
from hockey_scheduler.web import server as srv

UTC = timezone.utc


def _seeded_api():
    """Home + away team, a future published game with the goalie slot filled so
    only a skater slot stays open — mirrors test_player_home's fixture."""
    s = InMemoryStore()
    s.add_league_season(LeagueSeason(id="ls", league_id="l", season_id="se"))
    s.add_division(Division(id="d", league_season_id="ls", name="D1"))
    s.add_rink(Rink(id="r1", venue_id="v", name="Main"))
    s.add_team(Team(id="home", name="Home", division="D1", division_id="d"))
    s.add_team(Team(id="away", name="Away", division="D1", division_id="d"))
    s.add_team(Team(id="other", name="Other", division="D1", division_id="d"))
    s.add_player(Player(id="goalie_rostered", team_id="home", name="G0",
                        position=Position.GOALIE))
    s.add_player(Player(id="goalie1", team_id="home", name="G1",
                        position=Position.GOALIE))
    s.add_player(Player(id="skater1", team_id="home", name="S1",
                        position=Position.FORWARD))
    s.add_player(Player(id="skater2", team_id="home", name="S2",
                        position=Position.FORWARD))
    s.add_player(Player(id="other_skater", team_id="other", name="OS",
                        position=Position.FORWARD))
    api = ApiService(s)
    api.roster.clock = FakeClock()
    base = api.roster.clock()
    g = Game(id="g1", home_team_id="home", away_team_id="away",
             start_time=base + timedelta(days=1),
             end_time=base + timedelta(days=1, hours=1),
             division_id="d", published=True, rink="Rink 1",
             target_goalies=1, target_skaters=2)
    s.add_game(g)
    api.select_roster("g1", ["goalie_rostered", "skater1"])
    return api, s, base


class SubstituteOpportunityDetailTest(unittest.TestCase):
    def setUp(self):
        self.api, self.store, self.base = _seeded_api()

    def test_eligible_skater_can_accept(self):
        d = self.api.get_substitute_opportunity("skater2", "g1")
        self.assertTrue(d["can_accept"])
        self.assertFalse(d["can_withdraw"])
        self.assertIsNone(d["blocked_reason"])
        self.assertEqual(d["opponent_name"], "Away")
        self.assertEqual(d["position_needed"], "skater")
        self.assertEqual(d["open_skater_slots"], 1)
        self.assertEqual(d["team_status"], "short")

    def test_after_enroll_detail_offers_withdraw(self):
        self.api.enroll_substitute("g1", "skater2")
        d = self.api.get_substitute_opportunity("skater2", "g1")
        self.assertFalse(d["can_accept"])
        self.assertTrue(d["can_withdraw"])
        self.assertEqual(d["enrollment_status"], "enrolled")
        # An enrolled player is never "blocked" — Withdraw is always offered.
        self.assertIsNone(d["blocked_reason"])

    def test_goalie_with_no_open_slot_is_blocked_with_reason(self):
        d = self.api.get_substitute_opportunity("goalie1", "g1")
        self.assertFalse(d["can_accept"])
        self.assertFalse(d["can_withdraw"])
        self.assertIn("open slot", d["blocked_reason"])

    def test_already_rostered_player_is_blocked_with_reason(self):
        d = self.api.get_substitute_opportunity("skater1", "g1")
        self.assertFalse(d["can_accept"])
        self.assertIn("already on the roster", d["blocked_reason"])

    def test_locked_game_is_blocked_with_reason(self):
        self.api.lock_roster("g1")
        d = self.api.get_substitute_opportunity("skater2", "g1")
        self.assertFalse(d["can_accept"])
        self.assertIn("locked", d["blocked_reason"])

    def test_enrolled_player_cannot_withdraw_from_locked_game(self):
        # Withdrawal goes through _guard_mutable, so once enrolled, a lock
        # blocks withdraw too — the detail must not offer a dead-end button.
        self.api.enroll_substitute("g1", "skater2")
        self.api.lock_roster("g1")
        d = self.api.get_substitute_opportunity("skater2", "g1")
        self.assertFalse(d["can_withdraw"])
        self.assertFalse(d["can_accept"])
        self.assertIn("locked", d["blocked_reason"])

    def test_enrolled_player_cannot_withdraw_from_cancelled_game(self):
        self.api.enroll_substitute("g1", "skater2")
        self.api.cancel_game("g1")
        d = self.api.get_substitute_opportunity("skater2", "g1")
        self.assertFalse(d["can_withdraw"])
        self.assertIn("cancelled", d["blocked_reason"])

    def test_cancelled_game_is_blocked_with_reason(self):
        self.api.cancel_game("g1")
        d = self.api.get_substitute_opportunity("skater2", "g1")
        self.assertFalse(d["can_accept"])
        self.assertIn("cancelled", d["blocked_reason"])

    def test_cross_team_player_gets_not_found_no_info_leak(self):
        # A player whose team isn't in the game must not even confirm it
        # exists — cross-team borrowing is off, and we don't leak the fixture.
        result = self.api.get_substitute_opportunity("other_skater", "g1")
        self.assertIn("error", result)
        self.assertEqual(result["error"]["code"], "not_found")

    def test_unknown_game_is_not_found(self):
        result = self.api.get_substitute_opportunity("skater2", "nope")
        self.assertIn("error", result)
        self.assertEqual(result["error"]["code"], "not_found")

    def test_accept_then_withdraw_round_trips_state(self):
        # The scoped actions are the existing enroll/withdraw services.
        self.api.enroll_substitute("g1", "skater2")
        self.assertTrue(
            self.api.get_substitute_opportunity("skater2", "g1")["can_withdraw"])
        self.api.withdraw_substitute("g1", "skater2")
        # Withdrawn → eligible to accept again, back in the opportunity pool.
        d = self.api.get_substitute_opportunity("skater2", "g1")
        self.assertTrue(d["can_accept"])
        ids = [o["game_id"]
               for o in self.api.get_player_home("skater2")["substitute_opportunities"]]
        self.assertIn("g1", ids)

    def test_blocked_opportunity_is_absent_from_home_list(self):
        # The detail view explains a block; the Home list simply omits it.
        # (goalie1 has no open goalie slot on g1.)
        ids = [o["game_id"]
               for o in self.api.get_player_home("goalie1")["substitute_opportunities"]]
        self.assertNotIn("g1", ids)

    def test_same_team_offer_read_and_accept_stay_open_at_exact_start(self):
        """#287's half-open boundary must not rewrite legacy same-team UX."""
        self.api.enroll_substitute("g1", "skater2")
        self.api.offer_substitute("g1", "skater2")
        drop = self.store.get_game("g1").start_time
        self.api.roster.clock = lambda: drop

        offers = self.api.get_player_home("skater2")["substitute_offers"]
        self.assertEqual([row["game_id"] for row in offers], ["g1"])
        self.assertTrue(offers[0]["can_accept_offer"])
        accepted = self.api.accept_substitute("g1", "skater2")
        self.assertNotIn("error", accepted)


class SubstituteOpportunityHttpTest(unittest.TestCase):
    """Signed-in-player scoping for the /api/me/substitute-opportunities
    routes: identity comes from the session, never a request field."""

    def setUp(self):
        srv.STATE.reset()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.store = srv.STATE.api.store
        self.pid = srv.STATE.ids["selected_player_id"]
        team_id = self.store.get_player(self.pid).team_id
        # A future published game for this player's team with both slots open,
        # so whatever position the demo player is, they have a matching slot.
        # The demo ApiService uses the real wall clock, so anchor far ahead.
        from datetime import datetime
        self.gid = self.store.next_id("game")
        self.store.add_game(Game(
            id=self.gid, home_team_id=team_id, away_team_id="opp110",
            start_time=datetime(2030, 1, 1, 18, tzinfo=UTC),
            end_time=datetime(2030, 1, 1, 19, tzinfo=UTC),
            division_id=self.store.get_team(team_id).division_id,
            published=True, target_goalies=1, target_skaters=2))
        # A game between two teams the demo player is NOT on (for the 404 case).
        self.store.add_team(Team(id="t110a", name="A110", division_id="d110"))
        self.store.add_team(Team(id="t110b", name="B110", division_id="d110"))
        self.other_gid = self.store.next_id("game")
        self.store.add_game(Game(
            id=self.other_gid, home_team_id="t110a", away_team_id="t110b",
            start_time=datetime(2030, 1, 2, 18, tzinfo=UTC),
            end_time=datetime(2030, 1, 2, 19, tzinfo=UTC),
            division_id="d110", published=True, target_skaters=2))

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

    def test_detail_and_accept_decline_round_trip(self):
        c = self._login("player")
        status, d = self._get(c, f"/api/me/substitute-opportunities/{self.gid}")
        self.assertEqual(status, 200)
        self.assertTrue(d["can_accept"])
        # Accept enrols via the scoped route (no player_id in the body).
        status, _ = self._post(
            c, f"/api/me/substitute-opportunities/{self.gid}/enroll", {})
        self.assertEqual(status, 200)
        _, d2 = self._get(c, f"/api/me/substitute-opportunities/{self.gid}")
        self.assertTrue(d2["can_withdraw"])
        # Decline withdraws.
        status, _ = self._post(
            c, f"/api/me/substitute-opportunities/{self.gid}/withdraw", {})
        self.assertEqual(status, 200)
        _, d3 = self._get(c, f"/api/me/substitute-opportunities/{self.gid}")
        self.assertTrue(d3["can_accept"])

    def test_detail_requires_a_player_session(self):
        anon = urllib.request.build_opener()  # no cookie
        status, _ = self._get(anon, f"/api/me/substitute-opportunities/{self.gid}")
        self.assertEqual(status, 401)

    def test_detail_forbidden_for_non_player_role(self):
        c = self._login("coach")
        status, _ = self._get(c, f"/api/me/substitute-opportunities/{self.gid}")
        self.assertEqual(status, 403)

    def test_accept_forbidden_for_non_player_role(self):
        c = self._login("coach")
        status, _ = self._post(
            c, f"/api/me/substitute-opportunities/{self.gid}/enroll", {})
        self.assertEqual(status, 403)

    def test_detail_for_unrelated_game_is_not_found(self):
        c = self._login("player")
        status, _ = self._get(
            c, f"/api/me/substitute-opportunities/{self.other_gid}")
        self.assertEqual(status, 404)

    def test_detail_forwards_compound_target_identity(self):
        """The target side is explicit; player identity remains session-only."""
        c = self._login("player")
        target = "bronze team/4"
        with mock.patch.object(
                srv.STATE.api, "get_substitute_opportunity",
                return_value={"game_id": self.gid,
                              "target_team_id": target}) as get_detail:
            status, body = self._get(
                c,
                f"/api/me/substitute-opportunities/{self.gid}"
                f"?target_team_id={quote(target, safe='')}")
        self.assertEqual(status, 200)
        self.assertEqual(body["target_team_id"], target)
        get_detail.assert_called_once_with(
            self.pid, self.gid, target_team_id=target)

    def test_detail_without_target_preserves_same_team_contract(self):
        c = self._login("player")
        with mock.patch.object(
                srv.STATE.api, "get_substitute_opportunity",
                return_value={"game_id": self.gid}) as get_detail:
            status, _body = self._get(
                c, f"/api/me/substitute-opportunities/{self.gid}")
        self.assertEqual(status, 200)
        get_detail.assert_called_once_with(
            self.pid, self.gid, target_team_id=None)

    def test_enroll_forwards_target_but_never_accepts_player_identity(self):
        c = self._login("player")
        target = "bronze-team-4"
        with mock.patch.object(
                srv.STATE.api, "enroll_substitute",
                return_value={"game_id": self.gid,
                              "player_id": self.pid,
                              "target_team_id": target}) as enroll:
            status, body = self._post(
                c, f"/api/me/substitute-opportunities/{self.gid}/enroll",
                {"target_team_id": target})
        self.assertEqual(status, 200)
        self.assertEqual(body["target_team_id"], target)
        enroll.assert_called_once()
        args, kwargs = enroll.call_args
        self.assertEqual(args, (self.gid, self.pid))
        self.assertEqual(kwargs["target_team_id"], target)
        self.assertIsNotNone(kwargs["actor_id"])

        with mock.patch.object(
                srv.STATE.api, "enroll_substitute") as enroll:
            status, body = self._post(
                c, f"/api/me/substitute-opportunities/{self.gid}/enroll",
                {"target_team_id": target, "player_id": "someone-else"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        self.assertEqual(body["error"]["details"]["fields"], ["player_id"])
        enroll.assert_not_called()

    def test_enroll_target_is_optional_but_when_present_must_be_string(self):
        c = self._login("player")
        with mock.patch.object(
                srv.STATE.api, "enroll_substitute",
                return_value={"game_id": self.gid}) as enroll:
            status, _body = self._post(
                c, f"/api/me/substitute-opportunities/{self.gid}/enroll", {})
        self.assertEqual(status, 200)
        args, kwargs = enroll.call_args
        self.assertEqual(args, (self.gid, self.pid))
        self.assertIsNone(kwargs["target_team_id"])

        non_strings = (None, True, 4, ["team-4"], {"id": "team-4"})
        for value in non_strings:
            with self.subTest(value=value), mock.patch.object(
                    srv.STATE.api, "enroll_substitute") as enroll:
                status, body = self._post(
                    c,
                    f"/api/me/substitute-opportunities/{self.gid}/enroll",
                    {"target_team_id": value})
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["details"], {
                    "reason": "wrong_type", "field": "target_team_id"})
                enroll.assert_not_called()

        for value in ("", " ", "\t\n"):
            with self.subTest(value=value), mock.patch.object(
                    srv.STATE.api, "enroll_substitute") as enroll:
                status, body = self._post(
                    c,
                    f"/api/me/substitute-opportunities/{self.gid}/enroll",
                    {"target_team_id": value})
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["details"], {
                    "reason": "field_required", "field": "target_team_id"})
                enroll.assert_not_called()

    def test_legacy_enroll_route_still_enforces_player_scope(self):
        # #110 keeps the existing /api/games/{gid}/substitutes/enroll route.
        # A player must not be able to enrol a DIFFERENT player through it —
        # scope enforcement (scope.py) must still hold (issue §7 regression).
        other = next(p.id for p in self.store.all_players()
                     if p.id != self.pid)
        c = self._login("player")
        status, _ = self._post(
            c, f"/api/games/{self.gid}/substitutes/enroll", {"player_id": other})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
