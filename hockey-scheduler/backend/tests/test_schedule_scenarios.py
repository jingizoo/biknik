"""Named immutable schedule scenarios and stale atomic commit (#206/#378)."""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND, fresh_sql_store  # noqa: F401

from hockey_scheduler.api import ApiService
from hockey_scheduler.api.service import ApiService as BaseApiService
from hockey_scheduler.domain import (
    Division,
    Game,
    IceSlot,
    IceSlotStatus,
    League,
    LeagueSeason,
    Organization,
    PolicyScopeType,
    Program,
    Rink,
    ScheduleScenario,
    SchedulingPolicy,
    Season,
    SeasonTeamRegistration,
    SeasonVenueAccess,
    Team,
    Venue,
)
from hockey_scheduler.domain.errors import ScheduleConflictError
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv


UTC = timezone.utc
BASE = datetime(2026, 9, 7, 18, tzinfo=UTC)


def _seed(store):
    store.add_organization(Organization(id="org", name="Owner"))
    store.add_program(Program(
        id="program", name="Adult", operator_organization_id="org"))
    store.add_season(Season(id="season", program_id="program", name="Fall"))
    store.add_league(League(
        id="league", program_id="program", name="Silver"))
    store.add_league_season(LeagueSeason(
        id="league_season", league_id="league", season_id="season"))
    store.add_division(Division(
        id="division", league_season_id="league_season", name="North"))
    store.add_venue(Venue(
        id="venue", name="Arena", organization_id="org"))
    store.add_season_venue_access(SeasonVenueAccess(
        id="access", season_id="season", venue_id="venue", active=True))
    store.add_rink(Rink(id="rink", venue_id="venue", name="Main"))
    for index in range(4):
        team_id = f"team_{index}"
        store.add_team(Team(
            id=team_id, name=f"Team {index}", program_id="program",
            league_id="league", division_id="division"))
        store.add_season_team_registration(SeasonTeamRegistration(
            id=f"registration_{index}", league_season_id="league_season",
            team_id=team_id, division_id="division", active=True))
    for index in range(8):
        store.add_ice_slot(IceSlot(
            id=f"slot_{index}", rink_id="rink",
            start_time=BASE + timedelta(days=index),
            end_time=BASE + timedelta(days=index, hours=1)))


