"""A team is never proposed for two overlapping games (#373).

The scheduler preview used to place the same team in two games at the same
time on different rinks, report ``0 conflict(s)``, and leave "Commit as draft"
enabled. The commit gate refused the batch — so nothing impossible was ever
persisted — but an operator was shown, and invited to commit, a schedule that
could not physically happen.

These regressions pin BOTH halves of the corrected rule:

* the PREVIEW carries team-wide occupancy — from persisted games AND from the
  candidates this same batch has already accepted — keyed by stable team id
  and real interval, so an impossible row is never proposed and never
  silently counted as zero conflicts;
* the COMMIT gate stays the integrity boundary. It is revalidated inside the
  transaction that creates the games, so a forced/stale/racing commit request
  that bypasses the preview entirely is still refused atomically.

The matrix below runs on Memory, file-backed SQLite, and PostgreSQL: the
occupancy read is a real store scan, so "works in memory" is not evidence it
works on a database. Mutation proofs at the end delete each half of the fix in
turn and assert the suite would go red — a guard nothing exercises is not a
guard.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND, commit_fresh_draft  # noqa: F401  (BACKEND: sys.path)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Division, Game, IceSlot, IceSlotStatus, League, LeagueSeason, Organization,
    Program, Rink, Season, SeasonTeamRegistration, SeasonVenueAccess, Team,
    Venue)
from hockey_scheduler.services import scheduler as sched
from hockey_scheduler.services.setup_service import SetupService
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv

UTC = timezone.utc
# The issue's own scenario: a 17:00 face-off with ice free on two rinks.
BASE = datetime(2026, 11, 3, 17, tzinfo=UTC)

# Named exactly as issue #373 reports them, so a failure message reads like the
# bug report.
GOLD_1, GOLD_2, REDWINGS = "Gold team 1", "Gold team 2", "Redwings"


def _seed(store, team_names):
    """One League+Season+Division, three rinks, and ``team_names`` registered
    for the season. Returns the created team ids in order.

    Also creates ``tx`` ("Other Club") — a real Team deliberately left
    UNREGISTERED, so it never enters the round robin and can stand in as the
    opponent of a pre-existing game without changing which pairings the
    generator produces."""
    store.add_organization(Organization(id="org", name="Owner"))
    store.add_program(Program(id="pg", name="Program",
                              operator_organization_id="org"))
    store.add_season(Season(id="se", program_id="pg", name="2026-27"))
    store.add_league(League(id="lg", program_id="pg", name="Bronze"))
    store.add_league_season(LeagueSeason(id="ls", league_id="lg",
                                         season_id="se"))
    store.add_division(Division(id="d", league_season_id="ls", name="Bronze"))
    store.add_venue(Venue(id="v", name="Arena", organization_id="org",
                          league_id="pg"))
    store.add_season_venue_access(SeasonVenueAccess(
        id="sva", season_id="se", venue_id="v", active=True))
    # Ids are chosen so the slot sort key (start_time, id) is unambiguous.
    for rink_id, name in (("blue", "Blue Rink"), ("red", "Red Rink"),
                          ("zgreen", "Green Rink")):
        store.add_rink(Rink(id=rink_id, venue_id="v", name=name))
    team_ids = []
    for i, name in enumerate(team_names):
        tid = f"t{i}"
        store.add_team(Team(id=tid, name=name, division="Bronze",
                            division_id="d", program_id="pg", league_id="lg"))
        store.add_season_team_registration(SeasonTeamRegistration(
            id=f"reg{i}", league_season_id="ls", team_id=tid,
            division_id="d", active=True))
        team_ids.append(tid)
    store.add_team(Team(id="tx", name="Other Club", program_id="pg",
                        league_id="lg"))
    return team_ids


def _slot(store, slot_id, rink_id, start, minutes=60):
    store.add_ice_slot(IceSlot(id=slot_id, rink_id=rink_id, start_time=start,
                               end_time=start + timedelta(minutes=minutes)))
    return slot_id


def _persist_game(store, game_id, home, away, slot_id, **flags):
    """A real Game occupying ``slot_id``. ``flags`` sets the lifecycle fields
    (``is_draft``/``published``/``cancelled``) the occupancy rule must ignore:
    a booked sheet of ice is booked whatever the fixture's review state."""
    slot = store.get_ice_slot(slot_id)
    store.add_game(Game(
        id=game_id, home_team_id=home, away_team_id=away,
        start_time=slot.start_time, end_time=slot.end_time,
        ice_slot_id=slot_id, division_id="d", season_id="se", league_id="lg",
        league_season_id="ls", **flags))


