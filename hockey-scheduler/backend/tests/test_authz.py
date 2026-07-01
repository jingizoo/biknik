import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import (
    Permission,
    Role,
    can,
    permissions_for,
    role_from_str,
)
from hockey_scheduler.web.authz import authorize, required_permission


class RolePolicyTest(unittest.TestCase):
    def test_viewer_has_only_view(self):
        self.assertEqual(permissions_for(Role.VIEWER), [Permission.VIEW.value])
        self.assertFalse(can(Role.VIEWER, Permission.MANAGE_ROSTER))

    def test_admin_has_everything(self):
        for perm in Permission:
            self.assertTrue(can(Role.LEAGUE_ADMIN, perm))

    def test_coach_can_manage_roster_not_arena(self):
        self.assertTrue(can(Role.COACH, Permission.MANAGE_ROSTER))
        self.assertTrue(can(Role.COACH, Permission.RESPOND_AVAILABILITY))
        self.assertFalse(can(Role.COACH, Permission.MANAGE_ARENA))
        self.assertFalse(can(Role.COACH, Permission.MANAGE_SCHEDULE))

    def test_arena_manager_can_schedule_not_roster(self):
        self.assertTrue(can(Role.ARENA_MANAGER, Permission.MANAGE_ARENA))
        self.assertTrue(can(Role.ARENA_MANAGER, Permission.MANAGE_SCHEDULE))
        self.assertFalse(can(Role.ARENA_MANAGER, Permission.MANAGE_ROSTER))

    def test_player_can_only_respond(self):
        self.assertTrue(can(Role.PLAYER, Permission.RESPOND_AVAILABILITY))
        self.assertFalse(can(Role.PLAYER, Permission.MANAGE_ROSTER))

    def test_role_from_str_defaults(self):
        self.assertEqual(role_from_str("coach"), Role.COACH)
        self.assertEqual(role_from_str(""), Role.VIEWER)
        self.assertEqual(role_from_str("nonsense"), Role.VIEWER)
        self.assertEqual(role_from_str(None, default=Role.LEAGUE_ADMIN),
                         Role.LEAGUE_ADMIN)


class RequiredPermissionTest(unittest.TestCase):
    def test_setup_paths(self):
        self.assertEqual(required_permission("/api/setup/league"),
                         Permission.MANAGE_SETUP)
        self.assertEqual(required_permission("/api/setup/team"),
                         Permission.MANAGE_SETUP)
        self.assertEqual(required_permission("/api/setup/venue"),
                         Permission.MANAGE_ARENA)
        self.assertEqual(required_permission("/api/setup/ice-slot"),
                         Permission.MANAGE_ARENA)
        self.assertEqual(required_permission("/api/setup/game"),
                         Permission.MANAGE_SCHEDULE)

    def test_game_action_paths(self):
        self.assertEqual(required_permission("/api/games/g1/move"),
                         Permission.MANAGE_SCHEDULE)
        self.assertEqual(required_permission("/api/games/g1/publish"),
                         Permission.MANAGE_SCHEDULE)
        self.assertEqual(required_permission("/api/games/g1/roster/select"),
                         Permission.MANAGE_ROSTER)
        self.assertEqual(required_permission("/api/games/g1/roster/lock"),
                         Permission.MANAGE_ROSTER)
        self.assertEqual(required_permission("/api/games/g1/availability"),
                         Permission.RESPOND_AVAILABILITY)
        self.assertEqual(required_permission("/api/games/g1/substitutes/enroll"),
                         Permission.RESPOND_AVAILABILITY)

    def test_substitute_offer_vs_accept(self):
        self.assertEqual(
            required_permission("/api/games/g1/substitutes/p9/offer"),
            Permission.MANAGE_ROSTER)
        self.assertEqual(
            required_permission("/api/games/g1/substitutes/p9/add-to-roster"),
            Permission.MANAGE_ROSTER)
        self.assertEqual(
            required_permission("/api/games/g1/substitutes/p9/accept"),
            Permission.RESPOND_AVAILABILITY)

    def test_reset_is_unguarded(self):
        self.assertIsNone(required_permission("/api/reset"))


class AuthorizeTest(unittest.TestCase):
    def test_viewer_blocked_from_mutations(self):
        for path in ("/api/setup/league", "/api/games/g1/roster/select",
                     "/api/games/g1/move", "/api/games/g1/availability"):
            self.assertFalse(authorize(Role.VIEWER, path), path)

    def test_coach_scope(self):
        self.assertTrue(authorize(Role.COACH, "/api/games/g1/roster/select"))
        self.assertTrue(authorize(Role.COACH, "/api/games/g1/availability"))
        self.assertFalse(authorize(Role.COACH, "/api/games/g1/move"))
        self.assertFalse(authorize(Role.COACH, "/api/setup/venue"))

    def test_arena_manager_scope(self):
        self.assertTrue(authorize(Role.ARENA_MANAGER, "/api/setup/rink"))
        self.assertTrue(authorize(Role.ARENA_MANAGER, "/api/games/g1/move"))
        self.assertFalse(authorize(Role.ARENA_MANAGER, "/api/games/g1/roster/select"))

    def test_admin_allowed_everywhere(self):
        for path in ("/api/setup/league", "/api/setup/venue", "/api/setup/game",
                     "/api/games/g1/roster/select", "/api/games/g1/move",
                     "/api/games/g1/availability", "/api/reset"):
            self.assertTrue(authorize(Role.LEAGUE_ADMIN, path), path)

    def test_everyone_can_reset(self):
        self.assertTrue(authorize(Role.VIEWER, "/api/reset"))


if __name__ == "__main__":
    unittest.main()
