"""get_setup_progress's League-narrowing axis (#367, on top of #345's

Program/Season active-context selection).

``test_setup_progress.py`` already has full coverage for the pre-existing
Program/Season scoping (cross-Program isolation, per-Season participation/
facilities narrowing, role-aware ``next``/redaction, etc.) — this file adds
ONLY the third, League, axis #367 layered on top: a selected League further
narrows "Permanent teams", "Clubs, players and staff" (roster), and "Season
participation and divisions" to that League's own Teams/LeagueSeason, while
explicit "No League" keeps the full Program/Season-wide union (byte-identical
to pre-#367 behavior), and "League profile and seasons" / "Venues, rinks and
ice" are Program-wide/Season-wide facts that must never move with League
selection at all.

Facade-level, Memory-backed only (matches ``SetupProgressComputationTest``'s
own scope decision) — this narrowing logic is a pure read with no
concurrency angle of its own.
"""

import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.store import InMemoryStore

ADMIN = (Role.LEAGUE_ADMIN, {})
ARENA = (Role.ARENA_MANAGER, {})


def _workflow(progress, key):
    return next(w for w in progress["workflows"] if w["key"] == key)


class LeagueFilteredSetupProgressTest(unittest.TestCase):

    def _api(self):
        return ApiService(InMemoryStore())

    def test_teams_and_roster_narrow_to_selected_league_and_flip_on_switch(self):
        """Two Leagues in the SAME Program+Season, each with a different
        number of permanent Teams and Players on those Teams: selecting
        League X must reflect ONLY League X's counts in "teams"/"roster",
        and switching to League Y must flip both counts to Y's — proving the
        narrowing is live per-selection, not merely correct on first read."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season = api.create_season(program["id"], "Fall", actor_id="admin")
        league_x = api.create_league(season["id"], "League X", actor_id="admin")
        league_y = api.create_league(season["id"], "League Y", actor_id="admin")
        club = api.create_club("C", actor_id="admin")

        team_x1 = api.create_team(club["id"], None, "X1", actor_id="admin",
                                  program_id=program["id"], league_id=league_x["id"])
        team_x2 = api.create_team(club["id"], None, "X2", actor_id="admin",
                                  program_id=program["id"], league_id=league_x["id"])
        team_y1 = api.create_team(club["id"], None, "Y1", actor_id="admin",
                                  program_id=program["id"], league_id=league_y["id"])
        self.assertNotIn("error", team_x1, team_x1)
        self.assertNotIn("error", team_y1, team_y1)

        # League X: 2 teams, 3 players (2 on X1, 1 on X2).
        api.create_player(team_x1["id"], "X1 Player A", "forward", actor_id="admin")
        api.create_player(team_x1["id"], "X1 Player B", "forward", actor_id="admin")
        api.create_player(team_x2["id"], "X2 Player A", "forward", actor_id="admin")
        # League Y: 1 team, 1 player.
        api.create_player(team_y1["id"], "Y1 Player A", "forward", actor_id="admin")

        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season["id"], league_x["id"])
        progress_x = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(progress_x["program_id"], program["id"])
        teams_x = _workflow(progress_x, "teams")
        self.assertEqual(teams_x["status"], "done")
        self.assertEqual(teams_x["detail"], "2 team(s)")
        roster_x = _workflow(progress_x, "roster")
        self.assertEqual(roster_x["status"], "done")
        self.assertEqual(roster_x["detail"], "3 player(s)")

        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season["id"], league_y["id"])
        progress_y = api.get_setup_progress("admin", *ADMIN)
        teams_y = _workflow(progress_y, "teams")
        self.assertEqual(teams_y["detail"], "1 team(s)",
                         "switching to League Y must flip the count to Y's "
                         "own teams, not keep League X's")
        roster_y = _workflow(progress_y, "roster")
        self.assertEqual(roster_y["detail"], "1 player(s)",
                         "switching to League Y must flip the count to Y's "
                         "own players, not keep League X's")

        # Switching back to League X reproduces the original counts — proves
        # neither read mutated shared state.
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season["id"], league_x["id"])
        progress_x_again = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(_workflow(progress_x_again, "teams")["detail"], "2 team(s)")
        self.assertEqual(_workflow(progress_x_again, "roster")["detail"], "3 player(s)")

    def test_no_league_selected_shows_union_of_both_leagues(self):
        """Explicit "No League" (never selecting one, or selecting one and
        then clearing it back to None) must show the BROADER union across
        every League's Teams/Players — a strict superset of either League's
        own narrowed view, matching pre-#367 (Program/Season-only) behavior
        exactly."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season = api.create_season(program["id"], "Fall", actor_id="admin")
        league_x = api.create_league(season["id"], "League X", actor_id="admin")
        league_y = api.create_league(season["id"], "League Y", actor_id="admin")
        club = api.create_club("C", actor_id="admin")

        team_x1 = api.create_team(club["id"], None, "X1", actor_id="admin",
                                  program_id=program["id"], league_id=league_x["id"])
        team_x2 = api.create_team(club["id"], None, "X2", actor_id="admin",
                                  program_id=program["id"], league_id=league_x["id"])
        team_y1 = api.create_team(club["id"], None, "Y1", actor_id="admin",
                                  program_id=program["id"], league_id=league_y["id"])
        api.create_player(team_x1["id"], "X1 Player A", "forward", actor_id="admin")
        api.create_player(team_x1["id"], "X1 Player B", "forward", actor_id="admin")
        api.create_player(team_x2["id"], "X2 Player A", "forward", actor_id="admin")
        api.create_player(team_y1["id"], "Y1 Player A", "forward", actor_id="admin")

        # Case A: a League is never explicitly selected at all — only
        # Program+Season are set (5-arg legacy call, league_id omitted).
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season["id"])
        never_selected = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(_workflow(never_selected, "teams")["detail"], "3 team(s)")
        self.assertEqual(_workflow(never_selected, "roster")["detail"], "4 player(s)")

        # Case B: a League WAS selected, then explicitly cleared back to
        # None — proves clearing genuinely widens the view again rather than
        # leaving a stale narrowed selection in place.
        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season["id"], league_x["id"])
        narrowed = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(_workflow(narrowed, "teams")["detail"], "2 team(s)")

        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season["id"], None)
        cleared = api.get_setup_progress("admin", *ADMIN)
        self.assertEqual(_workflow(cleared, "teams")["detail"], "3 team(s)",
                         "clearing back to No League must widen to the "
                         "union, not keep League X's narrowed count")
        self.assertEqual(_workflow(cleared, "roster")["detail"], "4 player(s)")

    def test_participation_narrows_by_league_and_no_league_shows_combined(self):
        """"Season participation and divisions": one Team registered under
        League X's LeagueSeason, a different Team under League Y's, same
        Season. Selecting League X shows only X's schedulable registration
        count, League Y only Y's, and No League shows both combined — the
        same narrow/union contract as "teams"/"roster", applied to
        registrations instead of Team/Player rows."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season = api.create_season(program["id"], "Fall", actor_id="admin")
        league_x = api.create_league(season["id"], "League X", actor_id="admin")
        league_y = api.create_league(season["id"], "League Y", actor_id="admin")
        club = api.create_club("C", actor_id="admin")

        team_x = api.create_team(club["id"], None, "TeamX", actor_id="admin",
                                 program_id=program["id"], league_id=league_x["id"])
        team_y = api.create_team(club["id"], None, "TeamY", actor_id="admin",
                                 program_id=program["id"], league_id=league_y["id"])
        reg_x = api.register_team_for_season(season["id"], team_x["id"],
                                             actor_id="admin", league_id=league_x["id"])
        reg_y = api.register_team_for_season(season["id"], team_y["id"],
                                             actor_id="admin", league_id=league_y["id"])
        self.assertNotIn("error", reg_x, reg_x)
        self.assertNotIn("error", reg_y, reg_y)

        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season["id"], league_x["id"])
        progress_x = api.get_setup_progress("admin", *ADMIN)
        participation_x = _workflow(progress_x, "participation")
        self.assertEqual(participation_x["status"], "done")
        self.assertEqual(participation_x["detail"], "1 schedulable registration(s)")

        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season["id"], league_y["id"])
        progress_y = api.get_setup_progress("admin", *ADMIN)
        participation_y = _workflow(progress_y, "participation")
        self.assertEqual(participation_y["status"], "done")
        self.assertEqual(participation_y["detail"], "1 schedulable registration(s)",
                         "League Y's own view must not include League X's "
                         "registration")

        api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                               program["id"], season["id"], None)
        progress_none = api.get_setup_progress("admin", *ADMIN)
        participation_none = _workflow(progress_none, "participation")
        self.assertEqual(participation_none["detail"], "2 schedulable registration(s)",
                         "No League selected must show both registrations "
                         "combined, matching pre-#367 full-season behavior")

    def test_league_selection_never_leaks_across_programs(self):
        """Two Programs, each with their own two Leagues, every one of the
        four (Program, League) combinations carrying a DIFFERENT team count
        (1/2/3/4) so any leak — wrong Program OR wrong League — is caught
        precisely. Switches BOTH axes together (not just Program, like
        ``test_cross_program_isolation`` does for the pre-existing two-axis
        case) and revisits an earlier combination to prove no residual state
        survives a round trip through the others."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        club = api.create_club("C", actor_id="admin")

        program_a = api.create_program("Prog A", actor_id="admin")
        season_a = api.create_season(program_a["id"], "Season A", actor_id="admin")
        league_ax = api.create_league(season_a["id"], "AX", actor_id="admin")
        league_ay = api.create_league(season_a["id"], "AY", actor_id="admin")

        program_b = api.create_program("Prog B", actor_id="admin")
        season_b = api.create_season(program_b["id"], "Season B", actor_id="admin")
        league_bx = api.create_league(season_b["id"], "BX", actor_id="admin")
        league_by = api.create_league(season_b["id"], "BY", actor_id="admin")

        def _team(name, program, league):
            t = api.create_team(club["id"], None, name, actor_id="admin",
                                program_id=program["id"], league_id=league["id"])
            self.assertNotIn("error", t, t)
            return t

        _team("AX1", program_a, league_ax)                      # A/AX: 1
        _team("AY1", program_a, league_ay)                      # A/AY: 2
        _team("AY2", program_a, league_ay)
        _team("BX1", program_b, league_bx)                      # B/BX: 3
        _team("BX2", program_b, league_bx)
        _team("BX3", program_b, league_bx)
        _team("BY1", program_b, league_by)                      # B/BY: 4
        _team("BY2", program_b, league_by)
        _team("BY3", program_b, league_by)
        _team("BY4", program_b, league_by)

        def _select_and_read(program, season, league):
            api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                                   program["id"], season["id"], league["id"])
            progress = api.get_setup_progress("admin", *ADMIN)
            self.assertEqual(progress["program_id"], program["id"])
            return _workflow(progress, "teams")["detail"]

        self.assertEqual(_select_and_read(program_a, season_a, league_ax), "1 team(s)")
        self.assertEqual(_select_and_read(program_a, season_a, league_ay), "2 team(s)")
        self.assertEqual(_select_and_read(program_b, season_b, league_bx), "3 team(s)")
        self.assertEqual(_select_and_read(program_b, season_b, league_by), "4 team(s)")

        # Revisit combinations already read, out of order, to rule out any
        # residual state leaking from a PRIOR selection.
        self.assertEqual(_select_and_read(program_a, season_a, league_ax), "1 team(s)")
        self.assertEqual(_select_and_read(program_b, season_b, league_by), "4 team(s)")
        self.assertEqual(_select_and_read(program_a, season_a, league_ay), "2 team(s)")
        self.assertEqual(_select_and_read(program_b, season_b, league_bx), "3 team(s)")

    def test_league_profile_and_facilities_are_invariant_to_league_selection(self):
        """"League profile and seasons" (a Program-wide integrity check) and
        "Venues, rinks and ice" (a physical-resource workflow with no
        competition-League axis at all) must report the SAME value
        regardless of which League — or none — is active. Directly asserted
        for both League Admin (who sees both workflows) and Arena Manager
        (whose only visible workflow is facilities), since this is the one
        thing most likely to accidentally regress if a future change narrows
        too aggressively."""
        api = self._api()
        api.create_user_account("admin", "pw", "league_admin")
        program = api.create_program("Prog", actor_id="admin")
        season = api.create_season(program["id"], "Fall", actor_id="admin")
        league_x = api.create_league(season["id"], "League X", actor_id="admin")
        league_y = api.create_league(season["id"], "League Y", actor_id="admin")

        venue = api.create_venue("V", league_id=program["id"], actor_id="admin")
        rink = api.create_rink(venue["id"], "R", actor_id="admin")
        api.create_ice_slot(rink["id"], "2026-09-01T18:30:00+00:00",
                            "2026-09-01T20:00:00+00:00", actor_id="admin")
        api.grant_season_venue_access(season["id"], venue["id"], actor_id="admin")

        def _read(role_tuple, league):
            if league is None:
                api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                                       program["id"], season["id"], None)
            else:
                api.set_active_context("admin", Role.LEAGUE_ADMIN, {},
                                       program["id"], season["id"], league["id"])
            progress = api.get_setup_progress("admin", *role_tuple)
            return progress

        # League Admin sees both "league_season" and "facilities".
        admin_x = _read(ADMIN, league_x)
        admin_y = _read(ADMIN, league_y)
        admin_none = _read(ADMIN, None)

        league_season_x = _workflow(admin_x, "league_season")
        self.assertEqual(league_season_x["status"], "done")
        self.assertEqual(league_season_x, _workflow(admin_y, "league_season"),
                         "league_season must be identical for League Y as "
                         "for League X")
        self.assertEqual(league_season_x, _workflow(admin_none, "league_season"),
                         "league_season must be identical for No League too")

        facilities_x = _workflow(admin_x, "facilities")
        self.assertEqual(facilities_x["status"], "done")
        self.assertEqual(facilities_x["detail"], "1 available game slot(s)")
        self.assertEqual(facilities_x, _workflow(admin_y, "facilities"),
                         "facilities must be identical for League Y as for "
                         "League X")
        self.assertEqual(facilities_x, _workflow(admin_none, "facilities"),
                         "facilities must be identical for No League too")

        # Arena Manager's own (redacted-to-facilities-only) view must show
        # the exact same invariance.
        arena_x = _read(ARENA, league_x)
        arena_y = _read(ARENA, league_y)
        arena_none = _read(ARENA, None)
        self.assertEqual([w["key"] for w in arena_x["workflows"]], ["facilities"])
        self.assertEqual(_workflow(arena_x, "facilities"), facilities_x)
        self.assertEqual(_workflow(arena_y, "facilities"), facilities_x,
                         "Arena Manager's facilities view must also be "
                         "unaffected by League selection")
        self.assertEqual(_workflow(arena_none, "facilities"), facilities_x,
                         "Arena Manager's facilities view must also be "
                         "unaffected by clearing League selection")


if __name__ == "__main__":
    unittest.main()
