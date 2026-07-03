import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND, cookie_from_set_cookie  # noqa: F401  (sets up sys.path)

from hockey_scheduler.web import server as srv

# The per-game endpoints that expose player names / availability / roster
# internals / staff assignments — must never be reachable unauthenticated (#73).
PLAYER_DATA_SUBS = ["board", "lineups", "roster", "roster-status",
                    "substitutes", "officials"]


class _HttpBase(unittest.TestCase):
    def _get(self, path, cookie=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     method="GET")
        if cookie is not None:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _login(self, username, password):
        """Log in and return the manual ``hs_sid=…`` cookie header. In
        production the cookie is Secure (#76) and won't auto-replay over the
        test's plain-HTTP loopback, so we send it explicitly on later GETs."""
        url = f"http://127.0.0.1:{self.port}/api/auth/login"
        data = json.dumps({"username": username, "password": password}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req) as r:
            set_cookie = r.headers.get("Set-Cookie")
        return cookie_from_set_cookie(set_cookie, srv.SESSION_COOKIE)


class ProductionPublicPrivacyTest(_HttpBase):
    """Anonymous production requests cannot reach any player-level data (#73)."""

    @classmethod
    def setUpClass(cls):
        os.environ["APP_MODE"] = "production"
        os.environ["BOOTSTRAP_ADMIN_USER"] = "privadmin"
        os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "pw"
        srv.STATE.reset()
        # Seed a real game with a real roster so there IS player data to leak,
        # created through the bootstrapped admin's authority.
        api = srv.STATE.api
        from hockey_scheduler.full_demo import build_full_demo_store
        # Production STATE starts empty; build a demo dataset into its store so
        # the endpoints have something to return, then test access control.
        build_full_demo_store(api.store)
        cls.game_id = next(iter(api.store.games))
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        for k in ("APP_MODE", "BOOTSTRAP_ADMIN_USER", "BOOTSTRAP_ADMIN_PASSWORD"):
            os.environ.pop(k, None)
        srv.STATE.reset()

    def test_anonymous_cannot_read_any_player_data_endpoint(self):
        for sub in PLAYER_DATA_SUBS:
            status, body = self._get(f"/api/games/{self.game_id}/{sub}")
            self.assertEqual(status, 401, f"{sub} should require auth")
            self.assertEqual(body["error"]["code"], "unauthorized")

    def test_public_fixture_record_and_standings_stay_open(self):
        # The bare game record (schedule/teams/score) is public...
        status, body = self._get(f"/api/games/{self.game_id}")
        self.assertEqual(status, 200)
        self.assertNotIn("error", body)
        # ...and neither the fixture record nor the public overview leak names.
        players = [p.name for p in srv.STATE.api.store.players.values()]
        _, ov = self._get("/api/demo/overview")
        blob = json.dumps(body) + json.dumps(ov)
        for name in players:
            self.assertNotIn(name, blob)

    def test_signed_in_user_can_read_player_data(self):
        cookie = self._login("privadmin", "pw")
        self.assertIsNotNone(cookie)
        for sub in PLAYER_DATA_SUBS:
            status, _ = self._get(f"/api/games/{self.game_id}/{sub}",
                                  cookie=cookie)
            self.assertEqual(status, 200, sub)

    def test_invalid_cookie_is_rejected_on_player_data(self):
        status, body = self._get(f"/api/games/{self.game_id}/lineups",
                                 cookie=f"{srv.SESSION_COOKIE}=bogus")
        self.assertEqual(status, 401)

    # -- role/scope authorization (#73 review) -----------------------------
    def _get_as(self, account_id, role, scope, path):
        # Create a real account with this role/scope, then issue a store-backed
        # session for it (#74 — role/scope are resolved from the account).
        store = srv.STATE.api.store
        acct = srv.STATE.api.accounts.create_account(
            username=account_id, password="pw", role=role, scope=scope)
        token = srv.SESSIONS.login(store, acct.id)
        return self._get(path, cookie=f"{srv.SESSION_COOKIE}={token}")

    def _game(self):
        return srv.STATE.api.store.get_game(self.game_id)

    def test_signed_in_viewer_is_denied_private_data(self):
        from hockey_scheduler.domain import Role
        status, body = self._get_as("u_v", Role.VIEWER, {},
                                    f"/api/games/{self.game_id}/lineups")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

    def test_coach_scope_controls_roster_access(self):
        from hockey_scheduler.domain import Role
        g = self._game()
        # In-scope coach (home team) → allowed.
        ok, _ = self._get_as("u_c1", Role.COACH, {"team_id": g.home_team_id},
                             f"/api/games/{self.game_id}/roster")
        self.assertEqual(ok, 200)
        # Unrelated coach → 403.
        no, _ = self._get_as("u_c2", Role.COACH, {"team_id": "team_elsewhere"},
                             f"/api/games/{self.game_id}/roster")
        self.assertEqual(no, 403)

    def test_player_scope_controls_roster_status_access(self):
        from hockey_scheduler.domain import Role
        g = self._game()
        ok, _ = self._get_as("u_p1", Role.PLAYER, {"team_id": g.away_team_id},
                             f"/api/games/{self.game_id}/roster-status")
        self.assertEqual(ok, 200)
        no, _ = self._get_as("u_p2", Role.PLAYER, {"team_id": "team_elsewhere"},
                             f"/api/games/{self.game_id}/roster-status")
        self.assertEqual(no, 403)

    def test_official_assignment_controls_officials_access(self):
        from hockey_scheduler.domain import Role
        assigned = srv.STATE.api.store.assignments_for_game(self.game_id)
        self.assertTrue(assigned, "seed should assign an official")
        oid = assigned[0].official_id
        ok, _ = self._get_as("u_o1", Role.OFFICIAL, {"official_id": oid},
                             f"/api/games/{self.game_id}/officials")
        self.assertEqual(ok, 200)
        no, _ = self._get_as("u_o2", Role.OFFICIAL, {"official_id": "official_other"},
                             f"/api/games/{self.game_id}/officials")
        self.assertEqual(no, 403)


class DemoPublicPrivacyTest(_HttpBase):
    """Demo mode keeps the headerless-operator convenience for player data."""

    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.game_id = srv.STATE.game_id
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def test_headerless_demo_still_reads_player_data(self):
        # Demo's headerless fallback is the operator, so lineups load (no
        # regression for the demo SPA / mobile preview).
        for sub in PLAYER_DATA_SUBS:
            status, _ = self._get(f"/api/games/{self.game_id}/{sub}")
            self.assertEqual(status, 200, sub)

    def test_invalid_cookie_still_rejected_in_demo(self):
        status, _ = self._get(f"/api/games/{self.game_id}/roster",
                              cookie=f"{srv.SESSION_COOKIE}=bogus")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
