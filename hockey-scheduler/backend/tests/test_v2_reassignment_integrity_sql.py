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
from datetime import datetime, timezone

from hockey_scheduler.domain import (
    Game, IceSlotStatus, SeasonTeamRegistration, Team)
from hockey_scheduler.store import SqlStore

ADMIN = "admin"


# #283: a registration/Division no longer carries its own league_id — it is
# fixed by the LeagueSeason. Resolve the League from a raw store row.
def _reg_league(store, reg):
    ls = store.get_league_season(reg.league_season_id)
    return ls.league_id if ls else None


def _div_league(store, division_id):
    d = store.get_division(division_id)
    ls = store.get_league_season(d.league_season_id) if d else None
    return ls.league_id if ls else None


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
    def _game_slot(api, org, program, season_id):
        venue = api.create_venue("V", organization_id=org["id"],
                                 league_id=program["id"], actor_id=ADMIN)
        api.grant_season_venue_access(season_id, venue["id"], actor_id=ADMIN)
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
                                   program_id=program["id"], league_id=l1["id"])
            api.register_team_for_season(season["id"], team["id"], div["id"],
                                         actor_id=ADMIN, league_id=l1["id"])

            audits_before = len(store.all_setup_audit())
            res = api.assign_division_league(div["id"], l2["id"], actor_id=ADMIN,
                                             v2=True)
            self.assertEqual(res["error"]["code"], "validation_error", res)
            # The division row is unchanged and NO audit row was written.
            self.assertEqual(_div_league(store, div["id"]), l1["id"])
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
            self.assertEqual(_div_league(store, div["id"]), l1["id"])
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
                _reg_league(store,
                            store.get_season_team_registration(reg["id"])),
                l1["id"])

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
                                   program_id=program["id"], league_id=l1["id"])
            reg = api.register_team_for_season(season["id"], team["id"], d1["id"],
                                               actor_id=ADMIN, league_id=l1["id"])
            audits_before = len(store.all_setup_audit())
            res = api.assign_season_team_division(reg["id"], d2["id"],
                                                  actor_id=ADMIN, v2=True)
            self.assertEqual(res["error"]["code"], "validation_error", res)
            stored = store.get_season_team_registration(reg["id"])
            self.assertEqual(stored.division_id, d1["id"])
            self.assertEqual(_reg_league(store, stored), l1["id"])
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
            # #283 Slice E: a permanent-League team is pinned to its own League
            # (rule 7), short-circuiting before the game-strand guard this test
            # targets. Inject a league-less team_a and its registration directly
            # (register would backfill/pin the League) so the move L1→L2 reaches
            # the committed-game strand guard.
            _ta = Team(id=store.next_id("team"), name="A", club_id=club["id"],
                       program_id=program["id"])
            store.add_team(_ta)
            team_a = {"id": _ta.id}
            team_b = api.create_team(club["id"], None, "B", actor_id=ADMIN,
                                     program_id=program["id"], league_id=l1["id"])
            _reg_a = SeasonTeamRegistration(
                id=store.next_id("streg"),
                league_season_id=store.league_season_for(
                    l1["id"], season["id"]).id,
                team_id=team_a["id"], division_id=None, active=True)
            store.add_season_team_registration(_reg_a)
            reg_a = {"id": _reg_a.id}
            api.register_team_for_season(season["id"], team_b["id"],
                                         actor_id=ADMIN, league_id=l1["id"])
            # Inject a committed L1 game for the league-less team_a directly:
            # create_game now (correctly) refuses to put a league-less team in a
            # regular game (#283 rules 7-9), so this otherwise-unreachable state
            # is seeded straight into the store to exercise
            # assign_season_team_league's game-strand guard specifically.
            store.add_game(Game(
                id=store.next_id("game"), home_team_id=team_a["id"],
                away_team_id=team_b["id"],
                start_time=datetime(2026, 9, 1, 18, 30, tzinfo=timezone.utc),
                season_id=season["id"], league_id=l1["id"],
                league_season_id=store.league_season_for(
                    l1["id"], season["id"]).id))

            audits_before = len(store.all_setup_audit())
            res = api.assign_season_team_league(reg_a["id"], l2["id"],
                                                actor_id=ADMIN)
            self.assertEqual(res["error"]["code"], "validation_error", res)
            self.assertEqual(
                _reg_league(store,
                            store.get_season_team_registration(reg_a["id"])),
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
                                     program_id=program["id"], league_id=l1["id"])
            team_b = api.create_team(club["id"], None, "B", actor_id=ADMIN,
                                     program_id=program["id"], league_id=l1["id"])
            api.register_team_for_season(season["id"], team_a["id"],
                                         actor_id=ADMIN, league_id=l1["id"])
            api.register_team_for_season(season["id"], team_b["id"],
                                         actor_id=ADMIN, league_id=l1["id"])
            slot = self._game_slot(api, org, program, season["id"])

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


