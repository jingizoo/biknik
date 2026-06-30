import unittest

from helpers import make_service, select_and_confirm

from hockey_scheduler.domain import (
    AvailabilityStatus,
    GameStatus,
    RosterEntryStatus,
    SubstituteStatus,
)
from hockey_scheduler.domain.errors import (
    AlreadySelectedError,
    InvalidTransitionError,
    NotEligibleError,
    RosterLockedError,
    SlotAlreadyFilledError,
)


class SubstituteWorkflowTest(unittest.TestCase):
    def _full_roster(self, service, game_id):
        select_and_confirm(
            service, game_id,
            ["player_goalie_1", "player_skater_1", "player_skater_2",
             "player_skater_3", "player_skater_4"],
        )

    # -- core acceptance criteria -----------------------------------------
    def test_backout_with_substitute_needs_decision(self):
        service, _, game_id = make_service(target_skaters=4)
        self._full_roster(service, game_id)
        service.enroll_substitute(game_id, "player_skater_5")

        service.set_availability(
            game_id, "player_skater_1", AvailabilityStatus.UNAVAILABLE
        )
        status = service.compute_roster_status(game_id)
        self.assertEqual(status.status, GameStatus.NEEDS_SUBSTITUTE)
        self.assertEqual(status.open_skater_slots, 1)
        self.assertTrue(status.action_required)

    def test_backout_without_substitute_open_slot(self):
        service, _, game_id = make_service(target_skaters=4)
        self._full_roster(service, game_id)

        service.set_availability(
            game_id, "player_skater_1", AvailabilityStatus.UNAVAILABLE
        )
        status = service.compute_roster_status(game_id)
        self.assertEqual(status.status, GameStatus.OPEN_SLOT)
        self.assertIn("No substitutes enrolled", status.message)

    def test_coach_adds_substitute_closes_slot(self):
        service, _, game_id = make_service(target_skaters=4)
        self._full_roster(service, game_id)
        service.enroll_substitute(game_id, "player_skater_5")
        service.set_availability(
            game_id, "player_skater_1", AvailabilityStatus.UNAVAILABLE
        )

        entry = service.add_substitute_to_roster(game_id, "player_skater_5",
                                                 actor_id="coach_1")
        self.assertEqual(entry.status, RosterEntryStatus.ACCEPTED)
        status = service.compute_roster_status(game_id)
        self.assertEqual(status.status, GameStatus.ROSTER_CONFIRMED)
        self.assertEqual(status.open_skater_slots, 0)

    def test_offer_accept_flow(self):
        service, _, game_id = make_service(target_skaters=4)
        self._full_roster(service, game_id)
        service.enroll_substitute(game_id, "player_skater_5")
        service.set_availability(
            game_id, "player_skater_1", AvailabilityStatus.UNAVAILABLE
        )

        sub = service.offer_substitute(game_id, "player_skater_5", actor_id="coach_1")
        self.assertEqual(sub.status, SubstituteStatus.OFFERED)
        service.accept_substitute(game_id, "player_skater_5")
        status = service.compute_roster_status(game_id)
        self.assertEqual(status.open_skater_slots, 0)

    # -- position awareness for substitutes -------------------------------
    def test_goalie_open_slot_ignores_skater_substitutes(self):
        service, _, game_id = make_service(target_goalies=1, target_skaters=2)
        select_and_confirm(
            service, game_id,
            ["player_goalie_1", "player_skater_1", "player_skater_2"],
        )
        # A skater enrolls as substitute, then the goalie backs out.
        service.enroll_substitute(game_id, "player_skater_3")
        service.set_availability(
            game_id, "player_goalie_1", AvailabilityStatus.UNAVAILABLE
        )
        status = service.compute_roster_status(game_id)
        # Open slot is a goalie slot; a skater sub does not satisfy it.
        self.assertEqual(status.open_goalie_slots, 1)
        self.assertEqual(status.status, GameStatus.OPEN_SLOT)

    def test_goalie_substitute_satisfies_goalie_slot(self):
        service, _, game_id = make_service(target_goalies=1, target_skaters=2)
        select_and_confirm(
            service, game_id,
            ["player_goalie_1", "player_skater_1", "player_skater_2"],
        )
        service.enroll_substitute(game_id, "player_goalie_2")
        service.set_availability(
            game_id, "player_goalie_1", AvailabilityStatus.UNAVAILABLE
        )
        status = service.compute_roster_status(game_id)
        self.assertEqual(status.status, GameStatus.NEEDS_SUBSTITUTE)

    # -- edge cases -------------------------------------------------------
    def test_two_substitutes_first_accepted_wins(self):
        service, _, game_id = make_service(target_skaters=4)
        self._full_roster(service, game_id)
        service.enroll_substitute(game_id, "player_skater_5")
        service.enroll_substitute(game_id, "player_skater_6")
        service.set_availability(
            game_id, "player_skater_1", AvailabilityStatus.UNAVAILABLE
        )
        service.offer_substitute(game_id, "player_skater_5")
        service.offer_substitute(game_id, "player_skater_6")

        service.accept_substitute(game_id, "player_skater_5")  # fills the slot
        with self.assertRaises(SlotAlreadyFilledError):
            service.accept_substitute(game_id, "player_skater_6")

    def test_selected_player_cannot_enroll_as_substitute(self):
        service, _, game_id = make_service(target_skaters=4)
        self._full_roster(service, game_id)
        with self.assertRaises(AlreadySelectedError):
            service.enroll_substitute(game_id, "player_skater_1")

    def test_player_from_wrong_team_not_eligible(self):
        service, store, game_id = make_service(target_skaters=4)
        from hockey_scheduler.domain import Player, Position
        store.add_player(Player(id="player_other", team_id="team_other",
                                name="Outsider", position=Position.FORWARD))
        with self.assertRaises(NotEligibleError):
            service.enroll_substitute(game_id, "player_other")

    def test_withdraw_substitute(self):
        service, _, game_id = make_service(target_skaters=4)
        self._full_roster(service, game_id)
        service.enroll_substitute(game_id, "player_skater_5")
        sub = service.withdraw_substitute(game_id, "player_skater_5")
        self.assertEqual(sub.status, SubstituteStatus.WITHDRAWN)
        status = service.compute_roster_status(game_id)
        self.assertEqual(status.substitutes_enrolled, 0)

    def test_offer_requires_open_slot(self):
        service, _, game_id = make_service(target_skaters=4)
        self._full_roster(service, game_id)  # no open slots
        service.enroll_substitute(game_id, "player_skater_5")
        with self.assertRaises(SlotAlreadyFilledError):
            service.offer_substitute(game_id, "player_skater_5")

    def test_cannot_change_locked_roster(self):
        service, _, game_id = make_service(target_skaters=4)
        self._full_roster(service, game_id)
        service.lock_roster(game_id, actor_id="coach_1")
        with self.assertRaises(RosterLockedError):
            service.set_roster_entry_status(
                game_id, "player_skater_1", RosterEntryStatus.UNAVAILABLE
            )
        status = service.compute_roster_status(game_id)
        self.assertEqual(status.status, GameStatus.LOCKED)

    def test_accept_without_offer_is_invalid(self):
        service, _, game_id = make_service(target_skaters=4)
        self._full_roster(service, game_id)
        service.enroll_substitute(game_id, "player_skater_5")
        with self.assertRaises(InvalidTransitionError):
            service.accept_substitute(game_id, "player_skater_5")

    def test_locked_roster_blocks_availability_backout(self):
        # A locked roster must not be changed via the availability path either.
        service, _, game_id = make_service(target_skaters=4)
        self._full_roster(service, game_id)
        service.lock_roster(game_id, actor_id="coach_1")
        with self.assertRaises(RosterLockedError):
            service.set_availability(
                game_id, "player_skater_1", AvailabilityStatus.UNAVAILABLE
            )

    def test_backed_out_selected_player_cannot_enroll_as_substitute(self):
        # A previously-selected player who backed out is still "selected" for
        # this game and may not join the not-selected substitute pool.
        service, _, game_id = make_service(target_skaters=4)
        self._full_roster(service, game_id)
        service.set_availability(
            game_id, "player_skater_1", AvailabilityStatus.UNAVAILABLE
        )
        with self.assertRaises(AlreadySelectedError):
            service.enroll_substitute(game_id, "player_skater_1")

    def test_expired_offer_cannot_be_accepted(self):
        service, store, game_id = make_service(target_skaters=4)
        self._full_roster(service, game_id)
        service.enroll_substitute(game_id, "player_skater_5")
        service.set_availability(
            game_id, "player_skater_1", AvailabilityStatus.UNAVAILABLE
        )
        # Capture a timestamp now; the monotonic clock advances past it before
        # accept, so the offer is expired by acceptance time.
        expired_at = service.clock()
        service.offer_substitute(
            game_id, "player_skater_5", offer_expires_at=expired_at
        )
        with self.assertRaises(InvalidTransitionError):
            service.accept_substitute(game_id, "player_skater_5")
        sub = store.substitute_for_player(game_id, "player_skater_5")
        self.assertEqual(sub.status, SubstituteStatus.EXPIRED)


if __name__ == "__main__":
    unittest.main()
