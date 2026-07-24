"""Season scheduler engine v1 (#84), extended #233 Slice G.

Deterministic single round-robin pairings assigned to the earliest available
game ice slots. Produces a draft proposal only — nothing is persisted or
published, except via the explicit commit step, which stamps every created
Game with its resolved season/league/division scope. Slice G adds a
League-wide entry point (Season + League, optional Division), season-wide
blackout/holiday constraints, and structured unschedulable reason codes.
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

from hockey_scheduler.domain import (
    Division,
    Game,
    GameType,
    IceSlot,
    IceSlotStatus,
    IceSlotType,
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
from hockey_scheduler.domain.errors import ScheduleConflictError
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.services import (
    draft_schedule,
    draft_schedule_for_league,
    round_robin_pairings,
)
from hockey_scheduler.api import ApiService
from hockey_scheduler.api.service import ApiService as BaseApiService
from hockey_scheduler.web import server as srv

UTC = timezone.utc
BASE_TIME = datetime(2026, 5, 4, 18, tzinfo=UTC)  # a Monday


class RoundRobinTest(unittest.TestCase):
    def test_even_team_count_full_round_robin(self):
        pairs = round_robin_pairings(["a", "b", "c", "d"])
        self.assertEqual(len(pairs), 6)  # C(4,2)
        matchups = {frozenset(p) for p in pairs}
        self.assertEqual(len(matchups), 6)  # each pair exactly once

    def test_odd_team_count_has_byes(self):
        pairs = round_robin_pairings(["a", "b", "c", "d", "e"])
        self.assertEqual(len(pairs), 10)  # C(5,2)
        # Every team plays 4 games (one bye round each).
        counts = {}
        for h, a in pairs:
            counts[h] = counts.get(h, 0) + 1
            counts[a] = counts.get(a, 0) + 1
        self.assertTrue(all(c == 4 for c in counts.values()))

    def test_deterministic_regardless_of_input_order(self):
        self.assertEqual(round_robin_pairings(["d", "c", "b", "a"]),
                         round_robin_pairings(["a", "b", "c", "d"]))

    def test_degenerate_counts(self):
        self.assertEqual(round_robin_pairings([]), [])
        self.assertEqual(round_robin_pairings(["a"]), [])
        self.assertEqual(round_robin_pairings(["a", "b"]), [("a", "b")])


class SchedulerContract:
    """Shared fixtures + tests, run against Memory and a durable SQL backend."""

    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()
        self.api = ApiService(self.store)

    def tearDown(self):
        conn = getattr(self.store, "conn", None)
        if conn is not None:
            conn.close()

    # -- fixture builders -------------------------------------------------
    def _base(self):
        """Organization/Program/Season/Venue/rink shared by every fixture."""
        self.store.add_organization(Organization(id="org1", name="Owner"))
        self.store.add_program(Program(
            id="prog1", name="Program", operator_organization_id="org1"))
        self.store.add_season(Season(id="se1", program_id="prog1", name="Season"))
        self.store.add_venue(Venue(id="v1", name="Arena", organization_id="org1"))
        self.store.add_season_venue_access(SeasonVenueAccess(
            id="sva1", season_id="se1", venue_id="v1", active=True))
        self.store.add_rink(Rink(id="r1", venue_id="v1", name="Main"))

    def _slots(self, n=6, rink_id="r1", start=BASE_TIME):
        ids = []
        for i in range(n):
            slot_id = f"slot_{rink_id}_{i}_{start.isoformat()}"
            self.store.add_ice_slot(IceSlot(
                id=slot_id, rink_id=rink_id,
                start_time=start + timedelta(days=i),
                end_time=start + timedelta(days=i, hours=1)))
            ids.append(slot_id)
        return ids

    def _division_fixture(self, n_teams=4, n_slots=6):
        """One League, one Division, N teams registered in it — mirrors the
        real canonical shape (League required, Division optional) rather than
        leaving ``league_id`` unset (#233 Slice G: a stale fixture omitting it
        would never exercise the Game.league_id stamping fix below)."""
        self._base()
        self.store.add_league(League(id="lg1", program_id="prog1", name="League"))
        self.store.add_league_season(LeagueSeason(
            id="ls_lg1_se1", league_id="lg1", season_id="se1"))
        self.store.add_division(Division(
            id="div1", league_season_id="ls_lg1_se1", name="D1"))
        for i in range(n_teams):
            self.store.add_team(Team(id=f"t{i}", name=f"Team {i}",
                                     program_id="prog1", league_id="lg1"))
            self.store.add_season_team_registration(SeasonTeamRegistration(
                id=f"streg_t{i}", league_season_id="ls_lg1_se1", team_id=f"t{i}",
                division_id="div1", active=True))
        self._slots(n_slots)

    def _league_two_divisions_fixture(self, per_division=2, n_slots=8):
        """One League with two Divisions (Gold/Silver), each with its own
        teams — for asserting a league-wide draft never pairs across
        Divisions (#233 Slice G)."""
        self._base()
        self.store.add_league(League(id="lg1", program_id="prog1", name="League"))
        self.store.add_league_season(LeagueSeason(
            id="ls_lg1_se1", league_id="lg1", season_id="se1"))
        self.store.add_division(Division(
            id="gold", league_season_id="ls_lg1_se1", name="Gold"))
        self.store.add_division(Division(
            id="silver", league_season_id="ls_lg1_se1", name="Silver"))
        for i in range(per_division):
            self.store.add_team(Team(id=f"g{i}", name=f"Gold {i}",
                                     program_id="prog1", league_id="lg1"))
            self.store.add_season_team_registration(SeasonTeamRegistration(
                id=f"streg_g{i}", league_season_id="ls_lg1_se1", team_id=f"g{i}",
                division_id="gold", active=True))
            self.store.add_team(Team(id=f"s{i}", name=f"Silver {i}",
                                     program_id="prog1", league_id="lg1"))
            self.store.add_season_team_registration(SeasonTeamRegistration(
                id=f"streg_s{i}", league_season_id="ls_lg1_se1", team_id=f"s{i}",
                division_id="silver", active=True))
        self._slots(n_slots)

    def _two_leagues_fixture(self, n_slots=8):
        """Two Leagues in the same Season, each with its own Division — for
        asserting a league-wide draft rejects/excludes any Division reference
        that crosses League boundaries (#233 Slice G review)."""
        self._base()
        self.store.add_league(League(id="lg1", program_id="prog1", name="League One"))
        self.store.add_league(League(id="lg2", program_id="prog1", name="League Two"))
        self.store.add_league_season(LeagueSeason(
            id="ls_lg1_se1", league_id="lg1", season_id="se1"))
        self.store.add_league_season(LeagueSeason(
            id="ls_lg2_se1", league_id="lg2", season_id="se1"))
        self.store.add_division(Division(
            id="div1", league_season_id="ls_lg1_se1", name="D1"))
        self.store.add_division(Division(
            id="div2", league_season_id="ls_lg2_se1", name="D2"))
        for i in range(2):
            self.store.add_team(Team(id=f"t{i}", name=f"Team {i}",
                                     program_id="prog1", league_id="lg1"))
            self.store.add_season_team_registration(SeasonTeamRegistration(
                id=f"streg_t{i}", league_season_id="ls_lg1_se1", team_id=f"t{i}",
                division_id="div1", active=True))
        self._slots(n_slots)

    # -- division-only entry point (#84/#85, unchanged behavior) -----------
    def test_assigns_earliest_slots_without_reuse(self):
        self._division_fixture(4, 6)
        res = draft_schedule(self.store, "div1")
        self.assertEqual(len(res["draft_games"]), 6)
        self.assertEqual(res["unscheduled"], [])
        used = [d["ice_slot_id"] for d in res["draft_games"]]
        self.assertEqual(len(used), len(set(used)))  # no slot reuse
        self.assertEqual(used, sorted(used))  # earliest-first

    def test_too_few_slots_yields_unscheduled_with_reason(self):
        self._division_fixture(4, 4)  # 6 pairings, only 4 slots
        res = draft_schedule(self.store, "div1")
        self.assertEqual(len(res["draft_games"]), 4)
        self.assertEqual(len(res["unscheduled"]), 2)
        self.assertIn("reason", res["unscheduled"][0])
        self.assertEqual(res["unscheduled"][0]["reason_codes"],
                         ["no_ice_available"])

    def test_does_not_persist_or_publish_any_game(self):
        self._division_fixture(4, 6)
        draft_schedule(self.store, "div1")
        self.assertEqual(self.store.all_games(), [])  # nothing created

    def test_skips_non_available_and_non_game_slots(self):
        self._division_fixture(2, 0)
        base = datetime(2026, 2, 1, 18, tzinfo=UTC)
        self.store.add_ice_slot(IceSlot(
            id="prac", rink_id="r1", start_time=base,
            end_time=base + timedelta(hours=1), slot_type=IceSlotType.PRACTICE))
        self.store.add_ice_slot(IceSlot(
            id="blocked", rink_id="r1", start_time=base + timedelta(days=1),
            end_time=base + timedelta(days=1, hours=1),
            status=IceSlotStatus.BLOCKED))
        res = draft_schedule(self.store, "div1")
        # One pairing, but no usable slot -> unscheduled.
        self.assertEqual(res["draft_games"], [])
        self.assertEqual(len(res["unscheduled"]), 1)

    def test_deterministic_output(self):
        self._division_fixture(4, 6)
        a = draft_schedule(self.store, "div1")
        b = draft_schedule(self.store, "div1")
        self.assertEqual(a, b)

    # -- Game.league_id/season_id stamping on commit (#233 Slice G) --------
    def test_commit_stamps_season_and_league_id_on_created_games(self):
        # Previously commit_draft_schedule never set season_id/league_id at
        # all on a committed draft game — silently defeating the stranding
        # guards in assign_season_team_league/move_division_to_league for
        # every scheduler-created game. Game.league_id is the CANONICAL
        # grouping League (store.get_league) — resolved here from the
        # Division directly, not from the response's own "league_id" (that
        # key is this tenancy layer's separate, pre-existing Program-scoped
        # vocabulary; see the implementation's own comment for why).
        self._division_fixture(4, 6)
        result = commit_fresh_draft(
            self.api, division_id="div1", actor_id="admin")
        self.assertNotIn("error", result)
        self.assertEqual(result["season_id"], "se1")
        games = self.store.all_games()
        self.assertEqual(len(games), 6)
        for g in games:
            self.assertEqual(g.season_id, "se1")
            self.assertEqual(g.league_id, "lg1")
            self.assertEqual(g.division_id, "div1")

    # -- new constraint inputs (#233 Slice G) -------------------------------
    def test_season_blackout_date_blocks_every_game_that_day(self):
        self._division_fixture(2, 2)  # 1 pairing, 2 candidate days
        blocked_day = BASE_TIME.date().isoformat()
        res = draft_schedule(self.store, "div1", constraints={
            "season_blackout_dates": [blocked_day]})
        self.assertEqual(len(res["draft_games"]), 1)
        self.assertNotEqual(
            res["draft_games"][0]["start_time"][:10], blocked_day)

    def test_holiday_date_blocks_every_game_that_day(self):
        self._division_fixture(2, 2)
        blocked_day = BASE_TIME.date().isoformat()
        res = draft_schedule(self.store, "div1", constraints={
            "holiday_dates": [blocked_day]})
        self.assertEqual(len(res["draft_games"]), 1)
        self.assertNotEqual(
            res["draft_games"][0]["start_time"][:10], blocked_day)

    def test_season_blackout_and_holiday_are_distinct_reason_codes(self):
        self._division_fixture(2, 1)  # 1 pairing, exactly 1 slot
        day = BASE_TIME.date().isoformat()
        blackout = draft_schedule(self.store, "div1", constraints={
            "season_blackout_dates": [day]})
        self.assertEqual(blackout["unscheduled"][0]["reason_codes"],
                         ["season_blackout"])
        holiday = draft_schedule(self.store, "div1", constraints={
            "holiday_dates": [day]})
        self.assertEqual(holiday["unscheduled"][0]["reason_codes"],
                         ["holiday"])

    def test_invalid_holiday_date_format_rejected(self):
        self._division_fixture(2, 2)
        with self.assertRaises(Exception):
            draft_schedule(self.store, "div1", constraints={
                "holiday_dates": ["05/04/2026"]})

    # -- structured reason codes + per-team rollup (#233 Slice G) -----------
    def test_reason_codes_cover_every_existing_constraint(self):
        self._division_fixture(2, 1)
        day = BASE_TIME.date().isoformat()
        team_res = draft_schedule(self.store, "div1", constraints={
            "team_blackouts": {"t0": [day]}})
        self.assertEqual(team_res["unscheduled"][0]["reason_codes"],
                         ["team_blackout"])
        rink_res = draft_schedule(self.store, "div1", constraints={
            "rink_blackouts": {"r1": [day]}})
        self.assertEqual(rink_res["unscheduled"][0]["reason_codes"],
                         ["rink_blackout"])

    def test_unschedulable_teams_rollup_flags_fully_blocked_team(self):
        self._division_fixture(4, 6)
        all_days = [(BASE_TIME + timedelta(days=i)).date().isoformat()
                   for i in range(6)]
        res = draft_schedule(self.store, "div1", constraints={
            "team_blackouts": {"t0": all_days}})
        rollup = {row["team_id"]: row for row in res["unschedulable_teams"]}
        self.assertIn("t0", rollup)
        self.assertEqual(rollup["t0"]["reason_codes"], ["team_blackout"])
        self.assertEqual(rollup["t0"]["team_name"], "Team 0")
        # t1/t2/t3 still get SOME games against each other, so they must not
        # be flagged as fully blocked just because one of their pairings
        # (against t0) failed.
        self.assertNotIn("t1", rollup)

    def test_unschedulable_teams_empty_when_everything_scheduled(self):
        self._division_fixture(4, 6)
        res = draft_schedule(self.store, "div1")
        self.assertEqual(res["unschedulable_teams"], [])

    # -- League-wide entry point (#233 Slice G) -----------------------------
    def test_league_wide_draft_never_pairs_across_divisions(self):
        self._league_two_divisions_fixture(per_division=2, n_slots=8)
        res = draft_schedule_for_league(self.store, "se1", "lg1")
        self.assertEqual(res["team_count"], 4)
        self.assertEqual(len(res["draft_games"]), 2)  # 1 Gold + 1 Silver pairing
        for g in res["draft_games"]:
            home_is_gold = g["home_team_id"].startswith("g")
            away_is_gold = g["away_team_id"].startswith("g")
            self.assertEqual(home_is_gold, away_is_gold,
                             f"cross-division pairing: {g}")

    def test_league_wide_draft_can_be_narrowed_to_one_division(self):
        self._league_two_divisions_fixture(per_division=2, n_slots=8)
        res = draft_schedule_for_league(
            self.store, "se1", "lg1", division_id="gold")
        self.assertEqual(res["team_count"], 2)
        self.assertEqual(len(res["draft_games"]), 1)
        game = res["draft_games"][0]
        self.assertTrue(game["home_team_id"].startswith("g"))
        self.assertTrue(game["away_team_id"].startswith("g"))

    def test_league_wide_draft_rejects_unknown_league(self):
        self._base()
        with self.assertRaises(Exception):
            draft_schedule_for_league(self.store, "se1", "nope")

    def test_league_wide_draft_rejects_league_from_different_season(self):
        self._league_two_divisions_fixture(per_division=1, n_slots=2)
        self.store.add_season(Season(id="se_other", program_id="prog1",
                                     name="Other Season"))
        with self.assertRaises(Exception):
            draft_schedule_for_league(self.store, "se_other", "lg1")

    def test_league_wide_commit_stamps_league_and_per_row_division(self):
        self._league_two_divisions_fixture(per_division=2, n_slots=8)
        result = commit_fresh_draft(
            self.api, season_id="se1", league_id="lg1", actor_id="admin")
        self.assertNotIn("error", result)
        self.assertEqual(result["season_id"], "se1")
        self.assertEqual(result["league_id"], "lg1")
        games = self.store.all_games()
        self.assertEqual(len(games), 2)
        divisions = {g.division_id for g in games}
        self.assertEqual(divisions, {"gold", "silver"})
        for g in games:
            self.assertEqual(g.season_id, "se1")
            self.assertEqual(g.league_id, "lg1")
            self.assertEqual(g.league_season_id, "ls_lg1_se1")

        # A league-wide draft may contain Division-less rows in production,
        # so its exact competition identity cannot be inferred later from a
        # Division. Prove the persisted LeagueSeason is accepted by both
        # downstream integrity gates.
        game = games[0]
        published = self.api.publish_draft_games(
            game_ids=[game.id], actor_id="admin")
        self.assertNotIn("error", published, repr(published))
        used = {g.ice_slot_id for g in self.store.all_games()}
        spare = next(
            slot for slot in self.store.all_ice_slots() if slot.id not in used)
        moved = self.api.move_game(
            game.id, spare.id, reason="Exact-scope regression",
            actor_id="admin")
        self.assertNotIn("error", moved, repr(moved))
        self.assertEqual(
            self.store.get_game(game.id).league_season_id,
            "ls_lg1_se1")

    def test_division_commit_persists_exact_league_season_for_publish_and_move(self):
        self._division_fixture(n_teams=2, n_slots=3)
        result = commit_fresh_draft(
            self.api, "div1", actor_id="admin")
        self.assertNotIn("error", result, repr(result))
        game = self.store.all_games()[0]
        self.assertEqual(game.league_season_id, "ls_lg1_se1")

        published = self.api.publish_draft_games(
            game_ids=[game.id], actor_id="admin")
        self.assertNotIn("error", published, repr(published))
        spare = next(
            slot for slot in self.store.all_ice_slots()
            if slot.id != game.ice_slot_id)
        moved = self.api.move_game(
            game.id, spare.id, reason="Exact-scope regression",
            actor_id="admin")
        self.assertNotIn("error", moved, repr(moved))
        self.assertEqual(
            self.store.get_game(game.id).league_season_id,
            "ls_lg1_se1")

    def test_draft_requires_division_or_season_and_league(self):
        self._base()
        result = self.api.draft_season_schedule()
        self.assertEqual(result.get("error", {}).get("code"), "validation_error")

    # -- Division/League consistency (#233 Slice G review) ------------------
    def test_league_wide_draft_rejects_division_from_different_league(self):
        self._two_leagues_fixture()
        with self.assertRaises(Exception):
            draft_schedule_for_league(self.store, "se1", "lg1", division_id="div2")

    def test_league_wide_commit_rejects_cross_league_division_zero_mutation(self):
        self._two_leagues_fixture()
        result = commit_fresh_draft(
            self.api, season_id="se1", league_id="lg1", division_id="div2",
            actor_id="admin")
        self.assertEqual(result.get("error", {}).get("code"), "not_found")
        self.assertEqual(self.store.all_games(), [])
        self.assertEqual(self.store.all_setup_audit(), [])

    def test_league_wide_draft_excludes_malformed_cross_league_registration(self):
        self._two_leagues_fixture()
        # league_id correctly names lg1, but division_id points to lg2's
        # Division — a corrupt/cross-League row that must never be trusted.
        self.store.add_team(Team(id="bad", name="Bad Team", program_id="prog1",
                                 league_id="lg1"))
        self.store.add_season_team_registration(SeasonTeamRegistration(
            id="streg_bad", league_season_id="ls_lg1_se1", team_id="bad",
            division_id="div2", active=True))
        res = draft_schedule_for_league(self.store, "se1", "lg1")
        self.assertEqual(res["team_count"], 2)  # t0/t1 only; "bad" excluded
        played = {tid for g in res["draft_games"]
                 for tid in (g["home_team_id"], g["away_team_id"])}
        self.assertNotIn("bad", played)

    def test_league_wide_commit_never_creates_cross_league_division_game(self):
        self._two_leagues_fixture()
        self.store.add_team(Team(id="bad", name="Bad Team", program_id="prog1",
                                 league_id="lg1"))
        self.store.add_season_team_registration(SeasonTeamRegistration(
            id="streg_bad", league_season_id="ls_lg1_se1", team_id="bad",
            division_id="div2", active=True))
        result = commit_fresh_draft(
            self.api, season_id="se1", league_id="lg1", actor_id="admin")
        self.assertNotIn("error", result)
        games = self.store.all_games()
        self.assertEqual(len(games), 1)  # only t0 vs t1
        for g in games:
            self.assertEqual(g.league_id, "lg1")
            self.assertIn(g.division_id, (None, "div1"))
            self.assertNotIn("bad", (g.home_team_id, g.away_team_id))

    # -- preserve existing Games, generate only missing pairings (#206 slice 1)
    def test_already_scheduled_reports_existing_pairing_and_skips_it(self):
        self._division_fixture(4, 6)
        home, away = round_robin_pairings(["t0", "t1", "t2", "t3"])[0]
        self.store.add_game(Game(
            id="existing1", home_team_id=home, away_team_id=away,
            start_time=BASE_TIME - timedelta(days=100),
            end_time=BASE_TIME - timedelta(days=100) + timedelta(hours=1),
            division_id="div1", season_id="se1", league_id="lg1",
            league_season_id="ls_lg1_se1"))
        res = draft_schedule(self.store, "div1")
        # 6 pairings total, 1 already exists -> 5 genuinely missing.
        self.assertEqual(len(res["draft_games"]), 5)
        self.assertEqual(len(res["already_scheduled"]), 1)
        entry = res["already_scheduled"][0]
        self.assertEqual({entry["home_team_id"], entry["away_team_id"]},
                         {home, away})
        self.assertEqual(entry["existing_game_id"], "existing1")
        self.assertEqual(entry["division_id"], "div1")
        self.assertIn("home_team_name", entry)
        self.assertIn("away_team_name", entry)
        # Never re-proposed alongside the genuinely missing pairings.
        for d in res["draft_games"]:
            self.assertNotEqual(
                frozenset((d["home_team_id"], d["away_team_id"])),
                frozenset((home, away)))
        # And the pre-existing Game itself is untouched.
        self.assertEqual(len(self.store.all_games()), 1)

    def test_already_scheduled_exempts_cancelled_games(self):
        # #328 review: league_season_id must be set to the fixture's OWN
        # ls_lg1_se1, or _existing_pairing_games' (league_season_id,
        # division_id) scope filter rejects this Game before `cancelled` is
        # ever evaluated -- a vacuous proof (still green with the exemption
        # predicate deleted entirely).
        self._division_fixture(4, 6)
        home, away = round_robin_pairings(["t0", "t1", "t2", "t3"])[0]
        self.store.add_game(Game(
            id="cancelled1", home_team_id=home, away_team_id=away,
            start_time=BASE_TIME, cancelled=True,
            division_id="div1", season_id="se1", league_id="lg1",
            league_season_id="ls_lg1_se1"))
        res = draft_schedule(self.store, "div1")
        self.assertEqual(res["already_scheduled"], [])
        self.assertEqual(len(res["draft_games"]), 6)  # regenerated, not skipped

    def test_already_scheduled_exempts_exhibition_games(self):
        # #328 review: same scope-matching requirement as the cancelled
        # case above -- league_season_id must reach the type predicate.
        self._division_fixture(4, 6)
        home, away = round_robin_pairings(["t0", "t1", "t2", "t3"])[0]
        self.store.add_game(Game(
            id="exhib1", home_team_id=home, away_team_id=away,
            start_time=BASE_TIME, game_type=GameType.EXHIBITION.value,
            division_id="div1", season_id="se1", league_id="lg1",
            league_season_id="ls_lg1_se1"))
        res = draft_schedule(self.store, "div1")
        self.assertEqual(res["already_scheduled"], [])
        self.assertEqual(len(res["draft_games"]), 6)  # a friendly isn't a fixture

    def test_league_wide_draft_reports_already_scheduled_per_division(self):
        self._league_two_divisions_fixture(per_division=2, n_slots=8)
        # Gold has exactly one pairing (g0 vs g1) -- pre-seed it as an
        # existing Game so only Silver's pairing remains genuinely missing.
        self.store.add_game(Game(
            id="existing_gold", home_team_id="g0", away_team_id="g1",
            start_time=BASE_TIME - timedelta(days=100),
            division_id="gold", season_id="se1", league_id="lg1",
            league_season_id="ls_lg1_se1"))
        res = draft_schedule_for_league(self.store, "se1", "lg1")
        self.assertEqual(len(res["draft_games"]), 1)  # only Silver's pairing
        self.assertEqual(len(res["already_scheduled"]), 1)
        entry = res["already_scheduled"][0]
        self.assertEqual({entry["home_team_id"], entry["away_team_id"]},
                         {"g0", "g1"})
        self.assertEqual(entry["division_id"], "gold")
        self.assertEqual(entry["existing_game_id"], "existing_gold")
        for d in res["draft_games"]:
            self.assertNotEqual(
                frozenset((d["home_team_id"], d["away_team_id"])),
                frozenset({"g0", "g1"}))

    def test_league_wide_no_division_group_excludes_only_the_current_league_season(self):
        # #328 review: a league-wide draft's "no Division" group is keyed
        # by division_id=None for EVERY League/Season -- scoping the
        # exclusion by division_id alone would let a division-less Regular
        # Game from a DIFFERENT Season (teams are permanent, so the same
        # two ids can be registered again later) wrongly suppress a
        # pairing that has never actually been played in the CURRENT
        # Season+League.
        self._base()
        self.store.add_league(League(id="lg1", program_id="prog1", name="League"))
        self.store.add_season(Season(id="seB", program_id="prog1", name="Season B"))
        # test_scheduler.py's draft_schedule_for_league is the league-scoped
        # wrapper (services/league_scoped_scheduler.py), which restricts
        # candidate ice to Venues with active SeasonVenueAccess for the
        # target Season -- Season B needs its own grant on the shared venue.
        self.store.add_season_venue_access(SeasonVenueAccess(
            id="sva_seB", season_id="seB", venue_id="v1", active=True))
        self.store.add_league_season(LeagueSeason(
            id="ls_lg1_se1", league_id="lg1", season_id="se1"))
        self.store.add_league_season(LeagueSeason(
            id="ls_lg1_seB", league_id="lg1", season_id="seB"))
        for tid in ("t0", "t1"):
            self.store.add_team(Team(id=tid, name=tid, program_id="prog1",
                                     league_id="lg1"))
        # t0/t1 registered directly at the League level (no Division) in
        # BOTH Seasons -- permanent Teams re-registering in a later Season
        # is the normal case, not an anomaly.
        for ls_id, reg_prefix in (("ls_lg1_se1", "seA"), ("ls_lg1_seB", "seB")):
            for tid in ("t0", "t1"):
                self.store.add_season_team_registration(SeasonTeamRegistration(
                    id=f"streg_{reg_prefix}_{tid}", league_season_id=ls_id,
                    team_id=tid, division_id=None, active=True))
        self._slots(2, start=BASE_TIME + timedelta(days=30))
        # Season A's Game for t0-vs-t1: division-less, tagged to ls_lg1_se1.
        self.store.add_game(Game(
            id="seasonA_game", home_team_id="t0", away_team_id="t1",
            start_time=BASE_TIME - timedelta(days=100),
            division_id=None, season_id="se1", league_id="lg1",
            league_season_id="ls_lg1_se1"))
        res = draft_schedule_for_league(self.store, "seB", "lg1")
        # Season B has never played this pairing -- it must be freshly
        # generated, not silently suppressed by Season A's leftover Game.
        self.assertEqual(res["already_scheduled"], [])
        self.assertEqual(len(res["draft_games"]), 1)
        self.assertEqual(
            {res["draft_games"][0]["home_team_id"],
             res["draft_games"][0]["away_team_id"]},
            {"t0", "t1"})

    def test_league_wide_draft_excludes_by_division_not_pairing_alone(self):
        # #328 review round 2: the scope FILTER is (league_season_id,
        # division_id), but the returned map must also be KEYED by that
        # full tuple, not by pairing alone -- otherwise a league-wide call
        # with several Divisions in scope (all one LeagueSeason) could let
        # a real Game that only ever qualified for Gold's scope wrongly
        # match a lookup for Silver's fresh pairing. Proves BOTH directions
        # at once: Gold has its OWN current pairing (t2 vs t3) with a real
        # Game, correctly reported as already-scheduled for GOLD; t0/t1
        # have a STALE Game tagged to Gold from before they moved to
        # Silver (their CURRENT registration), which must NOT suppress
        # their fresh Silver pairing -- so the key is precise in both
        # directions, not just permissive in one.
        self._base()
        self.store.add_league(League(id="lg1", program_id="prog1", name="League"))
        self.store.add_league_season(LeagueSeason(
            id="ls_lg1_se1", league_id="lg1", season_id="se1"))
        self.store.add_division(Division(
            id="gold", league_season_id="ls_lg1_se1", name="Gold"))
        self.store.add_division(Division(
            id="silver", league_season_id="ls_lg1_se1", name="Silver"))
        for tid in ("t0", "t1", "t2", "t3"):
            self.store.add_team(Team(id=tid, name=tid, program_id="prog1",
                                     league_id="lg1"))
        # t2/t3 stay in Gold with their own CURRENT, genuinely
        # already-played pairing.
        for tid in ("t2", "t3"):
            self.store.add_season_team_registration(SeasonTeamRegistration(
                id=f"streg_{tid}", league_season_id="ls_lg1_se1",
                team_id=tid, division_id="gold", active=True))
        # t0/t1 have moved to Silver -- their CURRENT registration.
        for tid in ("t0", "t1"):
            self.store.add_season_team_registration(SeasonTeamRegistration(
                id=f"streg_{tid}", league_season_id="ls_lg1_se1",
                team_id=tid, division_id="silver", active=True))
        self._slots(2, start=BASE_TIME + timedelta(days=30))
        # Gold's OWN, current, genuinely already-scheduled pairing.
        self.store.add_game(Game(
            id="gold_real_game", home_team_id="t2", away_team_id="t3",
            start_time=BASE_TIME - timedelta(days=1),
            division_id="gold", season_id="se1", league_id="lg1",
            league_season_id="ls_lg1_se1"))
        # A STALE Game from when t0/t1 were in Gold, before they moved to
        # Silver -- Silver has never played this pairing, and Gold no
        # longer has these two teams at all.
        self.store.add_game(Game(
            id="stale_gold_game", home_team_id="t0", away_team_id="t1",
            start_time=BASE_TIME - timedelta(days=100),
            division_id="gold", season_id="se1", league_id="lg1",
            league_season_id="ls_lg1_se1"))
        res = draft_schedule_for_league(self.store, "se1", "lg1")
        # Silver's t0-vs-t1 must be freshly generated -- NOT suppressed by
        # Gold's stale Game for the identical pairing.
        self.assertEqual(len(res["draft_games"]), 1)
        self.assertEqual(res["draft_games"][0]["division_id"], "silver")
        self.assertEqual(
            {res["draft_games"][0]["home_team_id"],
             res["draft_games"][0]["away_team_id"]},
            {"t0", "t1"})
        # Gold's t2-vs-t3 must STILL be correctly reported already-scheduled
        # -- proving the key correctly excludes Gold's own real pairing
        # too, not just permissively skipping everything.
        self.assertEqual(len(res["already_scheduled"]), 1)
        entry = res["already_scheduled"][0]
        self.assertEqual({entry["home_team_id"], entry["away_team_id"]},
                         {"t2", "t3"})
        self.assertEqual(entry["division_id"], "gold")
        self.assertEqual(entry["existing_game_id"], "gold_real_game")

    # -- pairing-identity priority over physical conflicts (#328 review round 4)
    def _pairing_race_wins_over_physical_conflict(self, api_cls, physical):
        """Shared assertion: a same-pairing existing Game must win with the
        terminal ``pairing_already_scheduled`` reason (naming the winning
        Game) regardless of whether the SAME row would ALSO fail the
        physical placement gate. ``physical`` is one of ``"same_slot"``
        (the winner takes the identical slot the stale proposal's row
        references), ``"overlapping_slot"`` (the winner takes a DIFFERENT
        slot, same instant, different rink), or ``"none"`` (the winner's
        slot never conflicts with the stale row's own slot at all -- the
        original #206 scenario). ``api_cls`` selects which commit facade to
        exercise; both must independently enforce the same priority since
        neither delegates to the other.

        #328 review round 6: the raced pairing is the LAST of 6 rows, not
        the first (mirrors test_commit_rejects_a_pairing_that_already_has_a_
        real_game's established technique) -- proving the loser's own
        transaction tentatively creates the five EARLIER rows' Games and
        flips their slots ALLOCATED before reaching the contested one, and
        that the WHOLE batch (not merely the bad row) rolls back. The
        winning Game is inserted directly rather than via a second real
        commit -- what matters here is only that a real Game exists for
        this exact pairing on this exact ice, not which code path created
        it (a genuine concurrent commit is what the forced PostgreSQL races
        in test_placement_concurrency.py additionally prove)."""
        self._division_fixture(4, 6)
        api = api_cls(self.store)
        stale_proposal = api.draft_season_schedule("div1")
        rows = stale_proposal["draft_games"]
        self.assertEqual(len(rows), 6, repr(stale_proposal))
        row = rows[-1]
        if physical == "same_slot":
            winner_slot_id = row["ice_slot_id"]
        elif physical == "overlapping_slot":
            slot0 = self.store.get_ice_slot(row["ice_slot_id"])
            self.store.add_rink(Rink(
                id="r_overlap", venue_id="v1", name="Overlap"))
            self.store.add_ice_slot(IceSlot(
                id="overlap_slot", rink_id="r_overlap",
                start_time=slot0.start_time, end_time=slot0.end_time))
            winner_slot_id = "overlap_slot"
        else:
            assert physical == "none"
            far_start = BASE_TIME - timedelta(days=100)
            self.store.add_rink(Rink(id="r_far", venue_id="v1", name="Far"))
            self.store.add_ice_slot(IceSlot(
                id="far_slot", rink_id="r_far", start_time=far_start,
                end_time=far_start + timedelta(hours=1)))
            winner_slot_id = "far_slot"
        winner_slot = self.store.get_ice_slot(winner_slot_id)
        winning_game_id = "winning_game"
        self.store.add_game(Game(
            id=winning_game_id, home_team_id=row["home_team_id"],
            away_team_id=row["away_team_id"],
            start_time=winner_slot.start_time, end_time=winner_slot.end_time,
            ice_slot_id=winner_slot_id, division_id="div1", season_id="se1",
            league_id="lg1", league_season_id="ls_lg1_se1"))
        winner_slot.status = IceSlotStatus.ALLOCATED
        self.store.save_ice_slot(winner_slot)
        # Force the stale (pre-win) proposal through commit unchanged.
        api.draft_season_schedule = lambda *a, **k: stale_proposal
        res = commit_fresh_draft(api, "div1")
        self.assertIn("error", res, repr(res))
        self.assertEqual(res["error"]["details"]["reason"],
                         "pairing_already_scheduled", repr(res))
        self.assertEqual(res["error"]["details"]["existing_game_id"],
                         winning_game_id, repr(res))
        self.assertIn(row["home_team_name"], res["error"]["message"])
        self.assertIn(row["away_team_name"], res["error"]["message"])
        # Atomic rollback: only the winning Game exists -- the five EARLIER
        # rows the loser's own transaction tentatively created/allocated
        # are back to AVAILABLE, and no batch audit landed.
        self.assertEqual(len(self.store.all_games()), 1, repr(res))
        for r in rows[:-1]:
            self.assertEqual(
                self.store.get_ice_slot(r["ice_slot_id"]).status,
                IceSlotStatus.AVAILABLE, r["ice_slot_id"])
        self.assertFalse(any(a.action == "draft_schedule_committed"
                             for a in self.store.all_setup_audit()))

    def test_league_scoped_pairing_race_wins_over_same_slot_conflict(self):
        self._pairing_race_wins_over_physical_conflict(ApiService, "same_slot")

    def test_league_scoped_pairing_race_wins_over_overlapping_slot_conflict(self):
        self._pairing_race_wins_over_physical_conflict(
            ApiService, "overlapping_slot")

    def test_league_scoped_pairing_race_wins_over_non_overlapping_slot(self):
        self._pairing_race_wins_over_physical_conflict(ApiService, "none")

    def test_base_facade_pairing_race_wins_over_same_slot_conflict(self):
        self._pairing_race_wins_over_physical_conflict(
            BaseApiService, "same_slot")

    def test_base_facade_pairing_race_wins_over_overlapping_slot_conflict(self):
        self._pairing_race_wins_over_physical_conflict(
            BaseApiService, "overlapping_slot")

    def test_base_facade_pairing_race_wins_over_non_overlapping_slot(self):
        self._pairing_race_wins_over_physical_conflict(BaseApiService, "none")

    # -- direct proof of mid-transaction rollback (#328 review round 8
    # finding 2) --------------------------------------------------------
    def _later_row_failure_rolls_back_earlier_writes(self, api_cls):
        """Shared assertion: the forced PostgreSQL races above prove the
        OUTCOME (loser's whole batch absent) under genuine two-session
        concurrency, but by construction they cannot directly observe
        WHY -- both facades lock every batch Team upfront via
        ``_lock_teams``, so a losing session's ``get_team_for_update``
        blocks and only returns once the winner commits and releases the
        lock; the losing session's per-row loop (including its own earlier
        rows' writes) only ever executes AFTER that point. That's a claim
        about the CODE, not something the black-box final-state assertion
        alone demonstrates.

        This test proves the mechanism directly instead, with no threads
        needed: it injects a failure on the LAST of six rows, and the
        injection point itself queries the SAME store/transaction to
        confirm each of the five EARLIER rows' Games and slot flips are
        already visible -- BEFORE raising the error that unwinds the whole
        transaction. This is a direct, in-transaction observation of
        "written, then rolled back," not an inference from the final state
        (which alone cannot distinguish "written then rolled back" from
        "never attempted")."""
        self._division_fixture(4, 6)
        api = api_cls(self.store)
        preview = api.draft_season_schedule("div1")
        rows = preview["draft_games"]
        self.assertEqual(len(rows), 6, repr(preview))
        earlier_rows, last_row = rows[:-1], rows[-1]
        real_assert = api.setup._assert_slot_free_for_game
        observed_before_injection = {}

        def _spy(ice_slot_id, home_team_id, away_team_id, **kwargs):
            if ice_slot_id == last_row["ice_slot_id"]:
                for r in earlier_rows:
                    games = [
                        g for g in self.store.all_games()
                        if not g.cancelled
                        and {g.home_team_id, g.away_team_id}
                        == {r["home_team_id"], r["away_team_id"]}]
                    slot = self.store.get_ice_slot(r["ice_slot_id"])
                    observed_before_injection[r["ice_slot_id"]] = (
                        len(games) == 1
                        and slot.status == IceSlotStatus.ALLOCATED)
                raise ScheduleConflictError(
                    "Injected failure -- observing mid-transaction state "
                    "before unwinding (#328 review round 8 finding 2).",
                    details={"reason": "test_injected_after_earlier_rows_written"})
            return real_assert(ice_slot_id, home_team_id, away_team_id, **kwargs)

        api.setup._assert_slot_free_for_game = _spy
        res = commit_fresh_draft(api, "div1")
        self.assertEqual(len(earlier_rows), 5, repr(rows))
        self.assertTrue(
            all(observed_before_injection.values()),
            f"every earlier row's Game/slot must be visible in-transaction "
            f"BEFORE the injected failure: {observed_before_injection!r}")
        self.assertIn("error", res, repr(res))
        self.assertEqual(
            res["error"]["details"]["reason"],
            "test_injected_after_earlier_rows_written", repr(res))
        # And now the direct proof of ROLLBACK: what was just observed as
        # written, mid-transaction, is gone once the transaction unwinds.
        self.assertEqual(self.store.all_games(), [], repr(res))
        for r in earlier_rows + [last_row]:
            self.assertEqual(
                self.store.get_ice_slot(r["ice_slot_id"]).status,
                IceSlotStatus.AVAILABLE, r["ice_slot_id"])
        self.assertFalse(any(a.action == "draft_schedule_committed"
                             for a in self.store.all_setup_audit()))

    def test_league_scoped_later_row_failure_rolls_back_earlier_writes(self):
        self._later_row_failure_rolls_back_earlier_writes(ApiService)

    def test_base_facade_later_row_failure_rolls_back_earlier_writes(self):
        self._later_row_failure_rolls_back_earlier_writes(BaseApiService)

    # -- stale-preview TOCTOU gate (#328 review round 5) --------------------
    def _stale_preview_refused(self, api_cls, change):
        """Shared assertion: a Game created or cancelled in the window
        between Generate and Commit must invalidate the reviewed preview's
        fingerprint -- the commit is refused with the terminal
        ``preview_stale`` reason and writes nothing, rather than silently
        committing a batch that quietly grew or shrank relative to what the
        operator reviewed. ``change`` is ``"create"`` (a real Game appears
        for a pairing the stale preview still lists as missing -- the
        committed batch would otherwise silently shrink) or ``"cancel"``
        (the Game blocking a pairing the stale preview still lists as
        already-scheduled gets cancelled, so it's newly missing -- the
        committed batch would otherwise silently grow with a pairing the
        operator never reviewed). ``api_cls`` selects which commit facade
        to exercise; both independently reimplement the commit body, so
        both need the check."""
        self._division_fixture(4, 6)
        home, away = round_robin_pairings(["t0", "t1", "t2", "t3"])[0]
        if change == "cancel":
            # Seed the pairing's blocking Game BEFORE the preview, so the
            # preview reports it already-scheduled; cancelling it below
            # then makes that same pairing newly missing.
            self.store.add_game(Game(
                id="blocking_game", home_team_id=home, away_team_id=away,
                start_time=BASE_TIME - timedelta(days=100),
                end_time=BASE_TIME - timedelta(days=100) + timedelta(hours=1),
                division_id="div1", season_id="se1", league_id="lg1",
                league_season_id="ls_lg1_se1"))
        api = api_cls(self.store)
        preview = api.draft_season_schedule("div1")
        stale_fingerprint = preview["draft_fingerprint"]
        if change == "create":
            self.assertEqual(preview["already_scheduled"], [], repr(preview))
            # A Game for the pairing appears in the window after Generate --
            # simulating a concurrent commit, or any other write path, that
            # the operator's on-screen preview never saw.
            self.store.add_game(Game(
                id="drift_game", home_team_id=home, away_team_id=away,
                start_time=BASE_TIME - timedelta(days=100),
                end_time=BASE_TIME - timedelta(days=100) + timedelta(hours=1),
                division_id="div1", season_id="se1", league_id="lg1",
                league_season_id="ls_lg1_se1"))
        else:
            assert change == "cancel"
            self.assertEqual(len(preview["already_scheduled"]), 1, repr(preview))
            blocking = self.store.get_game("blocking_game")
            blocking.cancelled = True
            self.store.save_game(blocking)
        games_before = len(self.store.all_games())
        audits_before = len(self.store.all_setup_audit())
        res = api.commit_draft_schedule(
            "div1", draft_fingerprint=stale_fingerprint)
        self.assertIn("error", res, repr(res))
        self.assertEqual(
            res["error"]["details"]["reason"], "preview_stale", repr(res))
        self.assertEqual(len(self.store.all_games()), games_before, repr(res))
        self.assertEqual(
            len(self.store.all_setup_audit()), audits_before, repr(res))

    def test_league_scoped_commit_refuses_stale_preview_after_pairing_created(
            self):
        self._stale_preview_refused(ApiService, "create")

    def test_league_scoped_commit_refuses_stale_preview_after_blocking_game_cancelled(
            self):
        self._stale_preview_refused(ApiService, "cancel")

    def test_base_facade_commit_refuses_stale_preview_after_pairing_created(
            self):
        self._stale_preview_refused(BaseApiService, "create")

    def test_base_facade_commit_refuses_stale_preview_after_blocking_game_cancelled(
            self):
        self._stale_preview_refused(BaseApiService, "cancel")

    def _commit_requires_a_preview_fingerprint(self, api_cls):
        self._division_fixture(4, 6)
        api = api_cls(self.store)
        games_before = len(self.store.all_games())
        res = api.commit_draft_schedule("div1")  # no draft_fingerprint at all
        self.assertIn("error", res, repr(res))
        self.assertEqual(
            res["error"]["details"]["reason"], "preview_required", repr(res))
        self.assertEqual(len(self.store.all_games()), games_before, repr(res))

    def test_league_scoped_commit_requires_a_preview_fingerprint(self):
        self._commit_requires_a_preview_fingerprint(ApiService)

    def test_base_facade_commit_requires_a_preview_fingerprint(self):
        self._commit_requires_a_preview_fingerprint(BaseApiService)

    # -- fingerprint must bind PLACEMENT too, not just pairing identity
    # (#328 review round 7) -----------------------------------------------
    def _stale_preview_refused_on_placement_change(self, api_cls, change):
        """Shared assertion: the SAME still-missing pairing silently
        resolving to a DIFFERENT ice_slot_id/time between Generate and
        Commit must invalidate the preview -- the commit-time physical
        gate (``_assert_slot_free_for_game``) proves the new placement is
        LEGAL, not that it is the placement the operator actually
        reviewed. ``change`` is ``"slot_removed"`` (the reviewed slot
        becomes unavailable in the window, but a different slot still
        exists, so the pairing itself remains genuinely missing -- just
        onto different ice) or ``"scope_changed"`` (the commit call is
        given a different ``slot_ids`` restriction than the preview used,
        so its own regeneration resolves the same pairing onto different
        ice even though nothing in the store itself changed)."""
        self._division_fixture(2, 2)  # 1 pairing, 2 candidate slots
        api = api_cls(self.store)
        preview = api.draft_season_schedule("div1")
        self.assertEqual(len(preview["draft_games"]), 1, repr(preview))
        original_slot_id = preview["draft_games"][0]["ice_slot_id"]
        stale_fingerprint = preview["draft_fingerprint"]
        commit_kwargs = {}
        if change == "slot_removed":
            slot = self.store.get_ice_slot(original_slot_id)
            slot.status = IceSlotStatus.BLOCKED
            self.store.save_ice_slot(slot)
        else:
            assert change == "scope_changed"
            other_slot_id = next(
                s.id for s in self.store.all_ice_slots()
                if s.id != original_slot_id)
            commit_kwargs["slot_ids"] = [other_slot_id]
        games_before = len(self.store.all_games())
        audits_before = len(self.store.all_setup_audit())
        res = api.commit_draft_schedule(
            "div1", draft_fingerprint=stale_fingerprint, **commit_kwargs)
        self.assertIn("error", res, repr(res))
        self.assertEqual(
            res["error"]["details"]["reason"], "preview_stale", repr(res))
        self.assertEqual(len(self.store.all_games()), games_before, repr(res))
        self.assertEqual(
            len(self.store.all_setup_audit()), audits_before, repr(res))

    def test_league_scoped_commit_refuses_stale_preview_after_reviewed_slot_becomes_unavailable(
            self):
        self._stale_preview_refused_on_placement_change(
            ApiService, "slot_removed")

    def test_league_scoped_commit_refuses_stale_preview_when_commit_scope_differs_from_preview(
            self):
        self._stale_preview_refused_on_placement_change(
            ApiService, "scope_changed")

    def test_base_facade_commit_refuses_stale_preview_after_reviewed_slot_becomes_unavailable(
            self):
        self._stale_preview_refused_on_placement_change(
            BaseApiService, "slot_removed")

    def test_base_facade_commit_refuses_stale_preview_when_commit_scope_differs_from_preview(
            self):
        self._stale_preview_refused_on_placement_change(
            BaseApiService, "scope_changed")

    # -- already_scheduled revalidation under the lock (#328 review round 8
    # finding 1) -----------------------------------------------------------
    def _already_scheduled_cancelled_after_regen_refused(self, api_cls):
        """Shared assertion: an already_scheduled row's blocking Game being
        cancelled AFTER this method's own internal proposal regeneration (and
        its fingerprint compare) but BEFORE the locked recheck must still
        refuse the whole commit -- that row is not part of the batch's
        writes, so nothing else ever re-examines it. Without this check the
        5 genuinely-missing rows would commit anyway (a real bug found by
        review): the reviewed batch silently persists as reviewed even
        though the world moved a moment after the wide gate already passed,
        leaving the pairing that just opened up unscheduled with no error.

        Modeled by monkeypatching ``draft_season_schedule`` to return a
        FROZEN pre-cancellation proposal (so the fingerprint compare
        matches, exactly as it would if the cancellation lands a moment
        after that regeneration completes) and cancelling the blocking Game
        in the store before calling commit -- the locked recheck this test
        targets reads the store fresh, so it sees the cancellation
        regardless of when the mocked regeneration itself "ran"."""
        self._division_fixture(4, 6)
        home, away = round_robin_pairings(["t0", "t1", "t2", "t3"])[0]
        self.store.add_game(Game(
            id="blocking_game", home_team_id=home, away_team_id=away,
            start_time=BASE_TIME - timedelta(days=100),
            end_time=BASE_TIME - timedelta(days=100) + timedelta(hours=1),
            division_id="div1", season_id="se1", league_id="lg1",
            league_season_id="ls_lg1_se1"))
        api = api_cls(self.store)
        preview = api.draft_season_schedule("div1")
        self.assertEqual(len(preview["draft_games"]), 5, repr(preview))
        self.assertEqual(len(preview["already_scheduled"]), 1, repr(preview))
        stale_fingerprint = preview["draft_fingerprint"]
        # Freeze the regeneration commit performs internally to this
        # pre-cancellation snapshot -- its fingerprint still matches
        # ``stale_fingerprint`` below, exactly as if the cancellation lands
        # in the gap after that regeneration but before the locked recheck.
        api.draft_season_schedule = lambda *a, **k: preview
        blocking = self.store.get_game("blocking_game")
        blocking.cancelled = True
        self.store.save_game(blocking)
        games_before = len(self.store.all_games())
        audits_before = len(self.store.all_setup_audit())
        res = api.commit_draft_schedule(
            "div1", draft_fingerprint=stale_fingerprint)
        self.assertIn("error", res, repr(res))
        self.assertEqual(
            res["error"]["details"]["reason"], "preview_stale", repr(res))
        # Zero writes: none of the 5 genuinely-missing rows committed either
        # -- the whole reviewed batch is stale, not just the cancelled row.
        self.assertEqual(len(self.store.all_games()), games_before, repr(res))
        self.assertEqual(
            len(self.store.all_setup_audit()), audits_before, repr(res))

    def test_league_scoped_commit_refuses_when_already_scheduled_game_cancelled_after_regen(
            self):
        self._already_scheduled_cancelled_after_regen_refused(ApiService)

    def test_base_facade_commit_refuses_when_already_scheduled_game_cancelled_after_regen(
            self):
        self._already_scheduled_cancelled_after_regen_refused(BaseApiService)


class MemorySchedulerTest(SchedulerContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurableSchedulerTest(SchedulerContract, unittest.TestCase):
    def make_store(self):
        url = os.environ.get("TEST_DATABASE_URL") or ":memory:"
        store = SqlStore(url)
        store.reset_schema()
        return store


class SchedulerHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

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

    def test_non_operator_cannot_draft(self):
        div = srv.STATE.api.store.all_divisions()[0].id
        for who in ("coach", "player", "viewer"):
            c = self._client()
            self._req(c, "POST", "/api/auth/login", {"username": who, "password": "demo"})
            status, _ = self._req(c, "POST", "/api/scheduler/draft",
                                  {"division_id": div})
            self.assertEqual(status, 403, who)

    def test_operator_gets_a_draft_proposal(self):
        div = srv.STATE.api.store.all_divisions()[0].id
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        status, body = self._req(c, "POST", "/api/scheduler/draft",
                                 {"division_id": div})
        self.assertEqual(status, 200)
        self.assertIn("draft_games", body)
        self.assertIn("unscheduled", body)
        self.assertIn("unschedulable_teams", body)

    def test_unknown_division_is_404(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        status, body = self._req(c, "POST", "/api/scheduler/draft",
                                 {"division_id": "nope"})
        self.assertEqual(body["error"]["code"], "not_found")

    def test_league_wide_draft_via_http(self):
        div = srv.STATE.api.store.all_divisions()[0].id
        ls = srv.STATE.api.store.get_league_season(
            srv.STATE.api.store.get_division(div).league_season_id)
        season_id = ls.season_id
        league_id = ls.league_id
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        status, body = self._req(c, "POST", "/api/scheduler/draft",
                                 {"season_id": season_id, "league_id": league_id})
        self.assertEqual(status, 200)
        self.assertEqual(body["season_id"], season_id)
        self.assertEqual(body["league_id"], league_id)

    def test_draft_missing_scope_is_validation_error(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        status, body = self._req(c, "POST", "/api/scheduler/draft", {})
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_league_wide_draft_rejects_cross_league_division_via_http(self):
        div = srv.STATE.api.store.all_divisions()[0].id
        ls = srv.STATE.api.store.get_league_season(
            srv.STATE.api.store.get_division(div).league_season_id)
        season_id = ls.season_id
        league_id = ls.league_id
        program_id = srv.STATE.api.store.get_season(season_id).program_id
        other_league = League(id="lg_http_other", program_id=program_id,
                              name="Other League")
        srv.STATE.api.store.add_league(other_league)
        other_ls = LeagueSeason(id="ls_lg_http_other_" + season_id,
                                league_id="lg_http_other", season_id=season_id)
        srv.STATE.api.store.add_league_season(other_ls)
        other_division = Division(id="div_http_other",
                                  league_season_id=other_ls.id,
                                  name="Other Division")
        srv.STATE.api.store.add_division(other_division)
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        status, body = self._req(c, "POST", "/api/scheduler/draft",
                                 {"season_id": season_id, "league_id": league_id,
                                  "division_id": "div_http_other"})
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