class _ScenarioContract:
    def _make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self._make_store()
        _seed(self.store)
        self.api = ApiService(self.store)

    def tearDown(self):
        self.store.close()

    def _scenario(self, name="Baseline", **kwargs):
        result = self.api.create_schedule_scenario(
            name, division_id="division", actor_id="operator", **kwargs)
        self.assertNotIn("error", result, result)
        return result

    def _assert_stale(self, mutate, expected_section):
        scenario = self._scenario()
        games_before = list(self.store.all_games())
        audit_before = len(self.store.all_setup_audit())
        mutate()
        # The authoritative mutation itself is expected to remain.  The stale
        # commit must add nothing beyond that already-landed new reality.
        games_after_mutation = list(self.store.all_games())
        slot_status_after_mutation = {
            slot.id: slot.status for slot in self.store.all_ice_slots()}
        result = self.api.commit_schedule_scenario(
            scenario["scenario_id"], actor_id="operator")
        self.assertEqual(
            result["error"]["details"]["reason"],
            "schedule_scenario_stale", result)
        self.assertIn(
            expected_section,
            result["error"]["details"]["changed_inputs"], result)
        self.assertEqual(
            result["error"]["details"]["required_action"],
            "generate_new_scenario")
        self.assertEqual(self.store.all_games(), games_after_mutation)
        self.assertGreaterEqual(len(games_after_mutation), len(games_before))
        self.assertEqual(len(self.store.all_setup_audit()), audit_before)
        self.assertEqual(
            {slot.id: slot.status for slot in self.store.all_ice_slots()},
            slot_status_after_mutation)

    def test_generation_persists_immutable_review_without_games(self):
        scenario = self._scenario(
            "  Opening plan  ", slot_ids=["slot_0"],
            constraints={"team_blackouts": {"team_0": ["2026-09-08"]}})
        self.assertEqual(scenario["name"], "Opening plan")
        self.assertEqual(self.store.all_games(), [])
        self.assertEqual(
            scenario["scope"],
            {"program_id": "program", "season_id": "season",
             "league_id": "league", "league_season_id": "league_season",
             "division_id": "division"})
        self.assertEqual(scenario["planner"]["version"], "round-robin-v1")
        self.assertTrue(scenario["proposal"]["unscheduled"])

        # Mutate every nested caller-visible artifact independently.  The
        # in-memory store must behave like SQL serialization, and no update API
        # exists: a later read returns the original historical evidence.
        scenario["name"] = "Mutated"
        scenario["request_input"]["constraints"]["team_blackouts"].clear()
        scenario["proposal"]["unscheduled"][0]["reason"] = "mutated"
        scenario["generation_snapshot"]["ice"].clear()
        fetched = self.api.get_schedule_scenario(scenario["scenario_id"])
        self.assertEqual(fetched["name"], "Opening plan")
        self.assertTrue(
            fetched["request_input"]["constraints"]["team_blackouts"])
        self.assertNotEqual(
            fetched["proposal"]["unscheduled"][0]["reason"], "mutated")
        self.assertTrue(fetched["generation_snapshot"]["ice"])
        stored = self.store.get_schedule_scenario(scenario["scenario_id"])
        self.assertIsInstance(stored, ScheduleScenario)
        self.assertEqual(stored.created_by, "operator")

    def test_identical_inputs_are_deterministic(self):
        first = self._scenario("One")
        second = self._scenario("Two")
        self.assertEqual(first["planner"]["input_fingerprint"],
                         second["planner"]["input_fingerprint"])
        self.assertEqual(first["planner"]["proposal_fingerprint"],
                         second["planner"]["proposal_fingerprint"])
        self.assertEqual(first["generation_snapshot"],
                         second["generation_snapshot"])
        self.assertEqual(first["proposal"], second["proposal"])

    def test_league_mode_uses_permanent_and_seasonal_scope_identity(self):
        scenario = self.api.create_schedule_scenario(
            "League scope", season_id="season", league_id="league",
            division_id="division", actor_id="operator")
        self.assertNotIn("error", scenario, scenario)
        self.assertEqual(
            scenario["scope"],
            {"program_id": "program", "season_id": "season",
             "league_id": "league", "league_season_id": "league_season",
             "division_id": "division"})
        self.assertEqual(scenario["request_input"]["scope_mode"], "league")

    def test_immutable_scope_blocks_parent_deletion_with_named_dependency(self):
        scenario = self._scenario("Keep this scope")
        result = self.api.delete_division("division", actor_id="operator")
        self.assertEqual(result["error"]["code"], "has_dependencies", result)
        groups = result["error"]["details"]["dependencies"]
        scenario_group = next(
            group for group in groups if group["type"] == "schedule scenario")
        self.assertEqual(scenario_group["count"], 1)
        self.assertEqual(
            scenario_group["items"],
            [{"id": scenario["scenario_id"], "name": "Keep this scope"}])
        self.assertIsNotNone(self.store.get_division("division"))

    def test_registration_mutation_refuses_commit(self):
        def mutate():
            registration = self.store.get_season_team_registration(
                "registration_3")
            registration.active = False
            self.store.save_season_team_registration(registration)
        self._assert_stale(mutate, "registrations")

    def test_venue_access_mutation_refuses_commit(self):
        def mutate():
            access = self.store.get_season_venue_access("access")
            access.active = False
            self.store.save_season_venue_access(access)
        self._assert_stale(mutate, "venue_access")

    def test_ice_mutation_refuses_commit(self):
        def mutate():
            slot = self.store.get_ice_slot("slot_7")
            slot.status = IceSlotStatus.ALLOCATED
            self.store.save_ice_slot(slot)
        self._assert_stale(mutate, "ice")

    def test_game_mutation_refuses_commit(self):
        def mutate():
            self.store.add_game(Game(
                id="late_game", home_team_id="team_0",
                away_team_id="team_1", start_time=BASE - timedelta(days=30),
                end_time=BASE - timedelta(days=30) + timedelta(hours=1),
                season_id="season", league_id="league",
                league_season_id="league_season", division_id="division"))
        self._assert_stale(mutate, "games")

    def test_lock_only_mutation_refuses_even_when_proposal_is_identical(self):
        existing = Game(
            id="existing", home_team_id="team_0", away_team_id="team_1",
            start_time=BASE - timedelta(days=30),
            end_time=BASE - timedelta(days=30) + timedelta(hours=1),
            season_id="season", league_id="league",
            league_season_id="league_season", division_id="division")
        self.store.add_game(existing)
        scenario = self._scenario()
        existing.locked = True
        self.store.save_game(existing)
        fresh = self.api.draft_season_schedule("division")
        self.assertEqual(
            fresh, scenario["proposal"],
            "the snapshot guard, not proposal drift, must catch lock changes")
        result = self.api.commit_schedule_scenario(scenario["scenario_id"])
        self.assertEqual(result["error"]["details"]["reason"],
                         "schedule_scenario_stale", result)
        self.assertIn("games", result["error"]["details"]["changed_inputs"])
        self.assertEqual([game.id for game in self.store.all_games()],
                         ["existing"])

    def test_policy_mutation_refuses_even_when_placement_is_identical(self):
        scenario = self._scenario()
        self.store.add_scheduling_policy(SchedulingPolicy(
            id="policy", scope_type=PolicyScopeType.PROGRAM,
            scope_id="program", warmup_minutes=0))
        fresh = self.api.draft_season_schedule("division")
        self.assertEqual(fresh, scenario["proposal"])
        result = self.api.commit_schedule_scenario(scenario["scenario_id"])
        self.assertEqual(result["error"]["details"]["reason"],
                         "schedule_scenario_stale", result)
        self.assertIn("policies",
                      result["error"]["details"]["changed_inputs"])
        self.assertEqual(self.store.all_games(), [])

    def test_later_row_failure_rolls_back_games_slots_audits_and_counter(self):
        scenario = self._scenario()
        audit_before = len(self.store.all_setup_audit())
        if isinstance(self.store, InMemoryStore):
            counter_before = self.store._counters.get("game")
        else:
            counter_row = self.store._exec(
                "SELECT value FROM counters WHERE prefix = ?", ("game",)
            ).fetchone()
            counter_before = counter_row["value"] if counter_row else None
        original = self.api.setup._assert_slot_free_for_game
        calls = {"count": 0}

        def fail_second(*args, **kwargs):
            slot = original(*args, **kwargs)
            calls["count"] += 1
            if calls["count"] == 2:
                raise ScheduleConflictError(
                    "Forced later-row refusal.",
                    details={"reason": "forced_later_row_refusal"})
            return slot

        self.api.setup._assert_slot_free_for_game = fail_second
        try:
            result = self.api.commit_schedule_scenario(
                scenario["scenario_id"], actor_id="operator")
        finally:
            self.api.setup._assert_slot_free_for_game = original
        self.assertEqual(result["error"]["details"]["reason"],
                         "forced_later_row_refusal", result)
        self.assertGreaterEqual(calls["count"], 2)
        self.assertEqual(self.store.all_games(), [])
        self.assertTrue(all(
            slot.status == IceSlotStatus.AVAILABLE
            for slot in self.store.all_ice_slots()))
        self.assertEqual(len(self.store.all_setup_audit()), audit_before)
        # Counter allocation happened for row one inside the failed unit and
        # must roll back too. Compare the stored value without allocating a new
        # id: PostgreSQL test databases deliberately retain counters across
        # per-test data clears, so the absolute value need not be one.
        if isinstance(self.store, InMemoryStore):
            counter_after = self.store._counters.get("game")
        else:
            counter_row = self.store._exec(
                "SELECT value FROM counters WHERE prefix = ?", ("game",)
            ).fetchone()
            counter_after = counter_row["value"] if counter_row else None
        self.assertEqual(counter_after, counter_before)

    def test_success_creates_unpublished_drafts_and_never_mutates_scenario(self):
        scenario = self._scenario()
        before = self.api.get_schedule_scenario(scenario["scenario_id"])
        result = self.api.commit_schedule_scenario(
            scenario["scenario_id"], actor_id="operator")
        self.assertNotIn("error", result, result)
        self.assertEqual(len(result["created"]), 6)
        games = self.store.all_games()
        self.assertTrue(all(game.is_draft and not game.published
                            for game in games))
        self.assertEqual(self.api.get_public_schedule()["fixtures"], [])
        self.assertEqual(
            self.api.get_schedule_scenario(scenario["scenario_id"]), before)

    def test_scenario_wrapper_does_not_change_375_owned_generation(self):
        direct = self.api.draft_season_schedule("division")
        scenario = self._scenario()
        self.assertEqual(scenario["proposal"], direct)
        pairings = {
            frozenset((row["home_team_id"], row["away_team_id"]))
            for row in scenario["proposal"]["draft_games"]
        }
        self.assertEqual(len(pairings), 6)
        self.assertEqual(len(scenario["proposal"]["draft_games"]), 6)
        self.assertNotIn("meetings_per_opponent", scenario["request_input"])

    def test_base_facade_uses_the_same_stored_proposal_commit_contract(self):
        base = BaseApiService(self.store)
        scenario = base.create_schedule_scenario(
            "Base facade", division_id="division", actor_id="operator")
        self.assertNotIn("error", scenario, scenario)
        result = base.commit_schedule_scenario(
            scenario["scenario_id"], actor_id="operator")
        self.assertNotIn("error", result, result)
        self.assertEqual(len(result["created"]), 6)


