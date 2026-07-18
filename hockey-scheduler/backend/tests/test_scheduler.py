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

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import (
    Division,
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
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.services import (
    draft_schedule,
    draft_schedule_for_league,
    round_robin_pairings,
)
from hockey_scheduler.api import ApiService
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
        result = self.api.commit_draft_schedule(
            division_id="div1", actor_id="admin")
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
        result = self.api.commit_draft_schedule(
            season_id="se1", league_id="lg1", actor_id="admin")
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
        result = self.api.commit_draft_schedule(
            season_id="se1", league_id="lg1", division_id="div2", actor_id="admin")
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
        result = self.api.commit_draft_schedule(
            season_id="se1", league_id="lg1", actor_id="admin")
        self.assertNotIn("error", result)
        games = self.store.all_games()
        self.assertEqual(len(games), 1)  # only t0 vs t1
        for g in games:
            self.assertEqual(g.league_id, "lg1")
            self.assertIn(g.division_id, (None, "div1"))
            self.assertNotIn("bad", (g.home_team_id, g.away_team_id))


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
