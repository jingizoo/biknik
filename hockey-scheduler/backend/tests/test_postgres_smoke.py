"""PostgreSQL smoke tests — exercise the SQL store against real Postgres.

Skipped unless TEST_DATABASE_URL is set (CI sets it to a Postgres service).
The same SqlStore code path is covered against SQLite in test_sql_store.py.
"""

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain.errors import AlreadyClaimedError
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.store import SqlStore

URL = os.environ.get("TEST_DATABASE_URL")


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set (no Postgres available)")
class PostgresSmokeTest(unittest.TestCase):
    def setUp(self):
        # Start from a clean schema so the run is isolated.
        store = SqlStore(URL)
        try:
            store.reset_schema()
        finally:
            store.close()

    def test_seed_flow_and_reload_on_postgres(self):
        store = SqlStore(URL)
        _, gid, ids = build_full_demo_store(store)
        api = ApiService(store)
        self.assertEqual(api.get_roster_status(gid)["status"], "roster_confirmed")

        # Run the substitute flow + lock.
        api.set_availability(gid, ids["selected_player_id"], "unavailable")
        api.enroll_substitute(gid, ids["substitute_player_id"])
        api.add_substitute_to_roster(gid, ids["substitute_player_id"], actor_id="c")
        api.lock_roster(gid, actor_id="c")

        # Re-open Postgres in a fresh connection — state must persist.
        api2 = ApiService(SqlStore(URL))
        self.assertEqual(api2.get_roster_status(gid)["status"], "locked")
        ov = api2.get_demo_overview()
        self.assertEqual(ov["league"]["name"], "Alpine Ice Hockey League")
        # Pilot-scale demo data pack (#97): 12 teams, and the core scenario's
        # seeded game plus ~18 more published games. Derived from the
        # schedule itself (not hardcoded) so this doesn't drift again if the
        # pack's exact game count ever changes.
        self.assertEqual(len(ov["teams"]), 12)
        published = [g for g in ov["schedule"] if g["published"]]
        self.assertEqual(len(ov["public_fixtures"]), len(published))
        self.assertEqual(len(published), 19)
        store.close()
        api2.store.close()

    def test_concurrent_first_admin_claim_creates_exactly_one_account(self):
        """Two independent Postgres connections race the durable claim marker."""
        barrier = threading.Barrier(2)

        def attempt(username):
            store = SqlStore(URL)
            api = ApiService(store)
            try:
                barrier.wait()
                try:
                    account = api.accounts.create_first_admin_if_unclaimed(
                        username, "fixture-password", claim_method="browser")
                    return account.username
                except AlreadyClaimedError:
                    return "already_claimed"
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ("admin-a", "admin-b")))

        self.assertEqual(results.count("already_claimed"), 1)
        inspector = SqlStore(URL)
        try:
            self.assertEqual(len(inspector.all_user_accounts()), 1)
            marker = inspector.get_installation_state("primary")
            self.assertIsNotNone(marker)
            self.assertEqual(marker.claim_method, "browser")
        finally:
            inspector.close()


if __name__ == "__main__":
    unittest.main()
