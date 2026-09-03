"""Player deactivation versus cross-team substitute opt-in (#287).

An opt-in creates durable substitute state, so its active-player decision must
be made from a Player row locked in the same transaction as the insert.  A
plain read made before the Season lock is only a locator: on PostgreSQL another
connection may deactivate and commit the Player before enrollment writes.

Memory and SQLite exercise the deterministic post-deactivation refusal.  The
PostgreSQL test uses two independent real connections and pauses enrollment
after its initial plain Player read, lets deactivation commit first, then
resumes enrollment.  It is skipped loudly when ``TEST_DATABASE_URL`` is
absent; no SQLite fallback can masquerade as cross-connection coverage.
"""

import os
import threading
import unittest
from unittest import mock

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)
from helpers import fresh_sql_store
from test_cross_team_substitution import _Fixture

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain.errors import NotEligibleError
from hockey_scheduler.store import InMemoryStore, SqlStore


_PG_SKIP = (
    "PostgreSQL not configured (TEST_DATABASE_URL); the two-connection "
    "deactivation-wins race was NOT exercised. A skip is not a pass and no "
    "SQLite fallback is permitted for this ordering contract.")


def _game_audit_ids(store, game_id):
    return [row.id for row in store.audit_for_game(game_id)]


def _notification_ids(store, game_id):
    return [row.id for row in store.notifications_for_game(game_id)]


def _setup_audit_ids(store):
    return [row.id for row in store.all_setup_audit()]


def _assert_no_enrollment_effects(test, store, fx, audit_ids,
                                  notification_ids, setup_audit_ids, label):
    game_id = fx["game"]["id"]
    player_id = fx["player"]["id"]
    test.assertEqual(store.substitute_enrollments_for_player(player_id), [],
                     label)
    test.assertIsNone(store.substitute_for_player(game_id, player_id), label)
    test.assertEqual(_game_audit_ids(store, game_id), audit_ids, label)
    test.assertEqual(_notification_ids(store, game_id), notification_ids,
                     label)
    test.assertEqual(_setup_audit_ids(store), setup_audit_ids, label)


class DeactivatedPlayerEnrollmentRefusalTest(unittest.TestCase):
    """Every always-available backend refuses without enrollment side effects."""

    def test_deactivated_player_cannot_create_cross_team_enrollment(self):
        for label, store in (
                ("memory", InMemoryStore()),
                ("sqlite", SqlStore(":memory:"))):
            with self.subTest(backend=label):
                try:
                    fx = _Fixture().build(store)
                    game_id = fx["game"]["id"]
                    player_id = fx["player"]["id"]
                    target_id = fx["team4"]["id"]

                    fx["api"].setup.set_player_active(
                        player_id, False, actor_id="setup_admin")
                    audit_ids = _game_audit_ids(store, game_id)
                    notification_ids = _notification_ids(store, game_id)
                    setup_audit_ids = _setup_audit_ids(store)

                    with self.assertRaises(NotEligibleError, msg=label):
                        fx["api"].roster.enroll_substitute(
                            game_id, player_id, actor_id=player_id,
                            target_team_id=target_id)

                    self.assertFalse(store.get_player(player_id).is_active,
                                     label)
                    _assert_no_enrollment_effects(
                        self, store, fx, audit_ids, notification_ids,
                        setup_audit_ids, label)
                finally:
                    close = getattr(store, "close", None)
                    if close is not None:
                        close()


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), _PG_SKIP)
class PostgreSQLDeactivationWinsRaceTest(unittest.TestCase):
    """A committed deactivation beats an opt-in that read active beforehand."""

    def test_deactivation_commits_between_locator_read_and_enrollment_write(self):
        url = os.environ["TEST_DATABASE_URL"]
        checker = fresh_sql_store(url)
        enroll_store = None
        deactivate_store = None
        release_enrollment = threading.Event()
        worker = None
        try:
            self.assertIsInstance(checker, SqlStore)
            self.assertEqual(checker.backend, "postgres")
            fx = _Fixture().build(checker)
            game_id = fx["game"]["id"]
            player_id = fx["player"]["id"]
            target_id = fx["team4"]["id"]

            # One connection per actor.  Identity assertions ensure this test
            # can never silently collapse into same-connection serialization.
            enroll_store = SqlStore(url)
            deactivate_store = SqlStore(url)
            for store in (enroll_store, deactivate_store):
                self.assertEqual(store.backend, "postgres")
            self.assertIsNot(enroll_store.conn, deactivate_store.conn)
            self.assertIsNot(enroll_store.conn, checker.conn)
            self.assertIsNot(deactivate_store.conn, checker.conn)

            enroll_api = ApiService(enroll_store)
            enroll_api.roster.clock = fx["api"].roster.clock
            deactivate_api = ApiService(deactivate_store)

            locator_read = threading.Event()
            call_lock = threading.Lock()
            player_reads = 0
            original_get_player = enroll_store.get_player

            def pause_after_first_player_read(requested_id):
                nonlocal player_reads
                row = original_get_player(requested_id)
                if requested_id == player_id:
                    with call_lock:
                        player_reads += 1
                        first = player_reads == 1
                    if first:
                        locator_read.set()
                        if not release_enrollment.wait(20):
                            raise AssertionError(
                                "enrollment was never released after its "
                                "plain Player locator read")
                return row

            outcome = {}

            def enroll():
                try:
                    outcome["result"] = enroll_api.roster.enroll_substitute(
                        game_id, player_id, actor_id=player_id,
                        target_team_id=target_id)
                except Exception as exc:  # noqa: BLE001 - asserted below
                    outcome["error"] = exc

            with mock.patch.object(
                    enroll_store, "get_player",
                    side_effect=pause_after_first_player_read):
                worker = threading.Thread(target=enroll)
                worker.start()
                self.assertTrue(
                    locator_read.wait(20),
                    "enrollment never reached its pre-lock Player read")

                # The competing connection owns and commits the Player lock
                # while enrollment is parked on its earlier unlocked snapshot.
                deactivate_api.setup.set_player_active(
                    player_id, False, actor_id="setup_admin")
                self.assertFalse(
                    deactivate_store.get_player(player_id).is_active)
                audit_ids = _game_audit_ids(checker, game_id)
                notification_ids = _notification_ids(checker, game_id)
                setup_audit_ids = _setup_audit_ids(checker)
                release_enrollment.set()
                worker.join(30)

            self.assertFalse(worker.is_alive(), "enrollment worker hung")
            self.assertNotIn("result", outcome, outcome)
            self.assertIsInstance(outcome.get("error"), NotEligibleError,
                                  outcome)
            self.assertFalse(checker.get_player(player_id).is_active)
            _assert_no_enrollment_effects(
                self, checker, fx, audit_ids, notification_ids,
                setup_audit_ids, "postgres/deactivation-first")
        finally:
            release_enrollment.set()
            if worker is not None:
                worker.join(5)
            for store in (enroll_store, deactivate_store, checker):
                if store is not None:
                    store.close()


if __name__ == "__main__":
    unittest.main()
