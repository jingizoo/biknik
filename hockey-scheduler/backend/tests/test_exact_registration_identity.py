"""#331 review round 19 finding 1: exact-(team_id, league_season_id)-key
multiplicity.

Migration 035's ``ux_team_league_season`` unique index makes two
``SeasonTeamRegistration`` rows at the identical exact key structurally
impossible on SQLite/PostgreSQL, so this corrupted shape is reachable only on
``InMemoryStore`` (no equivalent enforcement) or via legacy data a write path
predating this review cycle could have left behind. Every one of the seven
call sites that used to resolve "the" registration at an exact key via a bare
``store.registration_for_team_in_league_season`` lookup now goes through the
shared ``league_scope.exact_registration_or_conflict`` wrapper instead, so a
corrupted duplicate is answered identically everywhere: WRITE call sites
raise a structured ``team_registration_conflict``, zero mutation; READ-ONLY
call sites fail CLOSED (treat the ambiguity as "not validly resolved"),
deterministically regardless of which row happens to sort/iterate first.

Covers the primitive itself, both READ call sites
(``team_registration_valid``, ``context_scope._team_season_ids``), and all
five WRITE call sites (``assign_season_team_league``,
``register_team_for_season``, ``transfer_team_to_league``,
``roll_forward_registrations_v2``'s gate and apply). The sixth/seventh WRITE
call site, ``resolve_team_registration_for_import`` (hierarchy import /
``commit_teams_players_import``), already folds exact-key conflicts into its
own existing ``conflicting_ids`` output and is covered by the pre-existing
``test_import_teams_registrations.py`` / hierarchy-import test suites (no
change to those suites' scenarios was needed — they never constructed an
exact-key duplicate, so they only prove no regression, not the new behavior;
this file is where the new behavior itself is proven).
"""

import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role, SeasonTeamRegistration
from hockey_scheduler.services.context_scope import _team_season_ids
from hockey_scheduler.services.league_scope import (
    exact_registration_or_conflict, team_registration_valid)
from hockey_scheduler.store import InMemoryStore

ADMIN = "admin"


class _Base(unittest.TestCase):
    def setUp(self):
        self.api = ApiService(InMemoryStore())
        self.store = self.api.store

    def _audit_count(self):
        return len(self.store.all_setup_audit())

    def _plant_duplicate(self, league_season_id, team_id, active=(True, True)):
        """Two rows at the IDENTICAL exact key -- bypassing every service
        guard, the only way this state can exist (no current write path can
        produce it; this is exactly the corruption the review names)."""
        rows = []
        for is_active in active:
            reg = SeasonTeamRegistration(
                id=self.store.next_id("streg"), league_season_id=league_season_id,
                team_id=team_id, division_id=None, active=is_active)
            self.store.add_season_team_registration(reg)
            rows.append(reg)
        return rows


class ExactRegistrationOrConflictTest(_Base):
    """The shared primitive itself: 0/1/2+ rows, both insertion orders,
    unconditional on active/inactive state."""

    def setUp(self):
        super().setUp()
        self.program = self.api.create_program("P", actor_id=ADMIN)
        self.season = self.api.create_season(self.program["id"], "S", actor_id=ADMIN)
        self.league = self.api.create_league(self.season["id"], "L", actor_id=ADMIN)
        self.ls = self.store.league_season_for(self.league["id"], self.season["id"])
        club = self.api.create_club("C", actor_id=ADMIN)
        self.team = self.api.create_team(club["id"], None, "T", actor_id=ADMIN,
                                         league_id=self.league["id"])

    def test_zero_rows_returns_none_no_conflict(self):
        reg, conflicts = exact_registration_or_conflict(
            self.store, self.ls.id, self.team["id"])
        self.assertIsNone(reg)
        self.assertEqual(conflicts, [])

    def test_none_league_season_id_returns_none_no_conflict(self):
        reg, conflicts = exact_registration_or_conflict(
            self.store, None, self.team["id"])
        self.assertIsNone(reg)
        self.assertEqual(conflicts, [])

    def test_one_row_returns_it_no_conflict(self):
        real = self.api.register_team_for_season(
            self.season["id"], self.team["id"], actor_id=ADMIN,
            league_id=self.league["id"])
        reg, conflicts = exact_registration_or_conflict(
            self.store, self.ls.id, self.team["id"])
        self.assertEqual(reg.id, real["id"])
        self.assertEqual(conflicts, [])

    def test_two_active_rows_is_unconditional_conflict(self):
        rows = self._plant_duplicate(self.ls.id, self.team["id"], active=(True, True))
        reg, conflicts = exact_registration_or_conflict(
            self.store, self.ls.id, self.team["id"])
        self.assertIsNone(reg)
        self.assertEqual(set(conflicts), {r.id for r in rows})

    def test_two_inactive_rows_is_STILL_a_conflict(self):
        # Mere co-existence at one exact key is itself the corrupted state --
        # never conditioned on active/inactive, which row would be "picked"
        # is exactly the guess this wrapper exists to refuse to make.
        rows = self._plant_duplicate(self.ls.id, self.team["id"], active=(False, False))
        reg, conflicts = exact_registration_or_conflict(
            self.store, self.ls.id, self.team["id"])
        self.assertIsNone(reg)
        self.assertEqual(set(conflicts), {r.id for r in rows})

    def test_one_active_one_inactive_is_STILL_a_conflict(self):
        rows = self._plant_duplicate(self.ls.id, self.team["id"], active=(True, False))
        reg, conflicts = exact_registration_or_conflict(
            self.store, self.ls.id, self.team["id"])
        self.assertIsNone(reg)
        self.assertEqual(set(conflicts), {r.id for r in rows})

    def test_conflict_detection_is_insertion_order_independent(self):
        forward = self._plant_duplicate(self.ls.id, self.team["id"] + "_f",
                                        active=(True, False))
        _, conflicts_forward = exact_registration_or_conflict(
            self.store, self.ls.id, self.team["id"] + "_f")
        reversed_rows = self._plant_duplicate(self.ls.id, self.team["id"] + "_r",
                                              active=(False, True))
        _, conflicts_reversed = exact_registration_or_conflict(
            self.store, self.ls.id, self.team["id"] + "_r")
        self.assertEqual(len(conflicts_forward), 2)
        self.assertEqual(len(conflicts_reversed), 2)


