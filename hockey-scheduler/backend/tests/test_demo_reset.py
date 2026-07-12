"""Demo reset behavior + atomicity (#215).

The reset must restore the canonical seeded baseline, be idempotent, audit the
authenticated actor, and — crucially — be atomic: a mid-seed failure leaves the
previous dataset (and the live facade) untouched, never a half-rebuilt demo.
The same guarantees hold whether the demo is backed by the in-memory store or a
durable SQL store, so the atomicity case runs against both.
"""

import os
import tempfile
import unittest

import hockey_scheduler.web.server as srv


class _ResetEnv:
    """Construct a fresh DemoState under a chosen backend, restoring env after."""

    def __init__(self, database_url=None):
        self.database_url = database_url
        self._saved = {}

    def __enter__(self):
        for key, val in {"APP_MODE": "demo",
                         "DATABASE_URL": self.database_url}.items():
            self._saved[key] = os.environ.get(key)
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        self.state = srv.DemoState()
        return self.state

    def __exit__(self, *exc):
        # Close the live store so a durable (Postgres) backend doesn't leak the
        # connection between tests.
        try:
            self.state.api.store.close()
        except Exception:
            pass
        for key, val in self._saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        return False


class DemoResetContract:
    """Backend-agnostic reset behavior; subclasses pick the store backend."""

    def make_env(self):
        raise NotImplementedError

    def assert_store_closed(self, store):
        """Assert the store's backing resource is released (backend-specific)."""
        raise NotImplementedError

    def _demo_resets(self, state):
        return [a for a in state.api.store.all_setup_audit()
                if a.action == "demo_reset"]

    def test_boot_is_a_clean_slate(self):
        # #215: the demo boots empty (no auto-seed), with only the admin persona.
        with self.make_env() as state:
            self.assertEqual(state.api.store.all_leagues(), [])
            self.assertEqual(state.api.store.all_teams(), [])
            self.assertIsNone(state.game_id)
            self.assertIsNotNone(state.api.store.get_user_account("user_admin"))

    def test_reset_seeds_the_baseline(self):
        with self.make_env() as state:
            state.reset()  # Load/Reset builds the canonical sample dataset
            self.assertTrue(state.api.store.all_leagues())
            self.assertTrue(state.api.store.all_teams())
            self.assertIsNotNone(state.game_id)

    def test_reset_is_idempotent(self):
        with self.make_env() as state:
            state.reset()
            names1 = sorted(lg.name for lg in state.api.store.all_leagues())
            teams1 = len(state.api.store.all_teams())
            state.reset()
            names2 = sorted(lg.name for lg in state.api.store.all_leagues())
            teams2 = len(state.api.store.all_teams())
            self.assertEqual(names1, names2)
            self.assertEqual(teams1, teams2)

    def test_clear_returns_to_a_clean_slate(self):
        with self.make_env() as state:
            state.reset()  # seed
            self.assertTrue(state.api.store.all_leagues())
            state.reset(seed=False, audit_action="demo_cleared")
            self.assertEqual(state.api.store.all_leagues(), [])
            self.assertEqual(state.api.store.all_teams(), [])
            self.assertIsNotNone(state.api.store.get_user_account("user_admin"))

    def test_boot_writes_no_demo_reset_audit(self):
        # The clean-slate boot (no actor) records no demo_reset row.
        with self.make_env() as state:
            self.assertEqual(self._demo_resets(state), [])

    def test_actor_reset_writes_one_audit_naming_the_actor(self):
        with self.make_env() as state:
            state.reset(actor_id="user_admin")
            resets = self._demo_resets(state)
            self.assertEqual(len(resets), 1)
            self.assertEqual(resets[0].actor_id, "user_admin")
            self.assertIn("at", resets[0].detail)

    def test_reset_is_atomic_on_seed_failure(self):
        # Establish a baseline, then force the reseed to fail partway. The live
        # facade and the previous dataset must be completely untouched.
        with self.make_env() as state:
            state.reset(actor_id="user_admin")
            before_api = state.api
            before_leagues = sorted(lg.name for lg in state.api.store.all_leagues())
            self.assertTrue(before_leagues)

            original = srv.build_full_demo_store

            def boom(_store):
                raise RuntimeError("forced mid-seed failure")

            srv.build_full_demo_store = boom
            try:
                with self.assertRaises(RuntimeError):
                    state.reset(actor_id="user_admin")
            finally:
                srv.build_full_demo_store = original

            # No swap happened, and the prior dataset is intact.
            self.assertIs(state.api, before_api)
            after_leagues = sorted(lg.name for lg in state.api.store.all_leagues())
            self.assertEqual(before_leagues, after_leagues)

    def test_successful_reset_closes_the_superseded_store(self):
        # Repeated resets must not leak the old store's connection (#215 r4).
        with self.make_env() as state:
            old_store = state.api.store
            state.reset(actor_id="user_admin")
            self.assertIsNot(state.api.store, old_store)
            self.assert_store_closed(old_store)

    def test_rate_limit_slate_is_reset_only_on_success(self):
        with self.make_env() as state:
            calls = []
            saved = srv.RATE_LIMITER.reset
            srv.RATE_LIMITER.reset = lambda: calls.append(1)
            original = srv.build_full_demo_store
            try:
                srv.build_full_demo_store = \
                    lambda _s: (_ for _ in ()).throw(RuntimeError("boom"))
                with self.assertRaises(RuntimeError):
                    state.reset(actor_id="user_admin")
                self.assertEqual(calls, [])  # failure → no side effect
                srv.build_full_demo_store = original
                state.reset(actor_id="user_admin")
                self.assertEqual(len(calls), 1)  # success → reset once
            finally:
                srv.RATE_LIMITER.reset = saved
                srv.build_full_demo_store = original

    def test_failed_reset_closes_the_half_built_store(self):
        with self.make_env() as state:
            made = []
            real_create = srv.create_store
            original = srv.build_full_demo_store
            srv.create_store = lambda *a, **k: made.append(real_create(*a, **k)) or made[-1]
            srv.build_full_demo_store = \
                lambda _s: (_ for _ in ()).throw(RuntimeError("boom"))
            try:
                with self.assertRaises(RuntimeError):
                    state.reset(actor_id="user_admin")
            finally:
                srv.create_store = real_create
                srv.build_full_demo_store = original
            self.assertTrue(made)
            self.assert_store_closed(made[-1])


