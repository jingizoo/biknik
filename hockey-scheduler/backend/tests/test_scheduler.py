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
from hockey_scheduler.services.scheduler import (
    MAX_MEETINGS_PER_OPPONENT,
    _draft_fingerprint,
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


class DraftFingerprintTest(unittest.TestCase):
    """Direct, deterministic proof that _draft_fingerprint is sensitive to
    every field it now binds (#328 review round 10 finding 1) -- tested
    against the pure function itself rather than an end-to-end scenario,
    since the eligible-team-set axis already gets full both-facade
    Generate-then-mutate-then-Commit coverage in SchedulerContract below,
    and organically reproducing a changed UNSCHEDULED reason code through
    the whole constraint-evaluation stack while keeping every other field
    byte-for-byte identical is far more fragile to construct than simply
    calling the function twice with one field varied."""

    def _base_args(self):
        return dict(
            league_season_id="ls1", team_ids=["t0", "t1", "t2", "t3"],
            draft_games=[{
                "division_id": "d1", "home_team_id": "t0", "away_team_id": "t3",
                "ice_slot_id": "s0", "start_time": "2026-09-12T08:00:00+00:00",
            }],
            unscheduled=[{
                "division_id": "d1", "home_team_id": "t1", "away_team_id": "t2",
                "reason_codes": ["no_ice_available"],
            }],
            unschedulable_teams=[],
            already_scheduled=[],
        )

    def test_changed_team_ids_changes_fingerprint(self):
        a = self._base_args()
        b = self._base_args()
        b["team_ids"] = a["team_ids"] + ["t4"]
        self.assertNotEqual(_draft_fingerprint(**a), _draft_fingerprint(**b))

    def test_changed_meetings_per_opponent_changes_fingerprint(self):
        # #375 -- the requested FORMAT is bound directly, so this holds even
        # with every row list held byte-for-byte identical. Asserted against
        # the pure function precisely because organically producing two
        # different N with identical buckets is not reproducible: the point
        # is that the guarantee does not depend on the buckets differing.
        a = self._base_args()
        b = self._base_args()
        b["meetings_per_opponent"] = 3
        self.assertNotEqual(_draft_fingerprint(**a), _draft_fingerprint(**b))

    def test_default_meetings_per_opponent_matches_explicit_one(self):
        # Anti-vacuity partner of the test above: the new payload field must
        # not make an omitted format hash differently from an explicit
        # single round-robin, or every pre-#375 caller would look "changed".
        a = self._base_args()
        b = self._base_args()
        b["meetings_per_opponent"] = 1
        self.assertEqual(_draft_fingerprint(**a), _draft_fingerprint(**b))

    def test_changed_unscheduled_reason_codes_changes_fingerprint(self):
        a = self._base_args()
        b = self._base_args()
        b["unscheduled"] = [dict(a["unscheduled"][0],
                                 reason_codes=["team_rest_violation"])]
        self.assertNotEqual(_draft_fingerprint(**a), _draft_fingerprint(**b))

    def test_changed_unscheduled_reason_text_changes_fingerprint(self):
        """#328 review round 11 finding 1: a policy THRESHOLD change (e.g.
        min_playable_minutes) can rewrite the human-readable `reason` text
        embedded with the current value while `reason_codes` -- a fixed
        category -- stays identical. The fingerprint must still catch it."""
        a = self._base_args()
        b = self._base_args()
        a["unscheduled"] = [dict(a["unscheduled"][0], reason_codes=[
            "insufficient_playable_time"],
            reason="No slot satisfies constraints: Ice slot s9 is only 30 "
                   "playable minutes; this competition requires at least "
                   "45.")]
        b["unscheduled"] = [dict(b["unscheduled"][0], reason_codes=[
            "insufficient_playable_time"],
            reason="No slot satisfies constraints: Ice slot s9 is only 30 "
                   "playable minutes; this competition requires at least "
                   "50.")]
        self.assertNotEqual(_draft_fingerprint(**a), _draft_fingerprint(**b))

    def test_changed_rink_name_changes_fingerprint(self):
        """#328 review round 12 finding 2: a repeat rinks CSV import that
        renames an existing Rink (matched by rink_code, #95) leaves
        ice_slot_id/start_time identical -- the fingerprint must still
        catch the divergence, since rink_name is both displayed and
        persisted verbatim onto Game.rink."""
        a = self._base_args()
        b = self._base_args()
        a["draft_games"] = [dict(a["draft_games"][0], rink_id="r1",
                                 rink_name="Main")]
        b["draft_games"] = [dict(b["draft_games"][0], rink_id="r1",
                                 rink_name="Main Renamed")]
        self.assertNotEqual(_draft_fingerprint(**a), _draft_fingerprint(**b))

    def test_changed_rink_id_changes_fingerprint(self):
        a = self._base_args()
        b = self._base_args()
        a["draft_games"] = [dict(a["draft_games"][0], rink_id="r1")]
        b["draft_games"] = [dict(b["draft_games"][0], rink_id="r2")]
        self.assertNotEqual(_draft_fingerprint(**a), _draft_fingerprint(**b))

    def test_changed_end_time_changes_fingerprint(self):
        """#328 review round 12 finding 2: an in-place edit of a slot's own
        end_time (its id/start_time unchanged) must also invalidate the
        preview -- end_time is persisted verbatim onto Game.end_time."""
        a = self._base_args()
        b = self._base_args()
        a["draft_games"] = [dict(a["draft_games"][0],
                                 end_time="2026-09-12T09:00:00+00:00")]
        b["draft_games"] = [dict(b["draft_games"][0],
                                 end_time="2026-09-12T09:30:00+00:00")]
        self.assertNotEqual(_draft_fingerprint(**a), _draft_fingerprint(**b))

    def test_changed_unschedulable_teams_changes_fingerprint(self):
        a = self._base_args()
        b = self._base_args()
        a["unschedulable_teams"] = [
            {"team_id": "t1", "reason_codes": ["no_ice_available"]}]
        b["unschedulable_teams"] = [
            {"team_id": "t1", "reason_codes": ["team_rest_violation"]}]
        self.assertNotEqual(_draft_fingerprint(**a), _draft_fingerprint(**b))

    def test_changed_draft_games_team_name_changes_fingerprint(self):
        """#328 review round 15 finding 1: a repeat teams/players CSV import
        that renames an existing Team (matched by team_code, #92) leaves
        every id-keyed field identical -- the fingerprint must still catch
        the divergence, since home_team_name/away_team_name are both
        displayed and persisted verbatim onto Game.home_team/away_team."""
        a = self._base_args()
        b = self._base_args()
        a["draft_games"] = [dict(a["draft_games"][0], home_team_name="T-Zero",
                                 away_team_name="T-Three")]
        b["draft_games"] = [dict(b["draft_games"][0],
                                 home_team_name="T-Zero Renamed",
                                 away_team_name="T-Three")]
        self.assertNotEqual(_draft_fingerprint(**a), _draft_fingerprint(**b))

    def test_changed_already_scheduled_team_name_changes_fingerprint(self):
        a = self._base_args()
        b = self._base_args()
        row = {"division_id": "d1", "home_team_id": "t0", "away_team_id": "t3",
               "existing_game_id": "g1"}
        a["already_scheduled"] = [dict(row, home_team_name="T-Zero",
                                       away_team_name="T-Three")]
        b["already_scheduled"] = [dict(row, home_team_name="T-Zero Renamed",
                                       away_team_name="T-Three")]
        self.assertNotEqual(_draft_fingerprint(**a), _draft_fingerprint(**b))

    def test_changed_unscheduled_team_name_changes_fingerprint(self):
        a = self._base_args()
        b = self._base_args()
        a["unscheduled"] = [dict(a["unscheduled"][0], home_team_name="T-One",
                                 away_team_name="T-Two")]
        b["unscheduled"] = [dict(b["unscheduled"][0],
                                 home_team_name="T-One Renamed",
                                 away_team_name="T-Two")]
        self.assertNotEqual(_draft_fingerprint(**a), _draft_fingerprint(**b))

    def test_changed_unschedulable_team_name_changes_fingerprint(self):
        a = self._base_args()
        b = self._base_args()
        a["unschedulable_teams"] = [
            {"team_id": "t1", "team_name": "T-One",
             "reason_codes": ["no_ice_available"]}]
        b["unschedulable_teams"] = [
            {"team_id": "t1", "team_name": "T-One Renamed",
             "reason_codes": ["no_ice_available"]}]
        self.assertNotEqual(_draft_fingerprint(**a), _draft_fingerprint(**b))

    # -- the pre-round-16 delimiter-joined encoding let differently-shaped
    # operator text collide into the same pre-hash string (#328 review
    # round 16) -------------------------------------------------------------
    def test_draft_games_team_name_split_does_not_collide(self):
        """A team name containing the OLD encoding's own "|" delimiter
        could make two genuinely different (home_team_name,
        away_team_name) pairs concatenate to the identical string: "A|B"
        home + "C" away, versus "A" home + "B|C" away, both joined
        f"{home}|{away}" as "A|B|C". A real-world equivalent: an operator
        splits a combined club/team name differently across two teams in a
        repeat CSV import. The fingerprint must still distinguish them."""
        a = self._base_args()
        b = self._base_args()
        a["draft_games"] = [dict(a["draft_games"][0], home_team_name="A|B",
                                 away_team_name="C")]
        b["draft_games"] = [dict(b["draft_games"][0], home_team_name="A",
                                 away_team_name="B|C")]
        self.assertNotEqual(_draft_fingerprint(**a), _draft_fingerprint(**b))

    def test_already_scheduled_team_name_split_does_not_collide(self):
        a = self._base_args()
        b = self._base_args()
        row = {"division_id": "d1", "home_team_id": "t0", "away_team_id": "t3",
               "existing_game_id": "g1"}
        a["already_scheduled"] = [dict(row, home_team_name="A|B",
                                       away_team_name="C")]
        b["already_scheduled"] = [dict(row, home_team_name="A",
                                       away_team_name="B|C")]
        self.assertNotEqual(_draft_fingerprint(**a), _draft_fingerprint(**b))

    def test_unscheduled_team_name_split_does_not_collide(self):
        a = self._base_args()
        b = self._base_args()
        a["unscheduled"] = [dict(a["unscheduled"][0], home_team_name="A|B",
                                 away_team_name="C")]
        b["unscheduled"] = [dict(b["unscheduled"][0], home_team_name="A",
                                 away_team_name="B|C")]
        self.assertNotEqual(_draft_fingerprint(**a), _draft_fingerprint(**b))

    def test_reason_codes_order_does_not_matter(self):
        """The round-16 refactor still embeds reason_codes as an explicit
        sorted JSON array, not a delimiter-joined string -- confirm this
        semantically-unordered set stays order-independent, the same
        property test_team_ids_order_does_not_matter proves for team_ids."""
        a = self._base_args()
        b = self._base_args()
        a["unscheduled"] = [dict(a["unscheduled"][0],
                                 reason_codes=["no_ice_available", "team_rest_violation"])]
        b["unscheduled"] = [dict(b["unscheduled"][0],
                                 reason_codes=["team_rest_violation", "no_ice_available"])]
        self.assertEqual(_draft_fingerprint(**a), _draft_fingerprint(**b))

    def test_identical_inputs_are_deterministic(self):
        a = self._base_args()
        b = self._base_args()
        self.assertEqual(_draft_fingerprint(**a), _draft_fingerprint(**b))

    def test_team_ids_order_does_not_matter(self):
        a = self._base_args()
        b = self._base_args()
        b["team_ids"] = list(reversed(a["team_ids"]))
        self.assertEqual(_draft_fingerprint(**a), _draft_fingerprint(**b))


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
        "never attempted").

        #328 review round 10 finding 3: rows/slots/audit disappearing is
        not the whole rollback contract -- a rollback that restores those
        but leaks the five ``next_id("game")`` increments would still pass
        every assertion above, permanently burning five ids and leaking
        the Memory ``_counters``/SQL ``counters`` mutation out of the
        transaction it belongs to (a real backend-parity gap: the Memory
        store's own comment on ``_counters`` says it must roll back like
        any other state; nothing previously proved the SQL ``counters``
        table does the same). So the injection point also captures each
        earlier row's actual allocated Game id, in allocation order, and
        once the transaction has unwound, a fresh ``next_id("game")`` call
        must return exactly the FIRST of those -- proving the counter
        itself rolled back to its pre-transaction value, not just the
        rows built on top of it."""
        self._division_fixture(4, 6)
        api = api_cls(self.store)
        preview = api.draft_season_schedule("div1")
        rows = preview["draft_games"]
        self.assertEqual(len(rows), 6, repr(preview))
        earlier_rows, last_row = rows[:-1], rows[-1]
        real_assert = api.setup._assert_slot_free_for_game
        observed_before_injection = {}
        tentative_game_ids = []

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
                    if games:
                        tentative_game_ids.append(games[0].id)
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
        self.assertEqual(len(tentative_game_ids), 5, repr(tentative_game_ids))
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
        # #328 review round 10 finding 3: the id COUNTER must roll back
        # too, not just the rows built on it -- next_id("game") must hand
        # out the SAME id the failed transaction first tentatively
        # allocated, proving the counter mutation was part of the rolled
        # back transaction rather than escaping it.
        self.assertEqual(
            self.store.next_id("game"), tentative_game_ids[0],
            "next_id(\"game\") must return the id the failed transaction "
            "first tentatively allocated -- a different (higher) value "
            "means the counter update escaped the rollback")

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

    # -- fingerprint must bind team eligibility too, not just placed/
    # already-scheduled rows (#328 review round 10 finding 1) --------------
    def _stale_preview_refused_on_team_eligibility_change(self, api_cls, change):
        """Shared assertion: the ELIGIBLE-TEAM set changing between Generate
        and Commit must invalidate the preview even when the placed row and
        its own fingerprint-bound placement stay byte-for-byte identical --
        #326 review round 10: a 4-team division with exactly one ice slot
        places one pairing (the circle method's `fixed` team vs the last
        team in sort order -- see round_robin_pairings) and leaves five
        unscheduled. ``change`` exploits the SAME circle-method mechanic in
        both directions to keep that one placed row unchanged while
        team_count/the unscheduled set silently change size:

        * ``"register"`` -- a NEW team sorting alphabetically BEFORE every
          existing team joins the division. It becomes the new `fixed`
          team and byes in round 0 (the total goes 4 -> 5, even -> odd), so
          the original four teams merely shift up one array position each
          and t0-vs-t3 remains round 0's first real pairing -- unchanged on
          the wire, yet the reviewed batch silently grew four new pairings
          the operator never saw.
        * ``"unregister"`` -- the fixture starts with that same extra team
          already registered (so t0-vs-t3 is again round 0's first real
          pairing, the extra team byeing instead), then removes it. The
          placed row is unaffected by the SAME mechanic in reverse, but
          team_count/unscheduled shrink -- pairings the operator reviewed
          as genuinely missing may no longer be real obligations at all.

        Without round 10's team_ids/unscheduled binding, both directions
        leave `missing`/`already_scheduled` -- and so the fingerprint --
        completely unchanged, and Commit would silently persist a batch the
        operator never actually reviewed."""
        self._division_fixture(4, 1)  # t0..t3, 1 slot
        if change == "unregister":
            self.store.add_team(Team(id="a_extra", name="Extra",
                                     program_id="prog1", league_id="lg1"))
            self.store.add_season_team_registration(SeasonTeamRegistration(
                id="streg_a_extra", league_season_id="ls_lg1_se1",
                team_id="a_extra", division_id="div1", active=True))
        api = api_cls(self.store)
        preview = api.draft_season_schedule("div1")
        self.assertEqual(len(preview["draft_games"]), 1, repr(preview))
        placed = preview["draft_games"][0]
        self.assertEqual(
            (placed["home_team_id"], placed["away_team_id"]), ("t0", "t3"),
            repr(preview))
        if change == "register":
            self.assertEqual(preview["team_count"], 4, repr(preview))
            self.assertEqual(len(preview["unscheduled"]), 5, repr(preview))
        else:
            assert change == "unregister"
            self.assertEqual(preview["team_count"], 5, repr(preview))
            self.assertEqual(len(preview["unscheduled"]), 9, repr(preview))
        stale_fingerprint = preview["draft_fingerprint"]
        if change == "register":
            self.store.add_team(Team(id="a_extra", name="Extra",
                                     program_id="prog1", league_id="lg1"))
            self.store.add_season_team_registration(SeasonTeamRegistration(
                id="streg_a_extra", league_season_id="ls_lg1_se1",
                team_id="a_extra", division_id="div1", active=True))
        else:
            reg = self.store.get_season_team_registration("streg_a_extra")
            reg.active = False
            self.store.save_season_team_registration(reg)
        # Confirm the fresh regeneration really does still place the exact
        # same row -- otherwise this fixture wouldn't isolate the
        # eligibility axis from the (already separately covered) placement
        # axis, and the OLD fingerprint would differ for an unrelated reason.
        fresh = api.draft_season_schedule("div1")
        self.assertEqual(len(fresh["draft_games"]), 1, repr(fresh))
        self.assertEqual(
            (fresh["draft_games"][0]["home_team_id"],
             fresh["draft_games"][0]["away_team_id"]), ("t0", "t3"),
            repr(fresh))
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

    def test_league_scoped_commit_refuses_stale_preview_after_team_registers(
            self):
        self._stale_preview_refused_on_team_eligibility_change(
            ApiService, "register")

    def test_league_scoped_commit_refuses_stale_preview_after_team_unregisters(
            self):
        self._stale_preview_refused_on_team_eligibility_change(
            ApiService, "unregister")

    def test_base_facade_commit_refuses_stale_preview_after_team_registers(
            self):
        self._stale_preview_refused_on_team_eligibility_change(
            BaseApiService, "register")

    def test_base_facade_commit_refuses_stale_preview_after_team_unregisters(
            self):
        self._stale_preview_refused_on_team_eligibility_change(
            BaseApiService, "unregister")

    # -- fingerprint must bind the unscheduled reason TEXT, not just its
    # code (#328 review round 11 finding 1) -----------------------------
    def _stale_preview_refused_on_unscheduled_reason_change(self, api_cls):
        """Shared assertion: a scheduling-POLICY threshold change between
        Generate and Commit can rewrite the human-readable `reason` an
        unscheduled row shows -- the number embedded in the message -- even
        though `reason_codes` (a fixed category) and every placed/
        already-scheduled row stay byte-for-byte identical. #328 review
        round 11: a 4-team division with one 60-minute slot (always
        playable) and one 30-minute slot (never, at either threshold used
        here) places exactly one pairing on the 60-minute slot and leaves
        the other five citing `insufficient_playable_time` against the
        30-minute slot. Raising `min_playable_minutes` from 45 to 50
        changes each of those five rows' displayed "requires at least N"
        text without moving the placed row, changing which pairings are
        unscheduled, or changing any reason CODE -- exactly the gap round
        10's `reason_codes`-only binding left open."""
        self._division_fixture(4, 0)
        self.store.add_ice_slot(IceSlot(
            id="slot_60", rink_id="r1", start_time=BASE_TIME,
            end_time=BASE_TIME + timedelta(minutes=60)))
        self.store.add_ice_slot(IceSlot(
            id="slot_30", rink_id="r1",
            start_time=BASE_TIME + timedelta(days=1),
            end_time=BASE_TIME + timedelta(days=1, minutes=30)))
        api = api_cls(self.store)
        r = api.set_scheduling_policy(
            scope_type="season", scope_id="se1", min_playable_minutes=45,
            actor_id="admin")
        self.assertNotIn("error", r, repr(r))
        preview = api.draft_season_schedule("div1")
        self.assertEqual(len(preview["draft_games"]), 1, repr(preview))
        self.assertEqual(preview["draft_games"][0]["ice_slot_id"], "slot_60",
                         repr(preview))
        self.assertEqual(len(preview["unscheduled"]), 5, repr(preview))
        for u in preview["unscheduled"]:
            self.assertEqual(
                u["reason_codes"], ["insufficient_playable_time"], repr(u))
            self.assertIn("at least 45", u["reason"], repr(u))
        stale_fingerprint = preview["draft_fingerprint"]
        r = api.set_scheduling_policy(
            scope_type="season", scope_id="se1", min_playable_minutes=50,
            actor_id="admin")
        self.assertNotIn("error", r, repr(r))
        # Confirm the fresh regeneration really does keep the placed row
        # and every identity/code unchanged -- otherwise this fixture
        # wouldn't isolate the reason-TEXT axis from the (already
        # separately covered) placement/eligibility axes.
        fresh = api.draft_season_schedule("div1")
        self.assertEqual(len(fresh["draft_games"]), 1, repr(fresh))
        self.assertEqual(
            fresh["draft_games"][0]["ice_slot_id"], "slot_60", repr(fresh))
        self.assertEqual(len(fresh["unscheduled"]), 5, repr(fresh))
        for u in fresh["unscheduled"]:
            self.assertEqual(
                u["reason_codes"], ["insufficient_playable_time"], repr(u))
            self.assertIn("at least 50", u["reason"], repr(u))
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

    def test_league_scoped_commit_refuses_stale_preview_after_unscheduled_reason_text_changes(
            self):
        self._stale_preview_refused_on_unscheduled_reason_change(ApiService)

    def test_base_facade_commit_refuses_stale_preview_after_unscheduled_reason_text_changes(
            self):
        self._stale_preview_refused_on_unscheduled_reason_change(
            BaseApiService)

    # -- fingerprint must bind rink identity/name, not just ice_slot_id
    # (#328 review round 12 finding 2) --------------------------------------
    def _stale_preview_refused_on_rink_rename(self, api_cls):
        """A repeat rinks/ice_slots CSV import (#95) that renames an
        EXISTING Rink -- matched by rink_code (``external_ref``), not
        name or id -- between Generate and Commit leaves ice_slot_id and
        start_time byte-for-byte identical: same slot, same instant. But
        the rink_name the operator reviewed on screen, and the name about
        to be written verbatim onto the created Game's own `rink` field
        (never re-resolved from a live Rink reference at commit time),
        have already diverged. Commit must still refuse as stale."""
        self._division_fixture(4, 1)  # t0..t3, 1 slot on r1: places (t0, t3)
        rink = self.store.get_rink("r1")
        rink.external_ref = "R1"
        self.store.save_rink(rink)
        api = api_cls(self.store)
        preview = api.draft_season_schedule("div1")
        self.assertEqual(len(preview["draft_games"]), 1, repr(preview))
        self.assertEqual(
            preview["draft_games"][0]["rink_name"], "Main", repr(preview))
        placed_slot_id = preview["draft_games"][0]["ice_slot_id"]
        stale_fingerprint = preview["draft_fingerprint"]

        renamed_csv = "venue_name,rink_code,rink_name\nArena,R1,Main Renamed\n"
        import_res = api.commit_rinks_ice_slots_import(
            {"rinks_csv": renamed_csv}, actor_id="admin")
        self.assertTrue(import_res.get("committed"), repr(import_res))
        self.assertEqual(self.store.get_rink("r1").name, "Main Renamed")
        # Confirm the fresh regeneration really does keep the placed row's
        # identity (same pairing, same slot) unchanged -- otherwise this
        # fixture wouldn't isolate the rink-NAME axis from the (already
        # separately covered) placement axis.
        fresh = api.draft_season_schedule("div1")
        self.assertEqual(len(fresh["draft_games"]), 1, repr(fresh))
        self.assertEqual(
            fresh["draft_games"][0]["ice_slot_id"], placed_slot_id,
            repr(fresh))
        self.assertEqual(
            fresh["draft_games"][0]["rink_name"], "Main Renamed", repr(fresh))

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

    def test_league_scoped_commit_refuses_stale_preview_after_rink_renamed(
            self):
        self._stale_preview_refused_on_rink_rename(ApiService)

    def test_base_facade_commit_refuses_stale_preview_after_rink_renamed(
            self):
        self._stale_preview_refused_on_rink_rename(BaseApiService)

    # -- the candidate-Rink set must be re-verified again AFTER the Season
    # lock, not just after the Rink lock (#328 review round 14) -------------
    def _new_rink_access_and_slot_after_rink_lock_forces_raced(self, api_cls):
        """`_batch_rinks` (round 13) is computed and locked BEFORE the
        Season lock (Program->Team->Rink->Season order). A concurrent
        grant_season_venue_access (Season-lock only) followed by
        create_ice_slot on a brand-new Rink (Rink-lock only) can therefore
        land in the exact gap between `_batch_rinks`'s computation and this
        transaction's own Season lock a few statements later -- the new
        Rink's row was never locked, and the Season lock that could have
        serialized against the grant is not held yet either.

        Forces this deterministically, without threads: both writes are
        applied directly against the store (matching
        `_venue_scope_change_after_internal_regen_refused`'s technique,
        not `grant_season_venue_access`/`create_ice_slot` themselves, which
        would each try to open their OWN transaction on the SAME store
        while this one is already open) as a side effect of a monkeypatched
        `self.setup._lock_seasons`'s FIRST call -- the exact point
        production reaches this gap, immediately before attempting its own
        Season lock. `_division_fixture()`'s default 4 teams/6 slots is
        fully saturated (6 pairings exactly fill r1's 6 slots), so the new
        Rink's new slot -- deliberately far in the future -- is never
        actually needed by the round-robin; this isolates whether the
        re-verification fires from whether the outcome happens to change."""
        self._division_fixture()  # 4 teams/6 slots on r1: fully saturated
        self.store.add_venue(Venue(
            id="v_new14", name="New14", organization_id="org1"))
        self.store.add_rink(Rink(id="r_new14", venue_id="v_new14", name="R14"))
        api = api_cls(self.store)
        real_lock_seasons = api.setup._lock_seasons
        calls = [0]

        def _mutate_then_delegate(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                self.store.add_season_venue_access(SeasonVenueAccess(
                    id="sva_new14", season_id="se1", venue_id="v_new14",
                    active=True))
                self.store.add_ice_slot(IceSlot(
                    id="slot_new14", rink_id="r_new14",
                    start_time=BASE_TIME + timedelta(days=200),
                    end_time=BASE_TIME + timedelta(days=200, hours=1),
                    slot_type=IceSlotType.GAME, status=IceSlotStatus.AVAILABLE))
            return real_lock_seasons(*args, **kwargs)

        api.setup._lock_seasons = _mutate_then_delegate
        result = commit_fresh_draft(api, "div1")
        self.assertNotIn("error", result, repr(result))
        self.assertGreaterEqual(
            calls[0], 2, f"the post-Season-lock candidate-Rink "
            f"re-verification never forced a retry -- calls={calls}")

    def test_league_scoped_commit_forces_retry_after_new_rink_access_and_slot_race(
            self):
        self._new_rink_access_and_slot_after_rink_lock_forces_raced(ApiService)

    def test_base_facade_commit_forces_retry_after_new_rink_access_and_slot_race(
            self):
        self._new_rink_access_and_slot_after_rink_lock_forces_raced(
            BaseApiService)

    # -- fingerprint must bind team display names, not just team ids
    # (#328 review round 15 finding 1) --------------------------------------
    def _stale_preview_refused_on_team_rename(self, api_cls):
        """A repeat teams/players CSV import (#92) that renames an EXISTING
        Team -- matched by team_code (``external_ref``), not name or id --
        between Generate and Commit leaves every id-keyed field
        byte-for-byte identical: same team, same pairing, same slot. But
        the team_name the operator reviewed on screen, and the name about
        to be written verbatim onto the created Game (never re-resolved
        from a live Team reference at commit time -- exactly like round 12
        finding 2's rink_name), have already diverged. Commit must still
        refuse as stale.

        ``division_name`` is included in the repeat row (matching the
        EXISTING registration's own Division name, "D1") so the import's
        idempotent registration upsert leaves t0's division/league-season
        untouched -- an omitted division_name would resolve to None and
        silently unregister t0 from div1, perturbing the fixture on an axis
        this test does not intend to exercise."""
        self._division_fixture(4, 1)  # t0..t3, 1 slot on r1: places (t0, t3)
        team = self.store.get_team("t0")
        team.external_ref = "T0"
        self.store.save_team(team)
        api = api_cls(self.store)
        preview = api.draft_season_schedule("div1")
        self.assertEqual(len(preview["draft_games"]), 1, repr(preview))
        self.assertEqual(
            preview["draft_games"][0]["home_team_name"], "Team 0", repr(preview))
        placed_slot_id = preview["draft_games"][0]["ice_slot_id"]
        stale_fingerprint = preview["draft_fingerprint"]

        renamed_csv = ("team_code,team_name,division_name\n"
                       "T0,Team Zero Renamed,D1\n")
        import_res = api.commit_teams_players_import(
            "se1", {"teams_csv": renamed_csv}, actor_id="admin")
        self.assertTrue(import_res.get("committed"), repr(import_res))
        self.assertEqual(self.store.get_team("t0").name, "Team Zero Renamed")
        # Confirm the fresh regeneration really does keep the placed row's
        # identity (same pairing, same slot) unchanged -- otherwise this
        # fixture wouldn't isolate the team-NAME axis from the (already
        # separately covered) placement/eligibility axes.
        fresh = api.draft_season_schedule("div1")
        self.assertEqual(len(fresh["draft_games"]), 1, repr(fresh))
        self.assertEqual(
            fresh["draft_games"][0]["ice_slot_id"], placed_slot_id, repr(fresh))
        self.assertEqual(
            fresh["draft_games"][0]["home_team_name"], "Team Zero Renamed",
            repr(fresh))

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

    def test_league_scoped_commit_refuses_stale_preview_after_team_renamed(
            self):
        self._stale_preview_refused_on_team_rename(ApiService)

    def test_base_facade_commit_refuses_stale_preview_after_team_renamed(
            self):
        self._stale_preview_refused_on_team_rename(BaseApiService)

    # -- the general locked fingerprint recheck must win over the narrower
    # per-slot Season-eligibility refusal (#328 review round 15 finding 2)
    # -------------------------------------------------------------------
    def _venue_scope_change_after_internal_regen_refused(self, api_cls, change):
        """Shared assertion: a SeasonVenueAccess revoke or a Rink->Venue
        reassignment landing AFTER commit's own wide-gate regeneration (the
        call the top-level `draft_fingerprint` check compares against) but
        BEFORE its locked regeneration must classify as `preview_stale` --
        the same general recheck round 11/12 already established for every
        OTHER fingerprint-bound dimension -- not the narrower
        `require_slots_belong_to_locked_season`'s own `venue_access_missing`,
        which the Scheduler UI's stale-preview recovery does not recognise.

        Forces this exact ordering deterministically, without threads. A
        SeasonVenueAccess revoke or Rink reassignment needs the same
        Rink+Season locks this transaction takes (`_require_active_season`
        / a Rink row lock), so a real race can only ever land it BEFORE
        this transaction's own locks are acquired -- by the time we are
        anywhere inside the transaction, every check observes the SAME
        already-mutated state uniformly, and it is purely the SOURCE-CODE
        ordering of which check runs first that decides which reason
        surfaces. Reproduced here by applying the mutation as a side
        effect of the monkeypatched `draft_season_schedule`'s FIRST call
        (the wide gate) -- which still returns the pre-mutation
        `frozen_preview` so the wide gate's own comparison, unaffected by
        this fix, keeps passing -- rather than gating it behind a later
        call count: the store must already reflect the new state by the
        time the transaction opens, exactly like a lock-blocked concurrent
        writer that fully committed before we started."""
        self._division_fixture(4, 1)  # t0..t3, 1 slot on r1: places (t0, t3)
        api = api_cls(self.store)
        frozen_preview = api.draft_season_schedule("div1")
        self.assertEqual(len(frozen_preview["draft_games"]), 1,
                         repr(frozen_preview))
        stale_fingerprint = frozen_preview["draft_fingerprint"]

        real_draft_season_schedule = api.draft_season_schedule
        calls = [0]

        def _mutate_then_delegate(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                if change == "revoke":
                    access = self.store.season_venue_access_for_pair(
                        "se1", "v1")
                    access.active = False
                    self.store.save_season_venue_access(access)
                else:
                    self.store.add_venue(Venue(
                        id="v2", name="Other Arena", organization_id="org1"))
                    rink = self.store.get_rink("r1")
                    rink.venue_id = "v2"
                    self.store.save_rink(rink)
                return frozen_preview
            return real_draft_season_schedule(*args, **kwargs)

        api.draft_season_schedule = _mutate_then_delegate
        games_before = len(self.store.all_games())
        audits_before = len(self.store.all_setup_audit())
        res = api.commit_draft_schedule(
            "div1", draft_fingerprint=stale_fingerprint)
        self.assertEqual(calls[0], 2, repr(calls))
        self.assertIn("error", res, repr(res))
        self.assertEqual(
            res["error"]["details"]["reason"], "preview_stale", repr(res))
        self.assertEqual(len(self.store.all_games()), games_before, repr(res))
        self.assertEqual(
            len(self.store.all_setup_audit()), audits_before, repr(res))

    def test_league_scoped_commit_refuses_preview_stale_after_venue_access_revoked_before_locked_regen(
            self):
        self._venue_scope_change_after_internal_regen_refused(
            ApiService, "revoke")

    def test_base_facade_commit_refuses_preview_stale_after_venue_access_revoked_before_locked_regen(
            self):
        self._venue_scope_change_after_internal_regen_refused(
            BaseApiService, "revoke")

    def test_league_scoped_commit_refuses_preview_stale_after_rink_reparented_before_locked_regen(
            self):
        self._venue_scope_change_after_internal_regen_refused(
            ApiService, "reparent")

    def test_base_facade_commit_refuses_preview_stale_after_rink_reparented_before_locked_regen(
            self):
        self._venue_scope_change_after_internal_regen_refused(
            BaseApiService, "reparent")

    # -- the fingerprint's delimiter-joined encoding let a differently-split
    # team-name pair produce the SAME pre-hash string (#328 review round 16)
    # -------------------------------------------------------------------
    def _stale_preview_refused_on_delimiter_colliding_team_rename(self, api_cls):
        """The exact real-world reproduction of round 16's finding: a
        repeat teams/players CSV import (#92) renames BOTH of a placed
        pairing's teams in the SAME import, from ("A|B", "C") to
        ("A", "B|C") -- splitting where the "|" delimiter falls
        differently across the two names. Under the pre-round-16 encoding
        (f"{home_team_name}|{away_team_name}") both states concatenate to
        the identical "A|B|C", so the fingerprint would have stayed
        unchanged despite two genuinely different reviewed matchup labels.
        Commit must still refuse as stale.

        Uses a 2-TEAM fixture (`_division_fixture(2, 1)`), not the usual
        4-team one: with more teams, the renamed pair would ALSO each
        appear in OTHER pairings (vs a third/fourth team whose own name is
        unchanged), and those OTHER rows' strings would legitimately
        differ before/after regardless of this fix -- masking the specific
        collision under test entirely (confirmed empirically while
        designing this test: a 4-team version of this same test stayed
        green even with the round-16 fix fully reverted). With only two
        teams, their one pairing is the ONLY row in the whole proposal, so
        nothing else can change the fingerprint out from under the
        isolated axis this test exercises."""
        self._division_fixture(2, 1)  # t0, t1, 1 slot on r1: places (t0, t1)
        home = self.store.get_team("t0")
        away = self.store.get_team("t1")
        home.name, home.external_ref = "A|B", "T0"
        away.name, away.external_ref = "C", "T1"
        self.store.save_team(home)
        self.store.save_team(away)
        api = api_cls(self.store)
        preview = api.draft_season_schedule("div1")
        self.assertEqual(len(preview["draft_games"]), 1, repr(preview))
        self.assertEqual(
            (preview["draft_games"][0]["home_team_name"],
             preview["draft_games"][0]["away_team_name"]),
            ("A|B", "C"), repr(preview))
        placed_slot_id = preview["draft_games"][0]["ice_slot_id"]
        stale_fingerprint = preview["draft_fingerprint"]

        renamed_csv = ("team_code,team_name,division_name\n"
                       "T0,A,D1\nT1,B|C,D1\n")
        import_res = api.commit_teams_players_import(
            "se1", {"teams_csv": renamed_csv}, actor_id="admin")
        self.assertTrue(import_res.get("committed"), repr(import_res))
        self.assertEqual(self.store.get_team("t0").name, "A")
        self.assertEqual(self.store.get_team("t1").name, "B|C")
        # Confirm the fresh regeneration keeps the placed row's identity
        # (same pairing, same slot) unchanged -- isolating the team-NAME
        # axis -- and that its fingerprint genuinely differs despite the
        # OLD encoding's own home|away concatenation being identical
        # either way ("A|B|C").
        fresh = api.draft_season_schedule("div1")
        self.assertEqual(len(fresh["draft_games"]), 1, repr(fresh))
        self.assertEqual(
            fresh["draft_games"][0]["ice_slot_id"], placed_slot_id, repr(fresh))
        self.assertEqual(
            (fresh["draft_games"][0]["home_team_name"],
             fresh["draft_games"][0]["away_team_name"]),
            ("A", "B|C"), repr(fresh))
        self.assertNotEqual(
            fresh["draft_fingerprint"], stale_fingerprint, repr(fresh))

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

    def test_league_scoped_commit_refuses_stale_preview_after_delimiter_colliding_team_rename(
            self):
        self._stale_preview_refused_on_delimiter_colliding_team_rename(ApiService)

    def test_base_facade_commit_refuses_stale_preview_after_delimiter_colliding_team_rename(
            self):
        self._stale_preview_refused_on_delimiter_colliding_team_rename(
            BaseApiService)

    # -- ALL fingerprint-bound state must be revalidated under the lock, not
    # just draft_games/already_scheduled row identity (#328 review round 11
    # finding 2) -------------------------------------------------------------
    def _team_eligibility_change_after_internal_regen_refused(
            self, api_cls, change):
        """Shared assertion: a team registering or unregistering AFTER
        commit's OWN internal pre-lock regeneration (the call the wide
        `draft_fingerprint` gate compares against) but BEFORE its locked
        regeneration (the round-11-added general recheck) is invisible to
        every check that only re-examines `draft_games`/`already_scheduled`
        row identity -- the wide gate only ever compares against ITS OWN
        regeneration (which ran before the change), and the changed team
        need not appear in either bucket at all (it only affects the
        `unscheduled` portion).

        Forces this exact ordering deterministically, without threads: a
        monkeypatched `draft_season_schedule` returns the SAME frozen
        proposal on its first call (modeling the wide gate's regeneration
        running BEFORE the registration change lands), applies the
        registration change on the second call, then delegates to the REAL
        function from then on -- so commit's own locked regeneration (its
        second internal call) is the first one to observe the new state,
        exactly reproducing the gap the finding describes."""
        self._division_fixture(4, 1)  # t0..t3, 1 slot: places (t0, t3)
        if change == "unregister":
            self.store.add_team(Team(id="a_extra", name="Extra",
                                     program_id="prog1", league_id="lg1"))
            self.store.add_season_team_registration(SeasonTeamRegistration(
                id="streg_a_extra", league_season_id="ls_lg1_se1",
                team_id="a_extra", division_id="div1", active=True))
        api = api_cls(self.store)
        frozen_preview = api.draft_season_schedule("div1")
        self.assertEqual(len(frozen_preview["draft_games"]), 1,
                         repr(frozen_preview))
        self.assertEqual(
            (frozen_preview["draft_games"][0]["home_team_id"],
             frozen_preview["draft_games"][0]["away_team_id"]), ("t0", "t3"),
            repr(frozen_preview))
        stale_fingerprint = frozen_preview["draft_fingerprint"]

        real_draft_season_schedule = api.draft_season_schedule
        calls = [0]

        def _apply_change_then_delegate(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                return frozen_preview
            if calls[0] == 2:
                if change == "register":
                    self.store.add_team(Team(
                        id="a_extra", name="Extra",
                        program_id="prog1", league_id="lg1"))
                    self.store.add_season_team_registration(
                        SeasonTeamRegistration(
                            id="streg_a_extra",
                            league_season_id="ls_lg1_se1",
                            team_id="a_extra", division_id="div1",
                            active=True))
                else:
                    reg = self.store.get_season_team_registration(
                        "streg_a_extra")
                    reg.active = False
                    self.store.save_season_team_registration(reg)
            return real_draft_season_schedule(*args, **kwargs)

        api.draft_season_schedule = _apply_change_then_delegate
        games_before = len(self.store.all_games())
        audits_before = len(self.store.all_setup_audit())
        res = api.commit_draft_schedule(
            "div1", draft_fingerprint=stale_fingerprint)
        self.assertEqual(calls[0], 2, repr(calls))
        self.assertIn("error", res, repr(res))
        self.assertEqual(
            res["error"]["details"]["reason"], "preview_stale", repr(res))
        self.assertEqual(len(self.store.all_games()), games_before, repr(res))
        self.assertEqual(
            len(self.store.all_setup_audit()), audits_before, repr(res))

    def test_league_scoped_commit_refuses_when_team_registers_after_internal_regen(
            self):
        self._team_eligibility_change_after_internal_regen_refused(
            ApiService, "register")

    def test_league_scoped_commit_refuses_when_team_unregisters_after_internal_regen(
            self):
        self._team_eligibility_change_after_internal_regen_refused(
            ApiService, "unregister")

    def test_base_facade_commit_refuses_when_team_registers_after_internal_regen(
            self):
        self._team_eligibility_change_after_internal_regen_refused(
            BaseApiService, "register")

    def test_base_facade_commit_refuses_when_team_unregisters_after_internal_regen(
            self):
        self._team_eligibility_change_after_internal_regen_refused(
            BaseApiService, "unregister")

    # -- terminal reason/status/UX contract must survive the round-11 locked
    # recheck (#328 review round 12 finding 1) ------------------------------
    def _pairing_race_lands_between_wide_gate_and_locked_regen(self, api_cls):
        """#328 review round 12 finding 1(a): a winning exact-pairing race
        landing AFTER commit's own unlocked wide-gate regeneration but
        BEFORE its locked regeneration (round 11's general recheck) must
        still surface the specific, product-confirmed `pairing_already_
        scheduled` (naming the pairing and winning Game) -- not the general
        recheck's generic `preview_stale` -- exactly like every other timing
        of the same race (round 2/3/4). Round 11's own fix ordered the
        general recheck BEFORE the pre-existing `_existing_now`-based
        per-row check, which happened to regress this exact scenario: the
        general recheck detects the drift (a real regeneration legitimately
        moves this pairing from draft_games to already_scheduled) but,
        without a carve-out, could only raise its own generic reason.

        Forces the timing deterministically, without threads: a call-counted
        monkeypatched ``draft_season_schedule`` returns the SAME frozen
        proposal on its first call (the wide gate), creates a real winning
        Game for the proposal's own pairing on a DIFFERENT slot on the
        second call (the locked regeneration), then delegates to the REAL
        function -- so the locked regeneration is the first call to observe
        the new state.

        The winning Game must exist BEFORE ``commit_draft_schedule`` is even
        called, exactly like ``_pairing_race_wins_over_physical_conflict``'s
        established technique -- not injected mid-flight from within the
        losing attempt's OWN transaction (an earlier version of this test
        did that via a ``_guard_active_seasons`` hook, and the store's
        transactional rollback on the eventual raise silently erased the
        "winning" Game along with the loser's own tentative writes, since
        both were mutations of the SAME transaction). A call-counted
        monkeypatched ``draft_season_schedule`` then freezes ONLY the FIRST
        (wide-gate) call to the pre-race snapshot -- simulating a wide gate
        that legitimately ran a moment before the race landed, even though
        the winner already durably exists in the store by the time anyone
        looks again -- and lets every later call (the locked regeneration)
        run for REAL, so it genuinely observes the winner and legitimately
        moves this pairing from ``draft_games`` to ``already_scheduled``,
        differing from ``draft_fingerprint``.

        Uses a FULLY saturated round-robin (``_division_fixture(4, 6)``,
        6 slots for 4 teams' 6 pairings -- mirrors
        ``_pairing_race_wins_over_physical_conflict``'s established
        technique) so the extra far-away winning-Game slot added below
        cannot itself change the wide gate's own fresh regeneration: with
        every pairing already placed, one more never-consumed slot changes
        nothing about ``draft_games``/``unscheduled``/the fingerprint. The
        raced pairing is the LAST of the 6 rows, proving the whole batch
        (not merely the bad row) rolls back."""
        self._division_fixture(4, 6)  # t0..t3, 6 slots: all 6 pairings placed
        api = api_cls(self.store)
        frozen_preview = api.draft_season_schedule("div1")
        self.assertEqual(len(frozen_preview["draft_games"]), 6,
                         repr(frozen_preview))
        row = frozen_preview["draft_games"][-1]
        stale_fingerprint = frozen_preview["draft_fingerprint"]

        # Strictly LATER than every fixture slot (which run BASE_TIME ..
        # BASE_TIME+5d): _available_game_slots sorts earliest-first, so a
        # slot any earlier could itself get assigned to a pairing instead
        # of one of the fixture's own 6 slots, silently changing WHICH
        # ice_slot_id each pairing lands on (round 7 binds that too) and
        # tripping the wide gate before the transaction even opens -- not
        # the scenario this test targets.
        far_start = BASE_TIME + timedelta(days=100)
        self.store.add_rink(Rink(id="r_r12a", venue_id="v1", name="Race12a"))
        self.store.add_ice_slot(IceSlot(
            id="r12a_slot", rink_id="r_r12a", start_time=far_start,
            end_time=far_start + timedelta(hours=1)))
        winning_game_id = "r12a_winner"
        self.store.add_game(Game(
            id=winning_game_id, home_team_id=row["home_team_id"],
            away_team_id=row["away_team_id"], start_time=far_start,
            end_time=far_start + timedelta(hours=1),
            ice_slot_id="r12a_slot", division_id="div1", season_id="se1",
            league_id="lg1", league_season_id="ls_lg1_se1"))
        winner_slot = self.store.get_ice_slot("r12a_slot")
        winner_slot.status = IceSlotStatus.ALLOCATED
        self.store.save_ice_slot(winner_slot)

        real_draft_season_schedule = api.draft_season_schedule
        calls = [0]

        def _freeze_wide_gate_only(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                return frozen_preview
            return real_draft_season_schedule(*args, **kwargs)

        api.draft_season_schedule = _freeze_wide_gate_only
        games_before = len(self.store.all_games())
        audits_before = len(self.store.all_setup_audit())
        res = api.commit_draft_schedule(
            "div1", draft_fingerprint=stale_fingerprint)
        self.assertGreaterEqual(calls[0], 2, repr(calls))
        self.assertIn("error", res, repr(res))
        self.assertEqual(
            res["error"]["details"]["reason"], "pairing_already_scheduled",
            repr(res))
        self.assertEqual(
            res["error"]["details"]["existing_game_id"], winning_game_id,
            repr(res))
        self.assertIn(row["home_team_name"], res["error"]["message"])
        self.assertIn(row["away_team_name"], res["error"]["message"])
        # Atomic rollback: only the pre-existing winning Game remains -- the
        # five EARLIER rows this loser's own transaction tentatively
        # created/allocated before reaching the raced (last) row are back
        # to AVAILABLE, and no batch audit landed.
        self.assertEqual(len(self.store.all_games()), games_before, repr(res))
        for r in frozen_preview["draft_games"][:-1]:
            self.assertEqual(
                self.store.get_ice_slot(r["ice_slot_id"]).status,
                IceSlotStatus.AVAILABLE, r["ice_slot_id"])
        self.assertEqual(
            len(self.store.all_setup_audit()), audits_before, repr(res))

    def test_league_scoped_commit_pairing_race_before_locked_regen_yields_pairing_already_scheduled(
            self):
        self._pairing_race_lands_between_wide_gate_and_locked_regen(
            ApiService)

    def test_base_facade_commit_pairing_race_before_locked_regen_yields_pairing_already_scheduled(
            self):
        self._pairing_race_lands_between_wide_gate_and_locked_regen(
            BaseApiService)

    def _placed_team_eligibility_visible_before_either_locked_check(
            self, api_cls):
        """#328 review round 12 finding 1(b): once this transaction holds
        the Season lock, a team's registration state is settled for the
        rest of the transaction -- a concurrent unregister needs that SAME
        lock, so it has either already committed (visible to every
        subsequent read) or is blocked behind us. Which of the two locked
        checks happens to run FIRST therefore decides the operator-facing
        reason: `_require_batch_team_participation`'s DivisionMismatchError
        reasons (e.g. `team_not_registered`) are not recognised by app.js's
        stale-preview recovery, while the general fingerprint recheck's
        `preview_stale` is.

        Deliberately unregisters t3 -- one of the ALREADY-PLACED pairing's
        own teams -- not a bystander: `_require_batch_team_participation`
        only re-validates teams actually named in `draft_games`'s rows, so
        a bystander team's registration changing (round 10/11's own
        fixtures, which register a BYE-taking team the round-robin never
        places) can never reach it at all in EITHER ordering -- only the
        general recheck's team_ids/unschedulable_teams fingerprint binding
        (round 10) ever catches that. Only a change to a team already IN
        the batch can make `_require_batch_team_participation` itself have
        something to say, which is what makes this fixture load-bearing
        for isolating the ordering axis specifically.

        Applying the change right after the Season lock is acquired
        (`_guard_active_seasons`, which both checks run strictly after) --
        rather than tying it to either check's own execution, the way
        round 11's own regression test does -- makes both checks observe
        the identical already-changed state and isolates pure
        ordering/precedence, exactly the axis this finding is about."""
        self._division_fixture(4, 1)  # places (t0, t3)
        api = api_cls(self.store)
        frozen_preview = api.draft_season_schedule("div1")
        self.assertEqual(len(frozen_preview["draft_games"]), 1,
                         repr(frozen_preview))
        self.assertEqual(
            (frozen_preview["draft_games"][0]["home_team_id"],
             frozen_preview["draft_games"][0]["away_team_id"]), ("t0", "t3"),
            repr(frozen_preview))
        stale_fingerprint = frozen_preview["draft_fingerprint"]

        real_guard = api._guard_active_seasons
        applied = [False]

        def _unregister_t3_then_guard(*args, **kwargs):
            result = real_guard(*args, **kwargs)
            if not applied[0]:
                applied[0] = True
                reg = self.store.get_season_team_registration("streg_t3")
                reg.active = False
                self.store.save_season_team_registration(reg)
            return result

        api._guard_active_seasons = _unregister_t3_then_guard
        games_before = len(self.store.all_games())
        audits_before = len(self.store.all_setup_audit())
        res = api.commit_draft_schedule(
            "div1", draft_fingerprint=stale_fingerprint)
        self.assertTrue(applied[0])
        self.assertIn("error", res, repr(res))
        self.assertEqual(
            res["error"]["details"]["reason"], "preview_stale", repr(res))
        self.assertEqual(len(self.store.all_games()), games_before, repr(res))
        self.assertEqual(
            len(self.store.all_setup_audit()), audits_before, repr(res))

    def test_league_scoped_commit_placed_team_unregisters_before_participation_check_yields_preview_stale(
            self):
        self._placed_team_eligibility_visible_before_either_locked_check(
            ApiService)

    def test_base_facade_commit_placed_team_unregisters_before_participation_check_yields_preview_stale(
            self):
        self._placed_team_eligibility_visible_before_either_locked_check(
            BaseApiService)

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

    # -- configurable regular-season format (#375) -------------------------
    # Every test below runs against BOTH backends via MemorySchedulerTest and
    # DurableSchedulerTest (SQLite by default, PostgreSQL when
    # TEST_DATABASE_URL is set) — the counting genuinely crosses that
    # boundary, because `store.all_games()` is insertion-ordered in memory
    # and `ORDER BY id` in SQL.

    def _meeting_counts(self, res):
        """``{frozenset(pair): count}`` over a proposal's PLACED rows —
        what the format actually asked the ice to host."""
        counts = {}
        for d in res["draft_games"]:
            key = frozenset((d["home_team_id"], d["away_team_id"]))
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _orientations(self, res):
        """The ordered ``(home, away)`` sequence a proposal placed — the
        home/away SPLIT itself, not just how many games each pair got."""
        return [(d["home_team_id"], d["away_team_id"])
                for d in res["draft_games"]]

    def test_one_meeting_per_opponent_is_the_historical_single_round_robin(self):
        # Anti-vacuity control for every N > 1 assertion below: the default
        # and an explicit 1 must both still be the single round-robin, so a
        # later "N meetings" assertion is proving the format changed rather
        # than reporting a number the generator produces regardless.
        self._division_fixture(4, 6)
        default = draft_schedule(self.store, "div1")
        explicit = draft_schedule(self.store, "div1", meetings_per_opponent=1)
        self.assertEqual(default, explicit)
        self.assertEqual(len(default["draft_games"]), 6)  # C(4,2)
        self.assertEqual(set(self._meeting_counts(default).values()), {1})

    def test_two_meetings_per_opponent_schedules_each_pair_twice(self):
        self._division_fixture(4, 12)  # C(4,2) x 2 = 12
        res = draft_schedule(self.store, "div1", meetings_per_opponent=2)
        self.assertEqual(res["unscheduled"], [])
        self.assertEqual(len(res["draft_games"]), 12)
        counts = self._meeting_counts(res)
        self.assertEqual(len(counts), 6)  # still exactly the 6 distinct pairs
        self.assertEqual(set(counts.values()), {2})

    def test_three_meetings_per_opponent_schedules_each_pair_three_times(self):
        self._division_fixture(4, 18)  # C(4,2) x 3 = 18
        res = draft_schedule(self.store, "div1", meetings_per_opponent=3)
        self.assertEqual(res["unscheduled"], [])
        self.assertEqual(len(res["draft_games"]), 18)
        counts = self._meeting_counts(res)
        self.assertEqual(len(counts), 6)
        self.assertEqual(set(counts.values()), {3})

    def test_six_teams_three_meetings_is_fifteen_games_per_team(self):
        # The issue's own worked example, asserted the way the issue states
        # it: not "45 rows exist" but "every team plays 15 games".
        self._division_fixture(6, 45)  # C(6,2) x 3 = 45
        res = draft_schedule(self.store, "div1", meetings_per_opponent=3)
        self.assertEqual(res["unscheduled"], [])
        self.assertEqual(len(res["draft_games"]), 45)
        per_team = {}
        for d in res["draft_games"]:
            for tid in (d["home_team_id"], d["away_team_id"]):
                per_team[tid] = per_team.get(tid, 0) + 1
        self.assertEqual(len(per_team), 6)
        self.assertEqual(set(per_team.values()), {15})
        self.assertEqual(set(self._meeting_counts(res).values()), {3})

    def test_home_away_split_is_balanced_within_one_game_per_pair(self):
        # "Balanced deterministically" has a balance half as well as a
        # determinism half. Odd N cannot split evenly, so the guarantee is
        # "within one game" -- and at even N it must be exactly even.
        self._division_fixture(4, 24)
        for meetings, allowed in ((2, {(1, 1)}), (3, {(2, 1), (1, 2)})):
            res = draft_schedule(self.store, "div1",
                                 meetings_per_opponent=meetings)
            oriented = {}
            for h, a in self._orientations(res):
                oriented[(h, a)] = oriented.get((h, a), 0) + 1
            seen = set()
            for (h, a) in list(oriented):
                if (a, h) in seen or (h, a) in seen:
                    continue
                seen.add((h, a))
                split = (oriented.get((h, a), 0), oriented.get((a, h), 0))
                self.assertIn(split, allowed,
                              f"meetings={meetings} pair={h}/{a} split={split}")

    def test_home_away_split_is_reproducible_from_the_same_inputs(self):
        # Determinism against repetition in ONE process/store.
        self._division_fixture(4, 18)
        first = draft_schedule(self.store, "div1", meetings_per_opponent=3)
        second = draft_schedule(self.store, "div1", meetings_per_opponent=3)
        self.assertEqual(self._orientations(first), self._orientations(second))
        self.assertEqual(first, second)

    def test_home_away_split_does_not_depend_on_registration_order(self):
        # The stronger determinism claim, and the one a hidden `random` /
        # set-iteration dependency would actually break: an INDEPENDENT
        # store, built with the teams registered in the reverse order, must
        # produce the byte-identical home/away split. Two separate stores
        # also means two separate id-counter sequences and two separate
        # dict insertion orders.
        self._division_fixture(4, 18)
        forward = draft_schedule(self.store, "div1", meetings_per_opponent=3)

        other = self.__class__()
        other.store = self.make_store()
        try:
            other._base()
            other.store.add_league(League(
                id="lg1", program_id="prog1", name="League"))
            other.store.add_league_season(LeagueSeason(
                id="ls_lg1_se1", league_id="lg1", season_id="se1"))
            other.store.add_division(Division(
                id="div1", league_season_id="ls_lg1_se1", name="D1"))
            for i in reversed(range(4)):  # reverse registration order
                other.store.add_team(Team(id=f"t{i}", name=f"Team {i}",
                                          program_id="prog1", league_id="lg1"))
                other.store.add_season_team_registration(
                    SeasonTeamRegistration(
                        id=f"streg_t{i}", league_season_id="ls_lg1_se1",
                        team_id=f"t{i}", division_id="div1", active=True))
            other._slots(18)
            reversed_build = draft_schedule(
                other.store, "div1", meetings_per_opponent=3)
        finally:
            conn = getattr(other.store, "conn", None)
            if conn is not None:
                conn.close()

        self.assertEqual(self._orientations(forward),
                         self._orientations(reversed_build))
        self.assertEqual(forward["draft_fingerprint"],
                         reversed_build["draft_fingerprint"])

    def _seed_first_pairing_game(self, **game_kwargs):
        """Plant ONE Game for the first round-robin pairing of the 4-team
        fixture, tagged into the fixture's own LeagueSeason/Division so the
        scope filter cannot reject it before the counting rule under test is
        ever evaluated (the vacuity trap #328's review already caught here)."""
        home, away = round_robin_pairings(["t0", "t1", "t2", "t3"])[0]
        self.store.add_game(Game(
            id=game_kwargs.pop("id", "seeded1"),
            home_team_id=home, away_team_id=away,
            start_time=BASE_TIME - timedelta(days=100),
            end_time=BASE_TIME - timedelta(days=100) + timedelta(hours=1),
            division_id="div1", season_id="se1", league_id="lg1",
            league_season_id="ls_lg1_se1", **game_kwargs))
        return frozenset((home, away))

    def test_played_regular_game_satisfies_one_of_three_meetings(self):
        # The POSITIVE control the two negatives below are measured against.
        # Without it, "a cancelled game leaves 3 outstanding" proves nothing:
        # 3 is also what a scheduler that ignored existing games entirely
        # would report.
        self._division_fixture(4, 18)
        pair = self._seed_first_pairing_game()
        res = draft_schedule(self.store, "div1", meetings_per_opponent=3)
        self.assertEqual(self._meeting_counts(res)[pair], 2)
        self.assertEqual(len(res["already_scheduled"]), 1)
        self.assertEqual(res["already_scheduled"][0]["existing_game_id"],
                         "seeded1")

    def test_cancelled_game_does_not_satisfy_a_meeting(self):
        self._division_fixture(4, 18)
        pair = self._seed_first_pairing_game(cancelled=True)
        res = draft_schedule(self.store, "div1", meetings_per_opponent=3)
        # All three meetings still outstanding -- contrast the control
        # above, where an identical but NON-cancelled game left two.
        self.assertEqual(self._meeting_counts(res)[pair], 3)
        self.assertEqual(res["already_scheduled"], [])

    def test_exhibition_game_does_not_satisfy_a_meeting(self):
        self._division_fixture(4, 18)
        pair = self._seed_first_pairing_game(
            game_type=GameType.EXHIBITION.value)
        res = draft_schedule(self.store, "div1", meetings_per_opponent=3)
        # A friendly is not a regular-season fixture, so it discharges none
        # of the three -- again contrast the control's two.
        self.assertEqual(self._meeting_counts(res)[pair], 3)
        self.assertEqual(res["already_scheduled"], [])

    def test_already_scheduled_game_ids_are_reported_backend_independently(self):
        # The Memory/SQL boundary the issue calls out. Two existing Games
        # for one pair are INSERTED in descending id order, so insertion
        # order (what the in-memory store returns) and id order (what the
        # SQL store returns) disagree. The proposal must report the same
        # ids, in the same order, either way.
        self._division_fixture(4, 18)
        home, away = round_robin_pairings(["t0", "t1", "t2", "t3"])[0]
        for gid in ("game_zz", "game_aa"):  # descending: insertion != sorted
            self.store.add_game(Game(
                id=gid, home_team_id=home, away_team_id=away,
                start_time=BASE_TIME - timedelta(days=100),
                end_time=BASE_TIME - timedelta(days=100) + timedelta(hours=1),
                division_id="div1", season_id="se1", league_id="lg1",
                league_season_id="ls_lg1_se1"))
        res = draft_schedule(self.store, "div1", meetings_per_opponent=3)
        pair = frozenset((home, away))
        reported = [a["existing_game_id"] for a in res["already_scheduled"]
                    if frozenset((a["home_team_id"], a["away_team_id"])) == pair]
        self.assertEqual(reported, ["game_aa", "game_zz"])
        self.assertEqual(self._meeting_counts(res)[pair], 1)  # 3 - 2 existing

    def test_regeneration_after_partial_schedule_adds_exactly_the_missing(self):
        self._division_fixture(4, 18)
        # Commit a single round-robin first: 6 of the 18 meetings exist.
        first = commit_fresh_draft(self.api, "div1")
        self.assertEqual(len(first["created"]), 6)
        before = {g.id for g in self.store.all_games()}
        self.assertEqual(len(before), 6)

        second = commit_fresh_draft(self.api, "div1", meetings_per_opponent=3)
        self.assertEqual(len(second["created"]), 12)  # 18 - 6, and nothing more
        after = {g.id for g in self.store.all_games()}
        self.assertEqual(len(after), 18)
        # The pre-existing six are untouched, not re-created or replaced.
        self.assertTrue(before < after)
        counts = {}
        for g in self.store.all_games():
            key = frozenset((g.home_team_id, g.away_team_id))
            counts[key] = counts.get(key, 0) + 1
        self.assertEqual(len(counts), 6)
        self.assertEqual(set(counts.values()), {3})

    def test_regeneration_twice_in_a_row_is_a_no_op(self):
        self._division_fixture(4, 18)
        first = commit_fresh_draft(self.api, "div1", meetings_per_opponent=3)
        self.assertEqual(len(first["created"]), 18)
        after_first = {g.id for g in self.store.all_games()}

        # Idempotence is proved by running it AGAIN and observing that ZERO
        # new Games appear -- not by re-asserting a total that would look
        # identical if the second run had replaced the first run's rows.
        second = commit_fresh_draft(self.api, "div1", meetings_per_opponent=3)
        self.assertEqual(second["created"], [])
        after_second = {g.id for g in self.store.all_games()}
        self.assertEqual(after_first, after_second)

        proposal = self.api.draft_season_schedule(
            division_id="div1", meetings_per_opponent=3)
        self.assertEqual(proposal["draft_games"], [])
        self.assertEqual(len(proposal["already_scheduled"]), 18)

    def test_committed_three_meeting_schedule_is_fifteen_games_per_team(self):
        # The worked example carried all the way through the facade and the
        # commit gate to persisted Games -- the end-to-end proof that the
        # format parameter survives every layer, not just the generator.
        self._division_fixture(6, 45)
        res = commit_fresh_draft(self.api, "div1", meetings_per_opponent=3)
        self.assertEqual(len(res["created"]), 45)
        per_team = {}
        for g in self.store.all_games():
            for tid in (g.home_team_id, g.away_team_id):
                per_team[tid] = per_team.get(tid, 0) + 1
        self.assertEqual(set(per_team.values()), {15})

    def test_league_wide_draft_honours_meetings_per_opponent_per_division(self):
        self._league_two_divisions_fixture(per_division=2, n_slots=8)
        res = draft_schedule_for_league(
            self.store, "se1", "lg1", meetings_per_opponent=3)
        # One pairing per Division x 3 meetings = 6, and still never a
        # cross-Division pairing: repeating each group's round-robin must
        # not start drawing opponents from the other group.
        self.assertEqual(len(res["draft_games"]), 6)
        for d in res["draft_games"]:
            self.assertIn(
                {d["home_team_id"], d["away_team_id"]},
                ({"g0", "g1"}, {"s0", "s1"}),
                "a league-wide multi-meeting draft paired across Divisions")
        self.assertEqual(set(self._meeting_counts(res).values()), {3})

    def test_commit_refuses_when_preview_used_a_different_format(self):
        # The format is bound into draft_fingerprint, so previewing N=1 and
        # committing N=3 must be refused rather than silently persisting a
        # schedule three times the size of the one reviewed.
        self._division_fixture(4, 18)
        preview = self.api.draft_season_schedule(division_id="div1")
        res = self.api.commit_draft_schedule(
            division_id="div1",
            draft_fingerprint=preview["draft_fingerprint"],
            meetings_per_opponent=3)
        self.assertEqual(res.get("error", {}).get("details", {}).get("reason"),
                         "preview_stale", repr(res))
        self.assertEqual(self.store.all_games(), [])

    def test_invalid_meetings_per_opponent_is_a_structured_error(self):
        self._division_fixture(4, 6)
        for bad in (0, -1, True, 1.5, "3", MAX_MEETINGS_PER_OPPONENT + 1):
            res = self.api.draft_season_schedule(
                division_id="div1", meetings_per_opponent=bad)
            self.assertEqual(res.get("error", {}).get("code"),
                             "validation_error", f"{bad!r}: {res!r}")


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

    # -- draft_fingerprint IS a breaking change to the Commit request
    # contract, over real HTTP (#328 review round 9) ------------------------
    def _build_committable_division(self, c, n_slots=1):
        """A small Division with two Teams and ``n_slots`` open ice slots,
        built entirely through real setup HTTP calls (not direct store
        writes) -- every id these routes hand back is freshly sequential, so
        repeated calls across tests in this class never collide.

        ``n_slots`` (#375) exists because a multi-meeting format needs one
        slot per meeting; it defaults to the single slot every pre-#375
        caller in this class already relies on."""
        def post(path, body):
            status, resp = self._req(c, "POST", path, body)
            self.assertEqual(status, 200, repr(resp))
            return resp
        league = post("/api/setup/league", {"name": "HTTP Commit Contract"})
        # #369: a setup mutation that names an EXISTING record (here the
        # venue-access grant below, whose Season AND Venue are both existing
        # records) is authorized against the caller's PERSISTED active Program,
        # not merely against its role — so select the Program this fixture just
        # created. v1 calls a Program a "league"; /api/context is canonical, so
        # the same id goes in as program_id.
        post("/api/context", {"program_id": league["id"]})
        season = post("/api/setup/season",
                      {"league_id": league["id"], "name": "HCC Season"})
        level = post("/api/setup/level",
                     {"season_id": season["id"], "name": "HCC Level"})
        division = post("/api/setup/division", {
            "season_id": season["id"], "level_id": level["id"],
            "name": "HCC Division"})
        club = post("/api/setup/club", {"name": "HCC Club"})
        # #367 prerequisite: a Team's League must belong to the caller's ACTIVE
        # Program. This class shares one server/store across test methods, so
        # the context can still resolve to a Program an earlier test created;
        # move to the one being built here before populating it. (`league` is
        # the legacy-vocabulary PROGRAM created above -- /api/setup/league.)
        post("/api/context",
             {"program_id": league["id"], "season_id": season["id"]})
        t0 = post("/api/v2/setup/team",
                  {"club_id": club["id"], "league_id": level["id"], "name": "HCC A"})
        t1 = post("/api/v2/setup/team",
                  {"club_id": club["id"], "league_id": level["id"], "name": "HCC B"})
        for team in (t0, t1):
            post(f"/api/setup/seasons/{season['id']}/team-registrations",
                 {"team_id": team["id"], "division_id": division["id"]})
        venue = post("/api/setup/venue",
                     {"name": "HCC Arena", "league_id": league["id"]})
        post(f"/api/v2/setup/seasons/{season['id']}/venue-access",
             {"venue_id": venue["id"]})
        rink = post("/api/setup/rink", {"venue_id": venue["id"], "name": "HCC Rink"})
        for day in range(n_slots):
            # A distinct DAY per slot, so two meetings of the same pair can
            # never be refused as a team double-booking (#373).
            post("/api/setup/ice-slot", {
                "rink_id": rink["id"],
                "start_time": f"2026-09-{12 + day:02d}T08:00:00+00:00",
                "end_time": f"2026-09-{12 + day:02d}T09:00:00+00:00",
                "slot_type": "game"})
        return division["id"]

    def test_commit_without_fingerprint_is_preview_required_via_http(self):
        """#328 review round 9 -- draft_fingerprint is a REQUIRED field on
        the real HTTP route, not an additive response-only change: the
        legacy payload a pre-round-5 caller would have sent (scope only, no
        fingerprint) must be refused with the documented terminal reason
        and write nothing, not silently accepted."""
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        div = self._build_committable_division(c)
        games_before = len(srv.STATE.api.store.all_games())
        status, body = self._req(
            c, "POST", "/api/scheduler/commit", {"division_id": div})
        self.assertEqual(status, 400, repr(body))
        self.assertEqual(body["error"]["code"], "validation_error", repr(body))
        self.assertEqual(
            body["error"]["details"]["reason"], "preview_required", repr(body))
        self.assertEqual(
            len(srv.STATE.api.store.all_games()), games_before, repr(body))

    def test_commit_with_exact_fingerprint_succeeds_via_http(self):
        """#328 review round 9 -- the documented, supported sequence (real
        Generate, then Commit with its exact returned fingerprint) works
        end-to-end over real HTTP, proving the new contract is usable, not
        just the legacy one refused."""
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        div = self._build_committable_division(c)
        status, preview = self._req(
            c, "POST", "/api/scheduler/draft", {"division_id": div})
        self.assertEqual(status, 200, repr(preview))
        self.assertEqual(len(preview["draft_games"]), 1, repr(preview))
        status, body = self._req(c, "POST", "/api/scheduler/commit", {
            "division_id": div,
            "draft_fingerprint": preview["draft_fingerprint"]})
        self.assertEqual(status, 200, repr(body))
        self.assertEqual(len(body["created"]), 1, repr(body))

    def test_commit_with_stale_fingerprint_is_preview_stale_via_http(self):
        """#328 review round 9 -- a fingerprint that was valid at Generate
        time but no longer matches current state (a Game created for the
        reviewed pairing in the meantime) is refused with the documented
        terminal conflict and writes nothing beyond the change that made it
        stale, over real HTTP."""
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        div = self._build_committable_division(c)
        status, preview = self._req(
            c, "POST", "/api/scheduler/draft", {"division_id": div})
        self.assertEqual(status, 200, repr(preview))
        stale_fingerprint = preview["draft_fingerprint"]
        row = preview["draft_games"][0]
        season_id = srv.STATE.api.store.get_division(div).league_season_id
        season_id = srv.STATE.api.store.get_league_season(season_id).season_id
        status, seeded = self._req(c, "POST", "/api/setup/game", {
            "season_id": season_id, "division_id": div,
            "home_team_id": row["home_team_id"],
            "away_team_id": row["away_team_id"],
            "ice_slot_id": row["ice_slot_id"]})
        self.assertEqual(status, 200, repr(seeded))
        games_before = len(srv.STATE.api.store.all_games())
        status, body = self._req(c, "POST", "/api/scheduler/commit", {
            "division_id": div, "draft_fingerprint": stale_fingerprint})
        self.assertEqual(status, 409, repr(body))
        self.assertEqual(body["error"]["code"], "concurrency_conflict", repr(body))
        self.assertEqual(
            body["error"]["details"]["reason"], "preview_stale", repr(body))
        self.assertEqual(
            len(srv.STATE.api.store.all_games()), games_before, repr(body))

    def test_meetings_per_opponent_round_trips_over_http(self):
        """#375 -- the configurable format survives the REAL transport on
        BOTH routes. The Generate route must accept it (3 rows for the one
        pairing, not 1), and the Commit route must forward the SAME value
        into its own server-side regeneration: if it dropped it, the
        regenerated proposal would be a single round-robin whose
        fingerprint could not match, and this commit would fail
        preview_stale instead of creating three Games."""
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        div = self._build_committable_division(c, n_slots=3)
        status, preview = self._req(c, "POST", "/api/scheduler/draft", {
            "division_id": div, "meetings_per_opponent": 3})
        self.assertEqual(status, 200, repr(preview))
        self.assertEqual(preview["meetings_per_opponent"], 3, repr(preview))
        self.assertEqual(len(preview["draft_games"]), 3, repr(preview))
        pairs = {frozenset((d["home_team_id"], d["away_team_id"]))
                 for d in preview["draft_games"]}
        self.assertEqual(len(pairs), 1, repr(preview))  # one pair, 3 meetings

        status, body = self._req(c, "POST", "/api/scheduler/commit", {
            "division_id": div, "meetings_per_opponent": 3,
            "draft_fingerprint": preview["draft_fingerprint"]})
        self.assertEqual(status, 200, repr(body))
        self.assertEqual(len(body["created"]), 3, repr(body))

    def test_commit_dropping_meetings_per_opponent_is_preview_stale_via_http(self):
        """#375 -- the negative partner of the round-trip above, and the
        reason the format is bound into draft_fingerprint: a Commit that
        omits the format the preview was generated with is REFUSED, not
        silently committed as a single round-robin a third of the size the
        operator reviewed."""
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        div = self._build_committable_division(c, n_slots=3)
        status, preview = self._req(c, "POST", "/api/scheduler/draft", {
            "division_id": div, "meetings_per_opponent": 3})
        self.assertEqual(status, 200, repr(preview))
        games_before = len(srv.STATE.api.store.all_games())
        status, body = self._req(c, "POST", "/api/scheduler/commit", {
            "division_id": div,  # format omitted
            "draft_fingerprint": preview["draft_fingerprint"]})
        self.assertEqual(status, 409, repr(body))
        self.assertEqual(
            body["error"]["details"]["reason"], "preview_stale", repr(body))
        self.assertEqual(
            len(srv.STATE.api.store.all_games()), games_before, repr(body))

    def test_invalid_meetings_per_opponent_is_rejected_over_http(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login", {"username": "admin", "password": "demo"})
        div = srv.STATE.api.store.all_divisions()[0].id
        for bad in (0, -2, "many", 2.5, True):
            status, body = self._req(c, "POST", "/api/scheduler/draft", {
                "division_id": div, "meetings_per_opponent": bad})
            self.assertEqual(status, 400, f"{bad!r}: {body!r}")
            self.assertEqual(body["error"]["code"], "validation_error",
                             f"{bad!r}: {body!r}")

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
