import unittest

from helpers import make_service, select_and_confirm

from hockey_scheduler.domain import AvailabilityStatus
from hockey_scheduler.domain.enums import AuditAction, NotificationType


class AuditAndNotificationTest(unittest.TestCase):
    def _full_roster(self, service, game_id):
        select_and_confirm(
            service, game_id,
            ["player_goalie_1", "player_skater_1", "player_skater_2",
             "player_skater_3", "player_skater_4"],
        )

    def test_every_state_change_is_audited(self):
        service, store, game_id = make_service(target_skaters=4)
        self._full_roster(service, game_id)
        service.enroll_substitute(game_id, "player_skater_5", actor_id="p5")
        service.set_availability(
            game_id, "player_skater_1", AvailabilityStatus.UNAVAILABLE
        )
        service.add_substitute_to_roster(game_id, "player_skater_5", actor_id="coach_1")

        actions = {a.action for a in store.audit_for_game(game_id)}
        self.assertIn(AuditAction.ROSTER_SELECTED, actions)
        self.assertIn(AuditAction.SUBSTITUTE_ENROLLED, actions)
        self.assertIn(AuditAction.PLAYER_BACKED_OUT, actions)
        self.assertIn(AuditAction.SUBSTITUTE_ADDED_TO_ROSTER, actions)

    def test_backout_without_sub_emits_slot_open_notification(self):
        service, store, game_id = make_service(target_skaters=4)
        self._full_roster(service, game_id)
        service.set_availability(
            game_id, "player_skater_1", AvailabilityStatus.UNAVAILABLE
        )
        types = [n.type for n in store.notifications_for_game(game_id)]
        self.assertIn(NotificationType.PLAYER_BACKED_OUT, types)
        self.assertIn(NotificationType.SLOT_OPEN, types)

    def test_lock_emits_team_notification(self):
        service, store, game_id = make_service(target_skaters=4)
        self._full_roster(service, game_id)
        service.lock_roster(game_id, actor_id="coach_1")
        locked = [n for n in store.notifications_for_game(game_id)
                  if n.type == NotificationType.ROSTER_LOCKED]
        self.assertEqual(len(locked), 1)
        self.assertEqual(locked[0].audience, "team")

    def test_manual_override_is_audited_with_flag(self):
        service, store, game_id = make_service(target_skaters=4)
        self._full_roster(service, game_id)
        service.enroll_substitute(game_id, "player_skater_5")
        service.set_availability(
            game_id, "player_skater_1", AvailabilityStatus.UNAVAILABLE
        )
        service.add_substitute_to_roster(game_id, "player_skater_5", actor_id="coach_1")
        override = [a for a in store.audit_for_game(game_id)
                    if a.action == AuditAction.SUBSTITUTE_ADDED_TO_ROSTER]
        self.assertTrue(override and override[0].detail.get("override") is True)


if __name__ == "__main__":
    unittest.main()