class ParticipationSeasonWideConflictSqlTest(_SqlIntegrityBase):
    """#331 review round 20 finding 1: a Team with an active registration
    in one League PLUS an active stray in a DIFFERENT League, in the same
    Season -- unlike the exact-key duplicate corruption
    test_exact_registration_identity.py covers (blocked by SQL's own
    ux_team_league_season unique index, Memory-only reachable), THIS shape
    is constructible on every backend: the two rows sit at two genuinely
    different LeagueSeason keys, so no unique index has anything to say
    about it. Proven here on the real SqlStore (SQLite always, PostgreSQL
    when TEST_DATABASE_URL is set) so the reviewer's explicit "Memory,
    SQLite, and PostgreSQL" bar is met on the actual production store, not
    just the in-memory one."""

    def test_register_team_for_season_rejects_active_stray_elsewhere_zero_mutation(self):
        def case(api, store):
            org, program = self._org_program(api)
            season = api.create_season(program["id"], "Fall", actor_id=ADMIN)
            l1 = api.create_league(season["id"], "L1", actor_id=ADMIN)
            l2 = api.create_league(season["id"], "L2", actor_id=ADMIN)
            club = api.create_club("C", actor_id=ADMIN)
            team = api.create_team(club["id"], None, "T", actor_id=ADMIN,
                                   program_id=program["id"], league_id=l1["id"])
            ls2 = store.league_season_for(l2["id"], season["id"])
            stray = SeasonTeamRegistration(
                id=store.next_id("streg"), league_season_id=ls2.id,
                team_id=team["id"], division_id=None, active=True)
            store.add_season_team_registration(stray)

            audits_before = len(store.all_setup_audit())
            res = api.register_team_for_season(
                season["id"], team["id"], actor_id=ADMIN, league_id=l1["id"])
            self.assertEqual(res["error"]["details"]["reason"],
                             "team_registration_conflict", res)
            self.assertEqual(res["error"]["details"]["affected_registration_ids"],
                             [stray.id])
            self.assertEqual(len(store.all_setup_audit()), audits_before)
            self.assertEqual(
                len(store.registrations_for_season(season["id"])), 1)
            still_active = store.get_season_team_registration(stray.id)
            self.assertTrue(still_active.active)

        self._run(case)

    def test_create_game_rejects_team_with_valid_plus_active_stray_zero_mutation(self):
        def case(api, store):
            org, program = self._org_program(api)
            season = api.create_season(program["id"], "Fall", actor_id=ADMIN)
            l1 = api.create_league(season["id"], "L1", actor_id=ADMIN)
            l2 = api.create_league(season["id"], "L2", actor_id=ADMIN)
            club = api.create_club("C", actor_id=ADMIN)
            team_a = api.create_team(club["id"], None, "A", actor_id=ADMIN,
                                     program_id=program["id"], league_id=l1["id"])
            team_b = api.create_team(club["id"], None, "B", actor_id=ADMIN,
                                     program_id=program["id"], league_id=l1["id"])
            api.register_team_for_season(season["id"], team_a["id"],
                                         actor_id=ADMIN, league_id=l1["id"])
            api.register_team_for_season(season["id"], team_b["id"],
                                         actor_id=ADMIN, league_id=l1["id"])
            # team_a picks up an active stray under l2 -- injected directly,
            # the same "legacy data / a write path predating this review"
            # shape every sibling test in this file uses (no live v2 write
            # path can build it once round_team_for_season's own guard,
            # proven above, is in place).
            ls2 = store.league_season_for(l2["id"], season["id"])
            stray = SeasonTeamRegistration(
                id=store.next_id("streg"), league_season_id=ls2.id,
                team_id=team_a["id"], division_id=None, active=True)
            store.add_season_team_registration(stray)
            slot = self._game_slot(api, org, program, season["id"])

            audits_before = len(store.all_setup_audit())
            res = api.create_game(season["id"], None, team_a["id"], team_b["id"],
                                  slot["id"], actor_id=ADMIN, league_id=l1["id"])
            self.assertIn("error", res, res)
            self.assertEqual(store.all_games(), [])
            self.assertEqual(store.get_ice_slot(slot["id"]).status,
                             IceSlotStatus.AVAILABLE)
            self.assertEqual(len(store.all_setup_audit()), audits_before)

        self._run(case)


