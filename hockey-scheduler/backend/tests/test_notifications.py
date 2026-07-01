import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import OfficialRole
from hockey_scheduler.api import ApiService
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.services import SetupService
from hockey_scheduler.store import InMemoryStore


class NotificationsTest(unittest.TestCase):
    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store()
        self.api = ApiService(self.store)
        self.ref_id = self.ids["referee_id"]

    def _kinds(self, role, scope=None):
        return [n["kind"] for n in
                self.api.get_notifications(role, scope or {})["notifications"]]

    # -- emission / audience ----------------------------------------------
    def test_seed_emits_offer_and_result(self):
        # Seed assigns a referee (offer → official) and finalizes a result
        # (public). Admin sees the whole feed.
        kinds = self._kinds("league_admin")
        self.assertIn("assignment_offered", kinds)
        self.assertIn("result_approved", kinds)

    def test_official_sees_own_offer(self):
        kinds = self._kinds("official", {"official_id": self.ref_id})
        self.assertIn("assignment_offered", kinds)
        self.assertIn("result_approved", kinds)  # public

    def test_other_official_does_not_see_offer(self):
        kinds = self._kinds("official", {"official_id": "official_other"})
        self.assertNotIn("assignment_offered", kinds)
        self.assertIn("result_approved", kinds)

    def test_viewer_sees_only_public(self):
        kinds = self._kinds("viewer")
        self.assertEqual(kinds, ["result_approved"])

    def test_accept_notifies_scheduler(self):
        self.api.respond_assignment(self.ids["ref_assignment_id"], accept=True)
        self.assertIn("assignment_accepted", self._kinds("arena_manager"))
        # A viewer must not see the scheduler notification.
        self.assertNotIn("assignment_accepted", self._kinds("viewer"))

    def test_open_slot_notifies_coach(self):
        # A confirmed home player backing out with no subs opens a slot.
        home = self.ids["home_team_id"]
        selected = self.ids["selected_player_id"]
        self.api.set_availability(self.game_id, selected, "unavailable")
        coach_kinds = self._kinds("coach", {"team_id": home})
        self.assertIn("roster_open_slot", coach_kinds)
        # The other team's coach does not see it.
        self.assertNotIn("roster_open_slot",
                         self._kinds("coach", {"team_id": self.ids["away_team_id"]}))

    # -- read state --------------------------------------------------------
    def test_unread_and_mark_read(self):
        feed = self.api.get_notifications("league_admin", {})
        self.assertEqual(feed["unread"], len(feed["notifications"]))
        nid = feed["notifications"][0]["id"]
        self.api.mark_notification_read(nid, "league_admin", {})
        feed2 = self.api.get_notifications("league_admin", {})
        self.assertEqual(feed2["unread"], feed["unread"] - 1)

    def test_viewer_cannot_mark_scheduler_notification_read(self):
        # A scheduler-audience notification is not visible to a viewer, so a
        # viewer must not be able to mark it read even if they know the id.
        self.api.respond_assignment(self.ids["ref_assignment_id"], accept=True)
        admin_feed = self.api.get_notifications("league_admin", {})
        scheduler_notif = next(
            n for n in admin_feed["notifications"]
            if n["kind"] == "assignment_accepted")
        res = self.api.mark_notification_read(
            scheduler_notif["id"], "viewer", {})
        self.assertEqual(res["error"]["code"], "forbidden")
        # It stays unread for the intended recipient.
        arena = self.api.get_notifications("arena_manager", {})
        target = next(n for n in arena["notifications"]
                      if n["id"] == scheduler_notif["id"])
        self.assertFalse(target["read"])

    def test_other_official_cannot_mark_offer_read(self):
        feed = self.api.get_notifications("league_admin", {})
        offer = next(n for n in feed["notifications"]
                     if n["kind"] == "assignment_offered")
        res = self.api.mark_notification_read(
            offer["id"], "official", {"official_id": "official_other"})
        self.assertEqual(res["error"]["code"], "forbidden")

    def test_mark_all_read(self):
        res = self.api.mark_all_notifications_read("league_admin", {})
        self.assertGreater(res["marked"], 0)
        self.assertEqual(self.api.get_notifications("league_admin", {})["unread"], 0)

    def test_mark_all_read_is_scoped(self):
        # A viewer marking all read clears only their own copies; the admin's
        # unread count is untouched (per-recipient read state, #57).
        before_admin = self.api.get_notifications("league_admin", {})["unread"]
        res = self.api.mark_all_notifications_read("viewer", {})
        self.assertEqual(res["marked"], 1)  # the single public notification
        after_admin = self.api.get_notifications("league_admin", {})["unread"]
        self.assertEqual(after_admin, before_admin)
        # And the viewer's own copy is now read.
        self.assertEqual(self.api.get_notifications("viewer", {})["unread"], 0)

    def test_read_state_is_per_recipient(self):
        # The public result is visible to both admin and viewer. Admin reading
        # it must not change the viewer's unread count for that notification.
        admin_feed = self.api.get_notifications("league_admin", {})
        public = next(n for n in admin_feed["notifications"]
                      if n["kind"] == "result_approved")
        viewer_before = self.api.get_notifications("viewer", {})
        self.assertTrue(all(not n["read"] for n in viewer_before["notifications"]))

        self.api.mark_notification_read(public["id"], "league_admin", {})

        # Admin's copy is read; the viewer's copy of the same id is still unread.
        admin_after = self.api.get_notifications("league_admin", {})
        admin_row = next(n for n in admin_after["notifications"]
                         if n["id"] == public["id"])
        self.assertTrue(admin_row["read"])
        viewer_after = self.api.get_notifications("viewer", {})
        viewer_row = next(n for n in viewer_after["notifications"]
                          if n["id"] == public["id"])
        self.assertFalse(viewer_row["read"])
        self.assertEqual(viewer_after["unread"], viewer_before["unread"])

    def test_mark_read_is_idempotent(self):
        feed = self.api.get_notifications("league_admin", {})
        nid = feed["notifications"][0]["id"]
        self.api.mark_notification_read(nid, "league_admin", {})
        # Marking the same notification again does not double-count or error.
        again = self.api.mark_all_notifications_read("league_admin", {})
        self.assertEqual(again["marked"], len(feed["notifications"]) - 1)
        self.assertEqual(
            self.api.get_notifications("league_admin", {})["unread"], 0)

    def test_mark_unknown_notification_errors(self):
        res = self.api.mark_notification_read(
            "notif_missing", "league_admin", {})
        self.assertEqual(res["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
