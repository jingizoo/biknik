import json
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService


class SetupHierarchyTest(unittest.TestCase):
    """The nested setup tree endpoint (#166 PR C) builds Organization → Venue
    → Rink and League → Season → Level → Division → Team, plus a
    missing_assignments block, from the flat store — counts only, no rosters.
    """

    def setUp(self):
        self.api = ApiService()

    def _competition(self):
        league = self.api.create_league("Over 55")
        season = self.api.create_season(league["id"], "Fall 2026")
        level = self.api.create_level(season["id"], "Level 1", sort_order=1)
        div = self.api.create_division(season["id"], "Div A", level_id=level["id"])
        club = self.api.create_club("Club X")
        team = self.api.create_team(club["id"], div["id"], "Team1")
        # #180: a team nests under a division via its registration, not the
        # legacy Team.division_id.
        self.api.register_team_for_season(season["id"], team["id"], div["id"])
        return league, season, level, div, club, team

    def test_empty_hierarchy_has_all_sections(self):
        h = self.api.get_setup_hierarchy()
        self.assertEqual(h["organizations"], [])
        self.assertEqual(h["leagues"], [])
        for key in ("venues_without_organization", "rinks_without_venue",
                    "divisions_without_level", "teams_without_club",
                    "teams_without_league", "players_without_team"):
            self.assertEqual(h["missing_assignments"][key], [])
        json.dumps(h)  # must round-trip with no custom encoder

    def test_facility_tree_nests_org_venue_rink_with_slot_count(self):
        # #173: venues nest under a league under the owner (Org → League → Venue).
        org = self.api.create_organization("Canlon")
        league = self.api.create_league("Over 55", organization_id=org["id"])
        venue = self.api.create_venue("Plainfield", league_id=league["id"])
        rink = self.api.create_rink(venue["id"], "Rink 1")
        self.api.create_ice_slot(
            rink["id"], "2026-09-01T18:30:00+00:00", "2026-09-01T20:00:00+00:00")
        h = self.api.get_setup_hierarchy()
        o = next(o for o in h["organizations"] if o["id"] == org["id"])
        v = o["leagues"][0]["venues"][0]
        self.assertEqual(v["id"], venue["id"])
        self.assertEqual(v["rinks"][0]["ice_slot_count"], 1)

    def test_competition_tree_nests_level_division_team(self):
        league, season, level, div, club, team = self._competition()
        self.api.create_player(team["id"], "Vince Skater", "forward")
        h = self.api.get_setup_hierarchy()
        lg = next(x for x in h["leagues"] if x["id"] == league["id"])
        se = lg["seasons"][0]
        lv = se["levels"][0]
        self.assertEqual(lv["id"], level["id"])
        team_node = lv["divisions"][0]["teams"][0]
        self.assertEqual(team_node["club_name"], "Club X")
        self.assertEqual(team_node["player_count"], 1)

    def test_hierarchy_carries_no_player_names(self):
        league, season, level, div, club, team = self._competition()
        self.api.create_player(team["id"], "Secret Name", "forward")
        h = self.api.get_setup_hierarchy()
        # Player names are PII — a count is fine, the name must never appear.
        self.assertNotIn("Secret Name", json.dumps(h))

    def test_division_without_level_is_listed_under_season(self):
        league = self.api.create_league("Over 55")
        season = self.api.create_season(league["id"], "Fall 2026")
        self.api.create_level(season["id"], "Level 1")
        loose = self.api.create_division(season["id"], "Senior A")  # no level
        h = self.api.get_setup_hierarchy()
        se = h["leagues"][0]["seasons"][0]
        names = [d["name"] for d in se["divisions_without_level"]]
        self.assertIn("Senior A", names)
        self.assertIn(loose["id"],
                      [d["id"] for d in h["missing_assignments"]["divisions_without_level"]])

    def test_orphan_player_surfaced_by_id_without_name(self):
        # A player whose team is (somehow) absent must still be findable — but
        # by id only; the name stays out of the structural tree.
        league, season, level, div, club, team = self._competition()
        p = self.api.create_player(team["id"], "Orphan Name", "forward")
        # Simulate a dangling team link by pointing the player at a ghost team.
        self.api.store.get_player(p["id"]).team_id = "team_ghost"
        h = self.api.get_setup_hierarchy()
        orphans = h["missing_assignments"]["players_without_team"]
        self.assertIn(p["id"], [o["id"] for o in orphans])
        self.assertNotIn("name", orphans[0])
        self.assertNotIn("Orphan Name", json.dumps(h))

    def test_facility_tree_nests_org_league_venue(self):
        # #173: the facility tree now flows Organization → League → Venue → Rink.
        org = self.api.create_organization("Canlon")
        league = self.api.create_league("Over 55", organization_id=org["id"])
        venue = self.api.create_venue("Plainfield", league_id=league["id"])
        self.api.create_rink(venue["id"], "Rink 1")
        h = self.api.get_setup_hierarchy()
        o = next(o for o in h["organizations"] if o["id"] == org["id"])
        lg = next(x for x in o["leagues"] if x["id"] == league["id"])
        self.assertEqual(lg["venues"][0]["id"], venue["id"])
        self.assertEqual(lg["venues"][0]["rinks"][0]["name"], "Rink 1")

    def test_missing_assignments_has_league_venue_buckets(self):
        # A league with no owner and a venue with no league surface as gaps.
        self.api.create_league("Ownerless League")
        self.api.create_organization("Canlon")
        self.api.create_venue("Unleagued Arena")
        h = self.api.get_setup_hierarchy()
        ma = h["missing_assignments"]
        self.assertIn("Ownerless League",
                      [x["name"] for x in ma["leagues_without_organization"]])
        self.assertIn("Unleagued Arena",
                      [x["name"] for x in ma["venues_without_league"]])
        self.assertIn("venue_owner_mismatches", ma)

    def test_missing_assignments_flags_orphans(self):
        # A venue with no owner and a team with no club surface as orphans.
        self.api.create_venue("Unowned Arena")
        club = self.api.create_club("Club X")
        league = self.api.create_league("Over 55")
        season = self.api.create_season(league["id"], "Fall 2026")
        div = self.api.create_division(season["id"], "Div A")
        self.api.create_team(club["id"], div["id"], "Team1")
        h = self.api.get_setup_hierarchy()
        ma = h["missing_assignments"]
        self.assertEqual([v["name"] for v in ma["venues_without_organization"]],
                         ["Unowned Arena"])
        # The lone division has no level → flagged.
        self.assertEqual([d["name"] for d in ma["divisions_without_level"]], ["Div A"])


if __name__ == "__main__":
    unittest.main()
