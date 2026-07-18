import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import Player, Position, Role, Team
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.web import server as srv
from hockey_scheduler.web.scope import can_read_private_game_data, scope_violation


class ScopeUnitTest(unittest.TestCase):
    """Pure resource-scoping policy (#51)."""

    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store()
        self.home = self.ids["home_team_id"]   # Lions
        self.away = self.ids["away_team_id"]    # Falcons
        self.home_player = self.store.players_for_team(self.home)[0].id
        self.away_player = self.store.players_for_team(self.away)[0].id

    def _v(self, role, scope, path, body):
        return scope_violation(role, scope, path, body, self.store)

    def test_admin_and_arena_never_scoped(self):
        for role in (Role.LEAGUE_ADMIN, Role.ARENA_MANAGER):
            self.assertIsNone(self._v(role, {}, "/api/games/g/roster/select",
                                      {"player_ids": [self.away_player]}))

    def test_coach_own_team_allowed(self):
        scope = {"team_id": self.home}
        self.assertIsNone(self._v(Role.COACH, scope,
                                  "/api/games/g/roster/select",
                                  {"player_ids": [self.home_player]}))

    def test_coach_other_team_player_blocked(self):
        scope = {"team_id": self.home}
        self.assertIsNotNone(self._v(Role.COACH, scope,
                                     "/api/games/g/roster/select",
                                     {"player_ids": [self.away_player]}))

    def test_coach_other_team_build_blocked(self):
        scope = {"team_id": self.home}
        self.assertIsNotNone(self._v(Role.COACH, scope,
                                     "/api/games/g/build-roster",
                                     {"team_id": self.away}))
        self.assertIsNone(self._v(Role.COACH, scope,
                                  "/api/games/g/build-roster",
                                  {"team_id": self.home}))

    def test_coach_remove_other_team_blocked(self):
        scope = {"team_id": self.home}
        self.assertIsNotNone(self._v(Role.COACH, scope,
                                     "/api/games/g/roster/remove",
                                     {"player_id": self.away_player}))

    def test_unbound_coach_fails_closed(self):
        # #266: a Coach with no team binding — an account created/left without a
        # valid team — has NO roster authority and is refused, rather than being
        # silently treated as unscoped and allowed to touch any team's roster.
        self.assertIsNotNone(self._v(Role.COACH, {},
                                     "/api/games/g/roster/select",
                                     {"player_ids": [self.away_player]}))

    def test_unbound_coach_dev_fallback_allowed_only_when_opted_in(self):
        # The demo-only X-Demo-Role fallback (an identity-less coach session)
        # stays permitted, but ONLY when the caller opts in explicitly. The
        # server sets that flag solely for the header path and never in
        # production, so the fallback is impossible to activate in a real
        # deployment (#266).
        self.assertIsNone(scope_violation(
            Role.COACH, {}, "/api/games/g/roster/select",
            {"player_ids": [self.away_player]}, self.store,
            allow_unscoped_dev_fallback=True))

    def test_scoped_coach_cannot_lock_unlock_or_cancel(self):
        scope = {"team_id": self.home}
        for path in ("/api/games/g/roster/lock",
                     "/api/games/g/roster/unlock",
                     "/api/games/g/cancel"):
            self.assertIsNotNone(self._v(Role.COACH, scope, path, {}), path)

    def test_admin_can_lock_unlock_cancel(self):
        for path in ("/api/games/g/roster/lock", "/api/games/g/cancel"):
            self.assertIsNone(self._v(Role.LEAGUE_ADMIN, {}, path, {}), path)

    def test_player_self_only(self):
        scope = {"player_id": self.home_player}
        self.assertIsNone(self._v(Role.PLAYER, scope,
                                  "/api/games/g/availability",
                                  {"player_id": self.home_player}))
        self.assertIsNotNone(self._v(Role.PLAYER, scope,
                                     "/api/games/g/availability",
                                     {"player_id": self.away_player}))

    def test_player_path_pid_scoped(self):
        scope = {"player_id": self.home_player}
        other = f"/api/games/g/substitutes/{self.away_player}/accept"
        mine = f"/api/games/g/substitutes/{self.home_player}/accept"
        self.assertIsNotNone(self._v(Role.PLAYER, scope, other, {}))
        self.assertIsNone(self._v(Role.PLAYER, scope, mine, {}))

    def test_unbound_player_fails_closed(self):
        # #282: a Player with no player_id binding has no self-service identity
        # and is refused, rather than being silently treated as unscoped and
        # allowed to respond for any player id.
        self.assertIsNotNone(self._v(Role.PLAYER, {},
                                     "/api/games/g/availability",
                                     {"player_id": self.away_player}))

    def test_unbound_player_dev_fallback_allowed_only_when_opted_in(self):
        # The demo-only X-Demo-Role fallback (an identity-less player session)
        # stays permitted, but ONLY when the caller opts in explicitly — the
        # server sets that flag solely for the header path and never in
        # production (#282), mirroring the unbound-coach fallback.
        self.assertIsNone(scope_violation(
            Role.PLAYER, {}, "/api/games/g/availability",
            {"player_id": self.away_player}, self.store,
            allow_unscoped_dev_fallback=True))

    # -- private-read gate derives the Player's team from player_id (#160) ----
    def test_player_private_read_derives_team_from_player_id_only(self):
        # A Player scope carrying player_id ONLY (no team_id) must still resolve
        # the player's team, so the private-read gate grants own-team access.
        # This is the #160 mismatch: creation keys player scope on player_id, but
        # the gate historically read only team_id.
        self.assertTrue(can_read_private_game_data(
            Role.PLAYER, {"player_id": self.home_player}, self.game_id, self.store))

    def test_player_private_read_denied_for_unrelated_team(self):
        # A player whose team is not in the game cannot read its private data,
        # even when the team is derived from player_id.
        self.store.add_team(Team(id="team_third", name="Otters"))
        self.store.add_player(Player(id="p_out", team_id="team_third",
                                     name="Out", position=Position.FORWARD))
        self.assertFalse(can_read_private_game_data(
            Role.PLAYER, {"player_id": "p_out"}, self.game_id, self.store))

    def test_player_private_read_fails_closed_without_resolvable_team(self):
        # No player_id and no team_id → no team → denied (fail closed).
        self.assertFalse(can_read_private_game_data(
            Role.PLAYER, {}, self.game_id, self.store))
        # An unknown player_id resolves to no player, so still denied.
        self.assertFalse(can_read_private_game_data(
            Role.PLAYER, {"player_id": "p_ghost"}, self.game_id, self.store))

    def test_coach_private_read_still_keyed_on_team_id(self):
        # Coach behavior is unchanged: their canonical scope key is team_id.
        self.assertTrue(can_read_private_game_data(
            Role.COACH, {"team_id": self.home}, self.game_id, self.store))
        self.assertFalse(can_read_private_game_data(
            Role.COACH, {}, self.game_id, self.store))


