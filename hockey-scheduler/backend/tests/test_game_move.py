import unittest
from datetime import datetime, timezone

from helpers import FakeClock

from hockey_scheduler.domain import IceSlotStatus, IceSlotType
from hockey_scheduler.domain.errors import (
    NotFoundError,
    ScheduleConflictError,
    ValidationError,
)
from hockey_scheduler.services import SetupService
from hockey_scheduler.store import InMemoryStore

UTC = timezone.utc


def dt(h, m=0):
    return datetime(2026, 9, 1, h, m, tzinfo=UTC)


def universe():
    store = InMemoryStore()
    svc = SetupService(store, clock=FakeClock())
    league = svc.create_league("L")
    season = svc.create_season(league.id, "S")
    div = svc.create_division(season.id, "U16")
    home = svc.create_team(svc.create_club("LH").id, div.id, "Lions")
    away = svc.create_team(svc.create_club("FH").id, div.id, "Falcons")
    bears = svc.create_team(svc.create_club("BH").id, div.id, "Bears")
    eagles = svc.create_team(svc.create_club("EH").id, div.id, "Eagles")
    venue = svc.create_venue("Arena")
    r1 = svc.create_rink(venue.id, "Rink 1")
    r2 = svc.create_rink(venue.id, "Rink 2")
    slot_a = svc.create_ice_slot(r1.id, dt(18, 30), dt(20))      # game lives here
    slot_b = svc.create_ice_slot(r2.id, dt(20, 30), dt(22))      # free target
    game = svc.create_game(season.id, div.id, home.id, away.id, slot_a.id)
    ctx = dict(store=store, svc=svc, season=season, div=div, home=home, away=away,
               bears=bears, eagles=eagles, r1=r1, r2=r2,
               slot_a=slot_a, slot_b=slot_b, game=game)
    return ctx


class GameMoveTest(unittest.TestCase):
    def test_valid_move_frees_old_and_allocates_new(self):
        c = universe()
        moved = c["svc"].move_game(c["game"].id, c["slot_b"].id, reason="drag")
        self.assertEqual(moved.ice_slot_id, c["slot_b"].id)
        self.assertEqual(moved.start_time, dt(20, 30))
        self.assertEqual(moved.rink, "Rink 2")
        self.assertEqual(c["store"].get_ice_slot(c["slot_a"].id).status,
                         IceSlotStatus.AVAILABLE)
        self.assertEqual(c["store"].get_ice_slot(c["slot_b"].id).status,
                         IceSlotStatus.ALLOCATED)

    def test_move_to_allocated_slot_rejected(self):
        c = universe()
        # Allocate slot_b with an unrelated game (Bears vs Eagles).
        c["svc"].create_game(c["season"].id, c["div"].id,
                             c["bears"].id, c["eagles"].id, c["slot_b"].id)
        with self.assertRaises(ScheduleConflictError):
            c["svc"].move_game(c["game"].id, c["slot_b"].id)

    def test_move_to_maintenance_slot_rejected(self):
        c = universe()
        maint = c["svc"].create_ice_slot(c["r2"].id, dt(6), dt(7),
                                         slot_type=IceSlotType.MAINTENANCE)
        with self.assertRaises(ValidationError):
            c["svc"].move_game(c["game"].id, maint.id)

    def test_move_team_overlap_rejected(self):
        c = universe()
        venue_rink3 = c["svc"].create_rink(
            c["store"].all_venues()[0].id, "Rink 3")
        # Falcons also play a later game (21:00–22:30 on Rink 1, no overlap there).
        slot_c = c["svc"].create_ice_slot(c["r1"].id, dt(21), dt(22, 30))
        c["svc"].create_game(c["season"].id, c["div"].id,
                             c["away"].id, c["bears"].id, slot_c.id)
        # A free target on Rink 3 overlapping slot_c's time.
        slot_d = c["svc"].create_ice_slot(venue_rink3.id, dt(21), dt(22, 30))
        # Moving game1 (has Falcons) onto it conflicts with the Falcons game.
        with self.assertRaises(ScheduleConflictError):
            c["svc"].move_game(c["game"].id, slot_d.id)

    def test_published_game_move_unpublishes(self):
        c = universe()
        c["svc"].publish_game(c["game"].id)
        moved = c["svc"].move_game(c["game"].id, c["slot_b"].id)
        self.assertFalse(moved.published)

    def test_move_locked_game_unlocks_roster_for_reconfirmation(self):
        c = universe()
        c["svc"].publish_game(c["game"].id)
        c["game"].locked = True
        c["store"].save_game(c["game"])
        moved = c["svc"].move_game(c["game"].id, c["slot_b"].id)
        self.assertFalse(moved.locked)       # unlocked for reconfirmation
        self.assertFalse(moved.published)    # and unpublished
        audit = [a for a in c["store"].all_setup_audit()
                 if a.action == "game_moved"][-1]
        self.assertTrue(audit.detail["roster_unlocked"])
        self.assertTrue(audit.detail["unpublished"])

    def test_move_audits_old_and_new_slot(self):
        c = universe()
        c["svc"].move_game(c["game"].id, c["slot_b"].id, reason="drag")
        moves = [a for a in c["store"].all_setup_audit() if a.action == "game_moved"]
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0].detail["old_slot_id"], c["slot_a"].id)
        self.assertEqual(moves[0].detail["new_slot_id"], c["slot_b"].id)

    def test_move_same_slot_rejected(self):
        c = universe()
        with self.assertRaises(ValidationError):
            c["svc"].move_game(c["game"].id, c["slot_a"].id)

    def test_move_missing_game_or_slot(self):
        c = universe()
        with self.assertRaises(NotFoundError):
            c["svc"].move_game("game_missing", c["slot_b"].id)
        with self.assertRaises(NotFoundError):
            c["svc"].move_game(c["game"].id, "slot_missing")


if __name__ == "__main__":
    unittest.main()
