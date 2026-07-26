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

#331 review round 20 finding 1 widens the invariant: an exact-key conflict
(one LeagueSeason, 2+ rows) is only ONE way a Team's participation in a
Season can be ambiguous. A Team can just as easily hold two unambiguous
single rows at TWO DIFFERENT LeagueSeasons in the same Season -- e.g. a
stale active row a since-superseded League transfer left behind under the
Team's OLD League, alongside a genuinely valid one under its current
permanent League -- constructible on EVERY backend (SQL's unique index is
scoped to one exact key; it has nothing to say about two different keys).
The required invariant is stricter than "no exact-key collision": live
participation exists only when the Team has EXACTLY ONE active registration
in the Season, full stop, and it is the one the caller expects. The new
shared primitive, ``league_scope.team_season_participation(store,
season_id, team_id)``, answers exactly that question -- season-wide, ACTIVE
rows only, a complement to (never a replacement for) the narrower
exact-key check, which stays unconditional on active state and must keep
running independently wherever both matter. Covers the primitive directly,
its cascade through ``team_registration_valid`` into every one of that
function's four callers (``create_game``, ``move_game``/``publish_game``
via ``_revalidate_game_participation``, ``_require_team_registered``,
``_registration_is_operational``/``get_demo_overview``), the widened
``register_team_for_season`` write-time guard, the ``_require_batch_team_
participation`` draft-commit fix (which closes an actual accept-when-
should-reject bug for the exact-key case, not merely a blind spot), and
``_require_team_registered``'s insertion-order-independent conflict reason.
"""

import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    DivisionMismatchError, Role, SeasonTeamRegistration)
from hockey_scheduler.services.context_scope import _team_season_ids
from hockey_scheduler.services.league_scope import (
    exact_registration_or_conflict, team_registration_valid,
    team_season_participation)
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


class TeamSeasonParticipationTest(_Base):
    """#331 review round 20: the season-wide primitive itself -- 0/1/2+
    ACTIVE rows anywhere in the Season, across any number of different
    Leagues, insertion-order independent. Complements (never replaces)
    ExactRegistrationOrConflictTest above -- this one is active-only and
    blind to a same-key active+inactive pair by design (that shape is still
    exact_registration_or_conflict's job, unconditional on active state; see
    ReadPathFailClosedSeasonWideTest below for the two checks layered
    together)."""

    def setUp(self):
        super().setUp()
        self.program = self.api.create_program("P", actor_id=ADMIN)
        self.season = self.api.create_season(self.program["id"], "S", actor_id=ADMIN)
        self.league_a = self.api.create_league(self.season["id"], "A", actor_id=ADMIN)
        self.league_b = self.api.create_league(self.season["id"], "B", actor_id=ADMIN)
        club = self.api.create_club("C", actor_id=ADMIN)
        self.team = self.api.create_team(club["id"], None, "T", actor_id=ADMIN,
                                         league_id=self.league_a["id"])

    def _inject(self, league, active=True):
        ls = self.store.league_season_for(league["id"], self.season["id"])
        reg = SeasonTeamRegistration(
            id=self.store.next_id("streg"), league_season_id=ls.id,
            team_id=self.team["id"], division_id=None, active=active)
        self.store.add_season_team_registration(reg)
        return reg

    def test_zero_active_rows_returns_none_no_conflict(self):
        reg, conflicts = team_season_participation(
            self.store, self.season["id"], self.team["id"])
        self.assertIsNone(reg)
        self.assertEqual(conflicts, [])

    def test_none_season_id_returns_none_no_conflict(self):
        reg, conflicts = team_season_participation(
            self.store, None, self.team["id"])
        self.assertIsNone(reg)
        self.assertEqual(conflicts, [])

    def test_one_active_row_returns_it(self):
        real = self._inject(self.league_a, active=True)
        reg, conflicts = team_season_participation(
            self.store, self.season["id"], self.team["id"])
        self.assertEqual(reg.id, real.id)
        self.assertEqual(conflicts, [])

    def test_inactive_row_alone_does_not_count(self):
        self._inject(self.league_a, active=False)
        reg, conflicts = team_season_participation(
            self.store, self.season["id"], self.team["id"])
        self.assertIsNone(reg)
        self.assertEqual(conflicts, [])

    def test_active_row_plus_inactive_sibling_elsewhere_is_still_unambiguous(self):
        # The inactive row is history, not a live competitor -- only ACTIVE
        # rows count toward season-wide ambiguity here.
        active = self._inject(self.league_a, active=True)
        self._inject(self.league_b, active=False)
        reg, conflicts = team_season_participation(
            self.store, self.season["id"], self.team["id"])
        self.assertEqual(reg.id, active.id)
        self.assertEqual(conflicts, [])

    def test_two_active_rows_at_different_leagues_is_a_conflict(self):
        a = self._inject(self.league_a, active=True)
        b = self._inject(self.league_b, active=True)
        reg, conflicts = team_season_participation(
            self.store, self.season["id"], self.team["id"])
        self.assertIsNone(reg)
        self.assertEqual(set(conflicts), {a.id, b.id})

    def test_conflict_detection_is_insertion_order_independent(self):
        self._inject(self.league_a, active=True)
        self._inject(self.league_b, active=True)
        _, conflicts_forward = team_season_participation(
            self.store, self.season["id"], self.team["id"])
        club2 = self.api.create_club("C2", actor_id=ADMIN)
        team2 = self.api.create_team(club2["id"], None, "T2", actor_id=ADMIN,
                                     league_id=self.league_a["id"])
        ls_b = self.store.league_season_for(self.league_b["id"], self.season["id"])
        ls_a = self.store.league_season_for(self.league_a["id"], self.season["id"])
        self.store.add_season_team_registration(SeasonTeamRegistration(
            id=self.store.next_id("streg"), league_season_id=ls_b.id,
            team_id=team2["id"], division_id=None, active=True))
        self.store.add_season_team_registration(SeasonTeamRegistration(
            id=self.store.next_id("streg"), league_season_id=ls_a.id,
            team_id=team2["id"], division_id=None, active=True))
        _, conflicts_reversed = team_season_participation(
            self.store, self.season["id"], team2["id"])
        self.assertEqual(len(conflicts_forward), 2)
        self.assertEqual(len(conflicts_reversed), 2)


class ReadPathFailClosedSeasonWideTest(_Base):
    """#331 review round 20: team_registration_valid fails closed
    season-wide too, not just at the exact key -- a single unambiguous row
    at the Team's OWN permanent League is no longer trusted if an active
    stray also exists elsewhere in the Season. The exact-key check
    (round 19) and this season-wide check are layered, not substitutes for
    each other."""

    def setUp(self):
        super().setUp()
        self.program = self.api.create_program("P", actor_id=ADMIN)
        self.season = self.api.create_season(self.program["id"], "S", actor_id=ADMIN)
        self.league_a = self.api.create_league(self.season["id"], "A", actor_id=ADMIN)
        self.league_b = self.api.create_league(self.season["id"], "B", actor_id=ADMIN)
        club = self.api.create_club("C", actor_id=ADMIN)
        self.team = self.api.create_team(club["id"], None, "T", actor_id=ADMIN,
                                         league_id=self.league_a["id"])

    def _inject(self, league, active=True):
        ls = self.store.league_season_for(league["id"], self.season["id"])
        reg = SeasonTeamRegistration(
            id=self.store.next_id("streg"), league_season_id=ls.id,
            team_id=self.team["id"], division_id=None, active=active)
        self.store.add_season_team_registration(reg)
        return reg

    def test_valid_row_at_own_league_resolves_with_no_stray(self):
        # Sanity check: the fixture's basic path resolves fine on its own.
        real = self.api.register_team_for_season(
            self.season["id"], self.team["id"], actor_id=ADMIN,
            league_id=self.league_a["id"])
        reg = team_registration_valid(
            self.store, self.store.get_season(self.season["id"]), self.team["id"])
        self.assertEqual(reg.id, real["id"])

    def test_valid_row_stops_resolving_once_an_active_stray_exists(self):
        self.api.register_team_for_season(
            self.season["id"], self.team["id"], actor_id=ADMIN,
            league_id=self.league_a["id"])
        self._inject(self.league_b, active=True)
        self.assertIsNone(team_registration_valid(
            self.store, self.store.get_season(self.season["id"]), self.team["id"]))

    def test_inactive_stray_never_blocks_the_valid_row(self):
        real = self.api.register_team_for_season(
            self.season["id"], self.team["id"], actor_id=ADMIN,
            league_id=self.league_a["id"])
        self._inject(self.league_b, active=False)
        reg = team_registration_valid(
            self.store, self.store.get_season(self.season["id"]), self.team["id"])
        self.assertEqual(reg.id, real["id"])

    def test_create_game_rejects_valid_plus_active_stray_pair(self):
        # The LIVE end-to-end path: create_game must never schedule a
        # regular Game against a Team whose participation is ambiguous,
        # even though the row it WOULD use is individually unambiguous at
        # its own exact key.
        self.api.register_team_for_season(
            self.season["id"], self.team["id"], actor_id=ADMIN,
            league_id=self.league_a["id"])
        self._inject(self.league_b, active=True)
        away_club = self.api.create_club("C2", actor_id=ADMIN)
        away = self.api.create_team(away_club["id"], None, "Away", actor_id=ADMIN,
                                    league_id=self.league_a["id"])
        self.api.register_team_for_season(self.season["id"], away["id"],
                                          actor_id=ADMIN, league_id=self.league_a["id"])
        venue = self.api.create_venue("V", league_id=self.program["id"], actor_id=ADMIN)
        self.api.grant_season_venue_access(self.season["id"], venue["id"], actor_id=ADMIN)
        rink = self.api.create_rink(venue["id"], "R", actor_id=ADMIN)
        slot = self.api.create_ice_slot(
            rink["id"], "2026-09-01T18:00:00+00:00", "2026-09-01T19:00:00+00:00",
            actor_id=ADMIN)
        game = self.api.create_game(
            self.season["id"], None, self.team["id"], away["id"], slot["id"],
            actor_id=ADMIN, league_id=self.league_a["id"])
        self.assertIn("error", game, game)


class WritePathRejectsSeasonWideConflictTest(_Base):
    """#331 review round 20: register_team_for_season additionally rejects
    when the Team already has an active registration ANYWHERE ELSE in the
    Season -- not just at the exact target key round 19 already covered."""

    def setUp(self):
        super().setUp()
        self.program = self.api.create_program("P", actor_id=ADMIN)
        self.season = self.api.create_season(self.program["id"], "S", actor_id=ADMIN)
        self.league_a = self.api.create_league(self.season["id"], "A", actor_id=ADMIN)
        self.league_b = self.api.create_league(self.season["id"], "B", actor_id=ADMIN)
        self.club = self.api.create_club("C", actor_id=ADMIN)

    def test_rejects_when_an_active_stray_exists_at_a_different_league(self):
        team = self.api.create_team(self.club["id"], None, "T", actor_id=ADMIN,
                                    league_id=self.league_a["id"])
        ls_b = self.store.league_season_for(self.league_b["id"], self.season["id"])
        stray = SeasonTeamRegistration(
            id=self.store.next_id("streg"), league_season_id=ls_b.id,
            team_id=team["id"], division_id=None, active=True)
        self.store.add_season_team_registration(stray)
        audits_before = self._audit_count()
        res = self.api.register_team_for_season(
            self.season["id"], team["id"], actor_id=ADMIN, league_id=self.league_a["id"])
        self.assertEqual(res["error"]["details"]["reason"],
                         "team_registration_conflict", res)
        self.assertEqual(res["error"]["details"]["affected_registration_ids"],
                         [stray.id])
        self.assertEqual(self._audit_count(), audits_before)
        self.assertEqual(
            len(self.store.registrations_for_season(self.season["id"])), 1)
        untouched = self.store.get_season_team_registration(stray.id)
        self.assertTrue(untouched.active)

    def test_inactive_stray_never_blocks_registration(self):
        team = self.api.create_team(self.club["id"], None, "T", actor_id=ADMIN,
                                    league_id=self.league_a["id"])
        ls_b = self.store.league_season_for(self.league_b["id"], self.season["id"])
        self.store.add_season_team_registration(SeasonTeamRegistration(
            id=self.store.next_id("streg"), league_season_id=ls_b.id,
            team_id=team["id"], division_id=None, active=False))
        res = self.api.register_team_for_season(
            self.season["id"], team["id"], actor_id=ADMIN, league_id=self.league_a["id"])
        self.assertNotIn("error", res, res)

    def test_two_active_strays_reports_both_conflicting_ids(self):
        team = self.api.create_team(self.club["id"], None, "T", actor_id=ADMIN,
                                    league_id=self.league_a["id"])
        third_league = self.api.create_league(self.season["id"], "C", actor_id=ADMIN)
        ls_b = self.store.league_season_for(self.league_b["id"], self.season["id"])
        ls_c = self.store.league_season_for(third_league["id"], self.season["id"])
        stray_b = SeasonTeamRegistration(
            id=self.store.next_id("streg"), league_season_id=ls_b.id,
            team_id=team["id"], division_id=None, active=True)
        stray_c = SeasonTeamRegistration(
            id=self.store.next_id("streg"), league_season_id=ls_c.id,
            team_id=team["id"], division_id=None, active=True)
        self.store.add_season_team_registration(stray_b)
        self.store.add_season_team_registration(stray_c)
        res = self.api.register_team_for_season(
            self.season["id"], team["id"], actor_id=ADMIN, league_id=self.league_a["id"])
        self.assertEqual(res["error"]["details"]["reason"],
                         "team_registration_conflict", res)
        self.assertEqual(set(res["error"]["details"]["affected_registration_ids"]),
                         {stray_b.id, stray_c.id})


class DraftCommitBatchParticipationTest(_Base):
    """#331 review round 20: _require_batch_team_participation (the
    draft-commit gate), tested directly against the function under test.
    The full commit_draft_schedule API additionally binds a
    draft_fingerprint that would independently reject ANY store mutation
    between preview and commit as a generic "preview_stale" -- a genuine,
    separate defense this same function sits behind in production, but one
    that would mask whether THIS specific check is the one doing the
    rejecting. A duplicate row for a team already in the roster doesn't
    change the SET of registered team ids draft generation computes (Python
    sets de-duplicate), so this corruption is invisible to that generic net
    -- confirming _require_batch_team_participation's own check is the ONLY
    line of defense against it, exactly matching the reviewer's
    characterization of this as an actual accept-when-should-reject bug,
    not merely a redundant belt-and-suspenders check."""

    def setUp(self):
        super().setUp()
        self.program = self.api.create_program("P", actor_id=ADMIN)
        self.season = self.api.create_season(self.program["id"], "S", actor_id=ADMIN)
        self.league = self.api.create_league(self.season["id"], "L", actor_id=ADMIN)
        self.ls = self.store.league_season_for(self.league["id"], self.season["id"])
        self.club = self.api.create_club("C", actor_id=ADMIN)
        self.home = self.api.create_team(self.club["id"], None, "Home", actor_id=ADMIN,
                                         league_id=self.league["id"])
        self.away = self.api.create_team(self.club["id"], None, "Away", actor_id=ADMIN,
                                         league_id=self.league["id"])
        self.home_reg = self.api.register_team_for_season(
            self.season["id"], self.home["id"], actor_id=ADMIN,
            league_id=self.league["id"])
        self.away_reg = self.api.register_team_for_season(
            self.season["id"], self.away["id"], actor_id=ADMIN,
            league_id=self.league["id"])
        self.row = {"home_team_id": self.home["id"], "away_team_id": self.away["id"],
                    "division_id": None}

    def test_baseline_accepts_a_clean_pairing(self):
        # Sanity check: without corruption, the gate accepts (raises nothing).
        self.api.setup._require_batch_team_participation(
            self.season["id"], self.ls.id, [self.row])

    def test_rejects_exact_key_duplicate_instead_of_silently_picking_a_winner(self):
        # The previous raw {team_id: r for r in ...} dict would silently
        # keep whichever of these two colliding rows won the dict-key
        # collision and accept the batch on it.
        dup = SeasonTeamRegistration(
            id=self.store.next_id("streg"), league_season_id=self.ls.id,
            team_id=self.home["id"], division_id=None, active=True)
        self.store.add_season_team_registration(dup)
        with self.assertRaises(DivisionMismatchError) as ctx:
            self.api.setup._require_batch_team_participation(
                self.season["id"], self.ls.id, [self.row])
        self.assertEqual(ctx.exception.details["reason"], "team_registration_conflict")
        self.assertEqual(set(ctx.exception.details["affected_registration_ids"]),
                         {self.home_reg["id"], dup.id})

    def test_rejects_when_a_team_also_has_an_active_stray_elsewhere(self):
        other_league = self.api.create_league(self.season["id"], "L2", actor_id=ADMIN)
        other_ls = self.store.league_season_for(other_league["id"], self.season["id"])
        stray = SeasonTeamRegistration(
            id=self.store.next_id("streg"), league_season_id=other_ls.id,
            team_id=self.home["id"], division_id=None, active=True)
        self.store.add_season_team_registration(stray)
        with self.assertRaises(DivisionMismatchError) as ctx:
            self.api.setup._require_batch_team_participation(
                self.season["id"], self.ls.id, [self.row])
        self.assertEqual(ctx.exception.details["reason"], "team_registration_conflict")
        self.assertEqual(set(ctx.exception.details["affected_registration_ids"]),
                         {self.home_reg["id"], stray.id})


class RequireTeamRegisteredDeterministicReasonTest(_Base):
    """#331 review round 20: _require_team_registered's error REASON no
    longer depends on which of several colliding/ambiguous rows a bare
    first-match pick happens to prefer -- a structured
    team_registration_conflict is reported BEFORE that guesswork,
    deterministically, regardless of insertion order or which shape of
    ambiguity (exact-key or season-wide) is present."""

    def setUp(self):
        super().setUp()
        self.program = self.api.create_program("P", actor_id=ADMIN)
        self.season = self.api.create_season(self.program["id"], "S", actor_id=ADMIN)
        self.league = self.api.create_league(self.season["id"], "L", actor_id=ADMIN)
        self.ls = self.store.league_season_for(self.league["id"], self.season["id"])
        self.club = self.api.create_club("C", actor_id=ADMIN)

    def _team(self, name):
        return self.api.create_team(self.club["id"], None, name, actor_id=ADMIN,
                                    league_id=self.league["id"])

    def test_exact_key_conflict_reason_is_deterministic_active_first(self):
        team = self._team("T1")
        rows = self._plant_duplicate(self.ls.id, team["id"], active=(True, False))
        with self.assertRaises(DivisionMismatchError) as ctx:
            self.api.setup._require_team_registered(
                self.season["id"], team["id"], None, require_division=False)
        self.assertEqual(ctx.exception.details["reason"], "team_registration_conflict")
        self.assertEqual(set(ctx.exception.details["affected_registration_ids"]),
                         {r.id for r in rows})

    def test_exact_key_conflict_reason_is_deterministic_inactive_first(self):
        team = self._team("T2")
        rows = self._plant_duplicate(self.ls.id, team["id"], active=(False, True))
        with self.assertRaises(DivisionMismatchError) as ctx:
            self.api.setup._require_team_registered(
                self.season["id"], team["id"], None, require_division=False)
        self.assertEqual(ctx.exception.details["reason"], "team_registration_conflict")
        self.assertEqual(set(ctx.exception.details["affected_registration_ids"]),
                         {r.id for r in rows})

    def test_season_wide_conflict_reason_is_reported_before_the_raw_pick(self):
        team = self._team("T3")
        other_league = self.api.create_league(self.season["id"], "L2", actor_id=ADMIN)
        other_ls = self.store.league_season_for(other_league["id"], self.season["id"])
        reg_a = SeasonTeamRegistration(
            id=self.store.next_id("streg"), league_season_id=self.ls.id,
            team_id=team["id"], division_id=None, active=True)
        reg_b = SeasonTeamRegistration(
            id=self.store.next_id("streg"), league_season_id=other_ls.id,
            team_id=team["id"], division_id=None, active=True)
        self.store.add_season_team_registration(reg_a)
        self.store.add_season_team_registration(reg_b)
        with self.assertRaises(DivisionMismatchError) as ctx:
            self.api.setup._require_team_registered(
                self.season["id"], team["id"], None, require_division=False)
        self.assertEqual(ctx.exception.details["reason"], "team_registration_conflict")
        self.assertEqual(set(ctx.exception.details["affected_registration_ids"]),
                         {reg_a.id, reg_b.id})


if __name__ == "__main__":
    unittest.main()