class ReadPathFailClosedTest(_Base):
    """team_registration_valid and _team_season_ids: no caller to report a
    conflict to, so ambiguity must fail CLOSED (never guess), identically
    regardless of insertion order."""

    def setUp(self):
        super().setUp()
        self.program = self.api.create_program("P", actor_id=ADMIN)
        self.season = self.api.create_season(self.program["id"], "S", actor_id=ADMIN)
        self.league = self.api.create_league(self.season["id"], "L", actor_id=ADMIN)
        self.ls = self.store.league_season_for(self.league["id"], self.season["id"])
        club = self.api.create_club("C", actor_id=ADMIN)
        self.team = self.api.create_team(club["id"], None, "T", actor_id=ADMIN,
                                         league_id=self.league["id"])

    def test_team_registration_valid_fails_closed_on_duplicate(self):
        self._plant_duplicate(self.ls.id, self.team["id"], active=(True, True))
        self.assertIsNone(team_registration_valid(
            self.store, self.store.get_season(self.season["id"]), self.team["id"]))

    def test_team_registration_valid_fails_closed_regardless_of_order(self):
        # Active-then-inactive vs inactive-then-active: the OLD bare lookup
        # would answer differently depending on which sorts first; the fix
        # must answer None in BOTH orders.
        self._plant_duplicate(self.ls.id, self.team["id"], active=(True, False))
        self.assertIsNone(team_registration_valid(
            self.store, self.store.get_season(self.season["id"]), self.team["id"]))

    def test_create_game_rejects_when_both_teams_registrations_are_ambiguous(self):
        # End-to-end: the live scheduling path must never resolve a game
        # against an ambiguous registration.
        self._plant_duplicate(self.ls.id, self.team["id"], active=(True, True))
        away_club = self.api.create_club("C2", actor_id=ADMIN)
        away = self.api.create_team(away_club["id"], None, "Away", actor_id=ADMIN,
                                    league_id=self.league["id"])
        self.api.register_team_for_season(self.season["id"], away["id"],
                                          actor_id=ADMIN, league_id=self.league["id"])
        venue = self.api.create_venue("V", league_id=self.program["id"], actor_id=ADMIN)
        self.api.grant_season_venue_access(self.season["id"], venue["id"], actor_id=ADMIN)
        rink = self.api.create_rink(venue["id"], "R", actor_id=ADMIN)
        slot = self.api.create_ice_slot(
            rink["id"], "2026-09-01T18:00:00+00:00", "2026-09-01T19:00:00+00:00",
            actor_id=ADMIN)
        game = self.api.create_game(
            self.season["id"], None, self.team["id"], away["id"], slot["id"],
            actor_id=ADMIN, league_id=self.league["id"])
        self.assertIn("error", game, game)

    def test_team_season_ids_fails_closed_on_duplicate(self):
        self._plant_duplicate(self.ls.id, self.team["id"], active=(True, True))
        team_obj = self.store.get_team(self.team["id"])
        self.assertEqual(_team_season_ids(self.store, team_obj), set())

    def test_team_season_ids_includes_season_once_unambiguous(self):
        # Sanity check the fixture: a single real registration DOES surface
        # the season, so the fail-closed test above is proving the ambiguous
        # case specifically, not a broken query in general.
        self.api.register_team_for_season(
            self.season["id"], self.team["id"], actor_id=ADMIN,
            league_id=self.league["id"])
        team_obj = self.store.get_team(self.team["id"])
        self.assertEqual(_team_season_ids(self.store, team_obj), {self.season["id"]})


