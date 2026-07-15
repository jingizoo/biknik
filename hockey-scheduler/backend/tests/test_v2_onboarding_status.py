"""v2 onboarding readiness — League required, Division optional (#233 Slice C2).

The FROZEN v1 readiness (`get_onboarding_status` / GET /api/onboarding/status)
blocks on `no_division` and has no League requirement. The canonical v2 readiness
enforces the TARGET model instead: a Season needs at least one grouping League
(blocker `no_league`) and Division is OPTIONAL (NO `no_division` blocker).

These tests prove the divergence directly: the SAME data reads as ready in v2 but
still blocks on `no_division` in v1, and a season lacking a League blocks v2 on
`no_league` while v1 (which has no such requirement) does not.
"""

import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.store import InMemoryStore


def _codes(status):
    return {b["code"] for b in status["blocking"]}


class V2OnboardingStatusTest(unittest.TestCase):
    def _api(self):
        return ApiService(InMemoryStore())

    def _base(self, api):
        """Deployment + facility + program chain up to an available game slot,
        plus a season and a team — but NO league and NO division yet."""
        api.create_user_account("admin", "pw", "league_admin")
        org = api.create_organization("Org", short_name="O", actor_id="admin")
        program = api.create_program("Prog", operator_organization_id=org["id"],
                                     actor_id="admin")
        venue = api.create_venue("V", league_id=program["id"], actor_id="admin")
        rink = api.create_rink(venue["id"], "R", actor_id="admin")
        api.create_ice_slot(rink["id"], "2026-09-01T18:30:00+00:00",
                            "2026-09-01T20:00:00+00:00", actor_id="admin")
        season = api.create_season(program["id"], "Fall", actor_id="admin")
        api.grant_season_venue_access(season["id"], venue["id"], actor_id="admin")
        self._club = api.create_club("C", actor_id="admin")
        return org, program, season

    def test_season_with_league_no_division_is_ready_in_v2(self):
        api = self._api()
        org, program, season = self._base(api)
        league = api.create_league(season["id"], "Adult League", actor_id="admin")
        # A team registered with the league but NO division.
        team = api.create_team(self._club["id"], None, "T", actor_id="admin",
                               program_id=program["id"])
        reg = api.register_team_for_season(season["id"], team["id"],
                                           actor_id="admin",
                                           league_id=league["id"])
        self.assertNotIn("error", reg, reg)

        status = api.get_onboarding_status_v2("demo")
        self.assertTrue(status["ready_to_schedule"], status["blocking"])
        self.assertNotIn("no_division", _codes(status))
        self.assertNotIn("no_league", _codes(status))

    def test_season_without_league_is_not_ready_in_v2(self):
        api = self._api()
        org, program, season = self._base(api)
        # A team exists, but no grouping League under the season.
        api.create_team(self._club["id"], None, "T", actor_id="admin",
                        program_id=program["id"])
        status = api.get_onboarding_status_v2("demo")
        self.assertFalse(status["ready_to_schedule"], status)
        self.assertIn("no_league", _codes(status))
        # Division optional — never a blocker in v2.
        self.assertNotIn("no_division", _codes(status))

    def test_program_without_organization_never_blocks_v2(self):
        """#233 B2b review: operator_organization_id is nullable on the
        canonical Program (B2a/ADR 0001), so a Program with no operator is a
        complete, valid Program — v2 readiness must never block on it."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Orgless Prog", actor_id="admin")
        self.assertIsNone(program.get("operator_organization_id"))
        season = api.create_season(program["id"], "Fall", actor_id="admin")
        league = api.create_league(season["id"], "Adult League", actor_id="admin")
        club = api.create_club("C", actor_id="admin")
        team = api.create_team(club["id"], None, "T", actor_id="admin",
                               program_id=program["id"])
        # No venue granted season access at all here — that's a SEPARATE
        # blocker (no_venue_access_granted) this test doesn't need to
        # clear; it only proves the operator gap itself never blocks
        # readiness (registration is set up so the rest of the chain is
        # otherwise valid, isolating the assertion to the operator gap).
        reg = api.register_team_for_season(season["id"], team["id"],
                                           actor_id="admin",
                                           league_id=league["id"])
        self.assertNotIn("error", reg, reg)

        status = api.get_onboarding_status_v2("demo")
        codes = _codes(status)
        self.assertNotIn("program_without_organization", codes)
        warning_codes = {w["code"] for w in status["warnings"]}
        self.assertIn("program_without_organization", warning_codes)
        step = {s["key"]: s for s in status["steps"]}["program_ownership"]
        self.assertFalse(step["blocking"])

    def test_same_program_without_organization_still_blocks_v1(self):
        """v1 readiness is FROZEN: the exact same orgless Program still blocks
        v1 on league_without_organization (v1's name for this gap)."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Orgless Prog", actor_id="admin")
        status = api.get_onboarding_status("demo")
        self.assertFalse(status["ready_to_schedule"], status)
        self.assertIn("league_without_organization",
                      {b["code"] for b in status["blocking"]})

    def test_same_data_still_blocks_no_division_in_v1(self):
        """The v1 readiness is FROZEN: the exact data that is v2-ready (league,
        no division) still blocks v1 on `no_division`, and v1 never asks for a
        grouping League."""
        api = self._api()
        org, program, season = self._base(api)
        league = api.create_league(season["id"], "Adult League", actor_id="admin")
        team = api.create_team(self._club["id"], None, "T", actor_id="admin",
                               program_id=program["id"])
        api.register_team_for_season(season["id"], team["id"], actor_id="admin",
                                     league_id=league["id"])

        v2 = api.get_onboarding_status_v2("demo")
        v1 = api.get_onboarding_status("demo")

        # v2 ready; v1 not — the frozen v1 still demands a Division.
        self.assertTrue(v2["ready_to_schedule"], v2["blocking"])
        self.assertFalse(v1["ready_to_schedule"], v1)
        self.assertIn("no_division", _codes(v1))
        self.assertNotIn("no_division", _codes(v2))
        # v1 has no grouping-League requirement (its `no_league` means "no
        # Program", which is satisfied here), so it never blocks on no_league.
        self.assertNotIn("no_league", _codes(v1))

    def test_second_season_without_league_blocks_no_league(self):
        """League is required PER Season: a second Season with no grouping League
        must fire `no_league` even though the first Season has one — the naming
        message identifies the offending Season."""
        api = self._api()
        org, program, season = self._base(api)
        # First season gets a league + a valid registration.
        league = api.create_league(season["id"], "Adult League", actor_id="admin")
        team = api.create_team(self._club["id"], None, "T", actor_id="admin",
                               program_id=program["id"])
        api.register_team_for_season(season["id"], team["id"], actor_id="admin",
                                     league_id=league["id"])
        # A SECOND season under the same program with NO league.
        season2 = api.create_season(program["id"], "Spring", actor_id="admin")

        status = api.get_onboarding_status_v2("demo")
        self.assertFalse(status["ready_to_schedule"], status)
        codes = _codes(status)
        self.assertIn("no_league", codes)
        # The blocker names the season that lacks a league.
        msg = next(b["message"] for b in status["blocking"]
                   if b["code"] == "no_league")
        self.assertIn(season2["name"], msg, msg)

    def test_invalid_registrations_are_reported(self):
        """Cross-League (league_id not in the season), cross-Program (team in
        another program), and cross-League-Division registrations are surfaced as
        an `invalid_registrations` blocker."""
        api = self._api()
        org, program, season = self._base(api)
        league = api.create_league(season["id"], "Adult League", actor_id="admin")
        team = api.create_team(self._club["id"], None, "T", actor_id="admin",
                               program_id=program["id"])
        # A valid registration keeps the installation otherwise ready...
        api.register_team_for_season(season["id"], team["id"], actor_id="admin",
                                     league_id=league["id"])
        # ...but a league from a DIFFERENT season is invalid participation.
        other_season = api.create_season(program["id"], "Other", actor_id="admin")
        other_league = api.create_league(other_season["id"], "OL",
                                         actor_id="admin")
        team2 = api.create_team(self._club["id"], None, "T2", actor_id="admin",
                                program_id=program["id"])
        # Inject a cross-season-league registration directly (the service would
        # reject it) to reproduce the invalid data the readiness must report.
        from hockey_scheduler.domain import SeasonTeamRegistration
        api.store.add_season_team_registration(SeasonTeamRegistration(
            id=api.store.next_id("streg"), season_id=season["id"],
            team_id=team2["id"], division_id=None,
            league_id=other_league["id"], active=True))

        status = api.get_onboarding_status_v2("demo")
        self.assertIn("invalid_registrations", _codes(status), status)

    def test_repair_via_v2_after_direct_injection(self):
        """#233 B2b review r2: the same defect a direct-injection test proves
        the readiness detects (test_invalid_registrations_are_reported) must
        also be fixable through the documented v2 write surface — the exact
        assign-league call the frontend's Needs-assignment repair row makes.

        registration_league_not_in_season is otherwise unreachable through
        any documented v2 mutation as of this review round: delete_league now
        blocks on a live registration referencing it (setup_service.py), so
        the injected row here is deliberately manufactured the same way
        test_invalid_registrations_are_reported does — direct store access,
        bypassing the service layer the real app always goes through."""
        api = self._api()
        org, program, season = self._base(api)
        league = api.create_league(season["id"], "Adult League", actor_id="admin")
        team = api.create_team(self._club["id"], None, "T", actor_id="admin",
                               program_id=program["id"])
        other_season = api.create_season(program["id"], "Other", actor_id="admin")
        other_league = api.create_league(other_season["id"], "OL", actor_id="admin")

        from hockey_scheduler.domain import SeasonTeamRegistration
        reg_id = api.store.next_id("streg")
        api.store.add_season_team_registration(SeasonTeamRegistration(
            id=reg_id, season_id=season["id"], team_id=team["id"],
            division_id=None, league_id=other_league["id"], active=True))

        before = api.get_setup_hierarchy_v2()
        season_node = next(s for p in before["programs"] for s in p["seasons"]
                           if s["id"] == season["id"])
        issues = season_node["needs_assignment"]["registrations"]
        self.assertEqual(len(issues), 1, issues)
        self.assertEqual(issues[0]["reason"], "registration_league_not_in_season")
        self.assertEqual(issues[0]["registration_id"], reg_id)
        status_before = api.get_onboarding_status_v2("demo")
        self.assertIn("invalid_registrations", _codes(status_before))

        # Repair through the exact v2 route the frontend's repair row Save
        # button calls.
        repaired = api.assign_season_team_league(reg_id, league["id"],
                                                  actor_id="admin")
        self.assertNotIn("error", repaired, repaired)

        after = api.get_setup_hierarchy_v2()
        season_node_after = next(s for p in after["programs"] for s in p["seasons"]
                                 if s["id"] == season["id"])
        self.assertEqual(season_node_after["needs_assignment"]["registrations"], [])
        league_node = next(lv for lv in season_node_after["leagues"]
                           if lv["id"] == league["id"])
        self.assertIn(team["id"],
                      {t["id"] for t in league_node["teams_without_division"]})
        status_after = api.get_onboarding_status_v2("demo")
        self.assertNotIn("invalid_registrations", _codes(status_after), status_after)

    def test_v2_uses_canonical_program_code(self):
        """With nothing created, v2 blocks on `no_program` (canonical umbrella
        code), never repurposing v1's `no_league` for the umbrella."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        api.create_organization("Org", short_name="O", actor_id="admin")
        status = api.get_onboarding_status_v2("demo")
        self.assertIn("no_program", _codes(status))
        self.assertNotIn("no_league", _codes(status))  # gated on has_season


if __name__ == "__main__":
    unittest.main()
