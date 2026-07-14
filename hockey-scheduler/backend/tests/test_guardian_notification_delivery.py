"""Guardian delivery fan-out + self-service preferences (#32).

Issue #32 ("notification delivery worker + push device tokens") turned out
to already be almost entirely built under #58/#59/#60/#62/#65/#79/#80/#81 —
the one real gap was audience routing: "guardian receives junior
notifications" was true for the in-app feed (get_guardian_home reuses the
player's own feed scope) but NOT for push/email delivery — a junior's
PLAYER-audience delivery rows only ever reached the player's own
recipient_ref, never a linked guardian's.

This covers the fix: `enqueue()` (services/delivery.py) now fans a
PLAYER-audience notification out to every VERIFIED guardian linked to that
player as well, addressed to `"guardian:<user_id>"` — reusing the existing
generic contact/preference/device-token machinery, no new concepts. A
signed-in guardian can now also self-manage their own channel preferences
(`_own_recipient_ref` gained a GUARDIAN branch) the same way a coach/official
already could — keyed off their own account id, since a guardian session
carries no `scope` at all (#26).

Two layers: the service (fan-out itself, verified-only, opt-out respected)
and the HTTP routes (guardian self-service preferences, no leakage).
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND, FakeClock  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import NotificationChannel, Position
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.store import InMemoryStore
from hockey_scheduler.web import server as srv

UTC = timezone.utc


def _seeded_api():
    """A published game with ONE open skater slot the junior can be offered
    — build_full_demo_store's own game is already fully rostered (15/15),
    so substitute enrollment there always hits slot_already_filled."""
    api = ApiService(InMemoryStore())
    api.roster.clock = FakeClock()
    league = api.setup.create_program("L")
    season = api.setup.create_season(league.id, "S")
    div = api.setup.create_division(season.id, "U16")
    home = api.setup.create_team(api.setup.create_club("LH").id, div.id, "Lions")
    away = api.setup.create_team(api.setup.create_club("FH").id, div.id, "Falcons")
    # Participation comes from the season registration (#180).
    api.setup.register_team_for_season(season.id, home.id, div.id)
    api.setup.register_team_for_season(season.id, away.id, div.id)
    venue = api.setup.create_venue("Arena", league_id=league.id)
    rink = api.setup.create_rink(venue.id, "Rink 1")
    base = datetime(2026, 9, 1, 18, 30, tzinfo=UTC)
    slot = api.setup.create_ice_slot(rink.id, base, base + timedelta(hours=1, minutes=30))
    game = api.setup.create_game(season.id, div.id, home.id, away.id, slot.id,
                                 target_goalies=0, target_skaters=1)
    api.setup.publish_game(game.id)
    junior = api.setup.add_player(home.id, "Junior J.", Position.FORWARD)
    return dict(api=api, store=api.store, season=season, div=div, home=home,
               away=away, rink=rink, game_id=game.id, junior_id=junior.id)


class GuardianDeliveryFanOutTest(unittest.TestCase):
    def setUp(self):
        self.c = _seeded_api()
        self.api = self.c["api"]
        self.store = self.c["store"]
        self.game_id = self.c["game_id"]
        self.junior_id = self.c["junior_id"]

    def _deliveries_for_kind(self, kind):
        notif = next(n for n in self.store.all_notifications_feed()
                    if n.kind.value == kind)
        return self.store.deliveries_for_notification(notif.id)

    def test_verified_guardian_gets_their_own_delivery_rows(self):
        self.api.accounts.create_account("g1", "pw", "guardian", account_id="guard_1")
        link = self.api.guardians.link_guardian("guard_1", self.junior_id)
        self.api.guardians.verify_link(link.id, consent_method="signed_form")

        self.api.enroll_substitute(self.game_id, self.junior_id)
        self.api.offer_substitute(self.game_id, self.junior_id)

        ds = self._deliveries_for_kind("substitute_offered")
        refs = {d.recipient_ref for d in ds}
        self.assertIn("player:" + self.junior_id, refs)
        self.assertIn("guardian:guard_1", refs)

    def test_unverified_guardian_link_gets_nothing(self):
        self.api.accounts.create_account("g2", "pw", "guardian", account_id="guard_2")
        self.api.guardians.link_guardian("guard_2", self.junior_id)  # NOT verified

        self.api.enroll_substitute(self.game_id, self.junior_id)
        self.api.offer_substitute(self.game_id, self.junior_id)

        ds = self._deliveries_for_kind("substitute_offered")
        refs = {d.recipient_ref for d in ds}
        self.assertNotIn("guardian:guard_2", refs)

    def test_a_player_with_no_guardian_link_is_unaffected(self):
        self.api.enroll_substitute(self.game_id, self.junior_id)
        self.api.offer_substitute(self.game_id, self.junior_id)
        ds = self._deliveries_for_kind("substitute_offered")
        refs = {d.recipient_ref for d in ds}
        self.assertEqual({r for r in refs if r.startswith("guardian:")}, set())

    def test_multiple_verified_guardians_all_get_delivery(self):
        self.api.accounts.create_account("g1", "pw", "guardian", account_id="guard_1")
        self.api.accounts.create_account("g2", "pw", "guardian", account_id="guard_2")
        for gid in ("guard_1", "guard_2"):
            link = self.api.guardians.link_guardian(gid, self.junior_id)
            self.api.guardians.verify_link(link.id, consent_method="verbal_confirmed")

        self.api.enroll_substitute(self.game_id, self.junior_id)
        self.api.offer_substitute(self.game_id, self.junior_id)

        ds = self._deliveries_for_kind("substitute_offered")
        refs = {d.recipient_ref for d in ds}
        self.assertIn("guardian:guard_1", refs)
        self.assertIn("guardian:guard_2", refs)

    def test_guardian_channel_opt_out_is_respected(self):
        self.api.accounts.create_account("g1", "pw", "guardian", account_id="guard_1")
        link = self.api.guardians.link_guardian("guard_1", self.junior_id)
        self.api.guardians.verify_link(link.id, consent_method="signed_form")
        self.api.set_notification_preference(
            "guardian:guard_1", "email", False)

        self.api.enroll_substitute(self.game_id, self.junior_id)
        self.api.offer_substitute(self.game_id, self.junior_id)

        ds = self._deliveries_for_kind("substitute_offered")
        guardian_ds = {d.channel: d for d in ds if d.recipient_ref == "guardian:guard_1"}
        self.assertNotIn(NotificationChannel.EMAIL, guardian_ds)
        self.assertIn(NotificationChannel.PUSH, guardian_ds)

    def test_guardian_push_opt_out_leaves_email_untouched(self):
        # Symmetric to the email opt-out above — channels are gated
        # independently, in both directions.
        self.api.accounts.create_account("g1", "pw", "guardian", account_id="guard_1")
        link = self.api.guardians.link_guardian("guard_1", self.junior_id)
        self.api.guardians.verify_link(link.id, consent_method="signed_form")
        self.api.set_notification_preference("guardian:guard_1", "push", False)

        self.api.enroll_substitute(self.game_id, self.junior_id)
        self.api.offer_substitute(self.game_id, self.junior_id)

        ds = self._deliveries_for_kind("substitute_offered")
        guardian_ds = {d.channel: d for d in ds if d.recipient_ref == "guardian:guard_1"}
        self.assertNotIn(NotificationChannel.PUSH, guardian_ds)
        self.assertIn(NotificationChannel.EMAIL, guardian_ds)

    def test_a_coach_audience_notification_is_not_fanned_out_to_guardians(self):
        # roster_open_slot is COACH-audience — guardian fan-out only applies
        # to PLAYER-audience notifications; a coach-facing event about the
        # WHOLE team's roster has no single "the player" to derive a
        # guardian from, and shouldn't try to. The junior below has a
        # verified guardian, but it's a DIFFERENT player's backing-out (on a
        # separate two-skater game, so backing one out leaves a genuine
        # partial "open slot" rather than an empty "draft" roster) that
        # triggers this event.
        self.api.accounts.create_account("g1", "pw", "guardian", account_id="guard_1")
        link = self.api.guardians.link_guardian("guard_1", self.junior_id)
        self.api.guardians.verify_link(link.id, consent_method="signed_form")

        base = datetime(2026, 9, 2, 18, 30, tzinfo=UTC)
        slot2 = self.api.setup.create_ice_slot(
            self.c["rink"].id, base, base + timedelta(hours=1, minutes=30))
        game2 = self.api.setup.create_game(
            self.c["season"].id, self.c["div"].id, self.c["home"].id,
            self.c["away"].id, slot2.id, target_goalies=0, target_skaters=2)
        self.api.setup.publish_game(game2.id)
        p1 = self.api.setup.add_player(self.c["home"].id, "P1", Position.FORWARD)
        p2 = self.api.setup.add_player(self.c["home"].id, "P2", Position.FORWARD)
        self.api.select_roster(game2.id, [p1.id, p2.id])
        self.api.set_availability(game2.id, p1.id, "available")
        self.api.set_availability(game2.id, p2.id, "available")
        self.api.set_availability(game2.id, p1.id, "unavailable")

        ds = self._deliveries_for_kind("roster_open_slot")
        refs = {d.recipient_ref for d in ds}
        self.assertEqual({r for r in refs if r.startswith("guardian:")}, set())


class GuardianPreferenceHttpTest(unittest.TestCase):
    """A signed-in guardian can self-manage their own channel preferences —
    same self-service pattern as coach/official, keyed off their own
    account id since a guardian session carries no scope."""

    @classmethod
    def setUpClass(cls):
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

    def _login(self, username):
        c = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        self._post(c, "/api/auth/login", {"username": username, "password": "demo"})
        return c

    def _post(self, opener, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _get(self, opener, path):
        try:
            with opener.open(f"http://127.0.0.1:{self.port}{path}") as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_guardian_manages_their_own_preference(self):
        admin = self._login("admin")
        _, accts = self._get(admin, "/api/accounts")
        guardian_id = next(a["id"] for a in accts["user_accounts"]
                           if a["username"] == "guardian")
        guardian = self._login("guardian")
        status, body = self._post(
            guardian, "/api/notifications/preferences",
            {"recipient_ref": f"guardian:{guardian_id}",
             "channel": "email", "enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(body["enabled"])

    def test_guardian_cannot_manage_another_recipients_preference(self):
        guardian = self._login("guardian")
        status, _ = self._post(
            guardian, "/api/notifications/preferences",
            {"recipient_ref": "team:some_team", "channel": "email", "enabled": False})
        self.assertEqual(status, 403)

    def test_guardian_cannot_impersonate_a_different_guardian(self):
        guardian = self._login("guardian")
        status, _ = self._post(
            guardian, "/api/notifications/preferences",
            {"recipient_ref": "guardian:someone_else", "channel": "email",
             "enabled": False})
        self.assertEqual(status, 403)

    def test_preference_set_over_http_actually_suppresses_a_real_delivery(self):
        # End-to-end proof, not just the preference-write endpoint in
        # isolation: opting out over the real HTTP route must be honored by
        # a SUBSEQUENT, separately-triggered notification.
        admin = self._login("admin")
        _, accts = self._get(admin, "/api/accounts")
        guardian_id = next(a["id"] for a in accts["user_accounts"]
                           if a["username"] == "guardian")
        guardian = self._login("guardian")
        # Other tests in this class (setUpClass resets the store only once)
        # also touch this same recipient's preferences — force a known
        # starting state (push ON) before disabling email, so this test's
        # own assertions don't depend on execution order.
        self._post(guardian, "/api/notifications/preferences",
                  {"recipient_ref": f"guardian:{guardian_id}",
                   "channel": "push", "enabled": True})
        status, _ = self._post(
            guardian, "/api/notifications/preferences",
            {"recipient_ref": f"guardian:{guardian_id}",
             "channel": "email", "enabled": False})
        self.assertEqual(status, 200)

        # The seeded guardian is linked+verified to selected_player_id, who
        # is already rostered — link a FRESH junior instead so they're
        # eligible for the substitute enroll->offer flow that emits a real
        # PLAYER-audience (substitute_offered) notification.
        home_team = srv.STATE.ids["home_team_id"]
        gid = srv.STATE.ids["next_game_id"]  # published below; unrostered
        self._post(admin, f"/api/games/{gid}/publish", {})
        _, p = self._post(admin, "/api/setup/player", {
            "team_id": home_team, "name": "Delivery Kid", "position": "forward"})
        _, link = self._post(admin, "/api/guardians/links", {
            "guardian_user_id": guardian_id, "player_id": p["id"]})
        self._post(admin, f"/api/guardians/links/{link['id']}/verify",
                  {"consent_method": "signed_form"})

        coach = self._login("coach")
        self._post(coach, f"/api/games/{gid}/substitutes/add-candidate",
                  {"player_id": p["id"]})
        self._post(coach, f"/api/games/{gid}/substitutes/{p['id']}/offer", {})

        notif = next(n for n in srv.STATE.api.store.all_notifications_feed()
                    if n.kind.value == "substitute_offered"
                    and n.audience_ref == p["id"])
        ds = srv.STATE.api.store.deliveries_for_notification(notif.id)
        guardian_ds = {d.channel.value: d for d in ds
                       if d.recipient_ref == f"guardian:{guardian_id}"}
        self.assertNotIn("email", guardian_ds)
        self.assertIn("push", guardian_ds)

    def test_coach_cannot_manage_a_guardian_recipient(self):
        coach = self._login("coach")
        status, _ = self._post(
            coach, "/api/notifications/preferences",
            {"recipient_ref": "guardian:user_guardian", "channel": "email",
             "enabled": False})
        self.assertEqual(status, 403)

    def test_admin_can_still_manage_any_guardian_recipient(self):
        admin = self._login("admin")
        status, body = self._post(
            admin, "/api/notifications/preferences",
            {"recipient_ref": "guardian:user_guardian", "channel": "push",
             "enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(body["enabled"])


if __name__ == "__main__":
    unittest.main()
