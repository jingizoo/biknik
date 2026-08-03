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
5. **Visible.** (#389 review.) Future-dated is not enough on its own — pushed
   far enough out, the demo is future-dated AND invisible, and the landing
   dashboard's "Games this week" tile reads 0 on a brand-new demo. Day zero is
   therefore bounded on both sides: at least a day out, at most six.
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
        # A Saturday instant, deliberately: day zero is now a plain lead, so
        # seeding ON the old anchor weekday gets no special treatment — it
        # lands the same 3 days out as any other day (#389 review).
        self.assertEqual(demo_day_zero(early), datetime(2019, 12, 3, tzinfo=UTC))

    def test_allocated_slots_are_exactly_the_games(self):
        store, _gid, _ids = build_full_demo_store(seed_instant=FAR_FUTURE)
        allocated = {s.start_time for s in store.all_ice_slots()
                     if s.status.value == "allocated"}
        self.assertEqual(len([s for s in store.all_ice_slots()
                              if s.status.value == "allocated"]), 20)
        self.assertEqual(allocated, {g.start_time for g in store.all_games()})

    def test_day_zero_is_a_fixed_lead_from_every_weekday(self):
        # Every UTC day of a fortnight, spelled out. Day zero used to snap to
        # the next Saturday, so this table used to repeat a date six times and
        # jump; it is now a plain +3 from whatever day the demo was seeded on,
        # and the interesting property has inverted: the weekday must make NO
        # difference at all. Any snap-to-weekday rule reintroduced here shows
        # up as a table that stutters (#389 review — see _DEMO_LEAD_DAYS for
        # why a fixed weekday and the dashboard's current week are exclusive).
        base = datetime(2026, 8, 3, tzinfo=UTC)             # a Monday
        expected = [
            datetime(2026, 8, 6, tzinfo=UTC),    # Mon 08-03 → Thu 08-06
            datetime(2026, 8, 7, tzinfo=UTC),    # Tue 08-04 → Fri 08-07
            datetime(2026, 8, 8, tzinfo=UTC),    # Wed 08-05 → Sat 08-08
            datetime(2026, 8, 9, tzinfo=UTC),    # Thu 08-06 → Sun 08-09
            datetime(2026, 8, 10, tzinfo=UTC),   # Fri 08-07 → Mon 08-10
            datetime(2026, 8, 11, tzinfo=UTC),   # Sat 08-08 → Tue 08-11
            datetime(2026, 8, 12, tzinfo=UTC),   # Sun 08-09 → Wed 08-12
            datetime(2026, 8, 13, tzinfo=UTC),   # Mon 08-10 → Thu 08-13
            datetime(2026, 8, 14, tzinfo=UTC),   # Tue 08-11 → Fri 08-14
            datetime(2026, 8, 15, tzinfo=UTC),   # Wed 08-12 → Sat 08-15
            datetime(2026, 8, 16, tzinfo=UTC),   # Thu 08-13 → Sun 08-16
            datetime(2026, 8, 17, tzinfo=UTC),   # Fri 08-14 → Mon 08-17
            datetime(2026, 8, 18, tzinfo=UTC),   # Sat 08-15 → Tue 08-18
            datetime(2026, 8, 19, tzinfo=UTC),   # Sun 08-16 → Wed 08-19
        ]
        self.assertEqual(
            [demo_day_zero(base + timedelta(days=n)) for n in range(14)],
            expected)
        # Premise: the table really does span every weekday, so "the weekday
        # makes no difference" is a claim about seven cases and not one.
        self.assertEqual({d.weekday() for d in
                          (base + timedelta(days=n) for n in range(14))},
                         set(range(7)))

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

    def test_every_instant_of_a_fortnight_yields_future_ice_at_a_fixed_lead(self):
        # 14 days x 24 hours of day-zero derivations (pure, so cheap): always
        # UTC midnight, always EXACTLY the configured lead out, always after
        # the instant itself. The lead used to vary over 7-13 days because of
        # the snap-to-Saturday step and could only be bounded by a range; with
        # a plain lead it is one number, so this pins equality (#389 review).
        base = datetime(2026, 8, 3, tzinfo=UTC)
        leads = set()
        for day in range(14):
            for hour in range(24):
                instant = base + timedelta(days=day, hours=hour, minutes=59,
                                           seconds=59)
                d0 = demo_day_zero(instant)
                self.assertEqual(d0.time(), datetime.min.time(), instant)
                leads.add((d0 - instant.replace(hour=0, minute=0,
                                                second=0)).days)
                # The earliest slot the demo lays down is day zero 16:00.
                self.assertGreater(d0.replace(hour=16), instant, instant)
        # ONE lead across all 336 instants — a snap-to-weekday step would
        # spread these over seven values. The exact number is pinned by the
        # spelled-out table in DemoLayoutIsExactTest; here it must simply not
        # depend on when the demo was seeded.
        self.assertEqual(len(leads), 1, leads)
        self.assertIn(leads.pop(), range(1, 7))


class DemoLandsInsideTheDashboardsCurrentWeekTest(unittest.TestCase):
    """#389 review: the demo must have games in the operator's CURRENT week.

    The landing dashboard's "Games this week" tile (``renderDashboard`` in
    ``web/static/app.js``) counts games in the inclusive 7-day window
    ``[today, today + 6]``. Pushing the whole inventory into the future is not
    enough on its own: pushed far enough out, the demo is future-dated AND
    invisible, and a freshly-seeded demo's first screen reads 0.

    So day zero carries a two-sided bound, and both sides matter:

    * at least 1 day out, or the demo is born partly past-dated at some hours
      of the day (the ``DemoSeedInstantSweepTest`` half);
    * at most 6 days out, or it never lands inside that window.

    A snap-to-weekday derivation cannot satisfy both, whatever its lead: a
    fixed weekday recurs every 7 days, so the lead it produces necessarily
    spans a 7-wide range ``[L, L + 6]``, which cannot fit inside ``[1, 6]``.
    That is why ``full_demo`` derives day zero from a plain fixed lead — see
    the note above ``_DEMO_LEAD_DAYS``.
    """

    def _window(self, seed_instant):
        """The dashboard's own window for ``seed_instant``, as calendar dates:
        the inclusive 7 days starting the day the demo was seeded."""
        first = seed_instant.astimezone(UTC).date()
        return [first + timedelta(days=n) for n in range(7)]

    def test_day_zero_is_both_future_and_inside_the_current_week(self):
        # 14 days x 24 hours, so neither the weekday nor the hour of seeding
        # can move day zero out of the window on some days and not others —
        # a 1-in-7 outcome is the #384 bug class, not a passing test.
        base = datetime(2026, 8, 3, tzinfo=UTC)
        for day in range(14):
            for hour in range(24):
                instant = base + timedelta(days=day, hours=hour, minutes=59,
                                           seconds=59)
                d0 = demo_day_zero(instant)
                lead = (d0.date() - instant.date()).days
                self.assertGreaterEqual(lead, 1, instant)
                self.assertLessEqual(lead, 6, instant)
                self.assertIn(d0.date(), self._window(instant), instant)

    def test_a_fresh_demo_has_games_in_the_dashboards_current_week(self):
        # The regression itself, stated as data: count the games the dashboard
        # tile would count, with the tile's own filter, on a brand-new demo.
        store, _gid, _ids = build_full_demo_store(seed_instant=FAR_FUTURE)
        games = store.all_games()
        # Premise: the demo really did seed its full schedule, so a non-zero
        # count below is a statement about WHERE the games are and not merely
        # about whether any exist.
        self.assertEqual(len(games), 20)
        window = self._window(FAR_FUTURE)
        this_week = [g for g in games if g.start_time.astimezone(UTC).date()
                     in window]
        # Exact, not ">= 1": with a 3-day lead the window holds day zero and
        # the first three days of the pilot pack — 1 + 3 + 3 + 3 games. Half
        # the demo's published schedule is in the operator's own week. This
        # number moves if the lead moves, which is the point: it makes the
        # lead a decision the suite states out loud rather than an accident.
        self.assertEqual(len(this_week), 10)
        self.assertEqual(
            sorted({g.start_time.astimezone(UTC).date() for g in this_week}),
            [demo_day_zero(FAR_FUTURE).date() + timedelta(days=n)
             for n in range(4)])

    def test_the_current_week_holds_games_whatever_weekday_it_is_seeded_on(self):
        # Every weekday, built for real (not just derived): a demo seeded on
        # any day of the week shows the same non-empty week.
        base = datetime(2026, 8, 3, 13, 5, tzinfo=UTC)   # a Monday
        for day in range(7):
            instant = base + timedelta(days=day)
            store, _gid, _ids = build_full_demo_store(seed_instant=instant)
            window = self._window(instant)
            this_week = [g for g in store.all_games()
                         if g.start_time.astimezone(UTC).date() in window]
            self.assertEqual(len(this_week), 10, instant)


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


class DemoSeedInstantEnvOverrideTest(unittest.TestCase):
    """The running web server can be told which instant to seed at.

    #389 review: the browser gate for all of this could only ever observe the
    machine's real clock, so it proved the app correct on today's date — which
    is exactly how the bug being fixed passed for years. Pinning the seed
    instant from outside the process is what lets a browser journey run against
    a demo seeded well past the retired September 2026 window.

    The hook is an environment variable, read at the moment the demo is
    (re)built. Unset -- production, and every ordinary dev run -- it is
    literally ``None`` handed to the same parameter that already defaulted to
    ``None``, so the seed path is byte-for-byte what it was.
    """

    ENV = "HOCKEY_DEMO_SEED_INSTANT"

    def setUp(self):
        from hockey_scheduler.web import server as srv

        self.srv = srv
        self.addCleanup(os.environ.pop, self.ENV, None)
        os.environ.pop(self.ENV, None)

    def test_unset_env_leaves_the_seed_on_its_own_clock(self):
        # The inert case, asserted rather than assumed: with nothing set, the
        # resolver contributes nothing and build_full_demo_store keeps the
        # default it always had.
        self.assertIsNone(self.srv.demo_seed_instant_from_env())

    def test_env_instant_is_used_verbatim(self):
        os.environ[self.ENV] = "2031-04-17T13:05:41.123456+00:00"
        self.assertEqual(self.srv.demo_seed_instant_from_env(),
                         datetime(2031, 4, 17, 13, 5, 41, 123456, tzinfo=UTC))

    def test_a_non_utc_offset_is_preserved_as_an_instant(self):
        os.environ[self.ENV] = "2031-04-17T18:05:41+05:00"
        self.assertEqual(self.srv.demo_seed_instant_from_env(),
                         datetime(2031, 4, 17, 13, 5, 41, tzinfo=UTC))

    def test_a_naive_instant_is_refused_not_guessed(self):
        # Same rule as demo_day_zero: guessing UTC would silently shift the
        # whole inventory by the caller's offset. Refused LOUDLY, because a
        # silently-ignored override would make the browser gate below pass
        # against a demo seeded at the real clock -- the exact vacuity this
        # hook exists to remove.
        os.environ[self.ENV] = "2031-04-17T13:05:41"
        with self.assertRaises(ValueError):
            self.srv.demo_seed_instant_from_env()

    def test_an_unparseable_instant_is_refused_not_ignored(self):
        os.environ[self.ENV] = "next tuesday"
        with self.assertRaises(ValueError):
            self.srv.demo_seed_instant_from_env()

    def test_the_seeded_store_really_lands_on_the_env_instant(self):
        # End to end through the server's own rebuild, not just the resolver:
        # a demo seeded five years past the retired window puts its ice there.
        os.environ[self.ENV] = "2031-04-17T13:05:41.123456+00:00"
        state = self.srv.DemoState()
        state.reset(seed=True)
        slots = state.api.store.all_ice_slots()
        self.assertEqual(len(slots), 57)      # premise: a full demo was built
        self.assertEqual(min(s.start_time for s in slots),
                         demo_day_zero(FAR_FUTURE).replace(hour=16))
        self.assertEqual(layout_only(state.api.store),
                         expected_layout(FAR_FUTURE))


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
        # This assertion used to read ``> now + 7 days``, which was itself the
        # time bomb this file exists to remove: with the old snap-to-Saturday
        # derivation a SATURDAY seed produced a lead of exactly 7, so day zero
        # 18:30 was EARLIER than now + 7d for the rest of that Saturday
        # evening. It passed only because no CI run had yet started after
        # 18:30 UTC on a Saturday (#389 review).
        #
        # The real property is "strictly future", and it is now stated exactly:
        # the call is bracketed by two clock readings, so the seeded game must
        # equal day zero 18:30 for one of them. That is a set of at most two
        # exact datetimes (two only if the call straddles UTC midnight), never
        # an inequality with slack in it.
        from hockey_scheduler.seed import build_seeded_store

        before = datetime.now(UTC)
        store, game_id = build_seeded_store()
        after = datetime.now(UTC)
        start = store.get_game(game_id).start_time
        self.assertGreater(start, after)
        self.assertIn(start, {demo_day_zero(i).replace(hour=18, minute=30)
                              for i in (before, after)})

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
