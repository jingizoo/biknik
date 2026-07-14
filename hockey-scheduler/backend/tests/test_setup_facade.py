import json
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService


class SetupFacadeTest(unittest.TestCase):
    def setUp(self):
        self.api = ApiService()

    def _build(self):
        league = self.api.create_league("EU Premier", country="DE")
        season = self.api.create_season(league["id"], "2026/27")
        division = self.api.create_division(season["id"], "U16", age_group="U16")
        club_a = self.api.create_club("Lions Club")
        club_b = self.api.create_club("Falcons Club")
        home = self.api.create_team(club_a["id"], division["id"], "U16 Lions")
        away = self.api.create_team(club_b["id"], division["id"], "U16 Falcons")
        # Participation comes from the season registration (#180).
        self.api.register_team_for_season(season["id"], home["id"], division["id"])
        self.api.register_team_for_season(season["id"], away["id"], division["id"])
        venue = self.api.create_venue("Ice Palace", league_id=league["id"])
        rink = self.api.create_rink(venue["id"], "Rink 2")
        slot = self.api.create_ice_slot(
            rink["id"], "2026-09-01T18:30:00+00:00", "2026-09-01T20:00:00+00:00"
        )
        return season, division, home, away, slot

    def test_full_setup_through_facade_is_json_safe(self):
        season, division, home, away, slot = self._build()
        game = self.api.create_game(
            season["id"], division["id"], home["id"], away["id"], slot["id"]
        )
        self.assertNotIn("error", game)
        self.assertEqual(game["ice_slot_id"], slot["id"])
        json.dumps(game)  # must round-trip with no custom encoder

    def test_duplicate_slot_returns_schedule_conflict(self):
        season, division, home, away, slot = self._build()
        self.api.create_game(season["id"], division["id"], home["id"], away["id"], slot["id"])
        result = self.api.create_game(
            season["id"], division["id"], home["id"], away["id"], slot["id"]
        )
        self.assertEqual(result["error"]["code"], "schedule_conflict")

    def test_missing_league_returns_not_found(self):
        result = self.api.create_season("league_missing", "S")
        self.assertEqual(result["error"]["code"], "not_found")

    def test_naive_ice_slot_time_returns_validation_error(self):
        venue = self.api.create_venue("V")
        rink = self.api.create_rink(venue["id"], "R")
        result = self.api.create_ice_slot(
            rink["id"], "2026-09-01T18:30:00", "2026-09-01T20:00:00"
        )
        self.assertEqual(result["error"]["code"], "validation_error")

    def test_create_league_with_owner(self):
        org = self.api.create_organization("Canlon")
        league = self.api.create_league("Over 55", organization_id=org["id"])
        self.assertNotIn("error", league)
        self.assertEqual(league["organization_id"], org["id"])

    def test_create_league_with_missing_owner_returns_not_found(self):
        result = self.api.create_league("Over 55", organization_id="org_missing")
        self.assertEqual(result["error"]["code"], "not_found")

    def test_create_league_without_owner_is_unassigned(self):
        league = self.api.create_league("Over 55")
        self.assertIsNone(league["organization_id"])

    def test_venue_with_league_derives_owner(self):
        org = self.api.create_organization("Canlon")
        league = self.api.create_league("Over 55", organization_id=org["id"])
        venue = self.api.create_venue("Plainfield", league_id=league["id"])
        self.assertEqual(venue["league_id"], league["id"])
        self.assertEqual(venue["organization_id"], org["id"])  # derived

    def test_venue_owner_mismatch_with_league_rejected(self):
        org = self.api.create_organization("Canlon")
        other = self.api.create_organization("Other Owner")
        league = self.api.create_league("Over 55", organization_id=org["id"])
        result = self.api.create_venue(
            "Plainfield", organization_id=other["id"], league_id=league["id"])
        self.assertEqual(result["error"]["code"], "validation_error")

    def test_venue_with_missing_league_returns_not_found(self):
        result = self.api.create_venue("Plainfield", league_id="league_missing")
        self.assertEqual(result["error"]["code"], "not_found")

    def test_create_organization_through_facade(self):
        org = self.api.create_organization("Summit Ice Facilities", short_name="Summit")
        self.assertNotIn("error", org)
        self.assertEqual(org["name"], "Summit Ice Facilities")
        self.assertEqual(org["short_name"], "Summit")
        json.dumps(org)  # must round-trip with no custom encoder

    def test_create_venue_assigns_organization(self):
        org = self.api.create_organization("Summit Ice Facilities")
        venue = self.api.create_venue("Ice Palace", organization_id=org["id"])
        self.assertNotIn("error", venue)
        self.assertEqual(venue["organization_id"], org["id"])

    def test_create_venue_without_organization_is_unassigned(self):
        venue = self.api.create_venue("Ice Palace")
        self.assertNotIn("error", venue)
        self.assertIsNone(venue["organization_id"])

    def test_create_venue_with_missing_organization_returns_not_found(self):
        result = self.api.create_venue("Ice Palace", organization_id="org_missing")
        self.assertEqual(result["error"]["code"], "not_found")

    def test_overview_surfaces_organization_on_venue_rows(self):
        org = self.api.create_organization("Summit Ice Facilities")
        self.api.create_venue("Ice Palace", organization_id=org["id"])
        overview = self.api.get_demo_overview()
        self.assertIn("organizations", overview)
        self.assertEqual(len(overview["organizations"]), 1)
        row = next(v for v in overview["venues"] if v["name"] == "Ice Palace")
        self.assertEqual(row["organization_id"], org["id"])
        self.assertEqual(row["organization_name"], "Summit Ice Facilities")

    def test_create_level_and_assign_division(self):
        league = self.api.create_league("Over 55")
        season = self.api.create_season(league["id"], "Fall 2026")
        level = self.api.create_level(season["id"], "Level 1", sort_order=1)
        self.assertNotIn("error", level)
        self.assertEqual(level["name"], "Level 1")
        self.assertEqual(level["sort_order"], 1)
        div = self.api.create_division(season["id"], "Div A", level_id=level["id"])
        self.assertEqual(div["level_id"], level["id"])

    def test_create_division_without_level_is_unassigned(self):
        league = self.api.create_league("Over 55")
        season = self.api.create_season(league["id"], "Fall 2026")
        div = self.api.create_division(season["id"], "Div A")
        self.assertIsNone(div["level_id"])

    def test_create_division_with_missing_level_returns_not_found(self):
        league = self.api.create_league("Over 55")
        season = self.api.create_season(league["id"], "Fall 2026")
        result = self.api.create_division(season["id"], "Div A", level_id="level_missing")
        self.assertEqual(result["error"]["code"], "not_found")

    def test_division_rejects_level_from_another_season(self):
        league = self.api.create_league("Over 55")
        season_a = self.api.create_season(league["id"], "Fall 2026")
        season_b = self.api.create_season(league["id"], "Spring 2027")
        level_a = self.api.create_level(season_a["id"], "Level 1")
        result = self.api.create_division(season_b["id"], "Div A", level_id=level_a["id"])
        self.assertEqual(result["error"]["code"], "validation_error")

    def test_overview_surfaces_level_on_division_rows(self):
        league = self.api.create_league("Over 55")
        season = self.api.create_season(league["id"], "Fall 2026")
        level = self.api.create_level(season["id"], "Level 1")
        self.api.create_division(season["id"], "Div A", level_id=level["id"])
        overview = self.api.get_demo_overview()
        self.assertIn("levels", overview)
        self.assertEqual(len(overview["levels"]), 1)
        row = next(d for d in overview["divisions"] if d["name"] == "Div A")
        self.assertEqual(row["level_id"], level["id"])
        self.assertEqual(row["level_name"], "Level 1")

    def test_created_game_drives_roster_endpoints(self):
        season, division, home, away, slot = self._build()
        game = self.api.create_game(
            season["id"], division["id"], home["id"], away["id"], slot["id"]
        )
        g1 = self.api.create_player(home["id"], "Goalie One", "goalie")
        self.api.create_player(home["id"], "Skater One", "forward")
        sel = self.api.select_roster(game["id"], [g1["id"]], actor_id="coach_1")
        self.assertNotIn("error", sel)
        status = self.api.get_roster_status(game["id"])
        self.assertEqual(status["game_id"], game["id"])