def _codes(row):
    return set(row.get("reason_codes") or ())


def _rows_for(preview, home, away):
    return [r for r in preview["unscheduled"]
            if r["home_team_id"] == home and r["away_team_id"] == away]


class _TeamOverlapMixin:
    """Backend-parameterized preview + commit regressions for #373."""

    def _make_store(self):
        raise NotImplementedError

    def _api(self, team_names):
        store = self._make_store()
        ids = _seed(store, team_names)
        return ApiService(store), store, ids

    # -- shared invariants ------------------------------------------------
    def _assert_no_team_double_booked(self, rows):
        """No team appears in two overlapping proposed/persisted rows. Rows are
        ``(team_ids, start, end)`` triples."""
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                (teams_i, si, ei), (teams_j, sj, ej) = rows[i], rows[j]
                if si < ej and sj < ei:
                    self.assertFalse(
                        set(teams_i) & set(teams_j),
                        f"team double-booked across {rows[i]} and {rows[j]}")

    def _proposed_rows(self, preview):
        return [((g["home_team_id"], g["away_team_id"]),
                 datetime.fromisoformat(g["start_time"]),
                 datetime.fromisoformat(g["end_time"]))
                for g in preview["draft_games"]]

    def _persisted_rows(self, store):
        out = []
        for g in store.all_games():
            if g.cancelled or not g.ice_slot_id:
                continue
            slot = store.get_ice_slot(g.ice_slot_id)
            if slot is not None:
                out.append(((g.home_team_id, g.away_team_id),
                            slot.start_time, slot.end_time))
        return out

    # -- candidate vs candidate (the reported defect) ----------------------
    def test_preview_never_proposes_two_simultaneous_games_for_one_team(self):
        # Issue #373 verbatim: three teams, 17:00 ice free on two rinks. The
        # round robin's consecutive pairings share a team, so the greedy
        # earliest-slot walk used to hand the SAME team both 17:00 sheets and
        # call it "2 game(s), 0 conflict(s)".
        api, store, _ = self._api([GOLD_1, GOLD_2, REDWINGS])
        _slot(store, "red_17", "red", BASE)
        _slot(store, "blue_17", "blue", BASE)

        preview = api.draft_season_schedule("d")

        # Only one game can happen at 17:00 in a three-team division: any
        # second one must reuse a team. The impossible pair is gone...
        self.assertEqual(len(preview["draft_games"]), 1, preview)
        self._assert_no_team_double_booked(self._proposed_rows(preview))
        # ...and it is REPORTED, not silently dropped: "0 conflict(s)" was the
        # visible half of the bug.
        self.assertTrue(preview["unscheduled"], preview)
        blocked = [r for r in preview["unscheduled"]
                   if sched.TEAM_OVERLAP in _codes(r)]
        self.assertTrue(blocked, preview["unscheduled"])
        # The structured half: which team, and that the booking it collides
        # with is another row of THIS batch, not something already persisted.
        for row in blocked:
            self.assertTrue(row["team_conflicts"], row)
            for conflict in row["team_conflicts"]:
                self.assertIn(conflict["team_id"],
                              (row["home_team_id"], row["away_team_id"]))
                self.assertEqual(conflict["conflict_source"], "proposed_game")
                self.assertIsNone(conflict["conflict_game_id"])
                self.assertEqual(conflict["team_name"],
                                 store.get_team(conflict["team_id"]).name)
            # The prose an operator actually reads names the team too.
            self.assertIn(row["team_conflicts"][0]["team_name"], row["reason"])

    def test_candidate_occupancy_covers_every_home_away_permutation(self):
        # The circle method fixes each pairing's orientation, so drive
        # ``_assign_ice`` with explicit pairings instead — the only way to
        # control BOTH candidates' home/away roles and prove the in-batch
        # occupancy is symmetric in all four combinations, not just the two
        # the round robin happens to produce.
        for label, first, second in (
                ("home/home", ("t0", "t1"), ("t0", "t2")),
                ("home/away", ("t0", "t1"), ("t2", "t0")),
                ("away/home", ("t1", "t0"), ("t0", "t2")),
                ("away/away", ("t1", "t0"), ("t2", "t0"))):
            with self.subTest(permutation=label):
                _api, store, _ = self._api([GOLD_1, GOLD_2, REDWINGS])
                _slot(store, "red_17", "red", BASE)
                _slot(store, "blue_17", "blue", BASE)
                slots = sched._available_game_slots(store)
                draft_games, unscheduled = sched._assign_ice(
                    store,
                    [(first[0], first[1], "d"), (second[0], second[1], "d")],
                    slots, None,
                    team_spans=sched._persisted_team_spans(
                        sched._active_game_slot_pairs(store)))
                # The second candidate shares t0 with the first at the same
                # instant, so exactly one is placed however the roles fall.
                self.assertEqual(len(draft_games), 1, (label, draft_games))
                self.assertEqual(len(unscheduled), 1, (label, unscheduled))
                self.assertEqual(_codes(unscheduled[0]), {sched.TEAM_OVERLAP},
                                 (label, unscheduled[0]))
                self.assertEqual(
                    unscheduled[0]["team_conflicts"],
                    [{"team_id": "t0", "team_name": GOLD_1,
                      "conflict_source": "proposed_game",
                      "conflict_game_id": None}], (label, unscheduled[0]))

    def test_both_teams_of_a_pairing_are_reported_when_both_are_booked(self):
        # A candidate can collide on BOTH sides at once. The report must name
        # both teams — telling an operator only about the first one they would
        # have to free is a half-answer.
        api, store, _ = self._api(["A", "B", "C", "D"])
        for slot_id, rink in (("red_17", "red"), ("blue_17", "blue"),
                              ("zgreen_17", "zgreen")):
            _slot(store, slot_id, rink, BASE)

        preview = api.draft_season_schedule("d")

        self.assertEqual(len(preview["draft_games"]), 2, preview)
        self._assert_no_team_double_booked(self._proposed_rows(preview))
        both = [r for r in preview["unscheduled"]
                if len(r["team_conflicts"]) == 2]
        self.assertTrue(both, preview["unscheduled"])
        row = both[0]
        self.assertEqual({c["team_id"] for c in row["team_conflicts"]},
                         {row["home_team_id"], row["away_team_id"]}, row)
        self.assertIn("have an overlapping game", row["reason"])

    def test_partial_overlap_at_a_different_start_time_is_refused(self):
        # Interval overlap, not same-start-time equality: a 17:30 face-off is
        # just as impossible for a team already playing 17:00-18:00.
        api, store, _ = self._api([GOLD_1, GOLD_2, REDWINGS])
        _slot(store, "red_17", "red", BASE)
        _slot(store, "blue_1730", "blue", BASE + timedelta(minutes=30))

        preview = api.draft_season_schedule("d")

        self.assertEqual(len(preview["draft_games"]), 1, preview)
        self._assert_no_team_double_booked(self._proposed_rows(preview))
        self.assertTrue(
            [r for r in preview["unscheduled"]
             if sched.TEAM_OVERLAP in _codes(r)], preview["unscheduled"])

    def test_back_to_back_games_remain_valid(self):
        # Positive control for the half-open interval: a team whose game ends
        # at 18:00 may start another at exactly 18:00. Over-tightening the rule
        # to "same day" or "any adjacency" would break this.
        api, store, _ = self._api([GOLD_1, GOLD_2, REDWINGS])
        _slot(store, "red_17", "red", BASE)
        _slot(store, "blue_18", "blue", BASE + timedelta(hours=1))

        preview = api.draft_season_schedule("d")

        self.assertEqual(len(preview["draft_games"]), 2, preview)
        for row in preview["unscheduled"]:
            self.assertNotIn(sched.TEAM_OVERLAP, _codes(row), row)
        # The two games really are back-to-back appearances by ONE team —
        # otherwise this control would pass for the wrong reason.
        appearances = [t for g in preview["draft_games"]
                       for t in (g["home_team_id"], g["away_team_id"])]
        self.assertTrue([t for t in appearances if appearances.count(t) == 2],
                        preview["draft_games"])

    def test_four_distinct_teams_may_play_simultaneously_on_two_rinks(self):
        # Positive control for the other direction: simultaneous games on
        # different rinks are perfectly legal when no team is shared. The rule
        # is team-wide, not "one game per time slot".
        api, store, _ = self._api(["A", "B", "C", "D"])
        _slot(store, "red_17", "red", BASE)
        _slot(store, "blue_17", "blue", BASE)

        preview = api.draft_season_schedule("d")

        self.assertEqual(len(preview["draft_games"]), 2, preview)
        starts = {g["start_time"] for g in preview["draft_games"]}
        rinks = {g["rink_id"] for g in preview["draft_games"]}
        teams = {t for g in preview["draft_games"]
                 for t in (g["home_team_id"], g["away_team_id"])}
        self.assertEqual(len(starts), 1, preview["draft_games"])
        self.assertEqual(len(rinks), 2, preview["draft_games"])
        self.assertEqual(len(teams), 4, preview["draft_games"])
        for row in preview["unscheduled"]:
            self.assertNotIn(sched.TEAM_OVERLAP, _codes(row), row)

    # -- candidate vs already-persisted games ------------------------------
    def _persisted_conflict_preview(self, *, existing_home, existing_away,
                                    **flags):
        """One 2-team division (its single pairing is home=t0, away=t1), one
        free 17:00 slot on red, and a pre-existing game on blue at 17:00
        between ``existing_home``/``existing_away``."""
        api, store, (t0, t1) = self._api([GOLD_1, GOLD_2])
        _slot(store, "red_17", "red", BASE)
        _slot(store, "blue_17", "blue", BASE)
        _persist_game(store, "existing", existing_home, existing_away,
                      "blue_17", **flags)
        return api, store, api.draft_season_schedule("d")

    def test_round_robin_pairing_orientation_is_the_one_assumed_below(self):
        # The home/away permutation matrix below is only meaningful if the two
        # -team round robin really does propose home=t0, away=t1. Pin it, so a
        # change to the circle method fails HERE with a clear message instead
        # of quietly turning four permutations into two.
        self.assertEqual(sched.round_robin_pairings(["t0", "t1"]),
                         [("t0", "t1")])

    def test_persisted_game_blocks_every_home_away_permutation(self):
        # The conflict is "this roster is already on the ice", which has no
        # home/away asymmetry. All four permutations of the shared team's role
        # in the candidate vs in the existing game must refuse identically.
        for label, existing_home, existing_away, shared in (
                ("home/home", "t0", "tx", "t0"),
                ("home/away", "tx", "t0", "t0"),
                ("away/home", "t1", "tx", "t1"),
                ("away/away", "tx", "t1", "t1")):
            with self.subTest(permutation=label):
                api, store, preview = self._persisted_conflict_preview(
                    existing_home=existing_home, existing_away=existing_away)
                self.assertEqual(preview["draft_games"], [], label)
                rows = _rows_for(preview, "t0", "t1")
                self.assertEqual(len(rows), 1, preview["unscheduled"])
                self.assertIn(sched.TEAM_OVERLAP, _codes(rows[0]), rows[0])
                self.assertEqual(
                    rows[0]["team_conflicts"],
                    [{"team_id": shared,
                      "team_name": store.get_team(shared).name,
                      "conflict_source": "existing_game",
                      "conflict_game_id": "existing"}], rows[0])

    def test_persisted_draft_game_blocks_a_candidate(self):
        # An unpublished draft fixture still occupies the team. Committing a
        # second batch against it would produce an impossible schedule the
        # moment both were published.
        _api, _store, preview = self._persisted_conflict_preview(
            existing_home="t0", existing_away="tx",
            is_draft=True, published=False)
        self.assertEqual(preview["draft_games"], [], preview)
        self.assertIn(sched.TEAM_OVERLAP,
                      _codes(_rows_for(preview, "t0", "t1")[0]))

    def test_persisted_published_game_blocks_a_candidate(self):
        _api, _store, preview = self._persisted_conflict_preview(
            existing_home="t0", existing_away="tx",
            is_draft=False, published=True)
        self.assertEqual(preview["draft_games"], [], preview)
        self.assertIn(sched.TEAM_OVERLAP,
                      _codes(_rows_for(preview, "t0", "t1")[0]))

    def test_cancelled_persisted_game_does_not_block_a_candidate(self):
        # Positive control, and parity with the commit gate: a CANCELLED game
        # holds no ice, so blocking on it would refuse schedules the gate would
        # happily accept.
        _api, _store, preview = self._persisted_conflict_preview(
            existing_home="t0", existing_away="tx", cancelled=True)
        self.assertEqual(len(preview["draft_games"]), 1, preview)
        for row in preview["unscheduled"]:
            self.assertNotIn(sched.TEAM_OVERLAP, _codes(row), row)

    # -- commit: the integrity boundary ------------------------------------
    def test_commit_persists_only_the_possible_subset(self):
        # End to end on the reported scenario: what the corrected preview
        # offers is exactly what commits, and the persisted schedule contains
        # no team in two places at once.
        api, store, _ = self._api([GOLD_1, GOLD_2, REDWINGS])
        _slot(store, "red_17", "red", BASE)
        _slot(store, "blue_17", "blue", BASE)

        result = commit_fresh_draft(api, "d")

        self.assertNotIn("error", result, result)
        self.assertEqual(len(result["created"]), 1, result)
        self._assert_no_team_double_booked(self._persisted_rows(store))

    def test_direct_commit_of_an_impossible_batch_is_refused_atomically(self):
        # The acceptance criterion covers "direct commit requests", not just
        # the UI path. Hold a proposal that predates the fix's occupancy rule
        # (forced through by pinning the regeneration) and prove the gate — not
        # the preview — is what makes the impossible schedule unreachable.
        api, store, (g1, g2, rw) = self._api([GOLD_1, GOLD_2, REDWINGS])
        _slot(store, "red_17", "red", BASE)
        _slot(store, "blue_17", "blue", BASE)
        proposal = api.draft_season_schedule("d")
        row = proposal["draft_games"][0]
        # Re-add the row the fix now (correctly) refuses to propose: the same
        # team, the same instant, the other rink.
        shared = row["home_team_id"]
        other = next(t for t in (g1, g2, rw)
                     if t not in (row["home_team_id"], row["away_team_id"]))
        impossible = dict(row)
        impossible.update({
            "home_team_id": shared, "away_team_id": other,
            "home_team_name": store.get_team(shared).name,
            "away_team_name": store.get_team(other).name,
            "ice_slot_id": "red_17" if row["ice_slot_id"] != "red_17"
                           else "blue_17",
            "rink_id": "red" if row["rink_id"] != "red" else "blue",
        })
        proposal["draft_games"] = [row, impossible]
        api.draft_season_schedule = lambda *a, **k: proposal
        audit_before = len(store.all_setup_audit())

        result = api.commit_draft_schedule(
            division_id="d",
            draft_fingerprint=proposal["draft_fingerprint"])

        self.assertIn("error", result, result)
        self.assertEqual(result["error"]["details"]["reason"], "team_overlap")
        # Atomic: not even the first, individually-valid row survives, and no
        # batch audit landed.
        self.assertEqual(store.all_games(), [])
        self.assertEqual(len(store.all_setup_audit()), audit_before)
        for slot_id in ("red_17", "blue_17"):
            self.assertEqual(store.get_ice_slot(slot_id).status,
                             IceSlotStatus.AVAILABLE, slot_id)

    def test_stale_preview_loses_to_a_game_committed_first(self):
        # The race the issue calls out: an operator reviews a preview, another
        # session commits a game for one of its teams, and the stale batch is
        # then submitted. It must refuse as one unit — no partial games, no
        # audit rows — never persist "most of" an impossible schedule.
        api, store, _ = self._api(["A", "B", "C", "D"])
        for slot_id, rink in (("red_17", "red"), ("blue_17", "blue"),
                              ("zgreen_17", "zgreen")):
            _slot(store, slot_id, rink, BASE)
        _slot(store, "red_19", "red", BASE + timedelta(hours=2))
        preview = api.draft_season_schedule("d")
        self.assertGreaterEqual(len(preview["draft_games"]), 2, preview)
        # Another session wins the race: it books two of the reviewed batch's
        # teams onto the still-free third rink, at a time the batch wants them
        # elsewhere. Written through the real placement path, exactly as a
        # concurrent operator would.
        raced = preview["draft_games"][0]
        opponent = next(
            t for row in preview["draft_games"]
            for t in (row["home_team_id"], row["away_team_id"])
            if t not in (raced["home_team_id"], raced["away_team_id"]))
        winner = api.create_game("se", "d", raced["home_team_id"], opponent,
                                 "zgreen_17", league_id="lg")
        self.assertNotIn("error", winner, winner)
        games_before = len(store.all_games())
        audit_before = len(store.all_setup_audit())

        result = api.commit_draft_schedule(
            division_id="d", draft_fingerprint=preview["draft_fingerprint"])

        self.assertIn("error", result, result)
        # The reviewed batch is no longer the batch that would be written, so
        # the preview binding refuses it before any row is attempted.
        self.assertEqual(result["error"]["details"]["reason"], "preview_stale")
        self.assertEqual(len(store.all_games()), games_before)
        self.assertEqual(len(store.all_setup_audit()), audit_before)
        self._assert_no_team_double_booked(self._persisted_rows(store))
        # ...and a fresh preview routes around the winner rather than
        # re-proposing rows that overlap it.
        fresh = api.draft_season_schedule("d")
        self._assert_no_team_double_booked(
            self._proposed_rows(fresh) + self._persisted_rows(store))

    # -- mutation proofs ---------------------------------------------------
    def test_mutation_dropping_candidate_occupancy_reopens_the_defect(self):
        # Delete ONLY the candidate-to-candidate half (persisted occupancy
        # still consulted) and the reported bug returns verbatim: two 17:00
        # games on different rinks sharing a team, reported as zero conflicts.
        api, store, _ = self._api([GOLD_1, GOLD_2, REDWINGS])
        _slot(store, "red_17", "red", BASE)
        _slot(store, "blue_17", "blue", BASE)
        original = sched._team_overlap_reason

        def persisted_only(store_, slot, home, away, team_spans):
            pruned = {tid: [s for s in spans if s[2] is not None]
                      for tid, spans in team_spans.items()}
            return original(store_, slot, home, away, pruned)

        sched._team_overlap_reason = persisted_only
        try:
            preview = api.draft_season_schedule("d")
        finally:
            sched._team_overlap_reason = original
        self.assertEqual(len(preview["draft_games"]), 2, preview)
        with self.assertRaises(AssertionError):
            self._assert_no_team_double_booked(self._proposed_rows(preview))

    def test_mutation_dropping_persisted_occupancy_reopens_the_defect(self):
        # Delete ONLY the persisted half and a candidate happily lands on top
        # of a real, already-booked fixture.
        api, store, _ = self._api([GOLD_1, GOLD_2])
        _slot(store, "red_17", "red", BASE)
        _slot(store, "blue_17", "blue", BASE)
        _persist_game(store, "existing", "t0", "tx", "blue_17")
        original = sched._persisted_team_spans
        sched._persisted_team_spans = lambda pairs: {}
        try:
            preview = api.draft_season_schedule("d")
        finally:
            sched._persisted_team_spans = original
        self.assertEqual(len(preview["draft_games"]), 1, preview)
        with self.assertRaises(AssertionError):
            self._assert_no_team_double_booked(
                self._proposed_rows(preview) + self._persisted_rows(store))

    def test_mutation_dropping_commit_revalidation_persists_the_impossible(self):
        # Reduce the commit gate to its PHYSICAL half (slot freedom, policy) —
        # i.e. remove the team revalidation — and an impossible batch is
        # actually written. This is why the preview is not, and must not
        # become, the integrity boundary.
        api, store, (g1, g2, rw) = self._api([GOLD_1, GOLD_2, REDWINGS])
        _slot(store, "red_17", "red", BASE)
        _slot(store, "blue_17", "blue", BASE)
        proposal = api.draft_season_schedule("d")
        row = proposal["draft_games"][0]
        shared = row["home_team_id"]
        other = next(t for t in (g1, g2, rw)
                     if t not in (row["home_team_id"], row["away_team_id"]))
        impossible = dict(row)
        impossible.update({
            "home_team_id": shared, "away_team_id": other,
            "home_team_name": store.get_team(shared).name,
            "away_team_name": store.get_team(other).name,
            "ice_slot_id": "red_17" if row["ice_slot_id"] != "red_17"
                           else "blue_17",
            "rink_id": "red" if row["rink_id"] != "red" else "blue",
        })
        proposal["draft_games"] = [row, impossible]
        api.draft_season_schedule = lambda *a, **k: proposal
        original = SetupService._assert_slot_free_for_game

        def physical_only(self_, ice_slot_id, home_team_id, away_team_id,
                          *, season_id=None, exclude_game_id=None):
            return self_._assert_slot_free(ice_slot_id, season_id=season_id,
                                           exclude_game_id=exclude_game_id)

        SetupService._assert_slot_free_for_game = physical_only
        try:
            result = api.commit_draft_schedule(
                division_id="d",
                draft_fingerprint=proposal["draft_fingerprint"])
        finally:
            SetupService._assert_slot_free_for_game = original
        self.assertNotIn("error", result, result)
        with self.assertRaises(AssertionError):
            self._assert_no_team_double_booked(self._persisted_rows(store))


