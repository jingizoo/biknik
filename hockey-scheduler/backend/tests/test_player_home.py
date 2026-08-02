"""Player Home Page (#107): next game, attendance, substitutes, notifications.

The backend adds no new attendance/checkout mechanism — "I'm In" and "Can't
Play" reuse the existing ``set_availability`` facade (the confirm/back-out
flow #107's own doc calls out as already covered). This file covers the two
genuinely new pieces: finding a player's next game and their substitute
opportunities, plus the ``/api/me/player-home`` route's identity scoping.
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
from hockey_scheduler.domain import (
    Division, Game, IceSlot, LeagueSeason, NotificationAudience,
    NotificationKind, Player, Position, Rink, Team, Venue)
from hockey_scheduler.services.notifier import push as push_notification
from hockey_scheduler.store import InMemoryStore
from hockey_scheduler.web import server as srv

UTC = timezone.utc


def _seeded_api():
    """One division, one rink, a home + away team, and a FakeClock so
    "next game"/"substitute opportunity" future-vs-past logic is
    deterministic rather than wall-clock dependent (#107 is the first
    feature in this codebase to filter on real elapsed time)."""
    s = InMemoryStore()
    s.add_league_season(LeagueSeason(id="ls", league_id="l", season_id="se"))
    s.add_division(Division(id="d", league_season_id="ls", name="D1"))
    s.add_rink(Rink(id="r1", venue_id="v", name="Main"))
    s.add_team(Team(id="home", name="Home", division="D1", division_id="d"))
    s.add_team(Team(id="away", name="Away", division="D1", division_id="d"))
    api = ApiService(s)
    clock = FakeClock()
    api.roster.clock = clock
    base = clock()  # anchor test games relative to the fixed clock, not wall time
    return api, s, base


class PlayerHomeNextGameTest(unittest.TestCase):
    def setUp(self):
        self.api, self.store, self.base = _seeded_api()
        self.store.add_player(Player(id="p_goalie", team_id="home", name="Goalie",
                                     position=Position.GOALIE))
        self.store.add_player(Player(id="p_skater", team_id="home", name="Skater",
                                     position=Position.FORWARD))

    def _add_game(self, gid, start, published=True, cancelled=False,
                 is_draft=False, home="home", away="away",
                 target_goalies=1, target_skaters=1):
        g = Game(id=gid, home_team_id=home, away_team_id=away,
                 start_time=start, end_time=start + timedelta(hours=1),
                 division_id="d", published=published, cancelled=cancelled,
                 is_draft=is_draft, target_goalies=target_goalies,
                 target_skaters=target_skaters)
        self.store.add_game(g)
        return g

    def test_no_upcoming_game_is_empty_state(self):
        home = self.api.get_player_home("p_skater")
        self.assertIsNone(home["next_game"])

    def test_picks_soonest_future_game(self):
        self._add_game("g_far", self.base + timedelta(days=10))
        self._add_game("g_near", self.base + timedelta(days=2))
        home = self.api.get_player_home("p_skater")
        self.assertEqual(home["next_game"]["game_id"], "g_near")

    def test_excludes_past_games(self):
        self._add_game("g_past", self.base - timedelta(days=1))
        home = self.api.get_player_home("p_skater")
        self.assertIsNone(home["next_game"])

    def test_excludes_unpublished_and_draft_and_cancelled_games(self):
        self._add_game("g_unpub", self.base + timedelta(days=1), published=False)
        self._add_game("g_draft", self.base + timedelta(days=1), is_draft=True)
        self._add_game("g_cancelled", self.base + timedelta(days=1), cancelled=True)
        home = self.api.get_player_home("p_skater")
        self.assertIsNone(home["next_game"])

    def test_opponent_resolves_to_the_other_side(self):
        # The player's team is on the AWAY side here — opponent must still
        # resolve to the OTHER team, not the player's own team.
        self._add_game("g_other", self.base + timedelta(days=1),
                       home="away", away="home")
        home = self.api.get_player_home("p_skater")
        self.assertEqual(home["next_game"]["game_id"], "g_other")
        self.assertEqual(home["next_game"]["opponent_name"], "Away")

    def test_venue_and_rink_names_resolved(self):
        self.store.add_venue(Venue(id="v", name="Nord Arena"))
        slot = IceSlot(id="s1", rink_id="r1",
                      start_time=self.base + timedelta(days=1),
                      end_time=self.base + timedelta(days=1, hours=1))
        self.store.add_ice_slot(slot)
        g = self._add_game("g1", self.base + timedelta(days=1))
        g.ice_slot_id = "s1"
        g.rink = "Main"
        self.store.save_game(g)
        home = self.api.get_player_home("p_skater")
        self.assertEqual(home["next_game"]["venue_name"], "Nord Arena")
        self.assertEqual(home["next_game"]["rink_name"], "Main")

    def test_inactive_player_sees_no_next_game(self):
        self._add_game("g1", self.base + timedelta(days=1))
        p = self.store.get_player("p_skater")
        p.is_active = False
        self.store.save_player(p)
        home = self.api.get_player_home("p_skater")
        self.assertIsNone(home["next_game"])

    def test_today_count_counts_only_todays_visible_games(self):
        # The FakeClock ticks ~1s per call from 2026-01-01 — all "base +
        # hours" games land on the clock's own current date.
        self._add_game("g_today", self.base + timedelta(hours=2))
        self._add_game("g_today2", self.base + timedelta(hours=4))
        self._add_game("g_tomorrow", self.base + timedelta(days=1))
        self._add_game("g_today_cancelled", self.base + timedelta(hours=3),
                       cancelled=True)
        self._add_game("g_today_draft", self.base + timedelta(hours=3),
                       is_draft=True)
        home = self.api.get_player_home("p_skater")
        self.assertEqual(home["today_count"], 2)


class PlayerHomeAttendanceTest(unittest.TestCase):
    def setUp(self):
        self.api, self.store, self.base = _seeded_api()
        self.store.add_player(Player(id="p1", team_id="home", name="P1",
                                     position=Position.FORWARD))
        self.store.add_player(Player(id="p2", team_id="home", name="P2",
                                     position=Position.FORWARD))
        g = Game(id="g1", home_team_id="home", away_team_id="away",
                start_time=self.base + timedelta(days=1),
                end_time=self.base + timedelta(days=1, hours=1),
                division_id="d", published=True,
                target_goalies=0, target_skaters=2)
        self.store.add_game(g)
        self.api.select_roster("g1", ["p1", "p2"])

    def test_not_responded_before_any_action(self):
        # Both players are selected (slots occupied) but neither has
        # confirmed yet -> AWAITING_RESPONSES, which reads as "not_responded".
        home = self.api.get_player_home("p1")
        self.assertEqual(home["next_game"]["attendance_status"], "not_responded")
        self.assertEqual(home["next_game"]["team_status"], "not_responded")

    def test_im_in_confirms_attendance(self):
        self.api.set_availability("g1", "p1", "available")
        home = self.api.get_player_home("p1")
        self.assertEqual(home["next_game"]["attendance_status"], "confirmed")

    def test_cant_play_checks_out_and_refreshes_team_status(self):
        # Both players confirm first so the team reads "full"...
        self.api.set_availability("g1", "p1", "available")
        self.api.set_availability("g1", "p2", "available")
        self.assertEqual(
            self.api.get_player_home("p1")["next_game"]["team_status"], "full")
        # ...then one backs out: their own status flips, and the team status
        # recalculates to reflect the newly-open slot (#107 AC6).
        self.api.set_availability("g1", "p1", "unavailable")
        home = self.api.get_player_home("p1")
        self.assertEqual(home["next_game"]["attendance_status"], "checked_out")
        self.assertEqual(home["next_game"]["team_status"], "short")

    def test_maybe_reads_as_pending(self):
        self.api.set_availability("g1", "p1", "maybe")
        home = self.api.get_player_home("p1")
        self.assertEqual(home["next_game"]["attendance_status"], "pending")

    def test_attendance_change_is_scoped_to_the_acting_player_only(self):
        self.api.set_availability("g1", "p1", "available")
        # p2's own status is untouched by p1's action.
        home_p2 = self.api.get_player_home("p2")
        self.assertEqual(home_p2["next_game"]["attendance_status"], "not_responded")


class PlayerHomeSubstituteOpportunitiesTest(unittest.TestCase):
    def setUp(self):
        self.api, self.store, self.base = _seeded_api()
        self.store.add_division(Division(id="d2", league_season_id="ls", name="D2"))
        self.store.add_team(Team(id="other", name="Other", division="D2",
                                 division_id="d2"))
        self.store.add_player(Player(id="goalie_rostered", team_id="home",
                                     name="G0", position=Position.GOALIE))
        self.store.add_player(Player(id="goalie1", team_id="home", name="G1",
                                     position=Position.GOALIE))
        self.store.add_player(Player(id="skater1", team_id="home", name="S1",
                                     position=Position.FORWARD))
        self.store.add_player(Player(id="skater2", team_id="home", name="S2",
                                     position=Position.FORWARD))
        self.store.add_player(Player(id="away_skater", team_id="away", name="AS",
                                     position=Position.FORWARD))
        self.store.add_player(Player(id="other_skater", team_id="other",
                                     name="OS", position=Position.FORWARD))
        # The goalie slot is filled (goalie_rostered) so only a skater slot
        # stays open — isolates the "wrong position" case from "wrong team".
        g = Game(id="g1", home_team_id="home", away_team_id="away",
                start_time=self.base + timedelta(days=1),
                end_time=self.base + timedelta(days=1, hours=1),
                division_id="d", published=True,
                target_goalies=1, target_skaters=2)
        self.store.add_game(g)
        self.api.select_roster("g1", ["goalie_rostered", "skater1"])

    def test_open_skater_slot_shows_up_for_eligible_skater(self):
        home = self.api.get_player_home("skater2")
        ids = [o["game_id"] for o in home["substitute_opportunities"]]
        self.assertIn("g1", ids)
        self.assertEqual(home["substitute_opportunities"][0]["position_needed"],
                         "skater")

    def test_open_skater_slot_does_not_show_for_a_goalie(self):
        # The goalie slot is already filled (goalie_rostered) — goalie1 has
        # no open slot matching their position, even though a skater slot
        # is open on the very same game.
        home = self.api.get_player_home("goalie1")
        ids = [o["game_id"] for o in home["substitute_opportunities"]]
        self.assertNotIn("g1", ids)

    def test_players_own_away_side_slot_is_a_legitimate_opportunity(self):
        # Home and away rosters are computed independently (#25) — the away
        # team also has 2 unfilled skater slots on this same game, and an
        # away-side player is entitled to see THEIR OWN team's opening. This
        # is not cross-team borrowing; it's the away team's own vacancy.
        home = self.api.get_player_home("away_skater")
        ids = [o["game_id"] for o in home["substitute_opportunities"]]
        self.assertIn("g1", ids)

    def test_player_on_an_unrelated_team_gets_no_opportunity(self):
        # Cross-team borrowing is off (matches enroll_substitute's rule at
        # roster_service.py:363) — a player whose team isn't even playing in
        # this game must never see it as an opportunity.
        home = self.api.get_player_home("other_skater")
        ids = [o["game_id"] for o in home["substitute_opportunities"]]
        self.assertNotIn("g1", ids)

    def test_already_rostered_player_has_no_opportunity_for_their_own_game(self):
        home = self.api.get_player_home("skater1")
        ids = [o["game_id"] for o in home["substitute_opportunities"]]
        self.assertNotIn("g1", ids)

    def test_already_enrolled_player_not_shown_again(self):
        self.api.enroll_substitute("g1", "skater2")
        home = self.api.get_player_home("skater2")
        ids = [o["game_id"] for o in home["substitute_opportunities"]]
        self.assertNotIn("g1", ids)

    def test_opportunity_disappears_once_slot_filled(self):
        self.api.select_roster("g1", ["skater1", "skater2"])
        self.store.add_player(Player(id="skater3", team_id="home", name="S3",
                                     position=Position.FORWARD))
        home3 = self.api.get_player_home("skater3")
        ids = [o["game_id"] for o in home3["substitute_opportunities"]]
        self.assertNotIn("g1", ids)

    def test_locked_game_is_not_an_opportunity(self):
        # enroll_substitute rejects locked games (RosterLockedError via
        # _guard_mutable) — the Home page must not advertise a dead-end.
        self.api.lock_roster("g1")
        home = self.api.get_player_home("skater2")
        ids = [o["game_id"] for o in home["substitute_opportunities"]]
        self.assertNotIn("g1", ids)


class PlayerHomeNotificationsTest(unittest.TestCase):
    def setUp(self):
        self.api, self.store, self.base = _seeded_api()
        self.store.add_player(Player(id="p1", team_id="home", name="P1",
                                     position=Position.FORWARD))
        self.store.add_player(Player(id="p2", team_id="home", name="P2",
                                     position=Position.FORWARD))

    def _notify(self, player_id):
        push_notification(self.store, self.api.roster.clock,
                          NotificationKind.GAME_PUBLISHED, NotificationAudience.PLAYER,
                          "t", "m", audience_ref=player_id)

    def test_unread_count_scoped_to_this_player_only(self):
        self._notify("p1")
        self._notify("p1")
        self._notify("p2")
        self.assertEqual(self.api.get_player_home("p1")["unread_notifications"], 2)
        self.assertEqual(self.api.get_player_home("p2")["unread_notifications"], 1)


class PlayerHomeNotFoundTest(unittest.TestCase):
    def test_unknown_player_is_a_structured_not_found_error(self):
        api, _, _ = _seeded_api()
        result = api.get_player_home("no_such_player")
        self.assertIn("error", result)
        self.assertEqual(result["error"]["code"], "not_found")


class PlayerHomeHttpTest(unittest.TestCase):
    """Session/cookie identity scoping, mirroring test_official_identity.py's
    /api/me/assignments coverage for the analogous player route."""

    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.player_id = srv.STATE.ids["selected_player_id"]

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
        with opener.open(f"http://127.0.0.1:{self.port}{path}") as r:
            return json.loads(r.read() or b"{}")

    def test_player_home_bound_to_signed_in_player(self):
        c = self._login("player")
        home = self._get(c, "/api/me/player-home")
        self.assertEqual(home["player_id"], self.player_id)

    def test_player_home_empty_without_session(self):
        c = urllib.request.build_opener()  # no cookie
        home = self._get(c, "/api/me/player-home")
        self.assertIsNone(home["player_id"])
        self.assertIsNone(home["next_game"])
        self.assertEqual(home["substitute_opportunities"], [])

    def test_player_home_invalid_session_cookie_returns_401(self):
        c = urllib.request.build_opener()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/me/player-home", method="GET",
            headers={"Cookie": f"{srv.SESSION_COOKIE}=bogustoken"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            c.open(req)
        self.assertEqual(ctx.exception.code, 401)

    def test_player_home_empty_for_non_player_session(self):
        c = self._login("coach")
        home = self._get(c, "/api/me/player-home")
        self.assertIsNone(home["player_id"])


if __name__ == "__main__":
    unittest.main()
