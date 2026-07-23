"""Permanent teams + season registrations via the canonical import (#180, #260).

Covers the parts of the nine-sheet hierarchy import ("permanent_teams" and
"registrations" sheets) that are specific to team/registration lifecycle:
idempotent convergence, the #260 canonical registration format (explicit
``league_code``, optional ``division_code``), and the reassignment-safety
preflight (#214 review, extended #260) that blocks a program/league/
division move or a venue-access revoke that would strand committed games or
orphan registration history. Fixtures live in ``hierarchy_fixtures.py``.
"""

import contextlib
import os
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401

import hierarchy_fixtures as fx
from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Division, Game, League, LeagueSeason, Season
from hockey_scheduler.store import InMemoryStore, SqlStore

by_ref = fx.by_ref

BASE = dict(
    organizations_csv=fx.organizations_csv(),
    programs_csv=fx.programs_csv(),
    competition_csv=fx.competition_csv(),
    clubs_csv="",
)


def base_payload(**overrides):
    body = {"import_type": "hierarchy", **BASE}
    body.update(overrides)
    return body


class ImportConvergenceContract:
    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()
        self.api = ApiService(self.store)

    def tearDown(self):
        conn = getattr(self.store, "conn", None)
        if conn is not None:
            conn.close()

    def _team(self, code):
        return by_ref(self.store.all_teams(), code)

    def _commit_base(self, **overrides):
        result = self.api.commit_hierarchy_import(
            base_payload(**overrides), actor_id="admin")
        self.assertTrue(result["committed"], result.get("errors"))
        return result

    def test_dry_run_counts_teams_and_registrations_no_writes(self):
        result = self.api.get_hierarchy_import_dry_run(base_payload(
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,LIONS,Lions,", "OVER55,L1,BEARS,Bears,")),
            registrations_csv=fx.registrations_csv(rows=(
                "FALL26,LIONS,L1,DIVA", "FALL26,BEARS,L1,DIVA"))))
        self.assertTrue(result["ok"], result.get("errors"))
        self.assertEqual(result["entities"]["permanent_teams"], 2)
        self.assertEqual(result["entities"]["registrations"], 2)
        self.assertEqual(self.store.all_teams(), [])

    def test_commit_creates_permanent_teams_and_registrations(self):
        self._commit_base(
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,LIONS,Lions,", "OVER55,L1,BEARS,Bears,")),
            registrations_csv=fx.registrations_csv(rows=(
                "FALL26,LIONS,L1,DIVA", "FALL26,BEARS,L1,DIVA")))
        lions = self._team("LIONS")
        program = by_ref(self.store.all_programs(), "OVER55")
        # Permanent program team: no season/division on the Team itself.
        self.assertEqual(lions.program_id, program.id)
        self.assertIsNone(lions.club_id)  # blank club_code (#260 decision 1)
        season = by_ref(self.store.all_seasons(), "FALL26")
        league = by_ref(self.store.all_leagues(), "L1")
        diva = by_ref(self.store.all_divisions(), "DIVA")
        reg = self.store.registration_for_team_in_season(season.id, lions.id)
        self.assertIsNotNone(reg)
        self.assertEqual(
            self.store.get_league_season(reg.league_season_id).league_id,
            league.id)
        self.assertEqual(reg.division_id, diva.id)
        self.assertTrue(reg.active)

    def test_reimport_is_idempotent(self):
        payload = base_payload(
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,LIONS,Lions,", "OVER55,L1,BEARS,Bears,")),
            registrations_csv=fx.registrations_csv(rows=(
                "FALL26,LIONS,L1,DIVA", "FALL26,BEARS,L1,DIVA")))
        self.api.commit_hierarchy_import(payload, actor_id="admin")
        result = self.api.commit_hierarchy_import(payload, actor_id="admin")
        self.assertEqual(result["summary"]["permanent_teams"]["created"], 0)
        self.assertEqual(result["summary"]["permanent_teams"]["skipped"], 2)
        self.assertEqual(result["summary"]["registrations"]["skipped"], 2)
        self.assertEqual(len(self.store.all_teams()), 2)
        self.assertEqual(len(self.store.all_season_team_registrations()), 2)

    def test_reimport_moves_registration_league_and_division_in_place(self):
        self._commit_base(
            competition_csv=fx.competition_csv(rows=(
                "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult",
                "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVB,Division B,Adult",
                "OVER55,FALL26,Fall 2026,L2,B League,2,,,")),
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,LIONS,Lions,",)),
            registrations_csv=fx.registrations_csv(rows=(
                "FALL26,LIONS,L1,DIVA",)))
        moved = base_payload(
            competition_csv=fx.competition_csv(rows=(
                "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult",
                "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVB,Division B,Adult",
                "OVER55,FALL26,Fall 2026,L2,B League,2,,,")),
            # #283 Slice E: a registration may only use the team's permanent
            # League, so moving the registration to L2 requires moving the
            # permanent team to L2 too.
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L2,LIONS,Lions,",)),
            registrations_csv=fx.registrations_csv(rows=("FALL26,LIONS,L2,",)))
        result = self.api.commit_hierarchy_import(moved, actor_id="admin")
        self.assertTrue(result["committed"], result.get("errors"))
        # #283 Slice E: moving the permanent team to L2 cascades its game-free
        # current registration to L2 (clearing the L1-only division) through the
        # team transfer, so the move is tallied on the permanent-team bucket and
        # the registrations sheet is left a no-op skip. The end state (single
        # registration now in L2 with no division) is asserted below.
        self.assertEqual(result["summary"]["permanent_teams"]["updated"], 1)
        self.assertEqual(result["summary"]["registrations"]["skipped"], 1)
        self.assertEqual(len(self.store.all_season_team_registrations()), 1)
        season = by_ref(self.store.all_seasons(), "FALL26")
        l2 = by_ref(self.store.all_leagues(), "L2")
        reg = self.store.registration_for_team_in_season(
            season.id, self._team("LIONS").id)
        self.assertEqual(
            self.store.get_league_season(reg.league_season_id).league_id, l2.id)
        self.assertIsNone(reg.division_id)

    def test_incremental_team_import_against_existing_program(self):
        self._commit_base()
        teams_only = {"import_type": "hierarchy", "permanent_teams_csv":
                      fx.permanent_teams_csv(rows=("OVER55,L1,PUMAS,Pumas,",))}
        result = self.api.commit_hierarchy_import(teams_only, actor_id="admin")
        self.assertTrue(result["committed"], result.get("errors"))
        self.assertEqual(result["summary"]["permanent_teams"]["created"], 1)
        self.assertEqual(
            self._team("PUMAS").program_id,
            by_ref(self.store.all_programs(), "OVER55").id)

    def test_imported_registered_team_is_schedulable(self):
        # End-to-end: an imported permanent team, once registered, passes the
        # scheduling guard and can be given a game.
        self._commit_base(
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,LIONS,Lions,", "OVER55,L1,BEARS,Bears,")),
            registrations_csv=fx.registrations_csv(rows=(
                "FALL26,LIONS,L1,DIVA", "FALL26,BEARS,L1,DIVA")))
        season = by_ref(self.store.all_seasons(), "FALL26")
        diva = by_ref(self.store.all_divisions(), "DIVA")
        venue = self.api.create_venue("Ice")
        self.api.grant_season_venue_access(season.id, venue["id"])
        rink = self.api.create_rink(venue["id"], "R1")
        slot = self.api.create_ice_slot(
            rink["id"], "2026-11-01T18:00:00+00:00", "2026-11-01T20:00:00+00:00")
        game = self.api.create_game(
            season.id, diva.id, self._team("LIONS").id, self._team("BEARS").id,
            slot["id"])
        self.assertNotIn("error", game)

    # -- #214/#260 review: import must not bypass reassignment safety ------
    def _commit_game(self):
        """Commit LIONS vs BEARS registered in FALL26/DIVA, then schedule a
        committed game between them. Returns the game id."""
        self._commit_base(
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,LIONS,Lions,", "OVER55,L1,BEARS,Bears,")),
            registrations_csv=fx.registrations_csv(rows=(
                "FALL26,LIONS,L1,DIVA", "FALL26,BEARS,L1,DIVA")))
        season = by_ref(self.store.all_seasons(), "FALL26")
        diva = by_ref(self.store.all_divisions(), "DIVA")
        venue = self.api.create_venue("Ice")
        self.api.grant_season_venue_access(season.id, venue["id"])
        rink = self.api.create_rink(venue["id"], "R1")
        slot = self.api.create_ice_slot(
            rink["id"], "2026-11-01T18:00:00+00:00", "2026-11-01T20:00:00+00:00")
        game = self.api.create_game(
            season.id, diva.id, self._team("LIONS").id, self._team("BEARS").id,
            slot["id"])
        return game["id"]

    def test_import_cannot_strand_games_by_moving_registration_division(self):
        game_id = self._commit_game()
        season = by_ref(self.store.all_seasons(), "FALL26")
        diva = by_ref(self.store.all_divisions(), "DIVA")
        lions = self._team("LIONS")
        audits_before = len(self.store.all_setup_audit())
        moved = base_payload(
            competition_csv=fx.competition_csv(),
            registrations_csv=fx.registrations_csv(rows=(
                "FALL26,LIONS,L1,DIVB",)))
        result = self.api.commit_hierarchy_import(moved, actor_id="admin")
        self.assertFalse(result["committed"])
        err = result["errors"][0]
        self.assertEqual(err["code"], "registration_division_move_strands_games")
        self.assertIn(game_id, err["affected_game_ids"])
        self.assertEqual(
            self.store.registration_for_team_in_season(season.id, lions.id)
            .division_id, diva.id)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_import_cannot_strand_a_committed_draft_by_moving_division(self):
        # #314 review: a committed (is_draft=True) draft game now counts as
        # stranding in the import's reassignment-safety preflight, exactly
        # like a published game — built directly (bypassing create_game/
        # commit_draft_schedule) since only the flag, not the commit path, is
        # relevant to this guard.
        self._commit_base(
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,LIONS,Lions,", "OVER55,L1,BEARS,Bears,")),
            registrations_csv=fx.registrations_csv(rows=(
                "FALL26,LIONS,L1,DIVA", "FALL26,BEARS,L1,DIVA")))
        season = by_ref(self.store.all_seasons(), "FALL26")
        diva = by_ref(self.store.all_divisions(), "DIVA")
        lions = self._team("LIONS")
        bears = self._team("BEARS")
        reg = self.store.registration_for_team_in_season(season.id, lions.id)
        game = Game(
            id=self.store.next_id("game"), home_team_id=lions.id,
            away_team_id=bears.id,
            start_time=datetime(2026, 11, 1, 18, tzinfo=timezone.utc),
            season_id=season.id, division_id=diva.id,
            league_season_id=reg.league_season_id, is_draft=True,
            published=False)
        self.store.add_game(game)
        audits_before = len(self.store.all_setup_audit())
        moved = base_payload(
            competition_csv=fx.competition_csv(),
            registrations_csv=fx.registrations_csv(rows=(
                "FALL26,LIONS,L1,DIVB",)))
        result = self.api.commit_hierarchy_import(moved, actor_id="admin")
        self.assertFalse(result["committed"])
        err = result["errors"][0]
        self.assertEqual(err["code"], "registration_division_move_strands_games")
        self.assertIn(game.id, err["affected_game_ids"])
        self.assertEqual(
            self.store.registration_for_team_in_season(season.id, lions.id)
            .division_id, diva.id)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_import_cannot_strand_games_by_moving_registration_league(self):
        game_id = self._commit_game()
        season = by_ref(self.store.all_seasons(), "FALL26")
        league = by_ref(self.store.all_leagues(), "L1")
        lions = self._team("LIONS")
        audits_before = len(self.store.all_setup_audit())
        moved = base_payload(
            competition_csv=fx.competition_csv(rows=(
                "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult",
                "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVB,Division B,Adult",
                "OVER55,FALL26,Fall 2026,L2,B League,2,,,")),
            # Move the permanent team to L2 as well, so the registration's
            # league_code is valid (matches the team's permanent League) and
            # the commit reaches the strand preflight instead of being
            # rejected earlier by registration_league_not_team_league.
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L2,LIONS,Lions,",)),
            registrations_csv=fx.registrations_csv(rows=("FALL26,LIONS,L2,",)))
        result = self.api.commit_hierarchy_import(moved, actor_id="admin")
        self.assertFalse(result["committed"])
        err = next(e for e in result["errors"]
                  if e["code"] == "registration_league_move_strands_games")
        self.assertIn(game_id, err["affected_game_ids"])
        moved_reg = self.store.registration_for_team_in_season(season.id, lions.id)
        self.assertEqual(
            self.store.get_league_season(moved_reg.league_season_id).league_id,
            league.id)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_import_cannot_move_team_program_while_registrations_remain(self):
        self._commit_base(
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,LIONS,Lions,",)),
            registrations_csv=fx.registrations_csv(rows=("FALL26,LIONS,L1,DIVA",)))
        second = base_payload(
            programs_csv=fx.programs_csv(rows=(
                "OVER55,CANLON,Over 55,US,America/Chicago",
                "OTHER,CANLON,Other,US,America/Chicago")),
            competition_csv=fx.competition_csv(rows=(
                "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult",
                "OTHER,OSEA,Other,O1,Other League,1,,,")))
        self.assertTrue(
            self.api.commit_hierarchy_import(second, actor_id="admin")["committed"])
        over55 = by_ref(self.store.all_programs(), "OVER55")
        season = by_ref(self.store.all_seasons(), "FALL26")
        lions = self._team("LIONS")
        reg = self.store.registration_for_team_in_season(season.id, lions.id)
        audits_before = len(self.store.all_setup_audit())
        move = {"import_type": "hierarchy", "permanent_teams_csv":
                fx.permanent_teams_csv(rows=("OTHER,O1,LIONS,Lions,",))}
        result = self.api.commit_hierarchy_import(move, actor_id="admin")
        self.assertFalse(result["committed"])
        err = result["errors"][0]
        self.assertEqual(err["code"], "team_program_move_strands_history")
        self.assertIn(reg.id, err["affected_registration_ids"])
        self.assertEqual(self._team("LIONS").program_id, over55.id)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_inactive_registration_blocks_team_program_move(self):
        # An INACTIVE prior-season registration is still a retained
        # dependency: moving the team's program would make a later
        # reactivation cross-program, so the import is rejected with zero
        # writes.
        self._commit_base(
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,LIONS,Lions,",)),
            registrations_csv=fx.registrations_csv(rows=("FALL26,LIONS,L1,DIVA",)))
        second = base_payload(
            programs_csv=fx.programs_csv(rows=(
                "OVER55,CANLON,Over 55,US,America/Chicago",
                "OTHER,CANLON,Other,US,America/Chicago")),
            # Give OTHER a League so the team's post-move permanent League
            # (O1) resolves cleanly and the commit reaches the strand
            # preflight rather than failing on unknown_league_code.
            competition_csv=fx.competition_csv(rows=(
                "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult",
                "OTHER,OSEA,Other,O1,Other League,1,,,")))
        self.assertTrue(
            self.api.commit_hierarchy_import(second, actor_id="admin")["committed"])
        season = by_ref(self.store.all_seasons(), "FALL26")
        over55 = by_ref(self.store.all_programs(), "OVER55")
        lions = self._team("LIONS")
        reg = self.store.registration_for_team_in_season(season.id, lions.id)
        reg.active = False
        self.store.save_season_team_registration(reg)
        audits_before = len(self.store.all_setup_audit())
        move = {"import_type": "hierarchy", "permanent_teams_csv":
                fx.permanent_teams_csv(rows=("OTHER,O1,LIONS,Lions,",))}
        result = self.api.commit_hierarchy_import(move, actor_id="admin")
        self.assertFalse(result["committed"])
        err = next(e for e in result["errors"]
                  if e["code"] == "team_program_move_strands_history")
        self.assertIn(reg.id, err["affected_registration_ids"])
        self.assertEqual(self._team("LIONS").program_id, over55.id)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_safe_registration_move_records_exact_from_and_to(self):
        # No games scheduled -> moving LIONS to DIVB is safe and audited.
        self._commit_base(
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,LIONS,Lions,",)),
            registrations_csv=fx.registrations_csv(rows=("FALL26,LIONS,L1,DIVA",)))
        season = by_ref(self.store.all_seasons(), "FALL26")
        diva = by_ref(self.store.all_divisions(), "DIVA")
        divb = by_ref(self.store.all_divisions(), "DIVB")
        moved = base_payload(
            competition_csv=fx.competition_csv(),
            registrations_csv=fx.registrations_csv(rows=("FALL26,LIONS,L1,DIVB",)))
        result = self.api.commit_hierarchy_import(moved, actor_id="admin")
        self.assertTrue(result["committed"], result.get("errors"))
        self.assertEqual(result["summary"]["registrations"]["updated"], 1)
        reg = self.store.registration_for_team_in_season(
            season.id, self._team("LIONS").id)
        entry = next(a for a in self.store.all_setup_audit()
                    if a.action == "season_team_registration_updated"
                    and a.entity_id == reg.id)
        self.assertEqual(entry.detail["from_division_id"], diva.id)
        self.assertEqual(entry.detail["to_division_id"], divb.id)

    def test_safe_team_program_move_records_exact_from_and_to(self):
        # PUMAS has no registrations, so moving its program is safe/audited.
        self._commit_base(
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,PUMAS,Pumas,",)))
        over55 = by_ref(self.store.all_programs(), "OVER55")
        pumas = self._team("PUMAS")
        move = base_payload(
            programs_csv=fx.programs_csv(rows=(
                "OVER55,CANLON,Over 55,US,America/Chicago",
                "OTHER,CANLON,Other,US,America/Chicago")),
            # OTHER needs a League for PUMAS's new permanent League to resolve.
            competition_csv=fx.competition_csv(rows=(
                "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult",
                "OTHER,OSEA,Other,O1,Other League,1,,,")),
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OTHER,O1,PUMAS,Pumas,",)))
        result = self.api.commit_hierarchy_import(move, actor_id="admin")
        self.assertTrue(result["committed"], result.get("errors"))
        other = by_ref(self.store.all_programs(), "OTHER")
        entry = next(a for a in self.store.all_setup_audit()
                    if a.action == "team_updated" and a.entity_id == pumas.id)
        self.assertEqual(entry.detail["from_program_id"], over55.id)
        self.assertEqual(entry.detail["to_program_id"], other.id)

    def test_import_cannot_move_season_program_with_registrations(self):
        self._commit_base(
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,LIONS,Lions,",)),
            registrations_csv=fx.registrations_csv(rows=("FALL26,LIONS,L1,DIVA",)))
        second = base_payload(
            programs_csv=fx.programs_csv(rows=(
                "OVER55,CANLON,Over 55,US,America/Chicago",
                "OTHER,CANLON,Other,US,America/Chicago")))
        self.assertTrue(
            self.api.commit_hierarchy_import(second, actor_id="admin")["committed"])
        season = by_ref(self.store.all_seasons(), "FALL26")
        over55 = by_ref(self.store.all_programs(), "OVER55")
        audits_before = len(self.store.all_setup_audit())
        moved = {"import_type": "hierarchy", "competition_csv":
                 fx.competition_csv(rows=(
                     "OTHER,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult",
                     "OTHER,FALL26,Fall 2026,L1,Adult League,1,DIVB,Division B,Adult"))}
        result = self.api.commit_hierarchy_import(moved, actor_id="admin")
        self.assertFalse(result["committed"])
        err = next(e for e in result["errors"]
                  if e["code"] == "season_program_move_strands_history")
        self.assertTrue(err["affected_registration_ids"])
        self.assertEqual(self.store.get_season(season.id).program_id, over55.id)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_division_season_move_blocked_by_cancelled_game(self):
        game_id = self._commit_game()
        self.api.cancel_game(game_id, actor_id="admin")
        second = {"import_type": "hierarchy", "competition_csv":
                  fx.competition_csv(rows=(
                      "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult",
                      "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVB,Division B,Adult",
                      "OVER55,SPRING,Spring,L2,Spring League,1,DIVC,Division C,Adult"))}
        self.assertTrue(
            self.api.commit_hierarchy_import(second, actor_id="admin")["committed"])
        diva = by_ref(self.store.all_divisions(), "DIVA")
        fall = by_ref(self.store.all_seasons(), "FALL26")
        for reg in list(self.store.all_season_team_registrations()):
            if reg.division_id == diva.id:
                reg.active = False
                self.store.save_season_team_registration(reg)
        audits_before = len(self.store.all_setup_audit())
        moved = {"import_type": "hierarchy", "competition_csv":
                 fx.competition_csv(rows=(
                     "OVER55,SPRING,Spring,L2,Spring League,1,DIVA,Division A,Adult",))}
        result = self.api.commit_hierarchy_import(moved, actor_id="admin")
        self.assertFalse(result["committed"])
        err = next(e for e in result["errors"]
                  if e["code"] == "division_season_move_strands_dependents")
        self.assertIn(game_id, err["affected_game_ids"])
        self.assertTrue(err["affected_registration_ids"])
        diva_ls = self.store.get_league_season(
            self.store.get_division(diva.id).league_season_id)
        self.assertEqual(diva_ls.season_id, fall.id)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_inactive_registration_with_committed_game_cannot_move_division(self):
        game_id = self._commit_game()
        season = by_ref(self.store.all_seasons(), "FALL26")
        diva = by_ref(self.store.all_divisions(), "DIVA")
        lions = self._team("LIONS")
        reg = self.store.registration_for_team_in_season(season.id, lions.id)
        reg.active = False
        self.store.save_season_team_registration(reg)
        audits_before = len(self.store.all_setup_audit())
        moved = base_payload(
            competition_csv=fx.competition_csv(),
            registrations_csv=fx.registrations_csv(rows=("FALL26,LIONS,L1,DIVB",)))
        result = self.api.commit_hierarchy_import(moved, actor_id="admin")
        self.assertFalse(result["committed"])
        err = next(e for e in result["errors"]
                  if e["code"] == "registration_division_move_strands_games")
        self.assertIn(game_id, err["affected_game_ids"])
        reg2 = self.store.registration_for_team_in_season(season.id, lions.id)
        self.assertFalse(reg2.active)
        self.assertEqual(reg2.division_id, diva.id)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_inactive_registration_with_no_games_can_reactivate_and_move(self):
        self._commit_base(
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,LIONS,Lions,",)),
            registrations_csv=fx.registrations_csv(rows=("FALL26,LIONS,L1,DIVA",)))
        season = by_ref(self.store.all_seasons(), "FALL26")
        divb = by_ref(self.store.all_divisions(), "DIVB")
        lions = self._team("LIONS")
        reg = self.store.registration_for_team_in_season(season.id, lions.id)
        reg.active = False
        self.store.save_season_team_registration(reg)
        moved = base_payload(
            competition_csv=fx.competition_csv(),
            registrations_csv=fx.registrations_csv(rows=("FALL26,LIONS,L1,DIVB",)))
        result = self.api.commit_hierarchy_import(moved, actor_id="admin")
        self.assertTrue(result["committed"], result.get("errors"))
        reg2 = self.store.registration_for_team_in_season(season.id, lions.id)
        self.assertTrue(reg2.active)
        self.assertEqual(reg2.division_id, divb.id)

    # -- season_venue_access revoke reassignment safety (#260, new) --------
    def test_season_venue_access_revoke_cannot_strand_games(self):
        # Import the venue+access through the hierarchy sheets (not
        # api.create_venue) so it carries the stable external_ref the
        # season_venue_access sheet keys off of.
        self._commit_base(
            venues_rinks_csv=fx.venues_rinks_csv(rows=(
                "PLAINFIELD,CANLON,Plainfield Ice,123 Main St,"
                "America/Chicago,PF1,Rink 1",)),
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,LIONS,Lions,", "OVER55,L1,BEARS,Bears,")),
            registrations_csv=fx.registrations_csv(rows=(
                "FALL26,LIONS,L1,DIVA", "FALL26,BEARS,L1,DIVA")),
            season_venue_access_csv=fx.season_venue_access_csv())
        season = by_ref(self.store.all_seasons(), "FALL26")
        diva = by_ref(self.store.all_divisions(), "DIVA")
        venue = by_ref(self.store.all_venues(), "PLAINFIELD")
        rink = self.store.all_rinks()[0]
        slot = self.api.create_ice_slot(
            rink.id, "2026-11-01T18:00:00+00:00", "2026-11-01T20:00:00+00:00")
        game = self.api.create_game(
            season.id, diva.id, self._team("LIONS").id, self._team("BEARS").id,
            slot["id"])
        game_id = game["id"]
        audits_before = len(self.store.all_setup_audit())
        revoke = {"import_type": "hierarchy", "season_venue_access_csv":
                  "season_code,venue_code,active\nFALL26,PLAINFIELD,false\n"}
        result = self.api.commit_hierarchy_import(revoke, actor_id="admin")
        self.assertFalse(result["committed"])
        err = next(e for e in result["errors"]
                  if e["code"] == "season_venue_access_revoke_strands_games")
        self.assertIn(game_id, err["affected_game_ids"])
        self.assertTrue(self.store.season_venue_access_for_pair(
            season.id, venue.id).active)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_preflight_runs_inside_the_transaction(self):
        # Prove the preflight's game read happens while the transaction is
        # held, so there is no check->write gap under the threaded server.
        self._commit_base(
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,LIONS,Lions,", "OVER55,L1,BEARS,Bears,")),
            registrations_csv=fx.registrations_csv(rows=(
                "FALL26,LIONS,L1,DIVA", "FALL26,BEARS,L1,DIVA")))
        state = {"depth": 0, "preflight_inside": None}
        orig_txn = self.store.transaction

        @contextlib.contextmanager
        def tracking_txn():
            state["depth"] += 1
            try:
                with orig_txn():
                    yield
            finally:
                state["depth"] -= 1

        orig_all_games = self.store.all_games

        def hooked_all_games():
            if state["preflight_inside"] is None:
                state["preflight_inside"] = state["depth"] > 0
            return orig_all_games()

        self.store.transaction = tracking_txn
        self.store.all_games = hooked_all_games
        moved = base_payload(
            competition_csv=fx.competition_csv(),
            registrations_csv=fx.registrations_csv(rows=(
                "FALL26,LIONS,L1,DIVB", "FALL26,BEARS,L1,DIVA")))
        self.api.commit_hierarchy_import(moved, actor_id="admin")
        self.assertTrue(state["preflight_inside"],
                        "preflight game read ran outside the transaction lock")


class MemoryImportConvergenceTest(ImportConvergenceContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurableImportConvergenceTest(ImportConvergenceContract, unittest.TestCase):
    def make_store(self):
        url = os.environ.get("TEST_DATABASE_URL") or ":memory:"
        store = SqlStore(url)
        store.reset_schema()
        return store


class ImportConvergenceValidationTest(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.api = ApiService(self.store)

    def _dry(self, body):
        return self.api.get_hierarchy_import_dry_run(body)

    def _has(self, result, needle):
        return any(needle in e["message"] for e in result["errors"])

    def test_unknown_program_on_team_rejected(self):
        result = self._dry(base_payload(
            permanent_teams_csv=
            "program_code,league_code,team_code,team_name,club_code\n"
            "NOPE,L1,X,X,\n"))
        self.assertFalse(result["ok"])
        self.assertTrue(self._has(result, "Unknown program_code NOPE"))

    def test_registration_unknown_codes_rejected(self):
        result = self._dry(base_payload(
            registrations_csv=fx.registrations_csv(
                rows=("NOSEASON,LIONS,L1,DIVA",))))
        self.assertFalse(result["ok"])
        self.assertTrue(self._has(result, "Unknown season_code NOSEASON"))

    def test_division_not_in_league_rejected(self):
        comp = fx.competition_csv(rows=(
            "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult",
            "OVER55,FALL26,Fall 2026,L2,B League,2,DIVB,Division B,Adult"))
        result = self._dry(base_payload(
            competition_csv=comp,
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,LIONS,Lions,",)),
            registrations_csv=fx.registrations_csv(
                rows=("FALL26,LIONS,L1,DIVB",))))
        self.assertFalse(result["ok"])
        self.assertTrue(self._has(result, "does not belong to"))

    def test_cross_program_registration_rejected(self):
        programs = fx.programs_csv(rows=(
            "OVER55,CANLON,Over 55,US,America/Chicago",
            "OTHER,CANLON,Other,US,America/Chicago"))
        comp = fx.competition_csv(rows=(
            "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult",
            "OTHER,OSEA,Other,O1,Other League,1,,,"))
        result = self._dry(base_payload(
            programs_csv=programs, competition_csv=comp,
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,LIONS,Lions,",)),
            registrations_csv=fx.registrations_csv(rows=(
                "OSEA,LIONS,O1,",))))
        self.assertFalse(result["ok"])
        self.assertTrue(self._has(result, "cannot register"))

    def test_duplicate_registration_rejected(self):
        result = self._dry(base_payload(
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,LIONS,Lions,",)),
            registrations_csv=fx.registrations_csv(rows=(
                "FALL26,LIONS,L1,DIVA", "FALL26,LIONS,L1,DIVB"))))
        self.assertFalse(result["ok"])
        self.assertTrue(self._has(result, "Duplicate registration"))

    def test_duplicate_team_code_rejected(self):
        result = self._dry(base_payload(permanent_teams_csv=(
            "program_code,league_code,team_code,team_name,club_code\n"
            "OVER55,L1,LIONS,Lions,\n"
            "OVER55,L1,LIONS,Lions Again,\n")))
        self.assertFalse(result["ok"])
        self.assertTrue(self._has(result, "Duplicate team_code"))

    def test_failed_validation_writes_nothing(self):
        bad = base_payload(registrations_csv=fx.registrations_csv(
            rows=("NOSEASON,LIONS,L1,DIVA",)))
        result = self.api.commit_hierarchy_import(bad, actor_id="admin")
        self.assertFalse(result["committed"])
        self.assertEqual(self.store.all_teams(), [])
        self.assertEqual(self.store.all_season_team_registrations(), [])

    # -- reject null/dangling parent chains, with zero writes --------------
    def test_null_team_program_registration_rejected(self):
        self.api.commit_hierarchy_import(base_payload(), actor_id="admin")
        from hockey_scheduler.domain import Team
        self.store.add_team(Team(id="team_nl", name="NoProgram",
                                 external_ref="NOLG", program_id=None))
        reg = {"import_type": "hierarchy", "registrations_csv":
               "season_code,team_code,league_code,division_code\n"
               "FALL26,NOLG,L1,DIVA\n"}
        result = self._dry(reg)
        self.assertFalse(result["ok"])
        self.assertTrue(self._has(result, "cannot register"))
        before = len(self.store.all_season_team_registrations())
        self.assertFalse(
            self.api.commit_hierarchy_import(reg, actor_id="admin")["committed"])
        self.assertEqual(
            len(self.store.all_season_team_registrations()), before)

    def test_null_season_program_registration_rejected(self):
        self.api.commit_hierarchy_import(base_payload(
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,LIONS,Lions,",))), actor_id="admin")
        # A season with no program (program_id="") plus its own league/
        # division, so the league_code itself resolves cleanly to this
        # season — isolating the failure to the season<->team program
        # mismatch rather than an unrelated league/season mismatch.
        self.store.add_season(Season(id="se_nl", program_id="", name="NL",
                                     external_ref="SNL"))
        self.store.add_league(League(id="lg_nl", program_id="",
                                     name="NL League", external_ref="LNL"))
        self.store.add_league_season(LeagueSeason(
            id="ls_nl", league_id="lg_nl", season_id="se_nl"))
        self.store.add_division(Division(id="d_nl", league_season_id="ls_nl",
                                         name="D", external_ref="DNL"))
        reg = {"import_type": "hierarchy", "registrations_csv":
               "season_code,team_code,league_code,division_code\n"
               "SNL,LIONS,LNL,DNL\n"}
        result = self._dry(reg)
        self.assertFalse(result["ok"])
        self.assertTrue(self._has(result, "cannot register"))

    def test_dangling_division_league_registration_rejected(self):
        self.api.commit_hierarchy_import(base_payload(
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,LIONS,Lions,",))), actor_id="admin")
        self.store.add_division(Division(id="d_dangle", league_season_id="missing",
                                         name="D", external_ref="DDG"))
        reg = {"import_type": "hierarchy", "registrations_csv":
               "season_code,team_code,league_code,division_code\n"
               "FALL26,LIONS,L1,DDG\n"}
        result = self._dry(reg)
        self.assertFalse(result["ok"])
        self.assertTrue(self._has(result, "does not belong to"))


if __name__ == "__main__":
    unittest.main()
