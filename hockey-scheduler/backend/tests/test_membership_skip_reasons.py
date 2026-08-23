"""PR #427 Part A — a DISTINCT, STABLE reason for every way a candidate is
skipped.

WHY THIS FILE EXISTS. The owner's #427 product ruling lets the two BATCH
seating entry points (``copy_previous_roster``, ``auto_build_roster``) skip an
ineligible player and seat the rest, but only on one condition: "The result
must deterministically identify both the players seated and the players
skipped, with a stable reason for each skip." At head b8a6415 that was
impossible to satisfy honestly. ``_resolve_context_with_reason``'s candidate
loop ``continue``d past every membership row that was "simply not about this
game" WITHOUT recording anything, so FOUR of the five candidate shapes the
ruling names by hand collapsed into one undifferentiated string:

    transferred            -> no_eligible_membership   (indistinguishable)
    membership inactive    -> no_eligible_membership   (indistinguishable)
    membership-less        -> no_eligible_membership   (correct, but collided)
    wrong-LeagueSeason     -> no_eligible_membership   (indistinguishable)
    missing-registration   -> team_not_registered      (already distinct)
    player deactivated     -> no reason at all (checked outside the spine)

An operator told "4 players skipped: no_eligible_membership" learns nothing
they can act on, and "never a silent partial success" would be satisfied only
in name.

WHAT CHANGED. The candidate filter became a CLASSIFIER *inside*
``_resolve_context_with_reason`` — never a second, parallel classifier, so the
eligibility GATE and the diagnostic reason can still never disagree — and the
new reason names live in ``services/membership_spine.py`` beside the spine's
own, because the skip vocabulary must have exactly one home. Deactivation is
NOT folded into the resolver (that would newly close reads that are open
today); it is the one extra leg ``RosterService.seating_block_reason`` layers
on top, in the same order ``select_roster`` applies the two gates.

THE GATE IS UNCHANGED, and section 3 asserts it: every shape that was refused
before is refused now, the ONE shape that resolved before (a deactivated
player still resolves a membership context) still resolves, and the reasons
are additive detail rather than new refusals.

TRI-STORE, PROVEN, NOT ASSUMED. ``_stores`` yields Memory, SQLite and — when
TEST_DATABASE_URL is set — real PostgreSQL; ``_assert_backend`` proves the
backend in hand and ``_assert_matrix_ran`` fails if any configured backend or
any shape did not actually execute. A SKIP IS NOT A PASS.
"""

import os
import unittest
from typing import NamedTuple

from helpers import BACKEND, FakeClock  # noqa: F401  (sets up sys.path)
from helpers import end_membership_directly, fresh_sql_store

from test_slot_overfill_regression import _OverfillFixture
from test_substitute_membership_cutover import ADMIN

from hockey_scheduler.domain import MembershipStatus
from hockey_scheduler.services import membership_spine as spine
from hockey_scheduler.services.roster_service import RosterService
from hockey_scheduler.store import InMemoryStore, SqlStore

_PG_SKIP = (
    "PostgreSQL not configured (TEST_DATABASE_URL) — the #427 skip-reason "
    "classifier was NOT exercised on PostgreSQL. A SKIP IS NOT A PASS: the "
    "membership/registration/Team reads behind every one of these reasons "
    "are real SQL there. Set TEST_DATABASE_URL (run_parallel.py --postgres "
    "does).")


class Shape(NamedTuple):
    """One way a candidate fails to seat, and the reason it must produce."""
    name: str
    reason: str            # what seating_block_reason() must answer
    spine_reason: str      # what membership_spine_break_reason() must answer
    resolves: bool         # does a membership context still resolve?
    note: str


SHAPES = (
    Shape("transferred", "membership_transferred", "membership_transferred",
          False,
          "the owner's first named shape: the HOME stint ended TRANSFERRED "
          "and a new ACTIVE stint opened on another team in the same "
          "LeagueSeason"),
    Shape("membership_inactive", "membership_inactive", "membership_inactive",
          False,
          "the owner's second named shape: the stint is parked, not ended"),
    Shape("membership_less", "no_eligible_membership", "no_eligible_membership",
          False,
          "the owner's third named shape, and the ONLY one that may still "
          "answer no_eligible_membership — the narrowed meaning is 'this "
          "player holds no membership rows at all'"),
    Shape("wrong_league_season", "membership_other_league_season",
          "membership_other_league_season", False,
          "the owner's fourth named shape: registered, but in a different "
          "competition"),
    Shape("missing_registration", "team_not_registered", "team_not_registered",
          False,
          "the owner's fifth named shape — already distinct before this "
          "commit, kept in the matrix so a regression in the spine legs is "
          "caught by the same table"),
    Shape("player_deactivated", "player_inactive", None, True,
          "#270's Player.is_active, which had NO reason code at all. The "
          "membership context still RESOLVES (unchanged behaviour); only "
          "the seating gate refuses, so the spine reason is None and the "
          "extra leg is seating_block_reason's"),
    Shape("other_team", "membership_other_team", "membership_other_team",
          False,
          "not in the owner's list, added because it is the shape the "
          "narrowed no_eligible_membership would otherwise still swallow: a "
          "live ACTIVE membership at this exact LeagueSeason, on a bench "
          "that is not playing in this game"),
)


