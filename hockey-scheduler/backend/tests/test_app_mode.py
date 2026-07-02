import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

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
        status, body = self._req(c, "POST", "/api/auth/login",
                                 {"username": "prod_admin",
                                  "password": "a-real-password"})
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["role"], "league_admin")
        # The session — not any header — now drives authorization normally.
        status, _ = self._req(c, "GET", "/api/notifications")
        self.assertEqual(status, 200)
        status, _ = self._req(c, "POST", "/api/setup/league",
                              {"name": "Real League"})
        self.assertNotEqual(status, 401)
        self.assertNotEqual(status, 403)

    def test_reset_still_forbidden_for_a_real_admin_session(self):
        # Disabled outright — even a legitimate, currently-signed-in admin
        # cannot trigger it in production.
        srv.STATE.api.accounts.create_account(
            "prod_admin2", "pw", Role.LEAGUE_ADMIN)
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                 {"username": "prod_admin2", "password": "pw"})
        status, body = self._req(c, "POST", "/api/reset", {})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")


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
                                 "player", "official", "viewer"})

    def test_headerless_request_still_defaults_to_admin(self):
        status, body = self._get("/api/notifications")
        self.assertEqual(status, 200)

    def test_x_demo_role_header_still_works(self):
        status, _ = self._get("/api/notifications",
                              {"X-Demo-Role": "viewer"})
        self.assertEqual(status, 200)

    def test_explicit_demo_value_behaves_the_same_as_unset(self):
        os.environ["APP_MODE"] = "demo"
        try:
            status, body = self._get("/api/notifications")
            self.assertEqual(status, 200)
        finally:
            os.environ.pop("APP_MODE", None)


if __name__ == "__main__":
    unittest.main()
