"""Production cookie hardening (#76).

The session cookie always carries HttpOnly + SameSite=Lax. The Secure flag is
added when the deployment is production or the request arrived over TLS
(directly or via a TLS-terminating proxy that sets X-Forwarded-Proto: https),
and is omitted for the plain-HTTP local/demo posture so the cookie still works
over http://localhost. The clearing (logout) cookie mirrors the same
attributes. These tests assert the Set-Cookie headers for each posture.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.web import server as srv


class _CookieBase(unittest.TestCase):
    APP_MODE = "demo"
    ENV = {}

    @classmethod
    def setUpClass(cls):
        cls._saved = {k: os.environ.get(k)
                      for k in ("APP_MODE", "BOOTSTRAP_ADMIN_USER",
                                "BOOTSTRAP_ADMIN_PASSWORD")}
        os.environ["APP_MODE"] = cls.APP_MODE
        for k, v in cls.ENV.items():
            os.environ[k] = v
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # Restore a clean demo store for any tests that follow.
        srv.STATE.reset()

    def _post(self, path, body=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else b"{}"
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers=headers or {})
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get_all("Set-Cookie") or []

    def _session_set_cookie(self, cookies):
        for c in cookies:
            if c.startswith(f"{srv.SESSION_COOKIE}="):
                return c
        return None


class DemoCookieTest(_CookieBase):
    APP_MODE = "demo"

    def test_plain_http_login_has_no_secure_flag(self):
        status, cookies = self._post("/api/auth/login",
                                     {"username": "admin", "password": "demo"})
        self.assertEqual(status, 200)
        c = self._session_set_cookie(cookies)
        self.assertIsNotNone(c)
        self.assertIn("HttpOnly", c)
        self.assertIn("SameSite=Lax", c)
        self.assertNotIn("Secure", c)

    def test_forwarded_https_login_adds_secure_flag(self):
        # A TLS-terminating proxy forwards the original scheme; trust it.
        status, cookies = self._post(
            "/api/auth/login", {"username": "admin", "password": "demo"},
            headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(status, 200)
        c = self._session_set_cookie(cookies)
        self.assertIn("Secure", c)
        self.assertIn("HttpOnly", c)
        self.assertIn("SameSite=Lax", c)

    def test_rfc7239_forwarded_proto_https_adds_secure_flag(self):
        status, cookies = self._post(
            "/api/auth/login", {"username": "admin", "password": "demo"},
            headers={"Forwarded": "for=203.0.113.5;proto=https"})
        self.assertEqual(status, 200)
        c = self._session_set_cookie(cookies)
        self.assertIn("Secure", c)

    def test_forwarded_http_login_stays_insecure(self):
        status, cookies = self._post(
            "/api/auth/login", {"username": "admin", "password": "demo"},
            headers={"X-Forwarded-Proto": "http"})
        c = self._session_set_cookie(cookies)
        self.assertNotIn("Secure", c)

    def test_logout_cookie_mirrors_insecure_attributes(self):
        status, cookies = self._post("/api/auth/logout")
        c = self._session_set_cookie(cookies)
        self.assertIsNotNone(c)
        self.assertIn("Max-Age=0", c)
        self.assertIn("HttpOnly", c)
        self.assertIn("SameSite=Lax", c)
        self.assertNotIn("Secure", c)

    def test_logout_over_forwarded_https_is_secure(self):
        status, cookies = self._post("/api/auth/logout",
                                     headers={"X-Forwarded-Proto": "https"})
        c = self._session_set_cookie(cookies)
        self.assertIn("Secure", c)
        self.assertIn("Max-Age=0", c)


class ProductionCookieTest(_CookieBase):
    APP_MODE = "production"
    ENV = {"BOOTSTRAP_ADMIN_USER": "boss", "BOOTSTRAP_ADMIN_PASSWORD": "secret"}

    def test_login_always_secure_in_production(self):
        # Even over plain HTTP (the app sits behind a TLS proxy in prod).
        status, cookies = self._post("/api/auth/login",
                                     {"username": "boss", "password": "secret"})
        self.assertEqual(status, 200)
        c = self._session_set_cookie(cookies)
        self.assertIn("Secure", c)
        self.assertIn("HttpOnly", c)
        self.assertIn("SameSite=Lax", c)

    def test_logout_cookie_is_secure_in_production(self):
        status, cookies = self._post("/api/auth/logout")
        c = self._session_set_cookie(cookies)
        self.assertIn("Secure", c)
        self.assertIn("Max-Age=0", c)
        self.assertIn("HttpOnly", c)
        self.assertIn("SameSite=Lax", c)


if __name__ == "__main__":
    unittest.main()
