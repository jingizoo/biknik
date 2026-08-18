import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import NotificationChannel, Role
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.services.delivery import resolve_destination


class ContactRegistryTest(unittest.TestCase):
    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store()
        self.api = ApiService(self.store)

    # -- set / list --------------------------------------------------------
    def test_set_and_list_contacts(self):
        oid = self.ids["referee_id"]
        self.api.set_contact_destination(
            "official:" + oid, "email", "ref.stone@contacts.invalid",
            label="Ref Stone")
        self.api.set_contact_destination(
            "scheduler", "email", "ops@contacts.invalid")
        rows = self.api.list_contact_destinations(actor_role=Role.LEAGUE_ADMIN)["contacts"]
        self.assertEqual(len(rows), 2)
        by_ref = {r["recipient_ref"]: r for r in rows}
        self.assertEqual(by_ref["official:" + oid]["destination"],
                         "ref.stone@contacts.invalid")
        self.assertEqual(by_ref["official:" + oid]["label"], "Ref Stone")
        self.assertEqual(by_ref["scheduler"]["destination"],
                         "ops@contacts.invalid")

    def test_set_is_an_upsert(self):
        self.api.set_contact_destination("scheduler", "email", "a@x.invalid")
        self.api.set_contact_destination("scheduler", "email", "b@x.invalid")
        rows = self.api.list_contact_destinations(actor_role=Role.LEAGUE_ADMIN)["contacts"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["destination"], "b@x.invalid")

    # -- resolution --------------------------------------------------------
    def test_resolve_uses_stored_contact_else_placeholder(self):
        ref = "official:" + self.ids["referee_id"]
        self.api.set_contact_destination(ref, "email", "ref@contacts.invalid")
        # Email is stored → used; push has no contact → placeholder.
        self.assertEqual(
            resolve_destination(self.store, ref, NotificationChannel.EMAIL),
            "ref@contacts.invalid")
        self.assertEqual(
            resolve_destination(self.store, ref, NotificationChannel.PUSH),
            "push-token:" + ref.replace(":", "-"))

    def test_push_token_registry(self):
        ref = "official:" + self.ids["referee_id"]
        self.api.set_contact_destination(ref, "push", "apns-placeholder-123")
        self.assertEqual(
            resolve_destination(self.store, ref, NotificationChannel.PUSH),
            "apns-placeholder-123")

    def test_new_emission_uses_stored_contact(self):
        # Register the scheduler inbox, then trigger a scheduler notification;
        # its email delivery must use the stored contact, not the placeholder.
        self.api.set_contact_destination(
            "scheduler", "email", "ops@contacts.invalid")
        self.api.respond_assignment(self.ids["ref_assignment_id"], accept=True)
        notif = next(n for n in self.store.all_notifications_feed()
                     if n.kind.value == "assignment_accepted")
        email = next(d for d in self.store.deliveries_for_notification(notif.id)
                     if d.channel == NotificationChannel.EMAIL)
        self.assertEqual(email.destination, "ops@contacts.invalid")

    def test_absent_contact_falls_back_to_placeholder(self):
        # No contacts registered: every seeded delivery keeps a .invalid target.
        for d in self.store.all_notification_deliveries():
            if d.channel == NotificationChannel.EMAIL:
                self.assertTrue(d.destination.endswith("@notify.invalid"))

    # -- validation --------------------------------------------------------
    def test_unknown_channel_is_rejected(self):
        res = self.api.set_contact_destination("scheduler", "carrier-pigeon",
                                               "x@y.invalid")
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_email_must_look_like_email(self):
        res = self.api.set_contact_destination("scheduler", "email", "not-email")
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_empty_destination_is_rejected(self):
        res = self.api.set_contact_destination("scheduler", "email", "  ")
        self.assertEqual(res["error"]["code"], "validation_error")


if __name__ == "__main__":
    unittest.main()
