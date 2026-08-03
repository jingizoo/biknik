"""#387 — the demo's ice inventory is RELATIVE to a seed instant.

``full_demo.py`` used to pin its ice to three absolute dates (2026-09-05, the
core scenario's "Saturday"; 2026-09-12, the unrostered "next game"; and
2026-09-06 + 14 days, the pilot-scale pack). Absolute seed dates are a
countdown, not data: once real time passes them, every freshly-seeded demo is
born past-dated, and each surface that reasons about "now" degrades silently
rather than failing — no next game on the Player Home Page, no acceptable
substitute offer, no deletable ice.

Nothing in the suite could catch that, because the suite runs at the current
wall clock: while today is still BEFORE September 2026, past-dated and
future-dated seeds are indistinguishable. So the coverage here is written
against seed instants the caller chooses — including one years past the old
dates — which is the only way to observe the failure at all.

Four properties are proven:

1. **Not past-dated.** A demo seeded well past September 2026 puts every ice
   slot and every game strictly after its own seed instant, still has
   schedulable game ice, still generates a draft schedule, and still answers
   "what is this player's next game?".
2. **Exact, not vague.** The whole inventory layout is pinned as an exact set
   of (start, end, rink, type) tuples derived from the same seed instant the
   fixture used — so this fails if the data moves, not merely if it vanishes.
3. **Deterministic.** The same seed instant rebuilds a byte-identical
   inventory across store backends (Memory / SQLite / PostgreSQL) and across
   processes started with different ``PYTHONHASHSEED`` values.
4. **Hour-independent.** The 24-hour faked-clock sweep #384 established, run
   over every hour of a UTC day (and every weekday of a fortnight): the
   derivation must not inherit the time of day it was called at. That is the
   exact bug class that took main red on 2026-08-02 — a fixture booking ice at
   ``now + 31..34 days`` inherited the current HOUR and collided with these
   very slots only between 15:00 and 21:00 UTC.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import IceSlotType
from hockey_scheduler.full_demo import build_full_demo_store, demo_day_zero
from hockey_scheduler.services import RosterService, SetupService
from hockey_scheduler.store import SqlStore

UTC = timezone.utc

# Well past the retired absolute dates (2026-09-05 … 2026-09-19), and
# deliberately NOT midnight and NOT a Saturday: 2031-04-17 is a Thursday, so
# both the time-of-day and the weekday have to be discarded by the derivation.
FAR_FUTURE = datetime(2031, 4, 17, 13, 5, 41, 123456, tzinfo=UTC)

# The dates the demo used to be pinned to, named once so the regressions below
# can state exactly what must no longer appear.
RETIRED_DAY_ZERO = datetime(2026, 9, 5, tzinfo=UTC)


def inventory(store):
    """A canonical, backend-independent rendering of the demo's ice.

    Rinks are named, not id'd, so the comparison is over the DATA rather than
    over id-allocation order; sorted, so it does not depend on a store's
    iteration order.
    """
    rinks = {r.id: r.name for r in store.all_rinks()}
    return sorted(
        [s.start_time.astimezone(UTC).isoformat(),
         s.end_time.astimezone(UTC).isoformat(),
         rinks[s.rink_id], s.slot_type.value, s.status.value]
        for s in store.all_ice_slots())


def inventory_digest(store):
    return hashlib.sha256(
        json.dumps(inventory(store), sort_keys=True).encode()).hexdigest()


def expected_layout(seed_instant):
    """The exact (start, end, rink, type) the demo must lay down for
    ``seed_instant`` — spelled out here rather than read back off the store,
    so this is a specification of the inventory and not a restatement of it.

    Statuses are deliberately excluded: which game slots end up ``allocated``
    is decided by the round-robin assignment, which this file does not
    duplicate. ``test_allocated_slots_are_exactly_the_games`` pins that
    separately, against the games actually created.
    """
    d0 = demo_day_zero(seed_instant)
    rinks = ("Main Rink", "Training Rink", "Lakeside Rink")
    rows = [
        [d0.replace(hour=16), d0.replace(hour=17, minute=30), "Main Rink", "game"],
        [d0.replace(hour=18, minute=30), d0.replace(hour=20), "Main Rink", "game"],
        [d0.replace(hour=20, minute=30), d0.replace(hour=22), "Main Rink", "game"],
        # The unrostered "next game" a week after day zero.
        [(d0 + timedelta(days=7)).replace(hour=18, minute=30),
         (d0 + timedelta(days=7)).replace(hour=20), "Main Rink", "game"],
    ]
    for offset in range(14):
        d = d0 + timedelta(days=offset + 1)
        for rink in rinks:
            start, end = d.replace(hour=18), d.replace(hour=19, minute=30)
            # Main Rink's day-zero+7 evening is already taken by the "next
            # game" slot above (18:30–20:00 overlaps 18:00–19:30), so the
            # pack skips it rather than crashing the seed on a collision.
            if not (rink == "Main Rink" and offset == 6):
                rows.append([start, end, rink, "game"])
            if offset % 4 == 0:
                rows.append([d.replace(hour=16), d.replace(hour=17, minute=15),
                             rink, "practice"])
    return sorted([s.isoformat(), e.isoformat(), rink, kind]
                  for s, e, rink, kind in rows)


def layout_only(store):
    """``inventory`` with the status column dropped."""
    return sorted(row[:4] for row in inventory(store))


class DemoIsNotPastDatedTest(unittest.TestCase):
    """A demo seeded years after the retired dates is still a live demo."""

    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store(
            seed_instant=FAR_FUTURE)
        self.api = ApiService(self.store)
        self.day_zero = demo_day_zero(FAR_FUTURE)

    def test_every_slot_and_game_starts_after_the_seed_instant(self):
        slots = self.store.all_ice_slots()
        self.assertEqual(len(slots), 57)      # premise: the inventory is here
        for s in slots:
            self.assertGreater(s.start_time, FAR_FUTURE, s.id)
        games = self.store.all_games()
        self.assertEqual(len(games), 20)
        for g in games:
            self.assertGreater(g.start_time, FAR_FUTURE, g.id)
        # Exact, not merely "in the future": the earliest ice is day zero's
        # 16:00 practice-hour game slot and the latest starts a fortnight on.
        self.assertEqual(min(s.start_time for s in slots),
                         self.day_zero.replace(hour=16))
        self.assertEqual(max(s.start_time for s in slots),
                         (self.day_zero + timedelta(days=14)).replace(hour=18))

    def test_no_slot_lands_on_the_retired_september_2026_dates(self):
        # The specific failure being regressed: a seed instant five years on
        # must not reproduce the old fixed calendar window.
        retired = {(RETIRED_DAY_ZERO + timedelta(days=n)).date()
                   for n in range(15)}
        self.assertEqual(
            {s.start_time.date() for s in self.store.all_ice_slots()} & retired,
            set())

    def test_schedulable_game_ice_survives_the_seed_instant(self):
        free = [s for s in self.store.all_ice_slots()
                if s.slot_type == IceSlotType.GAME
                and s.status.value == "available"
                and self.store.game_using_ice_slot(s.id) is None]
        self.assertEqual(len(free), 25)
        for s in free:
            self.assertGreater(s.start_time, FAR_FUTURE, s.id)

    def test_future_only_ice_operations_are_available_at_the_seed_instant(self):
        # delete_ice_slot refuses anything at or before its clock ("past slots
        # are history"), so it succeeds here only because the seeded ice really
        # is in this operator's future. A past-dated seed makes the whole
        # ice-inventory surface read-only.
        setup = SetupService(self.store, clock=lambda: FAR_FUTURE)
        free = sorted((s for s in self.store.all_ice_slots()
                       if s.status.value == "available"),
                      key=lambda s: (s.start_time, s.id))
        deleted = setup.delete_ice_slot(free[0].id, actor_id="user_admin")
        self.assertEqual(deleted.id, free[0].id)
        self.assertIsNone(self.store.get_ice_slot(free[0].id))

    def test_draft_schedule_is_generatable_and_lands_in_the_future(self):
        proposal = self.api.draft_season_schedule(
            division_id=self.ids["division_id"])
        self.assertNotIn("error", proposal)
        self.assertEqual(len(proposal["draft_games"]), 12)
        self.assertEqual(proposal["unscheduled"], [])
        self.assertEqual(proposal["unschedulable_teams"], [])
        for g in proposal["draft_games"]:
            self.assertGreater(datetime.fromisoformat(g["start_time"]),
                               FAR_FUTURE, g)

    def test_player_next_game_resolves_at_the_seed_instant(self):
        # find_next_game_for_player filters on `start_time >= clock()`, so a
        # past-dated seed returns None and the Player Home Page's headline
        # card goes empty on a brand-new demo.
        roster = RosterService(self.store, clock=lambda: FAR_FUTURE)
        nxt = roster.find_next_game_for_player(self.ids["selected_player_id"])
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.id, self.game_id)
        self.assertEqual(nxt.start_time, self.day_zero.replace(hour=18, minute=30))


class DemoLayoutIsExactTest(unittest.TestCase):
    """The inventory is pinned exactly, and pinned TO the seed instant."""

    def test_layout_matches_the_specification_for_a_far_future_instant(self):
        store, _gid, _ids = build_full_demo_store(seed_instant=FAR_FUTURE)
        self.assertEqual(layout_only(store), expected_layout(FAR_FUTURE))

    def test_layout_matches_the_specification_for_a_pre_2026_instant(self):
        # Before the retired dates as well as after them: the layout is a
        # function of the instant, not a floor or a clamp.
        early = datetime(2019, 11, 30, 4, 0, tzinfo=UTC)   # a Saturday
        store, _gid, _ids = build_full_demo_store(seed_instant=early)
        self.assertEqual(layout_only(store), expected_layout(early))
        self.assertEqual(demo_day_zero(early), datetime(2019, 12, 7, tzinfo=UTC))

    def test_allocated_slots_are_exactly_the_games(self):
        store, _gid, _ids = build_full_demo_store(seed_instant=FAR_FUTURE)
        allocated = {s.start_time for s in store.all_ice_slots()
                     if s.status.value == "allocated"}
        self.assertEqual(len([s for s in store.all_ice_slots()
                              if s.status.value == "allocated"]), 20)
        self.assertEqual(allocated, {g.start_time for g in store.all_games()})

    def test_day_zero_is_the_first_saturday_at_least_a_week_out(self):
        # Every UTC day of a fortnight, spelled out — the derivation must
        # discard the weekday it was called on and land on Saturday with a
        # lead of 7 to 13 days.
        base = datetime(2026, 8, 3, tzinfo=UTC)             # a Monday
        expected = [
            datetime(2026, 8, 15, tzinfo=UTC),   # Mon 08-03 → +12
            datetime(2026, 8, 15, tzinfo=UTC),   # Tue 08-04 → +11
            datetime(2026, 8, 15, tzinfo=UTC),   # Wed 08-05 → +10
            datetime(2026, 8, 15, tzinfo=UTC),   # Thu 08-06 → +9
            datetime(2026, 8, 15, tzinfo=UTC),   # Fri 08-07 → +8
            datetime(2026, 8, 15, tzinfo=UTC),   # Sat 08-08 → +7
            datetime(2026, 8, 22, tzinfo=UTC),   # Sun 08-09 → +13
            datetime(2026, 8, 22, tzinfo=UTC),   # Mon 08-10 → +12
            datetime(2026, 8, 22, tzinfo=UTC),   # Tue 08-11 → +11
            datetime(2026, 8, 22, tzinfo=UTC),   # Wed 08-12 → +10
            datetime(2026, 8, 22, tzinfo=UTC),   # Thu 08-13 → +9
            datetime(2026, 8, 22, tzinfo=UTC),   # Fri 08-14 → +8
            datetime(2026, 8, 22, tzinfo=UTC),   # Sat 08-15 → +7
            datetime(2026, 8, 29, tzinfo=UTC),   # Sun 08-16 → +13
        ]
        self.assertEqual(
            [demo_day_zero(base + timedelta(days=n)) for n in range(14)],
            expected)

    def test_day_zero_rejects_a_naive_instant(self):
        # Guessing UTC for a naive value would shift the whole inventory by
        # the caller's offset, silently.
        with self.assertRaises(ValueError):
            demo_day_zero(datetime(2031, 4, 17, 13, 5))
        with self.assertRaises(ValueError):
            demo_day_zero("2031-04-17T13:05:00+00:00")

    def test_a_non_utc_instant_is_normalized_not_rejected(self):
        # 2031-04-17 23:30 at +05:00 is 18:30 UTC the same day, so it must
        # derive the SAME day zero as the UTC instant — the offset is
        # converted, never dropped.
        offset_instant = datetime(2031, 4, 17, 18, 30,
                                  tzinfo=timezone(timedelta(hours=5)))
        self.assertEqual(demo_day_zero(offset_instant),
                         demo_day_zero(datetime(2031, 4, 17, 13, 30, tzinfo=UTC)))


class DemoDeterminismTest(unittest.TestCase):
    """The same seed instant rebuilds the same inventory, everywhere."""

    def _stores(self):
        yield "memory", None
        yield "sqlite-memory", ":memory:"
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        yield "sqlite-file", path
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            yield "postgres", url

    def test_identical_across_store_backends(self):
        digests = {}
        for label, url in self._stores():
            if url is None:
                store = None
            else:
                store = SqlStore(url)
                store.reset_schema()
            built, _gid, _ids = build_full_demo_store(store,
                                                      seed_instant=FAR_FUTURE)
            digests[label] = inventory_digest(built)
            # Premise guard: really on the backend this label claims, so an
            # equal digest is evidence about several stores and not one store
            # measured several times.
            self.assertEqual(built.__class__.__name__,
                             "InMemoryStore" if url is None else "SqlStore",
                             label)
            if url is not None:
                built.close()
        # 3 without a Postgres URL, 4 with one (CI and run_parallel set it).
        self.assertGreaterEqual(len(digests), 3, digests)
        self.assertEqual(len(set(digests.values())), 1, digests)

    def test_identical_across_processes_with_different_hash_seeds(self):
        script = (
            "import sys, os; sys.path.insert(0, os.environ['HS_BACKEND']);"
            "sys.path.insert(0, os.environ['HS_TESTS']);"
            "from test_demo_relative_ice import inventory_digest, FAR_FUTURE;"
            "from hockey_scheduler.full_demo import build_full_demo_store;"
            "s, _g, _i = build_full_demo_store(seed_instant=FAR_FUTURE);"
            "print(inventory_digest(s))"
        )
        env = dict(os.environ,
                   HS_BACKEND=str(BACKEND), HS_TESTS=str(BACKEND / "tests"))
        digests = {}
        for seed in ("0", "1", "4242"):
            env["PYTHONHASHSEED"] = seed
            out = subprocess.run([sys.executable, "-c", script], env=env,
                                 capture_output=True, text=True, check=True)
            digests[seed] = out.stdout.strip()
        self.assertTrue(all(digests.values()), digests)
        self.assertEqual(len(set(digests.values())), 1, digests)
        # And the child processes agree with this one.
        store, _gid, _ids = build_full_demo_store(seed_instant=FAR_FUTURE)
        self.assertEqual(set(digests.values()), {inventory_digest(store)})

    def test_different_seed_instants_produce_different_inventories(self):
        # The complement of determinism, and the guard against a derivation
        # that quietly ignores its argument: two instants a week apart must
        # NOT collapse to the same inventory.
        a, _g, _i = build_full_demo_store(seed_instant=FAR_FUTURE)
        b, _g, _i = build_full_demo_store(
            seed_instant=FAR_FUTURE + timedelta(days=7))
        self.assertNotEqual(inventory_digest(a), inventory_digest(b))


class DemoSeedInstantSweepTest(unittest.TestCase):
    """The 24-hour faked-clock sweep #384 established, for the new fixture."""

    def test_inventory_is_identical_at_every_hour_of_a_utc_day(self):
        # The 2026-08-02 outage was hour-dependent: a fixture inherited the
        # current HOUR and collided with the seeded evening slots only between
        # 15:00 and 21:00 UTC, so the same tree passed at 12:43Z and failed at
        # 15:08Z. Anything that derives ice from "now" must be swept over the
        # whole day, not sampled once.
        digests = set()
        for hour in range(24):
            instant = datetime(2026, 8, 3, hour, hour * 2, hour, tzinfo=UTC)
            store, _gid, _ids = build_full_demo_store(seed_instant=instant)
            digests.add(inventory_digest(store))
            for s in store.all_ice_slots():
                self.assertGreater(s.start_time, instant, (hour, s.id))
        self.assertEqual(len(digests), 1, "inventory varied by hour of day")

    def test_every_instant_of_a_fortnight_yields_future_saturday_ice(self):
        # 14 days x 24 hours of day-zero derivations (pure, so cheap): always
        # a Saturday, always 7-13 days out, always after the instant itself.
        base = datetime(2026, 8, 3, tzinfo=UTC)
        for day in range(14):
            for hour in range(24):
                instant = base + timedelta(days=day, hours=hour, minutes=59,
                                           seconds=59)
                d0 = demo_day_zero(instant)
                self.assertEqual(d0.weekday(), 5, instant)      # Saturday
                self.assertEqual(d0.time(), datetime.min.time(), instant)
                lead = (d0 - instant.replace(hour=0, minute=0, second=0)).days
                self.assertGreaterEqual(lead, 7, instant)
                self.assertLessEqual(lead, 13, instant)
                # The earliest slot the demo lays down is day zero 16:00.
                self.assertGreater(d0.replace(hour=16), instant, instant)


