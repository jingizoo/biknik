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

    def _demo_resets(self, state):
        return [a for a in state.api.store.all_setup_audit()
                if a.action == "demo_reset"]

    def test_reset_seeds_the_baseline(self):
        with self.make_env() as state:
            self.assertTrue(state.api.store.all_leagues())
            self.assertTrue(state.api.store.all_teams())
            self.assertIsNotNone(state.game_id)

    def test_reset_is_idempotent(self):
        with self.make_env() as state:
            names1 = sorted(lg.name for lg in state.api.store.all_leagues())
            teams1 = len(state.api.store.all_teams())
            state.reset()
            names2 = sorted(lg.name for lg in state.api.store.all_leagues())
            teams2 = len(state.api.store.all_teams())
            self.assertEqual(names1, names2)
            self.assertEqual(teams1, teams2)

    def test_boot_reset_writes_no_demo_reset_audit(self):
        # The initial seed (no actor) records no demo_reset row.
        with self.make_env() as state:
            self.assertEqual(self._demo_resets(state), [])

    def test_actor_reset_writes_one_audit_naming_the_actor(self):
        with self.make_env() as state:
            state.reset(actor_id="user_admin")
            resets = self._demo_resets(state)
            self.assertEqual(len(resets), 1)
            self.assertEqual(resets[0].actor_id, "user_admin")
            self.assertIn("reset_at", resets[0].detail)

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


class MemoryDemoResetTest(DemoResetContract, unittest.TestCase):
    def make_env(self):
        return _ResetEnv(database_url=None)  # in-memory store


class SqliteDemoResetTest(DemoResetContract, unittest.TestCase):
    def setUp(self):
        # A real file (not :memory:) so the reset's fresh SqlStore and the live
        # one share one durable database — the setting where a non-atomic reseed
        # could otherwise strand a half-empty schema.
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        try:
            os.remove(self._db_path)
        except OSError:
            pass

    def make_env(self):
        return _ResetEnv(database_url=self._db_path)


if __name__ == "__main__":
    unittest.main()
