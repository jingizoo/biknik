import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND, cookie_from_set_cookie  # noqa: F401  (sets up sys.path)

from hockey_scheduler.domain import Role
from hockey_scheduler.web import server as srv


class ProductionModeTest(unittest.TestCase):
    """APP_MODE=production removes the X-Demo-Role fallback entirely (#68)."""

    @classmethod
    def setUpClass(cls):
        # Set the mode BEFORE reset() so the demo account seed is skipped too.
        os.environ["APP_MODE"] = "production"
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        # Leave global state clean for every other test module in this run —
        # STATE and its account/session singletons are shared across files.
        os.environ.pop("APP_MODE", None)
        srv.STATE.reset()

    def setUp(self):
        # STATE is a module-level singleton shared by every test method in
        # this class; reset it per-test so one method creating an account
        # (e.g. a real admin) can't leak into another (e.g. the seed-empty
        # check), regardless of alphabetical method ordering.
        os.environ["APP_MODE"] = "production"
        srv.STATE.reset()

    def _client(self):
        # A plain opener with NO cookie jar: in production the session cookie is
        # Secure (#76), which a real client won't replay over plain-HTTP
        # loopback, so authenticated tests propagate the cookie manually.
        return urllib.request.build_opener()

    def _req(self, opener, method, path, body=None, headers=None, cookie=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        hdrs = dict(headers or {})
        if cookie:
            hdrs["Cookie"] = cookie
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _login(self, opener, username, password):
        """Log in and return (status, body, cookie) — cookie is the manual
        ``hs_sid=…`` header to send on subsequent authenticated requests."""
        url = f"http://127.0.0.1:{self.port}/api/auth/login"
        data = json.dumps({"username": username, "password": password}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                body = json.loads(r.read() or b"{}")
                cookie = cookie_from_set_cookie(
                    r.headers.get("Set-Cookie"), srv.SESSION_COOKIE)
                return r.status, body, cookie
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), None

    # -- no seeded accounts, no fallback -----------------------------------
    def test_demo_accounts_are_not_seeded(self):
        c = self._client()
        _, body = self._req(c, "GET", "/api/auth/accounts")
        self.assertEqual(body["accounts"], [])

    def test_auth_accounts_does_not_list_real_accounts_in_production(self):
        # Even after an account exists, the unauthenticated picker must not
        # enumerate real usernames/roles in production (#68 review).
        srv.STATE.api.accounts.create_account(
            "prod_admin_visible", "pw", Role.LEAGUE_ADMIN)
        c = self._client()
        status, body = self._req(c, "GET", "/api/auth/accounts")
        self.assertEqual(status, 200)
        self.assertEqual(body["accounts"], [])

    def test_headerless_request_is_unauthorized_not_admin(self):
        c = self._client()
        status, body = self._req(c, "GET", "/api/notifications")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_x_demo_role_header_is_ignored(self):
        # Even a syntactically valid role must not be honored without a
        # session — production never reads this header at all.
        c = self._client()
        status, body = self._req(
            c, "POST", "/api/setup/league", {"name": "Sneaky League"},
            headers={"X-Demo-Role": "league_admin"})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")
        # And nothing was created.
        status2, _ = self._req(c, "GET", "/api/notifications", None,
                               {"X-Demo-Role": "league_admin"})
        self.assertEqual(status2, 401)

    def test_unauthenticated_reset_attempt_is_401_before_reaching_the_route(self):
        # The X-Demo-Role header is ignored, so this request has no session at
        # all — the auth gate rejects it before the reset-specific 403 check
        # ever runs. (An authenticated admin's 403 is covered separately.)
        c = self._client()
        status, body = self._req(c, "POST", "/api/reset", {},
                                 {"X-Demo-Role": "league_admin"})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    # -- a real, operator-bootstrapped account still works ------------------
    def test_a_real_account_can_still_log_in_and_act(self):
        # Simulate an out-of-band production bootstrap (#68 does not solve
        # first-admin provisioning) by creating an account directly.
        srv.STATE.api.accounts.create_account(
            "prod_admin", "a-real-password", Role.LEAGUE_ADMIN)
        c = self._client()
        status, body, cookie = self._login(c, "prod_admin", "a-real-password")
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["role"], "league_admin")
        self.assertIsNotNone(cookie)
        # The session — not any header — now drives authorization normally.
        status, _ = self._req(c, "GET", "/api/notifications", cookie=cookie)
        self.assertEqual(status, 200)
        status, _ = self._req(c, "POST", "/api/setup/league",
                              {"name": "Real League"}, cookie=cookie)
        self.assertNotEqual(status, 401)
        self.assertNotEqual(status, 403)

    def test_reset_still_forbidden_for_a_real_admin_session(self):
        # Disabled outright — even a legitimate, currently-signed-in admin
        # cannot trigger it in production.
        srv.STATE.api.accounts.create_account(
            "prod_admin2", "pw", Role.LEAGUE_ADMIN)
        c = self._client()
        _, _, cookie = self._login(c, "prod_admin2", "pw")
        status, body = self._req(c, "POST", "/api/reset", {"confirm": "RESET"},
                                 cookie=cookie)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "demo_reset_not_available")


class DemoModeIsTheDefaultTest(unittest.TestCase):
    """Unset / non-"production" APP_MODE keeps today's demo behavior (#68)."""

    @classmethod
    def setUpClass(cls):
        os.environ.pop("APP_MODE", None)  # unset → defaults to demo
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        srv.STATE.reset()

    def _get(self, path, headers=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_demo_accounts_are_seeded(self):
        status, body = self._get("/api/auth/accounts")
        roles = {a["role"] for a in body["accounts"]}
        self.assertEqual(roles, {"league_admin", "arena_manager", "coach",
                                 "player", "official", "viewer", "guardian"})

    def test_headerless_request_is_signed_out(self):
        # The headerless League Admin fallback is no longer the default: a
        # cookieless request with no X-Demo-Role is signed out (401), so logout
        # is meaningful and cookieless callers are not silently admin.
        status, body = self._get("/api/notifications")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_headerless_admin_restored_by_opt_in_env(self):
        os.environ["DEMO_HEADERLESS_ADMIN"] = "1"
        try:
            status, _ = self._get("/api/notifications")
            self.assertEqual(status, 200)  # escape hatch restores old behavior
        finally:
            os.environ.pop("DEMO_HEADERLESS_ADMIN", None)

    def test_x_demo_role_header_still_works(self):
        status, _ = self._get("/api/notifications",
                              {"X-Demo-Role": "viewer"})
        self.assertEqual(status, 200)

    def test_explicit_demo_value_behaves_the_same_as_unset(self):
        # demo behaves like unset: a headerless request is signed out in both.
        os.environ["APP_MODE"] = "demo"
        try:
            status, body = self._get("/api/notifications")
            self.assertEqual(status, 401)
        finally:
            os.environ.pop("APP_MODE", None)


if __name__ == "__main__":
    unittest.main()