class MemorySchedulerTeamOverlapTest(_TeamOverlapMixin, unittest.TestCase):
    def _make_store(self):
        return InMemoryStore()


class SqliteFileSchedulerTeamOverlapTest(_TeamOverlapMixin, unittest.TestCase):
    """File-backed, not ``:memory:`` — the occupancy scan reads through real
    tables and a real connection, exactly as production SQLite does."""

    def setUp(self):
        self._stores = []
        self._paths = []

    def tearDown(self):
        for store in self._stores:
            try:
                store.close()
            except Exception:
                pass
        for path in self._paths:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _make_store(self):
        # A fresh database file per store: several tests build more than one
        # world in a single test method, and re-seeding into the same file
        # would collide on the fixture's fixed ids rather than exercise the
        # scheduler.
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._paths.append(path)
        store = SqlStore(path)
        self._stores.append(store)
        return store


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresSchedulerTeamOverlapTest(_TeamOverlapMixin, unittest.TestCase):
    def setUp(self):
        self._stores = []

    def tearDown(self):
        for store in self._stores:
            try:
                store.close()
            except Exception:
                pass

    def _make_store(self):
        store = SqlStore(os.environ["TEST_DATABASE_URL"])
        store.clear_all_data()
        self._stores.append(store)
        return store


class SchedulerTeamOverlapHttpTest(unittest.TestCase):
    """The same rule over the authenticated HTTP surface the browser uses
    (#373): the preview endpoint must report the team conflict rather than
    ``0 conflict(s)``, and the commit endpoint must refuse an impossible batch
    without persisting anything."""

    @classmethod
    def setUpClass(cls):
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
        srv.STATE.reset(seed=False)

    def setUp(self):
        # Per test, not per class: one of these tests commits, and a store left
        # carrying that game would silently turn the next test's pairing into
        # an already-scheduled one. Clean slate (no demo fixtures) so the #373
        # scenario is the only thing in the store; it still seeds the admin
        # persona, so the endpoints stay genuinely authenticated, not open.
        srv.STATE.reset(seed=False)
        store = srv.STATE.api.store
        _seed(store, [GOLD_1, GOLD_2, REDWINGS])
        _slot(store, "red_17", "red", BASE)
        _slot(store, "blue_17", "blue", BASE)

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

    def _admin(self):
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        self._req(opener, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        return opener

    def test_preview_reports_the_team_conflict_over_http(self):
        c = self._admin()
        status, preview = self._req(c, "POST", "/api/scheduler/draft",
                                    {"division_id": "d"})
        self.assertEqual(status, 200, preview)
        # The header the operator reads is rendered from these two lists: one
        # committable game, and conflicts that are NOT zero.
        self.assertEqual(len(preview["draft_games"]), 1, preview)
        self.assertNotEqual(len(preview["unscheduled"]), 0, preview)
        blocked = [r for r in preview["unscheduled"]
                   if "team_overlap" in (r.get("reason_codes") or ())]
        self.assertTrue(blocked, preview["unscheduled"])
        # The structured conflict survives JSON serialization intact — a UI or
        # downstream automation can act on it without parsing prose.
        self.assertTrue(blocked[0]["team_conflicts"], blocked[0])
        self.assertIn("team_name", blocked[0]["team_conflicts"][0])
        self.assertIn("conflict_source", blocked[0]["team_conflicts"][0])

    def test_committed_schedule_is_physically_possible_over_http(self):
        c = self._admin()
        _, preview = self._req(c, "POST", "/api/scheduler/draft",
                               {"division_id": "d"})
        status, res = self._req(
            c, "POST", "/api/scheduler/commit",
            {"division_id": "d",
             "draft_fingerprint": preview["draft_fingerprint"]})
        self.assertEqual(status, 200, res)
        self.assertEqual(len(res["created"]), 1, res)
        store = srv.STATE.api.store
        active = [g for g in store.all_games() if not g.cancelled]
        for i in range(len(active)):
            for j in range(i + 1, len(active)):
                si = store.get_ice_slot(active[i].ice_slot_id)
                sj = store.get_ice_slot(active[j].ice_slot_id)
                if si.start_time < sj.end_time and sj.start_time < si.end_time:
                    self.assertFalse(
                        {active[i].home_team_id, active[i].away_team_id}
                        & {active[j].home_team_id, active[j].away_team_id},
                        f"{active[i]} overlaps {active[j]}")


if __name__ == "__main__":
    unittest.main()
