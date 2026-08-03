"""Notification preferences (#81).

A recipient can opt out of a delivery channel; the delivery resolver then
skips it when a notification is enqueued. The in-app feed is unaffected.
Preferences are managed by an operator for anyone, or by a signed-in user for
their own recipient_ref.
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
from hockey_scheduler.domain import (
    NotificationAudience,
    NotificationChannel,
    NotificationKind,
    Notification,
)
from hockey_scheduler.services.delivery import channel_enabled, enqueue
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv

UTC = timezone.utc


def _notif(store, ref="o1"):
    n = Notification(
        id=store.next_id("notif"), kind=NotificationKind.ASSIGNMENT_OFFERED,
        audience=NotificationAudience.OFFICIAL, audience_ref=ref,
        title="Offer", message="You have an assignment offer",
        at=datetime(2026, 1, 1, tzinfo=UTC))
    store.add_notification_feed(n)
    return n


class PreferenceResolverContract:
    def _store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self._store()
        self.api = ApiService(self.store)
        # A real backing Official (#232 review 3): set_notification_preference
        # now rejects structured official:<id> refs whose subject doesn't
        # exist, so these tests need a genuine row rather than a synthetic
        # "o1" id with nothing behind it.
        self.official_id = self.api.create_official("Test Official")["id"]
        self.official_ref = f"official:{self.official_id}"

    def test_channels_default_enabled_when_no_preference(self):
        created = enqueue(self.store, _notif(self.store, ref=self.official_id))
        self.assertEqual(sorted(d.channel.value for d in created),
                         ["email", "push"])

    def test_disabled_email_prevents_email_delivery_but_keeps_push(self):
        self.api.set_notification_preference(self.official_ref, "email", False)
        created = enqueue(self.store, _notif(self.store, ref=self.official_id))
        self.assertEqual([d.channel for d in created],
                         [NotificationChannel.PUSH])
        self.assertFalse(channel_enabled(self.store, self.official_ref,
                                         NotificationChannel.EMAIL))

    def test_get_preferences_fills_defaults(self):
        prefs = self.api.get_notification_preferences("team:5")["preferences"]
        self.assertEqual({p["channel"]: p["enabled"] for p in prefs},
                         {"email": True, "push": True})

    def test_reenabling_restores_delivery(self):
        self.api.set_notification_preference(self.official_ref, "push", False)
        self.api.set_notification_preference(self.official_ref, "push", True)
        created = enqueue(self.store, _notif(self.store, ref=self.official_id))
        self.assertIn(NotificationChannel.PUSH, [d.channel for d in created])

    def test_unknown_channel_rejected(self):
        res = self.api.set_notification_preference("team:5", "carrier-pigeon", False)
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_setting_preference_writes_audit_entry(self):
        # Muting/unmuting a channel is a state change and must be auditable
        # (#81): action, entity, actor, and prior→new value are recorded, with
        # no secret material.
        before = len(self.store.all_setup_audit())
        self.api.set_notification_preference(self.official_ref, "email", False,
                                             actor_id="user_admin")
        entries = self.store.all_setup_audit()
        self.assertEqual(len(entries), before + 1)
        e = entries[-1]
        self.assertEqual(e.action, "notification_preference_set")
        self.assertEqual(e.entity_type, "notification_preference")
        self.assertEqual(e.actor_id, "user_admin")
        self.assertEqual(e.detail["recipient_ref"], self.official_ref)
        self.assertEqual(e.detail["channel"], "email")
        self.assertFalse(e.detail["enabled"])
        self.assertIsNone(e.detail["prior_enabled"])  # first write, no prior

    def test_audit_records_prior_value_on_update(self):
        self.api.set_notification_preference(self.official_ref, "push", False,
                                             actor_id="user_admin")
        self.api.set_notification_preference(self.official_ref, "push", True,
                                             actor_id="user_admin")
        e = self.store.all_setup_audit()[-1]
        self.assertFalse(e.detail["prior_enabled"])  # was disabled
        self.assertTrue(e.detail["enabled"])          # now enabled

    def test_deleted_subject_rejects_recreated_preference(self):
        # (#232 review 3/4) The preference row is retired — never deleted —
        # to clear the way for the Official's delete; the row survives as
        # retired history. Deletion must not leave a back door that lets a
        # later set_notification_preference call re-point a channel at the
        # now-dead recipient_ref.
        self.api.set_notification_preference(self.official_ref, "email", False)
        pref = self.store.get_notification_preference(
            self.official_ref, NotificationChannel.EMAIL)
        self.api.set_notification_preference_active(pref.id, False)
        self.api.delete_official(self.official_id)
        before = len(self.store.all_notification_preferences())
        res = self.api.set_notification_preference(self.official_ref, "email", False)
        self.assertEqual(res["error"]["code"], "validation_error")
        self.assertEqual(res["error"]["details"]["reason"], "scope_subject_missing")
        self.assertEqual(len(self.store.all_notification_preferences()), before)


class MemoryPreferenceTest(PreferenceResolverContract, unittest.TestCase):
    def _store(self):
        return InMemoryStore()


class SqlPreferenceTest(PreferenceResolverContract, unittest.TestCase):
    def _store(self):
        return SqlStore(":memory:")


class PreferenceHttpAccessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        # The demo coach is scoped to the home team.
        cls.home_team = srv.STATE.ids["home_team_id"]

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

    def test_operator_manages_any_recipient(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        # A real backing Official (#232 review 3): set_notification_preference
        # rejects structured official:<id> refs whose subject doesn't exist.
        status, official = self._req(c, "POST", "/api/v2/setup/official",
                                     {"name": "HTTP Test Official"})
        self.assertEqual(status, 200, official)
        ref = f"official:{official['id']}"
        status, body = self._req(c, "POST", "/api/notifications/preferences",
                                 {"recipient_ref": ref,
                                  "channel": "email", "enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(body["enabled"])
        status, got = self._req(
            c, "GET", f"/api/notifications/preferences?recipient_ref={ref}")
        self.assertEqual(status, 200)
        self.assertEqual({p["channel"]: p["enabled"] for p in got["preferences"]},
                         {"email": False, "push": True})

    def test_retiring_contact_and_preference_over_http_unblocks_official_delete(self):
        # #232 review 4: retiring (never deleting) a contact/preference is
        # the supported, audited way to clear a Player/Official delete's
        # dependency over the real HTTP contract — no internal-store access
        # needed anywhere in this test.
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        status, official = self._req(c, "POST", "/api/v2/setup/official",
                                     {"name": "Retire Official"})
        self.assertEqual(status, 200, official)
        ref = f"official:{official['id']}"
        status, contact = self._req(c, "POST", "/api/notifications/contacts",
                                    {"recipient_ref": ref, "channel": "email",
                                     "destination": "retire@example.com"})
        self.assertEqual(status, 200, contact)
        status, pref = self._req(c, "POST", "/api/notifications/preferences",
                                 {"recipient_ref": ref, "channel": "email",
                                  "enabled": False})
        self.assertEqual(status, 200, pref)

        status, blocked = self._req(
            c, "POST", f"/api/v2/setup/official/{official['id']}/delete", {})
        self.assertEqual(status, 409, blocked)
        self.assertEqual(blocked["error"]["code"], "has_dependencies")

        # Retire both dependencies through the real HTTP actions.
        status, retired_contact = self._req(
            c, "POST", f"/api/notifications/contacts/{contact['id']}/active",
            {"active": False})
        self.assertEqual(status, 200, retired_contact)
        self.assertFalse(retired_contact["active"])
        # The set-preference response carries the row id directly (#232
        # review 4 fixed this gap) — no internal-store access needed to
        # discover it.
        status, retired_pref = self._req(
            c, "POST", f"/api/notifications/preferences/{pref['id']}/active",
            {"active": False})
        self.assertEqual(status, 200, retired_pref)
        self.assertFalse(retired_pref["active"])

        status, deleted = self._req(
            c, "POST", f"/api/v2/setup/official/{official['id']}/delete", {})
        self.assertEqual(status, 200, deleted)
        self.assertNotIn("error", deleted)

        # The retired preference remains queryable, unchanged history after
        # the identity itself is gone (#232's no-erase contract) — reading
        # it never required internal-store access either.
        status, got = self._req(
            c, "GET", f"/api/notifications/preferences?recipient_ref={ref}")
        self.assertEqual(status, 200)
        email_pref = next(p for p in got["preferences"] if p["channel"] == "email")
        self.assertEqual(email_pref["id"], pref["id"])
        self.assertFalse(email_pref["active"])
        self.assertFalse(email_pref["enabled"])

        # Re-setting the preference against the now-dead ref is rejected —
        # the identity is genuinely gone, not merely hidden.
        status, resettle = self._req(c, "POST", "/api/notifications/preferences",
                                     {"recipient_ref": ref, "channel": "email",
                                      "enabled": False})
        self.assertEqual(status, 400)
        self.assertEqual(resettle["error"]["code"], "validation_error")

    def test_arena_manager_cannot_retire_or_reactivate_cleanup_rows(self):
        # Arena Manager holds MANAGE_SCHEDULE but not the League-Admin-only
        # MANAGE_SETUP the retire/reactivate routes require — same rationale
        # as the Player/Official delete they serve. Refused with zero
        # mutation in EITHER direction (#232 review 6).
        admin = self._client()
        self._req(admin, "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        status, official = self._req(admin, "POST", "/api/v2/setup/official",
                                     {"name": "Guarded Official"})
        self.assertEqual(status, 200, official)
        ref = f"official:{official['id']}"
        status, contact = self._req(admin, "POST", "/api/notifications/contacts",
                                    {"recipient_ref": ref, "channel": "email",
                                     "destination": "guarded@example.com"})
        self.assertEqual(status, 200, contact)
        status, pref = self._req(admin, "POST", "/api/notifications/preferences",
                                 {"recipient_ref": ref, "channel": "email",
                                  "enabled": False})
        self.assertEqual(status, 200, pref)

        arena = self._client()
        self._req(arena, "POST", "/api/auth/login", {"username": "arena", "password": "demo"})
        status, _ = self._req(
            arena, "POST", f"/api/notifications/contacts/{contact['id']}/active",
            {"active": False})
        self.assertEqual(status, 403)
        status, _ = self._req(
            arena, "POST", f"/api/notifications/preferences/{pref['id']}/active",
            {"active": False})
        self.assertEqual(status, 403)

        status, got = self._req(
            admin, "GET", f"/api/notifications/preferences?recipient_ref={ref}")
        self.assertEqual(status, 200)
        email_pref = next(p for p in got["preferences"] if p["channel"] == "email")
        self.assertTrue(email_pref["active"], "the Arena Manager's refused "
                        "attempt must not have retired the row")

        # Now the admin genuinely retires both — the Arena Manager must also
        # be refused REACTIVATING them.
        status, _ = self._req(
            admin, "POST", f"/api/notifications/contacts/{contact['id']}/active",
            {"active": False})
        self.assertEqual(status, 200)
        status, _ = self._req(
            admin, "POST", f"/api/notifications/preferences/{pref['id']}/active",
            {"active": False})
        self.assertEqual(status, 200)
        status, _ = self._req(
            arena, "POST", f"/api/notifications/contacts/{contact['id']}/active",
            {"active": True})
        self.assertEqual(status, 403)
        status, _ = self._req(
            arena, "POST", f"/api/notifications/preferences/{pref['id']}/active",
            {"active": True})
        self.assertEqual(status, 403)
        status, got = self._req(
            admin, "GET", f"/api/notifications/preferences?recipient_ref={ref}")
        self.assertEqual(status, 200)
        email_pref = next(p for p in got["preferences"] if p["channel"] == "email")
        self.assertFalse(email_pref["active"], "the Arena Manager's refused "
                         "reactivation attempt must not have reactivated the row")

    def test_blocked_official_delete_over_http_preserves_preference(self):
        # The scenario the reviewer flagged directly, exercised over real
        # HTTP: a delete blocked by something else (an active device token)
        # must leave the recipient's stored opt-out completely untouched —
        # not silently reverted to the resolver's enabled default.
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        status, official = self._req(c, "POST", "/api/v2/setup/official",
                                     {"name": "Blocked Official"})
        self.assertEqual(status, 200, official)
        ref = f"official:{official['id']}"
        status, _ = self._req(c, "POST", "/api/notifications/device-tokens",
                              {"recipient_ref": ref, "provider": "fcm",
                               "token": "blocked-tok"})
        self.assertEqual(status, 200)
        status, pref = self._req(c, "POST", "/api/notifications/preferences",
                                 {"recipient_ref": ref, "channel": "email",
                                  "enabled": False})
        self.assertEqual(status, 200, pref)

        status, blocked = self._req(
            c, "POST", f"/api/v2/setup/official/{official['id']}/delete", {})
        self.assertEqual(status, 409)
        self.assertEqual(blocked["error"]["code"], "has_dependencies")

        status, got = self._req(
            c, "GET", f"/api/notifications/preferences?recipient_ref={ref}")
        self.assertEqual(status, 200)
        self.assertFalse(
            next(p["enabled"] for p in got["preferences"] if p["channel"] == "email"),
            "a blocked delete must not revert the recipient's stored opt-out")

    def test_user_manages_own_recipient(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "coach", "password": "demo"})
        own = f"team:{self.home_team}"
        status, body = self._req(c, "POST", "/api/notifications/preferences",
                                 {"recipient_ref": own, "channel": "push",
                                  "enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(body["enabled"])

    def test_user_cannot_manage_another_recipient(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "coach", "password": "demo"})
        status, _ = self._req(c, "POST", "/api/notifications/preferences",
                              {"recipient_ref": "team:some-other-team",
                               "channel": "email", "enabled": False})
        self.assertEqual(status, 403)
        # And cannot read another recipient's prefs either.
        status2, _ = self._req(
            c, "GET", "/api/notifications/preferences?recipient_ref=official:x")
        self.assertEqual(status2, 403)

    def test_player_cannot_manage_team_preferences(self):
        # A player shares the team's delivery channel but does not *speak* for
        # it; letting them toggle team:<id> would mute the whole team (#81).
        # The player has no own delivery target in this slice, so every
        # preference recipient — including their own team — is forbidden.
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "player", "password": "demo"})
        own = f"team:{self.home_team}"
        status, _ = self._req(c, "POST", "/api/notifications/preferences",
                              {"recipient_ref": own, "channel": "email",
                               "enabled": False})
        self.assertEqual(status, 403)
        # The team's channel is untouched — still enabled for the coach.
        status2, got = self._req(
            c, "GET", f"/api/notifications/preferences?recipient_ref={own}")
        self.assertEqual(status2, 403)  # cannot even read the team's prefs


if __name__ == "__main__":
    unittest.main()
