import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import Role
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.web import server as srv
from hockey_scheduler.web.scope import scope_violation


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

    def test_unbound_coach_not_scoped(self):
        # Dev header fallback: no binding → not resource-scoped.
        self.assertIsNone(self._v(Role.COACH, {},
                                  "/api/games/g/roster/select",
                                  {"player_ids": [self.away_player]}))

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


if __name__ == "__main__":
    unittest.main()
