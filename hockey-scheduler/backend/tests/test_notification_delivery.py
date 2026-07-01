import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import DeliveryStatus, NotificationChannel
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.services import DeliveryWorker
from hockey_scheduler.services.delivery import DEFAULT_CHANNELS, MAX_ATTEMPTS


def _clock():
    return datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


class DeliveryQueueTest(unittest.TestCase):
    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store()
        self.api = ApiService(self.store)

    # -- enqueue on emission ----------------------------------------------
    def test_emission_creates_pending_deliveries_per_channel(self):
        # The seed emits notifications; each fans out to one row per channel,
        # all pending with no attempts yet.
        rows = self.store.all_notification_deliveries()
        self.assertTrue(rows)
        notif_ids = {n.id for n in self.store.all_notifications_feed()}
        for nid in notif_ids:
            channels = {d.channel for d in
                        self.store.deliveries_for_notification(nid)}
            self.assertEqual(channels, set(DEFAULT_CHANNELS))
        self.assertTrue(all(d.status == DeliveryStatus.PENDING and d.attempts == 0
                            for d in rows))

    # -- success -----------------------------------------------------------
    def test_process_marks_all_sent(self):
        worker = DeliveryWorker(self.store, _clock)
        total = len(self.store.all_notification_deliveries())
        res = worker.process_pending()
        self.assertEqual(res["processed"], total)
        self.assertEqual(res["sent"], total)
        self.assertEqual(res["failed"], 0)
        for d in self.store.all_notification_deliveries():
            self.assertEqual(d.status, DeliveryStatus.SENT)
            self.assertEqual(d.attempts, 1)
            self.assertEqual(d.sent_at, _clock())
            self.assertIsNone(d.last_error)
        # Nothing left to do on a second run.
        self.assertEqual(worker.process_pending()["processed"], 0)

    # -- failure -----------------------------------------------------------
    def test_failed_send_records_error_and_stays_failed(self):
        def boom(delivery, notification):
            raise RuntimeError("smtp unreachable")

        worker = DeliveryWorker(self.store, _clock, sender=boom)
        res = worker.process_pending()
        self.assertEqual(res["sent"], 0)
        self.assertEqual(res["failed"], res["processed"])
        for d in self.store.all_notification_deliveries():
            self.assertEqual(d.status, DeliveryStatus.FAILED)
            self.assertEqual(d.attempts, 1)
            self.assertEqual(d.last_error, "smtp unreachable")
            self.assertIsNone(d.sent_at)

    # -- retry -------------------------------------------------------------
    def test_failed_delivery_is_retried_until_it_succeeds(self):
        calls = {"n": 0}

        def flaky(delivery, notification):
            calls["n"] += 1
            if calls["n"] <= 2:  # fail the first two attempts overall
                raise RuntimeError("temporary")

        # Isolate to a single notification's deliveries for a deterministic count.
        nid = next(iter(self.store.all_notifications_feed())).id
        for d in list(self.store.all_notification_deliveries()):
            if d.notification_id != nid:
                self.store.notif_deliveries.pop(d.id)
        deliveries = self.store.deliveries_for_notification(nid)
        self.assertEqual(len(deliveries), len(DEFAULT_CHANNELS))

        worker = DeliveryWorker(self.store, _clock, sender=flaky)
        worker.process_pending()  # both fail (attempts=1)
        self.assertTrue(all(d.status == DeliveryStatus.FAILED
                            for d in self.store.deliveries_for_notification(nid)))
        worker.process_pending()  # one succeeds, one fails (attempts=2)
        worker.process_pending()  # the last one succeeds
        final = self.store.deliveries_for_notification(nid)
        self.assertTrue(all(d.status == DeliveryStatus.SENT for d in final))

    def test_retry_stops_after_max_attempts(self):
        def always_fail(delivery, notification):
            raise RuntimeError("down")

        worker = DeliveryWorker(self.store, _clock, sender=always_fail)
        for _ in range(MAX_ATTEMPTS + 3):
            worker.process_pending()
        for d in self.store.all_notification_deliveries():
            self.assertEqual(d.status, DeliveryStatus.FAILED)
            self.assertEqual(d.attempts, MAX_ATTEMPTS)  # not retried past budget
        # Once exhausted, the queue reports nothing deliverable.
        self.assertEqual(self.store.pending_deliveries(MAX_ATTEMPTS), [])

    # -- facade / overview -------------------------------------------------
    def test_facade_process_and_overview(self):
        total = len(self.store.all_notification_deliveries())
        res = self.api.process_notification_deliveries()
        self.assertEqual(res["sent"], total)
        ov = self.api.get_delivery_overview()
        self.assertEqual(ov["total"], total)
        self.assertEqual(ov["by_status"].get("sent"), total)
        self.assertEqual(ov["by_channel"].get(NotificationChannel.EMAIL.value),
                         total // len(DEFAULT_CHANNELS))


if __name__ == "__main__":
    unittest.main()
