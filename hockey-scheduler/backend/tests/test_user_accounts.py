import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.services import AccountService
from hockey_scheduler.services.passwords import hash_password, verify_password
from hockey_scheduler.store import InMemoryStore


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


class AccountServiceTest(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.accounts = AccountService(self.store, _clock)

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

    # -- login verification ---------------------------------------------------
    def test_wrong_password_returns_none(self):
        self.accounts.create_account("user5", "right-pw", Role.VIEWER)
        self.assertIsNone(self.accounts.verify_login("user5", "wrong-pw"))

    def test_unknown_username_returns_none(self):
        self.assertIsNone(self.accounts.verify_login("ghost", "anything"))

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
        self.api.create_user_account("op4", "pw", "coach", scope={"team_id": "t1"})
        rows = self.api.list_user_accounts()["user_accounts"]
        self.assertTrue(rows)
        self.assertTrue(all("password_hash" not in r for r in rows))

    def test_set_active_unknown_id_is_not_found(self):
        res = self.api.set_user_account_active("user_missing", False)
        self.assertEqual(res["error"]["code"], "not_found")

    def test_verify_login_end_to_end(self):
        self.api.create_user_account("op5", "correct", "official",
                                     scope={"official_id": "official_9"})
        self.assertIsNone(self.api.verify_login("op5", "wrong"))
        row = self.api.verify_login("op5", "correct")
        self.assertIsNotNone(row)
        self.assertEqual(row["role"], "official")
        self.assertEqual(row["scope"], {"official_id": "official_9"})
        self.assertNotIn("password_hash", row)


if __name__ == "__main__":
    unittest.main()
