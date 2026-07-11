import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService


class SetupReassignTest(unittest.TestCase):
    """Reassignment endpoints (#166 PR D) move a record under a new parent,
    validating the target and auditing the old→new ids. Nullable links accept
    None to unassign; required links reject an unknown target."""

    def setUp(self):
        self.api = ApiService()
        self.league = self.api.create_league("Over 55")
        self.season = self.api.create_season(self.league["id"], "Fall 2026")
        self.level = self.api.create_level(self.season["id"], "Level 1")
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
        assigned = self.api.assign_division_level(self.div["id"], self.level["id"])
        self.assertEqual(assigned["level_id"], self.level["id"])
        cleared = self.api.assign_division_level(self.div["id"], None)
        self.assertIsNone(cleared["level_id"])

    def test_assign_division_level_from_other_season_rejected(self):
        other = self.api.create_season(self.league["id"], "Spring 2027")
        div_other = self.api.create_division(other["id"], "Div B")
        r = self.api.assign_division_level(div_other["id"], self.level["id"])
        self.assertEqual(r["error"]["code"], "validation_error")

    # -- team → club (nullable) / team → division (required) ---------------
    def test_assign_and_clear_team_club(self):
        assigned = self.api.assign_team_club(self.team["id"], self.club2["id"])
        self.assertEqual(assigned["club_id"], self.club2["id"])
        cleared = self.api.assign_team_club(self.team["id"], None)
        self.assertIsNone(cleared["club_id"])

    def test_assign_team_division_syncs_label(self):
        d2 = self.api.create_division(self.season["id"], "Div Z")
        r = self.api.assign_team_division(self.team["id"], d2["id"])
        self.assertEqual(r["division_id"], d2["id"])
        self.assertEqual(r["division"], "Div Z")

    def test_assign_team_missing_division_not_found(self):
        r = self.api.assign_team_division(self.team["id"], "div_missing")
        self.assertEqual(r["error"]["code"], "not_found")

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


class LeagueVenueReassignTest(unittest.TestCase):
    """Cross-domain league↔facility reassignment (#173 PR B): league owner and
    venue league, with owner agreement, the owner-change guard, and the
    stranded-game safety check."""

    def setUp(self):
        self.api = ApiService()
        self.org = self.api.create_organization("Canlon")
        self.org2 = self.api.create_organization("Rival Owner")
        self.league = self.api.create_league("Over 55", organization_id=self.org["id"])
        self.venue = self.api.create_venue("Plainfield")

    def test_assign_league_organization(self):
        league = self.api.create_league("New League")  # ownerless
        r = self.api.assign_league_organization(league["id"], self.org["id"])
        self.assertEqual(r["organization_id"], self.org["id"])

    def test_assign_venue_league_derives_owner(self):
        r = self.api.assign_venue_league(self.venue["id"], self.league["id"])
        self.assertEqual(r["league_id"], self.league["id"])
        self.assertEqual(r["organization_id"], self.org["id"])  # derived

    def test_assign_venue_to_league_with_conflicting_owner_rejected(self):
        owned = self.api.create_venue("Owned", organization_id=self.org2["id"])
        r = self.api.assign_venue_league(owned["id"], self.league["id"])
        self.assertEqual(r["error"]["code"], "validation_error")

    def test_league_owner_change_with_attached_venue_rejected(self):
        self.api.assign_venue_league(self.venue["id"], self.league["id"])
        r = self.api.assign_league_organization(self.league["id"], self.org2["id"])
        self.assertEqual(r["error"]["code"], "validation_error")

    def test_unassign_venue_league(self):
        self.api.assign_venue_league(self.venue["id"], self.league["id"])
        r = self.api.assign_venue_league(self.venue["id"], None)
        self.assertIsNone(r["league_id"])

    def test_venue_league_reassignment_is_audited_from_to(self):
        self.api.assign_venue_league(self.venue["id"], self.league["id"])
        entry = next(a for a in self.api.store.all_setup_audit()
                     if a.action == "venue_league_assigned")
        self.assertIsNone(entry.detail["from"])
        self.assertEqual(entry.detail["to"], self.league["id"])

    def test_venue_reassignment_refuses_to_strand_scheduled_games(self):
        # A game scheduled on the venue's ice belongs to the league; moving the
        # venue to another league would strand it, so the move is refused.
        season = self.api.create_season(self.league["id"], "Fall 2026")
        div = self.api.create_division(season["id"], "Div A")
        c1 = self.api.create_club("Club A")
        c2 = self.api.create_club("Club B")
        home = self.api.create_team(c1["id"], div["id"], "Team A")
        away = self.api.create_team(c2["id"], div["id"], "Team B")
        self.api.register_team_for_season(season["id"], home["id"], div["id"])
        self.api.register_team_for_season(season["id"], away["id"], div["id"])
        rink = self.api.create_rink(self.venue["id"], "Rink 1")
        slot = self.api.create_ice_slot(
            rink["id"], "2026-09-01T18:30:00+00:00", "2026-09-01T20:00:00+00:00")
        self.api.assign_venue_league(self.venue["id"], self.league["id"])
        self.api.create_game(season["id"], div["id"], home["id"], away["id"], slot["id"])
        other = self.api.create_league("Other League", organization_id=self.org["id"])
        r = self.api.assign_venue_league(self.venue["id"], other["id"])
        self.assertEqual(r["error"]["code"], "validation_error")
        # The venue stayed put.
        self.assertEqual(self.api.store.get_venue(self.venue["id"]).league_id,
                         self.league["id"])


if __name__ == "__main__":
    unittest.main()
