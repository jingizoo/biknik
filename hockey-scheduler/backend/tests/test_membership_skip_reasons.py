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

import dataclasses
import itertools
import json
import os
import unittest
from typing import NamedTuple

from helpers import BACKEND, FakeClock  # noqa: F401  (sets up sys.path)
from helpers import end_membership_directly, fresh_sql_store

from test_batch_seating_partial import _BatchHarness
from test_slot_overfill_regression import _OverfillFixture
from test_substitute_membership_cutover import ADMIN

from hockey_scheduler.domain import MembershipStatus, Position
from hockey_scheduler.domain.errors import IntegrityConflictError
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


# ======================================================================
# 4. REASON PRECEDENCE — which reason wins when several apply
# ======================================================================
class ReasonPrecedenceIsPinned(_ReasonHarness, unittest.TestCase):
    """The #427 acceptance bar's first item: the classifier's reason
    precedence must be DOCUMENTED and pinned by a test.

    Sections 1-3 build candidates that match exactly ONE reason each, so
    they say nothing at all about the far more common real case — a player
    who is transferred AND deactivated, or parked on a side whose
    registration has ALSO lapsed. Without a written, tested order, two
    equally true reasons could be reported for the same shape depending on
    which store handed back which row first, and the "stable reason for each
    skip" the ruling asks for would be stable only by luck.

    ``services/membership_spine.SKIP_REASON_PRECEDENCE`` is the written
    order and the block above it is the rationale. THIS class asserts the
    CODE agrees with it: each case below is a candidate matching two or more
    reasons, and the one that must be reported is the earlier entry.

    WHAT THIS SECTION DOES AND DOES NOT COVER. The cases here govern which
    RUNG answers. Section 5 governs several rows sharing ONE ``(status,
    team)`` key — the ladder again, via ``_keep_best_reason``, plus an id
    tie-break. Section 6 governs rows spread ACROSS keys, which is the ladder
    a third time, via ``_pick_reason_membership``; until that existed the
    across-keys survivor was chosen by ``_pick_membership``'s
    status-then-home-before-away walk, which is the SEATING rule and answers
    a question the reason path never asked. Section 6 also pins that the
    seating rule itself is unmoved, and section 7 covers the one remaining
    collapse (``_keep_lowest_id``) that the ladder does NOT govern."""

    def _cases_for(self, make):
        """Run ``make(api, fx) -> (player, expected_reason)`` on every
        configured backend, asserting the reason the classifier reports."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, season, league, teams, game, ls_id = self._build(
                    store, target_skaters=4, target_goalies=1)
                fx = {"api": api, "season": season, "league": league,
                      "teams": teams, "game": game, "ls_id": ls_id,
                      "home": teams["home"]["id"],
                      "away": teams["away"]["id"],
                      "third": teams["third"]["id"]}
                pid, expected = make(api, fx)
                with self.subTest(backend=label):
                    g = api.store.get_game(fx["game"]["id"])
                    player = api.store.get_player(pid)
                    reason = api.roster.seating_block_reason(g, player)
                    self.assertEqual(reason, expected, (label, reason))
                    # …and it really is the EARLIER of the applicable rungs.
                    spine.reason_rank(reason)
                ran.append(label)
            finally:
                self._close(label, store)
        expected_backends = {"memory", "sqlite"}
        if os.environ.get("TEST_DATABASE_URL"):
            expected_backends.add("postgres")
        self.assertEqual(set(ran), expected_backends, sorted(ran))

    def test_a_membership_reason_outranks_deactivation(self):
        """``player_inactive`` is LAST on purpose, and this is why: the
        order is the GATE's order. ``select_roster`` tests the membership
        context FIRST and ``Player.is_active`` SECOND, so a candidate
        failing both must be reported under the context reason — the reason
        has to name the gate that would actually refuse."""
        def make(api, fx):
            p = self._pointer_only_player(api, fx["third"], "Both Ways")
            self._membership(api, p["id"], fx["ls_id"], fx["home"])
            self._transfer(api, p["id"], fx["ls_id"], fx["ls_id"],
                           fx["third"])
            res = api.set_player_active(p["id"], False, actor_id=ADMIN)
            self.assertNotIn("error", res, res)
            self.assertFalse(api.store.get_player(p["id"]).is_active)
            return p["id"], "membership_transferred"
        self._cases_for(make)

    def test_a_parked_membership_outranks_deactivation_too(self):
        """The same rung comparison with a different membership reason, so
        the case above cannot be passing because of something peculiar to
        the terminal statuses."""
        def make(api, fx):
            p = self._pointer_only_player(api, fx["third"], "Parked Gone")
            self._membership(api, p["id"], fx["ls_id"], fx["home"])
            res = api.set_season_roster_membership_status(
                self._stint_id(api, p["id"], fx["ls_id"]), "inactive",
                actor_id=ADMIN)
            self.assertNotIn("error", res, res)
            res = api.set_player_active(p["id"], False, actor_id=ADMIN)
            self.assertNotIn("error", res, res)
            return p["id"], "membership_inactive"
        self._cases_for(make)

    def test_a_broken_spine_outranks_a_parked_row_elsewhere(self):
        """The row that came CLOSEST to seating the player names the reason.
        Here an ACTIVE, right-keyed HOME membership has a broken spine (the
        Team's registration lapsed) while a RELEASED stint sits in history
        on the AWAY bench. The spine break is reported, because that is the
        row that would have seated them.

        The old stint has to be TERMINAL rather than merely parked: only one
        OPEN membership per (player, LeagueSeason) may exist at a time, so
        an applicant row and an active row cannot coexist at this key. That
        constraint is the reason the shape is built as
        "played for AWAY, released, signed for HOME"."""
        def make(api, fx):
            p = self._pointer_only_player(api, fx["third"], "Spine Vs Park")
            self._membership(api, p["id"], fx["ls_id"], fx["away"])
            end_membership_directly(
                api.store, self._stint_id(api, p["id"], fx["ls_id"]),
                "released")
            self._membership(api, p["id"], fx["ls_id"], fx["home"])
            (reg,) = api.store.registrations_for_team_in_league_season(
                fx["ls_id"], fx["home"])
            reg.active = False
            api.store.save_season_team_registration(reg)
            statuses = sorted(m.status.value for m in
                              api.store.memberships_for_player(p["id"]))
            self.assertEqual(statuses, ["active", "released"], statuses)
            return p["id"], spine.NOT_REGISTERED
        self._cases_for(make)

    def test_a_parked_row_here_outranks_a_live_row_on_another_bench(self):
        """A TRANSFERRED stint on a side of THIS game outranks a perfectly
        live ACTIVE stint on a team that is not playing — the parked row is
        about this game, the other one is not."""
        def make(api, fx):
            p = self._pointer_only_player(api, fx["third"], "Left For Third")
            self._membership(api, p["id"], fx["ls_id"], fx["home"])
            self._transfer(api, p["id"], fx["ls_id"], fx["ls_id"],
                           fx["third"])
            live = [m for m in api.store.memberships_for_player(p["id"])
                    if m.team_id == fx["third"]]
            self.assertEqual(len(live), 1, live)
            self.assertEqual(live[0].status, MembershipStatus.ACTIVE)
            return p["id"], "membership_transferred"
        self._cases_for(make)

    def test_another_bench_outranks_another_competition(self):
        """Rows at THIS LeagueSeason, even on a bench that is not playing,
        say more about this game than rows in a different competition."""
        def make(api, fx):
            team2 = self._other_competition(api, fx["season"], fx["teams"])
            p = self._pointer_only_player(api, fx["third"], "Two Places")
            # …a live row on a bench that is not in this game…
            self._membership(api, p["id"], fx["ls_id"], fx["third"])
            # …and a row in an entirely different competition.
            other_ls = [ls.id for ls in api.store.all_league_seasons()
                        if ls.id != fx["ls_id"]]
            self.assertTrue(other_ls, other_ls)
            self._membership(api, p["id"], other_ls[0], team2["id"])
            return p["id"], spine.MEMBERSHIP_OTHER_TEAM
        self._cases_for(make)

    def test_a_dangling_league_season_outranks_every_membership_reason(self):
        """A fact about the GAME beats every fact about the candidate: when
        the game's own LeagueSeason pointer dangles, nothing per-candidate
        can be more informative, so the same transferred-and-deactivated
        player is now reported under the game's reason instead."""
        def make(api, fx):
            p = self._pointer_only_player(api, fx["third"], "Both Ways")
            self._membership(api, p["id"], fx["ls_id"], fx["home"])
            self._transfer(api, p["id"], fx["ls_id"], fx["ls_id"],
                           fx["third"])
            res = api.set_player_active(p["id"], False, actor_id=ADMIN)
            self.assertNotIn("error", res, res)
            g = api.store.get_game(fx["game"]["id"])
            g.league_season_id = "ls_does_not_exist"
            api.store.save_game(g)
            return p["id"], spine.LEAGUE_SEASON_MISSING
        self._cases_for(make)

    def test_a_missing_player_row_outranks_the_rest(self):
        """The identity leg. The caller hands in a Player OBJECT it read a
        moment ago; the ROW is the authority. A candidate whose row has been
        deleted is reported as missing even though the object in hand is
        also deactivated and also transferred."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, season, league, teams, game, ls_id = self._build(
                    store, target_skaters=4, target_goalies=1)
                with self.subTest(backend=label):
                    # Pointer on HOME and no membership at all, so the
                    # OTHER applicable rungs are ``no_eligible_membership``
                    # and (via the stale object) ``player_inactive``. The
                    # Player row itself is then deleted — which is only
                    # possible for a player carrying no membership rows,
                    # since those are FK-constrained to it.
                    p = self._pointer_only_player(api, teams["home"]["id"],
                                                  "Deleted Anyway")
                    stale = api.store.get_player(p["id"])
                    stale.is_active = False
                    api.store.delete_player(p["id"])
                    self.assertIsNone(api.store.get_player(p["id"]))
                    g = api.store.get_game(game["id"])
                    self.assertEqual(
                        api.roster.seating_block_reason(g, stale),
                        spine.PLAYER_MISSING, label)
                ran.append(label)
            finally:
                self._close(label, store)
        expected = {"memory", "sqlite"}
        if os.environ.get("TEST_DATABASE_URL"):
            expected.add("postgres")
        self.assertEqual(set(ran), expected, sorted(ran))


class ThePrecedenceLadderIsClosedOverEveryProducibleReason(unittest.TestCase):
    """A pure, store-free closure check on the ladder itself.

    A ladder that omits a reason is worse than no ladder: ``reason_rank``
    raises for it, so the omission surfaces only when some unlucky operator
    hits that shape. This pins the ladder against the reason vocabulary the
    same way ``MembershipReasonsCoverEveryStatus`` pins the status table
    against the enum."""

    def _all_reasons(self):
        return {spine.TEAM_MISSING, spine.LEAGUE_SEASON_MISSING,
                spine.SEASON_MISSING, spine.LEAGUE_MISMATCH,
                spine.PROGRAM_MISMATCH, spine.NOT_REGISTERED,
                spine.REGISTRATION_CONFLICT, spine.PLAYER_MISSING,
                spine.DENORMALIZED_SEASON_MISMATCH,
                spine.MEMBERSHIP_OTHER_TEAM,
                spine.MEMBERSHIP_OTHER_LEAGUE_SEASON,
                spine.PLAYER_INACTIVE, spine.NO_ELIGIBLE_MEMBERSHIP,
                spine.PRIOR_SEAT_UNATTRIBUTED,
                *spine.MEMBERSHIP_STATUS_REASONS.values()}

    def test_the_ladder_holds_every_reason_exactly_once(self):
        ladder = list(spine.SKIP_REASON_PRECEDENCE)
        self.assertEqual(len(set(ladder)), len(ladder), sorted(ladder))
        self.assertEqual(set(ladder), self._all_reasons(),
                         sorted(set(ladder) ^ self._all_reasons()))

    def test_rank_is_a_total_order_starting_at_zero(self):
        ranks = [spine.reason_rank(r) for r in spine.SKIP_REASON_PRECEDENCE]
        self.assertEqual(ranks, list(range(len(ranks))), ranks)

    def test_an_unlisted_reason_fails_loudly(self):
        """The same fail-loud discipline ``status_ineligible_reason`` uses:
        a reason nobody has placed must raise rather than sort last by
        default."""
        with self.assertRaises(KeyError):
            spine.reason_rank("some_reason_nobody_classified")
        # ``roster_target_met`` is deliberately NOT an eligibility reason,
        # so it is deliberately NOT in the ladder.
        with self.assertRaises(KeyError):
            spine.reason_rank(RosterService.TARGET_MET)

    def test_the_discovery_stage_reason_outranks_everything(self):
        """``prior_seat_unattributed`` is rank 0 by construction: a
        candidate whose provenance cannot be proven was never established as
        a candidate for this side, so today's eligibility is not consulted
        at all. Asserted here as a property of the ladder, and exercised
        end-to-end in test_batch_seating_partial.py."""
        self.assertEqual(spine.reason_rank(spine.PRIOR_SEAT_UNATTRIBUTED), 0)


# ======================================================================
# 5. MULTIPLE HISTORICAL MEMBERSHIP ROWS — ONE reason, on every backend
# ======================================================================
# The owner's second added requirement (2026-08-23): "Part A's exact
# serialized reason strings and reason precedence must be deterministic when
# a player has multiple historical membership rows; Memory, SQLite and
# PostgreSQL must choose the same reason."
#
# Sections 1-4 build candidates holding ONE row apiece, so they say nothing
# about a player with a history — and a history is ordinary: a stint that
# was released, a later one that transferred, another opened and ended on
# the same bench. Every one of those rows reaches
# ``_resolve_context_with_reason``'s ``{status: {team_id: membership}}``
# dicts, which hold ONE row per key, so all but one of a repeated key's rows
# are DISCARDED. Which one survives used to be decided by the order the
# store handed them back — insertion order on ``InMemoryStore``, TEXT id
# order on ``SqlStore`` (so ``srm_10`` precedes ``srm_2`` there and follows
# it in memory).
#
# ``RosterService._keep_best_reason`` / ``_keep_lowest_id`` now decide it by
# an explicit key instead. This section pins all three halves of that:
# the ENGINE BOUND that says which multiplicities are constructible at all,
# the INVARIANCE of the reported reason under every row order, and the
# cross-backend EQUALITY of the whole reported outcome.


def _second_competition_team(test, api, season):
    """A genuinely different ``LeagueSeason`` — a Program-mate Season with
    its own League and its own registered Team.

    A standalone function rather than a harness method because both
    harnesses in this section need it and they do not share a base below
    ``_OverfillFixture``. Equivalent to ``_ReasonHarness._other_competition``
    (kept separate so a change to that matrix's fixture cannot silently
    reshape these combinations)."""
    program_id = api.store.get_season(season["id"]).program_id
    season2 = api.create_season(program_id, "Spring 2027", actor_id=ADMIN)
    test.assertNotIn("error", season2, season2)
    league2 = api.create_league(season2["id"], "Other", actor_id=ADMIN)
    test.assertNotIn("error", league2, league2)
    club2 = api.create_club("Other Club", actor_id=ADMIN)
    team2 = api.create_team(club2["id"], None, "Other Team", actor_id=ADMIN,
                            league_id=league2["id"])
    test.assertNotIn("error", team2, team2)
    reg = api.register_team_for_season(season2["id"], team2["id"],
                                       actor_id=ADMIN,
                                       league_id=league2["id"])
    test.assertNotIn("error", reg, reg)
    (ls2,) = [ls.id for ls in api.store.all_league_seasons()
              if ls.season_id == season2["id"]]
    test.assertNotEqual(ls2, None)
    return team2["id"], ls2


class MultiRowCombo(NamedTuple):
    """One player's HISTORY, and the single reason it must produce.

    ``stints`` is ``[(where, team_key, status)]`` applied in order, where
    ``where`` is ``"here"`` (this game's LeagueSeason) or ``"other"`` (a
    second competition) and a terminal ``status`` means the stint is opened
    and then ENDED, which is the only way a repeated key is constructible on
    a real database (migration 059's ``ux_srm_open_player_league_season``
    permits at most one NON-terminal row per player and LeagueSeason —
    ``TwoOpenStintsAtOneLeagueSeasonAreEngineRefused`` proves it)."""
    name: str
    stints: tuple
    reason: str
    note: str


_LIVE = "active"

MULTI_ROW_COMBOS = (
    MultiRowCombo(
        "several_terminal_stints_on_one_team",
        (("here", "home", "released"),
         ("here", "home", "transferred"),
         ("here", "home", "released"),
         ("here", "home", "transferred")),
        "membership_transferred",
        "FOUR rows collapsing onto TWO (status, team) keys — two of them "
        "onto the SAME key, which is the collapse itself. TRANSFERRED "
        "precedes RELEASED in _INELIGIBLE_MEMBERSHIP_STATUSES, so the rung "
        "is decided by status order and never by which row was scanned "
        "first"),
    MultiRowCombo(
        "the_same_status_on_both_sides",
        (("here", "home", "released"),
         ("here", "away", "released"),
         ("here", "home", "released"),
         ("here", "away", "released")),
        "membership_released",
        "one status, both benches, twice each: the status walk cannot "
        "separate them, so home-before-away does — and the reported string "
        "is the same either way, which is exactly the property that must "
        "be PROVEN rather than assumed"),
    MultiRowCombo(
        "rows_split_across_two_league_seasons",
        (("other", "other", "released"),
         ("here", "home", "released"),
         ("other", "other", "transferred"),
         ("here", "home", "transferred"),
         ("other", "other", _LIVE)),
        "membership_transferred",
        "TWO RUNGS applicable at once: parked rows at THIS LeagueSeason "
        "(rungs 11-15) and a LIVE row in ANOTHER competition (rung 17). "
        "The rows here must win, and the live row elsewhere must not "
        "shadow them"),
    MultiRowCombo(
        "another_bench_here_against_a_history_elsewhere",
        (("other", "other", "released"),
         ("other", "other", "transferred"),
         ("here", "third", _LIVE)),
        "membership_other_team",
        "the OTHER two-rung pair: nothing at all on either side of this "
        "game, a LIVE row on a bench at this LeagueSeason that is not "
        "playing (rung 16), and a history in another competition (rung "
        "17). Rung 16 must win"),
)


class _MultiRowFixture:
    """Builds one ``MultiRowCombo`` onto a player. Mixed into both harnesses
    below so the two of them cannot drift on what a combination means."""

    def _end(self, api, membership_id, status):
        end_membership_directly(api.store, membership_id, status)

    def _build_combo(self, api, fx, combo, name="Hank History",
                     pointer_team=None):
        """The combination's rows, on a fresh pointer-only player.

        ``pointer_team`` sets only the PERMANENT pointer, which this bound
        game's classifier never reads — it exists so the batch harness below
        can put the subject in ``auto_build_roster``'s pointer-half cohort
        without giving them a membership the combination did not ask for."""
        pid = self._pointer_only_player(
            api, pointer_team or fx["third"], name)["id"]
        other_team, other_ls = (None, None)
        if any(w == "other" for w, _t, _s in combo.stints):
            other_team, other_ls = _second_competition_team(
                self, api, fx["season"])
        teams = {"home": fx["home"], "away": fx["away"], "third": fx["third"],
                 "other": other_team}
        for where, team_key, status in combo.stints:
            ls = fx["ls_id"] if where == "here" else other_ls
            m = self._membership(api, pid, ls, teams[team_key])
            if status != _LIVE:
                self._end(api, m["id"], status)
        return pid

    def _assert_rows(self, api, pid, combo):
        """The fixture really did plant the whole history — a combination
        silently reduced to one row would make every assertion below
        vacuous."""
        rows = list(api.store.memberships_for_player(pid))
        self.assertEqual(len(rows), len(combo.stints),
                         [(m.id, m.team_id, m.status.value) for m in rows])
        return rows


class TwoOpenStintsAtOneLeagueSeasonAreEngineRefused(_ReasonHarness,
                                                     unittest.TestCase):
    """THE BOUND, stated first, because every combination above depends on
    it and because it is the honest answer to "why are these histories all
    terminal?".

    TWO of migration 059's partial unique indexes bound it, and the refusal
    is OVER-DETERMINED — widening either one alone leaves the insert still
    refused, and only widening both lets it through (measured, both ways):

    * ``ux_srm_open_player_league_season`` on ``(player_id,
      league_season_id) WHERE status NOT IN ('released','transferred')``;
    * ``ux_srm_active_player_season`` on ``(player_id, season_id) WHERE
      status = 'active'`` — the one PostgreSQL's message names here.

    So on SQLite and PostgreSQL a player holds at most ONE non-terminal row
    per LeagueSeason, which is why the ``valid``/``raw`` buckets — the only
    ones whose survivor can change a reason string or a resolved context —
    cannot collapse there at all, and why every constructible repeated key
    is TERMINAL.

    ``InMemoryStore`` enforces NO uniqueness and is a first-class backend of
    this suite, so the same input IS constructible there — which is why the
    tie-break is imposed in the SERVICE and not left to the index.
    ``TheReportedReasonIsInvariantUnderEveryRowOrder`` exercises exactly
    that shape."""

    def test_only_the_memory_store_accepts_a_second_open_row(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, season, league, teams, game, ls_id = self._build(
                    store, target_skaters=4, target_goalies=1)
                with self.subTest(backend=label):
                    pid = self._pointer_only_player(
                        api, teams["third"]["id"], "Twin Open")["id"]
                    m = self._membership(api, pid, ls_id,
                                         teams["home"]["id"])
                    row = api.store.get_season_roster_membership(m["id"])
                    clone = dataclasses.replace(
                        row, id=api.store.next_id("srm"))
                    if label == "memory":
                        api.store.add_season_roster_membership(clone)
                        self.assertEqual(
                            len(list(api.store.memberships_for_player(pid))),
                            2, label)
                    else:
                        with self.assertRaises(IntegrityConflictError):
                            api.store.add_season_roster_membership(clone)
                        self.assertEqual(
                            len(list(api.store.memberships_for_player(pid))),
                            1, label)
                ran.append(label)
            finally:
                self._close(label, store)
        expected = {"memory", "sqlite"}
        if os.environ.get("TEST_DATABASE_URL"):
            expected.add("postgres")
        else:
            print("\n[SKIP-REASON MATRIX] " + _PG_SKIP)
        self.assertEqual(set(ran), expected, sorted(ran))


class TheReportedReasonIsInvariantUnderEveryRowOrder(_MultiRowFixture,
                                                     _ReasonHarness,
                                                     unittest.TestCase):
    """The property itself, asserted the only way that settles it: run the
    classifier once per PERMUTATION of the player's membership rows and
    require ONE answer.

    Comparing two backends proves only that their two particular orders
    agree. Permuting proves the answer does not depend on order AT ALL, so
    no third backend, no index rebuild and no future ``ORDER BY`` change can
    move it. The permutation is injected at the STORE method the resolver
    actually reads (``memberships_for_player``), so nothing about the
    service is stubbed."""

    def _permuted_reasons(self, api, game, player):
        """Every reason the classifier gives across every row order."""
        store = api.store
        real = store.memberships_for_player
        rows = list(real(player.id))
        self.assertGreater(len(rows), 1, rows)
        # 5 rows -> 120 orders. Guard the fixture rather than the runtime:
        # a combination big enough to matter here is a combination too big
        # to be exhaustive about.
        self.assertLessEqual(len(rows), 5, len(rows))
        seen = {}
        try:
            for perm in itertools.permutations(rows):
                store.memberships_for_player = (
                    lambda pid, _p=perm: [m for m in _p if m.player_id == pid])
                reason = api.roster.seating_block_reason(game, player)
                seen.setdefault(reason, []).append([m.id for m in perm])
        finally:
            store.memberships_for_player = real
        return seen

    def test_every_combination_answers_the_same_in_every_row_order(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for combo in MULTI_ROW_COMBOS:
                    store.clear_all_data()
                    api, season, league, teams, game, ls_id = self._build(
                        store, target_skaters=4, target_goalies=1)
                    fx = {"season": season, "ls_id": ls_id,
                          "home": teams["home"]["id"],
                          "away": teams["away"]["id"],
                          "third": teams["third"]["id"]}
                    with self.subTest(backend=label, combo=combo.name):
                        pid = self._build_combo(api, fx, combo)
                        self._assert_rows(api, pid, combo)
                        seen = self._permuted_reasons(
                            api, api.store.get_game(game["id"]),
                            api.store.get_player(pid))
                        self.assertEqual(list(seen), [combo.reason],
                                         (label, combo.name, seen))
                        spine.reason_rank(combo.reason)
                    ran.append((label, combo.name))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, shapes=MULTI_ROW_COMBOS)

    def test_two_open_rows_failing_differently_still_answer_the_ladder(self):
        """THE REGRESSION for the defect this requirement uncovered, RED at
        head e2bde17 and MEMORY-ONLY by construction (the class above proves
        SQLite and PostgreSQL refuse the input).

        Two participation-granting rows at ONE (status, team) key, failing
        for DIFFERENT reasons: one carries a denormalized ``season_id``
        mismatch, the other is spine-correct but sits on a side whose
        registration has lapsed. Both are ACTIVE on HOME, so they collapse
        onto one key — and at head e2bde17 the survivor was whichever the
        store listed first, so the SERIALIZED REASON STRING flipped between
        ``team_not_registered`` and
        ``membership_denormalized_season_mismatch`` on nothing but insertion
        order. Captured, in that order:

            order ['srm_1', 'srm_2'] -> team_not_registered
            order ['srm_2', 'srm_1'] -> membership_denormalized_season_mismatch

        The answer must be the DENORMALIZED one in both orders, because
        ``SKIP_REASON_PRECEDENCE`` already ranks it (3) above
        ``team_not_registered`` (9) — the ladder governs WITHIN a rung, not
        only between rungs."""
        store = InMemoryStore()
        try:
            self._assert_backend("memory", store)
            api, season, league, teams, game, ls_id = self._build(
                store, target_skaters=4, target_goalies=1)
            home = teams["home"]["id"]
            pid = self._pointer_only_player(api, teams["third"]["id"],
                                            "Twin Failure")["id"]
            m = self._membership(api, pid, ls_id, home)
            row = api.store.get_season_roster_membership(m["id"])
            api.store.add_season_roster_membership(dataclasses.replace(
                row, id=api.store.next_id("srm"),
                season_id="season_that_does_not_exist"))
            # …and the spine-correct row fails too, so neither can seat and
            # the RAW bucket is what names the skip.
            (reg,) = api.store.registrations_for_team_in_league_season(
                ls_id, home)
            reg.active = False
            api.store.save_season_team_registration(reg)

            rows = list(api.store.memberships_for_player(pid))
            self.assertEqual(len(rows), 2, rows)
            self.assertEqual({m.status for m in rows},
                             {MembershipStatus.ACTIVE}, rows)
            seen = self._permuted_reasons(
                api, api.store.get_game(game["id"]),
                api.store.get_player(pid))
            self.assertEqual(list(seen),
                             [spine.DENORMALIZED_SEASON_MISMATCH], seen)
            # …and it really is the ladder that decided, not luck: the
            # OTHER applicable reason ranks strictly later.
            self.assertLess(
                spine.reason_rank(spine.DENORMALIZED_SEASON_MISMATCH),
                spine.reason_rank(spine.NOT_REGISTERED))
        finally:
            self._close("memory", store)


class MultipleHistoricalRowsReportOneOutcomeOnEveryBackend(_MultiRowFixture,
                                                           _BatchHarness,
                                                           unittest.TestCase):
    """The requirement's own wording, end to end: "Memory, SQLite and
    PostgreSQL must choose the same reason."

    Asserted by COMPARING THE THREE RESULTS TO EACH OTHER, never by
    asserting each independently — three backends each independently
    satisfying ``reason in SKIP_REASON_PRECEDENCE`` would pass while all
    three disagreed. The compared value is the whole reported outcome, not
    the bare string: the ordered ``seated`` list, the full ``skipped`` rows
    (player_id, name AND reason — everything that travels alongside the
    reason to an operator) and the durable AUDIT DETAIL, because the audit
    is the record that outlives the response and a divergence there would be
    just as real.

    The fixture is built by an identical call sequence on every backend, so
    the id allocation is identical too and the results must be EQUAL element
    for element — while the ORDER the store hands the rows back in is not
    equal, which is asserted rather than hoped for."""

    def _combo_outcome(self, api, fx, combo):
        """Run the whole copy-previous batch over this combination and
        return ``(payload, row_ids)``.

        DISCOVERY IS THE POINTER HALF of ``auto_build_roster``'s union
        cohort, deliberately. A copy-previous run would have to SEAT the
        subject on a prior game first, which needs a live HOME stint, which
        would leave an extra terminal row in the history and change what
        two of these combinations mean. The permanent pointer puts them in
        the cohort while contributing NOTHING the classifier reads (this
        game is LeagueSeason-bound), so the planted rows are exactly the
        combination's. ``mate`` is an ordinary eligible team-mate, so every
        result is a genuine PARTIAL outcome and never a zero-seat one."""
        mate = self._player(api, fx["home"], "Ada Available")
        pid = self._build_combo(api, fx, combo, pointer_team=fx["home"])
        rows = self._assert_rows(api, pid, combo)
        r = api.auto_build_roster(fx["gid"], team_id=fx["home"],
                                  actor_id=ADMIN)
        self.assertNotIn("error", r, r)
        self.assertEqual(r["seated"], [mate["id"]], r)
        detail = self._batch_audit(api, fx["gid"]).detail
        return (json.dumps({"seated": r["seated"], "skipped": r["skipped"],
                            "deferred": r["deferred"],
                            "candidate_count": r["candidate_count"],
                            "audit": detail}, sort_keys=True),
                tuple(m.id for m in rows))

    def test_the_three_backends_report_an_identical_outcome(self):
        outcomes, ran = {}, []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for combo in MULTI_ROW_COMBOS:
                    store.clear_all_data()
                    api, fx = self._pair(store)
                    payload, _order = self._combo_outcome(api, fx, combo)
                    outcomes.setdefault(combo.name, {})[label] = payload
                    ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)
        for combo in MULTI_ROW_COMBOS:
            got = outcomes[combo.name]
            # THE cross-backend assertion: ONE value, shared by all three.
            self.assertEqual(len(set(got.values())), 1, (combo.name, got))
            payload = json.loads(next(iter(got.values())))
            # …and the one value is the reason the combination declares.
            self.assertEqual(
                sorted({s["reason"] for s in payload["skipped"]}),
                [combo.reason], (combo.name, payload))
            # …carried identically into the durable audit row.
            self.assertEqual(
                [(s["player_id"], s["reason"])
                 for s in payload["audit"]["skipped"]],
                [(s["player_id"], s["reason"])
                 for s in payload["skipped"]], (combo.name, payload))

    def test_the_stores_really_did_hand_back_different_row_orders(self):
        """The falsifier for the class above. If Memory and SQL happened to
        list a player's rows in the SAME order, "the three agree" would be
        proving nothing about the collapse — so the divergence in the INPUT
        is asserted, not assumed. ``srm_10`` sorts before ``srm_2`` on a
        TEXT id column and after it in insertion order, which is exactly the
        divergence the service must absorb."""
        orders, ran = {}, []
        combo = MULTI_ROW_COMBOS[0]
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                # Burn membership ids until the combination's own rows
                # STRADDLE the srm_9/srm_10 boundary, which is the only
                # place a TEXT ordering and an insertion ordering can
                # disagree for ids of this shape.
                burn = self._pointer_only_player(api, fx["third"], "Padding")
                while len(api.store.all_season_roster_memberships()) < 7:
                    m = self._membership(api, burn["id"], fx["ls_id"],
                                         fx["home"])
                    self._end(api, m["id"], "released")
                pid = self._build_combo(api, fx, combo)
                rows = self._assert_rows(api, pid, combo)
                orders[label] = tuple(m.id for m in rows)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)
        self.assertNotEqual(orders["memory"], orders["sqlite"], orders)
        if "postgres" in orders:
            self.assertNotEqual(orders["memory"], orders["postgres"], orders)


# ======================================================================
# 6. the ladder governs ACROSS (status, team) keys, not only within one
# ======================================================================
class _TwoFailingRowsFixture(_ReasonHarness):
    """One player, TWO right-keyed participation-granting rows that both
    FAIL — one with a denormalized ``season_id`` (ladder rank 3), one
    spine-correct but on a bench whose registration has lapsed (rank 9).

    The three shapes differ ONLY in whether the two rows land on the same
    ``(status, team)`` key, and that is the whole point: the collapse
    ``_keep_best_reason`` resolves happens INSIDE a key, so the shapes that
    straddle two keys were never reached by it.

    MEMORY-ONLY, BY THE ENGINE AND NOT BY CHOICE. All three plant a SECOND
    non-terminal row for one player at one LeagueSeason, which migration
    059's ``ux_srm_open_player_league_season`` — ``(player_id,
    league_season_id) WHERE status NOT IN ('released','transferred')`` —
    refuses. Measured on both SQL backends, for each of the three shapes:

        IntegrityConflictError: Player already has an open membership on
        this league season; update or end it instead of creating another.

    The index does not care which BENCH or which non-terminal STATUS the
    second row carries, so widening the shape cannot buy a SQL leg.
    ``TwoOpenStintsAtOneLeagueSeasonAreEngineRefused`` above pins the
    refusal itself, on every backend. Writing this matrix as tri-store would
    therefore be a fake: ``InMemoryStore`` enforces no uniqueness and is a
    first-class backend of this suite, so the shape is REAL there and the
    tie-break has to be imposed in the service. That is the same reasoning
    ``_keep_best_reason``'s own regression test states, and it is why these
    are honestly labelled rather than dressed up."""

    #: ``(name, second row's status, second row's bench, note)``
    STRADDLES = (
        ("one_key",
         MembershipStatus.ACTIVE, "home",
         "THE CONTROL — both rows ACTIVE on HOME, so they share ONE "
         "(status, team) key and _keep_best_reason alone already answered "
         "this correctly at f570d78. It stays pinned as the BOUNDARY of "
         "what that commit closed"),
        ("two_statuses_one_bench",
         MembershipStatus.AFFILIATE, "home",
         "GAP A — same bench, DIFFERENT status, so the rows sit at two "
         "keys and the survivor used to be chosen by ACTIVE-before-"
         "AFFILIATE. That order has nothing to do with the ladder, so the "
         "rank-9 reason was reported while the rank-3 one also applied"),
        ("one_status_two_benches",
         MembershipStatus.ACTIVE, "away",
         "GAP B — same status, DIFFERENT bench: two keys again, and the "
         "survivor used to be chosen by home-before-away. Same defect, "
         "reached through the other half of the seating order"),
    )

    def _plant_two_failing_rows(self, api, teams, ls_id, status, bench):
        """The ACTIVE/HOME row whose registration has lapsed, plus a second
        row at ``(status, bench)`` carrying a denormalized ``season_id``."""
        home = teams["home"]["id"]
        pid = self._pointer_only_player(api, teams["third"]["id"],
                                        "Twin Failure")["id"]
        m = self._membership(api, pid, ls_id, home)
        row = api.store.get_season_roster_membership(m["id"])
        api.store.add_season_roster_membership(dataclasses.replace(
            row, id=api.store.next_id("srm"), status=status,
            team_id=teams[bench]["id"],
            season_id="season_that_does_not_exist"))
        # …and the spine-correct row fails too, so neither can seat and the
        # RAW bucket is what names the skip.
        (reg,) = api.store.registrations_for_team_in_league_season(
            ls_id, home)
        reg.active = False
        api.store.save_season_team_registration(reg)
        rows = list(api.store.memberships_for_player(pid))
        self.assertEqual(len(rows), 2, rows)
        return pid


class TheLadderGovernsAcrossKeysNotOnlyWithinOne(_TwoFailingRowsFixture,
                                                 unittest.TestCase):
    """``SKIP_REASON_PRECEDENCE`` decides the reported reason over the WHOLE
    ``raw`` bucket, not merely among the rows sharing one key.

    WHAT WAS WRONG, and it was the CLAIM before it was the code. f570d78
    made ``_keep_best_reason`` collapse rows at ONE ``(status, team)`` key
    by ``reason_rank``, and ``membership_spine`` then stated that the ladder
    "GOVERNS WITHIN A RUNG TOO, not only between rungs". For the ``raw``
    bucket that held only inside a key: the survivor ACROSS keys was still
    picked by ``_pick_eligible_membership``'s ACTIVE-before-AFFILIATE then
    home-before-away walk — the SEATING order, which has no relationship to
    the ladder. So a player carrying a rank-3 denormalized ``season_id``
    mismatch on one key and a rank-9 lapsed registration on another was
    reported under rank 9, and the ladder's own promise was false.

    Measured at f570d78, all three shapes deterministic under every row
    order:

        one_key                 -> membership_denormalized_season_mismatch
        two_statuses_one_bench  -> team_not_registered      (rank 3 applied)
        one_status_two_benches  -> team_not_registered      (rank 3 applied)

    Nothing about those answers was unstable or untrue — which is exactly
    why this is worth closing rather than documenting. A ladder that is
    authoritative except in two shapes nobody can predict from reading it
    reintroduces the confusion the ladder was written to remove.

    ``RosterService._pick_reason_membership`` now ranks the ``raw`` pick by
    ``reason_rank`` first, keeping status-then-side as the secondary keys so
    two rows carrying the SAME reason still answer exactly as before. The
    SEATING pick is a different method on a different branch and is
    unmoved — ``TheSeatingPickIsNotTheReasonPick`` below is its proof."""

    def test_the_higher_ranked_reason_wins_however_the_rows_are_keyed(self):
        for name, status, bench, note in self.STRADDLES:
            store = InMemoryStore()
            try:
                self._assert_backend("memory", store)
                api, season, league, teams, game, ls_id = self._build(
                    store, target_skaters=4, target_goalies=1)
                with self.subTest(shape=name, note=note):
                    pid = self._plant_two_failing_rows(
                        api, teams, ls_id, status, bench)
                    # Every row order, not just the two the store happens
                    # to produce: the answer must not depend on order AT
                    # ALL, which is the property f570d78 established and
                    # this must not regress.
                    seen = TheReportedReasonIsInvariantUnderEveryRowOrder.\
                        _permuted_reasons(
                            self, api, api.store.get_game(game["id"]),
                            api.store.get_player(pid))
                    self.assertEqual(list(seen),
                                     [spine.DENORMALIZED_SEASON_MISMATCH],
                                     (name, seen))
            finally:
                self._close("memory", store)

    def test_the_other_applicable_reason_really_does_rank_lower(self):
        """The assertion above means nothing unless ``team_not_registered``
        was genuinely applicable and genuinely later in the ladder — a
        shape where only ONE reason applied would pass it vacuously.

        Both halves are asserted here: the lapsed-registration row is
        planted ALONE and reports ``team_not_registered`` on its own, and
        the ladder ranks that strictly after the denormalized reason."""
        self.assertLess(
            spine.reason_rank(spine.DENORMALIZED_SEASON_MISMATCH),
            spine.reason_rank(spine.NOT_REGISTERED))
        store = InMemoryStore()
        try:
            api, season, league, teams, game, ls_id = self._build(
                store, target_skaters=4, target_goalies=1)
            home = teams["home"]["id"]
            pid = self._pointer_only_player(api, teams["third"]["id"],
                                            "Lone Lapse")["id"]
            self._membership(api, pid, ls_id, home)
            (reg,) = api.store.registrations_for_team_in_league_season(
                ls_id, home)
            reg.active = False
            api.store.save_season_team_registration(reg)
            self.assertEqual(
                api.roster.seating_block_reason(
                    api.store.get_game(game["id"]),
                    api.store.get_player(pid)),
                spine.NOT_REGISTERED)
        finally:
            self._close("memory", store)

    def test_the_two_gap_shapes_are_engine_refused_on_both_sql_backends(self):
        """The Memory-only label above, PROVEN rather than asserted in
        prose — and proven for the gap shapes specifically, not merely for
        the two-ACTIVE-rows shape the class above already covers.

        A shape claimed to be Memory-only that a SQL backend actually
        accepts would mean this matrix is silently under-covering."""
        ran = []
        for label, store in self._stores():
            if label == "memory":
                self._close(label, store)
                continue
            try:
                self._assert_backend(label, store)
                for name, status, bench, _note in self.STRADDLES:
                    store.clear_all_data()
                    api, season, league, teams, game, ls_id = self._build(
                        store, target_skaters=4, target_goalies=1)
                    with self.subTest(backend=label, shape=name):
                        with self.assertRaises(IntegrityConflictError):
                            self._plant_two_failing_rows(
                                api, teams, ls_id, status, bench)
                    ran.append((label, name))
            finally:
                self._close(label, store)
        backends = {b for b, _s in ran}
        expected = {"sqlite"}
        if os.environ.get("TEST_DATABASE_URL"):
            expected.add("postgres")
        else:
            print("\n[SKIP-REASON MATRIX] " + _PG_SKIP)
        self.assertEqual(backends, expected, sorted(backends))
        for backend in expected:
            self.assertEqual({s for b, s in ran if b == backend},
                             {s[0] for s in self.STRADDLES}, backend)


class TheSeatingPickIsNotTheReasonPick(_ReasonHarness, unittest.TestCase):
    """THE GUARD ON THE FIX ABOVE, and the higher-risk half of it.

    ``_pick_membership`` is shared. It backs the pick that NAMES a skip
    reason and, through ``_pick_eligible_membership``, the pick that SEATS a
    player — and only the first of those wants the reason ladder. The
    seating order is load-bearing for a different reason entirely: it is why
    a player pathologically eligible on BOTH sides resolves to exactly ONE,
    which is the double-count defect ``test_slot_overfill_regression`` was
    written for. Retuning the shared helper would have moved it silently.

    So the fix added a SEPARATE method rather than a parameter, and this
    class pins the seating answer independently: ACTIVE outranks AFFILIATE
    even when the AFFILIATE row is on HOME, and home breaks the tie only
    among equals. ``affiliate_home_active_away`` is the discriminating case
    — it is the one shape where the STATUS rung and the SIDE rung disagree,
    so a seating pick that had been re-ordered by anything else would show
    up here. Measured identical at f570d78 and after the fix, in both the
    single and the batched resolver, under every row order:

        active_home_affiliate_away  -> srm_1 on HOME
        affiliate_home_active_away  -> srm_2 on AWAY   <- discriminating
        active_both                 -> srm_1 on HOME

    MEMORY-ONLY, for the SAME engine reason as the class above and measured
    the same way: two open rows for one player at one LeagueSeason are
    refused by ``ux_srm_open_player_league_season`` on both SQL backends
    regardless of bench or status, so "eligible on both sides at once" is
    not constructible there at all. That is not a coverage hole in the fix —
    the seating path is exercised tri-store by the whole resolver /
    attribution / overfill suite, which this change leaves untouched — it is
    the honest bound on THIS shape."""

    SHAPES = (
        ("active_home_affiliate_away", MembershipStatus.ACTIVE,
         MembershipStatus.AFFILIATE, "home"),
        ("affiliate_home_active_away", MembershipStatus.AFFILIATE,
         MembershipStatus.ACTIVE, "away"),
        ("active_both", MembershipStatus.ACTIVE, MembershipStatus.ACTIVE,
         "home"),
    )

    def test_a_player_eligible_on_both_sides_seats_where_it_always_did(self):
        for name, home_status, away_status, expect in self.SHAPES:
            store = InMemoryStore()
            try:
                self._assert_backend("memory", store)
                api, season, league, teams, game, ls_id = self._build(
                    store, target_skaters=4, target_goalies=1)
                home, away = teams["home"]["id"], teams["away"]["id"]
                with self.subTest(shape=name):
                    pid = self._pointer_only_player(
                        api, teams["third"]["id"], "Both Benches")["id"]
                    m = self._membership(api, pid, ls_id, home)
                    row = api.store.get_season_roster_membership(m["id"])
                    api.store.save_season_roster_membership(
                        dataclasses.replace(row, status=home_status))
                    api.store.add_season_roster_membership(
                        dataclasses.replace(
                            row, id=api.store.next_id("srm"), team_id=away,
                            status=away_status))
                    want = home if expect == "home" else away
                    g = api.store.get_game(game["id"])
                    player = api.store.get_player(pid)
                    # (a) the SINGLE form, under every row order.
                    real = store.memberships_for_player
                    rows = list(real(pid))
                    seen = {}
                    try:
                        for perm in itertools.permutations(rows):
                            store.memberships_for_player = (
                                lambda p, _p=perm:
                                [x for x in _p if x.player_id == p])
                            ctx = api.roster.resolve_membership_context(
                                g, player)
                            seen.setdefault(
                                (ctx.membership.id, ctx.team_id), []).append(
                                    [x.id for x in perm])
                    finally:
                        store.memberships_for_player = real
                    self.assertEqual(list(seen), [("srm_1" if expect == "home"
                                                   else "srm_2", want)],
                                     (name, seen))
                    # (b) …and the BATCHED form agrees, which is what the
                    # slot arithmetic actually calls.
                    batch = api.roster.resolve_membership_contexts_for_game(g)
                    self.assertEqual(batch[pid].team_id, want, (name, batch))
                    self.assertEqual(batch[pid].membership.id,
                                     next(iter(seen))[0], (name, batch))
            finally:
                self._close("memory", store)

    def test_the_reason_pick_and_the_seating_pick_are_separate_methods(self):
        """The structural half. The two picks are separate ATTRIBUTES, so a
        future edit retuning one cannot silently retune the other — which is
        precisely what would have happened had the ladder been pushed into
        ``_pick_membership``.

        The seating pick is also asserted NOT to consult the reason map:
        rows reaching it SUCCEEDED and carry no reason at all, so ranking
        them by one is not merely wrong, it is unanswerable."""
        self.assertIsNot(RosterService._pick_reason_membership,
                         RosterService._pick_eligible_membership)
        import inspect
        seating = inspect.signature(RosterService._pick_eligible_membership)
        naming = inspect.signature(RosterService._pick_reason_membership)
        self.assertNotIn("why", seating.parameters, seating)
        self.assertIn("why", naming.parameters, naming)


# ======================================================================
# 7. the OTHER collapse tie-break — what _keep_lowest_id is really worth
# ======================================================================
class TheLowestIdTieBreakIsObservableWhereItSeats(
        _ReasonHarness, unittest.TestCase):
    """``_keep_lowest_id`` has THREE call sites, and they are not equally
    defensible. A previous round described the helper as "deliberately not
    mutation-observable"; reverting all three to the old ``setdefault``
    spelling does indeed leave the whole suite green. That is a statement
    about the SUITE, not about the helper, and this class corrects it by
    building the test that was missing.

    ``valid`` and ``per_player`` ARE observable, and here is the shape:
    two participation-granting rows at one ``(status, team)`` key carrying
    DIFFERENT season-scoped ``position`` values. The surviving row supplies
    ``GameMembershipContext.position``, hence the GOALIE/SKATER bucket the
    slot arithmetic counts the player in — so the two spellings do not merely
    pick a different row, they seat the player into a different SLOT TYPE.
    Measured by permuting the store read the resolver actually calls:

        _keep_lowest_id  ->  {('srm_1','forward')}                 one answer
        setdefault       ->  {('srm_1','forward'), ('srm_2','goalie')}

    Both spellings agree on the store's NATURAL order (insertion order on
    Memory puts the low id first, and SQL refuses the input outright), which
    is why no pre-existing test could see the difference. The permutation
    injection is what makes it visible, and it is the same technique
    ``TheReportedReasonIsInvariantUnderEveryRowOrder`` already uses.

    ``parked`` is the genuinely unfalsifiable one, and NOT merely for want
    of a test: its survivor is consumed as
    ``status_ineligible_reason(m.status)``, and ``status`` is half the key
    the rows collapsed onto. Every row at one parked key therefore yields
    the identical string no matter which survives. That call site is
    hardening against a future reader of the bucket, nothing more, and it is
    described that way in the helper rather than claimed as a fix.

    MEMORY-ONLY for the same engine reason as the classes above: two open
    rows at one LeagueSeason are refused by
    ``ux_srm_open_player_league_season`` on both SQL backends."""

    def _two_positions_at_one_key(self, api, teams, ls_id):
        """One player, two ACTIVE HOME rows, FORWARD and GOALIE. Both are
        fully spine-valid, so this exercises the ``valid``/``per_player``
        buckets and never the reason path."""
        pid = self._pointer_only_player(api, teams["third"]["id"],
                                        "Twin Position",
                                        position="forward")["id"]
        m = self._membership(api, pid, ls_id, teams["home"]["id"])
        row = api.store.get_season_roster_membership(m["id"])
        api.store.save_season_roster_membership(
            dataclasses.replace(row, position=Position.FORWARD))
        api.store.add_season_roster_membership(dataclasses.replace(
            row, id=api.store.next_id("srm"), position=Position.GOALIE))
        rows = list(api.store.memberships_for_player(pid))
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual({r.position for r in rows},
                         {Position.FORWARD, Position.GOALIE}, rows)
        return pid

    def test_the_single_resolver_answers_one_context_in_every_row_order(self):
        store = InMemoryStore()
        try:
            self._assert_backend("memory", store)
            api, season, league, teams, game, ls_id = self._build(
                store, target_skaters=4, target_goalies=1)
            pid = self._two_positions_at_one_key(api, teams, ls_id)
            g = api.store.get_game(game["id"])
            player = api.store.get_player(pid)
            real = store.memberships_for_player
            rows = list(real(pid))
            seen = {}
            try:
                for perm in itertools.permutations(rows):
                    store.memberships_for_player = (
                        lambda p, _p=perm: [x for x in _p if x.player_id == p])
                    ctx = api.roster.resolve_membership_context(g, player)
                    seen.setdefault((ctx.membership.id, ctx.position), []
                                    ).append([x.id for x in perm])
            finally:
                store.memberships_for_player = real
            self.assertEqual(list(seen), [("srm_1", Position.FORWARD)], seen)
        finally:
            self._close("memory", store)

    def test_the_batched_resolver_answers_one_context_too(self):
        """The third call site, reached through the store method the BATCH
        reads — a different method from the one above, so the single form's
        invariance says nothing about it."""
        store = InMemoryStore()
        try:
            self._assert_backend("memory", store)
            api, season, league, teams, game, ls_id = self._build(
                store, target_skaters=4, target_goalies=1)
            pid = self._two_positions_at_one_key(api, teams, ls_id)
            g = api.store.get_game(game["id"])
            real = store.memberships_for_league_season_team
            mine = [r for r in real(g.league_season_id, g.home_team_id)
                    if r.player_id == pid]
            self.assertEqual(len(mine), 2, mine)
            seen = {}
            try:
                for perm in itertools.permutations(mine):
                    store.memberships_for_league_season_team = (
                        lambda ls, t, _p=perm, _r=real:
                        list(_p) + [x for x in _r(ls, t)
                                    if x.player_id != pid])
                    ctx = api.roster.resolve_membership_contexts_for_game(
                        g)[pid]
                    seen.setdefault((ctx.membership.id, ctx.position), []
                                    ).append([x.id for x in perm])
            finally:
                store.memberships_for_league_season_team = real
            self.assertEqual(list(seen), [("srm_1", Position.FORWARD)], seen)
        finally:
            self._close("memory", store)

    def test_the_parked_call_site_cannot_be_observed_at_all(self):
        """The honest negative, asserted rather than assumed.

        Two RELEASED rows at one key differing in every field that is NOT
        part of the key still report ONE reason under EITHER spelling,
        because the reason is derived from the key itself. This test passes
        with ``_keep_lowest_id`` and passes with ``setdefault`` — that is
        the finding, and stating it here is the point of the test."""
        store = InMemoryStore()
        try:
            self._assert_backend("memory", store)
            api, season, league, teams, game, ls_id = self._build(
                store, target_skaters=4, target_goalies=1)
            pid = self._pointer_only_player(api, teams["third"]["id"],
                                            "Twin Parked")["id"]
            m = self._membership(api, pid, ls_id, teams["home"]["id"])
            end_membership_directly(api.store, m["id"], "released")
            row = api.store.get_season_roster_membership(m["id"])
            api.store.add_season_roster_membership(dataclasses.replace(
                row, id=api.store.next_id("srm"),
                position=Position.GOALIE, jersey_number=99))
            seen = TheReportedReasonIsInvariantUnderEveryRowOrder.\
                _permuted_reasons(self, api, api.store.get_game(game["id"]),
                                  api.store.get_player(pid))
            self.assertEqual(list(seen), ["membership_released"], seen)
            # …and it is the KEY that decided, not the tie-break: the reason
            # is a pure function of the status the rows share.
            self.assertEqual(
                spine.status_ineligible_reason(MembershipStatus.RELEASED),
                "membership_released")
        finally:
            self._close("memory", store)


if __name__ == "__main__":
    unittest.main()
