"""Effective ice-operations policy + its persistence (#277 Slice B).

Two things are pinned here:

  * the pure ``effective_policy`` resolver — per field, the Rink's own value,
    else the Program's, else the system default (turnover 0 / no curfew /
    buffers 0), with 0 treated as a real override rather than "inherit"; and
  * that the new columns from migration 046 — ``ice_slots`` warm-up/resurface
    buffers and the ``rinks`` / ``programs`` policy fields — round-trip on
    Memory / SQLite / PostgreSQL, including the NOT NULL DEFAULT 0 backfill for
    a slot that names no buffers.
"""

import os
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain.setup_models import (
    IceSlot, Program, Rink, Venue)
from hockey_scheduler.services.ice_policy import (
    DEFAULT_TURNOVER_MINUTES, IcePolicy, effective_policy)
from hockey_scheduler.store import InMemoryStore, SqlStore

UTC = timezone.utc


def dt(h):
    return datetime(2026, 9, 1, h, tzinfo=UTC)


class EffectivePolicyTest(unittest.TestCase):
    """The pure resolver — no store, no I/O."""

    def _rink(self, **kw):
        return Rink(id="rink_1", venue_id="venue_1", name="Rink A", **kw)

    def _program(self, **kw):
        return Program(id="prog_1", name="AHL", **kw)

    def test_system_defaults_when_nothing_set(self):
        self.assertEqual(
            effective_policy(self._rink(), self._program()),
            IcePolicy(turnover_minutes=0, warmup_minutes=0,
                      resurface_minutes=0, curfew_local=None))

    def test_program_value_used_when_rink_unset(self):
        p = effective_policy(
            self._rink(),
            self._program(turnover_minutes=15, curfew_local="23:00"))
        self.assertEqual(p.turnover_minutes, 15)
        self.assertEqual(p.curfew_local, "23:00")

    def test_rink_overrides_program(self):
        p = effective_policy(
            self._rink(turnover_minutes=20, curfew_local="22:30"),
            self._program(turnover_minutes=15, curfew_local="23:00"))
        self.assertEqual(p.turnover_minutes, 20)
        self.assertEqual(p.curfew_local, "22:30")

    def test_rink_zero_is_an_explicit_override_not_inherit(self):
        # 0 is a real value (not None), so a rink that explicitly sets turnover
        # to 0 turns it OFF — it must win over a non-zero Program default rather
        # than being read as "inherit".
        p = effective_policy(self._rink(turnover_minutes=0),
                             self._program(turnover_minutes=15))
        self.assertEqual(p.turnover_minutes, 0)

    def test_fields_resolve_independently(self):
        p = effective_policy(
            self._rink(warmup_minutes=10),
            self._program(turnover_minutes=15, resurface_minutes=12))
        self.assertEqual(p.warmup_minutes, 10)      # from the rink
        self.assertEqual(p.turnover_minutes, 15)    # from the program
        self.assertEqual(p.resurface_minutes, 12)   # from the program
        self.assertIsNone(p.curfew_local)           # system default

    def test_program_none_falls_back_to_system_default(self):
        p = effective_policy(self._rink(warmup_minutes=5), None)
        self.assertEqual(p.warmup_minutes, 5)
        self.assertEqual(p.turnover_minutes, DEFAULT_TURNOVER_MINUTES)
        self.assertIsNone(p.curfew_local)


class PolicyPersistenceContract:
    """Migration 046's new columns survive a write/read on every backend."""

    def _store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self._store()
        self.store.add_venue(Venue(id="venue_1", name="Main Arena"))

    def test_rink_policy_fields_round_trip(self):
        self.store.add_rink(Rink(
            id="rink_1", venue_id="venue_1", name="Rink A",
            turnover_minutes=15, curfew_local="23:00"))
        r = self.store.get_rink("rink_1")
        self.assertEqual(r.turnover_minutes, 15)
        self.assertEqual(r.curfew_local, "23:00")
        self.assertIsNone(r.warmup_minutes)      # unset -> NULL -> None
        self.assertIsNone(r.resurface_minutes)

    def test_program_policy_fields_round_trip(self):
        self.store.add_program(Program(
            id="prog_1", name="AHL", turnover_minutes=20, warmup_minutes=8))
        p = self.store.get_program("prog_1")
        self.assertEqual(p.turnover_minutes, 20)
        self.assertEqual(p.warmup_minutes, 8)
        self.assertIsNone(p.curfew_local)
        self.assertIsNone(p.resurface_minutes)

    def test_iceslot_buffers_round_trip(self):
        self.store.add_rink(Rink(id="rink_1", venue_id="venue_1", name="A"))
        self.store.add_ice_slot(IceSlot(
            id="slot_1", rink_id="rink_1", start_time=dt(18), end_time=dt(20),
            warmup_minutes=10, resurface_minutes=12))
        s = self.store.get_ice_slot("slot_1")
        self.assertEqual(s.warmup_minutes, 10)
        self.assertEqual(s.resurface_minutes, 12)

    def test_iceslot_buffers_default_zero(self):
        # A slot created without buffers reads back 0/0 (the NOT NULL DEFAULT),
        # so its playable window equals its reserved window.
        self.store.add_rink(Rink(id="rink_1", venue_id="venue_1", name="A"))
        self.store.add_ice_slot(IceSlot(
            id="slot_2", rink_id="rink_1", start_time=dt(18), end_time=dt(20)))
        s = self.store.get_ice_slot("slot_2")
        self.assertEqual(s.warmup_minutes, 0)
        self.assertEqual(s.resurface_minutes, 0)


class MemoryPolicyPersistenceTest(PolicyPersistenceContract, unittest.TestCase):
    def _store(self):
        return InMemoryStore()


class SqlPolicyPersistenceTest(PolicyPersistenceContract, unittest.TestCase):
    def _store(self):
        return SqlStore(":memory:")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresPolicyPersistenceTest(PolicyPersistenceContract, unittest.TestCase):
    def _store(self):
        store = SqlStore(os.environ["TEST_DATABASE_URL"])
        store.clear_all_data()
        return store


if __name__ == "__main__":
    unittest.main()
