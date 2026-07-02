import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import DeliveryStatus, NotificationChannel
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.services import (
    DeliveryWorker,
    DryRunEmailTransport,
    SmtpEmailTransport,
    email_transport_from_config,
    make_delivery_sender,
)


def _clock():
    return datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)


class FakeSMTP:
    """A stand-in smtplib.SMTP that records calls; optionally fails on send."""

    def __init__(self, fail=False):
        self.fail = fail
        self.started_tls = False
        self.logged_in = None
        self.sent = []
        self.quit_called = False

    def starttls(self):
        self.started_tls = True

    def login(self, user, pwd):
        self.logged_in = (user, pwd)

    def send_message(self, msg):
        if self.fail:
            raise OSError("connection refused")
        self.sent.append(msg)

    def quit(self):
        self.quit_called = True


class EmailTransportTest(unittest.TestCase):
    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store()

    def _emails(self):
        return [d for d in self.store.all_notification_deliveries()
                if d.channel == NotificationChannel.EMAIL]

    # -- dry-run (safe default) -------------------------------------------
    def test_dry_run_records_without_sending(self):
        transport = DryRunEmailTransport()
        worker = DeliveryWorker(self.store, _clock, email_transport=transport)
        res = worker.process_pending()
        self.assertEqual(res["failed"], 0)
        # Every email delivery is sent and captured in the outbox; push is not.
        emails = self._emails()
        self.assertTrue(emails)
        self.assertTrue(all(d.status == DeliveryStatus.SENT for d in emails))
        self.assertEqual(len(transport.outbox), len(emails))
        self.assertTrue(all("@" in m["to"] for m in transport.outbox))
        self.assertTrue(all(m["subject"] for m in transport.outbox))

    def test_default_worker_is_dry_run(self):
        worker = DeliveryWorker(self.store, _clock)
        self.assertEqual(worker.email_transport.mode, "dry_run")

    # -- config: no real send unless explicitly configured ----------------
    def test_config_defaults_to_dry_run(self):
        self.assertEqual(email_transport_from_config().mode, "dry_run")
        self.assertEqual(email_transport_from_config({}).mode, "dry_run")
        # mode smtp but no host → still dry-run (fail-safe).
        self.assertEqual(
            email_transport_from_config({"mode": "smtp"}).mode, "dry_run")

    def test_config_builds_smtp_when_explicit(self):
        t = email_transport_from_config(
            {"mode": "smtp", "host": "smtp.example.invalid", "port": 2525})
        self.assertIsInstance(t, SmtpEmailTransport)
        self.assertEqual(t.host, "smtp.example.invalid")
        self.assertEqual(t.port, 2525)

    # -- SMTP adapter (faked connection) ----------------------------------
    def test_smtp_success_sends_message(self):
        fake = FakeSMTP()
        transport = SmtpEmailTransport(
            host="smtp.example.invalid", username="u", password="p",
            smtp_factory=lambda: fake)
        worker = DeliveryWorker(self.store, _clock, email_transport=transport)
        worker.process_pending()
        emails = self._emails()
        self.assertTrue(all(d.status == DeliveryStatus.SENT for d in emails))
        self.assertEqual(len(fake.sent), len(emails))
        self.assertTrue(fake.started_tls)
        self.assertEqual(fake.logged_in, ("u", "p"))
        self.assertTrue(fake.quit_called)

    def test_smtp_failure_marks_failed_then_retries_to_success(self):
        state = {"fail": True}
        # A fresh fake per connection; flips to healthy after the first round.
        def factory():
            return FakeSMTP(fail=state["fail"])
        transport = SmtpEmailTransport(
            host="smtp.example.invalid", smtp_factory=factory)
        worker = DeliveryWorker(self.store, _clock, email_transport=transport)
        worker.process_pending()  # email sends fail
        emails = self._emails()
        self.assertTrue(all(d.status == DeliveryStatus.FAILED for d in emails))
        self.assertTrue(all(d.last_error for d in emails))
        self.assertTrue(all(d.attempts == 1 for d in emails))
        state["fail"] = False
        worker.process_pending()  # retried and now succeed
        emails = self._emails()
        self.assertTrue(all(d.status == DeliveryStatus.SENT for d in emails))
        self.assertTrue(all(d.attempts == 2 for d in emails))

    # -- routing -----------------------------------------------------------
    def test_only_email_goes_through_transport(self):
        transport = DryRunEmailTransport()
        sender = make_delivery_sender(transport)
        worker = DeliveryWorker(self.store, _clock, sender=sender)
        worker.process_pending()
        # The outbox holds exactly the email deliveries — push never touches it.
        self.assertEqual(len(transport.outbox), len(self._emails()))


if __name__ == "__main__":
    unittest.main()