class MemoryDemoResetTest(DemoResetContract, unittest.TestCase):
    def make_env(self):
        return _ResetEnv(database_url=None)  # in-memory store

    def assert_store_closed(self, store):
        # In-memory close() is a no-op (no external resource); the swap itself
        # is the observable contract, already asserted by the caller.
        pass


class DurableDemoResetTest(DemoResetContract, unittest.TestCase):
    """Reset atomicity against a durable SQL store.

    Honors TEST_DATABASE_URL so the PostgreSQL CI job exercises the reentrant
    transaction + transactional-DDL reset path against Postgres; falls back to a
    real SQLite file locally (not ``:memory:``, so the reset's fresh SqlStore and
    the live one share one durable database — the setting where a non-atomic
    reseed could otherwise strand a half-empty schema).
    """

    def setUp(self):
        self._db_path = None
        self._url = os.environ.get("TEST_DATABASE_URL")
        if not self._url:
            fd, self._db_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            self._url = self._db_path

    def tearDown(self):
        if self._db_path:
            try:
                os.remove(self._db_path)
            except OSError:
                pass

    def make_env(self):
        return _ResetEnv(database_url=self._url)

    def assert_store_closed(self, store):
        # A closed SQL connection can no longer serve queries.
        with self.assertRaises(Exception):
            store.all_leagues()


if __name__ == "__main__":
    unittest.main()
