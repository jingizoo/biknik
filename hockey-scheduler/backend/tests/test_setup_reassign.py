import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService


class SetupReassignTest(unittest.TestCase):
    """Reassignment endpoints (#166 PR D) move a record under a new parent,
    validating the target and auditing the old→new ids. Nullable links accept
    None to unassign; required links reject an unknown target."""

    def setUp(self):
        self.api = ApiService()
        self.league = self.api.create_program("Over 55")
        self.season = self.api.create_season(self.league["id"], "Fall 2026")
        self.level = self.api.create_league(self.season["id"], "Level 1")
        self.div = self.api.create_division(self.season["id"], "Div A")
        self.club = self.api.create_club("Club X")
        self.club2 = self.api.create_club("Club Y")
        self.team = self.api.create_team(self.club["id"], self.div["id"], "Team1")
        self.player = self.api.create_player(self.team["id"], "Vince", "forward")
        self.org = self.api.create_organization("Canlon")
        self.venue = self.api.create_venue("Plainfield")
        self.rink = self.api.create_rink(self.venue["id"], "Rink 1")

    # -- venue → organization (nullable) -----------------------------------
    def test_assign_and_clear_venue_organization(self):
        assigned = self.api.assign_venue_organization(self.venue["id"], self.org["id"])
        self.assertEqual(assigned["organization_id"], self.org["id"])
        cleared = self.api.assign_venue_organization(self.venue["id"], None)
        self.assertIsNone(cleared["organization_id"])

    def test_assign_venue_missing_organization_not_found(self):
        r = self.api.assign_venue_organization(self.venue["id"], "org_missing")
        self.assertEqual(r["error"]["code"], "not_found")

    def test_assign_missing_venue_not_found(self):
        r = self.api.assign_venue_organization("venue_missing", self.org["id"])
        self.assertEqual(r["error"]["code"], "not_found")

    # -- rink → venue (required) -------------------------------------------
    def test_assign_rink_venue(self):
        v2 = self.api.create_venue("Gurnee")
        r = self.api.assign_rink_venue(self.rink["id"], v2["id"])
        self.assertEqual(r["venue_id"], v2["id"])

    def test_assign_rink_missing_venue_not_found(self):
        r = self.api.assign_rink_venue(self.rink["id"], "venue_missing")
        self.assertEqual(r["error"]["code"], "not_found")

    # -- division → level (nullable, same-season) --------------------------
    def test_assign_and_clear_division_level(self):
        assigned = self.api.assign_division_league(self.div["id"], self.level["id"])
        self.assertEqual(assigned["league_id"], self.level["id"])
        cleared = self.api.assign_division_league(self.div["id"], None)
        self.assertIsNone(cleared["league_id"])

    def test_assign_division_level_from_other_season_rejected(self):
        other = self.api.create_season(self.league["id"], "Spring 2027")
        div_other = self.api.create_division(other["id"], "Div B")
        r = self.api.assign_division_league(div_other["id"], self.level["id"])
        self.assertEqual(r["error"]["code"], "validation_error")

    # -- team → club (nullable) --------------------------------------------
    def test_assign_and_clear_team_club(self):
        assigned = self.api.assign_team_club(self.team["id"], self.club2["id"])
        self.assertEqual(assigned["club_id"], self.club2["id"])
        cleared = self.api.assign_team_club(self.team["id"], None)
        self.assertIsNone(cleared["club_id"])

    def test_assign_team_division_is_removed(self):
        # #180: the legacy Team→Division reassignment no longer exists; a Team's
        # seasonal division lives in SeasonTeamRegistration.
        self.assertFalse(hasattr(self.api, "assign_team_division"))
        self.assertFalse(hasattr(self.api.setup, "assign_team_division"))

    # -- player → team (required) ------------------------------------------
    def test_assign_player_team(self):
        t2 = self.api.create_team(self.club["id"], self.div["id"], "Team2")
        r = self.api.assign_player_team(self.player["id"], t2["id"])
        self.assertEqual(r["team_id"], t2["id"])

    def test_assign_player_missing_team_not_found(self):
        r = self.api.assign_player_team(self.player["id"], "team_missing")
        self.assertEqual(r["error"]["code"], "not_found")

    # -- audit trail records the old→new move ------------------------------
    def test_reassignment_is_audited_with_from_to(self):
        self.api.assign_venue_organization(self.venue["id"], self.org["id"])
        entry = next(a for a in self.api.store.all_setup_audit()
                     if a.action == "venue_organization_assigned")
        self.assertEqual(entry.entity_id, self.venue["id"])
        self.assertIsNone(entry.detail["from"])
        self.assertEqual(entry.detail["to"], self.org["id"])


class LeagueOwnerReassignTest(unittest.TestCase):
    """League owner reassignment (#173 PR B, decoupled from Venue in #233
    Slice E): the operator organization move is independent of any Venue."""

    def setUp(self):
        self.api = ApiService()
        self.org = self.api.create_organization("Canlon")
        self.org2 = self.api.create_organization("Rival Owner")
        self.league = self.api.create_program("Over 55", operator_organization_id=self.org["id"])

    def test_assign_league_organization(self):
        league = self.api.create_program("New League")  # ownerless
        r = self.api.assign_program_organization(league["id"], self.org["id"])
        self.assertEqual(r["operator_organization_id"], self.org["id"])

    def test_league_owner_change_is_unconstrained_by_venues(self):
        # #233 Slice E: physical (facility) and competition (Program) structure
        # are independent trees, so an operator organization change never
        # depends on any Venue state.
        r = self.api.assign_program_organization(self.league["id"], self.org2["id"])
        self.assertEqual(r["operator_organization_id"], self.org2["id"])


if __name__ == "__main__":
    unittest.main()
