"""Regression guards for the #233 Slice C1b competition-model rename.

The rename is entities-only: it must not disturb the tenancy/authz layer, the
RBAC role, session/scope payload shapes, or the v1 API. These tests pin the
invariants the refinement calls out:
  * League Admin's permission set is identical to before the rename.
  * The RBAC role key / label are frozen.
  * An existing scoped user still resolves to (and sees) exactly the same
    records — the tenancy layer keeps its own ``league_id`` vocabulary while the
    canonical id value flowing in is the Program id.
  * Session/scope payload shapes keep the same keys/shape and values (the rename
    adds no scope keys and renames none).
  * Canonical domain dataclasses expose the renamed field (``program_id``) and
    no longer carry the legacy name.
"""

import unittest
from dataclasses import fields
from datetime import datetime, timezone

from helpers import BACKEND, FakeClock  # noqa: F401  (sys.path + clock)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Division, League, Program, Season, SeasonTeamRegistration, Team, Game,
)
from hockey_scheduler.domain.roles import (
    Permission, ROLE_LABELS, ROLE_PERMISSIONS, Role, permissions_for,
)
from hockey_scheduler.domain.errors import ValidationError
from hockey_scheduler.services import SetupService
from hockey_scheduler.services.league_scope import (
    registered_team_ids_in_division,
    require_slot_belongs_to_season,
    slot_belongs_to_season,
)
from hockey_scheduler.store import InMemoryStore


def _at(hour):
    return datetime(2026, 9, 1, hour, tzinfo=timezone.utc)


class LeagueAdminRbacUnchangedTest(unittest.TestCase):
    def test_role_key_and_label_are_frozen(self):
        self.assertEqual(Role.LEAGUE_ADMIN.value, "league_admin")
        self.assertEqual(ROLE_LABELS[Role.LEAGUE_ADMIN], "League Admin")

    def test_league_admin_permission_set_is_identical(self):
        # The exact pre-rename permission set for the role (roles.py).
        expected = {
            Permission.MANAGE_SETUP, Permission.MANAGE_ARENA,
            Permission.MANAGE_SCHEDULE, Permission.MANAGE_ROSTER,
            Permission.RESPOND_AVAILABILITY, Permission.RESPOND_ASSIGNMENT,
            Permission.MANAGE_USERS, Permission.VIEW,
        }
        self.assertEqual(ROLE_PERMISSIONS[Role.LEAGUE_ADMIN], expected)
        self.assertEqual(
            permissions_for(Role.LEAGUE_ADMIN),
            sorted(p.value for p in expected))


class CanonicalDomainFieldsTest(unittest.TestCase):
    def _field_names(self, cls):
        return {f.name for f in fields(cls)}

    def test_renamed_fields_present_and_legacy_names_gone(self):
        prog = self._field_names(Program)
        self.assertIn("operator_organization_id", prog)
        self.assertNotIn("organization_id", prog)

        season = self._field_names(Season)
        self.assertIn("program_id", season)
        self.assertNotIn("league_id", season)

        team = self._field_names(Team)
        self.assertIn("program_id", team)
        self.assertNotIn("league_id", team)

        division = self._field_names(Division)
        self.assertIn("league_id", division)
        self.assertNotIn("level_id", division)

        # New competition-league fields land on registration + game.
        self.assertIn("league_id", self._field_names(SeasonTeamRegistration))
        self.assertIn("league_id", self._field_names(Game))

    def test_grouping_league_is_the_competition_entity(self):
        # The class named League is now the Season-scoped grouping (formerly
        # Level), not the umbrella.
        self.assertIn("season_id", self._field_names(League))
        self.assertIn("sort_order", self._field_names(League))


class ScopedUserSeesSameRecordsTest(unittest.TestCase):
    """A league-scoped read must still resolve the tenancy scope from the
    (now Program) id and return exactly the division's registered teams/games —
    the tenancy layer keeps its ``league_id`` result key unchanged."""

    def setUp(self):
        self.store = InMemoryStore()
        self.setup = SetupService(self.store, clock=FakeClock())
        owner = self.setup.create_organization("Owner")
        self.program = self.setup.create_program(
            "Program", operator_organization_id=owner.id)
        self.season = self.setup.create_season(self.program.id, "Fall")
        self.div = self.setup.create_division(self.season.id, "A")
        club = self.setup.create_club("Club")
        self.t1 = self.setup.create_team(club.id, self.div.id, "T1")
        self.t2 = self.setup.create_team(club.id, self.div.id, "T2")
        self.setup.register_team_for_season(self.season.id, self.t1.id, self.div.id)
        self.setup.register_team_for_season(self.season.id, self.t2.id, self.div.id)
        venue = self.setup.create_venue("Arena", league_id=self.program.id)
        self.setup.grant_season_venue_access(self.season.id, venue.id)
        rink = self.setup.create_rink(venue.id, "Rink")
        self.setup.create_ice_slot(rink.id, _at(19), _at(20))
        self.setup.create_ice_slot(rink.id, _at(21), _at(22))

    def test_scope_resolves_to_program_id_and_returns_registered_teams(self):
        api = ApiService(self.store)
        result = api.draft_season_schedule(self.div.id)
        self.assertNotIn("error", result)
        # Tenancy vocabulary is unchanged: the scope result key is still
        # ``league_id``, and its value is the Program id flowing in at the seam.
        self.assertEqual(result["league_id"], self.program.id)
        self.assertEqual(result["season_id"], self.season.id)
        # The scoped view still sees exactly the division's registered teams.
        self.assertEqual(
            self.setup.registered_team_ids_in_division(self.div.id),
            {self.t1.id, self.t2.id})


