"""Swap-safe jersey imports + complete lost-race conflict context (#292).

Regression follow-up to #269 / PR #291. Two gaps that CI missed because no test
exercised the real preview→commit journey:

1. A valid batch jersey swap (A ``7→8``, B ``8→7`` on one team) previews clean
   but the sequential per-row commit collided on the intermediate state. Both
   the legacy teams+players import and the nine-sheet hierarchy import now apply
   a validated batch as one final-state transition (release-then-assign inside
   the single commit transaction), so a swap commits atomically while a TRUE
   final-state duplicate still reports every row and writes nothing.
2. A database-race unique conflict now carries the same conflicting-player
   context the service pre-check does — ``conflicting_player_id`` (+ name),
   never contact/private data.

Covered on Memory, SQLite, and (when ``TEST_DATABASE_URL`` is set) PostgreSQL.
"""

import os
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)
import hierarchy_fixtures as fx

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain.errors import IntegrityConflictError
from hockey_scheduler.services import SetupService
from hockey_scheduler.services.hierarchy_import import validate_hierarchy_import
from hockey_scheduler.store import InMemoryStore, SqlStore


def _backends():
    stores = [("memory", InMemoryStore()), ("sqlite", SqlStore(":memory:"))]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        pg = SqlStore(url)
        pg.reset_schema()
        stores.append(("postgres", pg))
    return stores


_TEAMS_CSV = ("team_code,team_name,club_name,division_name\n"
              "T1,Team One,Lions,U16\n")

_TWO_TEAMS_CSV = ("team_code,team_name,club_name,division_name\n"
                  "T1,Team One,Lions,U16\n"
                  "T2,Team Two,Falcons,U18\n")


def _players_csv(*rows):
    header = ("player_code,first_name,last_name,team_code,"
              "jersey_number,position,email\n")
    return header + "".join(r + "\n" for r in rows)


def _jersey_of(store, code):
    return next(p.jersey_number for p in store.all_players()
               if p.external_ref == code)


def _player_audits(store):
    return [a for a in store.all_setup_audit()
            if a.action in ("player_created", "player_updated", "player_added")]


