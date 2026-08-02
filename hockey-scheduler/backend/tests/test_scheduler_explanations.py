"""Bounded deterministic explanations for unplaced schedule pairings (#379).

Each regression is intentionally falsifiable on one guarantee: simultaneous
causes, canonical ordering, per-pair/global caps, scope privacy, honest
alternatives, and observer-only placement equivalence.
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

from helpers import BACKEND  # noqa: F401  (ensures package path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Division,
    Game,
    IceSlot,
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
from hockey_scheduler.services import scheduler as sched
from hockey_scheduler.services.schedule_explanations import (
    MAX_ALTERNATIVES_PER_PAIRING,
    MAX_CANDIDATE_WINDOWS_PER_PAIRING,
    MAX_CANDIDATE_WINDOWS_PER_PREVIEW,
    MAX_REJECTIONS_PER_CANDIDATE,
    build_unplaced_explanation,
)
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv


UTC = timezone.utc
BASE = datetime(2026, 10, 5, 18, tzinfo=UTC)


def _seed(store, *, team_count=2, active_access=True):
    """Two authorized venues plus a completely separate private scope."""
    store.add_organization(Organization(id="org", name="Owner"))
    store.add_program(Program(
        id="pg", name="Program", operator_organization_id="org",
        timezone="UTC"))
    store.add_season(Season(id="se", program_id="pg", name="Season"))
    store.add_league(League(id="lg", program_id="pg", name="League"))
    store.add_league_season(LeagueSeason(
        id="ls", league_id="lg", season_id="se"))
    store.add_division(Division(id="d", league_season_id="ls", name="D"))
    for i in range(team_count):
        store.add_team(Team(
            id=f"t{i}", name=f"Team {i}", program_id="pg", league_id="lg"))
        store.add_season_team_registration(SeasonTeamRegistration(
            id=f"reg{i}", league_season_id="ls", team_id=f"t{i}",
            division_id="d", active=True))

    for venue_id, rink_id in (("v1", "r1"), ("v2", "r2"), ("v2", "r3")):
        if store.get_venue(venue_id) is None:
            store.add_venue(Venue(
                id=venue_id, name=f"Arena {venue_id}", organization_id="org",
                league_id="pg", timezone="UTC"))
        store.add_rink(Rink(
            id=rink_id, venue_id=venue_id, name=f"Rink {rink_id}"))
    if active_access:
        for index, venue_id in enumerate(("v1", "v2"), start=1):
            store.add_season_venue_access(SeasonVenueAccess(
                id=f"sva{index}", season_id="se", venue_id=venue_id,
                active=True))

    # Another Program/League/Season's identifiers and operator text must never
    # enter target-scope candidate evidence, even when its ice is earlier.
    store.add_organization(Organization(id="org_private", name="Private Owner"))
    store.add_program(Program(
        id="pg_private", name="Secret Program",
        operator_organization_id="org_private", timezone="UTC"))
    store.add_season(Season(
        id="se_private", program_id="pg_private", name="Secret Season"))
    store.add_league(League(
        id="lg_private", program_id="pg_private", name="Secret League"))
    store.add_league_season(LeagueSeason(
        id="ls_private", league_id="lg_private", season_id="se_private"))
    store.add_division(Division(
        id="d_private", league_season_id="ls_private", name="Secret Division"))
    store.add_team(Team(
        id="t_private", name="Private Team", program_id="pg_private",
        league_id="lg_private"))
    store.add_season_team_registration(SeasonTeamRegistration(
        id="reg_private", league_season_id="ls_private", team_id="t_private",
        division_id="d_private", active=True))
    store.add_venue(Venue(
        id="v_private", name="Private Arena", organization_id="org_private",
        league_id="pg_private", timezone="UTC"))
    store.add_rink(Rink(
        id="r_private", venue_id="v_private", name="Private Rink"))
    store.add_season_venue_access(SeasonVenueAccess(
        id="sva_private", season_id="se_private", venue_id="v_private",
        active=True))
    _slot(store, "slot_private", "r_private", BASE - timedelta(days=1))
    return ApiService(store)


def _slot(store, slot_id, rink_id, start, minutes=60):
    store.add_ice_slot(IceSlot(
        id=slot_id, rink_id=rink_id, start_time=start,
        end_time=start + timedelta(minutes=minutes)))
    return slot_id


def _explanation(preview, index=0):
    return preview["unscheduled"][index]["explanation"]


class _ExplanationContract:
    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()

    def tearDown(self):
        conn = getattr(self.store, "conn", None)
        if conn is not None:
            conn.close()

    def test_no_available_ice_has_input_correction_not_invented_slot(self):
        api = _seed(self.store)
        preview = api.draft_season_schedule("d")
        explanation = _explanation(preview)
        self.assertEqual(
            explanation["blocking_constraint_codes"], ["no_ice_available"])
        self.assertEqual(explanation["candidate_windows"], [])
        self.assertEqual(
            explanation["alternatives"][0]["action_code"],
            "increase_available_game_ice")
        self.assertNotIn("ice_slot_id", explanation["alternatives"][0])

    def test_zero_access_is_fail_closed_without_leaking_inaccessible_ice(self):
        api = _seed(self.store, active_access=False)
        preview = api.draft_season_schedule("d")
        explanation = _explanation(preview)
        self.assertEqual(explanation["candidate_windows"], [])
        self.assertIn(
            "venue_access_missing", explanation["blocking_constraint_codes"])
        self.assertEqual(
            explanation["alternatives"][0], {
                "action_code": "review_season_venue_access",
                "reason_code": "venue_access_missing",
                "season_id": "se",
            })
        serialized = json.dumps(explanation, sort_keys=True)
        for private_value in (
                "slot_private", "r_private", "v_private", "Private Arena",
                "Private Rink", "Secret Program", "Private Team"):
            self.assertNotIn(private_value, serialized)

    def test_explicit_out_of_scope_slot_fails_before_candidate_evidence(self):
        """Venue eligibility stays the existing server-side fail-closed gate."""
        api = _seed(self.store)
        result = api.draft_season_schedule("d", slot_ids=["slot_private"])
        self.assertEqual(
            result["error"]["details"]["reason"], "venue_access_missing")
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn("candidate_windows", serialized)
        for private_text in (
                "Private Arena", "Private Rink", "Secret Program", "Private Team"):
            self.assertNotIn(private_text, serialized)

    def test_simultaneous_blackout_causes_are_all_retained_and_identifier_only(self):
        """Dropping any one cause makes this independent mutation proof fail."""
        api = _seed(self.store)
        _slot(self.store, "candidate", "r1", BASE)
        day = BASE.date().isoformat()
        preview = api.draft_season_schedule("d", constraints={
            "season_blackout_dates": [day],
            "holiday_dates": [day],
            "team_blackouts": {"t0": [day]},
            "rink_blackouts": {"r1": [day]},
        })
        row = preview["unscheduled"][0]
        # Backward compatibility: legacy first-cause semantics do not change.
        self.assertEqual(row["reason_codes"], ["season_blackout"])
        candidate = row["explanation"]["candidate_windows"][0]
        self.assertEqual(
            [r["code"] for r in candidate["rejections"]],
            ["season_blackout", "holiday", "team_blackout", "rink_blackout"])
        self.assertEqual(candidate["ice_slot_id"], "candidate")
        self.assertEqual(candidate["rink_id"], "r1")
        self.assertEqual(candidate["venue_id"], "v1")
        serialized = json.dumps(candidate, sort_keys=True)
        for forbidden in ("Team 0", "Rink r1", "Arena v1", "message", "name"):
            self.assertNotIn(forbidden, serialized)
        # Every suggested action corrects a real input; none claims this hard-
        # rejected candidate is playable.
        for alternative in row["explanation"]["alternatives"]:
            self.assertNotIn("ice_slot_id", alternative)
            self.assertNotEqual(alternative["action_code"], "use_ice_slot")
        self.assertEqual(
            len(row["explanation"]["alternatives"]),
            MAX_ALTERNATIVES_PER_PAIRING)
        self.assertEqual(
            row["explanation"]["bounds"]["alternative_omitted_count"], 1)

    def test_minimum_rest_and_max_per_day_are_both_explained(self):
        api = _seed(self.store, team_count=3)
        for index, hour in enumerate((0, 2, 4)):
            _slot(self.store, f"same_day_{index}", "r1",
                  BASE + timedelta(hours=hour))
        preview = api.draft_season_schedule("d", constraints={
            "max_games_per_team_per_day": 1,
            "min_rest_hours": 48,
        })
        rejections = [
            rejection
            for row in preview["unscheduled"]
            for candidate in row["explanation"]["candidate_windows"]
            for rejection in candidate["rejections"]
        ]
        codes = {rejection["code"] for rejection in rejections}
        self.assertIn("max_per_day", codes)
        self.assertIn("min_rest", codes)
        rest = next(r for r in rejections if r["code"] == "min_rest")
        self.assertEqual(rest["details"]["min_rest_hours"], 48.0)
        self.assertTrue(rest["details"]["conflicts"])

    def test_team_overlap_names_only_safe_ids_and_never_changes_selection(self):
        api = _seed(self.store, team_count=3)
        _slot(self.store, "simultaneous_r1", "r1", BASE)
        _slot(self.store, "simultaneous_r2", "r2", BASE)
        preview = api.draft_season_schedule("d")
        self.assertEqual(len(preview["draft_games"]), 1)
        overlap_rows = [
            row for row in preview["unscheduled"]
            if "team_overlap" in row["explanation"]["blocking_constraint_codes"]
        ]
        self.assertTrue(overlap_rows, preview)
        explanation = overlap_rows[0]["explanation"]
        overlap = next(
            rejection
            for candidate in explanation["candidate_windows"]
            for rejection in candidate["rejections"]
            if rejection["code"] == "team_overlap")
        self.assertTrue(overlap["details"]["team_ids"])
        self.assertNotIn("team_name", json.dumps(overlap))
        self.assertIn(
            "provide_non_overlapping_game_ice",
            {a["action_code"] for a in explanation["alternatives"]})

    def test_turnover_curfew_and_playable_time_use_shared_policy_codes(self):
        api = _seed(self.store)
        self.store.add_division(Division(
            id="host_div", league_season_id="ls", name="Host"))
        for index in range(2):
            tid = f"host_t{index}"
            self.store.add_team(Team(
                id=tid, name=f"Host {index}", program_id="pg", league_id="lg"))
            self.store.add_season_team_registration(SeasonTeamRegistration(
                id=f"host_reg{index}", league_season_id="ls", team_id=tid,
                division_id="host_div", active=True))

        _slot(self.store, "host", "r1", BASE)
        created = api.create_game(
            "se", "host_div", "host_t0", "host_t1", "host",
            league_id="lg", actor_id="admin")
        self.assertNotIn("error", created, created)
        _slot(self.store, "turnover", "r1", BASE + timedelta(minutes=65))
        _slot(self.store, "curfew", "r2", BASE + timedelta(days=1, hours=1))
        _slot(self.store, "sliver", "r3", BASE + timedelta(days=2), minutes=40)
        self.assertNotIn("error", api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", warmup_minutes=10,
            resurfacing_minutes=10, actor_id="admin"))
        self.assertNotIn("error", api.set_scheduling_policy(
            scope_type="rink", scope_id="r2", curfew_local="19:00",
            actor_id="admin"))
        self.assertNotIn("error", api.set_scheduling_policy(
            scope_type="rink", scope_id="r3", min_playable_minutes=60,
            actor_id="admin"))

        preview = api.draft_season_schedule("d")
        explanation = _explanation(preview)
        codes = {
            rejection["code"]
            for candidate in explanation["candidate_windows"]
            for rejection in candidate["rejections"]
        }
        self.assertEqual(codes, {
            "turnover_buffer_conflict", "curfew_violation",
            "insufficient_playable_time",
        })
        for candidate in explanation["candidate_windows"]:
            self.assertTrue(candidate["rejections"])
        self.assertEqual(
            {a["action_code"] for a in explanation["alternatives"]},
            {"review_rink_scheduling_policy"})

    def test_cap_order_and_scope_are_stable_across_multi_venue_inventory(self):
        """Reordering, over-cap, or out-of-scope mutations fail separately."""
        api = _seed(self.store)
        self.store.add_league(League(
            id="lg_cross", program_id="pg", name="Other League"))
        self.store.add_league_season(LeagueSeason(
            id="ls_cross", league_id="lg_cross", season_id="se"))
        self.store.add_division(Division(
            id="d_cross", league_season_id="ls_cross", name="Other Division"))
        self.store.add_team(Team(
            id="t_cross", name="Cross League Team", program_id="pg",
            league_id="lg_cross"))
        self.store.add_season_team_registration(SeasonTeamRegistration(
            id="reg_cross", league_season_id="ls_cross", team_id="t_cross",
            division_id="d_cross", active=True))
        expected = []
        # Insert in reverse chronological/id order; the response must use the
        # scheduler's canonical (start_time, id) order, not store order.
        for index in reversed(range(MAX_CANDIDATE_WINDOWS_PER_PAIRING + 1)):
            slot_id = f"candidate_{index:02d}"
            rink_id = "r1" if index % 2 == 0 else "r2"
            _slot(self.store, slot_id, rink_id, BASE + timedelta(days=index))
            expected.append((BASE + timedelta(days=index), slot_id))
        days = [(BASE + timedelta(days=index)).date().isoformat()
                for index in range(MAX_CANDIDATE_WINDOWS_PER_PAIRING + 1)]
        preview = api.draft_season_schedule(
            "d", constraints={"season_blackout_dates": days})
        explanation = _explanation(preview)
        ids = [c["ice_slot_id"] for c in explanation["candidate_windows"]]
        canonical = [slot_id for _start, slot_id in sorted(expected)]
        self.assertEqual(ids, canonical[:MAX_CANDIDATE_WINDOWS_PER_PAIRING])
        self.assertEqual(len(ids), MAX_CANDIDATE_WINDOWS_PER_PAIRING)
        self.assertEqual(
            explanation["bounds"]["candidate_window_total"],
            MAX_CANDIDATE_WINDOWS_PER_PAIRING + 1)
        self.assertEqual(
            explanation["bounds"]["candidate_window_omitted_count"], 1)
        self.assertTrue(explanation["bounds"]["candidate_windows_truncated"])
        serialized = json.dumps(explanation, sort_keys=True)
        for private_id in (
                "slot_private", "r_private", "v_private", "t_private",
                "t_cross", "Cross League Team"):
            self.assertNotIn(private_id, serialized)


class MemoryExplanationTest(_ExplanationContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class SqliteExplanationTest(_ExplanationContract, unittest.TestCase):
    def make_store(self):
        return SqlStore(":memory:")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresExplanationTest(_ExplanationContract, unittest.TestCase):
    def make_store(self):
        store = SqlStore(os.environ["TEST_DATABASE_URL"])
        store.clear_all_data()
        return store


class ExplanationBudgetAndObserverTest(unittest.TestCase):
    def test_rejection_and_alternative_caps_report_exact_omitted_counts(self):
        codes = [
            "season_blackout", "holiday", "team_blackout", "rink_blackout",
            "max_per_day", "min_rest", "insufficient_playable_time",
            "curfew_violation", "team_overlap",
        ]
        explanation = build_unplaced_explanation(
            pairing={"home_team_id": "t0", "away_team_id": "t1"},
            scope={"season_id": "se", "league_id": "lg"},
            legacy_reason_codes=[codes[0]],
            raw_candidates=[{
                "ice_slot_id": "s", "rink_id": "r", "venue_id": "v",
                "start_time": BASE.isoformat(),
                "end_time": (BASE + timedelta(hours=1)).isoformat(),
                "rejections": [{"code": code, "details": {}}
                               for code in codes],
            }],
            candidate_total=1,
        )
        candidate = explanation["candidate_windows"][0]
        self.assertEqual(
            len(candidate["rejections"]), MAX_REJECTIONS_PER_CANDIDATE)
        self.assertEqual(candidate["omitted_rejection_count"], 1)
        self.assertEqual(
            len(explanation["alternatives"]), MAX_ALTERNATIVES_PER_PAIRING)
        self.assertGreater(explanation["bounds"]["alternative_omitted_count"], 0)

    def test_whole_preview_candidate_budget_stops_at_exact_boundary(self):
        store = InMemoryStore()
        api = _seed(store)
        slots = []
        for index in range(MAX_CANDIDATE_WINDOWS_PER_PAIRING):
            _slot(store, f"budget_{index}", "r1", BASE + timedelta(days=index))
            slots.append(store.get_ice_slot(f"budget_{index}"))
        pair_count = MAX_CANDIDATE_WINDOWS_PER_PREVIEW \
            // MAX_CANDIDATE_WINDOWS_PER_PAIRING + 1
        pairings = [("t0", "t1", "d") for _ in range(pair_count)]
        days = [slot.start_time.date().isoformat() for slot in slots]
        _draft, unscheduled = sched._assign_ice(
            store, pairings, slots,
            {"season_blackout_dates": days},
            policy_check=sched._policy_advisor(store, "se"),
            explanation_context={"season_id": "se", "league_id": "lg"},
        )
        evidence_count = sum(
            len(row["explanation"]["candidate_windows"])
            for row in unscheduled)
        self.assertEqual(evidence_count, MAX_CANDIDATE_WINDOWS_PER_PREVIEW)
        self.assertEqual(
            unscheduled[-1]["explanation"]["candidate_windows"], [])
        self.assertTrue(
            unscheduled[-1]["explanation"]["bounds"]
            ["preview_candidate_budget_limited"])

    def test_explanation_toggle_cannot_change_scheduler_decisions(self):
        """Any observer mutation that changes selection fails this golden."""
        store = InMemoryStore()
        _seed(store, team_count=3)
        _slot(store, "toggle_0", "r1", BASE)
        _slot(store, "toggle_1", "r2", BASE)
        slots = sched._available_game_slots(store)
        pairings = [("t1", "t2", "d"), ("t1", "t0", "d"),
                    ("t0", "t2", "d")]
        active = sched._active_game_slot_pairs(store)
        common = dict(
            policy_check=sched._policy_advisor(store, "se", active),
            team_spans=sched._persisted_team_spans(active),
            explanation_context={"season_id": "se", "league_id": "lg"},
        )
        explained = sched._assign_ice(
            store, pairings, slots, None, explain=True, **common)
        plain = sched._assign_ice(
            store, pairings, slots, None, explain=False, **common)
        self.assertEqual(explained[0], plain[0])
        explained_rows = [
            {key: value for key, value in row.items() if key != "explanation"}
            for row in explained[1]
        ]
        self.assertEqual(explained_rows, plain[1])


class SchedulerExplanationHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(
            target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        srv.STATE.reset(seed=False)

    def setUp(self):
        srv.STATE.reset(seed=False)
        _seed(srv.STATE.api.store)
        _slot(srv.STATE.api.store, "http_candidate", "r1", BASE)

    def _request(self, opener, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with opener.open(request) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_authenticated_preview_serializes_additive_value_object(self):
        client = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        self._request(client, "POST", "/api/auth/login", {
            "username": "admin", "password": "demo"})
        status, preview = self._request(
            client, "POST", "/api/scheduler/draft", {
                "division_id": "d",
                "constraints": {
                    "season_blackout_dates": [BASE.date().isoformat()]},
            })
        self.assertEqual(status, 200, preview)
        explanation = _explanation(preview)
        self.assertEqual(explanation["value_object_version"], 1)
        self.assertEqual(
            explanation["candidate_windows"][0]["ice_slot_id"],
            "http_candidate")


if __name__ == "__main__":
    unittest.main()
