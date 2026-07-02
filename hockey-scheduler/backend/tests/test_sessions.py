"""Persistent, store-backed session store (#74).

The session layer keeps only a SHA-256 *hash* of each token in the store; the
raw token lives solely in the client cookie. Role/scope are resolved from the
backing UserAccount on every lookup (never duplicated on the session), so a
session always reflects the account's current state. These tests exercise the
SessionManager directly against both store backends and cover the guarantees:
hash-only storage, resolve-by-hash, logout revocation, expiry, deactivation
revocation, and SQL persistence across a reopen.
"""

import unittest
from datetime import datetime, timedelta, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.services import AccountService
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web.auth import SessionManager, hash_token

UTC = timezone.utc


class _Clock:
    """Settable clock so expiry is deterministic, not wall-clock dependent."""

    def __init__(self, t=None):
        self.t = t or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.t


def _make_account(store, username="coach", role="coach", scope=None):
    accounts = AccountService(store, lambda: datetime(2026, 1, 1, tzinfo=UTC))
    return accounts.create_account(username, "pw", role, scope=scope or {})


class SessionManagerTest(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.clock = _Clock()
        self.mgr = SessionManager(ttl_seconds=3600, clock=self.clock)
        self.acct = _make_account(self.store, scope={"team_id": "team_1"})

    def test_login_returns_token_but_stores_only_hash(self):
        token = self.mgr.login(self.store, self.acct.id)
        self.assertTrue(token)
        stored = list(self.store.sessions.values())
        self.assertEqual(len(stored), 1)
        sess = stored[0]
        # The raw token must never be persisted — only its hash.
        self.assertEqual(sess.token_hash, hash_token(token))
        self.assertNotEqual(sess.token_hash, token)
        self.assertNotIn(token, sess.token_hash)
        # Role/scope are NOT duplicated onto the session row.
        self.assertFalse(hasattr(sess, "role"))
        self.assertFalse(hasattr(sess, "scope"))

    def test_resolve_derives_role_and_scope_from_account(self):
        token = self.mgr.login(self.store, self.acct.id)
        resolved = self.mgr.resolve(self.store, token)
        self.assertEqual(resolved["user_id"], self.acct.id)
        self.assertEqual(resolved["role"], self.acct.role)
        self.assertEqual(resolved["scope"], {"team_id": "team_1"})

    def test_resolve_reflects_account_changes(self):
        token = self.mgr.login(self.store, self.acct.id)
        # Change the account's scope after the session was issued.
        self.acct.scope = {"team_id": "team_2"}
        self.store.save_user_account(self.acct)
        self.assertEqual(self.mgr.resolve(self.store, token)["scope"],
                         {"team_id": "team_2"})

    def test_resolve_unknown_or_empty_token_is_none(self):
        self.assertIsNone(self.mgr.resolve(self.store, None))
        self.assertIsNone(self.mgr.resolve(self.store, ""))
        self.assertIsNone(self.mgr.resolve(self.store, "not-a-real-token"))

    def test_logout_revokes_session(self):
        token = self.mgr.login(self.store, self.acct.id)
        self.assertIsNotNone(self.mgr.resolve(self.store, token))
        self.mgr.logout(self.store, token)
        self.assertIsNone(self.mgr.resolve(self.store, token))
        # Revocation is recorded, not deleted (auditable trail).
        sess = self.store.get_session_by_hash(hash_token(token))
        self.assertIsNotNone(sess.revoked_at)

    def test_expired_session_is_ignored(self):
        token = self.mgr.login(self.store, self.acct.id)
        self.assertIsNotNone(self.mgr.resolve(self.store, token))
        # Advance past the TTL.
        self.clock.t = self.clock.t + timedelta(seconds=3601)
        self.assertIsNone(self.mgr.resolve(self.store, token))

    def test_deactivation_revokes_all_user_sessions(self):
        t1 = self.mgr.login(self.store, self.acct.id)
        t2 = self.mgr.login(self.store, self.acct.id)
        self.mgr.revoke_for_user(self.store, self.acct.id)
        self.assertIsNone(self.mgr.resolve(self.store, t1))
        self.assertIsNone(self.mgr.resolve(self.store, t2))

    def test_inactive_account_cannot_resolve_even_without_explicit_revoke(self):
        token = self.mgr.login(self.store, self.acct.id)
        # Belt-and-suspenders: flip active without touching sessions.
        self.acct.active = False
        self.store.save_user_account(self.acct)
        self.assertIsNone(self.mgr.resolve(self.store, token))


class SqlSessionPersistenceTest(unittest.TestCase):
    """Sessions survive a store reopen when persisted to a real database file."""

    def test_session_persists_across_reopen(self):
        import os
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            clock = _Clock()
            store = SqlStore(path)
            acct = _make_account(store, scope={"team_id": "team_1"})
            mgr = SessionManager(ttl_seconds=3600, clock=clock)
            token = mgr.login(store, acct.id)

            # Reopen the database (fresh SqlStore over the same file).
            store2 = SqlStore(path)
            resolved = mgr.resolve(store2, token)
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved["user_id"], acct.id)
            self.assertEqual(resolved["scope"], {"team_id": "team_1"})

            # Logout on the reopened store also persists.
            mgr.logout(store2, token)
            store3 = SqlStore(path)
            self.assertIsNone(mgr.resolve(store3, token))
        finally:
            os.remove(path)

    def test_token_hash_is_unique_lookup(self):
        store = SqlStore(":memory:")
        a1 = _make_account(store, username="c1", scope={"team_id": "t1"})
        a2 = _make_account(store, username="c2", scope={"team_id": "t2"})
        mgr = SessionManager(clock=_Clock())
        tok1 = mgr.login(store, a1.id)
        tok2 = mgr.login(store, a2.id)
        self.assertEqual(mgr.resolve(store, tok1)["user_id"], a1.id)
        self.assertEqual(mgr.resolve(store, tok2)["user_id"], a2.id)


if __name__ == "__main__":
    unittest.main()
