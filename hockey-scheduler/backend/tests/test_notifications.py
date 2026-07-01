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
        self.api.mark_notification_read(nid)
        feed2 = self.api.get_notifications("league_admin", {})
        self.assertEqual(feed2["unread"], feed["unread"] - 1)

    def test_mark_all_read(self):
        res = self.api.mark_all_notifications_read("league_admin", {})
        self.assertGreater(res["marked"], 0)
        self.assertEqual(self.api.get_notifications("league_admin", {})["unread"], 0)

    def test_mark_all_read_is_scoped(self):
        # A viewer marking all read only clears the public ones they can see.
        before_admin = self.api.get_notifications("league_admin", {})["unread"]
        self.api.mark_all_notifications_read("viewer", {})
        after_admin = self.api.get_notifications("league_admin", {})["unread"]
        # Only the single public notification got marked.
        self.assertEqual(after_admin, before_admin - 1)

    def test_mark_unknown_notification_errors(self):
        res = self.api.mark_notification_read("notif_missing")
        self.assertEqual(res["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