class LegacyImportSwapTest(unittest.TestCase):
    """Two-sheet teams+players import: same-team 7↔8 swap and true collision."""

    def _seed(self, store):
        setup = SetupService(store)
        api = ApiService(store)
        program = setup.create_program("L", actor_id="a")
        season = setup.create_season(program.id, "S", actor_id="a")
        setup.create_league(season.id, "L1", actor_id="a")
        api.commit_teams_players_import(
            season.id,
            {"teams_csv": _TEAMS_CSV,
             "players_csv": _players_csv(
                 "P1,Ann,X,T1,7,forward,", "P2,Bob,Y,T1,8,forward,")},
            actor_id="a")
        return setup, api, season

    def test_same_team_swap_previews_clean_and_commits_atomically(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                setup, api, season = self._seed(store)
                swap = {"teams_csv": _TEAMS_CSV,
                        "players_csv": _players_csv(
                            "P1,Ann,X,T1,8,forward,", "P2,Bob,Y,T1,7,forward,")}
                # Preview is clean: the store-aware dry-run simulates the final
                # roster and sees a valid swap, not a duplicate.
                preview = api.get_import_dry_run(swap)
                self.assertTrue(preview["ok"], (label, preview.get("errors")))
                res = api.commit_teams_players_import(
                    season.id, swap, actor_id="a")
                self.assertTrue(res["committed"], (label, res))
                self.assertEqual(_jersey_of(store, "P1"), 8, label)
                self.assertEqual(_jersey_of(store, "P2"), 7, label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_true_final_state_collision_reports_and_writes_nothing(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                setup, api, season = self._seed(store)
                audits_before = len(_player_audits(store))
                clash = {"teams_csv": _TEAMS_CSV,
                         "players_csv": _players_csv(
                             "P1,Ann,X,T1,5,forward,", "P2,Bob,Y,T1,5,forward,")}
                res = api.commit_teams_players_import(
                    season.id, clash, actor_id="a")
                self.assertFalse(res["committed"], label)
                self.assertTrue(res.get("errors"), label)
                # Zero writes: jerseys unchanged, no new player audit.
                self.assertEqual(_jersey_of(store, "P1"), 7, label)
                self.assertEqual(_jersey_of(store, "P2"), 8, label)
                self.assertEqual(len(_player_audits(store)), audits_before, label)
                if isinstance(store, SqlStore):
                    store.close()

    def _seed_two_teams(self, store, *, second_team_players=()):
        setup = SetupService(store)
        api = ApiService(store)
        program = setup.create_program("L", actor_id="a")
        season = setup.create_season(program.id, "S", actor_id="a")
        setup.create_league(season.id, "L1", actor_id="a")
        api.commit_teams_players_import(
            season.id,
            {"teams_csv": _TWO_TEAMS_CSV,
             "players_csv": _players_csv(
                 "P1,Ann,X,T1,7,forward,", *second_team_players)},
            actor_id="a")
        return setup, api, season

    def test_team_move_with_blank_jersey_keeps_the_number(self):
        # A Team move whose jersey cell is blank must KEEP the existing number,
        # never erase it to NULL through the swap-safe staging (#292 review).
        for label, store in _backends():
            with self.subTest(backend=label):
                setup, api, season = self._seed_two_teams(store)
                t2 = next(t for t in store.all_teams()
                          if t.external_ref == "T2")
                move = {"teams_csv": _TWO_TEAMS_CSV,
                        "players_csv": _players_csv("P1,Ann,X,T2,,forward,")}
                res = api.commit_teams_players_import(season.id, move,
                                                      actor_id="a")
                self.assertTrue(res["committed"], (label, res))
                p1 = next(p for p in store.all_players()
                          if p.external_ref == "P1")
                self.assertEqual(p1.team_id, t2.id, label)      # Team changed
                self.assertEqual(p1.jersey_number, 7, label)    # number kept
                if isinstance(store, SqlStore):
                    store.close()

    def test_team_move_blank_jersey_into_occupied_number_writes_nothing(self):
        # Moving P1 (keeps #7) onto T2, which already has an active #7, is a
        # true final-state collision — zero writes, zero audits, unchanged rows.
        for label, store in _backends():
            with self.subTest(backend=label):
                setup, api, season = self._seed_two_teams(
                    store, second_team_players=("P2,Bob,Y,T2,7,forward,",))
                before = {p.external_ref: (p.team_id, p.jersey_number)
                          for p in store.all_players()}
                audits_before = len(_player_audits(store))
                move = {"teams_csv": _TWO_TEAMS_CSV,
                        "players_csv": _players_csv("P1,Ann,X,T2,,forward,")}
                res = api.commit_teams_players_import(season.id, move,
                                                      actor_id="a")
                self.assertIsNot(res.get("committed"), True, (label, res))
                after = {p.external_ref: (p.team_id, p.jersey_number)
                         for p in store.all_players()}
                self.assertEqual(after, before, label)          # nothing moved
                self.assertEqual(len(_player_audits(store)), audits_before,
                                 label)
                if isinstance(store, SqlStore):
                    store.close()


class HierarchyImportSwapTest(unittest.TestCase):
    """Nine-sheet hierarchy import: swap, cross-team exchange, true collision."""

    def _two(self, j1, j2, team1="LIONS", team2="LIONS"):
        return fx.players_csv(rows=(f"P1,{team1},Ann,X,{j1},forward,",
                                    f"P2,{team2},Bob,Y,{j2},forward,"))

    def test_same_team_swap_previews_clean_and_commits_atomically(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.commit_hierarchy_import(
                    fx.full_payload(players_csv=self._two(7, 8)), actor_id="a")
                swap = fx.full_payload(players_csv=self._two(8, 7))
                preview = validate_hierarchy_import(
                    {k[:-4]: _parse(v) for k, v in swap.items()
                     if k.endswith("_csv")}, store)
                self.assertTrue(preview["ok"], (label, preview.get("errors")))
                res = api.commit_hierarchy_import(swap, actor_id="a")
                self.assertTrue(res["committed"], (label, res))
                self.assertEqual(_jersey_of(store, "P1"), 8, label)
                self.assertEqual(_jersey_of(store, "P2"), 7, label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_cross_team_occupied_exchange_follows_final_state(self):
        # LIONS and BEARS both exist in the fixture. P1 starts LIONS#7, P2
        # BEARS#8; exchange to P1 BEARS#8, P2 LIONS#7 — both team AND number
        # move, and the occupied numbers are exchanged with no transient clash.
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.commit_hierarchy_import(
                    fx.full_payload(players_csv=self._two(
                        7, 8, team1="LIONS", team2="BEARS")), actor_id="a")
                res = api.commit_hierarchy_import(
                    fx.full_payload(players_csv=self._two(
                        8, 7, team1="BEARS", team2="LIONS")), actor_id="a")
                self.assertTrue(res["committed"], (label, res))
                p1 = next(p for p in store.all_players()
                          if p.external_ref == "P1")
                p2 = next(p for p in store.all_players()
                          if p.external_ref == "P2")
                self.assertEqual((p1.jersey_number, p2.jersey_number), (8, 7),
                                 label)
                self.assertNotEqual(p1.team_id, p2.team_id, label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_true_final_state_collision_writes_nothing(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.commit_hierarchy_import(
                    fx.full_payload(players_csv=self._two(7, 8)), actor_id="a")
                audits_before = len(_player_audits(store))
                res = api.commit_hierarchy_import(
                    fx.full_payload(players_csv=self._two(5, 5)), actor_id="a")
                self.assertFalse(res["committed"], label)
                self.assertTrue(res.get("errors"), label)
                self.assertEqual(_jersey_of(store, "P1"), 7, label)
                self.assertEqual(_jersey_of(store, "P2"), 8, label)
                self.assertEqual(len(_player_audits(store)), audits_before, label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_team_move_keeping_jersey_does_not_falsely_audit_jersey(self):
        # A Team-only move that keeps the same explicit number is released to
        # NULL during staging; the single final audit must describe the real
        # change (Team) and must NOT claim a jersey change (#292 review).
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.commit_hierarchy_import(
                    fx.full_payload(players_csv=self._two(7, 8)), actor_id="a")
                # P1 was LIONS#7; move to BEARS, still #7. P2 stays LIONS#8.
                api.commit_hierarchy_import(
                    fx.full_payload(players_csv=self._two(7, 8, team1="BEARS")),
                    actor_id="a")
                p1 = next(p for p in store.all_players()
                          if p.external_ref == "P1")
                bears = next(t for t in store.all_teams()
                             if t.external_ref == "BEARS")
                self.assertEqual(p1.team_id, bears.id, label)   # Team changed
                self.assertEqual(p1.jersey_number, 7, label)    # number kept
                updates = [a for a in store.all_setup_audit()
                           if a.action == "player_updated"
                           and a.detail.get("external_ref") == "P1"]
                self.assertTrue(updates, label)
                changed = updates[-1].detail.get("changed_fields", [])
                self.assertIn("team_id", changed, label)
                self.assertNotIn("jersey_number", changed, label)
                if isinstance(store, SqlStore):
                    store.close()


def _parse(csv_text):
    import csv as _csvmod
    import io
    return list(_csvmod.DictReader(io.StringIO(csv_text))) if csv_text else []


class LostRaceConflictContextTest(unittest.TestCase):
    """A database-race conflict carries the conflicting player, like the pre-check."""

    def _player(self, pid, team, jersey):
        from hockey_scheduler.domain import Player, Position
        return Player(id=pid, team_id=team, name=f"Name {pid}",
                      position=Position.FORWARD, jersey_number=jersey)

    def test_db_conflict_names_the_holder_on_sqlite(self):
        # A deterministic (single-connection) constraint hit still enriches the
        # conflict with the winning holder id + name post-rollback (#292).
        store = SqlStore(":memory:")
        try:
            # Seed the team the planted players reference so migration 040's
            # players.team_id → teams(id) foreign key is satisfied.
            from hockey_scheduler.domain import Team
            with store.transaction():
                store.add_team(Team(id="t1", name="t1"))
            with store.transaction():
                store.add_player(self._player("keep", "t1", 7))
            with self.assertRaises(IntegrityConflictError) as ctx:
                with store.transaction():
                    store.add_player(self._player("loser", "t1", 7))
            d = ctx.exception.details
            self.assertEqual(d["reason"], "duplicate_jersey_number")
            self.assertEqual(d["team_id"], "t1")
            self.assertEqual(d["jersey_number"], 7)
            self.assertEqual(d["conflicting_player_id"], "keep")
            self.assertEqual(d["conflicting_player_name"], "Name keep")
            self.assertNotIn("email", d)
        finally:
            store.close()

    # The authoritative forced-race journey lives in
    # test_jersey_number_rules.JerseyRaceTest: it holds the two-thread barrier
    # AFTER both service pre-checks observe a free slot, so the DATABASE index
    # (not the pre-check) decides the race, and asserts the enriched
    # conflicting-player context. A barrier before create_player can let one
    # request finish first and exercise only the pre-check path, so it is not
    # reproduced here.


if __name__ == "__main__":
    unittest.main()
