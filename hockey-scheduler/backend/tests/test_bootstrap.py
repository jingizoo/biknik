import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.bootstrap import bootstrap_admin, bootstrap_admin_from_env
from hockey_scheduler.store import SqlStore
from hockey_scheduler.web import server as srv


class BootstrapAdminTest(unittest.TestCase):
    def setUp(self):
        self.api = ApiService()  # empty store, no accounts

    def test_creates_first_admin_when_store_empty(self):
        created, msg = bootstrap_admin(self.api, "root", "s3cret")
        self.assertTrue(created, msg)
        acct = self.api.verify_login("root", "s3cret")
        self.assertIsNotNone(acct)
        self.assertEqual(acct["role"], "league_admin")

    def test_skips_when_an_account_already_exists(self):
        self.api.create_user_account("someone", "pw", "viewer")
        created, msg = bootstrap_admin(self.api, "root", "s3cret")
        self.assertFalse(created)
        self.assertIn("already exist", msg)
        # The intended admin was NOT created.
        self.assertIsNone(self.api.verify_login("root", "s3cret"))

    def test_requires_username_and_password(self):
        self.assertFalse(bootstrap_admin(self.api, "", "pw")[0])
        self.assertFalse(bootstrap_admin(self.api, "root", "")[0])
        self.assertEqual(self.api.list_user_accounts()["user_accounts"], [])

    def test_is_idempotent_second_run_is_a_safe_noop(self):
        self.assertTrue(bootstrap_admin(self.api, "root", "pw")[0])
        created, _ = bootstrap_admin(self.api, "root", "pw")
        self.assertFalse(created)  # already bootstrapped → no duplicate
        self.assertEqual(len(self.api.list_user_accounts()["user_accounts"]), 1)

    # -- from-env -----------------------------------------------------------
    def test_from_env_creates_when_both_vars_set(self):
        created = bootstrap_admin_from_env(
            self.api, {"BOOTSTRAP_ADMIN_USER": "ops", "BOOTSTRAP_ADMIN_PASSWORD": "pw"})
        self.assertTrue(created)
        self.assertIsNotNone(self.api.verify_login("ops", "pw"))

    def test_from_env_noop_when_vars_absent(self):
        self.assertFalse(bootstrap_admin_from_env(self.api, {}))
        self.assertFalse(bootstrap_admin_from_env(
            self.api, {"BOOTSTRAP_ADMIN_USER": "ops"}))  # password missing
        self.assertEqual(self.api.list_user_accounts()["user_accounts"], [])


class ProductionBootstrapHttpTest(unittest.TestCase):
    """A production deploy with bootstrap env vars is reachable end-to-end (#71)."""

    @classmethod
    def setUpClass(cls):
        os.environ["APP_MODE"] = "production"
        os.environ["BOOTSTRAP_ADMIN_USER"] = "prodroot"
        os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "prod-pass"
        srv.STATE.reset()  # boots production: no demo personas, bootstrap admin
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

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

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

    def test_account_picker_is_empty_but_bootstrap_admin_can_log_in(self):
        c = self._client()
        # Production still hides the picker (no username/role enumeration, #68).
        _, accts = self._req(c, "GET", "/api/auth/accounts")
        self.assertEqual(accts["accounts"], [])
        # But the bootstrapped admin can sign in via the manual form path.
        status, body = self._req(c, "POST", "/api/auth/login",
                                 {"username": "prodroot", "password": "prod-pass"})
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["role"], "league_admin")
        status, me = self._req(c, "GET", "/api/auth/me")
        self.assertEqual(me["user"]["username"], "prodroot")

    def test_wrong_bootstrap_password_is_rejected(self):
        c = self._client()
        status, _ = self._req(c, "POST", "/api/auth/login",
                              {"username": "prodroot", "password": "nope"})
        self.assertEqual(status, 401)


class ProductionResetPreservesSqlStoreTest(unittest.TestCase):
    """Production startup/reset must NOT wipe a persistent SQL store (#71 review)."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.environ["APP_MODE"] = "production"
        os.environ["DATABASE_URL"] = self.db_path

    def tearDown(self):
        for k in ("APP_MODE", "DATABASE_URL",
                  "BOOTSTRAP_ADMIN_USER", "BOOTSTRAP_ADMIN_PASSWORD"):
            os.environ.pop(k, None)
        try:
            os.remove(self.db_path)
        except OSError:
            pass
        srv.STATE.reset()  # restore the shared singleton to demo mode

    def _accounts(self):
        # A fresh connection to the same file to inspect persisted state.
        api = ApiService(SqlStore(self.db_path))
        return {a["username"] for a in api.list_user_accounts()["user_accounts"]}

    def test_reset_does_not_drop_existing_accounts_and_bootstrap_skips(self):
        # Boot once to create the first admin from env.
        os.environ["BOOTSTRAP_ADMIN_USER"] = "firstadmin"
        os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "pw1"
        srv.DemoState()  # __init__ → reset(): production path, bootstraps admin
        self.assertIn("firstadmin", self._accounts())

        # Add a second, non-admin account directly to the persistent store.
        ApiService(SqlStore(self.db_path)).create_user_account(
            "returning_coach", "pw2", "coach")

        # Reboot with a DIFFERENT bootstrap user set.
        os.environ["BOOTSTRAP_ADMIN_USER"] = "shouldnotexist"
        os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "pw3"
        srv.DemoState()  # reboot

        names = self._accounts()
        # Nothing was wiped: both prior accounts survive the reboot.
        self.assertIn("firstadmin", names)
        self.assertIn("returning_coach", names)
        # And bootstrap did NOT clobber/add a new admin (accounts already exist).
        self.assertNotIn("shouldnotexist", names)

    def test_production_reset_does_not_seed_demo_data(self):
        srv.DemoState()
        api = ApiService(SqlStore(self.db_path))
        ov = api.get_demo_overview()
        self.assertEqual(ov["teams"], [])
        self.assertEqual(ov["schedule"], [])


if __name__ == "__main__":
    unittest.main()
