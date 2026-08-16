"""#426 review finding 2, the two remaining unaudited-read paths named
explicitly: the delivery worker's contact-destination resolution (attributed
to a real SYSTEM principal, never a silent no-actor default) and the
Player/Official/Team delete dependency itemisation (masked rather than
routed through the full policy+audit boundary — see setup_service.py's
``_dep_group`` call sites for why masking, not gating, is the right fix
there).
"""

import unittest

from helpers import BACKEND  # noqa: F401

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    ACCESS_ALLOWED,
    ContactDestination,
    NotificationAudience,
    NotificationChannel,
    SensitiveFieldCategory,
)
from hockey_scheduler.store import InMemoryStore

SENTINEL_EMAIL = "sentinel-worker-leak-probe@leak-probe.invalid"
C = SensitiveFieldCategory


class DeliveryWorkerAuditTest(unittest.TestCase):
    """enqueue() and the drain worker's re-resolution both read a stored
    ContactDestination to actually deliver — attributed to SYSTEM_PRINCIPAL,
    never a signed-in user (there is none), with its own purpose and
    correlation id (#426 review finding 2)."""

    def setUp(self):
        self.store = InMemoryStore()
        self.api = ApiService(self.store)

    def _rows(self):
        return self.store.list_data_access()

    def test_enqueue_audits_a_real_stored_destination_as_system(self):
        oid = self.api.create_official("Ref One", actor_id="seed")["id"]
        ref = f"official:{oid}"
        self.api.set_contact_destination(ref, "email", SENTINEL_EMAIL)
        from hockey_scheduler.domain import Notification, NotificationKind
        notification = self.store.add_notification(Notification(
            id=self.store.next_id("notif"),
            kind=NotificationKind.ASSIGNMENT_OFFERED,
            audience=NotificationAudience.OFFICIAL, audience_ref=oid,
            title="Assigned", message="You have a new assignment.",
            at=self.api.roster.clock()))
        from hockey_scheduler.services.delivery import enqueue
        before = len(self._rows())
        enqueue(self.store, notification)
        new_rows = [r for r in self._rows()[before:] if r.subject_id == ref]
        self.assertEqual(len(new_rows), 1, self._rows()[before:])
        row = new_rows[0]
        self.assertEqual(row.category, C.CONTACT_DESTINATION)
        self.assertEqual(row.outcome, ACCESS_ALLOWED)
        self.assertEqual(row.actor_role, "system")
        self.assertIsNone(row.actor_user_id)
        self.assertEqual(row.purpose, "delivery_resolve")
        self.assertTrue(row.request_id.startswith("req_"))
        # Never the sentinel value itself, anywhere in the row.
        for value in vars(row).values():
            self.assertNotIn(SENTINEL_EMAIL, str(value))

    def test_worker_reresolution_audits_as_system_once_per_run(self):
        oid = self.api.create_official("Ref Two", actor_id="seed")["id"]
        ref = f"official:{oid}"
        self.api.set_contact_destination(ref, "email", SENTINEL_EMAIL)
        from hockey_scheduler.domain import Notification, NotificationKind
        notification = self.store.add_notification(Notification(
            id=self.store.next_id("notif"),
            kind=NotificationKind.ASSIGNMENT_OFFERED,
            audience=NotificationAudience.OFFICIAL, audience_ref=oid,
            title="Assigned", message="body",
            at=self.api.roster.clock()))
        from hockey_scheduler.services.delivery import enqueue
        enqueue(self.store, notification)

        before = len(self._rows())
        self.api.process_notification_deliveries()
        new_rows = [r for r in self._rows()[before:] if r.subject_id == ref]
        # One row per (recipient, channel) re-resolution this drain run
        # actually performed — every row shares ONE request_id (one worker
        # run = one correlation id, #426 review finding 3's "one request
        # shares one id" generalised to one worker run).
        self.assertGreaterEqual(len(new_rows), 1, self._rows()[before:])
        self.assertEqual(len({r.request_id for r in new_rows}), 1)
        for row in new_rows:
            self.assertEqual(row.actor_role, "system")
            self.assertEqual(row.purpose, "delivery_resolve")

    def test_placeholder_destination_is_not_audited(self):
        # No ContactDestination was ever registered for this recipient — the
        # worker falls back to the synthesized placeholder, which discloses
        # nothing stored, so no CONTACT_DESTINATION row.
        from hockey_scheduler.domain import Notification, NotificationKind
        notification = self.store.add_notification(Notification(
            id=self.store.next_id("notif"),
            kind=NotificationKind.ASSIGNMENT_OFFERED,
            audience=NotificationAudience.OFFICIAL, audience_ref="ghost_official",
            title="t", message="b", at=self.api.roster.clock()))
        from hockey_scheduler.services.delivery import enqueue
        before = len(self._rows())
        enqueue(self.store, notification)
        self.assertEqual(self._rows()[before:], [])


class DependencyItemizationMaskingTest(unittest.TestCase):
    """setup_service.py's delete-dependency itemisation must never echo a
    raw contact destination (#426 review finding 2: an unaudited read path
    outside the policy+audit boundary; masked because the raw value is
    unnecessary there — the caller only needs to know THAT a contact
    destination blocks the delete)."""

    def setUp(self):
        self.store = InMemoryStore()
        self.api = ApiService(self.store)

    def _official_with_contact(self, label=None):
        official = self.api.create_official("Ref One", actor_id="seed")
        self.store.add_contact_destination(ContactDestination(
            id=self.store.next_id("contact"),
            recipient_ref=f"official:{official['id']}",
            channel=NotificationChannel.EMAIL, destination=SENTINEL_EMAIL,
            label=label))
        return official["id"]

    def test_unlabeled_contact_dependency_never_echoes_the_raw_destination(self):
        oid = self._official_with_contact(label=None)
        result = self.api.delete_official(oid, actor_id="ops")
        self.assertEqual(result["error"]["code"], "has_dependencies", result)
        payload_text = str(result)
        self.assertNotIn(SENTINEL_EMAIL, payload_text)
        groups = result["error"]["details"]["dependencies"]
        contact_group = next(
            g for g in groups if g["type"] == "contact destination")
        self.assertEqual(contact_group["count"], 1)
        for item in contact_group["items"]:
            self.assertNotIn(SENTINEL_EMAIL, item["name"])

    def test_labeled_contact_dependency_shows_the_label_never_the_destination(self):
        oid = self._official_with_contact(label="Primary Ref Email")
        result = self.api.delete_official(oid, actor_id="ops")
        groups = result["error"]["details"]["dependencies"]
        contact_group = next(
            g for g in groups if g["type"] == "contact destination")
        self.assertEqual(contact_group["items"][0]["name"],
                         "Primary Ref Email")
        self.assertNotIn(SENTINEL_EMAIL, str(result))


if __name__ == "__main__":
    unittest.main()
