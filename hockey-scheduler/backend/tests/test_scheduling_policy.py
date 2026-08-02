"""Scheduling policy: turnover/warm-up/resurfacing buffers + curfew (#277 Slice B).

A SchedulingPolicy row per Program/Season/Rink scope, resolved field by field
(Rink > Season > Program, ``None`` = inherit) and enforced inside
``SetupService._assert_slot_free`` — THE shared placement gate — so
create_game, move_game, and both draft-commit implementations reject
``insufficient_playable_time`` / ``slot_overlap_conflict`` /
``turnover_buffer_conflict`` / ``curfew_violation`` identically, with no
draft-only exception. All-``None`` policies (every pre-Slice-B install)
short-circuit to the exact previous behavior — with one deliberate
exception: physically overlapping same-rink slots can never both host
games (``slot_overlap_conflict``), a rule that binds regardless of any
policy and even for season-less legacy games. Enforcement is read-time
only: no stored IceSlot/Game is ever rewritten by setting a policy (#277:
zero silent time shifts).

Curfew semantics pinned here (deterministic, policy-controlled — see
``ice_availability.curfew_instant``): the deadline is HH:MM in the slot's
VENUE timezone (Program fallback); the operating day is defined relative to
the curfew itself — an evening curfew binds the start date, a small-hours
curfew binds a start at/before its wall clock to THAT date and any later
start to the FOLLOWING date. Buffers are DIRECTIONAL per game (earlier
resurfacing + later warm-up, each from that game's own effective policy).
Ending exactly AT curfew — and a gap exactly EQUAL to the directional
requirement — are compliant (half-open, matching ``intervals_overlap``).
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

from helpers import BACKEND, commit_fresh_draft  # noqa: F401  (BACKEND: sys.path)

from hockey_scheduler.api import ApiService
from hockey_scheduler.api.service import ApiService as BaseFacadeApiService
from hockey_scheduler.services.scheduler import _policy_advisor
from hockey_scheduler.domain import (
    Organization, Program, Season, League, LeagueSeason, Division, Venue,
    SeasonVenueAccess, Rink, Team, SeasonTeamRegistration, IceSlot,
    IceSlotType, IceSlotStatus)
from hockey_scheduler.domain.errors import (ConcurrencyConflictError,
                                            ScheduleConflictError)
from hockey_scheduler.services.setup_service import (
    SetupService as BaseSetupService,
)
from hockey_scheduler.services.league_scoped_setup_service import (
    SetupService as LeagueSetupService,
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
    # A SECOND season sharing rink r1 via the same venue access (#318
    # directional-buffer review): policies inherit per Season, so a game in
    # se2 adjacent to a game in se1 exercises two DIFFERENT effective
    # policies on one rink.
    s.add_season(Season(id="se2", program_id="pg", name="SE2"))
    s.add_season_venue_access(SeasonVenueAccess(
        id="sva2", season_id="se2", venue_id="v", active=True))
    s.add_league_season(LeagueSeason(id="ls2", league_id="lg", season_id="se2"))
    s.add_division(Division(id="d2", league_season_id="ls2", name="D2"))
    for i in range(2):
        s.add_team(Team(id=f"u{i}", name=f"U{i}", division_id="d2",
                        program_id="pg", league_id="lg"))
        s.add_season_team_registration(SeasonTeamRegistration(
            id=f"reg2_u{i}", league_season_id="ls2", team_id=f"u{i}",
            division_id="d2", active=True))
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

    def test_overlapping_legacy_slots_refused_as_overlap_not_buffer(self):
        # create_ice_slot forbids same-rink overlap, but legacy/imported rows
        # can hold it; a game onto such a slot is refused with the DEDICATED
        # unconditional reason (#318 review) — never bent into a zero-gap
        # buffer conflict — naming the hosting game and its slot.
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
        self.assertEqual(_reason(g2), "slot_overlap_conflict", g2)
        d = g2["error"]["details"]
        self.assertEqual((d["conflict_game_id"], d["conflict_slot_id"]),
                         (g1["id"], "sA"), g2)

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
        res = commit_fresh_draft(self.api, "d1", slot_ids=["sB"])
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

    # -- reserved-vs-playable visibility (#277 step 4) ---------------------
    def test_overview_reserved_span_around_a_hosted_game(self):
        self._buffer_policy()
        g1 = self.api.create_game("se1", "d1", "t0", "t1", "sA",
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        ov = self.api.get_demo_overview()
        rows = {s["id"]: s for s in ov["ice_slots"]}
        hosted = rows["sA"]["reserved"]
        self.assertIsNotNone(hosted, rows["sA"])
        self.assertEqual(hosted["warmup_minutes"], 5)
        self.assertEqual(hosted["resurfacing_minutes"], 10)
        self.assertEqual(
            hosted["reserved_start_time"],
            (BASE - timedelta(minutes=5)).isoformat())
        self.assertEqual(
            hosted["reserved_end_time"],
            (BASE + timedelta(minutes=70)).isoformat())
        # A FREE slot reserves nothing yet — and with no policy at all,
        # even a hosted slot's field stays null.
        self.assertIsNone(rows["sB"]["reserved"], rows["sB"])

    def test_overview_reserved_is_null_without_policy(self):
        g1 = self.api.create_game("se1", "d1", "t0", "t1", "sA",
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        ov = self.api.get_demo_overview()
        rows = {s["id"]: s for s in ov["ice_slots"]}
        self.assertIsNone(rows["sA"]["reserved"], rows["sA"])
        # A policy with ONLY a minimum-playable knob reserves no extra
        # facility time either — the zero-buffer branch stays null.
        self.api.set_scheduling_policy(
            scope_type="season", scope_id="se1", min_playable_minutes=45,
            actor_id="admin")
        ov = self.api.get_demo_overview()
        rows = {s["id"]: s for s in ov["ice_slots"]}
        self.assertIsNone(rows["sA"]["reserved"], rows["sA"])

    # -- builder preview policy note + fingerprint binding (#277 step 5) ---
    def _preview(self):
        return self.api.preview_ice_availability(
            season_id="se1", rink_ids=["r1"], weekdays=[0],
            start_local="12:00", end_local="14:00",
            start_date="2026-02-02", end_date="2026-02-02",
            playable_minutes=60, turnover_minutes=0)

    def test_builder_commit_succeeds_with_matching_token_under_policy(self):
        # The commit recomputes the reviewed payload under its own locks;
        # policy_notes must be computed IDENTICALLY on both sides or every
        # commit under a policy would false-mismatch its own preview.
        self._buffer_policy()
        kw = dict(season_id="se1", rink_ids=["r1"], weekdays=[0],
                  start_local="12:00", end_local="14:00",
                  start_date="2026-02-02", end_date="2026-02-02",
                  playable_minutes=60, turnover_minutes=0, actor_id="admin")
        pv = self.api.preview_ice_availability(**kw)
        self.assertNotIn("error", pv, pv)
        self.assertTrue(pv["policy_notes"], pv)
        res = self.api.commit_ice_availability(
            template_fingerprint=pv["template_fingerprint"], **kw)
        self.assertNotIn("error", res, res)
        self.assertEqual(res["totals"]["created"], 2, res)

    def test_builder_preview_notes_sub_buffer_turnover_and_binds_token(self):
        before = self._preview()
        self.assertNotIn("error", before, before)
        self.assertEqual(before["policy_notes"], [], before)
        self._buffer_policy()
        after = self._preview()
        self.assertNotIn("error", after, after)
        # The note names the ACTUAL offending consecutive pair and its real
        # gap (#319 review) — here the template's two back-to-back slots.
        self.assertEqual(len(after["policy_notes"]), 1, after)
        note = after["policy_notes"][0]
        self.assertEqual(
            (note["rink_id"], note["rink_name"], note["date"],
             note["gap_minutes"], note["required_gap_minutes"],
             note["template_turnover_minutes"],
             note["policy_buffer_minutes"]),
            ("r1", "Main", "2026-02-02", 0, 15, 0, 15), after)
        self.assertEqual(note["pair_end_local"],
                         note["pair_next_start_local"], after)
        # The note is part of the reviewed payload, so the token moved: a
        # commit against the PRE-policy token is refused as a mismatch.
        self.assertNotEqual(before["template_fingerprint"],
                            after["template_fingerprint"])
        stale = self.api.commit_ice_availability(
            season_id="se1", rink_ids=["r1"], weekdays=[0],
            start_local="12:00", end_local="14:00",
            start_date="2026-02-02", end_date="2026-02-02",
            playable_minutes=60, turnover_minutes=0,
            template_fingerprint=before["template_fingerprint"])
        self.assertEqual(_reason(stale), "preview_mismatch", stale)

    # -- contracted-ice import advisories (#277 step 5) --------------------
    def test_import_dry_run_warns_on_sliver_gap_and_curfew(self):
        # Persist rink r1 with an external_ref the sheet can address, plus
        # rink-scope policy knobs; contracted spans must import untouched,
        # so all three findings are WARNINGS on an ok=True report.
        rink = self.store.get_rink("r1")
        rink.external_ref = "R1"
        self.store.save_rink(rink)
        self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", warmup_minutes=5,
            resurfacing_minutes=10, min_playable_minutes=45,
            curfew_local="22:00", actor_id="admin")
        rows = [
            # 40-minute sliver (min is 45).
            "R1,2026-03-02T18:00:00+00:00,2026-03-02T18:40:00+00:00,game",
            # 10-minute gap after the first row (buffer is 15).
            "R1,2026-03-02T18:50:00+00:00,2026-03-02T19:50:00+00:00,game",
            # Ends 23:30 local (curfew 22:00): 2026-03-03T05:30Z is 23:30
            # America/Chicago (CST) on Mar 2.
            "R1,2026-03-03T04:00:00+00:00,2026-03-03T05:30:00+00:00,game",
        ]
        report = self.api.get_import_dry_run({
            "rinks_csv": "venue_name,rink_code,rink_name,address\n"
                         "Arena,R1,Main,",
            "ice_slots_csv":
                "rink_code,start_time,end_time,slot_type\n" + "\n".join(rows)})
        self.assertTrue(report["ok"], report)
        text = " ".join(w["message"] for w in report["warnings"])
        self.assertIn("only 40 playable minutes", text, report)
        # Two sheet rows have no seasons yet, so the directional proximity
        # advisory is LABELED rink-level (#319 review), naming the real gap
        # and requirement.
        self.assertIn("Rink-level advisory", text, report)
        self.assertIn("only 10 min from", text, report)
        self.assertIn("15 min of resurfacing + warm-up", text, report)
        self.assertIn("22:00 curfew", text, report)

    def test_import_dry_run_warns_against_existing_committed_slots(self):
        rink = self.store.get_rink("r1")
        rink.external_ref = "R1"
        self.store.save_rink(rink)
        self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", warmup_minutes=5,
            resurfacing_minutes=10, actor_id="admin")
        # sA already exists in the store at BASE+60m end; a sheet row 10
        # minutes after it is inside the 15-minute buffer.
        csv_text = ("rink_code,start_time,end_time,slot_type\n"
                    f"R1,{(BASE + timedelta(minutes=70)).isoformat()},"
                    f"{(BASE + timedelta(minutes=130)).isoformat()},game")
        report = self.api.get_import_dry_run({
            "rinks_csv": "venue_name,rink_code,rink_name,address\n"
                         "Arena,R1,Main,",
            "ice_slots_csv": csv_text})
        self.assertTrue(report["ok"], report)
        text = " ".join(w["message"] for w in report["warnings"])
        self.assertIn("existing slot sA", text, report)

    def test_import_advisory_boundaries_match_the_gate(self):
        # Advisory and enforcement must agree at the exact boundaries: a gap
        # EXACTLY equal to warmup+resurfacing, and an end EXACTLY at curfew,
        # are both compliant — no warning (#277 half-open rule).
        rink = self.store.get_rink("r1")
        rink.external_ref = "R1"
        self.store.save_rink(rink)
        self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", warmup_minutes=5,
            resurfacing_minutes=10, curfew_local="22:00", actor_id="admin")
        rows = [
            "R1,2026-03-02T16:00:00+00:00,2026-03-02T17:00:00+00:00,game",
            # Exactly 15 minutes after the first row ends.
            "R1,2026-03-02T17:15:00+00:00,2026-03-02T18:15:00+00:00,game",
            # Ends exactly AT the 22:00 America/Chicago curfew (04:00Z CST).
            "R1,2026-03-03T03:00:00+00:00,2026-03-03T04:00:00+00:00,game",
        ]
        report = self.api.get_import_dry_run({
            "rinks_csv": "venue_name,rink_code,rink_name,address\n"
                         "Arena,R1,Main,",
            "ice_slots_csv":
                "rink_code,start_time,end_time,slot_type\n" + "\n".join(rows)})
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["warnings"], [], report)

    def test_import_advisory_ignores_non_game_ice_and_own_reimport_copy(self):
        rink = self.store.get_rink("r1")
        rink.external_ref = "R1"
        self.store.save_rink(rink)
        self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", warmup_minutes=5,
            resurfacing_minutes=10, actor_id="admin")
        # sA exists in the store (BASE..+60). A sheet row for sA's EXACT
        # tuple is the documented idempotent re-import identity — no
        # self-collision warning; a maintenance row 10 minutes later is not
        # game ice — buffers don't apply to it in either direction.
        rows = [
            f"R1,{BASE.isoformat()},"
            f"{(BASE + timedelta(minutes=60)).isoformat()},game",
            f"R1,{(BASE + timedelta(minutes=70)).isoformat()},"
            f"{(BASE + timedelta(minutes=100)).isoformat()},maintenance",
        ]
        report = self.api.get_import_dry_run({
            "rinks_csv": "venue_name,rink_code,rink_name,address\n"
                         "Arena,R1,Main,",
            "ice_slots_csv":
                "rink_code,start_time,end_time,slot_type\n" + "\n".join(rows)})
        self.assertTrue(report["ok"], report)
        text = " ".join(w["message"] for w in report["warnings"])
        # The row's own persisted exact-tuple copy (sA) never self-collides,
        # and nothing warns about (or on behalf of) the maintenance row —
        # the seeded NEIGHBOR slot sB may legitimately warn, which is why
        # these assertions target the two exclusions, not an empty list.
        self.assertNotIn("existing slot sA", text, report)
        self.assertNotIn("row 2", text, report)
        self.assertFalse(any(w["row"] == 2 for w in report["warnings"]
                             if "resurfacing + warm-up" in w["message"]),
                         report)

    def test_import_curfew_advisory_uses_program_tz_fallback(self):
        # A venue with NO timezone falls back to the Program's clock —
        # exactly like the gate — so the advisory and enforcement agree.
        venue = self.store.get_venue("v")
        venue.timezone = None
        self.store.save_venue(venue)
        rink = self.store.get_rink("r1")
        rink.external_ref = "R1"
        self.store.save_rink(rink)
        self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", curfew_local="22:00",
            actor_id="admin")
        # 04:00-05:30Z = 22:00-23:30 America/Chicago (the Program tz).
        report = self.api.get_import_dry_run({
            "rinks_csv": "venue_name,rink_code,rink_name,address\n"
                         "Arena,R1,Main,",
            "ice_slots_csv":
                "rink_code,start_time,end_time,slot_type\n"
                "R1,2026-03-03T04:00:00+00:00,"
                "2026-03-03T05:30:00+00:00,game"})
        self.assertTrue(report["ok"], report)
        text = " ".join(w["message"] for w in report["warnings"])
        self.assertIn("22:00 curfew", text, report)

    def test_commit_import_reports_the_same_advisories(self):
        rink = self.store.get_rink("r1")
        rink.external_ref = "R1"
        self.store.save_rink(rink)
        self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", min_playable_minutes=45,
            actor_id="admin")
        res = self.api.commit_rinks_ice_slots_import({
            "rinks_csv": "venue_name,rink_code,rink_name,address\n"
                         "Arena,R1,Main,",
            "ice_slots_csv":
                "rink_code,start_time,end_time,slot_type\n"
                "R1,2026-03-02T18:00:00+00:00,"
                "2026-03-02T18:40:00+00:00,game"}, actor_id="admin")
        self.assertTrue(res.get("committed"), res)
        text = " ".join(w["message"] for w in res.get("warnings", []))
        self.assertIn("only 40 playable minutes", text, res)

    def test_overview_reserved_null_for_cancelled_game(self):
        self._buffer_policy()
        g1 = self.api.create_game("se1", "d1", "t0", "t1", "sA",
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        self.api.cancel_game(g1["id"], actor_id="admin")
        ov = self.api.get_demo_overview()
        rows = {s["id"]: s for s in ov["ice_slots"]}
        # A cancelled game reserves nothing — the gate ignores it too.
        self.assertIsNone(rows["sA"]["reserved"], rows["sA"])

    def test_builder_note_skipped_without_back_to_back_slots(self):
        # A one-slot-per-day template has no adjacency for the buffer to
        # bite on — no note, even with a policy set.
        self._buffer_policy()
        pv = self.api.preview_ice_availability(
            season_id="se1", rink_ids=["r1"], weekdays=[0],
            start_local="12:00", end_local="13:00",
            start_date="2026-02-02", end_date="2026-02-02",
            playable_minutes=60, turnover_minutes=0, actor_id="admin")
        self.assertNotIn("error", pv, pv)
        self.assertEqual(pv["policy_notes"], [], pv)

    def test_builder_note_silent_for_far_apart_slots_on_one_day(self):
        # Two generated slots on ONE day whose REAL gap (180 min) dwarfs
        # the 15-minute requirement — silent (#319 review: adjacency comes
        # from the actual sorted consecutive intervals, never from a mere
        # same-day slot count).
        self._buffer_policy()
        pv = self.api.preview_ice_availability(
            season_id="se1", rink_ids=["r1"], weekdays=[0],
            start_local="12:00", end_local="17:00",
            start_date="2026-02-02", end_date="2026-02-02",
            playable_minutes=60, turnover_minutes=180, actor_id="admin")
        self.assertNotIn("error", pv, pv)
        self.assertEqual(pv["policy_notes"], [], pv)

    def test_builder_note_silent_at_exact_gap(self):
        # A pair whose gap EXACTLY equals the requirement is compliant —
        # half-open, matching the placement gate.
        self._buffer_policy()
        pv = self.api.preview_ice_availability(
            season_id="se1", rink_ids=["r1"], weekdays=[0],
            start_local="12:00", end_local="14:15",
            start_date="2026-02-02", end_date="2026-02-02",
            playable_minutes=60, turnover_minutes=15, actor_id="admin")
        self.assertNotIn("error", pv, pv)
        self.assertEqual(pv["policy_notes"], [], pv)

    def test_builder_note_silent_one_slot_on_each_of_two_dates(self):
        # One slot on each of two dates: consecutive only across midnight —
        # never a same-day pair, so no note.
        self._buffer_policy()
        pv = self.api.preview_ice_availability(
            season_id="se1", rink_ids=["r1"], weekdays=[0, 1],
            start_local="12:00", end_local="13:00",
            start_date="2026-02-02", end_date="2026-02-03",
            playable_minutes=60, turnover_minutes=0, actor_id="admin")
        self.assertNotIn("error", pv, pv)
        self.assertEqual(pv["policy_notes"], [], pv)

    def test_builder_note_absent_token_still_commits_under_policy(self):
        # The fingerprint round trip for the NOTE-ABSENT case: a policy is
        # set, the preview generates far-apart slots (no note), and the
        # commit under that token succeeds — the policy alone must not move
        # the token when it produces no note.
        self._buffer_policy()
        tmpl = dict(season_id="se1", rink_ids=["r1"], weekdays=[0],
                    start_local="12:00", end_local="17:00",
                    start_date="2026-02-02", end_date="2026-02-02",
                    playable_minutes=60, turnover_minutes=180)
        pv = self.api.preview_ice_availability(actor_id="admin", **tmpl)
        self.assertNotIn("error", pv, pv)
        self.assertEqual(pv["policy_notes"], [], pv)
        res = self.api.commit_ice_availability(
            actor_id="admin", template_fingerprint=pv["template_fingerprint"],
            **tmpl)
        self.assertNotIn("error", res, res)
        self.assertEqual(res["totals"]["created"], 2, res)

    def test_import_advisory_uses_hosting_games_season_policy(self):
        # #319 review — an existing slot HOSTING a game contributes that
        # game's FULL effective policy (its own Season chain), in BOTH
        # orderings, on dry-run AND commit. Season-scope-only knobs (no
        # rink scope at all) prove the season is really being resolved.
        rink = self.store.get_rink("r1")
        rink.external_ref = "R1"
        self.store.save_rink(rink)
        self.api.set_scheduling_policy(
            scope_type="season", scope_id="se1", warmup_minutes=20,
            resurfacing_minutes=20, actor_id="admin")
        g = self.api.create_game("se1", "d1", "t0", "t1", "sD",
                                 league_id="lg")
        self.assertNotIn("error", g, g)
        # sD spans 04:30-05:30Z, far from every other seeded slot (sB sits
        # right after sA, so sA would collide the commit half). One row 10
        # min AFTER it (needs the hosting game's resurfacing 20), one row
        # ending 10 min BEFORE it (needs its warm-up 20) — far enough from
        # each other to stay silent between themselves.
        _sd = datetime(2026, 1, 6, 4, 30, tzinfo=UTC)
        rows = [
            f"R1,{(_sd + timedelta(minutes=70)).isoformat()},"
            f"{(_sd + timedelta(minutes=100)).isoformat()},game",
            f"R1,{(_sd - timedelta(minutes=40)).isoformat()},"
            f"{(_sd - timedelta(minutes=10)).isoformat()},game",
        ]
        sheets = {
            "rinks_csv": "venue_name,rink_code,rink_name,address\n"
                         "Arena,R1,Main,",
            "ice_slots_csv":
                "rink_code,start_time,end_time,slot_type\n" + "\n".join(rows)}
        report = self.api.get_import_dry_run(sheets)
        self.assertTrue(report["ok"], report)
        hosted_warns = [w for w in report["warnings"]
                        if "hosts a game" in w["message"]]
        self.assertEqual({w["row"] for w in hosted_warns}, {1, 2}, report)
        # gap 10 < the hosting game's OWN side (20) in both orderings, so
        # both warnings are DEFINITIVE gate predictions naming that side.
        self.assertTrue(all("20 min on its side" in w["message"]
                            for w in hosted_warns), report)
        # Commit path reports the same advisories.
        res = self.api.commit_rinks_ice_slots_import(sheets,
                                                     actor_id="admin")
        self.assertTrue(res.get("committed"), res)
        commit_text = " ".join(w["message"] for w in res.get("warnings", []))
        self.assertIn("hosts a game", commit_text, res)

    def test_import_advisory_high_irrelevant_side_stays_silent(self):
        # A 60-minute resurfacing lives on the venue-bridged PROGRAM scope
        # — the ONLY high number anywhere — but for a row placed AFTER the
        # hosted game it sits entirely on irrelevant sides: the hosting
        # game's season shadows it to 0 on the hosted side, and on the
        # incoming side it is resurfacing (which applies after the row, not
        # between the pair). The OLD symmetric rink-level sum (0 warm-up +
        # 60 resurfacing) warned on exactly this sheet; the directional
        # split must stay silent, on dry-run AND commit.
        rink = self.store.get_rink("r1")
        rink.external_ref = "R1"
        self.store.save_rink(rink)
        self.api.set_scheduling_policy(
            scope_type="program", scope_id="pg", resurfacing_minutes=60,
            actor_id="admin")
        self.api.set_scheduling_policy(
            scope_type="season", scope_id="se1", resurfacing_minutes=0,
            actor_id="admin")
        g = self.api.create_game("se1", "d1", "t0", "t1", "sD",
                                 league_id="lg")
        self.assertNotIn("error", g, g)
        _sd = datetime(2026, 1, 6, 4, 30, tzinfo=UTC)
        sheets = {
            "rinks_csv": "venue_name,rink_code,rink_name,address\n"
                         "Arena,R1,Main,",
            "ice_slots_csv":
                "rink_code,start_time,end_time,slot_type\n"
                f"R1,{(_sd + timedelta(minutes=70)).isoformat()},"
                f"{(_sd + timedelta(minutes=100)).isoformat()},game"}
        report = self.api.get_import_dry_run(sheets)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["warnings"], [], report)
        res = self.api.commit_rinks_ice_slots_import(sheets,
                                                     actor_id="admin")
        self.assertTrue(res.get("committed"), res)
        self.assertEqual(res.get("warnings", []), [], res)

    def test_import_advisory_free_vs_hosted_labeling(self):
        # A counterpart HOSTING a game reads as a DEFINITIVE placement-gate
        # prediction only when the hosting game's own side alone exceeds
        # the gap; a season-less FREE slot always reads as a labeled
        # rink-level advisory — same rink policy, different confidence
        # (#319 review). Verified on dry-run AND commit.
        rink = self.store.get_rink("r1")
        rink.external_ref = "R1"
        self.store.save_rink(rink)
        self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", warmup_minutes=5,
            resurfacing_minutes=10, actor_id="admin")
        g = self.api.create_game("se1", "d1", "t0", "t1", "sA",
                                 league_id="lg")
        self.assertNotIn("error", g, g)
        # Row 1 ends 2 min BEFORE hosted sA (2 < the hosting game's own
        # 5-minute warm-up side -> definitive); row 2 sits 10 min after
        # FREE sB (ends BASE+130) -> rink-level. Both clear of everything
        # else (row 1 ends 72 min before sB starts; row 2 starts 80 min
        # after sA ends).
        rows = [
            f"R1,{(BASE - timedelta(minutes=32)).isoformat()},"
            f"{(BASE - timedelta(minutes=2)).isoformat()},game",
            f"R1,{(BASE + timedelta(minutes=140)).isoformat()},"
            f"{(BASE + timedelta(minutes=170)).isoformat()},game",
        ]
        sheets = {
            "rinks_csv": "venue_name,rink_code,rink_name,address\n"
                         "Arena,R1,Main,",
            "ice_slots_csv":
                "rink_code,start_time,end_time,slot_type\n" + "\n".join(rows)}
        report = self.api.get_import_dry_run(sheets)
        self.assertTrue(report["ok"], report)
        by_row = {}
        for w in report["warnings"]:
            by_row.setdefault(w["row"], []).append(w["message"])
        self.assertTrue(any("hosts a game" in m and "5 min on its side" in m
                            for m in by_row.get(1, [])), report)
        self.assertTrue(any(m.startswith("Rink-level advisory")
                            for m in by_row.get(2, [])), report)
        # The commit path reproduces the same labeling split.
        res = self.api.commit_rinks_ice_slots_import(sheets,
                                                     actor_id="admin")
        self.assertTrue(res.get("committed"), res)
        commit_by_row = {}
        for w in res.get("warnings", []):
            commit_by_row.setdefault(w["row"], []).append(w["message"])
        self.assertTrue(any("hosts a game" in m
                            for m in commit_by_row.get(1, [])), res)
        self.assertTrue(any(m.startswith("Rink-level advisory")
                            for m in commit_by_row.get(2, [])), res)

    def _overlap_preview_fixture(self):
        """A hosted slot (game on sA), a free slot (sB), and three upload
        rows: one overlapping each, plus one EXACTLY adjacent to sB's end
        (never an overlap — must stay silent at a zero requirement)."""
        rink = self.store.get_rink("r1")
        rink.external_ref = "R1"
        self.store.save_rink(rink)
        g = self.api.create_game("se1", "d1", "t0", "t1", "sA",
                                 league_id="lg")
        self.assertNotIn("error", g, g)
        rows = [
            f"R1,{(BASE + timedelta(minutes=30)).isoformat()},"
            f"{(BASE + timedelta(minutes=55)).isoformat()},game",
            f"R1,{(BASE + timedelta(minutes=80)).isoformat()},"
            f"{(BASE + timedelta(minutes=100)).isoformat()},game",
            f"R1,{(BASE + timedelta(minutes=130)).isoformat()},"
            f"{(BASE + timedelta(minutes=150)).isoformat()},game",
        ]
        return {
            "rinks_csv": "venue_name,rink_code,rink_name,address\n"
                         "Arena,R1,Main,",
            "ice_slots_csv":
                "rink_code,start_time,end_time,slot_type\n" + "\n".join(rows)}

    def _assert_overlap_previewed_and_commit_writes_nothing(self, sheets):
        # Preview surfaces BOTH persisted overlaps (hosted sA, free sB) as
        # definitive findings — independent of any configured gap (#319
        # review) — while the exactly-adjacent row stays silent.
        report = self.api.get_import_dry_run(sheets)
        self.assertTrue(report["ok"], report)
        by_row = {}
        for w in report["warnings"]:
            by_row.setdefault(w["row"], []).append(w["message"])
        self.assertTrue(any("physically overlaps existing slot sA" in m
                            for m in by_row.get(1, [])), report)
        self.assertTrue(any("physically overlaps existing slot sB" in m
                            for m in by_row.get(2, [])), report)
        self.assertNotIn(3, by_row, report)
        # The commit hard-refuses the overlapping rows — with ZERO writes.
        slots_before = sorted(s.id for s in self.store.all_ice_slots())
        games_before = sorted(g.id for g in self.store.all_games())
        audit_before = len(self.store.all_setup_audit())
        res = self.api.commit_rinks_ice_slots_import(sheets,
                                                     actor_id="admin")
        self.assertEqual(_reason(res), "ice_slot_overlap", res)
        self.assertEqual(
            sorted(s.id for s in self.store.all_ice_slots()), slots_before)
        self.assertEqual(
            sorted(g.id for g in self.store.all_games()), games_before)
        self.assertEqual(len(self.store.all_setup_audit()), audit_before,
                         "a refused import must not audit-write")

    def test_import_preview_surfaces_persisted_overlap_without_policy(self):
        # The pre-fix advisory recorded overlap only when required > 0, so
        # with NO policy rows these persisted overlaps vanished from the
        # preview while the commit (and the placement gate) refused them.
        self.assertEqual(self.store.all_scheduling_policies(), [])
        sheets = self._overlap_preview_fixture()
        self._assert_overlap_previewed_and_commit_writes_nothing(sheets)

    def test_import_preview_overlap_is_slot_type_agnostic(self):
        # The commit's overlap gate ignores slot types entirely — so must
        # the preview (#319 review): a PRACTICE row over persisted GAME
        # ice, and a GAME row over persisted PRACTICE ice, both surface as
        # definitive findings with no policy anywhere, and the commit
        # refuses the sheet with zero writes.
        rink = self.store.get_rink("r1")
        rink.external_ref = "R1"
        self.store.save_rink(rink)
        self.store.add_ice_slot(IceSlot(
            id="sPr", rink_id="r1",
            start_time=BASE + timedelta(minutes=400),
            end_time=BASE + timedelta(minutes=460),
            slot_type=IceSlotType.PRACTICE,
            status=IceSlotStatus.AVAILABLE))
        rows = [
            f"R1,{(BASE + timedelta(minutes=30)).isoformat()},"
            f"{(BASE + timedelta(minutes=55)).isoformat()},practice",
            f"R1,{(BASE + timedelta(minutes=410)).isoformat()},"
            f"{(BASE + timedelta(minutes=440)).isoformat()},game",
        ]
        sheets = {
            "rinks_csv": "venue_name,rink_code,rink_name,address\n"
                         "Arena,R1,Main,",
            "ice_slots_csv":
                "rink_code,start_time,end_time,slot_type\n" + "\n".join(rows)}
        report = self.api.get_import_dry_run(sheets)
        self.assertTrue(report["ok"], report)
        by_row = {}
        for w in report["warnings"]:
            by_row.setdefault(w["row"], []).append(w["message"])
        self.assertTrue(any("physically overlaps existing slot sA" in m
                            for m in by_row.get(1, [])), report)
        self.assertTrue(any("physically overlaps existing slot sPr" in m
                            for m in by_row.get(2, [])), report)
        slots_before = sorted(s.id for s in self.store.all_ice_slots())
        res = self.api.commit_rinks_ice_slots_import(sheets,
                                                     actor_id="admin")
        self.assertEqual(_reason(res), "ice_slot_overlap", res)
        self.assertEqual(
            sorted(s.id for s in self.store.all_ice_slots()), slots_before)

    def test_import_preview_surfaces_persisted_overlap_at_zero_policy(self):
        r = self.api.set_scheduling_policy(
            scope_type="season", scope_id="se1", warmup_minutes=0,
            resurfacing_minutes=0, actor_id="admin")
        self.assertNotIn("error", r, r)
        sheets = self._overlap_preview_fixture()
        self._assert_overlap_previewed_and_commit_writes_nothing(sheets)

    def test_import_preview_reimport_identity_not_flagged_against_third_slot(
            self):
        # Mirror-fidelity corner (#319 review): a row whose exact
        # (rink, start, end) tuple is already persisted takes the commit's
        # in-place update path — the commit never overlap-checks it against
        # OTHER persisted ice, so the preview must not claim "the commit
        # will refuse this row" even when such ice overlaps it.
        # (Overlapping persisted pairs are reachable state: same-upload
        # overlaps commit by design and stay _check_overlaps' warning-only
        # concern.)
        rink = self.store.get_rink("r1")
        rink.external_ref = "R1"
        self.store.save_rink(rink)
        self.store.add_ice_slot(IceSlot(
            id="sOv", rink_id="r1",
            start_time=BASE + timedelta(minutes=15),
            end_time=BASE + timedelta(minutes=45),
            slot_type=IceSlotType.GAME,
            status=IceSlotStatus.AVAILABLE))
        sheets = {
            "rinks_csv": "venue_name,rink_code,rink_name,address\n"
                         "Arena,R1,Main,",
            "ice_slots_csv":
                "rink_code,start_time,end_time,slot_type\n"
                f"R1,{BASE.isoformat()},"
                f"{(BASE + timedelta(minutes=60)).isoformat()},game"}
        report = self.api.get_import_dry_run(sheets)
        self.assertTrue(report["ok"], report)
        self.assertFalse(
            [w for w in report["warnings"]
             if "physically overlaps" in w["message"]], report)
        slots_before = sorted(s.id for s in self.store.all_ice_slots())
        res = self.api.commit_rinks_ice_slots_import(sheets,
                                                     actor_id="admin")
        self.assertTrue(res.get("committed"), res)
        self.assertEqual(
            sorted(s.id for s in self.store.all_ice_slots()), slots_before)

    def test_import_preview_surfaces_persisted_overlap_under_positive_policy(
            self):
        # With a REAL buffer policy configured, an overlap must still be
        # reported as the definitive commit refusal — not mislabeled as a
        # gap-0 directional-buffer advisory (the pre-fix behavior whenever
        # required > 0). The adjacent row also proves positive buffers
        # don't false-positive: sB is unhosted so its side resolves at
        # rink level (season policy invisible to _ingest_policy), and the
        # hosted sA game sits 70 min clear of its 15-min side.
        r = self.api.set_scheduling_policy(
            scope_type="season", scope_id="se1", warmup_minutes=15,
            resurfacing_minutes=15, actor_id="admin")
        self.assertNotIn("error", r, r)
        sheets = self._overlap_preview_fixture()
        self._assert_overlap_previewed_and_commit_writes_nothing(sheets)

    def _r1_sheets(self, rows):
        rink = self.store.get_rink("r1")
        rink.external_ref = "R1"
        self.store.save_rink(rink)
        return {
            "rinks_csv": "venue_name,rink_code,rink_name,address\n"
                         "Arena,R1,Main,",
            "ice_slots_csv":
                "rink_code,start_time,end_time,slot_type\n" + "\n".join(rows)}

    def test_import_preview_program_minimum_is_advisory_season_can_override(
            self):
        # #319 review — the gate resolves Rink > Season > Program, so a
        # PROGRAM-sourced minimum is only a fallback the future game's
        # season may override. Promising refusal here would be a lie —
        # proven by actually scheduling a game on the imported slot under
        # se1's softer minimum.
        for scope_type, scope_id, minimum in (("program", "pg", 60),
                                              ("season", "se1", 20)):
            r = self.api.set_scheduling_policy(
                scope_type=scope_type, scope_id=scope_id,
                min_playable_minutes=minimum, actor_id="admin")
            self.assertNotIn("error", r, r)
        day = BASE + timedelta(minutes=1440)
        sheets = self._r1_sheets([
            f"R1,{day.isoformat()},"
            f"{(day + timedelta(minutes=30)).isoformat()},game",
            # Exactly the 60-minute program fallback: boundary stays silent.
            f"R1,{(day + timedelta(minutes=120)).isoformat()},"
            f"{(day + timedelta(minutes=180)).isoformat()},game",
        ])
        report = self.api.get_import_dry_run(sheets)
        self.assertTrue(report["ok"], report)
        row1 = [w["message"] for w in report["warnings"] if w["row"] == 1]
        self.assertTrue(any("Program-level advisory" in m
                            and "program fallback minimum of 60" in m
                            for m in row1), report)
        self.assertFalse(any("will be refused" in m for m in row1), report)
        self.assertFalse([w for w in report["warnings"] if w["row"] == 2],
                         report)
        res = self.api.commit_rinks_ice_slots_import(sheets,
                                                     actor_id="admin")
        self.assertTrue(res.get("committed"), res)
        self.assertTrue(any("Program-level advisory" in w["message"]
                            for w in res.get("warnings", [])
                            if w["row"] == 1), res)
        slot = next(s for s in self.store.all_ice_slots()
                    if s.rink_id == "r1" and s.start_time == day)
        g = self.api.create_game("se1", "d1", "t0", "t1", slot.id,
                                 league_id="lg")
        self.assertNotIn("error", g,
                         ("the season override makes the slot legal — the "
                          "preview was right not to promise refusal", g))

    def test_import_preview_rink_minimum_stays_definitive_despite_season(
            self):
        # A RINK-sourced minimum outranks every season at the gate, so the
        # definitive "will be refused a game" wording stays — proven by the
        # gate refusing even under se1's softer season policy.
        for scope_type, scope_id, minimum in (("rink", "r1", 60),
                                              ("season", "se1", 20)):
            r = self.api.set_scheduling_policy(
                scope_type=scope_type, scope_id=scope_id,
                min_playable_minutes=minimum, actor_id="admin")
            self.assertNotIn("error", r, r)
        day = BASE + timedelta(minutes=1440)
        sheets = self._r1_sheets([
            f"R1,{day.isoformat()},"
            f"{(day + timedelta(minutes=30)).isoformat()},game",
            f"R1,{(day + timedelta(minutes=120)).isoformat()},"
            f"{(day + timedelta(minutes=180)).isoformat()},game",
        ])
        report = self.api.get_import_dry_run(sheets)
        self.assertTrue(report["ok"], report)
        row1 = [w["message"] for w in report["warnings"] if w["row"] == 1]
        self.assertTrue(any("will be refused a game" in m for m in row1),
                        report)
        self.assertFalse(any("Program-level advisory" in m for m in row1),
                         report)
        self.assertFalse([w for w in report["warnings"] if w["row"] == 2],
                         report)
        res = self.api.commit_rinks_ice_slots_import(sheets,
                                                     actor_id="admin")
        self.assertTrue(res.get("committed"), res)
        slot = next(s for s in self.store.all_ice_slots()
                    if s.rink_id == "r1" and s.start_time == day)
        g = self.api.create_game("se1", "d1", "t0", "t1", slot.id,
                                 league_id="lg")
        self.assertEqual(_reason(g), "insufficient_playable_time", g)

    def test_import_preview_program_curfew_is_advisory_season_can_override(
            self):
        # Same sourcing hinge for curfew: program fallback 21:00, but se1
        # allows 23:30 — the imported 20:30-22:00 local slot is advisory
        # only, and a se1 game on it is legal.
        for scope_type, scope_id, curfew in (("program", "pg", "21:00"),
                                             ("season", "se1", "23:30")):
            r = self.api.set_scheduling_policy(
                scope_type=scope_type, scope_id=scope_id,
                curfew_local=curfew, actor_id="admin")
            self.assertNotIn("error", r, r)
        start = datetime(2026, 1, 7, 2, 30, tzinfo=UTC)   # Jan 6 20:30 local
        sheets = self._r1_sheets([
            f"R1,{start.isoformat()},"
            f"{(start + timedelta(minutes=90)).isoformat()},game",
            # Ends exactly AT the 21:00 fallback curfew: boundary silent.
            f"R1,{datetime(2026, 1, 9, 1, 30, tzinfo=UTC).isoformat()},"
            f"{datetime(2026, 1, 9, 3, 0, tzinfo=UTC).isoformat()},game",
        ])
        report = self.api.get_import_dry_run(sheets)
        self.assertTrue(report["ok"], report)
        row1 = [w["message"] for w in report["warnings"] if w["row"] == 1]
        self.assertTrue(any("Program-level advisory" in m
                            and "program fallback curfew 21:00" in m
                            and "22:00" in m for m in row1), report)
        self.assertFalse(any("will be refused" in m for m in row1), report)
        self.assertFalse([w for w in report["warnings"] if w["row"] == 2],
                         report)
        res = self.api.commit_rinks_ice_slots_import(sheets,
                                                     actor_id="admin")
        self.assertTrue(res.get("committed"), res)
        self.assertTrue(any("Program-level advisory" in w["message"]
                            for w in res.get("warnings", [])
                            if w["row"] == 1), res)
        slot = next(s for s in self.store.all_ice_slots()
                    if s.rink_id == "r1" and s.start_time == start)
        g = self.api.create_game("se1", "d1", "t0", "t1", slot.id,
                                 league_id="lg")
        self.assertNotIn("error", g, g)

    def test_import_preview_rink_curfew_stays_definitive_despite_season(
            self):
        # A RINK-sourced curfew binds whatever season the game brings —
        # se1's later 23:30 cannot soften it, so the definitive wording
        # stays and the gate indeed refuses.
        for scope_type, scope_id, curfew in (("rink", "r1", "21:00"),
                                             ("season", "se1", "23:30")):
            r = self.api.set_scheduling_policy(
                scope_type=scope_type, scope_id=scope_id,
                curfew_local=curfew, actor_id="admin")
            self.assertNotIn("error", r, r)
        start = datetime(2026, 1, 7, 2, 30, tzinfo=UTC)   # Jan 6 20:30 local
        sheets = self._r1_sheets([
            f"R1,{start.isoformat()},"
            f"{(start + timedelta(minutes=90)).isoformat()},game",
            f"R1,{datetime(2026, 1, 9, 1, 30, tzinfo=UTC).isoformat()},"
            f"{datetime(2026, 1, 9, 3, 0, tzinfo=UTC).isoformat()},game",
        ])
        report = self.api.get_import_dry_run(sheets)
        self.assertTrue(report["ok"], report)
        row1 = [w["message"] for w in report["warnings"] if w["row"] == 1]
        self.assertTrue(any("past rink R1's 21:00 curfew" in m
                            and "will be refused a game" in m
                            for m in row1), report)
        self.assertFalse(any("Program-level advisory" in m for m in row1),
                         report)
        self.assertFalse([w for w in report["warnings"] if w["row"] == 2],
                         report)
        res = self.api.commit_rinks_ice_slots_import(sheets,
                                                     actor_id="admin")
        self.assertTrue(res.get("committed"), res)
        slot = next(s for s in self.store.all_ice_slots()
                    if s.rink_id == "r1" and s.start_time == start)
        g = self.api.create_game("se1", "d1", "t0", "t1", slot.id,
                                 league_id="lg")
        self.assertEqual(_reason(g), "curfew_violation", g)

    def test_import_preview_malformed_row_never_claims_program_fallback(
            self):
        # Adversarial-review corner (#319): a malformed end<=start row with
        # NO policy resolved anywhere (source None, not "program") must not
        # fabricate a "Program-level advisory ... program fallback minimum"
        # claim — there is no Program policy to advise about. The row's
        # only warranted finding is the pre-existing end_time error.
        self.assertEqual(self.store.all_scheduling_policies(), [])
        start = datetime(2026, 1, 6, 18, tzinfo=UTC)
        sheets = self._r1_sheets([
            f"R1,{start.isoformat()},"
            f"{(start - timedelta(minutes=60)).isoformat()},game",
        ])
        report = self.api.get_import_dry_run(sheets)
        self.assertFalse(report["ok"], report)
        self.assertTrue(any(e["message"] == "end_time must be after start_time."
                            for e in report["errors"]), report)
        self.assertEqual(report["warnings"], [], report)

    def test_reserved_shown_for_committed_draft_and_discard_removes(self):
        # #319 review — a committed draft physically reserves warm-up +
        # resurfacing ice: the operator calendar AND the draft-review rows
        # must show the same derived span, and discarding the draft frees
        # it everywhere.
        self._buffer_policy()
        res = commit_fresh_draft(self.api, "d1", slot_ids=["sA"])
        self.assertNotIn("error", res, res)
        self.assertEqual(len(res["created"]), 1, res)
        ov = self.api.get_demo_overview()
        row = {s["id"]: s for s in ov["ice_slots"]}["sA"]
        self.assertIsNotNone(row["reserved"], row)
        self.assertEqual(
            (row["reserved"]["warmup_minutes"],
             row["reserved"]["resurfacing_minutes"]), (5, 10), row)
        review = self.api.list_draft_games()
        review_rows = [r for r in review["draft_games"] if r["is_draft"]]
        self.assertEqual(len(review_rows), 1, review)
        self.assertEqual(review_rows[0]["reserved"], row["reserved"], review)
        # The grid LABEL stays draft-free (#86) even while the reserved
        # span shows — physical ice vs review-only proposals.
        self.assertIsNone(row["game_id"], row)
        # Discard frees the reserved span everywhere.
        gid = review_rows[0]["game_id"]
        d = self.api.discard_draft_games(game_ids=[gid], actor_id="admin")
        self.assertNotIn("error", d, d)
        ov2 = self.api.get_demo_overview()
        row2 = {s["id"]: s for s in ov2["ice_slots"]}["sA"]
        self.assertIsNone(row2["reserved"], row2)

    def test_schedule_rows_carry_reserved_for_real_games(self):
        # The schedule review rows share the calendar's derivation: a real
        # game's row carries the span; a zero-buffer policy carries None.
        self._buffer_policy()
        g = self.api.create_game("se1", "d1", "t0", "t1", "sA",
                                 league_id="lg")
        self.assertNotIn("error", g, g)
        ov = self.api.get_demo_overview()
        sched = {r["game_id"]: r for r in ov["schedule"]}
        slot_row = {s["id"]: s for s in ov["ice_slots"]}["sA"]
        self.assertEqual(sched[g["id"]]["reserved"], slot_row["reserved"],
                         ov["schedule"])
        self.assertIsNotNone(sched[g["id"]]["reserved"])
        # PUBLISHING must not strip the span — a published game reserves
        # the same physical ice (the occupant map may never filter on the
        # published flag).
        p = self.api.publish_game(g["id"], actor_id="admin")
        self.assertNotIn("error", p, p)
        ovp = self.api.get_demo_overview()
        self.assertEqual(
            {r["game_id"]: r for r in ovp["schedule"]}[g["id"]]["reserved"],
            slot_row["reserved"], ovp["schedule"])
        self.assertEqual(
            {s["id"]: s for s in ovp["ice_slots"]}["sA"]["reserved"],
            slot_row["reserved"], ovp)
        # Zero-buffer scope (min_playable only) -> None on both surfaces.
        self.api.set_scheduling_policy(
            scope_type="season", scope_id="se1", min_playable_minutes=45,
            actor_id="admin")
        ov2 = self.api.get_demo_overview()
        self.assertIsNone(
            {r["game_id"]: r for r in ov2["schedule"]}[g["id"]]["reserved"],
            ov2["schedule"])
        self.assertIsNone(
            {s["id"]: s for s in ov2["ice_slots"]}["sA"]["reserved"], ov2)

    def test_import_dry_run_no_policy_no_warnings(self):
        rink = self.store.get_rink("r1")
        rink.external_ref = "R1"
        self.store.save_rink(rink)
        report = self.api.get_import_dry_run({
            "rinks_csv": "venue_name,rink_code,rink_name,address\n"
                         "Arena,R1,Main,",
            "ice_slots_csv":
                "rink_code,start_time,end_time,slot_type\n"
                "R1,2026-03-02T18:00:00+00:00,"
                "2026-03-02T18:40:00+00:00,game"})
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["warnings"], [], report)


class _DirectionalBufferContract:
    """#318 review — the required gap between two adjacent games is the
    EARLIER game's resurfacing + the LATER game's warm-up, each from that
    game's OWN effective policy (two seasons sharing a rink can differ).
    The candidate's irrelevant-side buffer never blocks."""

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

    def _seasons(self, se1=None, se2=None):
        for sid, knobs in (("se1", se1), ("se2", se2)):
            if knobs:
                r = self.api.set_scheduling_policy(
                    scope_type="season", scope_id=sid, actor_id="admin",
                    **knobs)
                assert "error" not in r, r

    def test_existing_earlier_games_resurfacing_blocks_the_candidate(self):
        # Existing se1 game on sA needs 15m resurfacing AFTER it; the se2
        # candidate has an all-None policy. The old candidate-only sum (0)
        # would have under-enforced.
        self._seasons(se1={"resurfacing_minutes": 15})
        g1 = self.api.create_game("se1", "d1", "t0", "t1", "sA",
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        g2 = self.api.create_game("se2", "d2", "u0", "u1", "sB",
                                  league_id="lg")
        self.assertEqual(_reason(g2), "turnover_buffer_conflict", g2)
        d = g2["error"]["details"]
        self.assertEqual((d["required_gap_minutes"], d["gap_minutes"]),
                         (15, 10), g2)

    def test_existing_later_games_warmup_blocks_an_earlier_candidate(self):
        # Existing se1 game on sB needs 15m warm-up BEFORE it; the se2
        # candidate placed EARLIER on sA has an all-None policy.
        self._seasons(se1={"warmup_minutes": 15})
        g1 = self.api.create_game("se1", "d1", "t0", "t1", "sB",
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        g2 = self.api.create_game("se2", "d2", "u0", "u1", "sA",
                                  league_id="lg")
        self.assertEqual(_reason(g2), "turnover_buffer_conflict", g2)
        self.assertEqual(
            g2["error"]["details"]["required_gap_minutes"], 15, g2)

    def test_candidates_irrelevant_side_buffer_never_blocks(self):
        # The LATER candidate carries a huge resurfacing (applies AFTER it,
        # not before) and no warm-up; the earlier existing game requires
        # nothing — the pair is compliant. The old symmetric sum (60) would
        # have over-enforced.
        self._seasons(se2={"resurfacing_minutes": 60})
        g1 = self.api.create_game("se1", "d1", "t0", "t1", "sA",
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        g2 = self.api.create_game("se2", "d2", "u0", "u1", "sB",
                                  league_id="lg")
        self.assertNotIn("error", g2, g2)

    def test_earlier_games_irrelevant_warmup_never_blocks(self):
        # The mirror pin: the EARLIER existing game carries a huge warm-up
        # (it applies BEFORE that game, not between the pair) and no
        # resurfacing; the later candidate needs nothing on its side either
        # — compliant. A formula that wrongly folded the neighbor's
        # irrelevant side into the requirement (e.g. earlier.resurfacing +
        # earlier.warmup + later.warmup) would block this pair.
        self._seasons(se1={"warmup_minutes": 60})
        g1 = self.api.create_game("se1", "d1", "t0", "t1", "sA",
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        g2 = self.api.create_game("se2", "d2", "u0", "u1", "sB",
                                  league_id="lg")
        self.assertNotIn("error", g2, g2)

    def test_candidates_own_resurfacing_applies_when_it_is_earlier(self):
        # Same policies as above, but the candidate goes EARLIER: now its
        # 60m resurfacing applies between it and the existing later game.
        self._seasons(se2={"resurfacing_minutes": 60})
        g1 = self.api.create_game("se1", "d1", "t0", "t1", "sB",
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        g2 = self.api.create_game("se2", "d2", "u0", "u1", "sA",
                                  league_id="lg")
        self.assertEqual(_reason(g2), "turnover_buffer_conflict", g2)
        self.assertEqual(
            g2["error"]["details"]["required_gap_minutes"], 60, g2)

    def test_directional_gap_exactly_equal_is_compliant(self):
        # required = earlier(se1).resurfacing 10 + later(se2).warmup 0 = 10
        # == the actual 10-minute sA->sB gap: compliant, half-open.
        self._seasons(se1={"resurfacing_minutes": 10})
        g1 = self.api.create_game("se1", "d1", "t0", "t1", "sA",
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        g2 = self.api.create_game("se2", "d2", "u0", "u1", "sB",
                                  league_id="lg")
        self.assertNotIn("error", g2, g2)

    def test_move_and_draft_commit_and_advisory_share_the_direction(self):
        self._seasons(se1={"resurfacing_minutes": 15})
        g1 = self.api.create_game("se1", "d1", "t0", "t1", "sA",
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        # move: an se2 game parked far away refuses to move next to it.
        g2 = self.api.create_game("se2", "d2", "u0", "u1", "sC",
                                  league_id="lg")
        self.assertNotIn("error", g2, g2)
        mv = self.api.move_game(g2["id"], "sB")
        self.assertEqual(_reason(mv), "turnover_buffer_conflict", mv)
        # A third team (#206 slice 1): d2's only pairing (u0 vs u1) already
        # has a real game (g2), so the scheduler would otherwise find
        # nothing left to (re)propose against sB -- a genuinely open pairing
        # is needed to exercise the advisory/commit gate below.
        self.store.add_team(Team(id="u2", name="U2", division_id="d2",
                                 program_id="pg", league_id="lg"))
        self.store.add_season_team_registration(SeasonTeamRegistration(
            id="reg2_u2", league_season_id="ls2", team_id="u2",
            division_id="d2", active=True))
        # draft-commit + scheduler advisory (same shared implementation):
        # an se2 draft over sB reports the code instead of proposing it.
        prop = self.api.draft_season_schedule("d2", slot_ids=["sB"])
        self.assertNotIn("error", prop, prop)
        self.assertEqual(prop["draft_games"], [], prop)
        codes = {c for row in prop["unscheduled"]
                 for c in row["reason_codes"]}
        self.assertIn("turnover_buffer_conflict", codes, prop)
        res = commit_fresh_draft(self.api, "d2", slot_ids=["sB"])
        if "error" in res:
            self.assertEqual(_reason(res), "turnover_buffer_conflict", res)
        else:
            self.assertEqual(res["created"], [], res)


class MemoryDirectionalBufferTest(_DirectionalBufferContract,
                                  unittest.TestCase):
    def _make_store(self):
        return InMemoryStore()


class SqliteDirectionalBufferTest(_DirectionalBufferContract,
                                  unittest.TestCase):
    def _make_store(self):
        return SqlStore(":memory:")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresDirectionalBufferTest(_DirectionalBufferContract,
                                    unittest.TestCase):
    def _make_store(self):
        store = SqlStore(os.environ["TEST_DATABASE_URL"])
        store.clear_all_data()
        return store


class _SlotOverlapContract:
    """#318 review — two games can NEVER share physically overlapping slots
    on one rink, regardless of configured turnover: the import path
    deliberately persists overlapping contracted rows (warnings only), so
    the placement gate is the physical-exclusivity enforcement point.
    Overlapping GAME rows are seeded through that real import; the gate
    must refuse the second game through create, move, BOTH draft-commit
    facades, and the scheduler advisory — under NO policy and under an
    EXPLICIT all-zero policy — while exact adjacency stays legal at a zero
    requirement and a refused/empty batch allocates nothing."""

    _T0 = datetime(2026, 1, 20, 18, 0, tzinfo=UTC)

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

    def _import_overlapping_pair(self):
        """Two OVERLAPPING game rows in ONE upload on rink r1 (persisted
        with warnings — the import's documented no-silent-rewrite
        contract), far from every seeded slot; returns
        (earlier_slot_id, later_slot_id)."""
        rink = self.store.get_rink("r1")
        rink.external_ref = "R1"
        self.store.save_rink(rink)
        res = self.api.commit_rinks_ice_slots_import({
            "rinks_csv": "venue_name,rink_code,rink_name,address\n"
                         "Arena,R1,Main,",
            "ice_slots_csv":
                "rink_code,start_time,end_time,slot_type\n"
                f"R1,{self._T0.isoformat()},"
                f"{(self._T0 + timedelta(minutes=60)).isoformat()},game\n"
                f"R1,{(self._T0 + timedelta(minutes=30)).isoformat()},"
                f"{(self._T0 + timedelta(minutes=90)).isoformat()},game"},
            actor_id="admin")
        self.assertTrue(res.get("committed"), res)
        pair = sorted((s for s in self.store.all_ice_slots()
                       if s.rink_id == "r1" and s.start_time >= self._T0),
                      key=lambda s: s.start_time)
        self.assertEqual(len(pair), 2, pair)
        return pair[0].id, pair[1].id

    def _placement_state(self):
        return (
            sorted((g.id, g.ice_slot_id) for g in self.store.all_games()
                   if not g.cancelled),
            sorted((s.id, s.status.value)
                   for s in self.store.all_ice_slots()),
        )

    def _assert_overlap_refused_everywhere(self):
        sx, sy = self._import_overlapping_pair()
        g1 = self.api.create_game("se1", "d1", "t0", "t1", sx,
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        # create — refused with games, slot statuses, AND audits untouched.
        audit_len = len(self.store.all_setup_audit())
        state = self._placement_state()
        g2 = self.api.create_game("se1", "d1", "t2", "t3", sy,
                                  league_id="lg")
        self.assertEqual(_reason(g2), "slot_overlap_conflict", g2)
        self.assertEqual(self._placement_state(), state, g2)
        self.assertEqual(len(self.store.all_setup_audit()), audit_len,
                         "a refused create must not audit-write")
        # move: an se2 game parked on another rink refuses to move onto
        # the overlapping slot (cross-season — the rule is season-blind),
        # again leaving state and audits untouched.
        g3 = self.api.create_game("se2", "d2", "u0", "u1", "sE",
                                  league_id="lg")
        self.assertNotIn("error", g3, g3)
        # A third team (#206 slice 1): d2's only pairing (u0 vs u1) already
        # has a real game (g3), so the scheduler would otherwise find
        # nothing left to (re)propose against sy -- a genuinely open pairing
        # is needed to exercise the advisory/commit gate below.
        self.store.add_team(Team(id="u2", name="U2", division_id="d2",
                                 program_id="pg", league_id="lg"))
        self.store.add_season_team_registration(SeasonTeamRegistration(
            id="reg2_u2", league_season_id="ls2", team_id="u2",
            division_id="d2", active=True))
        audit_len = len(self.store.all_setup_audit())
        state = self._placement_state()
        mv = self.api.move_game(g3["id"], sy)
        self.assertEqual(_reason(mv), "slot_overlap_conflict", mv)
        self.assertEqual(self._placement_state(), state, mv)
        self.assertEqual(len(self.store.all_setup_audit()), audit_len,
                         "a refused move must not audit-write")
        # Scheduler advisory: the shared implementation reports the same
        # code instead of proposing the doomed row — even with ZERO policy
        # rows in the store.
        prop = self.api.draft_season_schedule("d2", slot_ids=[sy])
        self.assertNotIn("error", prop, prop)
        self.assertEqual(prop["draft_games"], [], prop)
        codes = {c for row in prop["unscheduled"]
                 for c in row["reason_codes"]}
        self.assertIn("slot_overlap_conflict", codes, prop)
        # ...and BOTH draft-commit facades allocate nothing. Because the
        # commit regenerates its proposal through the SAME shared
        # evaluation, the advisory deterministically empties the batch
        # before the per-row gate could fire — created stays [], and games
        # + slot statuses are untouched (the empty commit's own audit row
        # is its documented, legitimate write).
        for facade in (self.api, BaseFacadeApiService(self.store)):
            before = self._placement_state()
            res = commit_fresh_draft(facade, "d2", slot_ids=[sy])
            self.assertNotIn("error", res, res)
            self.assertEqual(res["created"], [], res)
            self.assertEqual(self._placement_state(), before,
                             "a refused batch must allocate nothing")

    def test_seasonless_legacy_game_move_refused(self):
        # The rule binds even when NO season — hence no policy scope —
        # resolves (#318 review round 2): a legacy exhibition whose
        # season_id was never backfilled still cannot land on ice that
        # overlaps an active game.
        sx, sy = self._import_overlapping_pair()
        g1 = self.api.create_game("se1", "d1", "t0", "t1", sx,
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        gx = self.api.create_game("se2", None, "u0", "u1", "sE",
                                  league_id=None, game_type="exhibition")
        self.assertNotIn("error", gx, gx)
        legacy = self.store.get_game(gx["id"])
        legacy.season_id = None
        self.store.save_game(legacy)
        # The PRODUCTION league-scoped path fails closed even earlier —
        # season-less game ice is refused outright before any assignment.
        mv = self.api.move_game(gx["id"], sy)
        self.assertEqual(_reason(mv), "season_missing", mv)
        # The base service (a deployment without the league-scope layer)
        # reaches the shared gate — where the unconditional overlap rule
        # now binds despite season_id=None.
        setup = BaseSetupService(self.store)
        with self.assertRaises(ScheduleConflictError) as ctx:
            setup.move_game(gx["id"], sy)
        self.assertEqual(ctx.exception.details["reason"],
                         "slot_overlap_conflict")

    def test_advisory_reports_overlap_without_a_season(self):
        # The advisory closure mirrors the gate even for a season-less
        # (legacy-division) run: same shared implementation, same code.
        sx, sy = self._import_overlapping_pair()
        g1 = self.api.create_game("se1", "d1", "t0", "t1", sx,
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        check = _policy_advisor(self.store, None)
        code, _msg = check(self.store.get_ice_slot(sy), ())
        self.assertEqual(code, "slot_overlap_conflict")

    def test_overlap_refused_without_any_policy(self):
        self.assertEqual(self.store.all_scheduling_policies(), [])
        self._assert_overlap_refused_everywhere()

    def test_overlap_refused_with_explicit_zero_policy(self):
        for sid in ("se1", "se2"):
            r = self.api.set_scheduling_policy(
                scope_type="season", scope_id=sid, warmup_minutes=0,
                resurfacing_minutes=0, actor_id="admin")
            self.assertNotIn("error", r, r)
        self._assert_overlap_refused_everywhere()

    def test_exact_adjacency_at_zero_requirement_stays_legal(self):
        # end == start is NOT overlap: with explicit zeros the touching
        # pair hosts two games (half-open boundary, like the buffers).
        r = self.api.set_scheduling_policy(
            scope_type="season", scope_id="se1", warmup_minutes=0,
            resurfacing_minutes=0, actor_id="admin")
        self.assertNotIn("error", r, r)
        self.store.add_ice_slot(IceSlot(
            id="sT", rink_id="r1",
            start_time=BASE + timedelta(minutes=60),
            end_time=BASE + timedelta(minutes=70),
            slot_type=IceSlotType.GAME, status=IceSlotStatus.AVAILABLE))
        g1 = self.api.create_game("se1", "d1", "t0", "t1", "sA",
                                  league_id="lg")
        self.assertNotIn("error", g1, g1)
        g2 = self.api.create_game("se1", "d1", "t2", "t3", "sT",
                                  league_id="lg")
        self.assertNotIn("error", g2, g2)


class MemorySlotOverlapTest(_SlotOverlapContract, unittest.TestCase):
    def _make_store(self):
        return InMemoryStore()


class SqliteSlotOverlapTest(_SlotOverlapContract, unittest.TestCase):
    def _make_store(self):
        return SqlStore(":memory:")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresSlotOverlapTest(_SlotOverlapContract, unittest.TestCase):
    def _make_store(self):
        store = SqlStore(os.environ["TEST_DATABASE_URL"])
        store.clear_all_data()
        return store


class _CurfewOperatingDayContract:
    """#318 review — the operating day is defined relative to the curfew
    itself: a small-hours start at/before the curfew uses that date's
    deadline; any start after the curfew (morning, midday, evening) uses
    the following date's. Spring-forward-skipped curfew wall times pin to
    the normalized instant."""

    def _make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self._make_store()
        _seed(self.store)
        self.api = ApiService(self.store)
        r = self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", curfew_local="01:00",
            actor_id="admin")
        assert "error" not in r, r

    def tearDown(self):
        conn = getattr(self.store, "conn", None)
        if conn is not None:
            conn.close()

    def _slot(self, sid, start_utc, minutes=60):
        self.store.add_ice_slot(IceSlot(
            id=sid, rink_id="r1", start_time=start_utc,
            end_time=start_utc + timedelta(minutes=minutes),
            slot_type=IceSlotType.GAME, status=IceSlotStatus.AVAILABLE))
        return sid

    def _place(self, sid):
        return self.api.create_game("se1", "d1", "t0", "t1", sid,
                                    league_id="lg")

    def test_small_hours_slot_violates_that_nights_close(self):
        # 00:30-02:00 local (06:30Z) vs 01:00 -> tonight's close, rejected.
        self._slot("cur1", datetime(2026, 1, 6, 6, 30, tzinfo=UTC), 90)
        self.assertEqual(_reason(self._place("cur1")), "curfew_violation")

    def test_slot_starting_exactly_at_the_curfew_violates(self):
        # A non-zero slot starting AT 01:00 necessarily ends past it —
        # bound to that date's deadline (pinned boundary rule).
        self._slot("cur2", datetime(2026, 1, 6, 7, 0, tzinfo=UTC))
        self.assertEqual(_reason(self._place("cur2")), "curfew_violation")

    def test_morning_and_midday_slots_are_not_falsely_rejected(self):
        # 08:00 and 11:30 local starts belong to the NEXT operating day.
        self._slot("cur3", datetime(2026, 1, 6, 14, 0, tzinfo=UTC))
        self._slot("cur4", datetime(2026, 1, 7, 17, 30, tzinfo=UTC))
        g = self._place("cur3")
        self.assertNotIn("error", g, g)
        g = self.api.create_game("se1", "d1", "t2", "t3", "cur4",
                                 league_id="lg")
        self.assertNotIn("error", g, g)

    def test_evening_overnight_slot_uses_the_following_morning(self):
        # 22:30-23:30 local (sD): following 01:00 -> compliant; a longer
        # 22:30-01:30 overnight slot violates it.
        g = self._place("sD")
        self.assertNotIn("error", g, g)
        self._slot("cur5", datetime(2026, 1, 8, 4, 30, tzinfo=UTC), 180)
        g = self.api.create_game("se1", "d1", "t2", "t3", "cur5",
                                 league_id="lg")
        self.assertEqual(_reason(g), "curfew_violation", g)

    def test_spring_forward_skipped_curfew_pins_to_normalized_instant(self):
        # 2026-03-08 America/Chicago springs 02:00->03:00; a 02:30 curfew
        # resolves to 03:30 CDT (08:30Z). A 01:45-03:00 CDT slot (ends
        # 08:00Z) passes; extending to 04:00 CDT (09:00Z) violates.
        r = self.api.set_scheduling_policy(
            scope_type="rink", scope_id="r1", curfew_local="02:30",
            actor_id="admin")
        self.assertNotIn("error", r, r)
        # A 01:00 CST start (07:00Z) is at/before the curfew wall clock, so
        # tonight's deadline applies; 105 minutes lands at 03:45 CDT
        # (08:45Z) — past the pinned 03:30 CDT (08:30Z) — and violates,
        # while a 01:45 CST start ending 03:00 CDT (08:00Z) is compliant.
        self._slot("dst2", datetime(2026, 3, 8, 7, 0, tzinfo=UTC), 105)
        g = self.api.create_game("se1", "d1", "t2", "t3", "dst2",
                                 league_id="lg")
        self.assertEqual(_reason(g), "curfew_violation", g)
        self._slot("dst1", datetime(2026, 3, 8, 7, 45, tzinfo=UTC), 15)
        g = self._place("dst1")
        self.assertNotIn("error", g, g)

    def test_scheduler_advisory_shares_the_operating_day_rule(self):
        self._slot("cur6", datetime(2026, 1, 6, 6, 30, tzinfo=UTC), 90)
        prop = self.api.draft_season_schedule("d1", slot_ids=["cur6"])
        self.assertNotIn("error", prop, prop)
        self.assertEqual(prop["draft_games"], [], prop)
        codes = {c for row in prop["unscheduled"]
                 for c in row["reason_codes"]}
        self.assertIn("curfew_violation", codes, prop)


class MemoryCurfewOperatingDayTest(_CurfewOperatingDayContract,
                                   unittest.TestCase):
    def _make_store(self):
        return InMemoryStore()


class SqliteCurfewOperatingDayTest(_CurfewOperatingDayContract,
                                   unittest.TestCase):
    def _make_store(self):
        return SqlStore(":memory:")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresCurfewOperatingDayTest(_CurfewOperatingDayContract,
                                     unittest.TestCase):
    def _make_store(self):
        store = SqlStore(os.environ["TEST_DATABASE_URL"])
        store.clear_all_data()
        return store


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
    """A slot that materializes between create_game's pre-lock locator reads
    (the scope-plan locator and the slot locator, which decide WHETHER to
    take the rink lock) and the gate's own re-read would otherwise run the
    whole placement — including the turnover-buffer scan, which has no DB
    backstop — with no rink lock held. The defensive post-gate re-verify
    refuses it with a stable retryable conflict instead (#277 Slice B
    review; mirrors move_game's _MoveGameRaced re-check)."""

    def test_slot_materializing_after_locator_read_is_refused(self):
        store = InMemoryStore()
        _seed(store)
        setup = BaseSetupService(store)
        real = store.get_ice_slot
        calls = {"n": 0}

        def locator_miss(slot_id):
            if slot_id == "sA":
                calls["n"] += 1
                if calls["n"] <= 2:
                    return None  # BOTH pre-lock locators ran before it existed
            return real(slot_id)

        store.get_ice_slot = locator_miss
        with self.assertRaises(ConcurrencyConflictError) as ctx:
            setup.create_game("se1", "d1", "t0", "t1", "sA", league_id="lg")
        self.assertEqual(ctx.exception.details["reason"], "placement_raced")
        # Zero writes: no game, slot untouched.
        self.assertEqual([g for g in store.all_games() if not g.cancelled], [])
        self.assertEqual(store.get_ice_slot("sA").status,
                         IceSlotStatus.AVAILABLE)


class LeagueScopedCreateLocatorRaceTest(unittest.TestCase):
    """The PRODUCTION create path (the league-scoped override) pins the
    lock order at its entry point; when BOTH of its pre-lock slot reads
    miss (the slot materializes mid-flight — a builder/import commit
    landing between them) the base body may see the slot afterwards but
    must NOT take a fresh Rink lock after the override's Season locks
    (Rink-before-Season would invert against the builder, #318 round-2
    review). It refuses with the retryable ``placement_raced`` instead;
    the API facade's retry loop then re-plans in canonical order on a
    fresh attempt."""

    def test_slot_materializing_after_override_locators_is_refused(self):
        store = InMemoryStore()
        _seed(store)
        setup = LeagueSetupService(store)
        real = store.get_ice_slot
        calls = {"n": 0}

        def locator_miss(slot_id):
            if slot_id == "sA":
                calls["n"] += 1
                if calls["n"] <= 2:      # both OVERRIDE locators miss
                    return None
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
        cls.httpd.server_close()

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


class _PolicyPgRaceHarness:
    """Shared PostgreSQL harness for the forced policy races: a per-test
    seeded database, connection-TRACKED stores (every store any thread
    opens is registered and closed in tearDown — the pause hooks create
    instance->closure->instance cycles that would otherwise keep psycopg
    connections alive until a cyclic GC), and the GRACE window a paused
    writer holds open. A plain mixin, NOT a TestCase — the two race
    classes stay unrelated so neither collects the other's tests."""

    GRACE = 1.5  # seconds the paused rival window stays open

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        self._opened = []
        seed = self._store()
        seed.clear_all_data()
        _seed(seed)

    def tearDown(self):
        for s in self._opened:
            conn = getattr(s, "conn", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _store(self):
        s = SqlStore(self.url)
        self._opened.append(s)  # GIL-atomic; threads register here too
        return s


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresPolicyEditPlacementRaceTest(_PolicyPgRaceHarness,
                                          unittest.TestCase):
    """#318 review — placement snapshots must CONTEND with policy edits at
    EVERY scope the gate reads: the candidate's chain AND each same-rink
    neighbor game's Season/Program (the directional buffer resolves the
    neighbor's own policy). Placements lock Program -> Team -> Rink ->
    Season (the ice-availability builder's Program-first canonical order —
    builder-vs-placement ordering itself is pinned by
    test_placement_concurrency's _builder_vs races), planned by a pre-lock
    locator and re-verified under the locks; set_scheduling_policy holds
    its single scope row FOR UPDATE.

    The FORCED tests interleave deterministically: the placement's own
    store pauses at its FIRST Game write — every policy read done, every
    lock held — and only then wakes the editor, recording whether the edit
    finished inside that window. Serialized code makes that impossible (the
    edit blocks on a locked scope row until the placement commits), so an
    edit-finished-inside-the-window run that still placed its game is
    exactly the stale-policy acceptance this class exists to refuse — the
    unfixed code fails these tests deterministically. A barrier storm then
    proves the whole order deadlock-free."""

    def _run(self, targets):
        barrier = threading.Barrier(len(targets))
        out = [None] * len(targets)

        def wrap(i, fn):
            api = ApiService(self._store())
            barrier.wait()
            try:
                out[i] = fn(api)
            except Exception as exc:  # surfaced by the caller's asserts
                out[i] = exc
        threads = [threading.Thread(target=wrap, args=(i, fn))
                   for i, fn in enumerate(targets)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for r in out:
            self.assertFalse(isinstance(r, Exception), out)
        return out

    def _forced(self, place, edit):
        """Deterministic interleaving: run ``place`` on a store whose first
        Game write (add_game/save_game) pauses — policy reads done, locks
        held — wakes the editor, and holds the window ``GRACE`` seconds.
        Returns (place_res, edit_res, edit_in_window, paused): whether the
        edit COMPLETED inside the window, and whether the pause fired at
        all (a placement refused before writing never pauses)."""
        reads_done = threading.Event()
        edit_done = threading.Event()
        res = {}

        def _place():
            store = self._store()
            fired = []

            def _pause(orig):
                def inner(*a, **kw):
                    if not fired:
                        fired.append(1)
                        reads_done.set()
                        res["edit_in_window"] = edit_done.wait(self.GRACE)
                    return orig(*a, **kw)
                return inner
            for name in ("add_game", "save_game"):
                setattr(store, name, _pause(getattr(store, name)))
            api = ApiService(store)
            try:
                res["place"] = place(api)
            except Exception as exc:  # surfaced by the caller's asserts
                res["place"] = exc
            finally:
                res["paused"] = bool(fired)
                reads_done.set()  # a refused placement still frees the editor

        def _edit():
            api = ApiService(self._store())
            reads_done.wait(15)
            try:
                res["edit"] = edit(api)
            except Exception as exc:
                res["edit"] = exc
            edit_done.set()

        tp = threading.Thread(target=_place)
        te = threading.Thread(target=_edit)
        tp.start()
        te.start()
        tp.join()
        te.join()
        self.assertFalse(isinstance(res["place"], Exception), res)
        self.assertFalse(isinstance(res["edit"], Exception), res)
        return (res["place"], res["edit"],
                res.get("edit_in_window", False), res["paused"])

    def _assert_forced_serialized(self, place_res, edit_res, edit_in_window,
                                  paused, refusal_reason):
        self.assertTrue(paused, (place_res, edit_res))
        self.assertNotIn("error", edit_res, (place_res, edit_res))
        if edit_in_window:
            # The edit committed while the placement's policy reads were
            # already behind it — the only consistent continuation is the
            # placement enforcing the NEW policy. Unserialized code lands
            # here WITH a placed game (its transaction never saw the row it
            # raced) and fails; serialized code never lands here at all,
            # because the edit blocks on the scope row until the placement
            # commits.
            self.assertIn("error", place_res, (place_res, edit_res))
            self.assertEqual(_reason(place_res), refusal_reason, place_res)
        else:
            self.assertNotIn("error", place_res, (place_res, edit_res))

    def test_create_vs_program_policy_set_forced(self):
        place, edit, in_window, paused = self._forced(
            lambda a: a.create_game("se1", "d1", "t0", "t1", "sC",
                                    league_id="lg"),
            lambda a: a.set_scheduling_policy(
                scope_type="program", scope_id="pg",
                min_playable_minutes=999, actor_id="admin"))
        self._assert_forced_serialized(place, edit, in_window, paused,
                                       "insufficient_playable_time")
        games = [g for g in self._store().all_games() if not g.cancelled]
        if isinstance(place, dict) and "error" in place:
            self.assertEqual(games, [], place)
        else:
            self.assertEqual(len(games), 1, place)

    def test_create_vs_neighbor_season_policy_set_forced(self):
        # The directional gate reads the NEIGHBOR game's own scopes, so an
        # edit to the neighbor's Season must serialize with a candidate
        # from ANOTHER season (#318 review round 2): the se2 placement's
        # lock plan includes se1 — the season of the game already on the
        # rink — precisely so this edit blocks.
        api0 = ApiService(self._store())
        g = api0.create_game("se1", "d1", "t0", "t1", "sA", league_id="lg")
        self.assertNotIn("error", g, g)
        place, edit, in_window, paused = self._forced(
            lambda a: a.create_game("se2", "d2", "u0", "u1", "sB",
                                    league_id="lg"),
            lambda a: a.set_scheduling_policy(
                scope_type="season", scope_id="se1",
                resurfacing_minutes=15, actor_id="admin"))
        self._assert_forced_serialized(place, edit, in_window, paused,
                                       "turnover_buffer_conflict")

    def test_move_vs_program_policy_set_forced(self):
        api0 = ApiService(self._store())
        g = api0.create_game("se1", "d1", "t0", "t1", "sA", league_id="lg")
        self.assertNotIn("error", g, g)
        place, edit, in_window, paused = self._forced(
            lambda a: a.move_game(g["id"], "sC"),
            lambda a: a.set_scheduling_policy(
                scope_type="program", scope_id="pg",
                min_playable_minutes=999, actor_id="admin"))
        self._assert_forced_serialized(place, edit, in_window, paused,
                                       "insufficient_playable_time")

    def test_draft_commit_vs_program_policy_set_forced(self):
        place, edit, in_window, paused = self._forced(
            lambda a: commit_fresh_draft(a, "d2", slot_ids=["sC"]),
            lambda a: a.set_scheduling_policy(
                scope_type="program", scope_id="pg",
                min_playable_minutes=999, actor_id="admin"))
        self._assert_forced_serialized(place, edit, in_window, paused,
                                       "insufficient_playable_time")
        games = [g for g in self._store().all_games() if not g.cancelled]
        if isinstance(place, dict) and "error" in place:
            self.assertEqual(games, [], place)
        else:
            self.assertEqual([g.ice_slot_id for g in games], ["sC"], place)

    def test_create_vs_program_policy_clear(self):
        # Barrier race (both orderings legitimate, run-to-run): clear-first
        # -> the create passes; create-first -> refused under the still-set
        # policy. Either way both complete and the outcome pair is one of
        # the two serializations.
        api0 = ApiService(self._store())
        r = api0.set_scheduling_policy(
            scope_type="program", scope_id="pg", min_playable_minutes=999,
            actor_id="admin")
        self.assertNotIn("error", r, r)
        out = self._run([
            lambda a: a.create_game("se1", "d1", "t0", "t1", "sC",
                                    league_id="lg"),
            lambda a: a.set_scheduling_policy(
                scope_type="program", scope_id="pg", actor_id="admin"),
        ])
        self.assertNotIn("error", out[1], out)
        store = self._store()
        games = [g for g in store.all_games() if not g.cancelled]
        if isinstance(out[0], dict) and "error" not in out[0]:
            self.assertEqual(len(games), 1, out)
        else:
            self.assertEqual(_reason(out[0]),
                             "insufficient_playable_time", out)
            self.assertEqual(games, [], out)

    def test_builder_commit_vs_create_lock_order_forced(self):
        # Deterministic Program-first ordering pin against the
        # ice-availability builder (round 1's Program-LAST placement chain
        # ABBA-deadlocked it; the barrier-start builder races in
        # test_placement_concurrency catch an inversion only sometimes).
        # The builder pauses BETWEEN its Program lock and its Rink lock
        # and wakes the placement: correctly ordered code parks the
        # placement on the Program row for the whole window, so both
        # serialize cleanly; an inverted placement instead acquires
        # Rink+Season inside the window and then requests the Program —
        # the cycle Postgres aborts, which the no-deadlock asserts below
        # turn into a deterministic failure.
        tmpl = dict(season_id="se1", rink_ids=["r1"], weekdays=[0],
                    start_local="12:00", end_local="13:00",
                    start_date="2026-01-05", end_date="2026-01-05",
                    playable_minutes=60, turnover_minutes=0)
        fp = ApiService(self._store()).preview_ice_availability(
            actor_id="b", **tmpl)["template_fingerprint"]
        prog_locked = threading.Event()
        place_done = threading.Event()
        res = {}

        def _builder():
            store = self._store()
            real = store.get_rink_for_update

            def hooked(rid):
                if not prog_locked.is_set():
                    prog_locked.set()            # Program row held NOW
                    place_done.wait(self.GRACE)  # the placement's window
                return real(rid)
            store.get_rink_for_update = hooked
            api = ApiService(store)
            try:
                res["builder"] = api.commit_ice_availability(
                    actor_id="b", template_fingerprint=fp, **tmpl)
            except Exception as exc:
                res["builder"] = exc
            finally:
                prog_locked.set()

        def _place():
            api = ApiService(self._store())
            prog_locked.wait(15)
            try:
                res["place"] = api.create_game("se1", "d1", "t0", "t1",
                                               "sA", league_id="lg")
            except Exception as exc:
                res["place"] = exc
            place_done.set()

        tb = threading.Thread(target=_builder)
        tp = threading.Thread(target=_place)
        tb.start()
        tp.start()
        tb.join()
        tp.join()
        self.assertFalse(isinstance(res["builder"], Exception), res)
        self.assertFalse(isinstance(res["place"], Exception), res)
        for r in (res["builder"], res["place"]):
            self.assertNotEqual(_reason(r), "deadlock_detected", res)
        self.assertNotIn("error", res["builder"], res)
        self.assertNotIn("error", res["place"], res)

    def test_mixed_scope_edit_storm_is_deadlock_free(self):
        # Placements + policy edits at all three scopes at once: every call
        # completes and none is aborted by the database's deadlock detector
        # (the builder-vs-placement half of the ordering is stormed by
        # test_placement_concurrency's _builder_vs races).
        out = self._run([
            lambda a: a.create_game("se1", "d1", "t0", "t1", "sA",
                                    league_id="lg"),
            lambda a: a.create_game("se2", "d2", "u0", "u1", "sB",
                                    league_id="lg"),
            lambda a: a.set_scheduling_policy(
                scope_type="program", scope_id="pg", warmup_minutes=1,
                actor_id="admin"),
            lambda a: a.set_scheduling_policy(
                scope_type="season", scope_id="se1", resurfacing_minutes=1,
                actor_id="admin"),
            lambda a: a.set_scheduling_policy(
                scope_type="rink", scope_id="r1", curfew_local="23:59",
                actor_id="admin"),
        ])
        for r in out:
            self.assertNotEqual(_reason(r), "deadlock_detected", out)
        for r in out[2:]:
            self.assertNotIn("error", r, out)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresPolicyScopeDeleteRaceTest(_PolicyPgRaceHarness,
                                        unittest.TestCase):
    """#318 review — a Program/Rink policy set racing that scope's delete
    must never strand an unreachable orphan row: the delete row-locks the
    scope FIRST and holds it through the cascade, so either the set commits
    first (the cascade removes the row) or the delete commits first (the
    set fails policy_scope_missing). FORCED here like the placement races:
    the deleter's own store pauses right AFTER the cascade's policy-row
    read — the scope row lock held, the cascade's view of "what to delete"
    frozen — and wakes the setter. Serialized code makes the setter block
    until the delete commits; unserialized code lets it insert a row the
    already-past cascade can no longer see, and the zero-rows assertion
    fails on exactly that orphan."""

    def _forced_delete(self, scope_type, scope_id, delete):
        reads_done = threading.Event()
        set_done = threading.Event()
        res = {}

        def _del():
            store = self._store()
            orig = store.find_scheduling_policy
            fired = []

            def hooked(st, sid):
                row = orig(st, sid)
                if (not fired
                        and getattr(st, "value", st) == scope_type
                        and sid == scope_id):
                    fired.append(1)
                    reads_done.set()
                    res["set_in_window"] = set_done.wait(self.GRACE)
                return row
            store.find_scheduling_policy = hooked
            api = ApiService(store)
            try:
                res["delete"] = delete(api)
            except Exception as exc:  # surfaced by the caller's asserts
                res["delete"] = exc
            finally:
                res["paused"] = bool(fired)
                reads_done.set()

        def _set():
            api = ApiService(self._store())
            reads_done.wait(15)
            try:
                res["set"] = api.set_scheduling_policy(
                    scope_type=scope_type, scope_id=scope_id,
                    warmup_minutes=5, actor_id="admin")
            except Exception as exc:
                res["set"] = exc
            set_done.set()

        td = threading.Thread(target=_del)
        ts = threading.Thread(target=_set)
        td.start()
        ts.start()
        td.join()
        ts.join()
        self.assertFalse(isinstance(res["delete"], Exception), res)
        self.assertFalse(isinstance(res["set"], Exception), res)
        return res

    def _assert_no_orphan(self, res, scope_type, scope_id):
        store = self._store()
        self.assertEqual(
            [p for p in store.all_scheduling_policies()
             if getattr(p.scope_type, "value", p.scope_type) == scope_type
             and p.scope_id == scope_id],
            [], res)
        self.assertNotIn("error", res["delete"], res)
        self.assertTrue(res["paused"], res)
        if res.get("set_in_window"):
            # Only an unserialized delete lets the set finish mid-cascade —
            # and then the zero-rows assertion above has already caught the
            # orphan. Serialized code holds the setter until the scope is
            # gone, so it must have failed policy_scope_missing.
            self.assertEqual(_reason(res["set"]), "policy_scope_missing",
                             res)
        elif isinstance(res["set"], dict) and "error" in res["set"]:
            self.assertEqual(_reason(res["set"]), "policy_scope_missing",
                             res)

    def test_program_policy_set_vs_delete_forced(self):
        api0 = ApiService(self._store())
        pg2 = api0.create_program("Doomed", actor_id="admin")
        pid = pg2["id"]
        res = self._forced_delete(
            "program", pid,
            lambda a: a.delete_program(pid, actor_id="admin"))
        self.assertIsNone(self._store().get_program(pid))
        self._assert_no_orphan(res, "program", pid)

    def test_rink_policy_set_vs_delete_forced(self):
        api0 = ApiService(self._store())
        rk = api0.create_rink("v", "Doomed Rink", actor_id="admin")
        rid = rk["id"]
        res = self._forced_delete(
            "rink", rid,
            lambda a: a.delete_rink(rid, actor_id="admin"))
        self.assertIsNone(self._store().get_rink(rid))
        self._assert_no_orphan(res, "rink", rid)


if __name__ == "__main__":
    unittest.main()
