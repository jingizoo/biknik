import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

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
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        url = f"http://127.0.0.1:{self.port}/api/auth/login"
        data = json.dumps({"username": username, "password": password}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with opener.open(req) as r:
            r.read()
        return opener


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
        c = self._login("privadmin", "pw")
        # Reuse the opener's cookie jar for an authenticated GET.
        for sub in PLAYER_DATA_SUBS:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/games/{self.game_id}/{sub}")
            with c.open(req) as r:
                self.assertEqual(r.status, 200, sub)

    def test_invalid_cookie_is_rejected_on_player_data(self):
        status, body = self._get(f"/api/games/{self.game_id}/lineups",
                                 cookie=f"{srv.SESSION_COOKIE}=bogus")
        self.assertEqual(status, 401)


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
