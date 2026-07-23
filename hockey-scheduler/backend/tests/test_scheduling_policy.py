"""Scheduling policy: turnover/warm-up/resurfacing buffers + curfew (#277 Slice B).

A SchedulingPolicy row per Program/Season/Rink scope, resolved field by field
(Rink > Season > Program, ``None`` = inherit) and enforced inside
``SetupService._assert_slot_free`` — THE shared placement gate — so
create_game, move_game, and both draft-commit implementations reject
``insufficient_playable_time`` / ``turnover_buffer_conflict`` /
``curfew_violation`` identically, with no draft-only exception. All-``None``
policies (every pre-Slice-B install) short-circuit to the exact previous
behavior, and enforcement is read-time only: no stored IceSlot/Game is ever
rewritten by setting a policy (#277: zero silent time shifts).

Curfew semantics pinned here (deterministic, policy-controlled): the curfew
instant is HH:MM in the slot's VENUE timezone (Program fallback), on the
slot's local start date for an afternoon/evening curfew (>= 12:00) or the
following morning for a small-hours one (< 12:00); ending exactly AT curfew —
and a same-rink gap exactly EQUAL to warmup+resurfacing — are compliant
(half-open boundaries, matching ``intervals_overlap``).
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

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Organization, Program, Season, League, LeagueSeason, Division, Venue,
    SeasonVenueAccess, Rink, Team, SeasonTeamRegistration, IceSlot,
    IceSlotType, IceSlotStatus)
from hockey_scheduler.domain.errors import ConcurrencyConflictError
from hockey_scheduler.services.setup_service import (
    SetupService as BaseSetupService,
)
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv

UTC = timezone.utc
BASE = datetime(2026, 1, 5, 18, tzinfo=UTC)  # Mon 12:00 America/Chicago


def _seed(s):
    """One Program (America/Chicago) -> Season se1 -> League lg -> Division d1
    with four registered teams; one venue (same tz) with rinks r1/r2 and GAME
    slots on r1: sA 18:00Z+60m, sB 70m after sA's start (10-minute gap), sC a
    40-minute sliver, sD starting 22:30 LOCAL, sE on r2 at sB's exact time."""
    s.add_organization(Organization(id="org", name="O"))
    s.add_program(Program(id="pg", name="P", operator_organization_id="org",
                          timezone="America/Chicago"))
    s.add_league(League(id="lg", program_id="pg", name="L"))
    s.add_venue(Venue(id="v", name="Arena", organization_id="org",
                      league_id="pg", timezone="America/Chicago"))
    s.add_rink(Rink(id="r1", venue_id="v", name="Main"))
    s.add_rink(Rink(id="r2", venue_id="v", name="Aux"))
    s.add_season(Season(id="se1", program_id="pg", name="SE1"))
    s.add_season_venue_access(SeasonVenueAccess(
        id="sva", season_id="se1", venue_id="v", active=True))
    s.add_league_season(LeagueSeason(id="ls1", league_id="lg", season_id="se1"))
    s.add_division(Division(id="d1", league_season_id="ls1", name="D1"))
    for i in range(4):
        s.add_team(Team(id=f"t{i}", name=f"T{i}", division_id="d1",
                        program_id="pg", league_id="lg"))
        s.add_season_team_registration(SeasonTeamRegistration(
            id=f"reg{i}", league_season_id="ls1", team_id=f"t{i}",
            division_id="d1", active=True))

    def gslot(sid, rink, start, minutes=60):
        s.add_ice_slot(IceSlot(
            id=sid, rink_id=rink, start_time=start,
            end_time=start + timedelta(minutes=minutes),
            slot_type=IceSlotType.GAME, status=IceSlotStatus.AVAILABLE))

    gslot("sA", "r1", BASE)
    gslot("sB", "r1", BASE + timedelta(minutes=70))
    gslot("sC", "r1", BASE + timedelta(minutes=300), 40)
    gslot("sD", "r1", datetime(2026, 1, 6, 4, 30, tzinfo=UTC))  # 22:30 local
    gslot("sE", "r2", BASE + timedelta(minutes=70))
    return s


def _reason(r):
    return (r.get("error", {}).get("details", {}).get("reason")
            if isinstance(r, dict) else None)