class AssignClubOptionalSqlTest(_SqlIntegrityBase):
    """Club is optional on a Team in both v1 and v2 (#233 Slice D)."""

    def test_assign_club_null_unassigns_with_audit(self):
        def case(api, store):
            org, program = self._org_program(api)
            # #283 Slice E: a Team needs a resolvable permanent League — give the
            # program one so create_team resolves it (this test is about the
            # optional Club, not the League).
            season = api.create_season(program["id"], "Fall", actor_id=ADMIN)
            league = api.create_league(season["id"], "L", actor_id=ADMIN)
            club = api.create_club("C", actor_id=ADMIN)
            team = api.create_team(club["id"], None, "T", actor_id=ADMIN,
                                   program_id=program["id"], league_id=league["id"])
            audits_before = len(store.all_setup_audit())
            res = api.assign_team_club(team["id"], None, actor_id=ADMIN)
            self.assertNotIn("error", res, res)
            self.assertIsNone(res["club_id"])
            self.assertIsNone(store.get_team(team["id"]).club_id)
            audits = store.all_setup_audit()
            self.assertEqual(len(audits), audits_before + 1)
            last = audits[-1]
            self.assertEqual(last.action, "team_club_assigned")
            self.assertEqual(last.detail["from"], club["id"])
            self.assertIsNone(last.detail["to"])
            # No placeholder Club created as a side effect.
            self.assertEqual(len(store.all_clubs()), 1)

        self._run(case)

    def test_assign_unknown_club_rejected_zero_mutation(self):
        def case(api, store):
            org, program = self._org_program(api)
            # #283 Slice E: a Team needs a resolvable permanent League — give the
            # program one so create_team resolves it (this test is about the
            # optional Club, not the League).
            season = api.create_season(program["id"], "Fall", actor_id=ADMIN)
            league = api.create_league(season["id"], "L", actor_id=ADMIN)
            club = api.create_club("C", actor_id=ADMIN)
            team = api.create_team(club["id"], None, "T", actor_id=ADMIN,
                                   program_id=program["id"], league_id=league["id"])
            audits_before = len(store.all_setup_audit())
            res = api.assign_team_club(team["id"], "club_missing", actor_id=ADMIN)
            self.assertEqual(res["error"]["code"], "not_found", res)
            self.assertEqual(store.get_team(team["id"]).club_id, club["id"])
            self.assertEqual(len(store.all_setup_audit()), audits_before)

        self._run(case)


class RegisterProgramMatchSqlTest(_SqlIntegrityBase):
    def test_v2_register_program_less_team_rejected_zero_mutation(self):
        def case(api, store):
            org, program = self._org_program(api)
            season = api.create_season(program["id"], "Fall", actor_id=ADMIN)
            league = api.create_league(season["id"], "L", actor_id=ADMIN)
            club = api.create_club("C", actor_id=ADMIN)
            # A legacy Team with no program (create_team requires one → inject).
            team = Team(id=store.next_id("team"), name="Legacy",
                        club_id=club["id"], program_id=None)
            store.add_team(team)

            audits_before = len(store.all_setup_audit())
            res = api.register_team_for_season(
                season["id"], team.id, actor_id=ADMIN, league_id=league["id"])
            self.assertEqual(res["error"]["code"], "validation_error", res)
            self.assertEqual(store.registrations_for_season(season["id"]), [])
            self.assertEqual(len(store.all_setup_audit()), audits_before)

        self._run(case)


