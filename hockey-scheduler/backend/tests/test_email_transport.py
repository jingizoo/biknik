import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    DeliveryStatus,
    NotificationChannel,
    NotificationDelivery,
)
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

    # -- SMTP fail-closed on placeholder addresses ------------------------
    def _email_delivery(self, destination):
        return NotificationDelivery(
            id="d_test", notification_id="n_test",
            channel=NotificationChannel.EMAIL, destination=destination)

    def test_smtp_rejects_placeholder_without_connecting(self):
        connected = []
        transport = SmtpEmailTransport(
            host="mail.example", smtp_factory=lambda: connected.append(1) or FakeSMTP())
        for bad in ("public@notify.invalid", "official-x@notify.invalid", "", "   "):
            with self.assertRaises(ValueError):
                transport.send(self._email_delivery(bad), None)
        self.assertEqual(connected, [])  # never opened a connection

    def test_worker_smtp_marks_placeholder_email_failed(self):
        # With no registered contacts, seeded email deliveries are all .invalid
        # placeholders — live SMTP must refuse them (and never connect).
        fake = FakeSMTP()
        transport = SmtpEmailTransport(
            host="mail.example", smtp_factory=lambda: fake)
        worker = DeliveryWorker(self.store, _clock, email_transport=transport)
        worker.process_pending()
        for d in self._emails():
            self.assertEqual(d.status, DeliveryStatus.FAILED)
            self.assertIn("placeholder", (d.last_error or "").lower())
        self.assertEqual(fake.sent, [])  # nothing was ever sent

    # -- SMTP adapter with a real destination (faked connection) ----------
    def _register_scheduler_email(self, address="ops@club.example"):
        api = ApiService(self.store)
        api.set_contact_destination("scheduler", "email", address)
        api.respond_assignment(self.ids["ref_assignment_id"], accept=True)
        return next(d for d in self._emails() if d.recipient_ref == "scheduler")

    def test_smtp_success_sends_real_destination(self):
        self._register_scheduler_email()
        fake = FakeSMTP()
        transport = SmtpEmailTransport(
            host="mail.example", username="u", password="p",
            smtp_factory=lambda: fake)
        worker = DeliveryWorker(self.store, _clock, email_transport=transport)
        worker.process_pending()
        sched = next(d for d in self._emails() if d.recipient_ref == "scheduler")
        self.assertEqual(sched.status, DeliveryStatus.SENT)
        self.assertTrue(any(m["To"] == "ops@club.example" for m in fake.sent))
        self.assertTrue(fake.started_tls)
        self.assertEqual(fake.logged_in, ("u", "p"))
        self.assertTrue(fake.quit_called)

    def test_smtp_failure_then_retries_to_success(self):
        self._register_scheduler_email()
        state = {"fail": True}
        transport = SmtpEmailTransport(
            host="mail.example", smtp_factory=lambda: FakeSMTP(fail=state["fail"]))
        worker = DeliveryWorker(self.store, _clock, email_transport=transport)

        def sched():
            return next(d for d in self._emails() if d.recipient_ref == "scheduler")

        worker.process_pending()  # real-destination send fails
        self.assertEqual(sched().status, DeliveryStatus.FAILED)
        self.assertEqual(sched().attempts, 1)
        self.assertTrue(sched().last_error)
        state["fail"] = False
        worker.process_pending()  # retried and now succeeds
        self.assertEqual(sched().status, DeliveryStatus.SENT)
        self.assertEqual(sched().attempts, 2)

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