class MemoryScheduleScenarioTest(_ScenarioContract, unittest.TestCase):
    def _make_store(self):
        return InMemoryStore()


class SqliteScheduleScenarioTest(_ScenarioContract, unittest.TestCase):
    def _make_store(self):
        return SqlStore(":memory:")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresScheduleScenarioTest(_ScenarioContract, unittest.TestCase):
    def _make_store(self):
        return fresh_sql_store(os.environ["TEST_DATABASE_URL"])

    def test_independent_registration_write_during_commit_is_stale_atomic(self):
        """A second connection wins before the scenario lock plan is held.

        This is the PostgreSQL-only falsification for the narrow commit race:
        the scenario transaction is already open and its row is locked when a
        real unregister request commits on another connection. The subsequent
        scheduler locks and material-input guard must observe that winner and
        refuse before any draft/audit/slot write.
        """
        scenario = self._scenario("PostgreSQL race")
        original = self.api.setup._lock_programs
        race = {"calls": 0, "result": None, "error": None}

        def mutate_on_second_connection():
            other_store = SqlStore(os.environ["TEST_DATABASE_URL"])
            try:
                other_api = ApiService(other_store)
                race["result"] = other_api.unregister_team_from_season(
                    "registration_3", actor_id="racing-operator")
            except BaseException as error:  # surfaced on the test thread below
                race["error"] = error
            finally:
                other_store.close()

        def let_registration_win(program_ids):
            race["calls"] += 1
            worker = threading.Thread(
                target=mutate_on_second_connection, daemon=True)
            worker.start()
            worker.join(timeout=10)
            if worker.is_alive():
                self.fail("concurrent registration writer did not finish")
            if race["error"] is not None:
                raise race["error"]
            self.assertNotIn("error", race["result"], race["result"])
            return original(program_ids)

        self.api.setup._lock_programs = let_registration_win
        try:
            result = self.api.commit_schedule_scenario(
                scenario["scenario_id"], actor_id="operator")
        finally:
            self.api.setup._lock_programs = original

        self.assertEqual(race["calls"], 1)
        self.assertEqual(result["error"]["details"]["reason"],
                         "schedule_scenario_stale", result)
        self.assertIn("registrations",
                      result["error"]["details"]["changed_inputs"])
        self.assertEqual(self.store.all_games(), [])
        self.assertTrue(all(
            slot.status == IceSlotStatus.AVAILABLE
            for slot in self.store.all_ice_slots()))
        self.assertFalse(self.store.get_season_team_registration(
            "registration_3").active)
        self.assertFalse(any(
            audit.action == "schedule_scenario_committed"
            and audit.entity_id == scenario["scenario_id"]
            for audit in self.store.all_setup_audit()))