class ScopeHttpTest(unittest.TestCase):
    """End-to-end: a bound session is scoped to its team / self (#51)."""

    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.gid = srv.STATE.game_id
        store = srv.STATE.api.store
        cls.home = srv.STATE.ids["home_team_id"]
        cls.away = srv.STATE.ids["away_team_id"]
        cls.away_player = store.players_for_team(cls.away)[0].id

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _post(self, opener, path, body):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _login(self, username):
        c = self._client()
        self._post(c, "/api/auth/login", {"username": username, "password": "demo"})
        return c

    def test_coach_cannot_build_other_team(self):
        c = self._login("coach")  # bound to home (Lions)
        status, body = self._post(c, f"/api/games/{self.gid}/build-roster",
                                  {"team_id": self.away})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

    def test_coach_can_build_own_team(self):
        c = self._login("coach")
        status, _ = self._post(c, f"/api/games/{self.gid}/build-roster",
                               {"team_id": self.home})
        self.assertNotEqual(status, 403)

    def test_coach_cannot_select_other_team_player(self):
        c = self._login("coach")
        status, _ = self._post(c, f"/api/games/{self.gid}/roster/select",
                               {"player_ids": [self.away_player]})
        self.assertEqual(status, 403)

    def test_player_cannot_respond_for_another(self):
        c = self._login("player")  # bound to a specific Lions player
        status, _ = self._post(c, f"/api/games/{self.gid}/availability",
                               {"player_id": self.away_player,
                                "availability_status": "available"})
        self.assertEqual(status, 403)

    def test_coach_cannot_lock_whole_game(self):
        c = self._login("coach")
        status, _ = self._post(c, f"/api/games/{self.gid}/roster/lock", {})
        self.assertEqual(status, 403)

    def test_coach_cannot_cancel_whole_game(self):
        c = self._login("coach")
        status, _ = self._post(c, f"/api/games/{self.gid}/cancel", {})
        self.assertEqual(status, 403)

    def test_admin_can_lock_whole_game(self):
        c = self._login("admin")
        status, _ = self._post(c, f"/api/games/{self.gid}/roster/lock", {})
        self.assertNotEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
