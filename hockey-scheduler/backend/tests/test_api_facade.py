import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.seed import build_seeded_store


class ApiFacadeTest(unittest.TestCase):
    def setUp(self):
        store, self.game_id = build_seeded_store()
        self.api = ApiService(store)

    def test_get_game_returns_dict(self):
        game = self.api.get_game(self.game_id)
        self.assertEqual(game["id"], self.game_id)
        self.assertEqual(game["target_skaters"], 15)

    def test_not_found_returns_structured_error(self):
        result = self.api.get_roster_status("game_missing")
        self.assertIn("error", result)
        self.assertEqual(result["error"]["code"], "not_found")

    def test_empty_roster_status(self):
        status = self.api.get_roster_status(self.game_id)
        self.assertEqual(status["status"], "draft")
        self.assertEqual(status["message"], "No players selected yet.")

    def test_full_happy_path_through_facade(self):
        selected = ["player_goalie_1"] + [f"player_skater_{i}" for i in range(1, 16)]
        self.api.select_roster(self.game_id, selected, actor_id="coach_1")
        for pid in selected:
            self.api.set_availability(self.game_id, pid, "available")
        status = self.api.get_roster_status(self.game_id)
        self.assertEqual(status["status"], "roster_confirmed")

        # Back out → open slot (no subs).
        self.api.set_availability(self.game_id, "player_skater_5", "unavailable")
        status = self.api.get_roster_status(self.game_id)
        self.assertEqual(status["status"], "open_slot")
        self.assertEqual(status["open_skater_slots"], 1)

        # Enroll + add sub → closed.
        self.api.enroll_substitute(self.game_id, "player_skater_16")
        status = self.api.get_roster_status(self.game_id)
        self.assertEqual(status["status"], "needs_substitute")
        self.api.add_substitute_to_roster(self.game_id, "player_skater_16",
                                          actor_id="coach_1")
        status = self.api.get_roster_status(self.game_id)
        self.assertEqual(status["open_skater_slots"], 0)

    def test_enroll_selected_player_returns_error(self):
        self.api.select_roster(self.game_id, ["player_skater_1"], actor_id="coach_1")
        result = self.api.enroll_substitute(self.game_id, "player_skater_1")
        self.assertEqual(result["error"]["code"], "already_selected")

    def test_invalid_roster_status_returns_structured_error(self):
        self.api.select_roster(self.game_id, ["player_skater_1"], actor_id="coach_1")
        result = self.api.set_roster_status(self.game_id, "player_skater_1", "bad")
        self.assertEqual(result["error"]["code"], "validation_error")

    def test_invalid_availability_status_returns_structured_error(self):
        result = self.api.set_availability(self.game_id, "player_skater_1", "bad")
        self.assertEqual(result["error"]["code"], "validation_error")

    def test_get_game_returns_json_serializable_timestamps(self):
        game = self.api.get_game(self.game_id)
        self.assertIsInstance(game["start_time"], str)
        self.assertTrue(game["start_time"].endswith("+00:00"))

    def test_board_is_fully_json_serializable(self):
        import json
        self.api.select_roster(self.game_id, ["player_skater_1"], actor_id="coach_1")
        self.api.set_availability(self.game_id, "player_skater_1", "available")
        board = self.api.get_board(self.game_id)
        # Must round-trip through json with no custom encoder.
        json.dumps(board)


if __name__ == "__main__":
    unittest.main()
