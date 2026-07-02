"""Delivery worker loop (#79).

Wraps the existing DeliveryWorker with a controllable loop: run-once for a
bounded batch, start/stop for a daemon loop, disabled by default so nothing
runs implicitly. The underlying processor, retry budget, and dry-run/live
transports are unchanged — the loop only decides when and how many rows to
process.
"""

import threading
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import DeliveryStatus
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.services import (
    DeliveryLoop,
    DeliveryWorker,
    delivery_loop_from_env,
)


def _clock():
    return datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


class DeliveryWorkerLoopTest(unittest.TestCase):
    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store()
        self.worker = DeliveryWorker(self.store, _clock)
        self.total = len(self.store.all_notification_deliveries())
        self.assertTrue(self.total > 0)

    # -- run-once ----------------------------------------------------------
    def test_run_once_processes_pending(self):
        loop = DeliveryLoop(self.worker, batch_size=1000)
        res = loop.run_once()
        self.assertEqual(res["processed"], self.total)
        self.assertEqual(res["sent"], self.total)
        for d in self.store.all_notification_deliveries():
            self.assertEqual(d.status, DeliveryStatus.SENT)

    def test_run_once_respects_batch_size(self):
        loop = DeliveryLoop(self.worker, batch_size=2)
        res = loop.run_once()
        self.assertEqual(res["processed"], 2)
        sent = [d for d in self.store.all_notification_deliveries()
                if d.status == DeliveryStatus.SENT]
        self.assertEqual(len(sent), 2)

    def test_run_once_twice_does_not_resend(self):
        loop = DeliveryLoop(self.worker, batch_size=1000)
        loop.run_once()
        again = loop.run_once()
        self.assertEqual(again["processed"], 0)  # nothing deliverable remains
        for d in self.store.all_notification_deliveries():
            self.assertEqual(d.attempts, 1)  # not re-attempted

    # -- disabled by default -----------------------------------------------
    def test_disabled_worker_start_is_noop(self):
        loop = DeliveryLoop(self.worker, enabled=False)
        self.assertFalse(loop.start())
        self.assertFalse(loop.is_running())
        # Nothing was processed by merely (not) starting.
        self.assertTrue(all(d.status == DeliveryStatus.PENDING
                            for d in self.store.all_notification_deliveries()))

    # -- background lifecycle ----------------------------------------------
    def test_start_runs_loop_then_stop_halts_it(self):
        # A fake worker that signals each batch so the test is deterministic
        # (wait on an event, not a fixed sleep).
        ran = threading.Event()
        calls = {"n": 0}

        class FakeWorker:
            def process_pending(self, limit=None):
                calls["n"] += 1
                ran.set()
                return {"processed": 0, "sent": 0, "failed": 0}

        loop = DeliveryLoop(FakeWorker(), enabled=True, interval_seconds=0.01)
        self.assertTrue(loop.start())
        self.assertTrue(loop.is_running())
        self.assertTrue(ran.wait(2.0), "loop never executed a batch")
        loop.stop()
        self.assertFalse(loop.is_running())
        # Second start after stop works again (clean lifecycle).
        self.assertTrue(loop.start())
        loop.stop()

    def test_double_start_does_not_spawn_second_thread(self):
        loop = DeliveryLoop(self.worker, enabled=True, interval_seconds=5)
        self.assertTrue(loop.start())
        self.assertFalse(loop.start())  # already running
        loop.stop()

    # -- env factory --------------------------------------------------------
    def test_env_factory_defaults_disabled(self):
        loop = delivery_loop_from_env(self.worker, {})
        st = loop.status()
        self.assertFalse(st["enabled"])
        self.assertEqual(st["interval_seconds"], 30)
        self.assertEqual(st["batch_size"], 50)

    def test_env_factory_reads_config(self):
        loop = delivery_loop_from_env(self.worker, {
            "DELIVERY_WORKER_ENABLED": "true",
            "DELIVERY_WORKER_INTERVAL": "7",
            "DELIVERY_WORKER_BATCH": "9"})
        st = loop.status()
        self.assertTrue(st["enabled"])
        self.assertEqual(st["interval_seconds"], 7)
        self.assertEqual(st["batch_size"], 9)

    def test_env_factory_ignores_bad_values(self):
        loop = delivery_loop_from_env(self.worker, {
            "DELIVERY_WORKER_ENABLED": "nope",
            "DELIVERY_WORKER_INTERVAL": "-5",
            "DELIVERY_WORKER_BATCH": "abc"})
        st = loop.status()
        self.assertFalse(st["enabled"])
        self.assertEqual(st["interval_seconds"], 30)
        self.assertEqual(st["batch_size"], 50)

    # -- concurrency: manual drain + loop share one worker -----------------
    def test_concurrent_process_pending_does_not_double_send(self):
        # The manual drain endpoint and the background loop both call
        # process_pending() on the SAME worker. Without a worker-level lock
        # they can read the same pending row before either saves it and send
        # it twice. Reduce the queue to a single pending row, then race two
        # process_pending(limit=1) calls with a blocking sender and prove the
        # row is sent exactly once.
        rows = self.store.all_notification_deliveries()
        one = rows[0]
        for d in rows[1:]:  # leave exactly one row pending
            d.status = DeliveryStatus.SENT
            self.store.save_notification_delivery(d)

        entered = threading.Event()   # sender has been called (lock is held)
        release = threading.Event()   # test lets the blocked send complete
        calls = []

        def blocking_sender(delivery, notification):
            calls.append(delivery.id)
            entered.set()
            release.wait(2.0)

        worker = DeliveryWorker(self.store, _clock, sender=blocking_sender)
        results = {}

        def drain(key):
            results[key] = worker.process_pending(limit=1)

        t_a = threading.Thread(target=drain, args=("a",))
        t_b = threading.Thread(target=drain, args=("b",))
        t_a.start()
        self.assertTrue(entered.wait(2.0), "first sender never ran")
        # A now holds the worker lock inside the blocked send; B must queue on
        # the lock rather than read the same pending row.
        t_b.start()
        release.set()
        t_a.join(3.0)
        t_b.join(3.0)
        self.assertFalse(t_a.is_alive())
        self.assertFalse(t_b.is_alive())

        # The row was handed to the sender exactly once...
        self.assertEqual(calls, [one.id])
        # ...attempted once, and left SENT (not re-attempted by the loser).
        refreshed = self.store.get_notification_delivery(one.id)
        self.assertEqual(refreshed.attempts, 1)
        self.assertEqual(refreshed.status, DeliveryStatus.SENT)
        # The two calls together processed the one row exactly once.
        total_processed = results["a"]["processed"] + results["b"]["processed"]
        self.assertEqual(total_processed, 1)

    # -- transports unchanged ----------------------------------------------
    def test_live_and_dry_run_transports_still_respected(self):
        # A failing sender (simulating an unreachable live transport) still
        # marks rows failed with the retry budget intact — the loop doesn't
        # change delivery semantics.
        def boom(delivery, notification):
            raise RuntimeError("smtp down")

        worker = DeliveryWorker(self.store, _clock, sender=boom)
        loop = DeliveryLoop(worker, batch_size=1000)
        res = loop.run_once()
        self.assertEqual(res["sent"], 0)
        self.assertEqual(res["failed"], res["processed"])
        for d in self.store.all_notification_deliveries():
            self.assertEqual(d.status, DeliveryStatus.FAILED)
            self.assertEqual(d.last_error, "smtp down")


if __name__ == "__main__":
    unittest.main()
