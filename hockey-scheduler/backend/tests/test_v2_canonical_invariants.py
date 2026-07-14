"""Canonical v2 invariants for #233 Slice C2 (PR #242 review corrections).

These exercise the canonical-model rules the reviewer flagged as gaps, at the
facade level (ApiService over InMemoryStore) so the store and audit log can be
inspected directly:

  1. A v2 Game is tied to its teams' registration League.
  2. The v2 rollover honors an explicit per-selection League.
  3. The v2 hierarchy places a division-less registered Team directly under its
     League (teams_without_division), never dropping it.
  4-6. v2 reassignment integrity: Division reparent may not strand dependents;
     clearing a registration's Division preserves its required League and setting
     one must match it; a registration's League may not change out from under a
     committed Game.

Every failure-path assertion checks BOTH the structured ``validation_error`` AND
that ZERO records / audit-log entries were mutated (the store transaction is a
lock, not a rollback, so the guard must run entirely before any write). The v1
paths are covered elsewhere and stay frozen.
"""

import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import IceSlotStatus
from hockey_scheduler.store import InMemoryStore

ADMIN = "admin"


class _Base(unittest.TestCase):
    def setUp(self):
        self.api = ApiService(InMemoryStore())

    def _audit_count(self):
        return len(self.api.store.all_setup_audit())

    def _org_program(self):
        org = self.api.create_organization("Org", "O", actor_id=ADMIN)
        program = self.api.create_program(
            "Prog", operator_organization_id=org["id"], actor_id=ADMIN)
        return org, program

    def _playable_slot(self, org, program):
        """A GAME ice slot whose venue is soundly linked to the program, so the
        league-ice isolation guard passes and game creation reaches the
        registration-league check."""
        venue = self.api.create_venue("V", organization_id=org["id"],
                                      league_id=program["id"], actor_id=ADMIN)
        rink = self.api.create_rink(venue["id"], "R", actor_id=ADMIN)
        slot = self.api.create_ice_slot(
            rink["id"], "2026-09-01T18:30:00+00:00",
            "2026-09-01T20:00:00+00:00", "game", actor_id=ADMIN)
        return slot


class GameRegistrationLeagueTest(_Base):
    def test_game_league_must_match_both_teams_registration_league(self):
        org, program = self._org_program()
        season = self.api.create_season(program["id"], "Fall", actor_id=ADMIN)
        l1 = self.api.create_league(season["id"], "L1", actor_id=ADMIN)
        l2 = self.api.create_league(season["id"], "L2", actor_id=ADMIN)
        club = self.api.create_club("C", actor_id=ADMIN)
        team_a = self.api.create_team(club["id"], None, "A", actor_id=ADMIN,
                                      program_id=program["id"])
        team_b = self.api.create_team(club["id"], None, "B", actor_id=ADMIN,
                                      program_id=program["id"])
        # Both teams registered in L1 (division-less).
        self.api.register_team_for_season(season["id"], team_a["id"],
                                          actor_id=ADMIN, league_id=l1["id"])
        self.api.register_team_for_season(season["id"], team_b["id"],
                                          actor_id=ADMIN, league_id=l1["id"])
        slot = self._playable_slot(org, program)

        audits_before = self._audit_count()
        # v2 game scoped to L2 while both teams are registered in L1 → rejected.
        res = self.api.create_game(
            season["id"], None, team_a["id"], team_b["id"], slot["id"],
            actor_id=ADMIN, league_id=l2["id"])
        self.assertEqual(res["error"]["code"], "validation_error", res)
        # Zero mutation: no game row, slot still AVAILABLE, no audit written.
        self.assertEqual(self.api.store.all_games(), [])
        self.assertEqual(self.api.store.get_ice_slot(slot["id"]).status,
                         IceSlotStatus.AVAILABLE)
        self.assertEqual(self._audit_count(), audits_before)

        # The same game scoped to L1 (the teams' registration League) succeeds.
        ok = self.api.create_game(
            season["id"], None, team_a["id"], team_b["id"], slot["id"],
            actor_id=ADMIN, league_id=l1["id"])
        self.assertNotIn("error", ok, ok)
        self.assertEqual(ok["league_id"], l1["id"])


