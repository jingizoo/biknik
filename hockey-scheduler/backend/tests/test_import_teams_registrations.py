"""Team + season-registration import convergence (#180).

The code-keyed hierarchy import gains two sheets so the CSV path builds the
#180 model directly: permanent league teams (keyed by team_code, owned by a
league, carrying no division) and season registrations (keyed by
(season_code, team_code), assigning that season's division). Validation reuses
the hierarchy import's reference checks and enforces the #200 invariants — a
registration's division must belong to its season and the team's league must
match the season's league. Commit is idempotent and single-transaction.
"""

import contextlib
import os
import unittest

from helpers import BACKEND  # noqa: F401

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Club, Division, Season, SeasonTeamRegistration, Team)
from hockey_scheduler.store import InMemoryStore, SqlStore

ORGANIZATIONS = (
    "organization_code,organization_name,short_name\nCANLON,Canlon,Canlon\n")
LEAGUES = (
    "league_code,organization_code,league_name,country,timezone\n"
    "OVER55,CANLON,Over 55,US,America/Chicago\n")
COMPETITION = (
    "league_code,season_code,season_name,level_code,level_name,level_sort_order,"
    "division_code,division_name,age_group\n"
    "OVER55,FALL26,Fall 2026,,,,DIVA,Division A,Adult\n"
    "OVER55,FALL26,Fall 2026,,,,DIVB,Division B,Adult\n")
PERMANENT_TEAMS = (
    "league_code,team_code,team_name\n"
    "OVER55,LIONS,Lions\n"
    "OVER55,BEARS,Bears\n")
REGISTRATIONS = (
    "season_code,team_code,division_code\n"
    "FALL26,LIONS,DIVA\n"
    "FALL26,BEARS,DIVA\n")


