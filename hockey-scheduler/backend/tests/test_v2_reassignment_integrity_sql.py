"""v2 reassignment / game integrity on the REAL SQL store (#233 Slice C2 review).

The canonical-invariant guards in ``test_v2_canonical_invariants`` run over the
in-memory store. This file re-exercises the mutation-integrity FAILURE paths the
reviewer flagged against the actual ``SqlStore`` so the "zero record / zero audit
mutation on rejection" property is proven where it matters: the SQL transaction
is a lock, not a rollback, so a guard that writes-then-raises would leave a
partial mutation behind. Each case asserts the structured ``validation_error``
AND that the store row + audit-log count are byte-identical before and after.

Runs on SQLite (``:memory:``) always, and on PostgreSQL when ``TEST_DATABASE_URL``
is set (CI), so the reviewer's "both SQLite and PostgreSQL CI" bar is met by the
same assertions on both backends.
"""

import os
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import IceSlotStatus
from hockey_scheduler.store import SqlStore

ADMIN = "admin"


def _sql_backends():
    backends = [("sqlite", ":memory:")]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        backends.append(("postgres", url))
    return backends


def _fresh(url):
    store = SqlStore(url)
    if url != ":memory:":
        store.reset_schema()
    return store


def _teardown(store):
    # The sqlite backend is in-memory and discarded on close; only the shared
    # PostgreSQL database needs to be returned to a clean canonical baseline so
    # it can't leak rows into the next test.
    try:
        if store.backend == "postgres":
            store.reset_schema()
    finally:
        store.close()


class _SqlIntegrityBase(unittest.TestCase):
    """Runs each ``_case`` closure against every SQL backend, sharing the
    before/after zero-mutation bookkeeping so the individual tests stay focused
    on WHAT is rejected, not the plumbing."""

    def _run(self, case):
        for name, url in _sql_backends():
            with self.subTest(backend=name):
                store = _fresh(url)
                try:
                    api = ApiService(store)
                    case(api, store)
                finally:
                    _teardown(store)

    # -- shared scenario builders (over the real SqlStore) ------------------
    @staticmethod
    def _org_program(api):
        org = api.create_organization("Org", "O", actor_id=ADMIN)
        program = api.create_program("Prog", operator_organization_id=org["id"],
                                     actor_id=ADMIN)
        return org, program

    @staticmethod
    def _game_slot(api, org, program):
        venue = api.create_venue("V", organization_id=org["id"],
                                 league_id=program["id"], actor_id=ADMIN)
        rink = api.create_rink(venue["id"], "R", actor_id=ADMIN)
        return api.create_ice_slot(
            rink["id"], "2026-09-01T18:30:00+00:00",
            "2026-09-01T20:00:00+00:00", "game", actor_id=ADMIN)


class DivisionReparentStrandSqlTest(_SqlIntegrityBase):
    def test_reparent_stranding_a_registration_rejected_zero_mutation(self):
        def case(api, store):
            org, program = self._org_program(api)
            season = api.create_season(program["id"], "Fall", actor_id=ADMIN)
            l1 = api.create_league(season["id"], "L1", actor_id=ADMIN)
            l2 = api.create_league(season["id"], "L2", actor_id=ADMIN)
            div = api.create_division_v2(l1["id"], "D", actor_id=ADMIN)
            club = api.create_club("C", actor_id=ADMIN)
            team = api.create_team(club["id"], None, "T", actor_id=ADMIN,
                                   program_id=program["id"])
            api.register_team_for_season(season["id"], team["id"], div["id"],
                                         actor_id=ADMIN, league_id=l1["id"])

            audits_before = len(store.all_setup_audit())
            res = api.assign_division_league(div["id"], l2["id"], actor_id=ADMIN,
                                             v2=True)
            self.assertEqual(res["error"]["code"], "validation_error", res)
            # The division row is unchanged and NO audit row was written.
            self.assertEqual(store.get_division(div["id"]).league_id, l1["id"])
            self.assertEqual(len(store.all_setup_audit()), audits_before)

        self._run(case)

    def test_reparent_requires_league_zero_mutation(self):
        def case(api, store):
            org, program = self._org_program(api)
            season = api.create_season(program["id"], "Fall", actor_id=ADMIN)
            l1 = api.create_league(season["id"], "L1", actor_id=ADMIN)
            div = api.create_division_v2(l1["id"], "D", actor_id=ADMIN)
            audits_before = len(store.all_setup_audit())
            res = api.assign_division_league(div["id"], None, actor_id=ADMIN,
                                             v2=True)
            self.assertEqual(res["error"]["code"], "validation_error", res)
            self.assertEqual(store.get_division(div["id"]).league_id, l1["id"])
            self.assertEqual(len(store.all_setup_audit()), audits_before)

        self._run(case)


