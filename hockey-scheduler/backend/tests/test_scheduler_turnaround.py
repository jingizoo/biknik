"""Configurable turnaround measured from the previous game's END (#390).

The owner's worked example, verbatim: *a 2:00-3:30 game followed by a 3:30
game is currently allowed*. Three defects compounded so that none of them was
visible on its own —

1. the Generate screen never sent a rest/turnaround value at all, so it
   defaulted to zero;
2. the calculation compared game START times, so even a configured value
   passed a back-to-back pair (15:30 - 14:00 = 90 minutes of "rest" for a
   pairing with zero minutes of actual ice-free time); and
3. it consulted only this batch's own picks, never the games already on the
   ice.

Each test below isolates ONE of those clauses, so the falsifying mutation for
one clause leaves the others' tests green. The mandatory anti-vacuity control
— *the same pair with sufficient turnaround is accepted* — accompanies every
refusal assertion: without it, "refused" is indistinguishable from "nothing
was schedulable".

Fixture geometry is the issue's own example throughout: one rink, a
14:00-15:30 game on the ice, and a 15:30 slot behind it.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND, commit_fresh_draft  # noqa: F401  (BACKEND: sys.path)

from hockey_scheduler.api import ApiService
from hockey_scheduler.api.service import ApiService as BaseApiService
from hockey_scheduler.domain import (
    Division,
    Game,
    GameType,
    IceSlot,
    IceSlotStatus,
    League,
    LeagueSeason,
    Organization,
    Program,
    Rink,
    Season,
    SeasonTeamRegistration,
    SeasonVenueAccess,
    Team,
    Venue,
)
from hockey_scheduler.domain.errors import ValidationError
from hockey_scheduler.services import draft_schedule
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv

UTC = timezone.utc
# A Saturday, so the calendar day is stable and human-checkable in the
# browser journeys that read the same date off the rendered row.
DAY = datetime(2026, 9, 5, tzinfo=UTC)
PRIOR_START = DAY.replace(hour=14)                 # 2:00 pm
PRIOR_END = DAY.replace(hour=15, minute=30)        # 3:30 pm


def _minutes(n):
    return timedelta(minutes=n)


class TurnaroundFixture:
    """One Division, one rink, and the issue's own 14:00-15:30 prior game.

    ``make_store`` is supplied by the concrete Memory/durable subclasses so
    every behavioural claim below is proven at the service boundary on each
    backend, not only in memory.
    """

    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()
        self.api = ApiService(self.store)

    def tearDown(self):
        conn = getattr(self.store, "conn", None)
        if conn is not None:
            conn.close()

    def _hierarchy(self, n_teams=2):
        s = self.store
        s.add_organization(Organization(id="org1", name="Owner"))
        s.add_program(Program(id="prog1", name="Program",
                              operator_organization_id="org1"))
        s.add_season(Season(id="se1", program_id="prog1", name="Season"))
        s.add_venue(Venue(id="v1", name="Arena", organization_id="org1"))
        s.add_season_venue_access(SeasonVenueAccess(
            id="sva1", season_id="se1", venue_id="v1", active=True))
        s.add_rink(Rink(id="r1", venue_id="v1", name="Main"))
        s.add_league(League(id="lg1", program_id="prog1", name="League"))
        s.add_league_season(LeagueSeason(
            id="ls1", league_id="lg1", season_id="se1"))
        s.add_division(Division(id="div1", league_season_id="ls1", name="D1"))
        for i in range(n_teams):
            s.add_team(Team(id=f"t{i}", name=f"Team {i}",
                            program_id="prog1", league_id="lg1"))
            s.add_season_team_registration(SeasonTeamRegistration(
                id=f"reg{i}", league_season_id="ls1", team_id=f"t{i}",
                division_id="div1", active=True))

    def _slot(self, slot_id, start, end, status=IceSlotStatus.AVAILABLE):
        self.store.add_ice_slot(IceSlot(
            id=slot_id, rink_id="r1", start_time=start, end_time=end,
            status=status))
        return slot_id

    def _prior_game(self, game_id="g_prior", home="t0", away="t1",
                    game_type=GameType.EXHIBITION.value):
        """The 2:00-3:30 game already on the ice.

        EXHIBITION by default and deliberately: ``_existing_pairing_games``
        counts only REGULAR fixtures, so an exhibition leaves the round-robin
        pairing outstanding and the generator must still place it. That keeps
        the fixture at exactly ONE pairing and exactly ONE candidate slot, so
        "refused" and "accepted" are unambiguous rather than a side effect of
        the already-scheduled split (#206 slice 1) removing the pairing.
        """
        self._slot("slotA", PRIOR_START, PRIOR_END, IceSlotStatus.ALLOCATED)
        self.store.add_game(Game(
            id=game_id, home_team_id=home, away_team_id=away,
            start_time=PRIOR_START, end_time=PRIOR_END,
            ice_slot_id="slotA", division_id="div1", season_id="se1",
            league_id="lg1", league_season_id="ls1", game_type=game_type))
        return game_id

    def _draft(self, **constraints):
        return draft_schedule(self.store, "div1", constraints=constraints or None)


class ExistingGameTurnaroundTest(TurnaroundFixture):
    """Clause 2 + clause 3: measured from the previous game's END, against a
    game that is ALREADY on the ice."""

    # -- the worked example -------------------------------------------------
    def test_back_to_back_start_after_existing_game_is_refused(self):
        """*A 2:00-3:30 game followed by a 3:30 game* — refused.

        Nothing but the turnaround can refuse this candidate: the slot is
        AVAILABLE game ice on a rink with no scheduling policy configured,
        and 15:30-17:00 does not OVERLAP 14:00-15:30, so neither
        ``slot_overlap_conflict`` nor ``team_overlap`` applies. The
        control immediately below proves the fixture can place this pairing
        when the turnaround permits it.
        """
        self._hierarchy()
        blocker = self._prior_game()
        self._slot("slotB", PRIOR_END, PRIOR_END + timedelta(hours=1, minutes=30))
        res = self._draft(min_turnaround_minutes=60)
        self.assertEqual(res["draft_games"], [], repr(res))
        self.assertEqual(len(res["unscheduled"]), 1, repr(res))
        row = res["unscheduled"][0]
        self.assertIn("min_turnaround", row["reason_codes"], repr(row))
        self.assertIn("minimum turnaround not met", row["reason"])
        # The refusal NAMES the blocking game and the shortfall (#390 req 5).
        conflicts = row["turnaround_conflicts"]
        self.assertEqual(len(conflicts), 2, repr(conflicts))  # both teams
        for conflict in conflicts:
            self.assertEqual(conflict["conflict_game_id"], blocker)
            self.assertEqual(conflict["conflict_source"], "existing_game")
            self.assertEqual(conflict["gap_minutes"], 0)
            self.assertEqual(conflict["shortfall_minutes"], 60)
            self.assertEqual(conflict["conflict_start_time"],
                             PRIOR_START.isoformat())
            self.assertEqual(conflict["conflict_end_time"],
                             PRIOR_END.isoformat())
        self.assertEqual({c["team_id"] for c in conflicts}, {"t0", "t1"})

    def test_a_later_game_constrains_the_turnaround_too(self):
        """Turnaround is an UNDIRECTED gap between two of a team's games.

        The issue names the previous game's end because that is the worked
        example, but the same ice-free interval has to exist on the other
        side: a candidate that ENDS moments before an already-booked game
        BEGINS leaves the team exactly as little turnaround. Persisted
        occupancy is not ordered relative to the candidate, so a
        one-directional check would silently pass this.
        """
        self._hierarchy()
        blocker = self._prior_game()
        earlier = PRIOR_START - timedelta(hours=1)
        self._slot("slotB", earlier, PRIOR_START)
        res = self._draft(min_turnaround_minutes=60)
        self.assertEqual(res["draft_games"], [], repr(res))
        conflict = res["unscheduled"][0]["turnaround_conflicts"][0]
        self.assertEqual(conflict["conflict_game_id"], blocker)
        self.assertEqual(conflict["gap_minutes"], 0)
        self.assertEqual(conflict["shortfall_minutes"], 60)

    def test_a_later_game_control_with_sufficient_turnaround(self):
        """Anti-vacuity control for the undirected case."""
        self._hierarchy()
        self._prior_game()
        earlier = PRIOR_START - _minutes(60)
        self._slot("slotB", earlier - timedelta(hours=1), earlier)
        res = self._draft(min_turnaround_minutes=60)
        self.assertEqual(res["unscheduled"], [], repr(res))
        self.assertEqual(len(res["draft_games"]), 1, repr(res))

    def test_sufficient_turnaround_after_existing_game_is_accepted(self):
        """THE ANTI-VACUITY CONTROL. Identical fixture, identical
        constraint, candidate moved to 16:30 — 60 minutes after the prior
        game ends. It must be PLACED. Without this, the refusal above is
        indistinguishable from "nothing was schedulable"."""
        self._hierarchy()
        self._prior_game()
        later = PRIOR_END + _minutes(60)
        self._slot("slotB", later, later + timedelta(hours=1))
        res = self._draft(min_turnaround_minutes=60)
        self.assertEqual(res["unscheduled"], [], repr(res))
        self.assertEqual(len(res["draft_games"]), 1, repr(res))
        self.assertEqual(res["draft_games"][0]["ice_slot_id"], "slotB")

    def test_turnaround_boundary_is_inclusive_at_exactly_the_configured_gap(self):
        """Exactly the configured turnaround is ENOUGH; one minute less is
        not. Pins the comparison edge so a `<=`/`<` slip is a failure rather
        than a silent off-by-one either way."""
        self._hierarchy()
        self._prior_game()
        exact = PRIOR_END + _minutes(60)
        self._slot("slotB", exact, exact + timedelta(hours=1))
        self.assertEqual(len(self._draft(
            min_turnaround_minutes=60)["draft_games"]), 1)
        self.assertEqual(self._draft(
            min_turnaround_minutes=61)["draft_games"], [])

    # -- clause 2 in isolation: END, not START ------------------------------
    def test_measured_from_end_not_start(self):
        """The fixture that separates the two calculations.

        Prior game 14:00-15:30, candidate at 15:30, turnaround 60 minutes:

        * comparing STARTS gives 15:30 - 14:00 = 90 minutes, which PASSES;
        * comparing the previous game's END gives 15:30 - 15:30 = 0 minutes,
          which FAILS.

        So a start-time comparison places this pairing and an end-time
        comparison refuses it. The 90-minute assertion is written out
        explicitly so the fixture cannot drift into one where both
        calculations happen to agree.
        """
        self._hierarchy()
        self._prior_game()
        self._slot("slotB", PRIOR_END, PRIOR_END + timedelta(hours=1))
        self.assertEqual(
            (PRIOR_END - PRIOR_START).total_seconds() / 60.0, 90.0,
            "fixture drift: start-vs-start must exceed the 60-minute "
            "turnaround, or this test proves nothing about which edge is "
            "measured")
        res = self._draft(min_turnaround_minutes=60)
        self.assertEqual(res["draft_games"], [], repr(res))
        self.assertIn("min_turnaround", res["unscheduled"][0]["reason_codes"])

    def test_min_rest_hours_alone_still_permits_the_back_to_back_pair(self):
        """The pre-#390 knob, held to its own unchanged contract.

        ``min_rest_hours`` is start-to-start by definition and stays that
        way (#85 semantics, and every existing regression depends on it):
        one hour of it does NOT refuse a 15:30 start behind a 14:00 start.
        That is precisely why a separate END-measured turnaround had to
        exist, and asserting it here stops a future "fix" from quietly
        redefining the old field instead of adding the new one.
        """
        self._hierarchy()
        self._prior_game()
        self._slot("slotB", PRIOR_END, PRIOR_END + timedelta(hours=1))
        res = self._draft(min_rest_hours=1)
        self.assertEqual(len(res["draft_games"]), 1, repr(res))

    # -- clause 3 in isolation: already-scheduled games count ---------------
    def test_a_committed_regular_game_also_blocks_the_turnaround(self):
        """Not only exhibitions: a REGULAR committed Game for a DIFFERENT
        pairing constrains the teams it involves just the same.

        Three teams, so t0-vs-t1's real Game leaves t0-vs-t2 and t1-vs-t2
        outstanding, and the only free ice starts the instant that Game
        ends. Proves the comparison reads the persisted schedule rather
        than only this batch's own picks — with a candidate pairing that is
        NOT the blocking Game's own pairing, so the already-scheduled split
        cannot be what produced the refusal.
        """
        self._hierarchy(n_teams=3)
        blocker = self._prior_game(game_type=GameType.REGULAR.value)
        self._slot("slotB", PRIOR_END, PRIOR_END + timedelta(hours=1))
        res = self._draft(min_turnaround_minutes=60)
        self.assertEqual(res["draft_games"], [], repr(res))
        blocked = {frozenset((u["home_team_id"], u["away_team_id"]))
                   for u in res["unscheduled"]}
        self.assertEqual(blocked, {frozenset(("t0", "t2")),
                                   frozenset(("t1", "t2"))}, repr(res))
        for row in res["unscheduled"]:
            self.assertIn("min_turnaround", row["reason_codes"])
            self.assertEqual(
                {c["conflict_game_id"] for c in row["turnaround_conflicts"]},
                {blocker})
        # Control on the SAME fixture: the pairing t0-vs-t1 really was
        # already scheduled, so the refusal above is about the other two.
        self.assertEqual(len(res["already_scheduled"]), 1, repr(res))

    def test_committed_regular_game_control_with_sufficient_turnaround(self):
        """Anti-vacuity control for the regular-game fixture above."""
        self._hierarchy(n_teams=3)
        self._prior_game(game_type=GameType.REGULAR.value)
        later = PRIOR_END + _minutes(60)
        self._slot("slotB", later, later + timedelta(hours=1))
        self._slot("slotC", later + timedelta(days=1),
                   later + timedelta(days=1, hours=1))
        res = self._draft(min_turnaround_minutes=60)
        self.assertEqual(res["unscheduled"], [], repr(res))
        self.assertEqual(len(res["draft_games"]), 2, repr(res))

    def test_the_row_level_conflict_list_is_bounded_and_reports_the_near_miss(self):
        """One row per (team, source, blocking game) — the NEAREST miss.

        A pairing is tried against EVERY free slot, and each candidate has its
        own gap from the same blocker, so a list keyed by the measured gap
        would grow as slots x blockers x teams. That list is returned in the
        response AND hashed into ``draft_fingerprint``, so it has to be bounded
        by something structural, exactly as ``team_conflicts`` is (#373).

        Which row survives is not arbitrary: an operator asking "how close did
        this come?" wants the SMALLEST shortfall, so the surviving row is the
        largest gap. Ten candidates against one blocker and two teams must
        therefore yield two rows, naming the closest candidate's gap.
        """
        self._hierarchy()
        self._prior_game()
        for i in range(1, 11):
            start = PRIOR_END + _minutes(30 * i)
            self._slot(f"slotB{i}", start, start + _minutes(20))
        res = self._draft(min_turnaround_minutes=600)
        conflicts = res["unscheduled"][0]["turnaround_conflicts"]
        self.assertEqual(len(conflicts), 2, repr(conflicts))
        self.assertEqual({c["team_id"] for c in conflicts}, {"t0", "t1"})
        for conflict in conflicts:
            self.assertEqual(conflict["conflict_game_id"], "g_prior")
            # The last candidate starts 300 minutes after the blocker ends.
            self.assertEqual(conflict["gap_minutes"], 300.0)
            self.assertEqual(conflict["shortfall_minutes"], 300.0)

    def test_a_cancelled_game_does_not_block_the_turnaround(self):
        """Cancelled ice is free ice — the same rule every other occupancy
        reader in this engine follows (``_active_game_slot_pairs``). Proves
        the new check reuses that snapshot instead of scanning games itself
        with different rules."""
        self._hierarchy()
        self._prior_game()
        game = self.store.get_game("g_prior")
        game.cancelled = True
        self.store.save_game(game)
        self._slot("slotB", PRIOR_END, PRIOR_END + timedelta(hours=1))
        res = self._draft(min_turnaround_minutes=60)
        self.assertEqual(len(res["draft_games"]), 1, repr(res))

    # -- clause: zero preserves today's behaviour EXACTLY -------------------
    def test_zero_turnaround_preserves_todays_behaviour(self):
        """Zero (and omitted) must be byte-identical to the pre-#390
        proposal, ``draft_fingerprint`` included — the field is additive,
        not a behaviour change for callers that do not set it."""
        self._hierarchy()
        self._prior_game()
        self._slot("slotB", PRIOR_END, PRIOR_END + timedelta(hours=1))
        omitted = draft_schedule(self.store, "div1")
        zero = self._draft(min_turnaround_minutes=0)
        self.assertEqual(len(omitted["draft_games"]), 1, repr(omitted))
        self.assertEqual(omitted, zero)
        self.assertEqual(omitted["draft_fingerprint"],
                         zero["draft_fingerprint"])


class SameBatchTurnaroundTest(TurnaroundFixture):
    """Clause: a pairing placed EARLIER in the same generation is as real a
    constraint as one already committed."""

    def _three_slots(self):
        """15:30 sits back-to-back behind 14:00-15:30; the third slot is a
        day away so a refused pairing still has somewhere to go (otherwise
        "refused" and "no ice" are the same observation)."""
        self._slot("slot1", PRIOR_START, PRIOR_END)
        self._slot("slot2", PRIOR_END, PRIOR_END + timedelta(hours=1))
        self._slot("slot3", PRIOR_START + timedelta(days=1),
                   PRIOR_END + timedelta(days=1))

    def test_same_batch_proposal_blocks_a_back_to_back_pairing(self):
        """Three teams, three pairings, nothing persisted at all — every
        constraint here comes from this batch's own accepted candidates."""
        self._hierarchy(n_teams=3)
        self._three_slots()
        res = self._draft(min_turnaround_minutes=60)
        placed = {d["ice_slot_id"]: (d["home_team_id"], d["away_team_id"])
                  for d in res["draft_games"]}
        self.assertIn("slot1", placed, repr(res))
        # slot2 starts the instant slot1 ends, and every remaining pairing
        # shares a team with the one on slot1 (three teams, three pairings),
        # so slot2 must go unused.
        self.assertNotIn("slot2", placed, repr(res))
        self.assertIn("slot3", placed, repr(res))
        self.assertEqual(len(res["unscheduled"]), 1, repr(res))
        row = res["unscheduled"][0]
        self.assertIn("min_turnaround", row["reason_codes"], repr(row))
        sources = {c["conflict_source"] for c in row["turnaround_conflicts"]}
        self.assertIn("proposed_game", sources, repr(row))
        # A same-batch conflict names no Game id — nothing is persisted yet.
        for conflict in row["turnaround_conflicts"]:
            if conflict["conflict_source"] == "proposed_game":
                self.assertIsNone(conflict["conflict_game_id"])

    def test_same_batch_control_with_sufficient_turnaround(self):
        """THE ANTI-VACUITY CONTROL for the same-batch clause: identical
        three-pairing fixture, slot2 moved 60 minutes later, all three
        pairings placed."""
        self._hierarchy(n_teams=3)
        self._slot("slot1", PRIOR_START, PRIOR_END)
        self._slot("slot2", PRIOR_END + _minutes(60),
                   PRIOR_END + _minutes(120))
        self._slot("slot3", PRIOR_START + timedelta(days=1),
                   PRIOR_END + timedelta(days=1))
        res = self._draft(min_turnaround_minutes=60)
        self.assertEqual(res["unscheduled"], [], repr(res))
        self.assertEqual(
            {d["ice_slot_id"] for d in res["draft_games"]},
            {"slot1", "slot2", "slot3"}, repr(res))

    def test_zero_turnaround_places_the_back_to_back_same_batch_pair(self):
        """The same three-pairing fixture with the constraint OFF fills
        slot2 — proving the refusal above is caused by the turnaround and
        not by the fixture's own geometry."""
        self._hierarchy(n_teams=3)
        self._three_slots()
        res = self._draft(min_turnaround_minutes=0)
        self.assertEqual(res["unscheduled"], [], repr(res))
        self.assertEqual({d["ice_slot_id"] for d in res["draft_games"]},
                         {"slot1", "slot2", "slot3"}, repr(res))


class MemoryTurnaroundTest(ExistingGameTurnaroundTest, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurableTurnaroundTest(ExistingGameTurnaroundTest, unittest.TestCase):
    def make_store(self):
        url = os.environ.get("TEST_DATABASE_URL") or ":memory:"
        store = SqlStore(url)
        store.reset_schema()
        return store


class MemorySameBatchTest(SameBatchTurnaroundTest, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurableSameBatchTest(SameBatchTurnaroundTest, unittest.TestCase):
    def make_store(self):
        url = os.environ.get("TEST_DATABASE_URL") or ":memory:"
        store = SqlStore(url)
        store.reset_schema()
        return store


class TurnaroundValidationTest(TurnaroundFixture, unittest.TestCase):
    """Client input, so every bad shape is a structured ValidationError, never
    a raw exception across the facade boundary (#85's contract)."""

    def make_store(self):
        return InMemoryStore()

    def test_bad_shapes_are_validation_errors(self):
        self._hierarchy()
        self._slot("slotB", PRIOR_END, PRIOR_END + timedelta(hours=1))
        for bad in ("abc", -1, True, False, [], {}, object()):
            with self.assertRaises(ValidationError, msg=repr(bad)):
                draft_schedule(self.store, "div1",
                               constraints={"min_turnaround_minutes": bad})

    def test_facade_returns_a_structured_error(self):
        self._hierarchy()
        res = self.api.draft_season_schedule(
            "div1", constraints={"min_turnaround_minutes": "abc"})
        self.assertEqual(res["error"]["code"], "validation_error", repr(res))

    def test_fractional_minutes_are_accepted(self):
        """Minutes are a duration, not a count of things — a 90-second
        resurfacing allowance is a legitimate value, and rejecting floats
        would make the field less expressive than ``min_rest_hours``."""
        self._hierarchy()
        self._prior_game()
        self._slot("slotB", PRIOR_END + _minutes(1),
                   PRIOR_END + _minutes(61))
        self.assertEqual(
            self._draft(min_turnaround_minutes=1.5)["draft_games"], [])
        self.assertEqual(
            len(self._draft(min_turnaround_minutes=0.5)["draft_games"]), 1)


class TurnaroundExplanationTest(TurnaroundFixture, unittest.TestCase):
    """The refusal's bounded explanation extends #379's contract rather than
    inventing a parallel one: the same value object, the same per-pairing and
    per-preview candidate caps, the same allowlist mechanism, the same nested
    4-row conflict cap ``min_rest`` already uses, and the same in-scope-game-id
    rule that stops a neighbouring Season's Game id reaching the payload."""

    def make_store(self):
        return InMemoryStore()

    def test_explanation_carries_a_bounded_min_turnaround_rejection(self):
        self._hierarchy()
        blocker = self._prior_game()
        self._slot("slotB", PRIOR_END, PRIOR_END + timedelta(hours=1))
        res = self._draft(min_turnaround_minutes=60)
        explanation = res["unscheduled"][0]["explanation"]
        self.assertIn("min_turnaround",
                      explanation["blocking_constraint_codes"])
        window = explanation["candidate_windows"][0]
        rejection = next(r for r in window["rejections"]
                         if r["code"] == "min_turnaround")
        details = rejection["details"]
        self.assertEqual(details["min_turnaround_minutes"], 60.0)
        self.assertEqual(details["team_ids"], ["t0", "t1"])
        self.assertEqual(details["omitted_conflict_count"], 0)
        self.assertEqual(details["conflicts"], [
            {"conflict_end_time": PRIOR_END.isoformat(),
             "conflict_game_id": blocker,
             "conflict_source": "existing_game",
             "conflict_start_time": PRIOR_START.isoformat(),
             "gap_minutes": 0.0, "shortfall_minutes": 60.0,
             "team_id": "t0"},
            {"conflict_end_time": PRIOR_END.isoformat(),
             "conflict_game_id": blocker,
             "conflict_source": "existing_game",
             "conflict_start_time": PRIOR_START.isoformat(),
             "gap_minutes": 0.0, "shortfall_minutes": 60.0,
             "team_id": "t1"},
        ])

    def test_explanation_offers_a_turnaround_correction(self):
        self._hierarchy()
        self._prior_game()
        self._slot("slotB", PRIOR_END, PRIOR_END + timedelta(hours=1))
        res = self._draft(min_turnaround_minutes=60)
        alternatives = res["unscheduled"][0]["explanation"]["alternatives"]
        self.assertIn(
            {"action_code": "review_minimum_turnaround_policy",
             "reason_code": "min_turnaround",
             "min_turnaround_minutes": 60.0,
             "team_ids": ["t0", "t1"]},
            alternatives, repr(alternatives))

    def test_nested_conflict_rows_are_allowlisted_and_capped(self):
        """The nested row allowlist and the 4-row cap #379 applies to
        ``min_rest.conflicts`` apply identically here. Driven through the
        formatter directly, with a free-text field and nine rows planted,
        because the generator itself can never produce either."""
        from hockey_scheduler.services.schedule_explanations import (
            build_unplaced_explanation)
        raw = [{
            "ice_slot_id": "s", "rink_id": "r", "venue_id": "v",
            "start_time": "x", "end_time": "y",
            "rejections": [{
                "code": "min_turnaround",
                "details": {
                    "team_ids": ["t0"], "min_turnaround_minutes": 60,
                    "omitted_conflict_count": 5,
                    "conflicts": [{
                        "team_id": "t0", "conflict_source": "existing_game",
                        "conflict_game_id": "g1",
                        "conflict_start_time": "w", "conflict_end_time": "z",
                        "gap_minutes": 0, "shortfall_minutes": 60,
                        "team_name": "LEAK", "note": "LEAK",
                    }],
                },
            }],
        }]
        out = build_unplaced_explanation(
            pairing={"home_team_id": "t0", "away_team_id": "t1"},
            scope={}, legacy_reason_codes=["min_turnaround"],
            raw_candidates=raw, candidate_total=1)
        row = out["candidate_windows"][0]["rejections"][0]["details"][
            "conflicts"][0]
        self.assertNotIn("team_name", row)
        self.assertNotIn("note", row)
        self.assertEqual(set(row), {
            "team_id", "conflict_source", "conflict_game_id",
            "conflict_start_time", "conflict_end_time",
            "gap_minutes", "shortfall_minutes"})
        self.assertNotIn("LEAK", json.dumps(out))

    def test_conflicts_are_capped_at_four_with_an_omitted_count(self):
        """Same bound ``min_rest`` carries: at most four nested conflict
        rows, and the remainder is COUNTED rather than dropped silently."""
        self._hierarchy(n_teams=2)
        self._slot("slotA", PRIOR_START, PRIOR_END, IceSlotStatus.ALLOCATED)
        # Six prior games for the same two teams, each on its own rink so
        # they do not collide with one another, all ending 15:30.
        for i in range(6):
            self.store.add_rink(Rink(id=f"rx{i}", venue_id="v1",
                                     name=f"Extra {i}"))
            self.store.add_ice_slot(IceSlot(
                id=f"sx{i}", rink_id=f"rx{i}",
                start_time=PRIOR_START - _minutes(i),
                end_time=PRIOR_END - _minutes(i),
                status=IceSlotStatus.ALLOCATED))
            self.store.add_game(Game(
                id=f"gx{i}", home_team_id="t0", away_team_id="t1",
                start_time=PRIOR_START - _minutes(i),
                end_time=PRIOR_END - _minutes(i), ice_slot_id=f"sx{i}",
                division_id="div1", season_id="se1", league_id="lg1",
                league_season_id="ls1",
                game_type=GameType.EXHIBITION.value))
        self._slot("slotB", PRIOR_END, PRIOR_END + timedelta(hours=1))
        res = self._draft(min_turnaround_minutes=60)
        window = res["unscheduled"][0]["explanation"]["candidate_windows"][0]
        details = next(r for r in window["rejections"]
                       if r["code"] == "min_turnaround")["details"]
        self.assertEqual(len(details["conflicts"]), 4)
        self.assertEqual(details["omitted_conflict_count"], 8)

    def test_out_of_scope_game_ids_never_reach_the_payload(self):
        """#379's in-scope-game-id rule, held on the NEW code too.

        The active-game snapshot is deliberately unfiltered (#373) so the
        preview and the commit gate measure the same physical edges, which
        is exactly why a neighbouring Season sharing this Venue can put a
        foreign Game into the turnaround comparison. The collision is still
        reported; only the out-of-context id is withheld.
        """
        self._hierarchy()
        self.store.add_season(Season(id="se2", program_id="prog1",
                                     name="Other Season"))
        self.store.add_league_season(LeagueSeason(
            id="ls2", league_id="lg1", season_id="se2"))
        self._slot("slotA", PRIOR_START, PRIOR_END, IceSlotStatus.ALLOCATED)
        self.store.add_game(Game(
            id="g_foreign", home_team_id="t0", away_team_id="t1",
            start_time=PRIOR_START, end_time=PRIOR_END, ice_slot_id="slotA",
            season_id="se2", league_id="lg1", league_season_id="ls2",
            game_type=GameType.EXHIBITION.value))
        self._slot("slotB", PRIOR_END, PRIOR_END + timedelta(hours=1))
        res = self._draft(min_turnaround_minutes=60)
        row = res["unscheduled"][0]
        self.assertIn("min_turnaround", row["reason_codes"])
        window = row["explanation"]["candidate_windows"][0]
        details = next(r for r in window["rejections"]
                       if r["code"] == "min_turnaround")["details"]
        self.assertTrue(details["conflicts"], repr(details))
        for conflict in details["conflicts"]:
            self.assertNotIn("conflict_game_id", conflict)
        self.assertNotIn("g_foreign", json.dumps(row["explanation"]))
        # Held one notch TIGHTER than #379 requires: the row-level list is
        # withheld too, so the id cannot reach the response through the
        # structured sibling of ``team_conflicts`` either. The collision is
        # still reported — only the out-of-context id is withheld.
        self.assertTrue(row["turnaround_conflicts"], repr(row))
        for conflict in row["turnaround_conflicts"]:
            self.assertEqual(conflict["conflict_source"], "existing_game")
            self.assertIsNone(conflict["conflict_game_id"])
        self.assertNotIn("g_foreign", json.dumps(row))

    def test_explanation_is_deterministic(self):
        self._hierarchy()
        self._prior_game()
        self._slot("slotB", PRIOR_END, PRIOR_END + timedelta(hours=1))
        first = self._draft(min_turnaround_minutes=60)
        second = self._draft(min_turnaround_minutes=60)
        self.assertEqual(first, second)


class TurnaroundCommitTest(TurnaroundFixture, unittest.TestCase):
    """The IDENTICAL predicate at commit (#390 req 3).

    The preview and the commit gate call ONE function —
    ``scheduler.turnaround_conflicts`` — over the same union of persisted and
    same-batch spans. #382 shipped a commit guard that asked a DIFFERENT
    question from the preview and refused legitimate commits for a month;
    sharing the predicate is what makes that structurally impossible here.
    """

    def make_store(self):
        return InMemoryStore()

    def _game_counter(self):
        """The Game id counter's current value, read the only way the store
        interface allows — by consuming one. Callers compare DELTAS."""
        return int(self.store.next_id("game").rsplit("_", 1)[1])

    def _committable(self):
        """One pairing, one candidate slot 60 minutes clear of the prior
        game — a proposal the preview accepts and the commit must persist."""
        self._hierarchy()
        self._prior_game()
        later = PRIOR_END + _minutes(60)
        self._slot("slotB", later, later + timedelta(hours=1))

    def _commit_races(self, api_cls):
        """A racing Game lands after the preview was reviewed; the per-row
        gate refuses it with zero Game, slot, counter or audit effects.

        Technique is the repo's established one for every other per-row
        commit gate (``_pairing_race_wins_over_physical_conflict``): the
        winning Game exists DURABLY in the store before ``commit`` is
        called, and ``draft_season_schedule`` is frozen to the pre-race
        proposal so the wide ``draft_fingerprint`` gate — which is the
        FIRST line of defence and would otherwise classify this as
        ``preview_stale`` — sees the world the operator reviewed. What is
        left is exactly the window the per-row gate exists for.

        The racing Game is an EXHIBITION for the batch's own two teams, so
        ``pairing_already_scheduled`` (REGULAR fixtures only) cannot fire,
        and it does not OVERLAP the reviewed slot, so
        ``_assert_slot_free_for_game``'s ``team_overlap`` cannot fire
        either. Only the turnaround predicate can refuse it — which is what
        makes the "remove the commit-time check" mutation observable.
        """
        self._committable()
        api = api_cls(self.store)
        constraints = {"min_turnaround_minutes": 60}
        reviewed = api.draft_season_schedule("div1", constraints=constraints)
        self.assertEqual(len(reviewed["draft_games"]), 1, repr(reviewed))
        row = reviewed["draft_games"][0]

        # The race: a friendly booked on another rink, ending 30 minutes
        # before the reviewed game starts.
        reviewed_start = datetime.fromisoformat(row["start_time"])
        racer_end = reviewed_start - _minutes(30)
        self.store.add_rink(Rink(id="r2", venue_id="v1", name="Second"))
        self.store.add_ice_slot(IceSlot(
            id="slotRace", rink_id="r2", start_time=racer_end - timedelta(hours=1),
            end_time=racer_end, status=IceSlotStatus.ALLOCATED))
        self.store.add_game(Game(
            id="g_race", home_team_id="t0", away_team_id="t1",
            start_time=racer_end - timedelta(hours=1), end_time=racer_end,
            ice_slot_id="slotRace", division_id="div1", season_id="se1",
            league_id="lg1", league_season_id="ls1",
            game_type=GameType.EXHIBITION.value))

        games_before = len(self.store.all_games())
        audits_before = len(self.store.all_setup_audit())
        counter_before = self._game_counter()
        api.draft_season_schedule = lambda *a, **k: reviewed
        res = api.commit_draft_schedule(
            "div1", constraints=constraints,
            draft_fingerprint=reviewed["draft_fingerprint"])
        self.assertIn("error", res, repr(res))
        details = res["error"]["details"]
        self.assertEqual(details["reason"], "min_turnaround", repr(res))
        self.assertEqual(details["conflict_game_id"], "g_race", repr(res))
        self.assertEqual(details["gap_minutes"], 30.0, repr(res))
        self.assertEqual(details["shortfall_minutes"], 30.0, repr(res))
        self.assertIn("minimum turnaround", res["error"]["message"].lower())
        # Zero Game, slot, counter and audit effects.
        self.assertEqual(len(self.store.all_games()), games_before, repr(res))
        self.assertEqual(self.store.get_ice_slot("slotB").status,
                         IceSlotStatus.AVAILABLE)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)
        # ``next_id`` has no peek, so the claim is made as a DELTA: one
        # bump for the probe itself and no others. A refused row that had
        # already minted a Game id would show two.
        self.assertEqual(self._game_counter(), counter_before + 1, repr(res))

    def test_league_scoped_commit_refuses_the_turnaround_race(self):
        self._commit_races(ApiService)

    def test_base_facade_commit_refuses_the_turnaround_race(self):
        self._commit_races(BaseApiService)

    def _commit_accepts(self, api_cls):
        """THE ANTI-VACUITY CONTROL for the commit gate: the identical
        fixture with NO racing game commits normally. Without it, "the
        commit refused" is indistinguishable from "this commit path refuses
        everything once a turnaround is configured"."""
        self._committable()
        api = api_cls(self.store)
        res = commit_fresh_draft(
            api, "div1", constraints={"min_turnaround_minutes": 60})
        self.assertNotIn("error", res, repr(res))
        self.assertEqual(len(res["created"]), 1, repr(res))
        self.assertEqual(self.store.get_ice_slot("slotB").status,
                         IceSlotStatus.ALLOCATED)
        self.assertTrue(any(a.action == "draft_schedule_committed"
                            for a in self.store.all_setup_audit()))

    def test_league_scoped_commit_accepts_sufficient_turnaround(self):
        self._commit_accepts(ApiService)

    def test_base_facade_commit_accepts_sufficient_turnaround(self):
        self._commit_accepts(BaseApiService)

    def _commit_accepts_own_batch(self, api_cls):
        """A multi-row batch whose OWN rows sit a legal turnaround apart
        commits in full. The commit gate grows its occupancy exactly as the
        preview does, so a batch the preview accepted can never be refused
        by its own siblings — the #382 failure mode, ruled out directly."""
        self._hierarchy(n_teams=3)
        self._slot("slot1", PRIOR_START, PRIOR_END)
        self._slot("slot2", PRIOR_END + _minutes(60), PRIOR_END + _minutes(120))
        self._slot("slot3", PRIOR_START + timedelta(days=1),
                   PRIOR_END + timedelta(days=1))
        api = api_cls(self.store)
        res = commit_fresh_draft(
            api, "div1", constraints={"min_turnaround_minutes": 60})
        self.assertNotIn("error", res, repr(res))
        self.assertEqual(len(res["created"]), 3, repr(res))

    def test_league_scoped_commit_accepts_its_own_legal_batch(self):
        self._commit_accepts_own_batch(ApiService)

    def test_base_facade_commit_accepts_its_own_legal_batch(self):
        self._commit_accepts_own_batch(BaseApiService)


class TurnaroundHttpTest(unittest.TestCase):
    """The whole path over authenticated HTTP: the route accepts the
    constraint, the generator enforces it, and the refusal reaches the wire
    with its reason code and named blocking game."""

    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _fixture(self, c, gap_minutes):
        """A Division with two Teams, one prior 14:00-15:30 REGULAR game, and
        one free slot ``gap_minutes`` after it — built entirely through the
        real setup routes so every id is freshly sequential.

        The pair is asked for TWO meetings (#375), which is what leaves a
        round-robin obligation outstanding behind a REGULAR game that already
        satisfies the first one. A REGULAR fixture rather than an exhibition
        deliberately: an EXHIBITION has no owning League by design (#283
        Slice D), so it can never be inside #379's in-scope-game-id set and
        its id is correctly withheld from the explanation — which would make
        "the refusal names the blocking game" unassertable here for a reason
        that has nothing to do with the turnaround.
        """
        def post(path, body):
            status, resp = self._req(c, "POST", path, body)
            self.assertEqual(status, 200, repr(resp))
            return resp
        program = post("/api/setup/league", {"name": f"TA {gap_minutes}"})
        post("/api/context", {"program_id": program["id"]})
        season = post("/api/setup/season",
                      {"league_id": program["id"], "name": "TA Season"})
        level = post("/api/setup/level",
                     {"season_id": season["id"], "name": "TA Level"})
        division = post("/api/setup/division", {
            "season_id": season["id"], "level_id": level["id"],
            "name": "TA Division"})
        club = post("/api/setup/club", {"name": f"TA Club {gap_minutes}"})
        post("/api/context",
             {"program_id": program["id"], "season_id": season["id"]})
        teams = [post("/api/v2/setup/team",
                      {"club_id": club["id"], "league_id": level["id"],
                       "name": f"TA {name} {gap_minutes}"})
                 for name in ("Home", "Away")]
        for team in teams:
            post(f"/api/setup/seasons/{season['id']}/team-registrations",
                 {"team_id": team["id"], "division_id": division["id"]})
        venue = post("/api/setup/venue",
                     {"name": f"TA Arena {gap_minutes}",
                      "league_id": program["id"]})
        post(f"/api/v2/setup/seasons/{season['id']}/venue-access",
             {"venue_id": venue["id"]})
        rink = post("/api/setup/rink",
                    {"venue_id": venue["id"], "name": "TA Rink"})
        prior = post("/api/setup/ice-slot", {
            "rink_id": rink["id"], "start_time": PRIOR_START.isoformat(),
            "end_time": PRIOR_END.isoformat(), "slot_type": "game"})
        game = post("/api/v2/setup/game", {
            "season_id": season["id"], "division_id": division["id"],
            "league_id": level["id"],
            "home_team_id": teams[0]["id"], "away_team_id": teams[1]["id"],
            "ice_slot_id": prior["id"], "game_type": "regular"})
        free_start = PRIOR_END + _minutes(gap_minutes)
        post("/api/setup/ice-slot", {
            "rink_id": rink["id"], "start_time": free_start.isoformat(),
            "end_time": (free_start + timedelta(hours=1)).isoformat(),
            "slot_type": "game"})
        return division["id"], game.get("game_id") or game.get("id")

    def test_http_draft_refuses_a_back_to_back_start(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        division_id, game_id = self._fixture(c, 0)
        status, body = self._req(c, "POST", "/api/scheduler/draft", {
            "division_id": division_id, "meetings_per_opponent": 2,
            "constraints": {"min_turnaround_minutes": 60}})
        self.assertEqual(status, 200, repr(body))
        self.assertEqual(body["draft_games"], [], repr(body))
        self.assertEqual(len(body["unscheduled"]), 1, repr(body))
        row = body["unscheduled"][0]
        self.assertIn("min_turnaround", row["reason_codes"], repr(row))
        self.assertIn("minimum turnaround not met", row["reason"])
        self.assertEqual(
            {c_["conflict_game_id"] for c_ in row["turnaround_conflicts"]},
            {game_id}, repr(row))

    def test_http_draft_accepts_a_sufficient_turnaround(self):
        """THE ANTI-VACUITY CONTROL over HTTP."""
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        division_id, _ = self._fixture(c, 60)
        status, body = self._req(c, "POST", "/api/scheduler/draft", {
            "division_id": division_id, "meetings_per_opponent": 2,
            "constraints": {"min_turnaround_minutes": 60}})
        self.assertEqual(status, 200, repr(body))
        self.assertEqual(len(body["draft_games"]), 1, repr(body))
        self.assertEqual(body["unscheduled"], [], repr(body))

    def test_http_bad_turnaround_is_a_validation_error(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        division_id, _ = self._fixture(c, 60)
        status, body = self._req(c, "POST", "/api/scheduler/draft", {
            "division_id": division_id, "meetings_per_opponent": 2,
            "constraints": {"min_turnaround_minutes": -5}})
        self.assertEqual(body["error"]["code"], "validation_error", repr(body))

    def test_http_commit_round_trip_with_a_turnaround(self):
        """Generate then Commit carrying the SAME constraints — the shape
        the Scheduler UI must send. The constraint set is bound into
        ``draft_fingerprint``, so a Commit that dropped it would be refused
        as ``preview_stale``; this proves the round trip succeeds."""
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        division_id, _ = self._fixture(c, 60)
        constraints = {"min_turnaround_minutes": 60}
        status, preview = self._req(c, "POST", "/api/scheduler/draft", {
            "division_id": division_id, "meetings_per_opponent": 2,
            "constraints": constraints})
        self.assertEqual(status, 200, repr(preview))
        status, body = self._req(c, "POST", "/api/scheduler/commit", {
            "division_id": division_id, "meetings_per_opponent": 2,
            "constraints": constraints,
            "draft_fingerprint": preview["draft_fingerprint"]})
        self.assertEqual(status, 200, repr(body))
        self.assertEqual(len(body["created"]), 1, repr(body))


if __name__ == "__main__":
    unittest.main()