class _ReasonHarness(_OverfillFixture):
    """Fixture + shape construction written ONCE, invoked by every backend."""

    def _stores(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            yield "postgres", fresh_sql_store(url)

    def _assert_backend(self, label, store):
        """PROVE the backend, never assume it — ``skipUnless`` on the env var
        would prove only that a URL was SET."""
        if label == "postgres":
            self.assertIsInstance(store, SqlStore, label)
            self.assertEqual(store.backend, "postgres", store.backend)
        elif label == "sqlite":
            self.assertIsInstance(store, SqlStore, label)
            self.assertEqual(store.backend, "sqlite", store.backend)
        else:
            self.assertIsInstance(store, InMemoryStore, label)

    def _close(self, label, store):
        if isinstance(store, SqlStore):
            if label == "postgres":
                store.reset_schema()
            store.close()

    def _assert_matrix_ran(self, ran, shapes=SHAPES):
        backends = {b for b, _s in ran}
        expected = {"memory", "sqlite"}
        if os.environ.get("TEST_DATABASE_URL"):
            expected.add("postgres")
        else:
            print("\n[SKIP-REASON MATRIX] " + _PG_SKIP)
        self.assertEqual(backends, expected, sorted(backends))
        for backend in expected:
            names = {s for b, s in ran if b == backend}
            self.assertEqual(names, {s.name for s in shapes},
                             (backend, sorted(names)))

    # -- the shapes ------------------------------------------------------
    def _other_competition(self, api, season, teams):
        """A SECOND Program-mate Season + League + registered Team, i.e. a
        genuinely different ``LeagueSeason`` — the only honest way to build
        the owner's "wrong-LeagueSeason" candidate. Reusing this game's own
        LeagueSeason with a different team would be the OTHER_TEAM shape,
        which is a different reason on purpose."""
        program_id = api.store.get_season(season["id"]).program_id
        season2 = api.create_season(program_id, "Spring 2027", actor_id=ADMIN)
        self.assertNotIn("error", season2, season2)
        league2 = api.create_league(season2["id"], "Other", actor_id=ADMIN)
        self.assertNotIn("error", league2, league2)
        club2 = api.create_club("Other Club", actor_id=ADMIN)
        team2 = api.create_team(club2["id"], None, "Other Team",
                                actor_id=ADMIN, league_id=league2["id"])
        self.assertNotIn("error", team2, team2)
        reg = api.register_team_for_season(season2["id"], team2["id"],
                                           actor_id=ADMIN,
                                           league_id=league2["id"])
        self.assertNotIn("error", reg, reg)
        return team2

    def _shape(self, api, fx, shape):
        """Build ONE candidate exhibiting ``shape`` and return its player id."""
        home, third, ls_id = fx["home"], fx["third"], fx["ls_id"]
        season, teams = fx["season"], fx["teams"]
        if shape.name == "transferred":
            p = self._pointer_only_player(api, third, "Tessa Transferred")
            self._membership(api, p["id"], ls_id, home)
            # The seasonal model's real transfer: the old stint becomes
            # TRANSFERRED history and a new ACTIVE stint opens elsewhere.
            self._transfer(api, p["id"], ls_id, ls_id, third)
            return p["id"]
        if shape.name == "membership_inactive":
            p = self._pointer_only_player(api, third, "Ivan Inactive")
            self._membership(api, p["id"], ls_id, home)
            res = api.set_season_roster_membership_status(
                self._stint_id(api, p["id"], ls_id), "inactive",
                actor_id=ADMIN)
            self.assertNotIn("error", res, res)
            return p["id"]
        if shape.name == "membership_less":
            # Pointer on HOME, seasonal record SILENT — the bulk-import shape.
            return self._pointer_only_player(api, home, "Nadia None")["id"]
        if shape.name == "wrong_league_season":
            team2 = self._other_competition(api, season, teams)
            # create_player's parity dual-write opens the ACTIVE stint on
            # team2's OWN LeagueSeason, which is not this game's.
            return self._player(api, team2["id"], "Wanda Wrongseason")["id"]
        if shape.name == "missing_registration":
            p = self._pointer_only_player(api, third, "Rhea Unregistered")
            self._membership(api, p["id"], ls_id, home)
            (reg,) = api.store.registrations_for_team_in_league_season(
                ls_id, home)
            reg.active = False
            api.store.save_season_team_registration(reg)
            return p["id"]
        if shape.name == "player_deactivated":
            p = self._pointer_only_player(api, third, "Dev Deactivated")
            self._membership(api, p["id"], ls_id, home)
            res = api.set_player_active(p["id"], False, actor_id=ADMIN)
            self.assertNotIn("error", res, res)
            return p["id"]
        if shape.name == "other_team":
            p = self._pointer_only_player(api, home, "Otto Otherbench")
            self._membership(api, p["id"], ls_id, third)
            return p["id"]
        raise AssertionError(f"unbuilt shape {shape.name}")

    def _cases(self, shapes=SHAPES):
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for shape in shapes:
                    store.clear_all_data()
                    api, season, league, teams, game, ls_id = self._build(
                        store, target_skaters=4, target_goalies=1)
                    fx = {"api": api, "season": season, "league": league,
                          "teams": teams, "game": game, "ls_id": ls_id,
                          "home": teams["home"]["id"],
                          "away": teams["away"]["id"],
                          "third": teams["third"]["id"]}
                    pid = self._shape(api, fx, shape)
                    yield label, shape, fx, pid
            finally:
                self._close(label, store)


# ======================================================================
# 1. every shape has its OWN stable reason
# ======================================================================
class EverySkipShapeHasItsOwnStableReason(_ReasonHarness, unittest.TestCase):
    """THE #427 requirement, asserted directly and per shape.

    RED at head b8a6415 on Memory/SQLite/PostgreSQL: ``transferred``,
    ``membership_inactive``, ``wrong_league_season`` and ``other_team`` all
    answered ``no_eligible_membership``, and ``player_deactivated`` answered
    nothing at all (``seating_block_reason`` did not exist)."""

    def test_each_shape_yields_its_own_reason(self):
        ran = []
        for label, shape, fx, pid in self._cases():
            with self.subTest(backend=label, shape=shape.name):
                r = fx["api"].roster
                game = fx["api"].store.get_game(fx["game"]["id"])
                player = fx["api"].store.get_player(pid)
                self.assertEqual(r.seating_block_reason(game, player),
                                 shape.reason, (label, shape.name))
                # The membership-only view agrees where it has an opinion,
                # and is silent (None) exactly for the one shape that is NOT
                # a membership question at all.
                self.assertEqual(r.membership_spine_break_reason(game, player),
                                 shape.spine_reason, (label, shape.name))
            ran.append((label, shape.name))
        self._assert_matrix_ran(ran)

    def test_the_reasons_are_pairwise_distinct(self):
        """The point of the exercise: no two shapes may share a reason. A
        classifier that collapses two shapes back together passes every
        per-shape assertion above (each one still gets *a* reason) and fails
        only here."""
        reasons = [s.reason for s in SHAPES]
        self.assertEqual(len(set(reasons)), len(reasons), sorted(reasons))

    def test_no_eligible_membership_now_means_only_no_rows_at_all(self):
        """The NARROWING, asserted from both directions: the membership-less
        candidate is the ONLY shape that still answers
        ``no_eligible_membership``, and it answers it because the player
        genuinely holds zero membership rows."""
        ran = []
        for label, shape, fx, pid in self._cases():
            with self.subTest(backend=label, shape=shape.name):
                r = fx["api"].roster
                game = fx["api"].store.get_game(fx["game"]["id"])
                player = fx["api"].store.get_player(pid)
                reason = r.seating_block_reason(game, player)
                rows = fx["api"].store.memberships_for_player(pid)
                if reason == spine.NO_ELIGIBLE_MEMBERSHIP:
                    self.assertEqual(shape.name, "membership_less",
                                     (label, shape.name))
                    self.assertEqual(list(rows), [], (label, shape.name))
                else:
                    self.assertTrue(rows or shape.name == "player_deactivated",
                                    (label, shape.name))
            ran.append((label, shape.name))
        self._assert_matrix_ran(ran)


# ======================================================================
# 2. the reason vocabulary is closed over the enum
# ======================================================================
class MembershipReasonsCoverEveryStatus(unittest.TestCase):
    """A pure, store-free partition check.

    Deriving a skip reason from ``MembershipStatus`` is only safe while every
    value is classified. This is the ``_PLAYER_CONFIRM_SOURCE_STATES``
    discipline applied to the reason table: a new enum value fails HERE and
    forces somebody to decide, instead of being silently reported under some
    other status's wording (or, worse, silently seated)."""

    def test_eligible_and_ineligible_partition_the_enum(self):
        eligible = set(RosterService._ELIGIBLE_MEMBERSHIP_STATUSES)
        ineligible = set(RosterService._INELIGIBLE_MEMBERSHIP_STATUSES)
        self.assertEqual(eligible & ineligible, set())
        self.assertEqual(eligible | ineligible, set(MembershipStatus))
        # ORDERED tuples, not sets: the report order is a determinism
        # guarantee, so a duplicate would silently shadow a later entry.
        self.assertEqual(len(RosterService._INELIGIBLE_MEMBERSHIP_STATUSES),
                         len(ineligible))

    def test_every_ineligible_status_has_exactly_one_reason(self):
        ineligible = set(RosterService._INELIGIBLE_MEMBERSHIP_STATUSES)
        self.assertEqual(set(spine.MEMBERSHIP_STATUS_REASONS), ineligible)
        names = list(spine.MEMBERSHIP_STATUS_REASONS.values())
        self.assertEqual(len(set(names)), len(names), sorted(names))
        for status in ineligible:
            self.assertEqual(spine.status_ineligible_reason(status),
                             spine.MEMBERSHIP_STATUS_REASONS[status])

    def test_an_eligible_status_has_no_reason_and_says_so_loudly(self):
        for status in RosterService._ELIGIBLE_MEMBERSHIP_STATUSES:
            with self.assertRaises(KeyError):
                spine.status_ineligible_reason(status)

    def test_every_skip_reason_string_is_unique(self):
        """One string, one meaning. Two constants sharing a value would make
        the UI's code->text map ambiguous in a way no per-shape test sees."""
        names = [spine.TEAM_MISSING, spine.LEAGUE_SEASON_MISSING,
                 spine.SEASON_MISSING, spine.LEAGUE_MISMATCH,
                 spine.PROGRAM_MISMATCH, spine.NOT_REGISTERED,
                 spine.REGISTRATION_CONFLICT, spine.PLAYER_MISSING,
                 spine.DENORMALIZED_SEASON_MISMATCH,
                 spine.MEMBERSHIP_OTHER_TEAM,
                 spine.MEMBERSHIP_OTHER_LEAGUE_SEASON,
                 spine.PLAYER_INACTIVE, spine.NO_ELIGIBLE_MEMBERSHIP,
                 *spine.MEMBERSHIP_STATUS_REASONS.values()]
        self.assertEqual(len(set(names)), len(names), sorted(names))


# ======================================================================
# 3. the GATE did not move
# ======================================================================
class TheReasonsAreAdditiveDetailNotNewRefusals(_ReasonHarness,
                                                unittest.TestCase):
    """A4: the classifier must refuse EXACTLY what it refused before.

    The one shape that must still RESOLVE is the deactivated player —
    ``resolve_membership_context`` has never consulted ``Player.is_active``,
    and ``compute_roster_status``/``_slot_summaries``/the private reads all
    hang off that resolution. Folding deactivation into the resolver would
    have made a seated-then-deactivated player vanish from the governed
    count, which is precisely the defect
    test_roster_attribution_durability.py exists to prevent."""

    def test_the_resolver_still_closes_and_opens_exactly_as_before(self):
        ran = []
        for label, shape, fx, pid in self._cases():
            with self.subTest(backend=label, shape=shape.name):
                r = fx["api"].roster
                game = fx["api"].store.get_game(fx["game"]["id"])
                player = fx["api"].store.get_player(pid)
                ctx = r.resolve_membership_context(game, player)
                if shape.resolves:
                    self.assertIsNotNone(ctx, (label, shape.name))
                    self.assertEqual(ctx.team_id, fx["home"],
                                     (label, shape.name))
                    self.assertIn(
                        pid, r.resolve_membership_contexts_for_game(game),
                        (label, shape.name))
                else:
                    self.assertIsNone(ctx, (label, shape.name))
                    self.assertIsNone(r.team_for_game(game, player),
                                      (label, shape.name))
                    self.assertNotIn(
                        pid, r.resolve_membership_contexts_for_game(game),
                        (label, shape.name))
            ran.append((label, shape.name))
        self._assert_matrix_ran(ran)

    def test_an_ordinary_eligible_player_has_no_reason_at_all(self):
        """The control. A reason classifier that answers a reason for a
        perfectly eligible player would pass every assertion above."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, season, league, teams, game, ls_id = self._build(
                    store, target_skaters=4, target_goalies=1)
                p = self._player(api, teams["home"]["id"], "Olive Ordinary")
                g = api.store.get_game(game["id"])
                player = api.store.get_player(p["id"])
                self.assertIsNone(
                    api.roster.seating_block_reason(g, player), label)
                self.assertIsNone(
                    api.roster.membership_spine_break_reason(g, player), label)
                self.assertIsNotNone(
                    api.roster.resolve_membership_context(g, player), label)
                ran.append(label)
            finally:
                self._close(label, store)
        expected = {"memory", "sqlite"}
        if os.environ.get("TEST_DATABASE_URL"):
            expected.add("postgres")
        self.assertEqual(set(ran), expected, sorted(ran))


if __name__ == "__main__":
    unittest.main()