class AssignDivisionLeagueSqlTest(_SqlIntegrityBase):
    def test_clearing_division_preserves_league(self):
        def case(api, store):
            org, program = self._org_program(api)
            season = api.create_season(program["id"], "Fall", actor_id=ADMIN)
            l1 = api.create_league(season["id"], "L1", actor_id=ADMIN)
            d1 = api.create_division_v2(l1["id"], "D1", actor_id=ADMIN)
            club = api.create_club("C", actor_id=ADMIN)
            team = api.create_team(club["id"], None, "T", actor_id=ADMIN,
                                   program_id=program["id"])
            reg = api.register_team_for_season(season["id"], team["id"], d1["id"],
                                               actor_id=ADMIN, league_id=l1["id"])
            cleared = api.assign_season_team_division(reg["id"], None,
                                                      actor_id=ADMIN, v2=True)
            self.assertNotIn("error", cleared, cleared)
            self.assertIsNone(cleared["division_id"])
            # Required League preserved through the SQL round-trip.
            self.assertEqual(cleared["league_id"], l1["id"])
            self.assertEqual(
                store.get_season_team_registration(reg["id"]).league_id, l1["id"])

        self._run(case)

    def test_setting_cross_league_division_rejected_zero_mutation(self):
        def case(api, store):
            org, program = self._org_program(api)
            season = api.create_season(program["id"], "Fall", actor_id=ADMIN)
            l1 = api.create_league(season["id"], "L1", actor_id=ADMIN)
            l2 = api.create_league(season["id"], "L2", actor_id=ADMIN)
            d1 = api.create_division_v2(l1["id"], "D1", actor_id=ADMIN)
            d2 = api.create_division_v2(l2["id"], "D2", actor_id=ADMIN)
            club = api.create_club("C", actor_id=ADMIN)
            team = api.create_team(club["id"], None, "T", actor_id=ADMIN,
                                   program_id=program["id"])
            reg = api.register_team_for_season(season["id"], team["id"], d1["id"],
                                               actor_id=ADMIN, league_id=l1["id"])
            audits_before = len(store.all_setup_audit())
            res = api.assign_season_team_division(reg["id"], d2["id"],
                                                  actor_id=ADMIN, v2=True)
            self.assertEqual(res["error"]["code"], "validation_error", res)
            stored = store.get_season_team_registration(reg["id"])
            self.assertEqual(stored.division_id, d1["id"])
            self.assertEqual(stored.league_id, l1["id"])
            self.assertEqual(len(store.all_setup_audit()), audits_before)

        self._run(case)


class AssignLeagueGameStrandSqlTest(_SqlIntegrityBase):
    def test_league_change_blocked_by_committed_game_zero_mutation(self):
        def case(api, store):
            org, program = self._org_program(api)
            season = api.create_season(program["id"], "Fall", actor_id=ADMIN)
            l1 = api.create_league(season["id"], "L1", actor_id=ADMIN)
            l2 = api.create_league(season["id"], "L2", actor_id=ADMIN)
            club = api.create_club("C", actor_id=ADMIN)
            team_a = api.create_team(club["id"], None, "A", actor_id=ADMIN,
                                     program_id=program["id"])
            team_b = api.create_team(club["id"], None, "B", actor_id=ADMIN,
                                     program_id=program["id"])
            reg_a = api.register_team_for_season(season["id"], team_a["id"],
                                                 actor_id=ADMIN, league_id=l1["id"])
            api.register_team_for_season(season["id"], team_b["id"],
                                         actor_id=ADMIN, league_id=l1["id"])
            slot = self._game_slot(api, org, program)
            game = api.create_game(season["id"], None, team_a["id"], team_b["id"],
                                   slot["id"], actor_id=ADMIN, league_id=l1["id"])
            self.assertNotIn("error", game, game)

            audits_before = len(store.all_setup_audit())
            res = api.assign_season_team_league(reg_a["id"], l2["id"],
                                                actor_id=ADMIN)
            self.assertEqual(res["error"]["code"], "validation_error", res)
            self.assertEqual(
                store.get_season_team_registration(reg_a["id"]).league_id,
                l1["id"])
            self.assertEqual(len(store.all_setup_audit()), audits_before)

        self._run(case)


class GameRegistrationLeagueSqlTest(_SqlIntegrityBase):
    def test_game_wrong_league_rejected_zero_mutation(self):
        def case(api, store):
            org, program = self._org_program(api)
            season = api.create_season(program["id"], "Fall", actor_id=ADMIN)
            l1 = api.create_league(season["id"], "L1", actor_id=ADMIN)
            l2 = api.create_league(season["id"], "L2", actor_id=ADMIN)
            club = api.create_club("C", actor_id=ADMIN)
            team_a = api.create_team(club["id"], None, "A", actor_id=ADMIN,
                                     program_id=program["id"])
            team_b = api.create_team(club["id"], None, "B", actor_id=ADMIN,
                                     program_id=program["id"])
            api.register_team_for_season(season["id"], team_a["id"],
                                         actor_id=ADMIN, league_id=l1["id"])
            api.register_team_for_season(season["id"], team_b["id"],
                                         actor_id=ADMIN, league_id=l1["id"])
            slot = self._game_slot(api, org, program)

            audits_before = len(store.all_setup_audit())
            # Game scoped to L2 while both teams are registered in L1 → rejected.
            res = api.create_game(season["id"], None, team_a["id"], team_b["id"],
                                  slot["id"], actor_id=ADMIN, league_id=l2["id"])
            self.assertEqual(res["error"]["code"], "validation_error", res)
            # No game row persisted, the slot is still AVAILABLE, no audit grew.
            self.assertEqual(store.all_games(), [])
            self.assertEqual(store.get_ice_slot(slot["id"]).status,
                             IceSlotStatus.AVAILABLE)
            self.assertEqual(len(store.all_setup_audit()), audits_before)

        self._run(case)


if __name__ == "__main__":
    unittest.main()