class _PolicyContract:
    """Cross-backend contract; subclasses provide the store."""

    def _make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self._make_store()
        _seed(self.store)
        self.api = ApiService(self.store)

    def tearDown(self):
        conn = getattr(self.store, "conn", None)
        if conn is not None:
            conn.close()

    # -- CRUD + resolution -------------------------------------------------
    def test_set_get_and_field_level_inheritance(self):
        r = self.api.set_scheduling_policy(
            scope_type="program", scope_id="pg", min_playable_minutes=30,
            actor_id="admin")
        self.assertNotIn("error", r, r)
        r = self.api.set_scheduling_policy(
            scope_type="season", scope_id="se1", warmup_minutes=5,
            resurfacing_minutes=10, actor_id="admin")
        self.assertNotIn("error", r, r)
        r = self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", curfew_local="22:00",
            actor_id="admin")
        self.assertNotIn("error", r, r)
        g = self.api.get_scheduling_policy(
            scope_type="rink", scope_id="r1", season_id="se1")
        self.assertEqual(g["policy"]["curfew_local"], "22:00")
        self.assertEqual(g["effective"], {
            "warmup_minutes": 5, "resurfacing_minutes": 10,
            "min_playable_minutes": 30, "curfew_local": "22:00"})
        self.assertEqual(g["effective_sources"], {
            "warmup_minutes": "season", "resurfacing_minutes": "season",
            "min_playable_minutes": "program", "curfew_local": "rink"})

    def test_set_replaces_wholesale_and_all_none_clears(self):
        self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", warmup_minutes=5,
            curfew_local="22:00", actor_id="admin")
        r = self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", curfew_local="23:00",
            actor_id="admin")
        # A set is a settings form, not a patch: warmup went back to inherit.
        self.assertIsNone(r["policy"]["warmup_minutes"], r)
        self.assertEqual(r["policy"]["curfew_local"], "23:00")
        r = self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", actor_id="admin")
        self.assertIsNone(r["policy"], r)
        self.assertEqual(self.store.all_scheduling_policies(), [])
        actions = [a.action for a in self.store.all_setup_audit()]
        self.assertIn("scheduling_policy_set", actions)
        self.assertIn("scheduling_policy_cleared", actions)

    def test_validation_reasons(self):
        for field in ("warmup_minutes", "resurfacing_minutes",
                      "min_playable_minutes"):
            for bad in (-1, "5", True, 1.5):
                r = self.api.set_scheduling_policy(
                    scope_type="rink", scope_id="r1", **{field: bad})
                self.assertEqual(_reason(r), f"invalid_{field}",
                                 (field, bad, r))
        self.assertEqual(_reason(self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", curfew_local="25:00")),
            "invalid_curfew_local")
        self.assertEqual(_reason(self.api.set_scheduling_policy(
            scope_type="rink", scope_id="nope", curfew_local="22:00")),
            "policy_scope_missing")
        self.assertEqual(_reason(self.api.set_scheduling_policy(
            scope_type="club", scope_id="x", warmup_minutes=1)),
            "unknown_policy_scope")
        # A failed set never wrote anything.
        self.assertEqual(self.store.all_scheduling_policies(), [])

    def test_no_policy_means_no_behavior_change(self):
        # The pre-Slice-B world: back-to-back slivers place freely.
        g1 = self.api.create_game("se1", "d1", "t0", "t1", "sA",
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        g2 = self.api.create_game("se1", "d1", "t2", "t3", "sB",
                                  league_id="lg")
        self.assertNotIn("error", g2, g2)
        g3 = self.api.create_game("se1", "d1", "t0", "t2", "sC",
                                  league_id="lg")
        self.assertNotIn("error", g3, g3)

    # -- gate enforcement: create_game ------------------------------------
    def _buffer_policy(self):
        self.api.set_scheduling_policy(
            scope_type="season", scope_id="se1", warmup_minutes=5,
            resurfacing_minutes=10, min_playable_minutes=45,
            actor_id="admin")

    def test_create_rejects_turnover_buffer_conflict(self):
        self._buffer_policy()
        g1 = self.api.create_game("se1", "d1", "t0", "t1", "sA",
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        g2 = self.api.create_game("se1", "d1", "t2", "t3", "sB",
                                  league_id="lg")
        self.assertEqual(_reason(g2), "turnover_buffer_conflict", g2)
        d = g2["error"]["details"]
        self.assertEqual((d["required_gap_minutes"], d["gap_minutes"]),
                         (15, 10))
        self.assertEqual(d["conflict_slot_id"], "sA")

    def test_other_rink_is_not_a_buffer_conflict(self):
        self._buffer_policy()
        g1 = self.api.create_game("se1", "d1", "t0", "t1", "sA",
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        # sE overlaps sB's time entirely but sits on r2.
        g2 = self.api.create_game("se1", "d1", "t2", "t3", "sE",
                                  league_id="lg")
        self.assertNotIn("error", g2, g2)

    def test_gap_exactly_equal_to_buffer_is_compliant(self):
        self._buffer_policy()
        self.store.add_ice_slot(IceSlot(
            id="sX", rink_id="r1", start_time=BASE + timedelta(minutes=75),
            end_time=BASE + timedelta(minutes=135),
            slot_type=IceSlotType.GAME, status=IceSlotStatus.AVAILABLE))
        g1 = self.api.create_game("se1", "d1", "t0", "t1", "sA",
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        g2 = self.api.create_game("se1", "d1", "t2", "t3", "sX",
                                  league_id="lg")
        self.assertNotIn("error", g2, g2)

    def test_create_rejects_insufficient_playable_time(self):
        self._buffer_policy()
        g = self.api.create_game("se1", "d1", "t0", "t1", "sC",
                                 league_id="lg")
        self.assertEqual(_reason(g), "insufficient_playable_time", g)
        d = g["error"]["details"]
        self.assertEqual((d["slot_minutes"], d["required_minutes"]), (40, 45))

    def test_curfew_violation_and_exact_end_at_curfew(self):
        # sD runs 22:30-23:30 local: even STARTING past a 22:00 curfew ends
        # past it -> violation with the local end in the details.
        self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", curfew_local="22:00",
            actor_id="admin")
        g = self.api.create_game("se1", "d1", "t0", "t1", "sD",
                                 league_id="lg")
        self.assertEqual(_reason(g), "curfew_violation", g)
        self.assertEqual(g["error"]["details"]["slot_end_local"], "23:30")
        # Ending exactly AT curfew is compliant (half-open).
        self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", curfew_local="23:30",
            actor_id="admin")
        g = self.api.create_game("se1", "d1", "t0", "t1", "sD",
                                 league_id="lg")
        self.assertNotIn("error", g, g)

    def test_small_hours_curfew_means_the_following_morning(self):
        # A 01:00 building close: sD (22:30-23:30 local) is fine against
        # NEXT-day 01:00 — never judged against "this morning's" 01:00.
        self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", curfew_local="01:00",
            actor_id="admin")
        g = self.api.create_game("se1", "d1", "t0", "t1", "sD",
                                 league_id="lg")
        self.assertNotIn("error", g, g)

    def test_slot_starting_in_the_small_hours_judged_against_that_morning(
            self):
        # A 00:30-02:00 LOCAL slot violates tonight's 01:00 close — it is
        # never waved through to tomorrow morning's curfew just because its
        # start already crossed midnight.
        self.store.add_ice_slot(IceSlot(
            id="sSH", rink_id="r1",
            start_time=datetime(2026, 1, 6, 6, 30, tzinfo=UTC),
            end_time=datetime(2026, 1, 6, 8, 0, tzinfo=UTC),
            slot_type=IceSlotType.GAME, status=IceSlotStatus.AVAILABLE))
        self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", curfew_local="01:00",
            actor_id="admin")
        g = self.api.create_game("se1", "d1", "t0", "t1", "sSH",
                                 league_id="lg")
        self.assertEqual(_reason(g), "curfew_violation", g)
        self.assertEqual(g["error"]["details"]["slot_end_local"], "02:00")

    def test_overlapping_legacy_slots_report_zero_gap_not_negative(self):
        # create_ice_slot forbids same-rink overlap, but legacy/imported rows
        # can hold it; the buffer detail clamps to a 0-minute gap.
        self._buffer_policy()
        self.store.add_ice_slot(IceSlot(
            id="sOV", rink_id="r1",
            start_time=BASE + timedelta(minutes=30),
            end_time=BASE + timedelta(minutes=90),
            slot_type=IceSlotType.GAME, status=IceSlotStatus.AVAILABLE))
        g1 = self.api.create_game("se1", "d1", "t0", "t1", "sA",
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        g2 = self.api.create_game("se1", "d1", "t2", "t3", "sOV",
                                  league_id="lg")
        self.assertEqual(_reason(g2), "turnover_buffer_conflict", g2)
        self.assertEqual(g2["error"]["details"]["gap_minutes"], 0, g2)

    def test_clearing_the_policy_restores_placement(self):
        self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", curfew_local="22:00",
            actor_id="admin")
        self.assertEqual(_reason(self.api.create_game(
            "se1", "d1", "t0", "t1", "sD", league_id="lg")),
            "curfew_violation")
        self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", actor_id="admin")
        g = self.api.create_game("se1", "d1", "t0", "t1", "sD",
                                 league_id="lg")
        self.assertNotIn("error", g, g)

    # -- gate enforcement: move_game + draft-commit ------------------------
    def test_move_rejected_identically_and_never_conflicts_with_itself(self):
        g1 = self.api.create_game("se1", "d1", "t0", "t1", "sA",
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        self._buffer_policy()
        # sB sits 10 < 15 minutes from the game's OWN current slot — the move
        # must exclude the game itself (its old ice frees on move), so this
        # succeeds even under the buffer.
        mv = self.api.move_game(g1["id"], "sB")
        self.assertNotIn("error", mv, mv)
        # But moving a DIFFERENT game against it enforces the buffer: t2/t3
        # on sC would be fine by itself (45m short check first: sC is 40m).
        g2 = self.api.create_game("se1", "d1", "t2", "t3", "sE",
                                  league_id="lg")
        self.assertNotIn("error", g2, g2)
        mv2 = self.api.move_game(g2["id"], "sA")
        self.assertEqual(_reason(mv2), "turnover_buffer_conflict", mv2)

    def test_draft_commit_rejected_identically_with_full_rollback(self):
        g1 = self.api.create_game("se1", "d1", "t0", "t1", "sA",
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        # Set the policy only AFTER the proposal-blind placement exists:
        # the scheduler's own advisory (tested below) would otherwise stop
        # sB from ever being proposed — this test pins the COMMIT GATE, so
        # defeat the advisory by making it see no policy at generation...
        # impossible sequentially; instead pin the gate directly through the
        # shared checker with a hand-built row.
        self._buffer_policy()
        audits = len(self.store.all_setup_audit())
        games = len(self.store.all_games())
        # The advisory keeps sB out of a proposal now, so the commit result
        # is "nothing schedulable" — never a Game the gate would refuse.
        res = self.api.commit_draft_schedule("d1", slot_ids=["sB"])
        if "error" in res:
            self.assertEqual(_reason(res), "turnover_buffer_conflict", res)
        else:
            self.assertEqual(res["created"], [], res)
            self.assertTrue(res["unscheduled"], res)
        self.assertEqual(len(self.store.all_games()), games)
        self.assertEqual(len(self.store.all_setup_audit()) - audits, 1
                         if "error" not in res else 0)
        self.assertEqual(self.store.get_ice_slot("sB").status,
                         IceSlotStatus.AVAILABLE)
        # The gate itself still refuses the identical stale row — pinned
        # directly through the shared checker create/move/commit all use.
        from hockey_scheduler.domain.errors import ScheduleConflictError
        with self.assertRaises(ScheduleConflictError) as ctx:
            self.api.setup._assert_slot_free_for_game(
                "sB", "t2", "t3", season_id="se1")
        self.assertEqual(ctx.exception.details["reason"],
                         "turnover_buffer_conflict")

    # -- scheduler advisory parity (#277 step 3) ---------------------------
    def test_proposal_reports_buffer_conflict_instead_of_offering_the_slot(
            self):
        self._buffer_policy()
        g1 = self.api.create_game("se1", "d1", "t0", "t1", "sA",
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        prop = self.api.draft_season_schedule("d1", slot_ids=["sB"])
        self.assertNotIn("error", prop, prop)
        self.assertEqual(prop["draft_games"], [], prop)
        codes = {c for row in prop["unscheduled"]
                 for c in row["reason_codes"]}
        self.assertIn("turnover_buffer_conflict", codes, prop)

    def test_proposal_reports_sliver_and_curfew_codes(self):
        self._buffer_policy()
        self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", curfew_local="22:00",
            actor_id="admin")
        prop = self.api.draft_season_schedule("d1", slot_ids=["sC", "sD"])
        self.assertNotIn("error", prop, prop)
        self.assertEqual(prop["draft_games"], [], prop)
        codes = {c for row in prop["unscheduled"]
                 for c in row["reason_codes"]}
        self.assertIn("insufficient_playable_time", codes, prop)
        self.assertIn("curfew_violation", codes, prop)

    def test_proposal_respects_buffer_between_its_own_tentative_picks(self):
        # No committed games at all: the greedy loop itself must not pick
        # sA and then sB (10 < 15 min apart on one rink) in the same run.
        self._buffer_policy()
        prop = self.api.draft_season_schedule("d1", slot_ids=["sA", "sB"])
        self.assertNotIn("error", prop, prop)
        placed = {row["ice_slot_id"] for row in prop["draft_games"]}
        self.assertNotEqual(placed, {"sA", "sB"}, prop)
        self.assertEqual(len(prop["draft_games"]), 1, prop)
        # And the losing pairings say why.
        codes = {c for row in prop["unscheduled"]
                 for c in row["reason_codes"]}
        self.assertIn("turnover_buffer_conflict", codes, prop)

    def test_proposal_without_policies_is_unchanged(self):
        prop = self.api.draft_season_schedule("d1", slot_ids=["sA", "sB"])
        self.assertNotIn("error", prop, prop)
        self.assertEqual({row["ice_slot_id"] for row in prop["draft_games"]},
                         {"sA", "sB"}, prop)


class MemorySchedulingPolicyTest(_PolicyContract, unittest.TestCase):
    def _make_store(self):
        return InMemoryStore()


class SqliteSchedulingPolicyTest(_PolicyContract, unittest.TestCase):
    def _make_store(self):
        return SqlStore(":memory:")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresSchedulingPolicyTest(_PolicyContract, unittest.TestCase):
    def _make_store(self):
        store = SqlStore(os.environ["TEST_DATABASE_URL"])
        store.clear_all_data()
        return store


class _ScopeDeletionCascadeContract:
    """Deleting a scope entity deletes (and audits) its policy row — without
    an FK (the scope column is polymorphic), an orphan would be permanently
    unreachable through the API, which validates scope existence on every
    read/write (#277 Slice B review)."""

    def _make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self._make_store()
        _seed(self.store)
        self.api = ApiService(self.store)

    def tearDown(self):
        conn = getattr(self.store, "conn", None)
        if conn is not None:
            conn.close()

    def _assert_cascaded(self, scope_type, scope_id, delete_call):
        r = self.api.set_scheduling_policy(
            scope_type=scope_type, scope_id=scope_id, warmup_minutes=5,
            actor_id="admin")
        self.assertNotIn("error", r, r)
        res = delete_call()
        self.assertNotIn("error", res, res)
        self.assertEqual(self.store.all_scheduling_policies(), [])
        self.assertTrue(any(
            a.action == "scheduling_policy_cleared"
            and a.detail.get("cascade") == f"{scope_type}_deleted"
            for a in self.store.all_setup_audit()))

    def test_rink_delete_cascades_its_policy(self):
        self.store.add_rink(Rink(id="r3", venue_id="v", name="Spare"))
        self._assert_cascaded(
            "rink", "r3", lambda: self.api.delete_rink("r3", actor_id="admin"))

    def test_season_delete_cascades_its_policy(self):
        # An empty season: no levels/divisions/registrations/games/access.
        self.store.add_season(Season(id="se9", program_id="pg", name="SE9"))
        self._assert_cascaded(
            "season", "se9",
            lambda: self.api.delete_season("se9", actor_id="admin"))

    def test_program_delete_cascades_its_policy(self):
        self.store.add_program(Program(id="pg9", name="Empty",
                                       operator_organization_id="org"))
        self._assert_cascaded(
            "program", "pg9",
            lambda: self.api.delete_program("pg9", actor_id="admin"))


class CreateGameRinkLockRaceTest(unittest.TestCase):
    """A slot that materializes between create_game's pre-lock locator read
    (which decides WHETHER to take the rink lock) and the gate's own re-read
    would otherwise run the whole placement — including the turnover-buffer
    scan, which has no DB backstop — with no rink lock held. The defensive
    post-gate re-verify refuses it with a stable retryable conflict instead
    (#277 Slice B review; mirrors move_game's _MoveGameRaced re-check)."""

    def test_slot_materializing_after_locator_read_is_refused(self):
        store = InMemoryStore()
        _seed(store)
        setup = BaseSetupService(store)
        real = store.get_ice_slot
        calls = {"n": 0}

        def locator_miss(slot_id):
            if slot_id == "sA":
                calls["n"] += 1
                if calls["n"] == 1:
                    return None  # the locator ran before the slot existed
            return real(slot_id)

        store.get_ice_slot = locator_miss
        with self.assertRaises(ConcurrencyConflictError) as ctx:
            setup.create_game("se1", "d1", "t0", "t1", "sA", league_id="lg")
        self.assertEqual(ctx.exception.details["reason"], "placement_raced")
        # Zero writes: no game, slot untouched.
        self.assertEqual([g for g in store.all_games() if not g.cancelled], [])
        self.assertEqual(store.get_ice_slot("sA").status,
                         IceSlotStatus.AVAILABLE)


class MemoryScopeDeletionCascadeTest(_ScopeDeletionCascadeContract,
                                     unittest.TestCase):
    def _make_store(self):
        return InMemoryStore()


class SqliteScopeDeletionCascadeTest(_ScopeDeletionCascadeContract,
                                     unittest.TestCase):
    def _make_store(self):
        return SqlStore(":memory:")


class SchedulingPolicyHttpTest(unittest.TestCase):
    """HTTP surface (#277 Slice B review): both routes are reachable through
    the route tables, gated MANAGE_ARENA (admin/arena yes, coach no), and the
    POST enforces the STRICT write schema — vital here because a set replaces
    the row wholesale, so a typo'd knob key must 400, never silently clear
    that knob."""

    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, dict(r.headers), json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), json.loads(e.read() or b"{}")

    def _login(self, username):
        c = self._client()
        status, _h, _b = self._req(c, "POST", "/api/auth/login",
                                   {"username": username, "password": "demo"})
        self.assertEqual(status, 200)
        return c

    PATH = "/api/setup/scheduling-policy"

    def test_post_and_get_round_trip_as_admin(self):
        admin = self._login("admin")
        status, _h, body = self._req(admin, "POST", self.PATH, {
            "scope_type": "rink", "scope_id": "rink_1",
            "warmup_minutes": 5, "curfew_local": "22:00"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["policy"]["curfew_local"], "22:00")
        status, _h, body = self._req(
            admin, "GET",
            self.PATH + "?scope_type=rink&scope_id=rink_1"
                        "&season_id=season_1")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["policy"]["warmup_minutes"], 5)
        self.assertEqual(body["effective"]["curfew_local"], "22:00")
        self.assertEqual(body["effective_sources"]["curfew_local"], "rink")
        # Clean up for the other tests (wholesale-None clears).
        status, _h, body = self._req(admin, "POST", self.PATH, {
            "scope_type": "rink", "scope_id": "rink_1"})
        self.assertEqual(status, 200, body)
        self.assertIsNone(body["policy"], body)

    def test_unknown_key_is_rejected_not_silently_dropped(self):
        admin = self._login("admin")
        status, _h, body = self._req(admin, "POST", self.PATH, {
            "scope_type": "rink", "scope_id": "rink_1",
            "warmup_mins": 5})  # typo'd knob key
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")

    def test_wrong_type_and_missing_scope_are_400(self):
        admin = self._login("admin")
        status, _h, body = self._req(admin, "POST", self.PATH, {
            "scope_type": "rink", "scope_id": "rink_1",
            "warmup_minutes": "5"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "wrong_type")
        status, _h, body = self._req(admin, "POST", self.PATH, {
            "scope_id": "rink_1"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "field_required")

    def test_non_operator_is_403_and_unauthenticated_401(self):
        coach = self._login("coach")
        status, _h, _b = self._req(coach, "POST", self.PATH, {
            "scope_type": "rink", "scope_id": "rink_1",
            "warmup_minutes": 5})
        self.assertEqual(status, 403)
        status, _h, _b = self._req(
            coach, "GET", self.PATH + "?scope_type=rink&scope_id=rink_1")
        self.assertEqual(status, 403)
        anon = self._client()
        status, _h, _b = self._req(anon, "POST", self.PATH, {
            "scope_type": "rink", "scope_id": "rink_1"})
        self.assertEqual(status, 401)

    def test_unsupported_method_is_405_with_both_verbs_allowed(self):
        # Pins BOTH route-table entries: DELETE on the path must 405 with
        # GET and POST in Allow, proving the path is known to both tables.
        admin = self._login("admin")
        status, headers, body = self._req(admin, "DELETE", self.PATH)
        self.assertEqual(status, 405, body)
        allow = {m.strip() for m in headers.get("Allow", "").split(",")}
        self.assertIn("GET", allow)
        self.assertIn("POST", allow)


if __name__ == "__main__":
    unittest.main()