class RollForwardV2Test(_Base):
    def _two_league_target(self):
        org, program = self._org_program()
        s1 = self.api.create_season(program["id"], "S1", actor_id=ADMIN)
        s2 = self.api.create_season(program["id"], "S2", actor_id=ADMIN)
        src_league = self.api.create_league(s1["id"], "SrcL", actor_id=ADMIN)
        l1t = self.api.create_league(s2["id"], "L1t", actor_id=ADMIN)
        l2t = self.api.create_league(s2["id"], "L2t", actor_id=ADMIN)
        club = self.api.create_club("C", actor_id=ADMIN)
        team_a = self.api.create_team(club["id"], None, "A", actor_id=ADMIN,
                                      program_id=program["id"])
        team_b = self.api.create_team(club["id"], None, "B", actor_id=ADMIN,
                                      program_id=program["id"])
        self.api.register_team_for_season(s1["id"], team_a["id"],
                                          actor_id=ADMIN, league_id=src_league["id"])
        self.api.register_team_for_season(s1["id"], team_b["id"],
                                          actor_id=ADMIN, league_id=src_league["id"])
        return s1, s2, l1t, l2t, team_a, team_b

    def test_v2_rollover_writes_the_selected_league(self):
        s1, s2, l1t, l2t, team_a, team_b = self._two_league_target()
        res = self.api.roll_forward_registrations_v2(
            s1["id"], s2["id"],
            selections=[{"team_id": team_a["id"], "league_id": l1t["id"]},
                        {"team_id": team_b["id"], "league_id": l2t["id"]}],
            actor_id=ADMIN)
        self.assertEqual(res["rolled_forward"], 2, res)
        by_team = {r["team_id"]: r for r in res["registrations"]}
        self.assertEqual(by_team[team_a["id"]]["league_id"], l1t["id"])
        self.assertEqual(by_team[team_b["id"]]["league_id"], l2t["id"])
        self.assertTrue(all(r["division_id"] is None
                            for r in res["registrations"]))

    def test_v2_rollover_selection_without_league_is_rejected(self):
        s1, s2, l1t, l2t, team_a, team_b = self._two_league_target()
        audits_before = self._audit_count()
        res = self.api.roll_forward_registrations_v2(
            s1["id"], s2["id"],
            selections=[{"team_id": team_a["id"]}], actor_id=ADMIN)
        self.assertEqual(res["error"]["code"], "validation_error", res)
        # Zero writes into the target season, no audits.
        self.assertEqual(
            [r for r in self.api.store.registrations_for_season(s2["id"])
             if r.active], [])
        self.assertEqual(self._audit_count(), audits_before)


class HierarchyV2TeamsWithoutDivisionTest(_Base):
    def test_division_less_team_appears_under_its_league(self):
        org, program = self._org_program()
        season = self.api.create_season(program["id"], "Fall", actor_id=ADMIN)
        league = self.api.create_league(season["id"], "L", actor_id=ADMIN)
        club = self.api.create_club("C", actor_id=ADMIN)
        team = self.api.create_team(club["id"], None, "T", actor_id=ADMIN,
                                    program_id=program["id"])
        self.api.register_team_for_season(season["id"], team["id"],
                                          actor_id=ADMIN, league_id=league["id"])

        tree = self.api.get_setup_hierarchy_v2()
        prog = next(p for p in tree["programs"] if p["id"] == program["id"])
        s = next(x for x in prog["seasons"] if x["id"] == season["id"])
        lg = next(x for x in s["leagues"] if x["id"] == league["id"])
        # The division-less registered team hangs directly off the League.
        self.assertIn(team["id"], [t["id"] for t in lg["teams_without_division"]])
        # It is NOT surfaced as invalid/needs-assignment.
        na_team_ids = [r["team_id"] for r in s["needs_assignment"]["registrations"]]
        self.assertNotIn(team["id"], na_team_ids)


class DivisionReparentIntegrityTest(_Base):
    def _season_two_leagues_division(self):
        org, program = self._org_program()
        season = self.api.create_season(program["id"], "Fall", actor_id=ADMIN)
        l1 = self.api.create_league(season["id"], "L1", actor_id=ADMIN)
        l2 = self.api.create_league(season["id"], "L2", actor_id=ADMIN)
        div = self.api.create_division_v2(l1["id"], "D", actor_id=ADMIN)
        return org, program, season, l1, l2, div

    def test_reparent_requires_league(self):
        org, program, season, l1, l2, div = self._season_two_leagues_division()
        res = self.api.assign_division_league(div["id"], None, actor_id=ADMIN,
                                              v2=True)
        self.assertEqual(res["error"]["code"], "validation_error", res)

    def test_reparent_that_would_strand_a_registration_is_rejected(self):
        org, program, season, l1, l2, div = self._season_two_leagues_division()
        club = self.api.create_club("C", actor_id=ADMIN)
        team = self.api.create_team(club["id"], None, "T", actor_id=ADMIN,
                                    program_id=program["id"])
        # A registration bound to this division (League L1).
        self.api.register_team_for_season(season["id"], team["id"], div["id"],
                                          actor_id=ADMIN, league_id=l1["id"])
        audits_before = self._audit_count()
        res = self.api.assign_division_league(div["id"], l2["id"], actor_id=ADMIN,
                                              v2=True)
        self.assertEqual(res["error"]["code"], "validation_error", res)
        # Zero mutation: division still under L1, no audit written.
        self.assertEqual(self.api.store.get_division(div["id"]).league_id,
                         l1["id"])
        self.assertEqual(self._audit_count(), audits_before)

    def test_reparent_with_no_dependents_succeeds(self):
        org, program, season, l1, l2, div = self._season_two_leagues_division()
        res = self.api.assign_division_league(div["id"], l2["id"], actor_id=ADMIN,
                                              v2=True)
        self.assertNotIn("error", res, res)
        self.assertEqual(res["league_id"], l2["id"])


