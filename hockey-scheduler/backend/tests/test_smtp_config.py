import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import NotificationChannel, NotificationDelivery, Role
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.services import (
    DryRunEmailTransport,
    SmtpEmailTransport,
    email_config_from_env,
    email_transport_from_env,
)

SMTP_ENV = {
    "EMAIL_MODE": "smtp",
    "SMTP_HOST": "smtp.example.test",
    "SMTP_PORT": "2525",
    "SMTP_USER": "mailer",
    "SMTP_PASSWORD": "s3cret",
    "SMTP_SENDER": "league@club.example",
    "SMTP_USE_TLS": "false",
}


class SmtpConfigTest(unittest.TestCase):
    # -- default stays dry-run --------------------------------------------
    def test_empty_env_is_dry_run(self):
        self.assertEqual(email_transport_from_env({}).mode, "dry_run")

    def test_smtp_mode_without_host_is_dry_run(self):
        # "requires host": mode alone must not enable a live transport.
        t = email_transport_from_env({"EMAIL_MODE": "smtp"})
        self.assertIsInstance(t, DryRunEmailTransport)
        self.assertEqual(t.mode, "dry_run")

    # -- env → config mapping ---------------------------------------------
    def test_config_maps_all_fields(self):
        cfg = email_config_from_env(SMTP_ENV)
        self.assertEqual(cfg["mode"], "smtp")
        self.assertEqual(cfg["host"], "smtp.example.test")
        self.assertEqual(cfg["port"], 2525)
        self.assertEqual(cfg["username"], "mailer")
        self.assertEqual(cfg["password"], "s3cret")
        self.assertEqual(cfg["sender"], "league@club.example")
        self.assertFalse(cfg["use_tls"])

    def test_use_tls_defaults_true(self):
        cfg = email_config_from_env({"EMAIL_MODE": "smtp", "SMTP_HOST": "h"})
        self.assertTrue(cfg["use_tls"])

    # -- fail-safe parsing of malformed optional values -------------------
    def test_invalid_smtp_port_defaults_to_587(self):
        cfg = email_config_from_env({
            "EMAIL_MODE": "smtp", "SMTP_HOST": "smtp.example.test",
            "SMTP_PORT": "abc"})
        self.assertEqual(cfg["port"], 587)

    def test_invalid_port_does_not_crash_even_in_dry_run(self):
        # A bad port with no SMTP mode must not raise during config loading.
        t = email_transport_from_env({"SMTP_PORT": "not-a-number"})
        self.assertEqual(t.mode, "dry_run")

    def test_blank_smtp_host_is_dry_run(self):
        t = email_transport_from_env({"EMAIL_MODE": "smtp", "SMTP_HOST": "   "})
        self.assertEqual(t.mode, "dry_run")

    def test_blank_sender_uses_default(self):
        cfg = email_config_from_env({"SMTP_SENDER": "   "})
        self.assertEqual(cfg["sender"], "no-reply@hockey.invalid")

    def test_smtp_env_builds_configured_transport(self):
        t = email_transport_from_env(SMTP_ENV)
        self.assertIsInstance(t, SmtpEmailTransport)
        self.assertEqual(t.host, "smtp.example.test")
        self.assertEqual(t.port, 2525)
        self.assertEqual(t.username, "mailer")
        self.assertEqual(t.sender, "league@club.example")
        self.assertFalse(t.use_tls)

    # -- smtp mode still rejects placeholder addresses --------------------
    def test_env_smtp_rejects_placeholder_destination(self):
        t = email_transport_from_env(SMTP_ENV)
        d = NotificationDelivery(
            id="d1", notification_id="n1", channel=NotificationChannel.EMAIL,
            destination="public@notify.invalid")
        with self.assertRaises(ValueError):
            t.send(d, None)

    # -- ApiService receives the configured transport + overview shows it -
    def test_apiservice_default_is_dry_run(self):
        store, _, _ = build_full_demo_store()
        ov = ApiService(store).get_delivery_overview(actor_role=Role.LEAGUE_ADMIN)
        self.assertEqual(ov["email_mode"], "dry_run")
        self.assertIsNone(ov["email_sender"])

    def test_apiservice_uses_configured_smtp_transport(self):
        store, _, _ = build_full_demo_store()
        api = ApiService(store, email_transport=email_transport_from_env(SMTP_ENV))
        ov = api.get_delivery_overview(actor_role=Role.LEAGUE_ADMIN)
        self.assertEqual(ov["email_mode"], "smtp")
        self.assertEqual(ov["email_sender"], "league@club.example")


if __name__ == "__main__":
    unittest.main()
