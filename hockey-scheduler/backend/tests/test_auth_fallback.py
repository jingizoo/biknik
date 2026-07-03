"""Demo-mode auth fallback hardening.

The old headerless League Admin fallback made logout meaningless (a cookieless
request silently resolved to admin) and let cookieless callers act as admin. It
is now opt-in via ``DEMO_HEADERLESS_ADMIN=1``; by default a cookieless request
with no ``X-Demo-Role`` is signed out (401). The explicit ``X-Demo-Role`` dev
fallback and real session cookies are unchanged.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND, cookie_from_set_cookie  # noqa: F401

from hockey_scheduler.web import server as srv


class AuthFallbackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("APP_MODE", None)          # default → demo
        os.environ.pop("DEMO_HEADERLESS_ADMIN", None)
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        os.environ.pop("DEMO_HEADERLESS_ADMIN", None)

    def _req(self, method, path, body=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}"), \
                    r.headers.get("Set-Cookie")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), None

    def _login_cookie(self, username="admin", password="demo"):
        _, _, set_cookie = self._req(
            "POST", "/api/auth/login", {"username": username, "password": password})
        return cookie_from_set_cookie(set_cookie, srv.SESSION_COOKIE)

    # -- default posture ---------------------------------------------------
    def test_headerless_request_is_signed_out(self):
        status, body, _ = self._req("GET", "/api/notifications")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_explicit_x_demo_role_still_works(self):
        status, _, _ = self._req("GET", "/api/notifications",
                                 headers={"X-Demo-Role": "league_admin"})
        self.assertEqual(status, 200)

    def test_unknown_x_demo_role_is_forbidden(self):
        status, body, _ = self._req("GET", "/api/notifications",
                                    headers={"X-Demo-Role": "wizard"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

    def test_invalid_cookie_is_rejected(self):
        status, _, _ = self._req(
            "GET", "/api/notifications",
            headers={"Cookie": f"{srv.SESSION_COOKIE}=bogus"})
        self.assertEqual(status, 401)

    # -- logout is meaningful (the reported bug) ---------------------------
    def test_logout_then_cookieless_request_is_signed_out(self):
        cookie = self._login_cookie()
        # Signed in: a protected read succeeds with the cookie.
        ok, _, _ = self._req("GET", "/api/notifications",
                             headers={"Cookie": cookie})
        self.assertEqual(ok, 200)
        # Log out (server ends the session).
        self._req("POST", "/api/auth/logout", {}, headers={"Cookie": cookie})
        # The now-ended session cookie is rejected...
        st_old, _, _ = self._req("GET", "/api/notifications",
                                 headers={"Cookie": cookie})
        self.assertEqual(st_old, 401)
        # ...and a cookieless request (a browser refresh after logout) is signed
        # out, not silently admin.
        st_none, _, _ = self._req("GET", "/api/notifications")
        self.assertEqual(st_none, 401)
        # /api/auth/me reports signed out for the cookieless refresh.
        st_me, me, _ = self._req("GET", "/api/auth/me")
        self.assertEqual(st_me, 200)
        self.assertIsNone(me["user"])

    # -- opt-in escape hatch -----------------------------------------------
    def test_opt_in_env_restores_headerless_admin(self):
        os.environ["DEMO_HEADERLESS_ADMIN"] = "1"
        try:
            status, _, _ = self._req("GET", "/api/notifications")
            self.assertEqual(status, 200)
        finally:
            os.environ.pop("DEMO_HEADERLESS_ADMIN", None)


if __name__ == "__main__":
    unittest.main()