class AssignDivisionPreservesLeagueTest(_Base):
    def _reg_in_league(self):
        org, program = self._org_program()
        season = self.api.create_season(program["id"], "Fall", actor_id=ADMIN)
        l1 = self.api.create_league(season["id"], "L1", actor_id=ADMIN)
        l2 = self.api.create_league(season["id"], "L2", actor_id=ADMIN)
        d1 = self.api.create_division_v2(l1["id"], "D1", actor_id=ADMIN)
        d2 = self.api.create_division_v2(l2["id"], "D2", actor_id=ADMIN)
        club = self.api.create_club("C", actor_id=ADMIN)
        team = self.api.create_team(club["id"], None, "T", actor_id=ADMIN,
                                    program_id=program["id"])
        reg = self.api.register_team_for_season(season["id"], team["id"], d1["id"],
                                                actor_id=ADMIN, league_id=l1["id"])
        return season, l1, l2, d1, d2, reg

    def test_clearing_division_preserves_required_league(self):
        season, l1, l2, d1, d2, reg = self._reg_in_league()
        cleared = self.api.assign_season_team_division(reg["id"], None,
                                                       actor_id=ADMIN, v2=True)
        self.assertNotIn("error", cleared, cleared)
        self.assertIsNone(cleared["division_id"])
        # The required League is PRESERVED, never nulled.
        self.assertEqual(cleared["league_id"], l1["id"])

    def test_setting_division_from_other_league_is_rejected(self):
        season, l1, l2, d1, d2, reg = self._reg_in_league()
        audits_before = self._audit_count()
        res = self.api.assign_season_team_division(reg["id"], d2["id"],
                                                   actor_id=ADMIN, v2=True)
        self.assertEqual(res["error"]["code"], "validation_error", res)
        # Zero mutation: still on d1, league unchanged, no audit.
        stored = self.api.store.get_season_team_registration(reg["id"])
        self.assertEqual(stored.division_id, d1["id"])
        self.assertEqual(stored.league_id, l1["id"])
        self.assertEqual(self._audit_count(), audits_before)


class AssignLeagueGameStrandTest(_Base):
    def test_league_change_blocked_when_committed_game_uses_old_league(self):
        org, program = self._org_program()
        season = self.api.create_season(program["id"], "Fall", actor_id=ADMIN)
        l1 = self.api.create_league(season["id"], "L1", actor_id=ADMIN)
        l2 = self.api.create_league(season["id"], "L2", actor_id=ADMIN)
        club = self.api.create_club("C", actor_id=ADMIN)
        team_a = self.api.create_team(club["id"], None, "A", actor_id=ADMIN,
                                      program_id=program["id"])
        team_b = self.api.create_team(club["id"], None, "B", actor_id=ADMIN,
                                      program_id=program["id"])
        reg_a = self.api.register_team_for_season(season["id"], team_a["id"],
                                                  actor_id=ADMIN, league_id=l1["id"])
        self.api.register_team_for_season(season["id"], team_b["id"],
                                          actor_id=ADMIN, league_id=l1["id"])
        venue = self.api.create_venue("V", organization_id=org["id"],
                                      league_id=program["id"], actor_id=ADMIN)
        rink = self.api.create_rink(venue["id"], "R", actor_id=ADMIN)
        slot = self.api.create_ice_slot(
            rink["id"], "2026-09-01T18:30:00+00:00",
            "2026-09-01T20:00:00+00:00", "game", actor_id=ADMIN)
        game = self.api.create_game(season["id"], None, team_a["id"],
                                    team_b["id"], slot["id"], actor_id=ADMIN,
                                    league_id=l1["id"])
        self.assertNotIn("error", game, game)

        audits_before = self._audit_count()
        res = self.api.assign_season_team_league(reg_a["id"], l2["id"],
                                                 actor_id=ADMIN)
        self.assertEqual(res["error"]["code"], "validation_error", res)
        # Zero mutation: registration still in L1, no audit written.
        self.assertEqual(
            self.api.store.get_season_team_registration(reg_a["id"]).league_id,
            l1["id"])
        self.assertEqual(self._audit_count(), audits_before)

    def test_league_change_allowed_without_committed_games(self):
        org, program = self._org_program()
        season = self.api.create_season(program["id"], "Fall", actor_id=ADMIN)
        l1 = self.api.create_league(season["id"], "L1", actor_id=ADMIN)
        l2 = self.api.create_league(season["id"], "L2", actor_id=ADMIN)
        club = self.api.create_club("C", actor_id=ADMIN)
        team = self.api.create_team(club["id"], None, "T", actor_id=ADMIN,
                                    program_id=program["id"])
        reg = self.api.register_team_for_season(season["id"], team["id"],
                                                actor_id=ADMIN, league_id=l1["id"])
        moved = self.api.assign_season_team_league(reg["id"], l2["id"],
                                                   actor_id=ADMIN)
        self.assertNotIn("error", moved, moved)
        self.assertEqual(moved["league_id"], l2["id"])


if __name__ == "__main__":
    unittest.main()
