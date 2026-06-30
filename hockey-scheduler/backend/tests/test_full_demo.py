import json
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.full_demo import build_full_demo_store


class FullDemoTest(unittest.TestCase):
    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store()
        self.api = ApiService(self.store)

    def test_seed_builds_full_universe(self):
        ov = self.api.get_demo_overview()
        self.assertEqual(ov["league"]["name"], "Alpine Ice Hockey League")
        self.assertEqual({d["name"] for d in ov["divisions"]},
                         {"U16 Elite", "U18 Development", "Senior A"})
        self.assertEqual({t["name"] for t in ov["teams"]},
                         {"U16 Lions", "U16 Falcons", "U18 Lions", "Senior Lions"})
        self.assertEqual({r["name"] for r in ov["rinks"]},
                         {"Main Rink", "Training Rink"})
        # Exactly one slot is allocated to the game.
        allocated = [s for s in ov["ice_slots"] if s["status"] == "allocated"]
        self.assertEqual(len(allocated), 1)
        self.assertEqual(allocated[0]["game_label"], "U16 Lions vs U16 Falcons")

    def test_opens_on_confirmed_roster(self):
        status = self.api.get_roster_status(self.game_id)
        self.assertEqual(status["status"], "roster_confirmed")

    def test_end_to_end_backout_substitute_lock(self):
        gid, sub = self.game_id, self.ids["substitute_player_id"]
        selected = self.ids["selected_player_id"]

        # 1. A selected skater backs out → Open Slot (no subs yet).
        self.api.set_availability(gid, selected, "unavailable")
        self.assertEqual(self.api.get_roster_status(gid)["status"], "open_slot")

        # 2. A non-selected player enrolls → Needs Substitute Decision.
        self.api.enroll_substitute(gid, sub)
        self.assertEqual(self.api.get_roster_status(gid)["status"], "needs_substitute")

        # 3. Coach adds the substitute → Roster Confirmed again.
        self.api.add_substitute_to_roster(gid, sub, actor_id="coach_lions")
        self.assertEqual(self.api.get_roster_status(gid)["status"], "roster_confirmed")

        # 4. Coach locks the roster.
        self.api.lock_roster(gid, actor_id="coach_lions")
        self.assertEqual(self.api.get_roster_status(gid)["status"], "locked")

    def test_public_overview_has_no_player_pii(self):
        ov = self.api.get_demo_overview()
        # The public fixtures payload must not contain any player names.
        blob = json.dumps(ov["public_fixtures"])
        for player in self.store.players.values():
            self.assertNotIn(player.name, blob)
        # And fixtures expose only safe fixture fields.
        for f in ov["public_fixtures"]:
            self.assertEqual(set(f.keys()), {
                "division_name", "home_team_name", "away_team_name",
                "venue_name", "rink_name", "start_time", "status", "is_junior",
            })

    def test_overview_is_json_serializable(self):
        json.dumps(self.api.get_demo_overview())


if __name__ == "__main__":
    unittest.main()
