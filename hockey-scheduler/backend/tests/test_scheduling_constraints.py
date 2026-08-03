"""Scheduling constraints v1 (#85).

The draft generator (#84) now respects optional constraints: team and rink
blackout dates, a minimum rest between a team's games, and a max
games-per-team-per-day cap. Slots that violate a constraint are skipped, and a
pairing with no valid slot is returned unscheduled with the reason.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Division, IceSlot, League, LeagueSeason, Organization, Program, Rink,
    Season, SeasonTeamRegistration, SeasonVenueAccess, Team, Venue)
from hockey_scheduler.domain.errors import ValidationError
from hockey_scheduler.store import InMemoryStore
from hockey_scheduler.services import draft_schedule
from hockey_scheduler.web import server as srv

UTC = timezone.utc


def _store(n_teams, slot_times):
    """Build a division with ``n_teams`` teams and a game slot at each of
    ``slot_times`` (datetimes) on rink r1."""
    s = InMemoryStore()
    s.add_organization(Organization(id="org", name="Owner"))
    s.add_program(Program(id="league", name="League", operator_organization_id="org"))
    s.add_season(Season(id="se", program_id="league", name="Season"))
    s.add_league(League(id="lg", program_id="league", name="Div League"))
    s.add_league_season(LeagueSeason(id="ls", league_id="lg", season_id="se"))
    s.add_division(Division(id="d", league_season_id="ls", name="D"))
    s.add_venue(Venue(id="v", name="Arena", organization_id="org",
                      league_id="league"))
    s.add_season_venue_access(SeasonVenueAccess(
        id="sva1", season_id="se", venue_id="v", active=True))
    s.add_rink(Rink(id="r1", venue_id="v", name="Main"))
    for i in range(n_teams):
        s.add_team(Team(id=f"t{i}", name=f"T{i}", division="D", division_id="d",
                        program_id="league", league_id="lg"))
        # Draft scheduling reads the season registration, not division_id (#180).
        s.add_season_team_registration(SeasonTeamRegistration(
            id=f"streg_t{i}", league_season_id="ls", team_id=f"t{i}",
            division_id="d", active=True))
    for i, t in enumerate(slot_times):
        s.add_ice_slot(IceSlot(id=f"s{i}", rink_id="r1", start_time=t,
                               end_time=t + timedelta(hours=1)))
    return s


def _days(n):
    base = datetime(2026, 1, 5, 18, tzinfo=UTC)
    return [base + timedelta(days=i) for i in range(n)]


class ConstraintTest(unittest.TestCase):
    def test_no_constraints_matches_84_behavior(self):
        s = _store(4, _days(6))
        res = draft_schedule(s, "d")  # constraints default None
        self.assertEqual(len(res["draft_games"]), 6)
        self.assertEqual(res["unscheduled"], [])

    def test_team_blackout_date_is_respected(self):
        # 2 teams, one game; the only slots fall on t0's blackout day.
        day = "2026-01-05"
        s = _store(2, _days(1))
        res = draft_schedule(s, "d",
                             constraints={"team_blackouts": {"t0": [day]}})
        self.assertEqual(res["draft_games"], [])
        self.assertIn("team blackout", res["unscheduled"][0]["reason"])

    def test_team_blackout_moves_game_to_a_free_day(self):
        s = _store(2, _days(2))  # day0 and day1
        res = draft_schedule(s, "d",
                             constraints={"team_blackouts": {"t0": ["2026-01-05"]}})
        self.assertEqual(len(res["draft_games"]), 1)
        self.assertTrue(res["draft_games"][0]["start_time"].startswith("2026-01-06"))

    def test_rink_blackout_date_is_respected(self):
        s = _store(2, _days(1))
        res = draft_schedule(s, "d",
                             constraints={"rink_blackouts": {"r1": ["2026-01-05"]}})
        self.assertEqual(res["draft_games"], [])
        self.assertIn("rink blackout", res["unscheduled"][0]["reason"])

    def test_max_games_per_team_per_day(self):
        # 3 teams → 3 games; put every slot on the SAME day, cap 1/day.
        base = datetime(2026, 1, 5, 18, tzinfo=UTC)
        same_day = [base + timedelta(hours=h) for h in range(6)]
        s = _store(3, same_day)
        res = draft_schedule(s, "d",
                             constraints={"max_games_per_team_per_day": 1})
        # Each team may appear at most once across the drafted games.
        appearances = {}
        for g in res["draft_games"]:
            for tid in (g["home_team_id"], g["away_team_id"]):
                appearances[tid] = appearances.get(tid, 0) + 1
        self.assertTrue(all(v <= 1 for v in appearances.values()))
        self.assertTrue(res["unscheduled"])
        self.assertIn("max games", res["unscheduled"][0]["reason"])

    def test_min_rest_between_games_is_respected(self):
        # 3 teams, all slots a few hours apart on one day; require 48h rest.
        base = datetime(2026, 1, 5, 18, tzinfo=UTC)
        close = [base + timedelta(hours=h) for h in range(6)]
        s = _store(3, close)
        res = draft_schedule(s, "d", constraints={"min_rest_hours": 48})
        # No team plays two drafted games closer than the required rest.
        starts = {}
        for g in res["draft_games"]:
            t = datetime.fromisoformat(g["start_time"])
            for tid in (g["home_team_id"], g["away_team_id"]):
                for prev in starts.get(tid, []):
                    self.assertGreaterEqual(abs(t - prev), timedelta(hours=48))
                starts.setdefault(tid, []).append(t)

    def test_unscheduled_pairings_carry_reasons(self):
        s = _store(4, _days(2))  # 6 pairings, 2 slots
        res = draft_schedule(s, "d")
        self.assertEqual(len(res["unscheduled"]), 4)
        for u in res["unscheduled"]:
            self.assertTrue(u["reason"])


class ConstraintValidationTest(unittest.TestCase):
    """Malformed ``constraints`` input yields a structured validation_error, not
    a raw Python exception across the facade boundary (#85)."""

    def _api(self):
        return ApiService(_store(2, _days(1)))  # division "d" exists

    def test_direct_generator_raises_validation_error(self):
        s = _store(2, _days(1))
        with self.assertRaises(ValidationError):
            draft_schedule(s, "d", constraints="bad")

    def test_non_object_constraints_is_validation_error(self):
        res = self._api().draft_season_schedule("d", constraints="bad")
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_non_numeric_min_rest_is_validation_error(self):
        res = self._api().draft_season_schedule(
            "d", constraints={"min_rest_hours": "abc"})
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_non_integer_max_per_day_is_validation_error(self):
        res = self._api().draft_season_schedule(
            "d", constraints={"max_games_per_team_per_day": "abc"})
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_bad_blackout_shape_is_validation_error(self):
        # team_blackouts must be an object of id -> list, not a bare list.
        res = self._api().draft_season_schedule(
            "d", constraints={"team_blackouts": ["2026-01-05"]})
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_blackout_dates_must_be_strings(self):
        res = self._api().draft_season_schedule(
            "d", constraints={"rink_blackouts": {"r1": [20260105]}})
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_blackout_dates_must_be_strict_yyyy_mm_dd(self):
        # Loosely-formatted dates would never match the scheduler's
        # start_time.date().isoformat() and be silently ignored — reject them.
        for bad in ("2026/01/05", "not-a-date", "Jan 5", "2026-1-5"):
            res = self._api().draft_season_schedule(
                "d", constraints={"team_blackouts": {"t0": [bad]}})
            self.assertEqual(res["error"]["code"], "validation_error", bad)

    def test_valid_iso_blackout_date_is_accepted(self):
        # A canonical YYYY-MM-DD date is honored (t0's only slot is blacked out).
        res = self._api().draft_season_schedule(
            "d", constraints={"team_blackouts": {"t0": ["2026-01-05"]}})
        self.assertNotIn("error", res)
        self.assertEqual(res["draft_games"], [])
        self.assertIn("team blackout", res["unscheduled"][0]["reason"])

    def test_empty_constraints_matches_baseline(self):
        api = ApiService(_store(4, _days(6)))
        base = api.draft_season_schedule("d")
        empty = api.draft_season_schedule("d", constraints={})
        self.assertEqual(base, empty)
        self.assertEqual(len(empty["draft_games"]), 6)


class ConstraintHttpValidationTest(unittest.TestCase):
    """The scheduler route surfaces malformed constraints as a 400 error."""

    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.div_id = srv.STATE.api.store.all_divisions()[0].id
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def _post(self, body):
        url = f"http://127.0.0.1:{self.port}/api/scheduler/draft"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Demo-Role", "league_admin")  # operator
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_malformed_constraints_returns_400(self):
        status, body = self._post(
            {"division_id": self.div_id, "constraints": {"min_rest_hours": "abc"}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_malformed_blackout_date_returns_400(self):
        status, body = self._post(
            {"division_id": self.div_id,
             "constraints": {"team_blackouts": {"t0": ["2026/01/05"]}}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_valid_request_still_succeeds(self):
        status, body = self._post(
            {"division_id": self.div_id, "constraints": {}})
        self.assertEqual(status, 200)
        self.assertIn("draft_games", body)


if __name__ == "__main__":
    unittest.main()
