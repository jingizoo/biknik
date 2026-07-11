"""Team + season-registration import convergence (#180).

The code-keyed hierarchy import gains two sheets so the CSV path builds the
#180 model directly: permanent league teams (keyed by team_code, owned by a
league, carrying no division) and season registrations (keyed by
(season_code, team_code), assigning that season's division). Validation reuses
the hierarchy import's reference checks and enforces the #200 invariants — a
registration's division must belong to its season and the team's league must
match the season's league. Commit is idempotent and single-transaction.
"""

import os
import tempfile
import unittest

from helpers import BACKEND  # noqa: F401

from hockey_scheduler.api import ApiService
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
    "league_code,team_code,team_name,club_name\n"
    "OVER55,LIONS,Lions,Lions HC\n"
    "OVER55,BEARS,Bears,Bears HC\n")
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
        league = self._by_ref(self.store.all_leagues(), "OVER55")
        # Permanent league team: owned by the league, no division on the Team.
        self.assertEqual(lions.league_id, league.id)
        self.assertIsNone(lions.division_id)
        self.assertTrue(any(c.name == "Lions HC" for c in self.store.all_clubs()))
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
            self._team("PUMAS").league_id,
            self._by_ref(self.store.all_leagues(), "OVER55").id)

    def test_imported_registered_team_is_schedulable(self):
        # End-to-end: an imported permanent team, once registered, passes the
        # #200 scheduling guard and can be given a game.
        self.api.commit_hierarchy_import(payload(), actor_id="admin")
        league = self._by_ref(self.store.all_leagues(), "OVER55")
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


class MemoryImportConvergenceTest(ImportConvergenceContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurableImportConvergenceTest(ImportConvergenceContract, unittest.TestCase):
    def make_store(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return SqlStore(path)


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
        self.assertTrue(self._has(result, "belongs to season"))

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


if __name__ == "__main__":
    unittest.main()
