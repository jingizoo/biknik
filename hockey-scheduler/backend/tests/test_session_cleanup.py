"""Expired/revoked session cleanup (#77).

The persistent sessions table (#74) would otherwise grow forever. Cleanup
deletes sessions that *finished* — expired or were revoked — more than a
retention window ago, while keeping active sessions and a recent grace window
of finished ones for audit/debugging. It is deterministic under an injected
clock and behaves identically on the in-memory and SQL stores.
"""

import unittest
from datetime import datetime, timedelta, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.services import AccountService
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web.auth import SessionManager, hash_token

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


class _Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def _account(store):
    # Role is irrelevant to session cleanup; a viewer needs no team scope (#266).
    return AccountService(store, lambda: T0).create_account("c", "pw", "viewer")


class _CleanupContract:
    """Shared assertions run against both store backends."""

    def _store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self._store()
        self.acct = _account(self.store)
        self.clock = _Clock()
        # 1h TTL, 7-day retention, deterministic clock.
        self.mgr = SessionManager(ttl_seconds=3600, clock=self.clock,
                                  retention_seconds=7 * 24 * 3600)

    def test_login_prunes_long_finished_but_keeps_active_and_recent(self):
        # A session revoked right away — will age past retention.
        old_rev = self.mgr.login(self.store, self.acct.id)
        self.mgr.logout(self.store, old_rev)
        self.assertIsNotNone(
            self.store.get_session_by_hash(hash_token(old_rev)))

        # Jump 8 days: the early-revoked one is now beyond the 7-day window.
        self.clock.t = T0 + timedelta(days=8)

        # A freshly revoked session — inside the grace window, must survive.
        recent_rev = self.mgr.login(self.store, self.acct.id)
        self.mgr.logout(self.store, recent_rev)

        # A subsequent login triggers opportunistic cleanup.
        self.mgr.login(self.store, self.acct.id)

        # The long-ago revoked session is gone; the recent one is retained.
        self.assertIsNone(
            self.store.get_session_by_hash(hash_token(old_rev)))
        self.assertIsNotNone(
            self.store.get_session_by_hash(hash_token(recent_rev)))

    def test_cleanup_removes_old_expired_unrevoked_sessions(self):
        token = self.mgr.login(self.store, self.acct.id)
        # Well past TTL and retention: expired long ago, never revoked.
        self.clock.t = T0 + timedelta(days=8)
        removed = self.mgr.cleanup(self.store)
        self.assertEqual(removed, 1)
        self.assertIsNone(self.store.get_session_by_hash(hash_token(token)))

    def test_cleanup_keeps_active_sessions(self):
        token = self.mgr.login(self.store, self.acct.id)
        # Barely advance — still within TTL, so still active.
        self.clock.t = T0 + timedelta(minutes=1)
        removed = self.mgr.cleanup(self.store)
        self.assertEqual(removed, 0)
        self.assertIsNotNone(self.mgr.resolve(self.store, token))

    def test_cleanup_keeps_recently_expired_within_grace(self):
        token = self.mgr.login(self.store, self.acct.id)
        # Expired (past 1h TTL) but only ~2h old — inside the 7-day window.
        self.clock.t = T0 + timedelta(hours=2)
        removed = self.mgr.cleanup(self.store)
        self.assertEqual(removed, 0)
        # Still present in the table, even though it no longer resolves.
        self.assertIsNotNone(self.store.get_session_by_hash(hash_token(token)))
        self.assertIsNone(self.mgr.resolve(self.store, token))

    def test_cleanup_does_not_disturb_resolve_of_survivors(self):
        keep = self.mgr.login(self.store, self.acct.id)
        stale = self.mgr.login(self.store, self.acct.id)
        self.mgr.logout(self.store, stale)
        self.clock.t = T0 + timedelta(days=8)
        # Re-issue a fresh, valid session at day 8 (so there is a survivor).
        fresh = self.mgr.login(self.store, self.acct.id)
        self.mgr.cleanup(self.store)
        # The day-8 session still resolves normally after cleanup.
        self.assertIsNotNone(self.mgr.resolve(self.store, fresh))


class MemoryCleanupTest(_CleanupContract, unittest.TestCase):
    def _store(self):
        return InMemoryStore()


class SqlCleanupTest(_CleanupContract, unittest.TestCase):
    def _store(self):
        return SqlStore(":memory:")


if __name__ == "__main__":
    unittest.main()
