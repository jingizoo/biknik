"""Shared final slot-conflict check (#277 Slice A).

create_game and move_game route through SetupService._assert_slot_free_for_game,
so they enforce identical slot + team-overlap rules and emit the same
machine-readable reason codes. The draft-commit path routes through its
slot-scoped half, ._assert_slot_free (the slot exists, is a GAME slot, is
AVAILABLE, and is not already held by another active game) — a committed draft
occupies its ice, but a draft's team double-bookings are surfaced in review, not
rejected at commit. This pins the checker's contract directly and confirms
create_game now surfaces the same structured ``details["reason"]`` codes
move_game already did. Runs on Memory / SQLite / PostgreSQL.
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
        # game (legacy/imported data, or a slot manually flipped back), which the
        # status check alone would miss.
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

    def test_slot_free_stops_at_slot_and_ignores_team_overlap(self):
        # The slot-scoped half (what the draft-commit path uses) enforces slot
        # freeness only: slot_overlap is a free GAME slot, so it returns even
        # though `home` already plays an overlapping game. A draft's team
        # double-booking is surfaced in review, not rejected at commit — the
        # deliberate difference from the full create/move checker above.
        got = self.svc._assert_slot_free(self.slot_overlap.id)
        self.assertEqual(got.id, self.slot_overlap.id)

    def test_slot_free_still_enforces_slot_rules(self):
        # ...but it is not a no-op: the same slot_missing / not_game_slot /
        # slot_unavailable / slot_already_filled codes still fire.
        with self.assertRaises(ScheduleConflictError) as cm:
            self.svc._assert_slot_free(self.slot_a.id)  # ALLOCATED by self.game
        self.assertEqual(cm.exception.details["reason"], "slot_unavailable")

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