def payload(**overrides):
    body = {
        "import_type": "hierarchy",
        "organizations_csv": ORGANIZATIONS,
        "leagues_csv": LEAGUES,
        "competition_csv": COMPETITION,
        "permanent_teams_csv": PERMANENT_TEAMS,
        "registrations_csv": REGISTRATIONS,
    }
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
        return next(t for t in self.store.all_teams() if t.external_ref == code)

    def _by_ref(self, rows, code):
        return next(r for r in rows if r.external_ref == code)

    def test_dry_run_counts_teams_and_registrations_no_writes(self):
        result = self.api.get_hierarchy_import_dry_run(payload())
        self.assertTrue(result["ok"], result.get("errors"))
        self.assertEqual(result["entities"]["permanent_teams"], 2)
        self.assertEqual(result["entities"]["registrations"], 2)
        self.assertEqual(self.store.all_teams(), [])

    def test_commit_creates_permanent_teams_and_registrations(self):
        result = self.api.commit_hierarchy_import(payload(), actor_id="admin")
        self.assertTrue(result["committed"], result.get("errors"))
        self.assertEqual(result["summary"]["permanent_teams"]["created"], 2)
        self.assertEqual(result["summary"]["registrations"]["created"], 2)
        lions = self._team("LIONS")
        league = self._by_ref(self.store.all_programs(), "OVER55")
        # Permanent league team: owned by the league, no division on the Team.
        self.assertEqual(lions.program_id, league.id)
        self.assertIsNone(lions.division_id)
        # Club association is deferred from this slice (#214 review): the team
        # imports with no club, and the import creates no Club records.
        self.assertIsNone(lions.club_id)
        self.assertEqual(self.store.all_clubs(), [])
        # Participation lives in the registration.
        season = self._by_ref(self.store.all_seasons(), "FALL26")
        diva = self._by_ref(self.store.all_divisions(), "DIVA")
        reg = self.store.registration_for_team_in_season(season.id, lions.id)
        self.assertIsNotNone(reg)
        self.assertEqual(reg.division_id, diva.id)
        self.assertTrue(reg.active)

    def test_reimport_is_idempotent(self):
        self.api.commit_hierarchy_import(payload(), actor_id="admin")
        result = self.api.commit_hierarchy_import(payload(), actor_id="admin")
        self.assertEqual(result["summary"]["permanent_teams"]["created"], 0)
        self.assertEqual(result["summary"]["permanent_teams"]["skipped"], 2)
        self.assertEqual(result["summary"]["registrations"]["skipped"], 2)
        self.assertEqual(len(self.store.all_teams()), 2)
        self.assertEqual(len(self.store.all_season_team_registrations()), 2)

    def test_reimport_moves_registration_division_in_place(self):
        self.api.commit_hierarchy_import(payload(), actor_id="admin")
        moved = payload(registrations_csv=(
            "season_code,team_code,division_code\n"
            "FALL26,LIONS,DIVB\n"
            "FALL26,BEARS,DIVA\n"))
        result = self.api.commit_hierarchy_import(moved, actor_id="admin")
        self.assertEqual(result["summary"]["registrations"]["updated"], 1)
        self.assertEqual(result["summary"]["registrations"]["skipped"], 1)
        # No duplicate row — the (season, team) registration was updated.
        self.assertEqual(len(self.store.all_season_team_registrations()), 2)
        season = self._by_ref(self.store.all_seasons(), "FALL26")
        divb = self._by_ref(self.store.all_divisions(), "DIVB")
        reg = self.store.registration_for_team_in_season(season.id, self._team("LIONS").id)
        self.assertEqual(reg.division_id, divb.id)

    def test_incremental_team_import_against_existing_league(self):
        base = {"import_type": "hierarchy", "organizations_csv": ORGANIZATIONS,
                "leagues_csv": LEAGUES, "competition_csv": COMPETITION}
        self.assertTrue(
            self.api.commit_hierarchy_import(base, actor_id="admin")["committed"])
        teams_only = {"import_type": "hierarchy", "permanent_teams_csv":
                      "league_code,team_code,team_name\nOVER55,PUMAS,Pumas\n"}
        result = self.api.commit_hierarchy_import(teams_only, actor_id="admin")
        self.assertTrue(result["committed"], result.get("errors"))
        self.assertEqual(result["summary"]["permanent_teams"]["created"], 1)
        self.assertEqual(
            self._team("PUMAS").program_id,
            self._by_ref(self.store.all_programs(), "OVER55").id)

    def test_imported_registered_team_is_schedulable(self):
        # End-to-end: an imported permanent team, once registered, passes the
        # #200 scheduling guard and can be given a game.
        self.api.commit_hierarchy_import(payload(), actor_id="admin")
        league = self._by_ref(self.store.all_programs(), "OVER55")
        season = self._by_ref(self.store.all_seasons(), "FALL26")
        diva = self._by_ref(self.store.all_divisions(), "DIVA")
        venue = self.api.create_venue("Ice", league_id=league.id)
        rink = self.api.create_rink(venue["id"], "R1")
        slot = self.api.create_ice_slot(
            rink["id"], "2026-11-01T18:00:00+00:00", "2026-11-01T20:00:00+00:00")
        game = self.api.create_game(
            season.id, diva.id, self._team("LIONS").id, self._team("BEARS").id,
            slot["id"])
        self.assertNotIn("error", game)

    # -- #214 review: import must not bypass the reassignment safety guards --
    def _commit_game(self):
        """Commit the base import, then schedule one committed LIONS vs BEARS
        game in FALL26/DIVA. Returns the game id."""
        self.api.commit_hierarchy_import(payload(), actor_id="admin")
        league = self._by_ref(self.store.all_programs(), "OVER55")
        season = self._by_ref(self.store.all_seasons(), "FALL26")
        diva = self._by_ref(self.store.all_divisions(), "DIVA")
        rink = self.api.create_rink(
            self.api.create_venue("Ice", league_id=league.id)["id"], "R1")
        slot = self.api.create_ice_slot(
            rink["id"], "2026-11-01T18:00:00+00:00", "2026-11-01T20:00:00+00:00")
        game = self.api.create_game(
            season.id, diva.id, self._team("LIONS").id, self._team("BEARS").id,
            slot["id"])
        return game["id"]

    def test_import_cannot_strand_games_by_moving_registration_division(self):
        game_id = self._commit_game()
        season = self._by_ref(self.store.all_seasons(), "FALL26")
        diva = self._by_ref(self.store.all_divisions(), "DIVA")
        lions = self._team("LIONS")
        audits_before = len(self.store.all_setup_audit())
        moved = payload(registrations_csv=(
            "season_code,team_code,division_code\nFALL26,LIONS,DIVB\n"))
        result = self.api.commit_hierarchy_import(moved, actor_id="admin")
        self.assertFalse(result["committed"])
        err = result["errors"][0]
        self.assertEqual(err["reason"], "registration_division_move_strands_games")
        self.assertIn(game_id, err["affected_game_ids"])
        # Atomic zero-write: registration still in DIVA, no new audits.
        self.assertEqual(
            self.store.registration_for_team_in_season(season.id, lions.id).division_id,
            diva.id)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_import_cannot_move_team_league_while_registrations_remain(self):
        self.api.commit_hierarchy_import(payload(), actor_id="admin")
        # A second league so LIONS has somewhere to be (wrongly) moved.
        second = {"import_type": "hierarchy", "organizations_csv": ORGANIZATIONS,
                  "leagues_csv": LEAGUES + "OTHER,CANLON,Other,US,America/Chicago\n",
                  "competition_csv":
                      COMPETITION + "OTHER,OSEA,Other,,,,ODIV,Other Div,Adult\n"}
        self.assertTrue(
            self.api.commit_hierarchy_import(second, actor_id="admin")["committed"])
        over55 = self._by_ref(self.store.all_programs(), "OVER55")
        season = self._by_ref(self.store.all_seasons(), "FALL26")
        lions = self._team("LIONS")
        reg = self.store.registration_for_team_in_season(season.id, lions.id)
        audits_before = len(self.store.all_setup_audit())
        move = {"import_type": "hierarchy", "permanent_teams_csv":
                "league_code,team_code,team_name\nOTHER,LIONS,Lions\n"}
        result = self.api.commit_hierarchy_import(move, actor_id="admin")
        self.assertFalse(result["committed"])
        err = result["errors"][0]
        self.assertEqual(err["reason"], "team_league_move_strands_history")
        self.assertIn(reg.id, err["affected_registration_ids"])
        # Atomic zero-write: LIONS still in OVER55, no new audits.
        self.assertEqual(self._team("LIONS").program_id, over55.id)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_inactive_registration_blocks_team_league_move(self):
        # An INACTIVE prior-season registration is still a retained dependency:
        # moving the team's league would make a later reactivation cross-league
        # (#214 review), so the import is rejected with zero writes.
        self.api.commit_hierarchy_import(payload(), actor_id="admin")
        second = {"import_type": "hierarchy", "organizations_csv": ORGANIZATIONS,
                  "leagues_csv": LEAGUES + "OTHER,CANLON,Other,US,America/Chicago\n"}
        self.assertTrue(
            self.api.commit_hierarchy_import(second, actor_id="admin")["committed"])
        season = self._by_ref(self.store.all_seasons(), "FALL26")
        over55 = self._by_ref(self.store.all_programs(), "OVER55")
        lions = self._team("LIONS")
        reg = self.store.registration_for_team_in_season(season.id, lions.id)
        reg.active = False  # historical registration
        self.store.save_season_team_registration(reg)
        audits_before = len(self.store.all_setup_audit())
        move = {"import_type": "hierarchy", "permanent_teams_csv":
                "league_code,team_code,team_name\nOTHER,LIONS,Lions\n"}
        result = self.api.commit_hierarchy_import(move, actor_id="admin")
        self.assertFalse(result["committed"])
        err = next(e for e in result["errors"]
                   if e["reason"] == "team_league_move_strands_history")
        self.assertIn(reg.id, err["affected_registration_ids"])
        self.assertEqual(self._team("LIONS").program_id, over55.id)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_legacy_division_does_not_block_team_league_move(self):
        # #180: a preserved legacy Team.division_id is no longer operational, so
        # it never strands a team's league move. With no registrations to strand,
        # the move commits and the team's permanent league changes.
        self.api.commit_hierarchy_import(payload(), actor_id="admin")
        over55 = self._by_ref(self.store.all_programs(), "OVER55")
        diva = self._by_ref(self.store.all_divisions(), "DIVA")
        self.store.add_team(Team(id="team_ld", name="Legacy", external_ref="LEGDIV",
                                 club_id=None, division_id=diva.id,
                                 division="Division A", program_id=over55.id))
        second = {"import_type": "hierarchy", "organizations_csv": ORGANIZATIONS,
                  "leagues_csv": LEAGUES + "OTHER,CANLON,Other,US,America/Chicago\n"}
        self.assertTrue(
            self.api.commit_hierarchy_import(second, actor_id="admin")["committed"])
        move = {"import_type": "hierarchy", "permanent_teams_csv":
                "league_code,team_code,team_name\nOTHER,LEGDIV,Legacy\n"}
        result = self.api.commit_hierarchy_import(move, actor_id="admin")
        self.assertTrue(result["committed"], result.get("errors"))
        other = self._by_ref(self.store.all_programs(), "OTHER")
        self.assertEqual(self._team("LEGDIV").program_id, other.id)

    def test_safe_registration_move_records_exact_from_and_to(self):
        # No games scheduled → moving LIONS DIVA -> DIVB is safe and audited.
        self.api.commit_hierarchy_import(payload(), actor_id="admin")
        season = self._by_ref(self.store.all_seasons(), "FALL26")
        diva = self._by_ref(self.store.all_divisions(), "DIVA")
        divb = self._by_ref(self.store.all_divisions(), "DIVB")
        moved = payload(registrations_csv=(
            "season_code,team_code,division_code\n"
            "FALL26,LIONS,DIVB\nFALL26,BEARS,DIVA\n"))
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

    def test_safe_team_league_move_records_exact_from_and_to(self):
        # PUMAS has no registrations, so moving its league is safe and audited.
        base = dict(payload())
        base["permanent_teams_csv"] = (
            PERMANENT_TEAMS + "OVER55,PUMAS,Pumas\n")
        self.api.commit_hierarchy_import(base, actor_id="admin")
        over55 = self._by_ref(self.store.all_programs(), "OVER55")
        pumas = self._team("PUMAS")
        two_league = {"import_type": "hierarchy", "organizations_csv": ORGANIZATIONS,
                      "leagues_csv":
                          LEAGUES + "OTHER,CANLON,Other,US,America/Chicago\n",
                      "permanent_teams_csv":
                          "league_code,team_code,team_name\nOTHER,PUMAS,Pumas\n"}
        result = self.api.commit_hierarchy_import(two_league, actor_id="admin")
        self.assertTrue(result["committed"], result.get("errors"))
        other = self._by_ref(self.store.all_programs(), "OTHER")
        entry = next(a for a in self.store.all_setup_audit()
                     if a.action == "team_updated" and a.entity_id == pumas.id)
        self.assertEqual(entry.detail["from_league_id"], over55.id)
        self.assertEqual(entry.detail["to_league_id"], other.id)

    def test_import_cannot_move_season_league_with_registrations(self):
        self.api.commit_hierarchy_import(payload(), actor_id="admin")
        second = {"import_type": "hierarchy", "organizations_csv": ORGANIZATIONS,
                  "leagues_csv": LEAGUES + "OTHER,CANLON,Other,US,America/Chicago\n"}
        self.assertTrue(
            self.api.commit_hierarchy_import(second, actor_id="admin")["committed"])
        season = self._by_ref(self.store.all_seasons(), "FALL26")
        over55 = self._by_ref(self.store.all_programs(), "OVER55")
        audits_before = len(self.store.all_setup_audit())
        # Re-import FALL26 under a different league while teams are registered.
        moved = {"import_type": "hierarchy", "competition_csv": (
            "league_code,season_code,season_name,level_code,level_name,"
            "level_sort_order,division_code,division_name,age_group\n"
            "OTHER,FALL26,Fall 2026,,,,DIVA,Division A,Adult\n"
            "OTHER,FALL26,Fall 2026,,,,DIVB,Division B,Adult\n")}
        result = self.api.commit_hierarchy_import(moved, actor_id="admin")
        self.assertFalse(result["committed"])
        err = next(e for e in result["errors"]
                   if e["reason"] == "season_league_move_strands_history")
        self.assertTrue(err["affected_registration_ids"])
        self.assertEqual(self.store.get_season(season.id).program_id, over55.id)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_season_league_move_blocked_by_inactive_registration(self):
        # #214 review-6: a season league move must account for INACTIVE
        # registrations too. (#180: legacy Team->Division links no longer count.)
        self.api.commit_hierarchy_import(payload(), actor_id="admin")
        second = {"import_type": "hierarchy", "organizations_csv": ORGANIZATIONS,
                  "leagues_csv": LEAGUES + "OTHER,CANLON,Other,US,America/Chicago\n"}
        self.assertTrue(
            self.api.commit_hierarchy_import(second, actor_id="admin")["committed"])
        season = self._by_ref(self.store.all_seasons(), "FALL26")
        over55 = self._by_ref(self.store.all_programs(), "OVER55")
        # Make both registrations historical (inactive).
        for reg in list(self.store.all_season_team_registrations()):
            if reg.season_id == season.id:
                reg.active = False
                self.store.save_season_team_registration(reg)
        audits_before = len(self.store.all_setup_audit())
        moved = {"import_type": "hierarchy", "competition_csv": (
            "league_code,season_code,season_name,level_code,level_name,"
            "level_sort_order,division_code,division_name,age_group\n"
            "OTHER,FALL26,Fall 2026,,,,DIVA,Division A,Adult\n")}
        result = self.api.commit_hierarchy_import(moved, actor_id="admin")
        self.assertFalse(result["committed"])
        err = next(e for e in result["errors"]
                   if e["reason"] == "season_league_move_strands_history")
        self.assertTrue(err["affected_registration_ids"])   # inactive counted
        self.assertEqual(self.store.get_season(season.id).program_id, over55.id)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_division_season_move_blocked_by_cancelled_game(self):
        # #214 review-6: a division season move must account for cancelled
        # historical games too. (#180: legacy Team->Division links don't count.)
        game_id = self._commit_game()  # a committed FALL26/DIVA game
        self.api.cancel_game(game_id, actor_id="admin")  # now cancelled history
        second = {"import_type": "hierarchy", "competition_csv": (
            COMPETITION + "OVER55,SPRING,Spring,,,,DIVC,Division C,Adult\n")}
        self.assertTrue(
            self.api.commit_hierarchy_import(second, actor_id="admin")["committed"])
        diva = self._by_ref(self.store.all_divisions(), "DIVA")
        fall = self._by_ref(self.store.all_seasons(), "FALL26")
        # Deactivate the DIVA registrations; the cancelled game still strands.
        for reg in list(self.store.all_season_team_registrations()):
            if reg.division_id == diva.id:
                reg.active = False
                self.store.save_season_team_registration(reg)
        audits_before = len(self.store.all_setup_audit())
        moved = {"import_type": "hierarchy", "competition_csv": (
            "league_code,season_code,season_name,level_code,level_name,"
            "level_sort_order,division_code,division_name,age_group\n"
            "OVER55,SPRING,Spring,,,,DIVA,Division A,Adult\n")}
        result = self.api.commit_hierarchy_import(moved, actor_id="admin")
        self.assertFalse(result["committed"])
        err = next(e for e in result["errors"]
                   if e["reason"] == "division_season_move_strands_dependents")
        self.assertIn(game_id, err["affected_game_ids"])      # cancelled counted
        self.assertTrue(err["affected_registration_ids"])     # inactive counted
        self.assertEqual(self.store.get_division(diva.id).season_id, fall.id)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_import_cannot_move_division_season_with_dependents(self):
        self.api.commit_hierarchy_import(payload(), actor_id="admin")
        second = {"import_type": "hierarchy", "competition_csv": (
            COMPETITION + "OVER55,SPRING,Spring,,,,DIVC,Division C,Adult\n")}
        self.assertTrue(
            self.api.commit_hierarchy_import(second, actor_id="admin")["committed"])
        diva = self._by_ref(self.store.all_divisions(), "DIVA")
        fall = self._by_ref(self.store.all_seasons(), "FALL26")
        audits_before = len(self.store.all_setup_audit())
        # Re-import DIVA under a different season while teams are registered in it.
        moved = {"import_type": "hierarchy", "competition_csv": (
            "league_code,season_code,season_name,level_code,level_name,"
            "level_sort_order,division_code,division_name,age_group\n"
            "OVER55,SPRING,Spring,,,,DIVA,Division A,Adult\n")}
        result = self.api.commit_hierarchy_import(moved, actor_id="admin")
        self.assertFalse(result["committed"])
        err = next(e for e in result["errors"]
                   if e["reason"] == "division_season_move_strands_dependents")
        self.assertTrue(err["affected_registration_ids"])
        self.assertEqual(self.store.get_division(diva.id).season_id, fall.id)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_reimport_preserves_legacy_division_fields_on_existing_team(self):
        # An existing coded team retains #180's legacy division_id/division
        # compatibility fields; a re-import converges league/name/club but must
        # NOT clear them (#214 review) — legacy game scope reads them until the
        # migration that removes those readers lands.
        self.api.commit_hierarchy_import(payload(), actor_id="admin")
        league = self._by_ref(self.store.all_programs(), "OVER55")
        season = self._by_ref(self.store.all_seasons(), "FALL26")
        diva = self._by_ref(self.store.all_divisions(), "DIVA")
        self.store.add_team(Team(id="team_legacy", name="Old Name",
                                 external_ref="LEGACY", club_id=None,
                                 division_id=diva.id, division="Division A",
                                 program_id=league.id))
        self.store.add_season_team_registration(SeasonTeamRegistration(
            id="streg_legacy", season_id=season.id, team_id="team_legacy",
            division_id=diva.id, active=True))
        reimport = {"import_type": "hierarchy", "permanent_teams_csv":
                    "league_code,team_code,team_name\n"
                    "OVER55,LEGACY,New Name\n"}
        result = self.api.commit_hierarchy_import(reimport, actor_id="admin")
        self.assertTrue(result["committed"], result.get("errors"))
        team = self._team("LEGACY")
        # Permanent fields converged.
        self.assertEqual(team.name, "New Name")
        self.assertEqual(team.program_id, league.id)
        # Legacy compatibility fields and legacy game scope preserved.
        self.assertEqual(team.division_id, diva.id)
        self.assertEqual(team.division, "Division A")
        reg = self.store.registration_for_team_in_season(season.id, team.id)
        self.assertEqual(reg.division_id, diva.id)

    def test_inactive_registration_with_committed_game_cannot_move_division(self):
        # An inactive/historical registration is still a move candidate: commit
        # would reactivate it and change its division, so a committed game in the
        # old division must still block the move (#214 review), with zero writes.
        game_id = self._commit_game()  # committed LIONS vs BEARS in FALL26/DIVA
        season = self._by_ref(self.store.all_seasons(), "FALL26")
        diva = self._by_ref(self.store.all_divisions(), "DIVA")
        lions = self._team("LIONS")
        reg = self.store.registration_for_team_in_season(season.id, lions.id)
        reg.active = False  # historical registration; the game stays committed
        self.store.save_season_team_registration(reg)
        audits_before = len(self.store.all_setup_audit())
        moved = payload(registrations_csv=(
            "season_code,team_code,division_code\nFALL26,LIONS,DIVB\n"))
        result = self.api.commit_hierarchy_import(moved, actor_id="admin")
        self.assertFalse(result["committed"])
        err = next(e for e in result["errors"]
                   if e["reason"] == "registration_division_move_strands_games")
        self.assertIn(game_id, err["affected_game_ids"])
        # Zero writes: registration still inactive and still in DIVA.
        reg2 = self.store.registration_for_team_in_season(season.id, lions.id)
        self.assertFalse(reg2.active)
        self.assertEqual(reg2.division_id, diva.id)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_inactive_registration_with_no_games_can_reactivate_and_move(self):
        # A safe reactivation: an inactive registration with no committed games
        # can be reactivated and moved to a new division by import.
        self.api.commit_hierarchy_import(payload(), actor_id="admin")
        season = self._by_ref(self.store.all_seasons(), "FALL26")
        divb = self._by_ref(self.store.all_divisions(), "DIVB")
        lions = self._team("LIONS")
        reg = self.store.registration_for_team_in_season(season.id, lions.id)
        reg.active = False
        self.store.save_season_team_registration(reg)
        moved = payload(registrations_csv=(
            "season_code,team_code,division_code\nFALL26,LIONS,DIVB\n"))
        result = self.api.commit_hierarchy_import(moved, actor_id="admin")
        self.assertTrue(result["committed"], result.get("errors"))
        reg2 = self.store.registration_for_team_in_season(season.id, lions.id)
        self.assertTrue(reg2.active)                 # reactivated
        self.assertEqual(reg2.division_id, divb.id)  # moved

    def test_import_never_creates_or_clears_clubs(self):
        # Club association is deferred from this slice (#214 review): the import
        # creates no Club records and never touches an existing team's club_id,
        # so there is no non-deterministic name-matching.
        self.api.commit_hierarchy_import(payload(), actor_id="admin")
        self.assertEqual(self.store.all_clubs(), [])  # commit created no clubs
        league = self._by_ref(self.store.all_programs(), "OVER55")
        club = Club(id="club_x", name="Existing Club")
        self.store.add_club(club)
        self.store.add_team(Team(id="team_wc", name="WithClub", external_ref="WC",
                                 club_id=club.id, division_id=None,
                                 program_id=league.id))
        clubs_before = len(self.store.all_clubs())
        reimport = {"import_type": "hierarchy", "permanent_teams_csv":
                    "league_code,team_code,team_name\nOVER55,WC,Renamed\n"}
        result = self.api.commit_hierarchy_import(reimport, actor_id="admin")
        self.assertTrue(result["committed"], result.get("errors"))
        team = self._team("WC")
        self.assertEqual(team.name, "Renamed")               # name converged
        self.assertEqual(team.club_id, club.id)              # club_id untouched
        self.assertEqual(len(self.store.all_clubs()), clubs_before)  # no new clubs

    def test_preflight_runs_inside_the_transaction(self):
        # #214 review: validation/preflight must run under the same transaction
        # lock as the writes, so there is no check->write gap. Prove the
        # preflight's game read happens while a transaction is held.
        self.api.commit_hierarchy_import(payload(), actor_id="admin")
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
        # A registration division move reaches the preflight's game-stranding read.
        moved = payload(registrations_csv=(
            "season_code,team_code,division_code\n"
            "FALL26,LIONS,DIVB\nFALL26,BEARS,DIVA\n"))
        self.api.commit_hierarchy_import(moved, actor_id="admin")
        self.assertTrue(state["preflight_inside"],
                        "preflight game read ran outside the transaction lock")


