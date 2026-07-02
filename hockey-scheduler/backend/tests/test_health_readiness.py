"""Production readiness + health endpoints (#90).

/api/health is a public, non-sensitive liveness + dependency snapshot.
/api/readiness reports deployment checks: a reachable DB, current migrations,
(in production) at least one active admin, and cookie hardening. Neither leaks
secrets.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.store import SqlStore
from hockey_scheduler.web import server as srv

SECRET_VALUES = ["s3cret-pw", "postgres://", "database_url"]


class HealthReadinessServiceTest(unittest.TestCase):
    def test_health_is_ok_and_reports_dependencies(self):
        api = ApiService(SqlStore(":memory:"))
        h = api.get_health()
        self.assertEqual(h["status"], "ok")
        self.assertTrue(h["database_reachable"])
        self.assertTrue(h["migrations"]["current"])
        self.assertIn("email_mode", h["delivery"])

    def test_readiness_fails_without_active_admin_in_production(self):
        api = ApiService(SqlStore(":memory:"))
        r = api.get_readiness("production", cookie_hardened=True)
        self.assertFalse(r["ready"])
        admin = next(c for c in r["checks"] if c["name"] == "active_admin")
        self.assertFalse(admin["ok"])

    def test_readiness_passes_with_active_admin_in_production(self):
        api = ApiService(SqlStore(":memory:"))
        api.accounts.create_account("boss", "s3cret-pw", "league_admin")
        r = api.get_readiness("production", cookie_hardened=True)
        self.assertTrue(r["ready"])

    def test_readiness_is_lenient_outside_production(self):
        api = ApiService(SqlStore(":memory:"))  # no admin, demo mode
        r = api.get_readiness("demo", cookie_hardened=False)
        self.assertTrue(r["ready"])

    def test_production_cookie_hardening_check(self):
        api = ApiService(SqlStore(":memory:"))
        api.accounts.create_account("boss", "s3cret-pw", "league_admin")
        r = api.get_readiness("production", cookie_hardened=False)
        cookie = next(c for c in r["checks"] if c["name"] == "cookie_hardening")
        self.assertFalse(cookie["ok"])
        self.assertFalse(r["ready"])

    def test_no_secret_values_in_health_or_readiness(self):
        api = ApiService(SqlStore(":memory:"))
        api.accounts.create_account("boss", "s3cret-pw", "league_admin")
        blob = json.dumps(api.get_health()) + json.dumps(
            api.get_readiness("production", cookie_hardened=True))
        low = blob.lower()
        for secret in SECRET_VALUES:
            self.assertNotIn(secret, low)
        # No account credentials leak either.
        self.assertNotIn("boss", low)
        self.assertNotIn("password", low)


class HealthReadinessHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    @staticmethod
    def _cookie_check(body):
        return next(c for c in body["checks"] if c["name"] == "cookie_hardening")

    def test_health_is_public(self):
        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_readiness_is_public(self):
        status, body = self._get("/api/readiness")
        self.assertEqual(status, 200)
        self.assertIn("checks", body)

    def test_readiness_cookie_check_reflects_real_secure_decision(self):
        # The cookie-hardening check must consult the actual Secure-cookie
        # decision (_cookie_is_secure, #76), not re-derive mode == production
        # (#90 review). Over plain HTTP in demo it reports "not enforced"; a
        # TLS-terminating proxy forwarding https flips it to "Secure cookies".
        # Under the old tautology both would read "not enforced" regardless of
        # the request, so this discriminates the fix from the bug.
        _, plain = self._get("/api/readiness")
        self.assertEqual(self._cookie_check(plain)["detail"], "not enforced")
        _, fwd = self._get("/api/readiness",
                           headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(self._cookie_check(fwd)["detail"], "Secure cookies")
        self.assertTrue(self._cookie_check(fwd)["ok"])  # lenient outside prod


class ProductionReadinessHttpTest(unittest.TestCase):
    """Readiness over HTTP in production mode (#90). With a bootstrapped admin
    every check is green — and the cookie-hardening check is driven by the real
    `_cookie_is_secure`, which is unconditionally True in production (#76), so it
    passes because cookies are genuinely hardened, not because the check compares
    app_mode to itself."""

    @classmethod
    def setUpClass(cls):
        cls._saved = {k: os.environ.get(k)
                      for k in ("APP_MODE", "BOOTSTRAP_ADMIN_USER",
                                "BOOTSTRAP_ADMIN_PASSWORD")}
        os.environ["APP_MODE"] = "production"
        os.environ["BOOTSTRAP_ADMIN_USER"] = "boss"
        os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "secret"
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
        srv.STATE.reset()  # restore a clean demo store for following tests

    def _get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     headers=headers or {})
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")

    def test_production_readiness_is_green_with_hardened_cookies(self):
        # Even over plain HTTP the production cookie decision is Secure, so the
        # cookie-hardening check passes on its own merit — not a tautology.
        status, body = self._get("/api/readiness")
        self.assertEqual(status, 200)
        self.assertEqual(body["app_mode"], "production")
        cookie = next(c for c in body["checks"]
                      if c["name"] == "cookie_hardening")
        self.assertTrue(cookie["ok"])
        self.assertEqual(cookie["detail"], "Secure cookies")
        self.assertTrue(body["ready"])  # admin bootstrapped, all checks green


if __name__ == "__main__":
    unittest.main()