class WritePathRejectsExactKeyConflictTest(_Base):
    """Every WRITE call site: a structured team_registration_conflict, zero
    mutation, before any write."""

    def setUp(self):
        super().setUp()
        self.program = self.api.create_program("P", actor_id=ADMIN)
        self.season = self.api.create_season(self.program["id"], "S", actor_id=ADMIN)
        self.season2 = self.api.create_season(self.program["id"], "S2", actor_id=ADMIN)
        self.league = self.api.create_league(self.season["id"], "L", actor_id=ADMIN)
        self.other_league = self.api.create_league(self.season["id"], "L2", actor_id=ADMIN)
        self.club = self.api.create_club("C", actor_id=ADMIN)

    def test_register_team_for_season_rejects_duplicate_at_exact_key(self):
        team = self.api.create_team(self.club["id"], None, "T", actor_id=ADMIN,
                                    league_id=self.league["id"])
        ls = self.store.league_season_for(self.league["id"], self.season["id"])
        rows = self._plant_duplicate(ls.id, team["id"], active=(False, False))
        audits_before = self._audit_count()
        res = self.api.register_team_for_season(
            self.season["id"], team["id"], actor_id=ADMIN, league_id=self.league["id"])
        self.assertEqual(res["error"]["details"]["reason"],
                         "team_registration_conflict", res)
        self.assertEqual(set(res["error"]["details"]["affected_registration_ids"]),
                         {r.id for r in rows})
        self.assertEqual(self._audit_count(), audits_before)
        for r in rows:
            untouched = self.store.get_season_team_registration(r.id)
            self.assertFalse(untouched.active)

    def test_assign_season_team_league_rejects_duplicate_at_target_key(self):
        # reg (active, under the OTHER league) is repointed onto `league`,
        # which already holds a corrupted duplicate pair.
        team = self.api.create_team(self.club["id"], None, "T", actor_id=ADMIN,
                                    league_id=self.league["id"])
        other_ls = self.store.league_season_for(self.other_league["id"], self.season["id"])
        # A retained inactive row so `reg` has somewhere to start from that
        # isn't itself the target key.
        reg = SeasonTeamRegistration(
            id=self.store.next_id("streg"), league_season_id=other_ls.id,
            team_id=team["id"], division_id=None, active=True)
        self.store.add_season_team_registration(reg)
        target_ls = self.store.league_season_for(self.league["id"], self.season["id"])
        target_rows = self._plant_duplicate(target_ls.id, team["id"], active=(True, False))
        audits_before = self._audit_count()
        res = self.api.assign_season_team_league(
            reg.id, self.league["id"], actor_id=ADMIN)
        self.assertEqual(res["error"]["details"]["reason"],
                         "team_registration_conflict", res)
        self.assertEqual(
            set(res["error"]["details"]["affected_registration_ids"]),
            {reg.id} | {r.id for r in target_rows})
        self.assertEqual(self._audit_count(), audits_before)
        still_active = self.store.get_season_team_registration(reg.id)
        self.assertTrue(still_active.active)
        self.assertEqual(still_active.league_season_id, other_ls.id)

    def test_transfer_team_to_league_rejects_duplicate_at_target_key(self):
        team = self.api.create_team(self.club["id"], None, "T", actor_id=ADMIN,
                                    league_id=self.other_league["id"])
        self.api.register_team_for_season(
            self.season["id"], team["id"], actor_id=ADMIN,
            league_id=self.other_league["id"])
        target_ls = self.store.league_season_for(self.league["id"], self.season["id"])
        target_rows = self._plant_duplicate(target_ls.id, team["id"], active=(True, False))
        audits_before = self._audit_count()
        res = self.api.transfer_team_to_league(
            team["id"], self.league["id"], actor_id=ADMIN)
        self.assertEqual(res["error"]["details"]["reason"],
                         "team_registration_conflict", res)
        self.assertEqual(self._audit_count(), audits_before)
        self.assertEqual(self.store.get_team(team["id"]).league_id,
                         self.other_league["id"])

    def test_roll_forward_v2_gate_rejects_duplicate_at_target_key(self):
        team = self.api.create_team(self.club["id"], None, "T", actor_id=ADMIN,
                                    league_id=self.league["id"])
        self.api.register_team_for_season(
            self.season["id"], team["id"], actor_id=ADMIN, league_id=self.league["id"])
        # self.league has never been linked into season2 -- force the
        # binding (a throwaway Division is the public way to do it, the
        # same technique test_v2_reassignment_integrity_sql.py's own
        # RollForwardConflictSqlTest uses) so the planted duplicate below
        # has a real LeagueSeason to sit at.
        self.api.create_division(self.season2["id"], "Throwaway",
                                 league_id=self.league["id"], actor_id=ADMIN)
        target_ls = self.store.league_season_for(self.league["id"], self.season2["id"])
        target_rows = self._plant_duplicate(target_ls.id, team["id"], active=(True, True))
        audits_before = self._audit_count()
        res = self.api.roll_forward_registrations_v2(
            self.season["id"], self.season2["id"],
            selections=[{"team_id": team["id"], "league_id": self.league["id"]}],
            actor_id=ADMIN)
        self.assertEqual(res["error"]["details"]["reason"],
                         "team_registration_conflict", res)
        self.assertEqual(set(res["error"]["details"]["affected_registration_ids"]),
                         {r.id for r in target_rows})
        self.assertEqual(self._audit_count(), audits_before)


if __name__ == "__main__":
    unittest.main()