class SetupValidationWordingTest(unittest.TestCase):
    """#233 B1: setup validation/deletion messages use the canonical display
    nouns — Program for the umbrella (internal ``league``), League for the
    grouping (internal ``level``), and facility-owner / operating-organization
    for the venue↔program org roles. Error codes stay frozen."""

    def setUp(self):
        self.api = ApiService()

    def _msg(self, result):
        return result["error"]["message"]

    def test_missing_umbrella_parent_says_program(self):
        r = self.api.create_season("nope", "S")
        self.assertEqual(r["error"]["code"], "not_found")
        self.assertEqual(self._msg(r), "Program nope not found.")

    def test_team_without_umbrella_says_program(self):
        club = self.api.create_club("C")
        r = self.api.create_team(club["id"], None, "T")  # no league/division
        self.assertIn("A team needs a program", self._msg(r))
        self.assertNotRegex(self._msg(r), r"(?i)\bleague\b")

    def test_missing_grouping_parent_says_league(self):
        league = self.api.create_league("P")
        season = self.api.create_season(league["id"], "S")
        r = self.api.create_division(season["id"], "D", level_id="nope")
        self.assertEqual(self._msg(r), "League nope not found.")

    def test_venue_program_owner_mismatch_uses_role_nouns_and_v1_qualifier(self):
        org1 = self.api.create_organization("O1")
        org2 = self.api.create_organization("O2")
        league = self.api.create_league("P", organization_id=org1["id"])
        venue = self.api.create_venue("V", organization_id=org2["id"])
        r = self.api.assign_venue_league(venue["id"], league["id"])
        msg = self._msg(r)
        self.assertIn("facility owner", msg)
        self.assertIn("operating organization", msg)
        # Presented as a temporary v1 constraint, not the target ownership model.
        self.assertIn("Current v1 compatibility", msg)
        self.assertIn("Slice E", msg)
        self.assertNotRegex(msg, r"(?i)\bleague\b")

    def test_create_venue_owner_mismatch_is_v1_qualified(self):
        org1 = self.api.create_organization("O1")
        org2 = self.api.create_organization("O2")
        league = self.api.create_league("P", organization_id=org1["id"])
        r = self.api.create_venue("V", organization_id=org2["id"],
                                  league_id=league["id"])
        msg = self._msg(r)
        self.assertIn("Current v1 compatibility", msg)
        self.assertIn("facility owner", msg)
        self.assertIn("operating organization", msg)

    def test_change_operating_org_blocked_while_venue_attached_is_v1_qualified(self):
        org1 = self.api.create_organization("O1")
        org2 = self.api.create_organization("O2")
        league = self.api.create_league("P", organization_id=org1["id"])
        self.api.create_venue("V", league_id=league["id"])  # attaches a venue
        r = self.api.assign_league_organization(league["id"], org2["id"])
        msg = self._msg(r)
        self.assertIn("Current v1 compatibility", msg)
        self.assertIn("operating organization", msg)
        self.assertIn("venue", msg)

    def test_rollover_across_programs_says_programs(self):
        p1 = self.api.create_league("P1")
        p2 = self.api.create_league("P2")
        s1 = self.api.create_season(p1["id"], "S1")
        s2 = self.api.create_season(p2["id"], "S2")
        r = self.api.roll_forward_registrations(s1["id"], s2["id"])
        msg = self._msg(r)
        self.assertIn("different programs", msg)
        self.assertNotRegex(msg, r"(?i)\bleague\b")

    def test_delete_umbrella_blocked_says_program(self):
        league = self.api.create_league("P")
        self.api.create_season(league["id"], "S")
        r = self.api.delete_league(league["id"])
        self.assertEqual(r["error"]["code"], "has_dependencies")
        self.assertIn("Can't delete this program", self._msg(r))

    def test_delete_grouping_blocked_says_league(self):
        league = self.api.create_league("P")
        season = self.api.create_season(league["id"], "S")
        level = self.api.create_level(season["id"], "Grp")
        self.api.create_division(season["id"], "D", level_id=level["id"])
        r = self.api.delete_level(level["id"])
        self.assertIn("Can't delete this league", self._msg(r))

    def test_delete_facility_owner_dep_renders_program_frozen_type(self):
        org = self.api.create_organization("O")
        self.api.create_league("P", organization_id=org["id"])
        r = self.api.delete_organization(org["id"])
        # Rendered noun is canonical…
        self.assertIn("1 program", self._msg(r))
        self.assertNotRegex(self._msg(r), r"(?i)\bleague\b")
        # …while the structured dependency type stays the frozen v1 code.
        deps = r["error"]["details"]["dependencies"]
        prog = next(g for g in deps if g["display"] == "program")
        self.assertEqual(prog["type"], "league")

    def test_delete_season_dep_renders_league_frozen_type(self):
        league = self.api.create_league("P")
        season = self.api.create_season(league["id"], "S")
        self.api.create_level(season["id"], "Grp")
        r = self.api.delete_season(season["id"])
        self.assertIn("1 league", self._msg(r))
        deps = r["error"]["details"]["dependencies"]
        grp = next(g for g in deps if g["display"] == "league")
        self.assertEqual(grp["type"], "level")


if __name__ == "__main__":
    unittest.main()
