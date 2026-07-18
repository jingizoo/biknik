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

    def _by_ref(self, rows, code):
        return next((r for r in rows if r.external_ref == code), None)

    def test_league_may_participate_in_multiple_seasons(self):
        # Blocker 5: a permanent League is NOT owned by one Season — the same
        # league_code appearing across two Seasons is valid and yields ONE
        # permanent League with a LeagueSeason per Season.
        payload = fx.full_payload(
            competition_csv=fx.competition_csv(rows=(
                "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult",
                "OVER55,SPRING27,Spring 2027,L1,Adult League,1,DIVB,Division B,Adult")),
            registrations_csv=fx.registrations_csv(rows=(
                "FALL26,LIONS,L1,DIVA",
                "SPRING27,LIONS,L1,DIVB")))
        dry = self.api.get_hierarchy_import_dry_run(payload)
        self.assertTrue(dry["ok"], dry.get("errors"))
        res = self.api.commit_hierarchy_import(payload)
        self.assertTrue(res["committed"], res.get("errors"))
        # Exactly one permanent League L1, with two LeagueSeasons (one per Season).
        l1 = [l for l in self.store.all_leagues() if l.external_ref == "L1"]
        self.assertEqual(len(l1), 1, l1)
        seasons = {s.external_ref: s.id for s in self.store.all_seasons()}
        ls_pairs = {(ls.league_id, ls.season_id)
                    for ls in self.store.all_league_seasons()}
        self.assertIn((l1[0].id, seasons["FALL26"]), ls_pairs)
        self.assertIn((l1[0].id, seasons["SPRING27"]), ls_pairs)

    def _l1_league_seasons(self):
        l1 = self._league_id("L1")
        seasons = {s.external_ref: s.id for s in self.store.all_seasons()}
        pairs = {(ls.league_id, ls.season_id)
                 for ls in self.store.all_league_seasons()}
        return l1, seasons, pairs

    def test_multi_season_league_binds_every_season_without_division(self):
        # Blocker: the SECOND Season row for L1 carries NO Division and NO
        # registration — the commit must still bind L1 to that Season via a
        # LeagueSeason (it is not reached by the Division/registration loops).
        payload = fx.full_payload(
            competition_csv=fx.competition_csv(rows=(
                "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult",
                "OVER55,SPRING27,Spring 2027,L1,Adult League,1,,,")),
            registrations_csv=fx.registrations_csv(rows=("FALL26,LIONS,L1,DIVA",)))
        res = self.api.commit_hierarchy_import(payload)
        self.assertTrue(res["committed"], res.get("errors"))
        l1, seasons, pairs = self._l1_league_seasons()
        # One permanent League, a LeagueSeason for BOTH Seasons.
        self.assertEqual(
            len([l for l in self.store.all_leagues() if l.external_ref == "L1"]), 1)
        self.assertIn((l1, seasons["FALL26"]), pairs)
        self.assertIn((l1, seasons["SPRING27"]), pairs)

    def test_multi_season_league_reimport_is_idempotent(self):
        payload = fx.full_payload(
            competition_csv=fx.competition_csv(rows=(
                "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult",
                "OVER55,SPRING27,Spring 2027,L1,Adult League,1,,,")),
            registrations_csv=fx.registrations_csv(rows=("FALL26,LIONS,L1,DIVA",)))
        self.assertTrue(self.api.commit_hierarchy_import(payload)["committed"])
        self.assertTrue(self.api.commit_hierarchy_import(payload)["committed"])
        l1, seasons, _pairs = self._l1_league_seasons()
        # No duplicate LeagueSeason rows for L1 after a second identical import.
        l1_ls = [ls for ls in self.store.all_league_seasons() if ls.league_id == l1]
        self.assertEqual(
            sorted(ls.season_id for ls in l1_ls),
            sorted([seasons["FALL26"], seasons["SPRING27"]]))

    def test_multi_season_league_commit_is_all_or_nothing(self):
        # A batch that declares L1 across two Seasons but also references an
        # unknown permanent league_code in permanent_teams must be rejected
        # whole — no League and no LeagueSeason is written (all-or-nothing).
        payload = fx.full_payload(
            competition_csv=fx.competition_csv(rows=(
                "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult",
                "OVER55,SPRING27,Spring 2027,L1,Adult League,1,,,")),
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,GHOST,LIONS,Lions,EAGLES",)),
            players_csv="",
            registrations_csv="")
        res = self.api.commit_hierarchy_import(payload)
        self.assertFalse(res["committed"], res)
        # Nothing partial: no L1 League, no LeagueSeason rows at all.
        self.assertIsNone(self._league_id("L1"))
        self.assertEqual(self.store.all_league_seasons(), [])

    def test_reimport_permanent_league_move_stranding_games_rejected(self):
        # Blocker 4: a re-import that changes a Team's permanent League goes
        # through the same transfer safeguards — it can't strand committed games.
        first = self.api.commit_hierarchy_import(fx.full_payload())
        self.assertTrue(first["committed"], first.get("errors"))
        # Schedule a committed FALL26 game for LIONS in its L1 division.
        season = self._by_ref(self.store.all_seasons(), "FALL26")
        diva = self._by_ref(self.store.all_divisions(), "DIVA")
        rink = self.store.all_rinks()[0]
        slot = self.api.create_ice_slot(
            rink.id, "2026-09-01T18:00:00+00:00", "2026-09-01T19:00:00+00:00",
            "game", actor_id="admin")
        game = self.api.create_game(
            season.id, diva.id, self._team("LIONS").id, self._team("BEARS").id,
            slot["id"], actor_id="admin", league_id=self._league_id("L1"))
        self.assertNotIn("error", game, game)

        # Re-import moving LIONS to a second permanent League (L2) — with a live
        # L1 game on the books, the batch is rejected with zero writes.
        moved = fx.full_payload(
            competition_csv=fx.competition_csv(rows=(
                "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult",
                "OVER55,FALL26,Fall 2026,L2,Second League,2,,,")),
            permanent_teams_csv=fx.permanent_teams_csv(rows=(
                "OVER55,L2,LIONS,Lions,EAGLES",
                "OVER55,L1,BEARS,Bears,")),
            players_csv="",
            registrations_csv="")
        res = self.api.commit_hierarchy_import(moved)
        self.assertFalse(res["committed"], res)
        codes = {e["code"] for e in res["errors"]}
        self.assertIn("team_league_move_strands_games", codes)
        # Zero mutation: LIONS is still a permanent L1 team.
        self.assertEqual(self._team("LIONS").league_id, self._league_id("L1"))

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
