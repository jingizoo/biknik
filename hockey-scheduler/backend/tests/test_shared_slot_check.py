"""Shared final slot-conflict check (#277 Slice A).

create_game, move_game, and the draft-commit path now all route through
SetupService._assert_slot_free_for_game, so they enforce identical slot +
team-overlap rules and emit the same machine-readable reason codes. This pins
the checker's contract directly and confirms create_game now surfaces the same
structured ``details["reason"]`` codes move_game already did. Runs on Memory /
SQLite / PostgreSQL.
"""

import os
import unittest
from datetime import datetime, timezone

from helpers import FakeClock

from hockey_scheduler.domain import IceSlotStatus, IceSlotType
from hockey_scheduler.domain.errors import (
    NotFoundError, ScheduleConflictError, ValidationError)
from hockey_scheduler.services import SetupService
from hockey_scheduler.store import InMemoryStore, SqlStore

UTC = timezone.utc


def dt(h, m=0):
    return datetime(2026, 9, 1, h, m, tzinfo=UTC)


class SharedSlotCheckContract:
    def _store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self._store()
        s = self.svc = SetupService(self.store, clock=FakeClock())
        league = s.create_program("L")
        self.season = s.create_season(league.id, "S")
        self.div = s.create_division(self.season.id, "U16")
        self.home = s.create_team(s.create_club("LH").id, self.div.id, "Lions")
        self.away = s.create_team(s.create_club("FH").id, self.div.id, "Falcons")
        self.bears = s.create_team(s.create_club("BH").id, self.div.id, "Bears")
        for t in (self.home, self.away, self.bears):
            s.register_team_for_season(self.season.id, t.id, self.div.id)
        venue = s.create_venue("Arena", league_id=league.id)
        s.grant_season_venue_access(self.season.id, venue.id)
        rink = s.create_rink(venue.id, "Rink 1")
        rink2 = s.create_rink(venue.id, "Rink 2")
        self.slot_a = s.create_ice_slot(rink.id, dt(18, 30), dt(20))       # game here
        self.slot_free = s.create_ice_slot(rink.id, dt(20, 30), dt(22))    # free
        # A different rink at an overlapping TIME — same-rink overlaps are
        # rejected at slot creation, but a team still can't be two places at once.
        self.slot_overlap = s.create_ice_slot(rink2.id, dt(19), dt(20, 30))
        self.maint = s.create_ice_slot(rink.id, dt(22), dt(23), IceSlotType.MAINTENANCE)
        self.game = s.create_game(self.season.id, self.div.id,
                                  self.home.id, self.away.id, self.slot_a.id)

    def _check(self, slot_id, home, away, **kw):
        return self.svc._assert_slot_free_for_game(slot_id, home, away, **kw)

    def test_free_slot_returns_slot(self):
        got = self._check(self.slot_free.id, self.bears.id, self.away.id)
        self.assertEqual(got.id, self.slot_free.id)

    def test_slot_missing(self):
        with self.assertRaises(NotFoundError) as cm:
            self._check("nope", self.bears.id, self.home.id)
        self.assertEqual(cm.exception.details["reason"], "slot_missing")

    def test_not_game_slot(self):
        with self.assertRaises(ValidationError) as cm:
            self._check(self.maint.id, self.bears.id, self.home.id)
        self.assertEqual(cm.exception.details["reason"], "not_game_slot")
        self.assertEqual(cm.exception.details["slot_type"], "maintenance")

    def test_slot_unavailable(self):
        # slot_a is ALLOCATED by the game — status is checked before teams.
        with self.assertRaises(ScheduleConflictError) as cm:
            self._check(self.slot_a.id, self.bears.id, self.home.id)
        self.assertEqual(cm.exception.details["reason"], "slot_unavailable")
        self.assertEqual(cm.exception.details["slot_status"], "allocated")

    def test_slot_already_filled(self):
        # Force the defensive path: a slot still AVAILABLE but already backing a
        # game (the state a not-yet-allocated draft game leaves behind).
        slot = self.store.get_ice_slot(self.slot_a.id)
        slot.status = IceSlotStatus.AVAILABLE
        self.store.save_ice_slot(slot)
        with self.assertRaises(ScheduleConflictError) as cm:
            self._check(self.slot_a.id, self.bears.id, self.home.id)
        self.assertEqual(cm.exception.details["reason"], "slot_already_filled")
        self.assertEqual(cm.exception.details["conflict_game_id"], self.game.id)

    def test_team_overlap(self):
        # home already plays slot_a (18:30-20:00); slot_overlap (19:00-20:30)
        # overlaps it, so home can't take it.
        with self.assertRaises(ScheduleConflictError) as cm:
            self._check(self.slot_overlap.id, self.home.id, self.bears.id)
        self.assertEqual(cm.exception.details["reason"], "team_overlap")
        self.assertEqual(cm.exception.details["conflict_game_id"], self.game.id)

    def test_exclude_game_id_skips_self(self):
        # With the game excluded, its own overlap no longer blocks the slot.
        got = self._check(self.slot_overlap.id, self.home.id, self.away.id,
                          exclude_game_id=self.game.id)
        self.assertEqual(got.id, self.slot_overlap.id)

    def test_create_game_now_carries_reason(self):
        # The additive win of routing create_game through the shared checker:
        # its conflict errors now carry the same details.reason move_game did.
        with self.assertRaises(ValidationError) as cm:
            self.svc.create_game(self.season.id, self.div.id,
                                 self.bears.id, self.away.id, self.maint.id)
        self.assertEqual(cm.exception.details["reason"], "not_game_slot")

    def test_insufficient_playable_time(self):
        # #277: warm-up + resurfacing buffers that consume the whole reserved
        # window leave no playable time, so no game can be placed there.
        slot = self.store.get_ice_slot(self.slot_free.id)   # 20:30-22:00 = 90 min
        slot.warmup_minutes = 60
        slot.resurface_minutes = 40                         # 100 > 90 reserved
        self.store.save_ice_slot(slot)
        with self.assertRaises(ScheduleConflictError) as cm:
            self._check(self.slot_free.id, self.bears.id, self.away.id)
        self.assertEqual(cm.exception.details["reason"],
                         "insufficient_playable_time")

    def test_buffers_within_window_still_playable(self):
        # Buffers that leave positive playable time don't block placement.
        slot = self.store.get_ice_slot(self.slot_free.id)   # 90 reserved minutes
        slot.warmup_minutes = 15
        slot.resurface_minutes = 15                         # 30 -> 60 playable
        self.store.save_ice_slot(slot)
        got = self._check(self.slot_free.id, self.bears.id, self.away.id)
        self.assertEqual(got.id, self.slot_free.id)


class MemorySharedSlotCheckTest(SharedSlotCheckContract, unittest.TestCase):
    def _store(self):
        return InMemoryStore()


class SqlSharedSlotCheckTest(SharedSlotCheckContract, unittest.TestCase):
    def _store(self):
        return SqlStore(":memory:")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresSharedSlotCheckTest(SharedSlotCheckContract, unittest.TestCase):
    def _store(self):
        store = SqlStore(os.environ["TEST_DATABASE_URL"])
        store.clear_all_data()
        return store


if __name__ == "__main__":
    unittest.main()
