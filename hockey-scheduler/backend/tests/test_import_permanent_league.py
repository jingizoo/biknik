"""#283 Slice E: hierarchy import binds a Team to its permanent League.

The permanent_teams sheet gained a required ``league_code`` (the Team's
permanent League, which must sit in the Team's Program). A registration may only
use a Team's own permanent League. These pin, on both stores:
  * a commit writes Team.league_id from the sheet's league_code;
  * an unknown league_code is rejected in the dry run;
  * a registration whose league_code differs from the Team's permanent League
    is rejected;
  * re-import is idempotent (no duplicate teams, league_id stable).
"""

import os
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

import hierarchy_fixtures as fx
from hockey_scheduler.api import ApiService
from hockey_scheduler.store import InMemoryStore, SqlStore


class _Contract:
    def setUp(self):
        self.store = self.make_store()
        self.api = ApiService(self.store)

    def tearDown(self):
        conn = getattr(self.store, "conn", None)
        if conn is not None:
            conn.close()

    def _league_id(self, code):
        lg = next((l for l in self.store.all_leagues()
                   if l.external_ref == code), None)
        return lg.id if lg else None

    def _team(self, code):
        return next((t for t in self.store.all_teams()
                     if t.external_ref == code), None)

    def test_commit_writes_team_permanent_league(self):
        res = self.api.commit_hierarchy_import(fx.full_payload())
        self.assertTrue(res["committed"], res.get("errors"))
        lions = self._team("LIONS")
        self.assertIsNotNone(lions)
        self.assertEqual(lions.league_id, self._league_id("L1"))

    def test_unknown_permanent_league_code_rejected(self):
        payload = fx.full_payload(
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,NOPE,LIONS,Lions,EAGLES",
                "OVER55,L1,BEARS,Bears,")))
        res = self.api.get_hierarchy_import_dry_run(payload)
        self.assertFalse(res["ok"])
        codes = {e["code"] for e in res["errors"]}
        self.assertIn("unknown_league_code", codes)

    def test_registration_into_non_permanent_league_rejected(self):
        # L2 exists in the same program/season; LIONS is a permanent L1 team but
        # the registration tries to put it in L2 → rejected.
        payload = fx.full_payload(
            competition_csv=fx.competition_csv(rows=(
                "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult",
                "OVER55,FALL26,Fall 2026,L2,Second League,2,,,")),
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L1,LIONS,Lions,EAGLES",)),
            players_csv="",
            registrations_csv=fx.registrations_csv(rows=("FALL26,LIONS,L2,",)))
        res = self.api.get_hierarchy_import_dry_run(payload)
        self.assertFalse(res["ok"])
        codes = {e["code"] for e in res["errors"]}
        self.assertIn("registration_league_not_team_league", codes)

    def test_reimport_is_idempotent(self):
        first = self.api.commit_hierarchy_import(fx.full_payload())
        self.assertTrue(first["committed"], first.get("errors"))
        team_ids = {t.external_ref: t.id for t in self.store.all_teams()}
        lg_before = self._team("LIONS").league_id

        second = self.api.commit_hierarchy_import(fx.full_payload())
        self.assertTrue(second["committed"], second.get("errors"))
        # No duplicate teams; ids and permanent league stable.
        again = {t.external_ref: t.id for t in self.store.all_teams()}
        self.assertEqual(again, team_ids)
        self.assertEqual(self._team("LIONS").league_id, lg_before)


class MemoryImportPermLeagueTest(_Contract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurableImportPermLeagueTest(_Contract, unittest.TestCase):
    def make_store(self):
        url = os.environ.get("TEST_DATABASE_URL") or ":memory:"
        store = SqlStore(url)
        store.reset_schema()
        return store


if __name__ == "__main__":
    unittest.main()