class ScheduleScenarioHttpTest(unittest.TestCase):
    """Authenticated route, strict schema, and server-side actor attribution."""

    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(
            target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        # This module's own four-team fixture, planted alongside the demo seed,
        # rather than the seed's first Division: the seed carries a SIX-game
        # series between one pair of teams, and #375's meeting-count contract
        # refuses any commit whose already-scheduled pairing holds MORE Games
        # than the format asks for — so a commit there answers `preview_stale`
        # for a reason that has nothing to do with scenarios. `_seed` uses
        # literal ids ("program"/"season"/…) that cannot collide with the
        # demo seed's generated `program_1`-style ids.
        _seed(srv.STATE.api.store)
        cls.division_id = "division"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _request(self, client, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with client.open(request) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read() or b"{}")

    def test_non_operator_cannot_create_read_or_commit_scenario(self):
        client = self._client()
        self._request(client, "POST", "/api/auth/login",
                      {"username": "coach", "password": "demo"})
        status, _ = self._request(
            client, "POST", "/api/scheduler/scenarios",
            {"name": "Forbidden", "division_id": self.division_id})
        self.assertEqual(status, 403)
        status, _ = self._request(client, "GET", "/api/scheduler/scenarios")
        self.assertEqual(status, 403)
        status, _ = self._request(
            client, "POST", "/api/scheduler/scenarios/missing/commit", {})
        self.assertEqual(status, 403)

    def test_operator_roundtrip_is_strict_and_server_attributed(self):
        client = self._client()
        self._request(client, "POST", "/api/auth/login",
                      {"username": "admin", "password": "demo"})
        status, error = self._request(
            client, "POST", "/api/scheduler/scenarios",
            {"name": "Bad", "division_id": self.division_id,
             "actor_id": "forged"})
        self.assertEqual(status, 400)
        self.assertEqual(error["error"]["details"]["reason"], "unknown_field")
        status, scenario = self._request(
            client, "POST", "/api/scheduler/scenarios",
            {"name": "HTTP scenario", "division_id": self.division_id})
        self.assertEqual(status, 200, scenario)
        self.assertNotEqual(scenario["created_by"], "forged")
        status, fetched = self._request(
            client, "GET",
            f"/api/scheduler/scenarios/{scenario['scenario_id']}")
        self.assertEqual(status, 200, fetched)
        self.assertEqual(fetched, scenario)
        status, listed = self._request(
            client, "GET", "/api/scheduler/scenarios")
        self.assertEqual(status, 200, listed)
        self.assertTrue(any(
            item["scenario_id"] == scenario["scenario_id"]
            for item in listed["scenarios"]))
        audits = [
            audit for audit in srv.STATE.api.store.all_setup_audit()
            if audit.action == "schedule_scenario_created"
            and audit.entity_id == scenario["scenario_id"]]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].actor_id, scenario["created_by"])

        status, error = self._request(
            client, "POST",
            f"/api/scheduler/scenarios/{scenario['scenario_id']}/commit",
            {"actor_id": "forged"})
        self.assertEqual(status, 400, error)
        self.assertEqual(error["error"]["details"]["reason"], "unknown_field")
        status, committed = self._request(
            client, "POST",
            f"/api/scheduler/scenarios/{scenario['scenario_id']}/commit", {})
        self.assertEqual(status, 200, committed)
        commit_audits = [
            audit for audit in srv.STATE.api.store.all_setup_audit()
            if audit.action == "schedule_scenario_committed"
            and audit.entity_id == scenario["scenario_id"]]
        self.assertEqual(len(commit_audits), 1)
        self.assertEqual(commit_audits[0].actor_id, scenario["created_by"])


if __name__ == "__main__":
    unittest.main()
