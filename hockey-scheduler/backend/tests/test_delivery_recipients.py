import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Notification,
    NotificationAudience,
    NotificationChannel,
    NotificationKind,
    Role,
)
from hockey_scheduler.domain.errors import ValidationError
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.services.delivery import enqueue, recipient_ref


class DeliveryRecipientTest(unittest.TestCase):
    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store()
        self.api = ApiService(self.store)
        # Exercise the remaining two audiences so all four are present:
        # accept → scheduler, a confirmed player backing out → coach open slot.
        self.api.respond_assignment(self.ids["ref_assignment_id"], accept=True)
        self.api.set_availability(
            self.game_id, self.ids["selected_player_id"], "unavailable")

    def _deliveries_for_kind(self, kind):
        notif = next(n for n in self.store.all_notifications_feed()
                     if n.kind.value == kind)
        return self.store.deliveries_for_notification(notif.id)

    def _by_channel(self, deliveries):
        return {d.channel: d for d in deliveries}

    # -- per-audience targeting -------------------------------------------
    def test_official_delivery_targets_the_official(self):
        oid = self.ids["referee_id"]
        ds = self._by_channel(self._deliveries_for_kind("assignment_offered"))
        self.assertEqual(ds[NotificationChannel.EMAIL].recipient_ref,
                         "official:" + oid)
        self.assertEqual(ds[NotificationChannel.EMAIL].destination,
                         "official-" + oid + "@notify.invalid")
        self.assertEqual(ds[NotificationChannel.PUSH].destination,
                         "push-token:official-" + oid)

    def test_scheduler_delivery_targets_scheduler(self):
        ds = self._by_channel(self._deliveries_for_kind("assignment_accepted"))
        self.assertEqual(ds[NotificationChannel.EMAIL].recipient_ref, "scheduler")
        self.assertEqual(ds[NotificationChannel.EMAIL].destination,
                         "scheduler@notify.invalid")
        self.assertEqual(ds[NotificationChannel.PUSH].destination,
                         "push-token:scheduler")

    def test_coach_delivery_targets_the_team(self):
        team = self.ids["home_team_id"]
        ds = self._by_channel(self._deliveries_for_kind("roster_open_slot"))
        self.assertEqual(ds[NotificationChannel.EMAIL].recipient_ref,
                         "team:" + team)
        self.assertEqual(ds[NotificationChannel.EMAIL].destination,
                         "team-" + team + "@notify.invalid")

    def test_public_delivery_targets_public(self):
        ds = self._by_channel(self._deliveries_for_kind("result_approved"))
        self.assertEqual(ds[NotificationChannel.EMAIL].recipient_ref, "public")
        self.assertEqual(ds[NotificationChannel.EMAIL].destination,
                         "public@notify.invalid")
        self.assertEqual(ds[NotificationChannel.PUSH].destination,
                         "push-token:public")

    # -- invariants --------------------------------------------------------
    def test_every_delivery_has_a_recipient_and_destination(self):
        for d in self.store.all_notification_deliveries():
            self.assertTrue(d.recipient_ref, d.id)
            self.assertTrue(d.destination, d.id)
            if d.channel == NotificationChannel.EMAIL:
                self.assertTrue(d.destination.endswith("@notify.invalid"))
            else:
                self.assertTrue(d.destination.startswith("push-token:"))

    def test_overview_row_exposes_recipient_and_destination(self):
        ov = self.api.get_delivery_overview(actor_role=Role.LEAGUE_ADMIN)
        self.assertTrue(all("recipient_ref" in row and "destination" in row
                            for row in ov["deliveries"]))

    # -- fail-closed routing (#60) ----------------------------------------
    def _notif(self, audience, ref):
        return Notification(
            id="n_test", kind=NotificationKind.ASSIGNMENT_OFFERED,
            audience=audience, title="t", message="m",
            at=datetime(2026, 3, 1, tzinfo=timezone.utc), audience_ref=ref)

    def test_official_without_ref_is_rejected(self):
        with self.assertRaises(ValidationError):
            recipient_ref(self._notif(NotificationAudience.OFFICIAL, None))

    def test_coach_without_ref_is_rejected(self):
        with self.assertRaises(ValidationError):
            recipient_ref(self._notif(NotificationAudience.COACH, None))

    def test_scheduler_and_public_need_no_ref(self):
        self.assertEqual(
            recipient_ref(self._notif(NotificationAudience.SCHEDULER, None)),
            "scheduler")
        self.assertEqual(
            recipient_ref(self._notif(NotificationAudience.PUBLIC, None)),
            "public")

    def test_enqueue_rejects_malformed_targeted_notification(self):
        # A malformed targeted notification must not fan out to any queue.
        with self.assertRaises(ValidationError):
            enqueue(self.store, self._notif(NotificationAudience.OFFICIAL, None))
        self.assertEqual(
            self.store.deliveries_for_notification("n_test"), [])


if __name__ == "__main__":
    unittest.main()