class RollForwardConflictSqlTest(_SqlIntegrityBase):
    def _two_league_target(self, api):
        # #283 Slice E: a rollover only ever targets a Team's OWN permanent
        # League. Each team's permanent League is the one it rolls into.
        org, program = self._org_program(api)
        s1 = api.create_season(program["id"], "S1", actor_id=ADMIN)
        s2 = api.create_season(program["id"], "S2", actor_id=ADMIN)
        l1t = api.create_league(s1["id"], "L1t", actor_id=ADMIN)
        l2t = api.create_league(s1["id"], "L2t", actor_id=ADMIN)
        club = api.create_club("C", actor_id=ADMIN)
        team_a = api.create_team(club["id"], None, "A", actor_id=ADMIN,
                                 league_id=l1t["id"])
        team_b = api.create_team(club["id"], None, "B", actor_id=ADMIN,
                                 league_id=l2t["id"])
        api.register_team_for_season(s1["id"], team_a["id"], actor_id=ADMIN,
                                     league_id=l1t["id"])
        api.register_team_for_season(s1["id"], team_b["id"], actor_id=ADMIN,
                                     league_id=l2t["id"])
        return s1, s2, l1t, l2t, team_a, team_b

    def test_rollover_into_non_permanent_league_rejected_zero_mutation(self):
        def case(api, store):
            s1, s2, l1t, l2t, team_a, team_b = self._two_league_target(api)
            audits_before = len(store.all_setup_audit())
            # A valid team_a→l1t (its permanent league) first, then the invalid
            # team_b→l1t (team_b's permanent league is l2t). The whole batch must
            # abort before any write.
            res = api.roll_forward_registrations_v2(
                s1["id"], s2["id"],
                selections=[{"team_id": team_a["id"], "league_id": l1t["id"]},
                            {"team_id": team_b["id"], "league_id": l1t["id"]}],
                actor_id=ADMIN)
            self.assertEqual(res["error"]["code"], "validation_error", res)
            self.assertEqual(res["error"]["details"]["reason"],
                             "rollover_league_not_team_league", res)
            active = {r.team_id: r for r in
                      store.registrations_for_season(s2["id"]) if r.active}
            self.assertEqual(active, {})
            self.assertEqual(len(store.all_setup_audit()), audits_before)

        self._run(case)

    def test_exact_match_active_target_idempotent_skip(self):
        def case(api, store):
            s1, s2, l1t, l2t, team_a, team_b = self._two_league_target(api)
            api.register_team_for_season(s2["id"], team_a["id"], actor_id=ADMIN,
                                         league_id=l1t["id"])
            res = api.roll_forward_registrations_v2(
                s1["id"], s2["id"],
                selections=[{"team_id": team_a["id"], "league_id": l1t["id"]}],
                actor_id=ADMIN)
            self.assertNotIn("error", res, res)
            self.assertEqual(res["rolled_forward"], 0, res)
            self.assertEqual(res["skipped"], 1, res)

        self._run(case)

    # -- #331 review round 18: exact-identity resolution in the target season

    def test_inactive_sibling_in_another_league_creates_distinct_active_row(self):
        def case(api, store):
            s1, s2, l1t, l2t, team_a, team_b = self._two_league_target(api)
            # l2t has never been linked into s2 -- force the binding (a
            # throwaway Division is the public way to do it) so the
            # injected stale row below has a real LeagueSeason to sit at.
            api.create_division(s2["id"], "L2t Div", league_id=l2t["id"],
                                actor_id=ADMIN)
            # A retained INACTIVE row from a former league sits at the
            # target season already (history from a prior unregister/
            # transfer cycle) -- NOT at team_a's real permanent league l1t.
            reg_stale = SeasonTeamRegistration(
                id=store.next_id("streg"),
                league_season_id=store.league_season_for(
                    l2t["id"], s2["id"]).id,
                team_id=team_a["id"], division_id=None, active=False)
            store.add_season_team_registration(reg_stale)

            res = api.roll_forward_registrations_v2(
                s1["id"], s2["id"],
                selections=[{"team_id": team_a["id"], "league_id": l1t["id"]}],
                actor_id=ADMIN)
            self.assertNotIn("error", res, res)
            self.assertEqual(res["rolled_forward"], 1, res)
            active = [r for r in store.registrations_for_season(s2["id"])
                     if r.active]
            self.assertEqual(len(active), 1)
            self.assertEqual(
                active[0].league_season_id,
                store.league_season_for(l1t["id"], s2["id"]).id)
            untouched = store.get_season_team_registration(reg_stale.id)
            self.assertFalse(untouched.active)
            self.assertEqual(untouched.league_season_id,
                             reg_stale.league_season_id)

        self._run(case)

    def test_active_row_in_another_league_always_rejected_game_free(self):
        def case(api, store):
            # v2 never silently MOVES an existing active registration to
            # match a selection (unlike v1/hierarchy import/
            # commit_teams_players_import's shared resolver) -- a mismatch
            # is always a contract violation the operator must resolve
            # explicitly, regardless of whether a game is even present to
            # strand. Proven here with NO game at all: this is not "safe to
            # auto-move but we're conservative", the rejection is
            # unconditional by design.
            s1, s2, l1t, l2t, team_a, team_b = self._two_league_target(api)
            api.create_division(s2["id"], "L2t Div", league_id=l2t["id"],
                                actor_id=ADMIN)
            reg_other = SeasonTeamRegistration(
                id=store.next_id("streg"),
                league_season_id=store.league_season_for(
                    l2t["id"], s2["id"]).id,
                team_id=team_a["id"], division_id=None, active=True)
            store.add_season_team_registration(reg_other)

            audits_before = len(store.all_setup_audit())
            res = api.roll_forward_registrations_v2(
                s1["id"], s2["id"],
                selections=[{"team_id": team_a["id"], "league_id": l1t["id"]}],
                actor_id=ADMIN)
            self.assertEqual(res["error"]["code"], "validation_error", res)
            self.assertEqual(res["error"]["details"]["reason"],
                             "rollover_conflicts_active_registration", res)
            self.assertEqual(
                res["error"]["details"]["affected_registration_ids"],
                [reg_other.id])
            self.assertEqual(len(store.all_setup_audit()), audits_before)
            still_active = store.get_season_team_registration(reg_other.id)
            self.assertTrue(still_active.active)
            self.assertEqual(still_active.league_season_id,
                             reg_other.league_season_id)

        self._run(case)

    def test_active_row_in_another_league_rejected_same_way_with_committed_game(self):
        def case(api, store):
            # The SAME rejection (never a "strands_games" variant) whether
            # or not a committed game references the stale row -- v2 never
            # attempts the move that would strand it in the first place, so
            # it never needs game-awareness to stay safe.
            s1, s2, l1t, l2t, team_a, team_b = self._two_league_target(api)
            api.create_division(s2["id"], "L2t Div", league_id=l2t["id"],
                                actor_id=ADMIN)
            reg_other = SeasonTeamRegistration(
                id=store.next_id("streg"),
                league_season_id=store.league_season_for(
                    l2t["id"], s2["id"]).id,
                team_id=team_a["id"], division_id=None, active=True)
            store.add_season_team_registration(reg_other)
            store.add_game(Game(
                id=store.next_id("game"), home_team_id=team_a["id"],
                away_team_id=None,
                start_time=datetime(2026, 9, 1, tzinfo=timezone.utc),
                season_id=s2["id"], league_id=l2t["id"], cancelled=False))

            audits_before = len(store.all_setup_audit())
            res = api.roll_forward_registrations_v2(
                s1["id"], s2["id"],
                selections=[{"team_id": team_a["id"], "league_id": l1t["id"]}],
                actor_id=ADMIN)
            self.assertEqual(res["error"]["code"], "validation_error", res)
            self.assertEqual(res["error"]["details"]["reason"],
                             "rollover_conflicts_active_registration", res)
            self.assertEqual(len(store.all_setup_audit()), audits_before)

        self._run(case)

    # -- #331 review round 19: explicit registration_id identity ------------

    def test_explicit_registration_id_matching_the_source_row_still_rolls(self):
        def case(api, store):
            s1, s2, l1t, l2t, team_a, team_b = self._two_league_target(api)
            src_reg = next(r for r in store.registrations_for_season(s1["id"])
                          if r.team_id == team_a["id"])
            res = api.roll_forward_registrations_v2(
                s1["id"], s2["id"],
                selections=[{"team_id": team_a["id"], "league_id": l1t["id"],
                             "registration_id": src_reg.id}],
                actor_id=ADMIN)
            self.assertNotIn("error", res, res)
            self.assertEqual(res["rolled_forward"], 1, res)

        self._run(case)

    def test_registration_id_for_a_different_team_rejected_zero_mutation(self):
        def case(api, store):
            # #331 review round 19 finding 4: the frontend's Season rollover
            # panel now sends the SOURCE registration id it rendered a row
            # for, keyed independently of team_id (two rows for one team must
            # never collide on a shared DOM/selection key). The backend must
            # cross-check it against team_id, not trust team_id alone --
            # otherwise a stale/mismatched registration_id (a bug, or a
            # tampered request) would silently pass unnoticed.
            s1, s2, l1t, l2t, team_a, team_b = self._two_league_target(api)
            team_b_reg = next(
                r for r in store.registrations_for_season(s1["id"])
                if r.team_id == team_b["id"])
            audits_before = len(store.all_setup_audit())
            res = api.roll_forward_registrations_v2(
                s1["id"], s2["id"],
                selections=[{"team_id": team_a["id"], "league_id": l1t["id"],
                             "registration_id": team_b_reg.id}],
                actor_id=ADMIN)
            self.assertEqual(res["error"]["code"], "validation_error", res)
            self.assertEqual(res["error"]["details"]["reason"],
                             "rollover_registration_mismatch", res)
            self.assertEqual(res["error"]["details"]["team_id"], team_a["id"])
            active = {r.team_id: r for r in
                      store.registrations_for_season(s2["id"]) if r.active}
            self.assertEqual(active, {})
            self.assertEqual(len(store.all_setup_audit()), audits_before)

        self._run(case)

    def test_inactive_registration_id_rejected_zero_mutation(self):
        def case(api, store):
            s1, s2, l1t, l2t, team_a, team_b = self._two_league_target(api)
            # team_a keeps its own real ACTIVE source registration (so it
            # still passes the "is this team registered in the source
            # season at all" gate) -- a SEPARATE, inactive row is what the
            # selection actually names, so this exercises the registration_
            # id cross-check specifically, not the earlier not-in-source-
            # season check.
            l3 = api.create_league(s1["id"], "L3", actor_id=ADMIN)
            api.create_division(s1["id"], "L3 Div", league_id=l3["id"],
                                actor_id=ADMIN)
            inactive_reg = SeasonTeamRegistration(
                id=store.next_id("streg"),
                league_season_id=store.league_season_for(l3["id"], s1["id"]).id,
                team_id=team_a["id"], division_id=None, active=False)
            store.add_season_team_registration(inactive_reg)
            audits_before = len(store.all_setup_audit())
            res = api.roll_forward_registrations_v2(
                s1["id"], s2["id"],
                selections=[{"team_id": team_a["id"], "league_id": l1t["id"],
                             "registration_id": inactive_reg.id}],
                actor_id=ADMIN)
            self.assertEqual(res["error"]["code"], "validation_error", res)
            self.assertEqual(res["error"]["details"]["reason"],
                             "rollover_registration_mismatch", res)
            active = {r.team_id: r for r in
                      store.registrations_for_season(s2["id"]) if r.active}
            self.assertEqual(active, {})
            self.assertEqual(len(store.all_setup_audit()), audits_before)

        self._run(case)

    def test_duplicate_team_selection_in_one_batch_rejected_zero_mutation(self):
        def case(api, store):
            # #331 review round 19 finding 4: two selections for the SAME
            # team_id (e.g. two source rows for a Team with a Rule 7
            # violation, both checked in the UI) is unresolvable ambiguity
            # -- the old `wanted[tid] = ...` aggregation silently kept only
            # the LAST one; this must reject the whole batch instead.
            s1, s2, l1t, l2t, team_a, team_b = self._two_league_target(api)
            audits_before = len(store.all_setup_audit())
            res = api.roll_forward_registrations_v2(
                s1["id"], s2["id"],
                selections=[
                    {"team_id": team_a["id"], "league_id": l1t["id"]},
                    {"team_id": team_a["id"], "league_id": l1t["id"],
                     "division_id": None},
                ],
                actor_id=ADMIN)
            self.assertEqual(res["error"]["code"], "validation_error", res)
            self.assertEqual(res["error"]["details"]["reason"],
                             "rollover_duplicate_team_selection", res)
            self.assertEqual(res["error"]["details"]["team_id"], team_a["id"])
            active = {r.team_id: r for r in
                      store.registrations_for_season(s2["id"]) if r.active}
            self.assertEqual(active, {})
            self.assertEqual(len(store.all_setup_audit()), audits_before)

        self._run(case)


