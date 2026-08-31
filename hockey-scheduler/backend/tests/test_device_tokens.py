import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import NotificationChannel, Role
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.services.delivery import resolve_destination

PUSH = NotificationChannel.PUSH

# list_device_tokens/set_device_token_active now route through the SAME
# policy+audit boundary list_contact_destinations/set_contact_destination_
# active use (#426 round-3 review finding 1) — every call in this file that
# lists/toggles now needs an authorized principal to reach the behavior
# under test (registration/resolution) exactly like it always did; only
# WHO may call it is new, not what a call does once authorized. The
# facade/audit contract itself (gating, denial rows, sanitizers) is pinned
# by test_sensitive_read_audit.py and test_sensitive_read_audit_http.py,
# not re-tested here.
OPS_ROLE = Role.LEAGUE_ADMIN
OPS_USER = "user_ops"


class DeviceTokenTest(unittest.TestCase):
    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store()
        self.api = ApiService(self.store)

    # -- register / validate ----------------------------------------------
    def test_register_and_list(self):
        row = self.api.register_device_token(
            "scheduler", "fcm", "tok-abc", label="Ops phone")
        self.assertTrue(row["active"])
        self.assertEqual(row["provider"], "fcm")
        listed = self.api.list_device_tokens(
            actor_role=OPS_ROLE, actor_user_id=OPS_USER)["device_tokens"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["token"], "tok-abc")

    def test_register_rejects_placeholder_token(self):
        res = self.api.register_device_token(
            "scheduler", "fcm", "push-token:scheduler")
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_register_requires_provider_and_token(self):
        self.assertEqual(
            self.api.register_device_token("scheduler", "", "tok")["error"]["code"],
            "validation_error")
        self.assertEqual(
            self.api.register_device_token("scheduler", "fcm", "  ")["error"]["code"],
            "validation_error")

    def test_register_is_upsert_and_reactivates(self):
        first = self.api.register_device_token("scheduler", "fcm", "tok-1")
        self.api.set_device_token_active(
            first["id"], False, actor_id=OPS_USER, actor_role=OPS_ROLE)
        again = self.api.register_device_token(
            "scheduler", "apns", "tok-1", label="renamed")
        self.assertEqual(again["id"], first["id"])
        self.assertTrue(again["active"])          # reactivated
        self.assertEqual(again["provider"], "apns")
        self.assertEqual(len(self.api.list_device_tokens(
            actor_role=OPS_ROLE, actor_user_id=OPS_USER)["device_tokens"]), 1)

    # -- resolution --------------------------------------------------------
    def test_push_resolves_to_active_token(self):
        self.api.register_device_token("scheduler", "fcm", "tok-real")
        self.assertEqual(
            resolve_destination(self.store, "scheduler", PUSH), "tok-real")

    def test_deactivated_token_falls_back_to_placeholder(self):
        row = self.api.register_device_token("scheduler", "fcm", "tok-real")
        self.api.set_device_token_active(
            row["id"], False, actor_id=OPS_USER, actor_role=OPS_ROLE)
        self.assertEqual(
            resolve_destination(self.store, "scheduler", PUSH),
            "push-token:scheduler")

    def test_token_takes_precedence_over_contact(self):
        # Both a push contact and a device token exist → the token wins.
        self.api.set_contact_destination("scheduler", "push", "contact-token")
        self.api.register_device_token("scheduler", "fcm", "device-token")
        self.assertEqual(
            resolve_destination(self.store, "scheduler", PUSH), "device-token")

    def test_new_emission_uses_active_token(self):
        self.api.register_device_token("scheduler", "fcm", "tok-live")
        self.api.respond_assignment(self.ids["ref_assignment_id"], accept=True)
        notif = next(n for n in self.store.all_notifications_feed()
                     if n.kind.value == "assignment_accepted")
        push = next(d for d in self.store.deliveries_for_notification(notif.id)
                    if d.channel == PUSH)
        self.assertEqual(push.destination, "tok-live")

    def test_set_active_unknown_token_errors(self):
        res = self.api.set_device_token_active(
            "devtok_missing", False, actor_id=OPS_USER, actor_role=OPS_ROLE)
        self.assertEqual(res["error"]["code"], "not_found")

    def test_set_active_refused_for_unauthorized_role(self):
        # #426 round-3 review finding 1: the policy gate runs BEFORE the
        # row lookup, so an unauthorized caller is refused — never told
        # whether the id exists — exactly like set_contact_destination_
        # active's own fail-closed ordering.
        row = self.api.register_device_token("scheduler", "fcm", "tok-guard")
        res = self.api.set_device_token_active(
            row["id"], False, actor_id="user_coach", actor_role=Role.COACH)
        self.assertEqual(res["error"]["code"], "forbidden")
        still = self.api.list_device_tokens(
            actor_role=OPS_ROLE, actor_user_id=OPS_USER)["device_tokens"]
        self.assertTrue(next(t for t in still if t["id"] == row["id"])["active"])

    def test_list_refused_for_unauthorized_role(self):
        self.api.register_device_token("scheduler", "fcm", "tok-guard-2")
        res = self.api.list_device_tokens(actor_role=Role.VIEWER)
        self.assertEqual(res["error"]["code"], "forbidden")
        self.assertNotIn("device_tokens", res)

    # -- overview placeholder flag ----------------------------------------
    def test_overview_flags_placeholder_pushes(self):
        ov = self.api.get_delivery_overview(actor_role=Role.LEAGUE_ADMIN)
        pushes = [d for d in ov["deliveries"] if d["channel"] == "push"]
        self.assertTrue(pushes)
        self.assertTrue(all(d["placeholder"] for d in pushes))  # none registered


if __name__ == "__main__":
    unittest.main()
