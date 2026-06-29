import unittest

from helpers import make_service, select_and_confirm

from hockey_scheduler.domain import AvailabilityStatus, GameStatus


class RosterStatusTest(unittest.TestCase):
    def test_empty_roster_is_draft(self):
        service, _, game_id = make_service()
        status = service.compute_roster_status(game_id)
        self.assertEqual(status.status, GameStatus.DRAFT)
        self.assertEqual(status.message, "No players selected yet.")

    def test_full_confirmed_roster(self):
        service, _, game_id = make_service(target_goalies=1, target_skaters=4)
        select_and_confirm(
            service, game_id,
            ["player_goalie_1", "player_skater_1", "player_skater_2",
             "player_skater_3", "player_skater_4"],
        )
        status = service.compute_roster_status(game_id)
        self.assertEqual(status.status, GameStatus.ROSTER_CONFIRMED)
        self.assertEqual(status.open_skater_slots, 0)
        self.assertEqual(status.open_goalie_slots, 0)
        self.assertEqual(status.confirmed_skaters, 4)
        self.assertEqual(status.confirmed_goalies, 1)
        self.assertFalse(status.action_required)

    def test_goalie_and_skater_counted_separately(self):
        # Confirm 1 goalie + only 3 of 4 skaters → skater slot open, goalie full.
        service, _, game_id = make_service(target_goalies=1, target_skaters=4)
        select_and_confirm(
            service, game_id,
            ["player_goalie_1", "player_skater_1", "player_skater_2",
             "player_skater_3"],
        )
        status = service.compute_roster_status(game_id)
        self.assertEqual(status.open_goalie_slots, 0)
        self.assertEqual(status.open_skater_slots, 1)
        self.assertEqual(status.status, GameStatus.OPEN_SLOT)

    def test_awaiting_responses_when_selected_but_not_confirmed(self):
        service, _, game_id = make_service(target_goalies=1, target_skaters=2)
        service.select_roster(
            game_id, ["player_goalie_1", "player_skater_1", "player_skater_2"]
        )
        status = service.compute_roster_status(game_id)
        # Slots are occupied by selected players, but nobody confirmed yet.
        self.assertEqual(status.status, GameStatus.AWAITING_RESPONSES)
        self.assertEqual(status.open_skater_slots, 0)

    def test_open_goalie_slot_message(self):
        service, _, game_id = make_service(target_goalies=1, target_skaters=2)
        select_and_confirm(
            service, game_id, ["player_skater_1", "player_skater_2"]
        )
        status = service.compute_roster_status(game_id)
        self.assertEqual(status.open_goalie_slots, 1)
        self.assertIn("goalie slot open", status.message)


if __name__ == "__main__":
    unittest.main()