class TransferRetainedTargetSqlTest(_SqlIntegrityBase):
    """#331 review round 18: _transfer_team_to_league_inner must never
    blindly rebind a Team's active registration onto its target
    LeagueSeason -- a retained/duplicate row may already sit there (history
    from a prior assignment/transfer cycle, or legacy Rule 7 drift). Proven
    on the REAL SQL store so a collision surfaces as the structured
    rejection this function documents ("Apply -- all writes happen only
    after every check passed"), never a raw unique_violation."""

    def _team_with_two_leagues(self, api):
        org, program = self._org_program(api)
        season = api.create_season(program["id"], "Fall", actor_id=ADMIN)
        l1 = api.create_league(season["id"], "L1", actor_id=ADMIN)
        l2 = api.create_league(season["id"], "L2", actor_id=ADMIN)
        club = api.create_club("C", actor_id=ADMIN)
        team = api.create_team(club["id"], None, "T", actor_id=ADMIN,
                               program_id=program["id"], league_id=l1["id"])
        reg_l1 = api.register_team_for_season(
            season["id"], team["id"], actor_id=ADMIN, league_id=l1["id"])
        return season, l1, l2, team, reg_l1

    def test_reactivates_retained_inactive_row_at_target(self):
        def case(api, store):
            season, l1, l2, team, reg_l1 = self._team_with_two_leagues(api)
            # A retained INACTIVE row already sits at the target League --
            # history from a prior cycle no current write path can leave
            # behind starting fresh, so inject it directly.
            reg_l2 = SeasonTeamRegistration(
                id=store.next_id("streg"),
                league_season_id=store.league_season_for(
                    l2["id"], season["id"]).id,
                team_id=team["id"], division_id=None, active=False)
            store.add_season_team_registration(reg_l2)

            res = api.transfer_team_to_league(team["id"], l2["id"],
                                              actor_id=ADMIN)
            self.assertNotIn("error", res, res)
            self.assertEqual(store.get_team(team["id"]).league_id, l2["id"])
            # The RETAINED row was reactivated in place (same id) ...
            reactivated = store.get_season_team_registration(reg_l2.id)
            self.assertTrue(reactivated.active)
            self.assertEqual(
                _reg_league(store, reactivated), l2["id"])
            # ... and the SOURCE row was retired (deactivated), not deleted
            # or rebound onto the target's unique key.
            retired = store.get_season_team_registration(reg_l1["id"])
            self.assertFalse(retired.active)
            self.assertEqual(_reg_league(store, retired), l1["id"])
            # No third row was created.
            self.assertEqual(
                len(store.registrations_for_season(season["id"])), 2)

        self._run(case)

    def test_rejects_when_target_already_has_an_active_row_zero_mutation(self):
        def case(api, store):
            season, l1, l2, team, reg_l1 = self._team_with_two_leagues(api)
            # The team ALREADY has a second active row at the target League
            # -- a legacy Rule 7 violation (two concurrent active
            # registrations for one team in one season) no current write
            # path can produce starting fresh. Moving reg_l1 onto it would
            # either silently duplicate (Memory) or raise a raw
            # unique_violation (SQL) instead of a structured rejection.
            reg_l2 = SeasonTeamRegistration(
                id=store.next_id("streg"),
                league_season_id=store.league_season_for(
                    l2["id"], season["id"]).id,
                team_id=team["id"], division_id=None, active=True)
            store.add_season_team_registration(reg_l2)

            audits_before = len(store.all_setup_audit())
            res = api.transfer_team_to_league(team["id"], l2["id"],
                                              actor_id=ADMIN)
            self.assertEqual(res["error"]["code"], "validation_error", res)
            self.assertEqual(res["error"]["details"]["reason"],
                             "team_registration_conflict", res)
            # Zero mutation: Team's permanent league unchanged, both rows
            # exactly as they were, no audit written.
            self.assertEqual(store.get_team(team["id"]).league_id, l1["id"])
            still_l1 = store.get_season_team_registration(reg_l1["id"])
            self.assertTrue(still_l1.active)
            self.assertEqual(_reg_league(store, still_l1), l1["id"])
            still_l2 = store.get_season_team_registration(reg_l2.id)
            self.assertTrue(still_l2.active)
            self.assertEqual(_reg_league(store, still_l2), l2["id"])
            self.assertEqual(len(store.all_setup_audit()), audits_before)

        self._run(case)


