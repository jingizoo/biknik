import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role, Team
from hockey_scheduler.services import AccountService
from hockey_scheduler.services.passwords import (
    DUMMY_PASSWORD_HASH,
    hash_password,
    verify_password,
)
from hockey_scheduler.store import InMemoryStore
from hockey_scheduler.web import server as srv


def _clock():
    return datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)


class PasswordHashingTest(unittest.TestCase):
    def test_hash_is_not_the_plaintext_and_verifies(self):
        h = hash_password("correct horse battery staple")
        self.assertNotIn("correct horse battery staple", h)
        self.assertTrue(verify_password("correct horse battery staple", h))

    def test_wrong_password_fails(self):
        h = hash_password("s3cret")
        self.assertFalse(verify_password("wrong", h))

    def test_two_hashes_of_the_same_password_differ(self):
        # Random per-password salt — no two hashes of "demo" should match byte
        # for byte, even though both verify the same plaintext.
        a, b = hash_password("demo"), hash_password("demo")
        self.assertNotEqual(a, b)
        self.assertTrue(verify_password("demo", a))
        self.assertTrue(verify_password("demo", b))

    def test_malformed_stored_hash_fails_closed(self):
        self.assertFalse(verify_password("demo", "not-a-real-hash"))
        self.assertFalse(verify_password("demo", ""))

    def test_dummy_hash_uses_current_iteration_count(self):
        # The unknown-username login path relies on the DUMMY placeholder paying
        # the SAME cost as a real hash (whatever the configured iteration count),
        # so it can't leak account existence via timing.
        from hockey_scheduler.services import passwords as _pw
        self.assertIn(f"pbkdf2_sha256${_pw._ITERATIONS}$", DUMMY_PASSWORD_HASH)
        self.assertFalse(verify_password("anything", DUMMY_PASSWORD_HASH))

    def test_production_default_iteration_count_is_strong(self):
        # Guard the strong production default independently of the test-only
        # override — nobody should weaken it.
        from hockey_scheduler.services import passwords as _pw
        self.assertEqual(_pw._DEFAULT_ITERATIONS, 260_000)

    def test_production_ignores_weak_iteration_override(self):
        # A stray HS_PBKDF2_ITERATIONS in a PRODUCTION process must NOT weaken
        # hashing — the override is test-only (#266 review). Run in a fresh
        # subprocess so the import-time resolution is exercised under
        # APP_MODE=production with the override set to 1.
        import os
        import subprocess
        import sys
        env = dict(os.environ)
        env["APP_MODE"] = "production"
        env["HS_PBKDF2_ITERATIONS"] = "1"
        code = (
            "from hockey_scheduler.services.passwords import hash_password, _ITERATIONS;"
            "assert _ITERATIONS == 260000, _ITERATIONS;"
            "assert '$260000$' in hash_password('x'), 'weak production hash!'")
        result = subprocess.run([sys.executable, "-c", code], env=env,
                                cwd=str(BACKEND), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class AccountServiceTest(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.accounts = AccountService(self.store, _clock)
        # A real team so a coach account has a valid scope subject (#266).
        self.store.add_team(Team(id="team_1", name="Test Team"))

    # -- create --------------------------------------------------------------
    def test_create_and_login(self):
        acct = self.accounts.create_account("coach1", "demo-pw", Role.COACH,
                                            scope={"team_id": "team_1"})
        self.assertEqual(acct.username, "coach1")
        self.assertEqual(acct.role, Role.COACH)
        self.assertTrue(acct.active)
        self.assertNotIn("demo-pw", acct.password_hash)
        found = self.accounts.verify_login("coach1", "demo-pw")
        self.assertIsNotNone(found)
        self.assertEqual(found.id, acct.id)

    def test_username_is_normalized(self):
        self.accounts.create_account("  Admin ", "pw", Role.LEAGUE_ADMIN)
        self.assertIsNotNone(self.accounts.verify_login("admin", "pw"))
        self.assertIsNotNone(self.accounts.verify_login("ADMIN", "pw"))

    def test_duplicate_username_rejected(self):
        self.accounts.create_account("dup", "pw1", Role.VIEWER)
        with self.assertRaises(Exception):
            self.accounts.create_account("dup", "pw2", Role.VIEWER)

    def test_blank_username_or_password_rejected(self):
        with self.assertRaises(Exception):
            self.accounts.create_account("", "pw", Role.VIEWER)
        with self.assertRaises(Exception):
            self.accounts.create_account("user2", "", Role.VIEWER)

    def test_unknown_role_rejected(self):
        with self.assertRaises(Exception):
            self.accounts.create_account("user3", "pw", "not-a-role")

    def test_scope_defaults_to_empty_dict(self):
        acct = self.accounts.create_account("user4", "pw", Role.VIEWER)
        self.assertEqual(acct.scope, {})

    def test_create_account_for_missing_official_is_rejected(self):
        with self.assertRaises(Exception):
            self.accounts.create_account(
                "no_such_official", "pw", Role.OFFICIAL,
                scope={"official_id": "official_missing"})
        self.assertIsNone(
            self.store.get_user_account_by_username("no_such_official"))

    def test_create_account_for_missing_player_is_rejected(self):
        with self.assertRaises(Exception):
            self.accounts.create_account(
                "no_such_player", "pw", Role.PLAYER,
                scope={"player_id": "player_missing"})
        self.assertIsNone(
            self.store.get_user_account_by_username("no_such_player"))

    def test_create_account_rolls_back_on_forced_audit_failure(self):
        # #232 review 7: create_account's account insert + audit write must
        # be one transaction — an audit failure must leave zero account row,
        # not a live account with no audit trail.
        accounts_before = len(self.store.all_user_accounts())
        audits_before = len(self.store.all_setup_audit())
        original = self.store.add_setup_audit
        def boom(entry):
            raise RuntimeError("forced audit failure")
        self.store.add_setup_audit = boom
        try:
            with self.assertRaises(RuntimeError):
                self.accounts.create_account("atomic_user", "pw", Role.VIEWER)
        finally:
            self.store.add_setup_audit = original
        self.assertEqual(len(self.store.all_user_accounts()), accounts_before)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)
        self.assertIsNone(self.store.get_user_account_by_username("atomic_user"))

    # -- login verification ---------------------------------------------------
    def test_wrong_password_returns_none(self):
        self.accounts.create_account("user5", "right-pw", Role.VIEWER)
        self.assertIsNone(self.accounts.verify_login("user5", "wrong-pw"))

    def test_unknown_username_returns_none(self):
        self.assertIsNone(self.accounts.verify_login("ghost", "anything"))

    def test_unknown_username_uses_full_cost_dummy_hash(self):
        # Regression: an earlier dummy hash used 1 PBKDF2 iteration versus the
        # real 260,000, so unknown usernames returned much faster than known
        # ones with a wrong password — a timing side channel for account
        # enumeration. Assert the dummy passed to verify_password has the
        # real iteration count embedded.
        import hockey_scheduler.services.account_service as account_mod

        seen = {}
        original = account_mod.verify_password

        def fake_verify(password, stored):
            seen["stored"] = stored
            return False

        account_mod.verify_password = fake_verify
        try:
            self.accounts.verify_login("ghost", "anything")
        finally:
            account_mod.verify_password = original

        from hockey_scheduler.services import passwords as _pw
        self.assertIn(f"pbkdf2_sha256${_pw._ITERATIONS}$", seen["stored"])

    def test_deactivated_account_cannot_login(self):
        acct = self.accounts.create_account("user6", "pw", Role.VIEWER)
        self.accounts.set_active(acct.id, False)
        self.assertIsNone(self.accounts.verify_login("user6", "pw"))

    def test_reactivated_account_can_login_again(self):
        acct = self.accounts.create_account("user7", "pw", Role.VIEWER)
        self.accounts.set_active(acct.id, False)
        self.accounts.set_active(acct.id, True)
        self.assertIsNotNone(self.accounts.verify_login("user7", "pw"))

    def test_set_active_unknown_id_raises(self):
        with self.assertRaises(Exception):
            self.accounts.set_active("user_missing", False)

    def test_list_accounts_sorted_by_username(self):
        self.accounts.create_account("zeta", "pw", Role.VIEWER)
        self.accounts.create_account("alpha", "pw", Role.VIEWER)
        names = [a.username for a in self.accounts.list_accounts()]
        self.assertEqual(names, ["alpha", "zeta"])


class ApiServiceAccountFacadeTest(unittest.TestCase):
    """Facade-level: structured errors, no password leakage, permissions."""

    def setUp(self):
        self.api = ApiService()

    def test_create_row_never_includes_password_hash(self):
        row = self.api.create_user_account("op1", "pw", "viewer")
        self.assertNotIn("password_hash", row)
        self.assertNotIn("password", row)
        self.assertEqual(row["role"], "viewer")
        self.assertTrue(row["active"])

    def test_duplicate_username_is_a_structured_error(self):
        self.api.create_user_account("op2", "pw", "viewer")
        res = self.api.create_user_account("op2", "pw2", "coach")
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_unknown_role_is_a_structured_error(self):
        res = self.api.create_user_account("op3", "pw", "wizard")
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_list_never_includes_password_hash(self):
        # A viewer needs no scope subject — this test is about the serialized
        # row shape, not scoping (#266 requires a real team for a coach).
        self.api.create_user_account("op4", "pw", "viewer")
        rows = self.api.list_user_accounts()["user_accounts"]
        self.assertTrue(rows)
        self.assertTrue(all("password_hash" not in r for r in rows))

    def test_set_active_unknown_id_is_not_found(self):
        res = self.api.set_user_account_active("user_missing", False)
        self.assertEqual(res["error"]["code"], "not_found")

    def test_verify_login_end_to_end(self):
        # #232 review 7: a scoped official_id must resolve to a real Official
        # to be accepted at all, so this exercises a genuinely created one
        # rather than an arbitrary literal id.
        official = self.api.create_official("Login Official")["id"]
        self.api.create_user_account("op5", "correct", "official",
                                     scope={"official_id": official})
        self.assertIsNone(self.api.verify_login("op5", "wrong"))
        row = self.api.verify_login("op5", "correct")
        self.assertIsNotNone(row)
        self.assertEqual(row["role"], "official")
        self.assertEqual(row["scope"], {"official_id": official})
        self.assertNotIn("password_hash", row)


class AccountCreationHttpTest(unittest.TestCase):
    """POST /api/accounts over real HTTP (#135) — the League-Admin-only
    create path a new "Create account" Users-tab form calls."""

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
        cls.httpd.server_close()

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _login(self, username, password="demo"):
        opener = self._client()
        self._req(opener, "POST", "/api/auth/login",
                  {"username": username, "password": password})
        return opener

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

    def test_body_actor_id_is_rejected_as_unknown_field(self):
        # A forged body actor_id must never reach the audit trail. #266 makes
        # this fail loudly: unknown top-level fields (actor_id included) are
        # rejected outright rather than silently ignored, so no account or audit
        # row is written at all.
        admin = self._login("admin")
        status, body = self._req(
            admin, "POST", "/api/accounts",
            {"username": "spoof_test", "password": "pw", "role": "viewer",
             "actor_id": "spoofed_someone_else"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertIn("actor_id", body["error"]["details"]["fields"])
        self.assertIsNone(
            srv.STATE.api.store.get_user_account_by_username("spoof_test"))

    def test_actor_attribution_is_server_resolved(self):
        # A clean create is attributed to the real, session-resolved league
        # admin's own account id — never a client-supplied value.
        admin = self._login("admin")
        admin_id = srv.STATE.api.store.get_user_account_by_username("admin").id
        status, body = self._req(
            admin, "POST", "/api/accounts",
            {"username": "attributed_ok", "password": "pw", "role": "viewer"})
        self.assertEqual(status, 200)
        entries = srv.STATE.api.store.all_setup_audit()
        mint = next(e for e in reversed(entries)
                   if e.action == "user_account_created" and e.entity_id == body["id"])
        self.assertEqual(mint.actor_id, admin_id)

    def test_league_admin_can_create_a_coach_account_with_team_scope(self):
        admin = self._login("admin")
        home_team = srv.STATE.ids["home_team_id"]
        status, body = self._req(
            admin, "POST", "/api/accounts",
            {"username": "new_coach_http", "password": "pw", "role": "coach",
             "scope": {"team_id": home_team}})
        self.assertEqual(status, 200)
        self.assertEqual(body["scope"], {"team_id": home_team})
        self.assertNotIn("password_hash", body)
        # The new account can actually sign in.
        new_coach = self._login("new_coach_http", "pw")
        me_status, me = self._req(new_coach, "GET", "/api/auth/me")
        self.assertEqual(me_status, 200)
        self.assertEqual(me["user"]["username"], "new_coach_http")

    def test_non_admin_cannot_create_account(self):
        coach = self._login("coach")
        status, body = self._req(
            coach, "POST", "/api/accounts",
            {"username": "sneaky3", "password": "pw", "role": "viewer"})
        self.assertEqual(status, 403)

    def test_duplicate_username_over_http_is_a_structured_error(self):
        admin = self._login("admin")
        self._req(admin, "POST", "/api/accounts",
                 {"username": "dupe_http", "password": "pw", "role": "viewer"})
        status, body = self._req(
            admin, "POST", "/api/accounts",
            {"username": "dupe_http", "password": "pw2", "role": "viewer"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_blank_username_over_http_is_a_structured_error(self):
        admin = self._login("admin")
        status, body = self._req(
            admin, "POST", "/api/accounts",
            {"username": "", "password": "pw", "role": "viewer"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_blank_password_over_http_is_a_structured_error(self):
        admin = self._login("admin")
        status, body = self._req(
            admin, "POST", "/api/accounts",
            {"username": "blank_pw_http", "password": "", "role": "viewer"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_creating_account_for_deleted_official_over_http_is_rejected(self):
        # #232 review 7: deleting an Official and then POSTing a fresh
        # account scoped to its old id over the real HTTP path a League
        # Admin actually uses must be refused, not just the service-level
        # call — with zero account/audit mutation.
        admin = self._login("admin")
        official = srv.STATE.api.create_official("HTTP Ghost Official")
        srv.STATE.api.delete_official(official["id"])
        accounts_before = len(srv.STATE.api.store.all_user_accounts())
        audits_before = len(srv.STATE.api.store.all_setup_audit())
        status, body = self._req(
            admin, "POST", "/api/accounts",
            {"username": "official_ghost_http", "password": "pw", "role": "official",
             "scope": {"official_id": official["id"]}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertEqual(body["error"]["details"]["reason"], "scope_subject_missing")
        self.assertEqual(len(srv.STATE.api.store.all_user_accounts()), accounts_before)
        self.assertEqual(len(srv.STATE.api.store.all_setup_audit()), audits_before)
        self.assertIsNone(
            srv.STATE.api.store.get_user_account_by_username("official_ghost_http"))

    def test_creating_account_for_deleted_player_over_http_is_rejected(self):
        admin = self._login("admin")
        home_team = srv.STATE.ids["home_team_id"]
        player = srv.STATE.api.create_player(
            home_team, "HTTP Ghost Player", "forward")
        srv.STATE.api.delete_player(player["id"])
        accounts_before = len(srv.STATE.api.store.all_user_accounts())
        audits_before = len(srv.STATE.api.store.all_setup_audit())
        status, body = self._req(
            admin, "POST", "/api/accounts",
            {"username": "player_ghost_http", "password": "pw", "role": "player",
             "scope": {"team_id": home_team, "player_id": player["id"]}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertEqual(body["error"]["details"]["reason"], "scope_subject_missing")
        self.assertEqual(len(srv.STATE.api.store.all_user_accounts()), accounts_before)
        self.assertEqual(len(srv.STATE.api.store.all_setup_audit()), audits_before)
        self.assertIsNone(
            srv.STATE.api.store.get_user_account_by_username("player_ghost_http"))

    def test_creating_account_for_existing_official_over_http_still_works(self):
        admin = self._login("admin")
        official = srv.STATE.api.create_official("HTTP Live Official")
        status, body = self._req(
            admin, "POST", "/api/accounts",
            {"username": "official_live_http", "password": "pw", "role": "official",
             "scope": {"official_id": official["id"]}})
        self.assertEqual(status, 200)
        self.assertEqual(body["scope"], {"official_id": official["id"]})


if __name__ == "__main__":
    unittest.main()