class MemoryImportConvergenceTest(ImportConvergenceContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurableImportConvergenceTest(ImportConvergenceContract, unittest.TestCase):
    def make_store(self):
        # Honor TEST_DATABASE_URL so the PostgreSQL CI job actually exercises
        # these tests against Postgres (#214 review); fall back to SQLite
        # otherwise. Mirrors DurableHierarchyImportTest.
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

    def test_unknown_league_on_team_rejected(self):
        result = self._dry(payload(
            permanent_teams_csv="league_code,team_code,team_name\nNOPE,X,X\n",
            registrations_csv="season_code,team_code,division_code\n"))
        self.assertFalse(result["ok"])
        self.assertTrue(self._has(result, "Unknown league_code NOPE"))

    def test_registration_unknown_codes_rejected(self):
        result = self._dry(payload(registrations_csv=(
            "season_code,team_code,division_code\nNOSEASON,LIONS,DIVA\n")))
        self.assertFalse(result["ok"])
        self.assertTrue(self._has(result, "Unknown season_code NOSEASON"))

    def test_division_not_in_season_rejected(self):
        comp = COMPETITION + "OVER55,SPRING,Spring,,,,DIVC,Division C,Adult\n"
        result = self._dry(payload(
            competition_csv=comp,
            registrations_csv=(
                "season_code,team_code,division_code\nSPRING,LIONS,DIVA\n"
                "FALL26,BEARS,DIVA\n")))
        self.assertFalse(result["ok"])
        self.assertTrue(self._has(result, "does not belong to season_code"))

    def test_cross_league_registration_rejected(self):
        leagues = LEAGUES + "OTHER,CANLON,Other,US,America/Chicago\n"
        comp = COMPETITION + "OTHER,OSEA,Other Season,,,,ODIV,Other Div,Adult\n"
        result = self._dry(payload(
            leagues_csv=leagues, competition_csv=comp,
            registrations_csv=(
                "season_code,team_code,division_code\nOSEA,LIONS,ODIV\n"
                "FALL26,BEARS,DIVA\n")))
        self.assertFalse(result["ok"])
        self.assertTrue(self._has(result, "cannot register"))

    def test_duplicate_registration_rejected(self):
        result = self._dry(payload(registrations_csv=(
            "season_code,team_code,division_code\n"
            "FALL26,LIONS,DIVA\n"
            "FALL26,LIONS,DIVB\n")))
        self.assertFalse(result["ok"])
        self.assertTrue(self._has(result, "Duplicate registration"))

    def test_duplicate_team_code_rejected(self):
        result = self._dry(payload(permanent_teams_csv=(
            "league_code,team_code,team_name\n"
            "OVER55,LIONS,Lions\n"
            "OVER55,LIONS,Lions Again\n"),
            registrations_csv="season_code,team_code,division_code\n"))
        self.assertFalse(result["ok"])
        self.assertTrue(self._has(result, "Duplicate team_code"))

    def test_failed_validation_writes_nothing(self):
        bad = payload(registrations_csv=(
            "season_code,team_code,division_code\nNOSEASON,LIONS,DIVA\n"))
        result = self.api.commit_hierarchy_import(bad, actor_id="admin")
        self.assertFalse(result["committed"])
        self.assertEqual(self.store.all_teams(), [])
        self.assertEqual(self.store.all_season_team_registrations(), [])

    # -- #214 review: reject null/dangling parent leagues, with zero writes ----
    def test_null_team_league_registration_rejected(self):
        base = {"import_type": "hierarchy", "organizations_csv": ORGANIZATIONS,
                "leagues_csv": LEAGUES, "competition_csv": COMPETITION}
        self.api.commit_hierarchy_import(base, actor_id="admin")
        self.store.add_team(Team(id="team_nl", name="NoLeague",
                                 external_ref="NOLG", division_id=None,
                                 program_id=None))
        reg = {"import_type": "hierarchy", "registrations_csv":
               "season_code,team_code,division_code\nFALL26,NOLG,DIVA\n"}
        self.assertFalse(self.api.get_hierarchy_import_dry_run(reg)["ok"])
        self.assertTrue(self._has(
            self.api.get_hierarchy_import_dry_run(reg), "cannot register"))
        before = len(self.store.all_season_team_registrations())
        self.assertFalse(
            self.api.commit_hierarchy_import(reg, actor_id="admin")["committed"])
        self.assertEqual(
            len(self.store.all_season_team_registrations()), before)

    def test_null_season_league_registration_rejected(self):
        self.api.commit_hierarchy_import(payload(), actor_id="admin")
        self.store.add_season(Season(id="se_nl", program_id="", name="NL",
                                     external_ref="SNL"))
        self.store.add_division(Division(id="d_nl", season_id="se_nl", name="D",
                                         external_ref="DNL"))
        reg = {"import_type": "hierarchy", "registrations_csv":
               "season_code,team_code,division_code\nSNL,LIONS,DNL\n"}
        result = self.api.get_hierarchy_import_dry_run(reg)
        self.assertFalse(result["ok"])
        self.assertTrue(self._has(result, "cannot register"))

    def test_dangling_division_season_registration_rejected(self):
        self.api.commit_hierarchy_import(payload(), actor_id="admin")
        self.store.add_division(Division(id="d_dangle", season_id="missing",
                                         name="D", external_ref="DDG"))
        reg = {"import_type": "hierarchy", "registrations_csv":
               "season_code,team_code,division_code\nFALL26,LIONS,DDG\n"}
        result = self.api.get_hierarchy_import_dry_run(reg)
        self.assertFalse(result["ok"])
        self.assertTrue(self._has(result, "does not belong to season_code"))


if __name__ == "__main__":
    unittest.main()
