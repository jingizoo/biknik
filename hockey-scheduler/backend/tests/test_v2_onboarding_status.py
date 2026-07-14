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
        self._club = api.create_club("C", actor_id="admin")
        return org, program, season

    def test_season_with_league_no_division_is_ready_in_v2(self):
        api = self._api()
        org, program, season = self._base(api)
        league = api.create_league(season["id"], "Diamond", actor_id="admin")
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

    def test_same_data_still_blocks_no_division_in_v1(self):
        """The v1 readiness is FROZEN: the exact data that is v2-ready (league,
        no division) still blocks v1 on `no_division`, and v1 never asks for a
        grouping League."""
        api = self._api()
        org, program, season = self._base(api)
        league = api.create_league(season["id"], "Diamond", actor_id="admin")
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
