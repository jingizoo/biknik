"""Dead-letter delivery operations (#80).

When a delivery exhausts its attempt budget it is parked as ``dead_lettered``
instead of silently staying ``failed``. An operator can then retry it (reset
the budget and requeue) or ignore it (never retry). Retry/ignore are
operator-only HTTP actions.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import DeliveryStatus
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.services import DeliveryWorker
from hockey_scheduler.services.delivery import MAX_ATTEMPTS


def _clock():
    return datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


class DeadLetterServiceTest(unittest.TestCase):
    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store()
        self.api = ApiService(self.store)

    def _exhaust(self):
        def boom(delivery, notification):
            raise RuntimeError("smtp down")
        worker = DeliveryWorker(self.store, _clock, sender=boom)
        for _ in range(MAX_ATTEMPTS):
            worker.process_pending()

    def test_delivery_becomes_dead_letter_after_max_retries(self):
        self._exhaust()
        rows = self.store.all_notification_deliveries()
        self.assertTrue(rows)
        for d in rows:
            self.assertEqual(d.status, DeliveryStatus.DEAD_LETTERED)
            self.assertEqual(d.attempts, MAX_ATTEMPTS)
            self.assertEqual(d.dead_lettered_at, _clock())
            self.assertEqual(d.last_attempt_at, _clock())
        # Dead-lettered rows are no longer deliverable.
        self.assertEqual(self.store.pending_deliveries(MAX_ATTEMPTS), [])

    def test_retry_requeues_and_clears_dead_letter_state(self):
        self._exhaust()
        d = self.store.all_notification_deliveries()[0]
        res = self.api.retry_notification_delivery(d.id)
        self.assertEqual(res["status"], "pending")
        self.assertEqual(res["attempts"], 0)
        self.assertIsNone(res["dead_lettered_at"])
        self.assertIsNone(res["last_error"])
        # It is deliverable again and a good sender now sends it.
        self.assertIn(d.id,
                      {x.id for x in self.store.pending_deliveries(MAX_ATTEMPTS)})
        DeliveryWorker(self.store, _clock).process_pending()
        self.assertEqual(self.store.get_notification_delivery(d.id).status,
                         DeliveryStatus.SENT)

    def test_ignored_delivery_is_not_retried(self):
        self._exhaust()
        d = self.store.all_notification_deliveries()[0]
        res = self.api.ignore_notification_delivery(d.id)
        self.assertEqual(res["status"], "ignored")
        # Never deliverable again, even by a fresh worker run.
        DeliveryWorker(self.store, _clock).process_pending()
        self.assertEqual(self.store.get_notification_delivery(d.id).status,
                         DeliveryStatus.IGNORED)

    def test_retry_sent_delivery_is_rejected(self):
        DeliveryWorker(self.store, _clock).process_pending()  # all sent
        d = self.store.all_notification_deliveries()[0]
        self.assertEqual(d.status, DeliveryStatus.SENT)
        res = self.api.retry_notification_delivery(d.id)
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_ignore_sent_delivery_is_rejected(self):
        # Ignoring a completed delivery would rewrite history to "won't
        # deliver"; it must be rejected exactly like retry, leaving it SENT.
        DeliveryWorker(self.store, _clock).process_pending()  # all sent
        d = self.store.all_notification_deliveries()[0]
        self.assertEqual(d.status, DeliveryStatus.SENT)
        res = self.api.ignore_notification_delivery(d.id)
        self.assertEqual(res["error"]["code"], "validation_error")
        self.assertEqual(self.store.get_notification_delivery(d.id).status,
                         DeliveryStatus.SENT)

    def test_retry_and_ignore_unknown_are_not_found(self):
        self.assertEqual(
            self.api.retry_notification_delivery("nope")["error"]["code"],
            "not_found")
        self.assertEqual(
            self.api.ignore_notification_delivery("nope")["error"]["code"],
            "not_found")


class DeadLetterHttpAuthzTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _a_delivery_id(self):
        return next(iter(self.srv.STATE.api.store.all_notification_deliveries())).id

    def test_non_operator_cannot_retry_or_ignore(self):
        did = self._a_delivery_id()
        for who in ("coach", "player", "viewer"):
            c = self._client()
            self._req(c, "POST", "/api/auth/login",
                      {"username": who, "password": "demo"})
            for op in ("retry", "ignore"):
                status, _ = self._req(
                    c, "POST", f"/api/notifications/deliveries/{did}/{op}")
                self.assertEqual(status, 403, f"{who}/{op}")

    def test_operator_can_retry(self):
        did = self._a_delivery_id()
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        status, body = self._req(
            c, "POST", f"/api/notifications/deliveries/{did}/retry")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "pending")


if __name__ == "__main__":
    unittest.main()
