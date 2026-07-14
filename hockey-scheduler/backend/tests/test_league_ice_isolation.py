"""League-isolated ice enforcement across scheduling paths (#173 PR C)."""

import unittest
from datetime import datetime, timezone

from helpers import FakeClock

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Game, IceSlotStatus
from hockey_scheduler.domain.errors import ValidationError
from hockey_scheduler.services import SetupService
from hockey_scheduler.store import InMemoryStore

UTC = timezone.utc


def at(hour):
    return datetime(2026, 9, 1, hour, tzinfo=UTC)


class LeagueIceIsolationTest(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.setup = SetupService(self.store, clock=FakeClock())

        self.owner_a = self.setup.create_organization("Owner A")
        self.owner_b = self.setup.create_organization("Owner B")
        self.league_a = self.setup.create_program(
            "League A", operator_organization_id=self.owner_a.id)
        self.league_b = self.setup.create_program(
            "League B", operator_organization_id=self.owner_b.id)
        self.season_a = self.setup.create_season(self.league_a.id, "Fall A")
        self.season_b = self.setup.create_season(self.league_b.id, "Fall B")
        self.div_a = self.setup.create_division(self.season_a.id, "A")
        self.div_b = self.setup.create_division(self.season_b.id, "B")

        club_a = self.setup.create_club("Club A")
        club_b = self.setup.create_club("Club B")
        self.a1 = self.setup.create_team(club_a.id, self.div_a.id, "A1")
        self.a2 = self.setup.create_team(club_a.id, self.div_a.id, "A2")
        self.b1 = self.setup.create_team(club_b.id, self.div_b.id, "B1")
        self.b2 = self.setup.create_team(club_b.id, self.div_b.id, "B2")
        # Participation is per-season (#180): register each team in its league's
        # season+division so its games can be scheduled.
        self.setup.register_team_for_season(self.season_a.id, self.a1.id, self.div_a.id)
        self.setup.register_team_for_season(self.season_a.id, self.a2.id, self.div_a.id)
        self.setup.register_team_for_season(self.season_b.id, self.b1.id, self.div_b.id)
        self.setup.register_team_for_season(self.season_b.id, self.b2.id, self.div_b.id)

        self.venue_a = self.setup.create_venue(
            "Arena A", league_id=self.league_a.id)
        self.venue_b = self.setup.create_venue(
            "Arena B", league_id=self.league_b.id)
        self.venue_unassigned = self.setup.create_venue(
            "Unassigned", organization_id=self.owner_a.id)
        self.rink_a = self.setup.create_rink(self.venue_a.id, "A Rink")
        self.rink_b = self.setup.create_rink(self.venue_b.id, "B Rink")
        self.rink_unassigned = self.setup.create_rink(
            self.venue_unassigned.id, "Open Rink")

        self.same_1 = self.setup.create_ice_slot(
            self.rink_a.id, at(19), at(20))
        self.same_2 = self.setup.create_ice_slot(
            self.rink_a.id, at(21), at(22))
        # Earlier than same-league ice, proving the scheduler filters before sort.
        self.cross = self.setup.create_ice_slot(
            self.rink_b.id, at(17), at(18))
        self.unassigned = self.setup.create_ice_slot(
            self.rink_unassigned.id, at(18), at(19))

    def _error_reason(self, fn):
        with self.assertRaises(ValidationError) as ctx:
            fn()
        return ctx.exception.details.get("reason")

    def test_manual_create_accepts_same_league_ice(self):
        game = self.setup.create_game(
            self.season_a.id, self.div_a.id,
            self.a1.id, self.a2.id, self.same_1.id)
        self.assertEqual(game.ice_slot_id, self.same_1.id)

    def test_manual_create_rejects_cross_league_ice(self):
        reason = self._error_reason(lambda: self.setup.create_game(
            self.season_a.id, self.div_a.id,
            self.a1.id, self.a2.id, self.cross.id))
        self.assertEqual(reason, "cross_league_slot")
        self.assertEqual(self.cross.status, IceSlotStatus.AVAILABLE)

    def test_manual_create_rejects_unassigned_venue_ice(self):
        reason = self._error_reason(lambda: self.setup.create_game(
            self.season_a.id, self.div_a.id,
            self.a1.id, self.a2.id, self.unassigned.id))
        self.assertEqual(reason, "venue_league_unassigned")

    def test_move_rejects_cross_league_target_without_partial_state(self):
        game = self.setup.create_game(
            self.season_a.id, self.div_a.id,
            self.a1.id, self.a2.id, self.same_1.id)
        reason = self._error_reason(
            lambda: self.setup.move_game(game.id, self.cross.id))
        self.assertEqual(reason, "cross_league_slot")
        self.assertEqual(game.ice_slot_id, self.same_1.id)
        self.assertEqual(self.same_1.status, IceSlotStatus.ALLOCATED)
        self.assertEqual(self.cross.status, IceSlotStatus.AVAILABLE)

    def test_reschedule_approval_inherits_cross_league_rejection(self):
        game = self.setup.create_game(
            self.season_a.id, self.div_a.id,
            self.a1.id, self.a2.id, self.same_1.id)
        self.setup.publish_game(game.id)
        request = self.setup.request_reschedule(
            game.id, self.a1.id, "Weather")
        request = self.setup.respond_to_reschedule(request.id, True)

        reason = self._error_reason(lambda: self.setup.decide_reschedule(
            request.id, True, new_ice_slot_id=self.cross.id,
            actor_id="league_admin"))
        self.assertEqual(reason, "cross_league_slot")

        reloaded = self.store.get_reschedule_request(request.id)
        self.assertEqual(reloaded.status.value, "pending_league_approval")
        self.assertIsNone(reloaded.new_ice_slot_id)
        self.assertEqual(game.ice_slot_id, self.same_1.id)
        self.assertTrue(game.published)
        self.assertEqual(self.same_1.status, IceSlotStatus.ALLOCATED)
        self.assertEqual(self.cross.status, IceSlotStatus.AVAILABLE)

    def test_draft_automatically_filters_to_divisions_league(self):
        api = ApiService(self.store)
        result = api.draft_season_schedule(self.div_a.id)
        self.assertNotIn("error", result)
        self.assertEqual(result["league_id"], self.league_a.id)
        self.assertEqual(result["season_id"], self.season_a.id)
        self.assertEqual(result["draft_games"][0]["ice_slot_id"], self.same_1.id)

    def test_explicit_cross_league_slot_is_structured_error(self):
        result = ApiService(self.store).draft_season_schedule(
            self.div_a.id, slot_ids=[self.cross.id])
        self.assertEqual(
            result["error"]["details"]["reason"], "cross_league_slot")

    def test_draft_commit_persists_season_and_league_context(self):
        api = ApiService(self.store)
        result = api.commit_draft_schedule(
            self.div_a.id, slot_ids=[self.same_1.id])
        self.assertNotIn("error", result)
        self.assertEqual(result["season_id"], self.season_a.id)
        self.assertEqual(result["league_id"], self.league_a.id)
        game = self.store.get_game(result["created"][0]["game_id"])
        self.assertEqual(game.season_id, self.season_a.id)
        self.assertEqual(result["created"][0]["league_id"], self.league_a.id)

    def test_publish_prevalidates_legacy_wrong_league_draft(self):
        game = Game(
            id="legacy_wrong_league",
            home_team_id=self.a1.id,
            away_team_id=self.a2.id,
            season_id=self.season_a.id,
            division_id=self.div_a.id,
            ice_slot_id=self.cross.id,
            start_time=self.cross.start_time,
            end_time=self.cross.end_time,
            rink=self.rink_b.name,
            published=False,
            is_draft=True,
        )
        self.store.add_game(game)
        api = ApiService(self.store)
        result = api.publish_draft_games(game_ids=[game.id])
        self.assertEqual(
            result["error"]["details"]["reason"], "cross_league_slot")
        self.assertTrue(game.is_draft)
        self.assertFalse(game.published)
        self.assertEqual(self.cross.status, IceSlotStatus.AVAILABLE)

        review = api.list_draft_games()
        row = next(r for r in review["draft_games"] if r["game_id"] == game.id)
        self.assertIn("wrong_league_ice", row["issues"])

    def test_legacy_conflicting_season_and_division_is_reported(self):
        game = Game(
            id="legacy_conflicting_context",
            home_team_id=self.a1.id,
            away_team_id=self.a2.id,
            season_id=self.season_a.id,
            division_id=self.div_b.id,
            ice_slot_id=self.same_1.id,
            start_time=self.same_1.start_time,
            end_time=self.same_1.end_time,
            rink=self.rink_a.name,
            published=False,
            is_draft=True,
        )
        self.store.add_game(game)
        api = ApiService(self.store)

        result = api.publish_draft_games(game_ids=[game.id])
        self.assertEqual(
            result["error"]["details"]["reason"], "game_league_ambiguous")
        self.assertEqual(
            set(result["error"]["details"]["league_ids"]),
            {self.league_a.id, self.league_b.id})
        self.assertTrue(game.is_draft)
        self.assertFalse(game.published)

        review = api.list_draft_games()
        row = next(r for r in review["draft_games"] if r["game_id"] == game.id)
        self.assertIn("league_scope_missing", row["issues"])

    def test_no_assigned_ice_has_clear_unscheduled_reason(self):
        # Simulate malformed legacy inventory after initial setup.
        self.venue_a.league_id = None
        self.store.save_venue(self.venue_a)
        result = ApiService(self.store).draft_season_schedule(self.div_a.id)
        self.assertEqual(result["draft_games"], [])
        self.assertIn("assigned to this league", result["unscheduled"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
