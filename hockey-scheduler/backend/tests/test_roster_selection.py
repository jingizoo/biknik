import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.full_demo import build_full_demo_store


class RosterSelectionTest(unittest.TestCase):
    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store()
        self.api = ApiService(self.store)
        self.next_id = self.ids["next_game_id"]

    def _available(self, gid):
        return [p for p in self.api.get_board(gid)["players"]
                if p["group"] == "available"]

    def _selected(self, gid):
        return [p for p in self.api.get_board(gid)["players"]
                if p["group"] == "selected"]

    def _occupying(self, gid):
        # Players holding a slot: selected group and not backed out.
        return [p for p in self.api.get_board(gid)["players"]
                if p["group"] == "selected" and not p["backed_out"]]

    # -- manual selection --------------------------------------------------
    def test_select_roster_adds_players(self):
        avail = self._available(self.next_id)
        pick = [avail[0]["id"], avail[1]["id"]]
        res = self.api.select_roster(self.next_id, pick, actor_id="coach")
        self.assertNotIn("error", res if isinstance(res, dict) else {})
        self.assertEqual(len(res), 2)
        selected_ids = {p["id"] for p in self._selected(self.next_id)}
        self.assertTrue(set(pick).issubset(selected_ids))

    def test_select_updates_counts(self):
        goalies = [p for p in self._available(self.next_id) if p["slot_type"] == "goalie"]
        self.api.select_roster(self.next_id, [goalies[0]["id"]], actor_id="coach")
        st = self.api.get_roster_status(self.next_id)
        # A goalie is now selected (occupying a slot), so an open goalie slot closes.
        self.assertEqual(st["open_goalie_slots"], st["target_goalies"] - 1)

    def test_remove_player(self):
        avail = self._available(self.next_id)
        pick = avail[0]["id"]
        self.api.select_roster(self.next_id, [pick], actor_id="coach")
        self.assertIn(pick, {p["id"] for p in self._occupying(self.next_id)})
        res = self.api.remove_player(self.next_id, pick, actor_id="coach")
        self.assertNotIn("error", res)
        # No longer occupies a slot; the board marks it removed/backed-out.
        self.assertNotIn(pick, {p["id"] for p in self._occupying(self.next_id)})
        row = next(p for p in self.api.get_board(self.next_id)["players"]
                   if p["id"] == pick)
        self.assertTrue(row["backed_out"])
        self.assertEqual(row["roster_status"], "removed")

    def test_away_team_player_is_eligible(self):
        # #25: the away team's players CAN be selected (their own lineup).
        away = self.store.players_for_team(self.ids["away_team_id"])
        res = self.api.select_roster(self.next_id, [away[0].id], actor_id="coach")
        self.assertNotIn("error", res if isinstance(res, dict) else {})
        self.assertEqual(len(res), 1)

    def test_select_player_from_third_team_rejected(self):
        # A player on neither team in this game is not eligible.
        club = self.api.create_club("Outsiders HC")
        team = self.api.create_team(club["id"], self.ids["division_id"], "Outsiders")
        player = self.api.create_player(team["id"], "Outsider O.", "forward")
        res = self.api.select_roster(self.next_id, [player["id"]], actor_id="coach")
        self.assertEqual(res["error"]["code"], "not_eligible")

    def test_select_on_locked_roster_rejected(self):
        self.api.lock_roster(self.next_id, actor_id="coach")
        avail = self._available(self.next_id)
        res = self.api.select_roster(self.next_id, [avail[0]["id"]], actor_id="coach")
        self.assertEqual(res["error"]["code"], "roster_locked")

    def test_add_back_removed_player_reuses_existing_entry(self):
        # Remove → Add back must revive the row, not create a duplicate.
        pick = self._available(self.next_id)[0]["id"]
        self.api.select_roster(self.next_id, [pick], actor_id="coach")
        before = self.store.roster_for_game(self.next_id)
        self.api.remove_player(self.next_id, pick, actor_id="coach")
        self.api.select_roster(self.next_id, [pick], actor_id="coach")
        after = self.store.roster_for_game(self.next_id)
        player_entries = [e for e in after if e.player_id == pick]
        self.assertEqual(len(player_entries), 1)
        self.assertTrue(player_entries[0].status.occupies_slot)
        self.assertEqual(len(after), len(before))

    def test_reselect_unavailable_player_reuses_entry(self):
        # A backed-out (unavailable) player re-selected does not duplicate rows.
        pick = self._available(self.next_id)[0]["id"]
        self.api.select_roster(self.next_id, [pick], actor_id="coach")
        self.api.set_availability(self.next_id, pick, "unavailable")
        self.api.select_roster(self.next_id, [pick], actor_id="coach")
        entries = [e for e in self.store.roster_for_game(self.next_id)
                   if e.player_id == pick]
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].status.occupies_slot)

    # -- copy previous roster ---------------------------------------------
    def test_copy_previous_roster(self):
        # The future draft game copies the seeded game's confirmed roster.
        res = self.api.copy_previous_roster(self.next_id, actor_id="coach")
        self.assertNotIn("error", res)
        self.assertEqual(res["from_game_id"], self.game_id)
        self.assertGreater(res["copied"], 0)
        self.assertEqual(len(self._selected(self.next_id)), res["copied"])

    def test_copy_previous_roster_none_available(self):
        # The seeded (earliest) game has no prior game to copy from.
        res = self.api.copy_previous_roster(self.game_id, actor_id="coach")
        self.assertEqual(res["error"]["code"], "validation_error")


if __name__ == "__main__":
    unittest.main()
