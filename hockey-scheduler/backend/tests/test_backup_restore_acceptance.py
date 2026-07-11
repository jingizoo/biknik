"""Backup/restore acceptance smoke coverage (#174 PR E).

Exercises hockey_scheduler.acceptance.backup_restore in CI: a configured
deployment backed up, restored into a fresh database, started against the
restore, and verified equivalent (record census + onboarding status +
migrations). Also proves the check actually *fails* on a divergent restore, so
a passing run means something.
"""

import os
import tempfile
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.acceptance import backup_restore as br
from hockey_scheduler.api import ApiService
from hockey_scheduler.store import SqlStore


class BackupRestoreAcceptanceTest(unittest.TestCase):
    def test_sample_deployment_survives_backup_and_restore(self):
        report = br.run_acceptance()
        self.assertTrue(report["ok"], report["mismatches"])
        self.assertEqual(report["mismatches"], [])
        # The sample is fully onboarded, so the restored copy is too.
        self.assertTrue(report["migrations_current"])
        self.assertTrue(report["ready_to_schedule"])
        self.assertTrue(report["onboarding_complete"])
        # A representative census landed (not an empty database).
        self.assertEqual(report["census"]["organizations"], 1)
        self.assertEqual(report["census"]["teams"], 2)
        self.assertGreaterEqual(report["census"]["setup_audit"], 6)

    def test_restore_of_an_existing_durable_db_matches(self):
        # An operator pointing the check at a real durable file: no reseed, just
        # back up what's there and verify the restore matches.
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, path)
        api = ApiService(SqlStore(path))
        br.configure_sample(api)
        del api

        report = br.run_acceptance(path)
        self.assertTrue(report["ok"], report["mismatches"])
        self.assertEqual(report["census"]["players"], 1)

    def test_compare_flags_a_divergent_restore(self):
        # The guard rail: if the restore is missing a record, compare() must
        # surface it — otherwise a passing run would prove nothing.
        source = {"census": {"players": 5}, "onboarding": {"complete": True},
                  "migrations_current": True}
        restored = {"census": {"players": 4}, "onboarding": {"complete": True},
                    "migrations_current": True}
        mismatches = br.compare(source, restored)
        self.assertTrue(any("players" in m for m in mismatches))

    def test_compare_flags_onboarding_and_migration_drift(self):
        source = {"census": {}, "onboarding": {"ready_to_schedule": True},
                  "migrations_current": True}
        restored = {"census": {}, "onboarding": {"ready_to_schedule": False},
                    "migrations_current": False}
        mismatches = br.compare(source, restored)
        self.assertIn("onboarding status differs after restore", mismatches)
        self.assertTrue(any("migrations are not current" in m for m in mismatches))

    def test_backup_refuses_non_sqlite_store(self):
        class _FakePg:
            backend = "postgres"
        with self.assertRaises(RuntimeError) as ctx:
            br.backup_sqlite(_FakePg(), "/tmp/unused.db")
        self.assertIn("pg_dump", str(ctx.exception))

    def test_report_carries_no_credentials_or_pii(self):
        # The sample creates a player and an admin credential; neither the name
        # nor the password may appear anywhere in the acceptance report.
        import json
        blob = json.dumps(br.run_acceptance())
        self.assertNotIn("Vince Skater", blob)
        self.assertNotIn("not-a-real-secret", blob)
        self.assertNotIn(br.SAMPLE_ADMIN, blob)


if __name__ == "__main__":
    unittest.main()