class AssignLeagueRetainedTargetSqlTest(_SqlIntegrityBase):
    """#331 review round 18: assign_season_team_league must never blindly
    rebind onto a retained/duplicate row at the target LeagueSeason either
    -- the same defect class as TransferRetainedTargetSqlTest above, for the
    sibling reassignment entry point."""

    def _reg_with_two_leagues(self, api, store):
        org, program = self._org_program(api)
        season = api.create_season(program["id"], "Fall", actor_id=ADMIN)
        l1 = api.create_league(season["id"], "L1", actor_id=ADMIN)
        l2 = api.create_league(season["id"], "L2", actor_id=ADMIN)
        club = api.create_club("C", actor_id=ADMIN)
        # No permanent league on the Team -- assign_season_team_league's own
        # rule-7 guard would otherwise refuse to move a pinned Team's
        # registration away from its permanent league before this test's
        # target logic is even reached. create_team now REQUIRES a
        # resolvable league, so a league-less Team (legacy data predating
        # rule 2, or any other write path that never backfilled one) has to
        # be injected directly -- the same technique
        # AssignLeagueGameStrandSqlTest uses above.
        team_obj = Team(id=store.next_id("team"), name="T", club_id=club["id"],
                        program_id=program["id"])
        store.add_team(team_obj)
        team = {"id": team_obj.id}
        reg_l1_obj = SeasonTeamRegistration(
            id=store.next_id("streg"),
            league_season_id=store.league_season_for(l1["id"], season["id"]).id,
            team_id=team["id"], division_id=None, active=True)
        store.add_season_team_registration(reg_l1_obj)
        reg_l1 = {"id": reg_l1_obj.id}
        return season, l1, l2, team, reg_l1

    def test_reactivates_retained_inactive_row_and_retires_source(self):
        def case(api, store):
            season, l1, l2, team, reg_l1 = self._reg_with_two_leagues(api, store)
            reg_l2 = SeasonTeamRegistration(
                id=store.next_id("streg"),
                league_season_id=store.league_season_for(
                    l2["id"], season["id"]).id,
                team_id=team["id"], division_id=None, active=False)
            store.add_season_team_registration(reg_l2)

            res = api.assign_season_team_league(reg_l1["id"], l2["id"],
                                                actor_id=ADMIN)
            self.assertNotIn("error", res, res)
            self.assertEqual(res["id"], reg_l2.id)  # the RETAINED row, reused
            reactivated = store.get_season_team_registration(reg_l2.id)
            self.assertTrue(reactivated.active)
            self.assertEqual(_reg_league(store, reactivated), l2["id"])
            retired = store.get_season_team_registration(reg_l1["id"])
            self.assertFalse(retired.active)
            self.assertEqual(
                len(store.registrations_for_season(season["id"])), 2)

        self._run(case)

    def test_rejects_when_target_already_has_an_active_row_zero_mutation(self):
        def case(api, store):
            season, l1, l2, team, reg_l1 = self._reg_with_two_leagues(api, store)
            reg_l2 = SeasonTeamRegistration(
                id=store.next_id("streg"),
                league_season_id=store.league_season_for(
                    l2["id"], season["id"]).id,
                team_id=team["id"], division_id=None, active=True)
            store.add_season_team_registration(reg_l2)

            audits_before = len(store.all_setup_audit())
            res = api.assign_season_team_league(reg_l1["id"], l2["id"],
                                                actor_id=ADMIN)
            self.assertEqual(res["error"]["code"], "validation_error", res)
            self.assertEqual(res["error"]["details"]["reason"],
                             "team_registration_conflict", res)
            still_l1 = store.get_season_team_registration(reg_l1["id"])
            self.assertTrue(still_l1.active)
            self.assertEqual(_reg_league(store, still_l1), l1["id"])
            still_l2 = store.get_season_team_registration(reg_l2.id)
            self.assertTrue(still_l2.active)
            self.assertEqual(len(store.all_setup_audit()), audits_before)

        self._run(case)


if __name__ == "__main__":
    unittest.main()