class TenancySeamUnchangedTest(unittest.TestCase):
    """The tenancy/scheduling layer resolves scope through the ONE canonical
    bridge (services/scope_bridge.py) after the rename, but keeps its own frozen
    vocabulary. These exercise the ACTUAL scoped authorization decision and
    record-visibility paths — not just a scheduler return key. Ice eligibility
    itself is season-based (#233 Slice E, SeasonVenueAccess) rather than
    Program-based, but the record-visibility seam it sits alongside is
    unchanged."""

    def setUp(self):
        self.store = InMemoryStore()
        self.setup = SetupService(self.store, clock=FakeClock())
        # Two programs under two owners, each with its own ice.
        self.orgA = self.setup.create_organization("Owner A")
        self.orgB = self.setup.create_organization("Owner B")
        self.progA = self.setup.create_program(
            "Program A", operator_organization_id=self.orgA.id)
        self.progB = self.setup.create_program(
            "Program B", operator_organization_id=self.orgB.id)
        self.seasonA = self.setup.create_season(self.progA.id, "Fall A")
        self.divA = self.setup.create_division(self.seasonA.id, "A")
        club = self.setup.create_club("Club")
        self.tA1 = self.setup.create_team(club.id, self.divA.id, "A1")
        self.tA2 = self.setup.create_team(club.id, self.divA.id, "A2")
        self.setup.register_team_for_season(self.seasonA.id, self.tA1.id, self.divA.id)
        self.setup.register_team_for_season(self.seasonA.id, self.tA2.id, self.divA.id)
        venueA = self.setup.create_venue("Arena A", league_id=self.progA.id)
        self.setup.grant_season_venue_access(self.seasonA.id, venueA.id)
        rinkA = self.setup.create_rink(venueA.id, "Rink A")
        self.slotA = self.setup.create_ice_slot(rinkA.id, _at(19), _at(20))
        # Arena B is never granted access to Season A — it is a different
        # Program's venue with no SeasonVenueAccess grant at all.
        venueB = self.setup.create_venue("Arena B", league_id=self.progB.id)
        rinkB = self.setup.create_rink(venueB.id, "Rink B")
        self.slotB = self.setup.create_ice_slot(rinkB.id, _at(19), _at(20))

    def test_authorization_decision_and_error_keys_unchanged(self):
        # A slot whose venue has been granted access is accepted; one that
        # hasn't is rejected.
        self.assertIsNotNone(
            require_slot_belongs_to_season(self.store, self.slotA.id, self.seasonA.id))
        self.assertTrue(
            slot_belongs_to_season(self.store, self.slotA.id, self.seasonA.id))
        self.assertFalse(
            slot_belongs_to_season(self.store, self.slotB.id, self.seasonA.id))
        # The rejection's machine-readable details identify the season/venue.
        with self.assertRaises(ValidationError) as ctx:
            require_slot_belongs_to_season(self.store, self.slotB.id, self.seasonA.id)
        details = ctx.exception.details
        self.assertEqual(details["reason"], "venue_access_missing")
        self.assertEqual(details["season_id"], self.seasonA.id)

    def test_record_visibility_through_scope_is_unchanged(self):
        # A league-scoped read still resolves the scope from the (now Program) id
        # and returns exactly the division's registered teams — no more, no less.
        self.assertEqual(
            registered_team_ids_in_division(self.store, self.divA.id),
            {self.tA1.id, self.tA2.id})


class SessionScopePayloadUnchangedTest(unittest.TestCase):
    """Creating and reading back a scoped account must preserve the exact
    scope-dict shape the rename must not touch."""

    def test_coach_scope_shape_is_unchanged(self):
        api = ApiService(self.store if hasattr(self, "store") else InMemoryStore())
        # A coach account bound to a team — the canonical scoped-user example.
        created = api.create_user_account(
            "coach1", "not-a-real-secret", "coach", scope={"team_id": "team_9"})
        self.assertEqual(created["scope"], {"team_id": "team_9"})
        self.assertEqual(created["role"], "coach")
        # Verify-login resolves the same scope shape (no added/renamed keys).
        verified = api.verify_login("coach1", "not-a-real-secret")
        self.assertEqual(verified["scope"], {"team_id": "team_9"})


if __name__ == "__main__":
    unittest.main()
