"""Coach/player availability UX helpers (#89).

An availability summary rolls a team's players into available / unavailable /
maybe / no_response buckets; a one-click reminder sends a player-targeted nudge
to each player who hasn't responded (delivery honoring each player's channel
preferences, #81). Access follows the same privacy gate as the other private
per-game data (#73), plus a team-level scope check: a coach/player may read only
their own team's summary, an operator any team in the game, a viewer none.
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
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.web import server as srv


class AvailabilitySummaryServiceTest(unittest.TestCase):
    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store()
        self.api = ApiService(self.store)
        self.home = self.ids["home_team_id"]

    def test_summary_covers_every_team_player_once(self):
        s = self.api.get_availability_summary(self.game_id, self.home)
        players = self.store.players_for_team(self.home)
        self.assertEqual(len(s["players"]), len(players))
        self.assertEqual(sum(s["counts"].values()), len(players))

    def test_summary_matches_recorded_responses(self):
        # Set two known responses, then check the buckets reflect them.
        players = sorted(self.store.players_for_team(self.home), key=lambda p: p.name)
        self.api.set_availability(self.game_id, players[0].id, "available")
        self.api.set_availability(self.game_id, players[1].id, "unavailable")
        s = self.api.get_availability_summary(self.game_id, self.home)
        by_id = {p["player_id"]: p["status"] for p in s["players"]}
        self.assertEqual(by_id[players[0].id], "available")
        self.assertEqual(by_id[players[1].id], "unavailable")

    def test_summary_rejects_team_not_in_game(self):
        res = self.api.get_availability_summary(self.game_id, "team_elsewhere")
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_remind_counts_and_emits_only_when_pending(self):
        # Force everyone available → no reminder emitted.
        for p in self.store.players_for_team(self.home):
            self.api.set_availability(self.game_id, p.id, "available")
        before = len(self.store.all_notifications_feed())
        res = self.api.remind_unresponded(self.game_id, self.home)
        self.assertEqual(res["reminded"], 0)
        self.assertEqual(len(self.store.all_notifications_feed()), before)

    def test_remind_targets_only_unresponded_players(self):
        # The reminder must actually reach the unresponded players — one
        # player-addressed notification each, routed to player:<id>, not a
        # single team/coach notification (#89 review, blocker 1). Drive the
        # split deterministically: everyone available, then a known subset back
        # to pending (which reads as no_response).
        from hockey_scheduler.domain import (
            NotificationAudience, NotificationKind)
        players = sorted(self.store.players_for_team(self.home),
                         key=lambda p: p.name)
        for p in players:
            self.api.set_availability(self.game_id, p.id, "available")
        expected = {p.id for p in players[:3]}
        for pid in expected:
            self.api.set_availability(self.game_id, pid, "pending")
        responded = players[3].id  # stays available → must not be reminded

        res = self.api.remind_unresponded(self.game_id, self.home)
        self.assertEqual(res["reminded"], len(expected))
        reminders = [n for n in self.store.all_notifications_feed()
                     if n.kind == NotificationKind.AVAILABILITY_REMINDER]
        # Exactly the unresponded players, each addressed to themselves.
        self.assertEqual({n.audience_ref for n in reminders}, expected)
        self.assertTrue(all(n.audience == NotificationAudience.PLAYER
                            for n in reminders))
        self.assertNotIn(responded, {n.audience_ref for n in reminders})
        # Deliveries route to the individual player, never the team.
        recips = {d.recipient_ref for n in reminders
                  for d in self.store.deliveries_for_notification(n.id)}
        self.assertTrue(recips)
        self.assertTrue(all(r.startswith("player:") for r in recips))
        self.assertNotIn(f"team:{self.home}", recips)

    def test_remind_honors_each_players_channel_optout(self):
        # #81 preferences apply per player recipient: a player who muted email
        # gets no email delivery for the reminder, but is still reminded on the
        # remaining channel(s).
        from hockey_scheduler.domain import (
            NotificationChannel, NotificationKind)
        muted = sorted(self.store.players_for_team(self.home),
                       key=lambda p: p.name)[0].id
        self.api.set_availability(self.game_id, muted, "pending")  # unresponded
        self.api.set_notification_preference(f"player:{muted}", "email", False)
        self.api.remind_unresponded(self.game_id, self.home)
        reminder = next(
            n for n in self.store.all_notifications_feed()
            if n.kind == NotificationKind.AVAILABILITY_REMINDER
            and n.audience_ref == muted)
        channels = {d.channel
                    for d in self.store.deliveries_for_notification(reminder.id)}
        self.assertNotIn(NotificationChannel.EMAIL, channels)
        self.assertTrue(channels)  # still delivered on the other channel(s)


class AvailabilityUxHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.game_id = srv.STATE.game_id
        cls.home = srv.STATE.ids["home_team_id"]
        cls.away = srv.STATE.ids["away_team_id"]

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

    def test_scoped_coach_sees_own_team_summary(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "coach", "password": "demo"})
        status, body = self._req(
            c, "GET", f"/api/games/{self.game_id}/availability-summary?team_id={self.home}")
        self.assertEqual(status, 200)
        self.assertIn("counts", body)

    def test_viewer_cannot_access_availability_summary(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "viewer", "password": "demo"})
        status, _ = self._req(
            c, "GET", f"/api/games/{self.game_id}/availability-summary?team_id={self.home}")
        self.assertEqual(status, 403)

    def test_scoped_coach_cannot_read_opponent_summary(self):
        # The #73 gate lets a home coach read this game's private data, but the
        # team-level scope check must still block reading the *opponent's*
        # availability (#89 review, blocker 2).
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "coach", "password": "demo"})
        status, _ = self._req(
            c, "GET", f"/api/games/{self.game_id}/availability-summary?team_id={self.away}")
        self.assertEqual(status, 403)

    def test_scoped_player_cannot_read_opponent_summary(self):
        # #160: the demo player's scope stores player_id only; its own team is
        # resolved live, so the opponent-summary block still holds (a stripped
        # team_id must not silently skip the check).
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "player", "password": "demo"})
        status, _ = self._req(
            c, "GET", f"/api/games/{self.game_id}/availability-summary?team_id={self.away}")
        self.assertEqual(status, 403)

    def test_scoped_player_sees_own_team_summary(self):
        # #160: the same player_id-only account CAN read its own team's summary,
        # its team resolved live from player_id.
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "player", "password": "demo"})
        status, body = self._req(
            c, "GET", f"/api/games/{self.game_id}/availability-summary?team_id={self.home}")
        self.assertEqual(status, 200)
        self.assertIn("counts", body)

    def test_operator_can_read_either_team_summary(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        for team in (self.home, self.away):
            status, body = self._req(
                c, "GET",
                f"/api/games/{self.game_id}/availability-summary?team_id={team}")
            self.assertEqual(status, 200)
            self.assertIn("counts", body)

    def test_player_cannot_remind_but_coach_can(self):
        player = self._client()
        self._req(player, "POST", "/api/auth/login", {"username": "player", "password": "demo"})
        status, _ = self._req(
            player, "POST", f"/api/games/{self.game_id}/availability/remind",
            {"team_id": self.home})
        self.assertEqual(status, 403)  # players don't hold MANAGE_ROSTER

        coach = self._client()
        self._req(coach, "POST", "/api/auth/login", {"username": "coach", "password": "demo"})
        status, body = self._req(
            coach, "POST", f"/api/games/{self.game_id}/availability/remind",
            {"team_id": self.home})
        self.assertEqual(status, 200)
        self.assertIn("reminded", body)


if __name__ == "__main__":
    unittest.main()
