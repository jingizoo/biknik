import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.web import server as srv


class AuthSessionTest(unittest.TestCase):
    """Server-issued identity/session drives the acting role (#50)."""

    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.game_id = srv.STATE.game_id

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _client(self):
        # Each opener has its own cookie jar = an isolated browser session.
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers or {})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    # -- login / me / logout ----------------------------------------------
    def test_login_sets_session_and_me(self):
        c = self._client()
        status, body = self._req(c, "POST", "/api/auth/login",
                                 {"username": "coach", "password": "demo"})
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["role"], "coach")
        # /me now reflects the session on the same client (cookie carried).
        status, me = self._req(c, "GET", "/api/auth/me")
        self.assertEqual(me["user"]["role"], "coach")

    def test_login_bad_password_rejected(self):
        c = self._client()
        status, body = self._req(c, "POST", "/api/auth/login",
                                 {"username": "coach", "password": "wrong"})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_login_unknown_user_rejected(self):
        c = self._client()
        status, _ = self._req(c, "POST", "/api/auth/login",
                              {"username": "ghost", "password": "demo"})
        self.assertEqual(status, 401)

    def test_logout_clears_session(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        self._req(c, "POST", "/api/auth/logout")
        status, me = self._req(c, "GET", "/api/auth/me")
        self.assertIsNone(me["user"])

    # -- session drives authorization -------------------------------------
    def test_session_role_enforced(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "coach", "password": "demo"})
        # A coach session may manage its own team's roster but not move a game
        # (scheduling) — and not lock/cancel the whole game (that is scoped out
        # for a bound coach in #51).
        status, _ = self._req(c, "POST", f"/api/games/{self.game_id}/move",
                              {"ice_slot_id": "x"})
        self.assertEqual(status, 403)
        home = srv.STATE.ids["home_team_id"]
        status, _ = self._req(c, "POST",
                              f"/api/games/{self.game_id}/build-roster",
                              {"team_id": home})
        self.assertNotEqual(status, 403)

    def test_session_beats_header(self):
        # A viewer session is authoritative even if a spoofed admin header is sent.
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "viewer", "password": "demo"})
        status, _ = self._req(c, "POST", "/api/setup/league", {"name": "X"},
                              headers={"X-Demo-Role": "league_admin"})
        self.assertEqual(status, 403)

    def test_invalid_session_cookie_rejected(self):
        c = self._client()
        status, body = self._req(
            c, "POST", f"/api/games/{self.game_id}/roster/lock", {},
            headers={"Cookie": f"{srv.SESSION_COOKIE}=bogustoken"})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_me_invalid_session_cookie_returns_401(self):
        # A present-but-invalid cookie must not read as signed out (which would
        # let the client silently auto-login as admin); it must be a 401.
        c = self._client()
        status, body = self._req(
            c, "GET", "/api/auth/me",
            headers={"Cookie": f"{srv.SESSION_COOKIE}=bogustoken"})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_me_no_cookie_is_signed_out(self):
        c = self._client()
        status, body = self._req(c, "GET", "/api/auth/me")
        self.assertEqual(status, 200)
        self.assertIsNone(body["user"])

    def test_accounts_endpoint_lists_demo_users(self):
        c = self._client()
        _, body = self._req(c, "GET", "/api/auth/accounts")
        roles = {a["role"] for a in body["accounts"]}
        self.assertEqual(roles, {"league_admin", "arena_manager", "coach",
                                 "player", "official", "viewer", "guardian"})

    # -- real accounts (#67): operator-created, hashed, session tied to id --
    def test_operator_created_account_can_log_in(self):
        admin = self._client()
        self._req(admin, "POST", "/api/auth/login",
                 {"username": "admin", "password": "demo"})
        from hockey_scheduler.domain import Team
        srv.STATE.api.store.add_team(Team(id="team_z", name="Team Z"))  # #266
        status, created = self._req(
            admin, "POST", "/api/accounts",
            {"username": "new_coach", "password": "real-pw", "role": "coach",
             "scope": {"team_id": "team_z"}})
        self.assertEqual(status, 200)

        newbie = self._client()
        status, body = self._req(newbie, "POST", "/api/auth/login",
                                 {"username": "new_coach", "password": "real-pw"})
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["role"], "coach")
        self.assertEqual(body["user"]["username"], "new_coach")
        _, me = self._req(newbie, "GET", "/api/auth/me")
        self.assertEqual(me["user"]["scope"]["team_id"], "team_z")

    def test_deactivating_an_account_ends_its_live_session(self):
        admin = self._client()
        self._req(admin, "POST", "/api/auth/login",
                 {"username": "admin", "password": "demo"})
        _, created = self._req(
            admin, "POST", "/api/accounts",
            {"username": "to_revoke", "password": "pw", "role": "viewer"})

        victim = self._client()
        self._req(victim, "POST", "/api/auth/login",
                 {"username": "to_revoke", "password": "pw"})
        status, me = self._req(victim, "GET", "/api/auth/me")
        self.assertEqual(me["user"]["role"], "viewer")  # session is live

        self._req(admin, "POST", f"/api/accounts/{created['id']}/active",
                 {"active": False})

        # The already-issued session must be dead, not just future logins.
        status, me2 = self._req(victim, "GET", "/api/auth/me")
        self.assertEqual(status, 401)

        status, relogin = self._req(victim, "POST", "/api/auth/login",
                                    {"username": "to_revoke", "password": "pw"})
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
