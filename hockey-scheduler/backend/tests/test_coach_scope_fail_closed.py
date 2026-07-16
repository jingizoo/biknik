"""Coach-scope fail-closed regression suite (#266).

The expert review (finding F-1) found that a Coach account created without a
valid ``scope.team_id`` was treated as *unbound* and therefore NOT
resource-scoped — so it could mutate any team's roster — and that the
account-creation path silently dropped a misplaced top-level ``team_id`` into an
empty scope. This suite pins the fail-closed behavior:

* the service refuses to create/reactivate a Coach without a real team, on
  Memory, SQLite and (when ``TEST_DATABASE_URL`` is set) PostgreSQL;
* the HTTP layer rejects unknown top-level account fields;
* an existing unscoped Coach (a pre-fix account, simulated by a direct store
  insert) is refused 403 on every roster mutation and private game read;
* the deployment readiness endpoint surfaces unscoped active coaches instead of
  silently grandfathering them.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role, Team, UserAccount
from hockey_scheduler.domain.errors import ValidationError
from hockey_scheduler.services.account_service import AccountService
from hockey_scheduler.services.passwords import hash_password
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv


def _clock():
    return datetime(2026, 7, 16, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Service layer, run against every backend (criteria 1 and 5).                 #
# --------------------------------------------------------------------------- #
class CoachScopeContract:
    """Backend-agnostic service assertions. Subclasses provide ``make_store``."""

    def setUp(self):
        self.store = self.make_store()
        self.accounts = AccountService(self.store, _clock)
        self.store.add_team(Team(id="team_home", name="Lions"))

    # -- create --------------------------------------------------------------
    def test_create_coach_without_team_scope_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            self.accounts.create_account("c1", "pw", Role.COACH, scope={})
        self.assertEqual(cm.exception.details.get("reason"), "scope_required")
        # No account and no audit row were written (the method is transactional).
        self.assertEqual(self.store.all_user_accounts(), [])
        self.assertEqual(
            [a for a in self.store.all_setup_audit()
             if a.action == "user_account_created"], [])

    def test_create_coach_with_nonexistent_team_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            self.accounts.create_account(
                "c2", "pw", Role.COACH, scope={"team_id": "team_ghost"})
        self.assertEqual(cm.exception.details.get("reason"),
                         "scope_subject_missing")
        self.assertEqual(self.store.all_user_accounts(), [])

    def test_create_coach_with_valid_team_succeeds(self):
        acct = self.accounts.create_account(
            "c3", "pw", Role.COACH, scope={"team_id": "team_home"})
        self.assertEqual(acct.role, Role.COACH)
        self.assertEqual(acct.scope, {"team_id": "team_home"})
        self.assertTrue(acct.active)

    def test_non_coach_roles_need_no_team(self):
        # A viewer/admin carries no team scope and must still create cleanly —
        # the requirement is coach-specific.
        self.assertIsNotNone(
            self.accounts.create_account("v1", "pw", Role.VIEWER))

    # -- reactivate ----------------------------------------------------------
    def _insert_inactive_coach(self, username, scope):
        acct = UserAccount(
            id=f"user_{username}", username=username,
            password_hash=hash_password("pw"), role=Role.COACH,
            created_at=_clock(), scope=dict(scope), active=False)
        self.store.add_user_account(acct)
        return acct

    def test_reactivate_unscoped_coach_rejected(self):
        acct = self._insert_inactive_coach("legacy1", {})
        with self.assertRaises(ValidationError) as cm:
            self.accounts.set_active(acct.id, True)
        self.assertEqual(cm.exception.details.get("reason"), "scope_required")
        self.assertFalse(self.store.get_user_account(acct.id).active)

    def test_reactivate_coach_with_dangling_team_rejected(self):
        acct = self._insert_inactive_coach("legacy2", {"team_id": "team_ghost"})
        with self.assertRaises(ValidationError) as cm:
            self.accounts.set_active(acct.id, True)
        self.assertEqual(cm.exception.details.get("reason"),
                         "scope_subject_missing")
        self.assertFalse(self.store.get_user_account(acct.id).active)

    def test_reactivate_coach_with_valid_team_succeeds(self):
        acct = self._insert_inactive_coach("legacy3", {"team_id": "team_home"})
        reactivated = self.accounts.set_active(acct.id, True)
        self.assertTrue(reactivated.active)


class MemoryCoachScopeTest(CoachScopeContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurableCoachScopeTest(CoachScopeContract, unittest.TestCase):
    def make_store(self):
        return SqlStore(":memory:")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresCoachScopeTest(CoachScopeContract, unittest.TestCase):
    def make_store(self):
        store = SqlStore(os.environ["TEST_DATABASE_URL"])
        store.clear_all_data()  # isolate from any prior run's rows
        return store


# --------------------------------------------------------------------------- #
# HTTP layer + live scope gate (criteria 1, 2, 3).                             #
# --------------------------------------------------------------------------- #
class CoachScopeHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.gid = srv.STATE.game_id
        cls.home = srv.STATE.ids["home_team_id"]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _admin(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        return c

    def test_create_coach_without_team_is_rejected(self):
        admin = self._admin()
        status, body = self._req(admin, "POST", "/api/accounts",
                                 {"username": "coach_noteam", "password": "pw",
                                  "role": "coach", "scope": {}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertEqual(body["error"]["details"].get("reason"), "scope_required")

    def test_create_coach_with_toplevel_team_id_is_unknown_field(self):
        admin = self._admin()
        status, body = self._req(admin, "POST", "/api/accounts",
                                 {"username": "coach_misplaced", "password": "pw",
                                  "role": "coach", "team_id": self.home})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertEqual(body["error"]["details"].get("reason"), "unknown_field")
        self.assertIn("team_id", body["error"]["details"]["fields"])
        # Nothing was created.
        self.assertIsNone(
            srv.STATE.api.store.get_user_account_by_username("coach_misplaced"))

    def test_create_coach_with_valid_team_succeeds(self):
        admin = self._admin()
        status, body = self._req(admin, "POST", "/api/accounts",
                                 {"username": "coach_ok", "password": "pw",
                                  "role": "coach", "scope": {"team_id": self.home}})
        self.assertEqual(status, 200)
        self.assertEqual(body["scope"], {"team_id": self.home})

    def _insert_unscoped_coach(self, username):
        # Simulate a pre-fix legacy account: an ACTIVE coach with no team scope,
        # inserted directly (the service now refuses to create one).
        store = srv.STATE.api.store
        store.add_user_account(UserAccount(
            id=f"user_{username}", username=username,
            password_hash=hash_password("pw"), role=Role.COACH,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            scope={}, active=True))

    def test_existing_unscoped_coach_forbidden_on_roster_mutation(self):
        self._insert_unscoped_coach("legacy_coach_mut")
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "legacy_coach_mut", "password": "pw"})
        status, body = self._req(
            c, "POST", f"/api/games/{self.gid}/build-roster",
            {"team_id": self.home})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

    def test_existing_unscoped_coach_forbidden_on_private_read(self):
        self._insert_unscoped_coach("legacy_coach_read")
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "legacy_coach_read", "password": "pw"})
        status, _ = self._req(c, "GET", f"/api/games/{self.gid}/board")
        self.assertEqual(status, 403)


# --------------------------------------------------------------------------- #
# Readiness remediation path (criterion: no silent grandfathering).           #
# --------------------------------------------------------------------------- #
class CoachScopeReadinessTest(unittest.TestCase):
    def _api_with_team(self):
        api = ApiService(SqlStore(":memory:"))
        api.store.add_team(Team(id="team_home", name="Lions"))
        return api

    def _check(self, api):
        checks = api.get_readiness("production", cookie_hardened=True)["checks"]
        return next(c for c in checks if c["name"] == "coach_scope_bound")

    def test_bound_coach_passes(self):
        api = self._api_with_team()
        api.accounts.create_account("c", "pw", Role.COACH,
                                    scope={"team_id": "team_home"})
        self.assertTrue(self._check(api)["ok"])

    def test_unscoped_active_coach_fails_readiness(self):
        api = self._api_with_team()
        # A pre-fix legacy account: active coach with no team scope.
        api.store.add_user_account(UserAccount(
            id="user_legacy", username="legacy", password_hash="x",
            role=Role.COACH, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            scope={}, active=True))
        check = self._check(api)
        self.assertFalse(check["ok"])
        self.assertIn("1", check["detail"])

    def test_dangling_team_coach_fails_readiness(self):
        api = self._api_with_team()
        api.store.add_user_account(UserAccount(
            id="user_dangle", username="dangle", password_hash="x",
            role=Role.COACH, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            scope={"team_id": "team_ghost"}, active=True))
        self.assertFalse(self._check(api)["ok"])

    def test_non_production_never_blocks(self):
        api = self._api_with_team()
        api.store.add_user_account(UserAccount(
            id="user_legacy2", username="legacy2", password_hash="x",
            role=Role.COACH, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            scope={}, active=True))
        checks = api.get_readiness("demo", cookie_hardened=False)["checks"]
        check = next(c for c in checks if c["name"] == "coach_scope_bound")
        self.assertTrue(check["ok"])


if __name__ == "__main__":
    unittest.main()
