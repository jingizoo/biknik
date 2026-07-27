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
        league = self.api.create_program("Over 55")
        season = self.api.create_season(league["id"], "Fall 2026")
        level = self.api.create_league(season["id"], "Level 1", sort_order=1)
        div = self.api.create_division(season["id"], "Div A", league_id=level["id"])
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
        league = self.api.create_program("Over 55", operator_organization_id=org["id"])
        venue = self.api.create_venue("Plainfield", league_id=league["id"])
        rink = self.api.create_rink(venue["id"], "Rink 1")
        self.api.create_ice_slot(
            rink["id"], "2026-09-01T18:30:00+00:00", "2026-09-01T20:00:00+00:00")
        h = self.api.get_setup_hierarchy()
        o = next(o for o in h["organizations"] if o["id"] == org["id"])
        v = o["leagues"][0]["venues"][0]
        self.assertEqual(v["id"], venue["id"])
        self.assertEqual(v["rinks"][0]["ice_slot_count"], 1)

    def test_v1_level_node_key_set_frozen(self):
        # #233 freezes the v1 hierarchy shape: the nested level node exposes ONLY
        # its legacy UI keys. The #159 unbind's league_season_id must never leak
        # into v1 (it lives on the v2 hierarchy only).
        league, season, level, div, club, team = self._competition()
        h = self.api.get_setup_hierarchy()
        lg = next(x for x in h["leagues"] if x["id"] == league["id"])
        lv = lg["seasons"][0]["levels"][0]
        self.assertEqual(set(lv), {"id", "name", "sort_order", "divisions"}, lv)
        self.assertNotIn("league_season_id", lv)
        self.assertNotIn("league_season_id", json.dumps(h))

    def test_v2_league_node_exposes_binding_id(self):
        # The v2 hierarchy DOES expose the LeagueSeason binding id, so the UI can
        # drive the explicit unbind (delete_league_season) before deleting a
        # League (#159).
        league, season, level, div, club, team = self._competition()
        binding = self.api.store.league_season_for(level["id"], season["id"])
        self.assertIsNotNone(binding)
        h2 = self.api.get_setup_hierarchy_v2()
        prog = next(p for p in h2["programs"] if p["id"] == league["id"])
        lv = prog["seasons"][0]["leagues"][0]
        self.assertEqual(lv["id"], level["id"])
        self.assertEqual(lv["league_season_id"], binding.id)

    def test_v2_team_node_carries_its_registration_id(self):
        # #331 review round 19: a v2 hierarchy team node names the EXACT
        # registration it represents, not just its team_id -- required so a
        # consumer with two active registrations to distinguish (a Rule 7
        # violation legacy data can leave behind) never has to reconstruct
        # identity from a lossy (team_id, league_id) pair.
        league, season, level, div, club, team = self._competition()
        reg = self.api.store.registration_for_team_in_league_season(
            self.api.store.league_season_for(level["id"], season["id"]).id,
            team["id"])
        h2 = self.api.get_setup_hierarchy_v2()
        prog = next(p for p in h2["programs"] if p["id"] == league["id"])
        division_team = prog["seasons"][0]["leagues"][0]["divisions"][0]["teams"][0]
        self.assertEqual(division_team["registration_id"], reg.id)
        # The PERMANENT Program->League->Team tree has no Season/registration
        # of its own -- its team nodes carry registration_id=None, never a
        # stale/misleading id borrowed from wherever the team happens to be
        # registered this Season.
        perm_team = prog["leagues"][0]["teams"][0]
        self.assertEqual(perm_team["id"], team["id"])
        self.assertIsNone(perm_team["registration_id"])

    def test_v2_two_active_registrations_carry_distinct_registration_ids(self):
        # #331 review round 19 finding 4: a Team with two simultaneously
        # active registrations in one Season across two Leagues (legacy data/
        # a write path predating Rule 7 -- register_team_for_season/
        # assign_season_team_league/transfer_team_to_league all now refuse to
        # create this) previously produced two structurally-IDENTICAL
        # division-less team nodes; a consumer had no way to tell them apart.
        league = self.api.create_program("Prog")
        season = self.api.create_season(league["id"], "Season")
        league_a = self.api.create_league(season["id"], "League A")
        league_b = self.api.create_league(season["id"], "League B")
        club = self.api.create_club("Club")
        team = self.api.create_team(club["id"], None, "Team",
                                    league_id=league_a["id"])
        reg_a = self.api.register_team_for_season(
            season["id"], team["id"], actor_id="admin",
            league_id=league_a["id"])
        # Planted directly -- no current write path can leave a second active
        # row under a different League behind for a Team with a permanent
        # League already set (the exact shape league_scope.py's own
        # team_registration_valid docstring names).
        from hockey_scheduler.domain import SeasonTeamRegistration
        ls_b = self.api.store.league_season_for(league_b["id"], season["id"])
        reg_b = SeasonTeamRegistration(
            id=self.api.store.next_id("streg"), league_season_id=ls_b.id,
            team_id=team["id"], division_id=None, active=True)
        self.api.store.add_season_team_registration(reg_b)

        h2 = self.api.get_setup_hierarchy_v2()
        prog = next(p for p in h2["programs"] if p["id"] == league["id"])
        season_node = prog["seasons"][0]
        nodes_by_league = {
            lv["id"]: lv["teams_without_division"] for lv in season_node["leagues"]}
        ids_a = [t["registration_id"] for t in nodes_by_league[league_a["id"]]]
        ids_b = [t["registration_id"] for t in nodes_by_league[league_b["id"]]]
        self.assertEqual(ids_a, [reg_a["id"]])
        self.assertEqual(ids_b, [reg_b.id])
        self.assertNotEqual(reg_a["id"], reg_b.id)

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
        league = self.api.create_program("Over 55")
        season = self.api.create_season(league["id"], "Fall 2026")
        self.api.create_league(season["id"], "Level 1")
        loose = self.api.create_division(season["id"], "Senior A")  # no explicit level
        h = self.api.get_setup_hierarchy()
        se = h["leagues"][0]["seasons"][0]
        # #283: a division created without an explicit league is no longer
        # level-less — it resolves to the season's sole League, so it is listed
        # under that level and never surfaces as a divisions_without_level gap.
        lv = se["levels"][0]
        self.assertIn("Senior A", [d["name"] for d in lv["divisions"]])
        self.assertNotIn(loose["id"],
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
        league = self.api.create_program("Over 55", operator_organization_id=org["id"])
        venue = self.api.create_venue("Plainfield", league_id=league["id"])
        self.api.create_rink(venue["id"], "Rink 1")
        h = self.api.get_setup_hierarchy()
        o = next(o for o in h["organizations"] if o["id"] == org["id"])
        lg = next(x for x in o["leagues"] if x["id"] == league["id"])
        self.assertEqual(lg["venues"][0]["id"], venue["id"])
        self.assertEqual(lg["venues"][0]["rinks"][0]["name"], "Rink 1")

    def test_missing_assignments_has_league_venue_buckets(self):
        # A league with no owner and a venue with no league surface as gaps.
        self.api.create_program("Ownerless League")
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
        league = self.api.create_program("Over 55")
        season = self.api.create_season(league["id"], "Fall 2026")
        div = self.api.create_division(season["id"], "Div A")
        self.api.create_team(club["id"], div["id"], "Team1")
        h = self.api.get_setup_hierarchy()
        ma = h["missing_assignments"]
        self.assertEqual([v["name"] for v in ma["venues_without_organization"]],
                         ["Unowned Arena"])
        # #283: a division always resolves to a League now (auto-provisioned
        # when the season has none), so it is never flagged as level-less.
        self.assertEqual([d["name"] for d in ma["divisions_without_level"]], [])


if __name__ == "__main__":
    unittest.main()
