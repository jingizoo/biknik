"""Schedule-change notifications (#87).

Game publish / move / cancel, official assign-unassign, and roster lock/unlock
emit delivery-backed feed notifications to the affected parties (teams,
officials, and — for publish/cancel — the public). Emission is idempotent on
no-op operations and honors each recipient's channel preferences (#81).
"""

import unittest
from datetime import datetime, timedelta, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import IceSlot, IceSlotType, NotificationChannel
from hockey_scheduler.full_demo import build_full_demo_store

UTC = timezone.utc


class ScheduleNotificationTest(unittest.TestCase):
    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store()
        self.api = ApiService(self.store)
        self.home = self.ids["home_team_id"]
        self.away = self.ids["away_team_id"]
        self.ref = self.ids["referee_id"]

    def _feed_kinds(self):
        return [n.kind.value for n in self.store.all_notifications_feed()]

    def _kinds_delta(self, before):
        after = self._feed_kinds()
        return after[len(before):]

    def _recipients_for_kind(self, kind):
        return {n.audience.value + ":" + (n.audience_ref or "")
                for n in self.store.all_notifications_feed()
                if n.kind.value == kind}

    def test_publish_notifies_teams_officials_and_public(self):
        self.api.setup.publish_game(self.game_id, published=False)
        before = self._feed_kinds()
        self.api.setup.publish_game(self.game_id, published=True)
        new = self._kinds_delta(before)
        self.assertTrue(all(k == "game_published" for k in new))
        recips = self._recipients_for_kind("game_published")
        self.assertIn(f"coach:{self.home}", recips)
        self.assertIn(f"coach:{self.away}", recips)
        self.assertIn(f"official:{self.ref}", recips)
        self.assertIn("public:", recips)

    def test_publish_is_idempotent(self):
        self.api.setup.publish_game(self.game_id, published=True)  # already published
        before = self._feed_kinds()
        self.api.setup.publish_game(self.game_id, published=True)
        self.assertEqual(self._kinds_delta(before), [])

    def test_move_notifies_teams_and_officials(self):
        # Add a free game slot to move into.
        base = datetime(2027, 1, 1, 18, tzinfo=UTC)
        self.store.add_ice_slot(IceSlot(id="slot_move", rink_id=self.ids.get("main_rink_id", "rink_1"),
                                        start_time=base, end_time=base + timedelta(hours=1),
                                        slot_type=IceSlotType.GAME))
        before = self._feed_kinds()
        self.api.setup.move_game(self.game_id, "slot_move", reason="Rink swap")
        new = self._kinds_delta(before)
        self.assertTrue(new and all(k == "game_moved" for k in new))
        recips = self._recipients_for_kind("game_moved")
        self.assertIn(f"coach:{self.home}", recips)
        self.assertIn(f"official:{self.ref}", recips)

    def test_cancel_notifies_and_is_idempotent(self):
        before = self._feed_kinds()
        self.api.roster.cancel_game(self.game_id)
        new = self._kinds_delta(before)
        self.assertTrue(all(k == "game_cancelled" for k in new))
        self.assertIn("public:", self._recipients_for_kind("game_cancelled"))
        # Cancelling again emits nothing.
        before2 = self._feed_kinds()
        self.api.roster.cancel_game(self.game_id)
        self.assertEqual(self._kinds_delta(before2), [])

    def test_lock_and_unlock_notify_and_are_idempotent(self):
        before = self._feed_kinds()
        self.api.roster.lock_roster(self.game_id)
        self.assertIn("roster_locked", self._kinds_delta(before))
        b2 = self._feed_kinds()
        self.api.roster.lock_roster(self.game_id)  # already locked
        self.assertNotIn("roster_locked", self._kinds_delta(b2))
        b3 = self._feed_kinds()
        self.api.roster.unlock_roster(self.game_id)
        self.assertIn("roster_unlocked", self._kinds_delta(b3))

    def test_unassign_notifies_the_official(self):
        before = self._feed_kinds()
        self.api.unassign_official(self.ids["ref_assignment_id"])
        new = self._kinds_delta(before)
        self.assertIn("assignment_unassigned", new)
        self.assertIn(f"official:{self.ref}",
                      self._recipients_for_kind("assignment_unassigned"))

    def test_disabled_preference_suppresses_that_channel_delivery(self):
        # The home team opts out of email; publishing still creates the feed
        # notification and the push delivery, but no NEW email delivery for them.
        self.api.set_notification_preference(f"team:{self.home}", "email", False)
        self.api.setup.publish_game(self.game_id, published=False)
        before_ids = {d.id for d in self.store.all_notification_deliveries()}
        self.api.setup.publish_game(self.game_id, published=True)
        new = [d for d in self.store.all_notification_deliveries()
               if d.id not in before_ids
               and d.recipient_ref == f"team:{self.home}"]
        self.assertTrue(new, "publish should create deliveries for the home team")
        self.assertFalse(any(d.channel == NotificationChannel.EMAIL for d in new))
        self.assertTrue(any(d.channel == NotificationChannel.PUSH for d in new))


class SqlBackedLockCancelTest(unittest.TestCase):
    """lock/unlock/cancel run inside a real transaction on SqlStore (#87).

    The schedule-change helpers (`_game_label`, `_notify_game_change`) are
    called from inside these @_transactional methods, so they must NOT be
    transactional themselves — otherwise SqlStore raises "cannot start a
    transaction within a transaction". InMemoryStore's no-op transaction hid
    this, so these cases run against a live SqlStore.
    """

    def setUp(self):
        from hockey_scheduler.store import SqlStore
        self.store, self.game_id, self.ids = build_full_demo_store(
            SqlStore(":memory:"))
        self.api = ApiService(self.store)

    def _kinds(self):
        return [n.kind.value for n in self.store.all_notifications_feed()]

    def test_lock_then_unlock_commit_and_notify_on_sql(self):
        locked = self.api.lock_roster(self.game_id)
        self.assertNotIn("error", locked)
        self.assertTrue(self.store.get_game(self.game_id).locked)  # committed
        self.assertIn("roster_locked", self._kinds())
        unlocked = self.api.unlock_roster(self.game_id)
        self.assertNotIn("error", unlocked)
        self.assertFalse(self.store.get_game(self.game_id).locked)
        self.assertIn("roster_unlocked", self._kinds())

    def test_cancel_game_commits_and_notifies_on_sql(self):
        res = self.api.cancel_game(self.game_id)
        self.assertNotIn("error", res)
        self.assertTrue(self.store.get_game(self.game_id).cancelled)  # committed
        self.assertIn("game_cancelled", self._kinds())

    def test_repeated_lock_unlock_never_nests_a_transaction_on_sql(self):
        # The core regression: each call runs its own transaction and the
        # nested-helper crash never occurs, across repeated transitions.
        for _ in range(3):
            self.assertNotIn("error", self.api.lock_roster(self.game_id))
            self.assertNotIn("error", self.api.unlock_roster(self.game_id))
        self.assertFalse(self.store.get_game(self.game_id).locked)


if __name__ == "__main__":
    unittest.main()