class DemoSeedInstantDefaultTest(unittest.TestCase):
    """Omitted, the instant comes from the seed path's own clock."""

    def test_default_instant_is_the_setup_service_clock(self):
        # Not a second, independent datetime.now(): patching the clock the
        # seed path already runs on must move the whole inventory. If
        # full_demo grew its own now() this would keep building today's data.
        from hockey_scheduler import full_demo as fd

        pinned = datetime(2033, 2, 14, 7, 45, tzinfo=UTC)
        real_init = SetupService.__init__

        def pinned_init(self, store, clock=None):
            real_init(self, store, clock=lambda: pinned)

        SetupService.__init__ = pinned_init
        try:
            store, _gid, _ids = fd.build_full_demo_store()
        finally:
            SetupService.__init__ = real_init
        self.assertEqual(min(s.start_time for s in store.all_ice_slots()),
                         demo_day_zero(pinned).replace(hour=16))
        self.assertEqual(layout_only(store), expected_layout(pinned))


class OtherSeedFixturesAreRelativeTest(unittest.TestCase):
    """The audit half of #387: the demo's ice was not the only pinned date."""

    def test_first_slice_seed_game_is_relative_to_its_instant(self):
        # seed.py's game sat at 2026-07-04 — a date real time had ALREADY
        # passed, which is why hockey_scheduler.demo was walking an operator
        # through accepting a substitute for a game that had been played.
        from hockey_scheduler.seed import build_seeded_store

        store, game_id = build_seeded_store(seed_instant=FAR_FUTURE)
        game = store.get_game(game_id)
        self.assertEqual(game.start_time,
                         demo_day_zero(FAR_FUTURE).replace(hour=18, minute=30))
        self.assertGreater(game.start_time, FAR_FUTURE)

    def test_first_slice_seed_game_is_future_dated_by_default(self):
        from hockey_scheduler.seed import build_seeded_store

        store, game_id = build_seeded_store()
        self.assertGreater(store.get_game(game_id).start_time,
                           datetime.now(UTC) + timedelta(days=7))

    def test_backup_acceptance_sample_ice_is_relative_to_its_instant(self):
        from hockey_scheduler.acceptance import backup_restore

        api = ApiService()
        backup_restore.configure_sample(api, seed_instant=FAR_FUTURE)
        slots = api.store.all_ice_slots()
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0].start_time,
                         demo_day_zero(FAR_FUTURE).replace(hour=18, minute=30))

    def test_no_seed_or_demo_module_pins_a_calendar_date(self):
        """No seed/demo module may name a YYYY-MM-DD date or build a
        ``datetime(YYYY, M, D)`` in executable code.

        This is the structural half of the fix, and it is what keeps
        ``setup_demo.py`` — a README-documented script with no behavioural
        test of its own — from quietly re-acquiring a pinned date. Comments
        and docstrings are exempt (the modules explain the retired dates by
        name on purpose); only code the interpreter actually runs is checked.
        """
        import ast

        modules = ["hockey_scheduler/full_demo.py", "hockey_scheduler/seed.py",
                   "hockey_scheduler/setup_demo.py",
                   "hockey_scheduler/acceptance/backup_restore.py"]
        checked = 0
        for rel in modules:
            path = BACKEND / rel
            tree = ast.parse(path.read_text())
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                    body = getattr(node, "body", None)
                    if (body and isinstance(body[0], ast.Expr)
                            and isinstance(body[0].value, ast.Constant)
                            and isinstance(body[0].value.value, str)):
                        docstrings.add(id(body[0].value))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and id(node) not in docstrings):
                    self.assertNotRegex(
                        node.value, r"\d{4}-\d{2}-\d{2}",
                        f"{rel}:{node.lineno} pins a calendar date")
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "datetime"
                        and sum(isinstance(a, ast.Constant)
                                and isinstance(a.value, int)
                                for a in node.args) >= 3):
                    self.fail(f"{rel}:{node.lineno} builds a fixed datetime")
            checked += 1
        # Premise: every listed module was really parsed, so a typo in a path
        # cannot turn this into a check of nothing.
        self.assertEqual(checked, len(modules))


if __name__ == "__main__":
    unittest.main()
