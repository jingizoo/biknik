"""Session inventory + revoke API, League-Admin only (#78).

A league admin can list any account's login sessions and revoke a single one.
The response exposes only lifecycle metadata — never the raw token (which is
not stored) or the token_hash. Non-user-managing roles are refused.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.web import server as srv


class SessionAdminTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()  # demo mode: six personas seeded, cookies not Secure
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _login(self, opener, username, password="demo"):
        return self._req(opener, "POST", "/api/auth/login",
                         {"username": username, "password": password})

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

    def _coach_account_id(self):
        return srv.STATE.api.store.get_user_account_by_username("coach").id

    # -- authorization ------------------------------------------------------
    def test_non_admin_roles_cannot_list_sessions(self):
        cid = self._coach_account_id()
        for who in ("coach", "player", "viewer"):
            c = self._client()
            self._login(c, who)
            status, _ = self._req(c, "GET", f"/api/accounts/{cid}/sessions")
            self.assertEqual(status, 403, who)

    def test_non_admin_roles_cannot_revoke_sessions(self):
        cid = self._coach_account_id()
        c = self._client()
        self._login(c, "coach")
        status, _ = self._req(
            c, "POST", f"/api/accounts/{cid}/sessions/session_x/revoke")
        self.assertEqual(status, 403)

    def test_invalid_session_cookie_is_rejected(self):
        # In demo a headerless request falls back to the operator, so the real
        # unauthenticated case is a present-but-invalid cookie → 401.
        cid = self._coach_account_id()
        bad = urllib.request.build_opener()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/accounts/{cid}/sessions")
        req.add_header("Cookie", f"{srv.SESSION_COOKIE}=bogus")
        try:
            with bad.open(req) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertEqual(code, 401)

    # -- behavior -----------------------------------------------------------
    def test_admin_lists_and_revokes_and_no_token_material_leaks(self):
        cid = self._coach_account_id()
        # Give the coach a couple of live sessions.
        t1 = srv.SESSIONS.login(srv.STATE.api.store, cid, user_agent="Firefox")
        srv.SESSIONS.login(srv.STATE.api.store, cid, user_agent="Safari")

        admin = self._client()
        self._login(admin, "admin")
        status, body = self._req(admin, "GET", f"/api/accounts/{cid}/sessions")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(body["sessions"]), 2)
        blob = json.dumps(body)
        self.assertNotIn("token_hash", blob)
        self.assertNotIn(t1, blob)  # raw token never appears
        for s in body["sessions"]:
            self.assertNotIn("token_hash", s)

        # Revoke the Firefox session (t1); it must stop resolving afterward.
        ff = next(s for s in body["sessions"] if s["user_agent"] == "Firefox")
        self.assertEqual(ff["status"], "active")
        self.assertIsNotNone(srv.SESSIONS.resolve(srv.STATE.api.store, t1))
        status, res = self._req(
            admin, "POST", f"/api/accounts/{cid}/sessions/{ff['id']}/revoke")
        self.assertEqual(status, 200)
        self.assertEqual(res["status"], "revoked")
        self.assertIsNone(srv.SESSIONS.resolve(srv.STATE.api.store, t1))

    def test_revoke_unknown_session_is_not_found(self):
        cid = self._coach_account_id()
        admin = self._client()
        self._login(admin, "admin")
        status, res = self._req(
            admin, "POST", f"/api/accounts/{cid}/sessions/nope/revoke")
        self.assertEqual(res["error"]["code"], "not_found")

    def test_list_unknown_account_is_not_found(self):
        admin = self._client()
        self._login(admin, "admin")
        status, res = self._req(admin, "GET", "/api/accounts/nope/sessions")
        self.assertEqual(res["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
