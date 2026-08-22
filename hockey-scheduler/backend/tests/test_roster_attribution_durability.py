"""#205 blocker 5, ROUND 2 — DURABLE game-side attribution on a roster row.

THE DEFECT THIS FILE PINS (owner comment 5370391045, exact-head review).
Round 1 moved ``_side_data`` off the permanent ``player.team_id`` pointer and
onto the game-scoped membership resolution. That corrected the pointer-THIRD/
current-HOME happy path but left the blocker open in a second direction: the
resolver re-derived the side of EVERY historical/accepted roster row from the
player's *current* eligible membership, on every read. A legitimate membership
status change therefore ERASED a seated row's game-side attribution without
removing or transitioning the row.

The owner's exact repro, reproduced RED on Memory/SQLite/PostgreSQL at head
580a09f using facade membership mutation only:

    HOME target_skaters=1; P1 has permanent pointer THIRD and an ACTIVE HOME
    membership; enroll -> offer -> accept P1 (HOME reads full: open=0,
    confirmed=1, roster_confirmed); then
    set_season_roster_membership_status(P1, 'inactive').

    => get_roster_status flipped to open_skater_slots=1 / confirmed_skaters=0
       / status='draft' ("No players selected yet."), while P1's
       GameRosterEntry was still ACCEPTED with occupies_slot=True.
    => P2's offer AND accept then BOTH succeeded, leaving TWO occupying
       roster rows against the one-skater target.

THE FIX (owner ruling, 2026-08-22: durable attribution, NOT an atomic entry
transition). ``GameRosterEntry`` now carries ``team_side`` and
``seated_position`` — the side and bucket the row was seated against, written
at every creation/re-seat site from the SAME validated
``GameMembershipContext`` that authorized the seating (migration 061). Slot
enforcement (``_require_open_slot`` via ``_slot_summaries``) and reporting
(``compute_roster_status``/``_derive_status``) both read that ONE durable
value, so an accepted row can never be simultaneously occupying in storage and
absent from the governed count.

WHAT REMAINS LIVE, deliberately. Membership resolution still decides
ELIGIBILITY (who may enroll, be offered, accept, or re-confirm) and still
decides which SUBSTITUTE ENROLLMENTS belong to a side — an enrollment is a
live candidacy, not a seating. Only the attribution of an already-seated body
became durable.

TWO ASSERTION FAMILIES, and why the split is not a gap. Seven of the eight
lifecycle shapes end ONE player's participation, so the other candidates still
resolve and the second seating attempt is refused by the SLOT gate
(``slot_already_filled``). ``registration_loss`` ends the whole HOME side's
participation, so EVERY candidate stops resolving and the refusal comes from
the ELIGIBILITY gate (``not_eligible``) — reached before the slot gate. That
shape therefore proves the REPORTING half (the slot must not reopen) plus a
refusal, not a slot-gate refusal. Both are asserted; the expected code is
carried per shape in :data:`SHAPES` so neither can silently become the other.

TRI-STORE, PROVEN. ``_stores`` yields Memory, SQLite and — when
TEST_DATABASE_URL is set — real PostgreSQL; ``_assert_backend`` proves each
one rather than trusting the env var, and ``_assert_matrix_ran`` fails if any
configured backend or any shape did not actually execute. A SKIP IS NOT A
PASS.
"""

import os
import unittest
from typing import NamedTuple, Optional

from helpers import BACKEND, FakeClock  # noqa: F401  (sets up sys.path)
from helpers import end_membership_directly, fresh_sql_store

from test_slot_overfill_regression import _OverfillFixture
from test_substitute_membership_cutover import ADMIN

from hockey_scheduler.domain import Position, SlotType
from hockey_scheduler.store import InMemoryStore, SqlStore


class Shape(NamedTuple):
    """One membership-lifecycle end-state, and how it is reached."""
    name: str
    start: str            # P1's membership status at seating time
    how: str              # "facade" | "terminal" | "registration"
    target: Optional[str]
    refusal: str          # the error code a second seating must fail with
    note: str


SHAPES = (
    Shape("active_to_inactive", "active", "facade", "inactive",
          "slot_already_filled", "the owner's exact repro"),
    Shape("active_to_injured", "active", "facade", "injured",
          "slot_already_filled", "injury is not a release"),
    Shape("active_to_applicant", "active", "facade", "applicant",
          "slot_already_filled",
          "facade-reachable and in the same defect class; not in the "
          "owner's list, added because it is one status value away"),
    Shape("affiliate_to_inactive", "affiliate", "facade", "inactive",
          "slot_already_filled", "the governed call-up, ended"),
    Shape("affiliate_to_injured", "affiliate", "facade", "injured",
          "slot_already_filled", "the governed call-up, injured"),
    Shape("active_to_released", "active", "terminal", "released",
          "slot_already_filled",
          "terminal: the facade refuses this unconditionally (#205 round 2 "
          "owner ruling), so it is constructed at the store"),
    Shape("active_to_transferred", "active", "terminal", "transferred",
          "slot_already_filled", "terminal, as above"),
    Shape("registration_loss", "active", "registration", None,
          "not_eligible",
          "the whole HOME side stops participating, so the ELIGIBILITY gate "
          "refuses before the slot gate is reached — see the module "
          "docstring's two-families note"),
)

_PG_SKIP = (
    "PostgreSQL not configured (TEST_DATABASE_URL) — #205 blocker 5's "
    "DURABLE ROSTER ATTRIBUTION was NOT exercised on PostgreSQL. A SKIP IS "
    "NOT A PASS: team_side/seated_position are real nullable columns here "
    "(migration 061) and the slot arithmetic reads them through real SQL. "
    "Set TEST_DATABASE_URL (run_parallel.py --postgres does).")


class _DurabilityHarness(_OverfillFixture):
    """Fixture + assertions written ONCE and invoked by every backend."""

    # -- stores ----------------------------------------------------------
    def _stores(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            yield "postgres", fresh_sql_store(url)

    def _assert_backend(self, label, store):
        """PROVE the backend, never assume it. ``skipUnless`` on the env var
        proves only that a URL was SET — the exact vacuous-coverage failure
        this PR is already under review for."""
        if label == "postgres":
            self.assertIsInstance(store, SqlStore, label)
            self.assertEqual(store.backend, "postgres", store.backend)
        elif label == "sqlite":
            self.assertIsInstance(store, SqlStore, label)
            self.assertEqual(store.backend, "sqlite", store.backend)
        else:
            self.assertIsInstance(store, InMemoryStore, label)

    def _close(self, label, store):
        if isinstance(store, SqlStore):
            if label == "postgres":
                store.reset_schema()
            store.close()

    def _assert_matrix_ran(self, ran, shapes=SHAPES):
        backends = {b for b, _s in ran}
        expected = {"memory", "sqlite"}
        if os.environ.get("TEST_DATABASE_URL"):
            expected.add("postgres")
        else:
            print("\n[DURABLE ATTRIBUTION MATRIX] " + _PG_SKIP)
        self.assertEqual(backends, expected, sorted(backends))
        for backend in expected:
            names = {s for b, s in ran if b == backend}
            self.assertEqual(names, {s.name for s in shapes},
                             (backend, sorted(names)))

    # -- write surfaces --------------------------------------------------
    def _writes(self, api, game_id):
        """ALL FOUR write classes the owner names, as comparable IDENTITY
        values (never bare counts — a count is satisfied by a same-cardinality
        row SWAP, precisely the write a refused path must not perform)."""
        store = api.store
        return {
            "substitutes": sorted(
                (s.id, s.player_id, s.status.value, s.team_id or "")
                for s in store.substitutes_for_game(game_id)),
            "roster": sorted(
                (e.id, e.player_id, e.status.value,
                 e.team_side or "", getattr(e.seated_position, "value", ""))
                for e in store.roster_for_game(game_id)),
            "audit": sorted(
                (a.id, a.action.value) for a in store.audit_for_game(game_id)),
            "setup_audit": sorted(
                (a.id, a.action, a.entity_type, a.entity_id)
                for a in store.all_setup_audit()),
            "notification_events": sorted(
                (n.id, n.type.value)
                for n in store.notifications_for_game(game_id)),
            "notification_feed": sorted(
                (n.id, n.kind.value, n.audience_ref or "")
                for n in store.all_notifications_feed()),
            "deliveries": sorted(d.id for d in
                                 store.all_notification_deliveries()),
        }

    # -- fixture ---------------------------------------------------------
    def _fixture(self, store, shape, target_skaters=1, seat=True):
        """HOME ``target_skaters=1``; P1 seated through the real
        enroll -> offer -> accept arc; P2 holding an OUTSTANDING OFFER made
        while the slot was still open (so ``accept``'s own gate and the
        Coach-add gate can each be exercised against a full slot rather than
        bouncing off "no active offer"); P3 merely ENROLLED (so the OFFER
        gate can be exercised without the enrollment itself being a write
        that lands after the snapshot)."""
        api, season, league, teams, game, ls_id = self._build(
            store, target_skaters=target_skaters, target_goalies=0)
        home, third = teams["home"]["id"], teams["third"]["id"]
        gid = game["id"]

        # Every candidate is the "Mover" shape round 1 established: permanent
        # pointer THIRD, seasonal record HOME. The permanent pointer must not
        # govern, in either direction.
        p1 = self._pointer_only_player(api, third, "Seated")
        self._membership(api, p1["id"], ls_id, home, status=shape.start)
        p2 = self._pointer_only_player(api, third, "Offered")
        self._membership(api, p2["id"], ls_id, home)
        p3 = self._pointer_only_player(api, third, "Enrolled")
        self._membership(api, p3["id"], ls_id, home)

        for p in (p1, p2, p3):
            r = api.enroll_substitute(gid, p["id"])
            assert "error" not in r, (p["name"], r)
        # P2's offer is made while the slot is genuinely open.
        r = api.offer_substitute(gid, p2["id"])
        assert "error" not in r, r
        r = api.offer_substitute(gid, p1["id"])
        assert "error" not in r, r
        if seat:
            r = api.accept_substitute(gid, p1["id"])
            assert "error" not in r, r
        return {"api": api, "season": season, "league": league,
                "teams": teams, "game": game, "gid": gid, "ls_id": ls_id,
                "home": home, "away": teams["away"]["id"], "third": third,
                "p1": p1, "p2": p2, "p3": p3}

    def _cases(self, shapes=SHAPES, seat=True, target_skaters=1):
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for shape in shapes:
                    store.clear_all_data()
                    yield label, shape, self._fixture(
                        store, shape, target_skaters=target_skaters,
                        seat=seat)
            finally:
                self._close(label, store)

    # -- the lifecycle mutation ------------------------------------------
    def _end_participation(self, fx, shape):
        api, ls_id = fx["api"], fx["ls_id"]
        if shape.how == "registration":
            (reg,) = api.store.registrations_for_team_in_league_season(
                ls_id, fx["home"])
            reg.active = False
            api.store.save_season_team_registration(reg)
            return
        mid = self._stint_id(api, fx["p1"]["id"], ls_id)
        if shape.how == "terminal":
            # The facade refuses EVERY terminal transition unconditionally
            # (#205 round 2 owner ruling) — assert that refusal here rather
            # than route around it silently, then construct the terminal
            # PRECONDITION at the store, the pattern the Slice A tests use.
            res = api.set_season_roster_membership_status(
                mid, shape.target, actor_id=ADMIN)
            self.assertEqual(res["error"]["code"], "forbidden", res)
            self.assertEqual(res["error"]["details"]["reason"],
                             "terminal_transition_not_authorized", res)
            end_membership_directly(api.store, mid, shape.target)
            return
        res = api.set_season_roster_membership_status(
            mid, shape.target, actor_id=ADMIN)
        self.assertNotIn("error", res, res)
        self.assertEqual(res["status"], shape.target, res)

    # -- shared assertions -----------------------------------------------
    def _assert_seated_and_full(self, fx, label):
        api, gid, home = fx["api"], fx["gid"], fx["home"]
        # ApiService.get_roster_status takes no team_id (it always answers
        # the HOME default); the per-side read is the service call the
        # facade wraps, which is the unit under test here anyway.
        st = api.roster.compute_roster_status(gid, home).to_dict()
        self.assertEqual(st["open_skater_slots"], 0, (label, st))
        self.assertEqual(st["confirmed_skaters"], 1, (label, st))
        self.assertEqual(st["status"], "roster_confirmed", (label, st))
        entry = api.store.roster_entry_for_player(gid, fx["p1"]["id"])
        self.assertEqual(entry.status.value, "accepted", label)
        self.assertTrue(entry.status.occupies_slot, label)
        # The durable attribution itself — written from the context that
        # gated the accept, NOT from the permanent pointer (which is THIRD).
        self.assertEqual(entry.team_side, home, label)
        self.assertEqual(entry.seated_position, Position.FORWARD, label)
        self.assertEqual(entry.seated_slot_type, SlotType.SKATER, label)
        return st


# ======================================================================
# 1. the slot does not reopen when participation ends
# ======================================================================
class DurableAttributionSurvivesTheEndOfParticipation(
        _DurabilityHarness, unittest.TestCase):
    """THE owner's correction, asserted directly: a seated row keeps its
    slot when the membership that once justified it stops granting
    participation.

    RED at head 580a09f, identically on Memory/SQLite/PostgreSQL, for all
    eight shapes: ``open_skater_slots`` 0 -> 1, ``confirmed_skaters`` 1 ->
    0, ``status`` roster_confirmed -> draft ("No players selected yet."),
    while the GameRosterEntry was still ACCEPTED and occupies_slot=True."""

    def test_the_slot_does_not_reopen_when_participation_ends(self):
        ran = []
        for label, shape, fx in self._cases():
            with self.subTest(backend=label, shape=shape.name):
                api, gid, home = fx["api"], fx["gid"], fx["home"]
                self._assert_seated_and_full(fx, label)

                self._end_participation(fx, shape)

                after = api.roster.compute_roster_status(gid, home).to_dict()
                self.assertEqual(after["open_skater_slots"], 0,
                                 (label, shape.name, after))
                self.assertEqual(after["confirmed_skaters"], 1,
                                 (label, shape.name, after))
                self.assertEqual(after["status"], "roster_confirmed",
                                 (label, shape.name, after))

                # DURABLE, not transitioned: the row is byte-for-byte the
                # row it was. The owner ruled between the two sanctioned
                # shapes and chose attribution over an atomic entry
                # transition, so a status change here would be the WRONG
                # fix even though it would also close the overfill.
                entry = api.store.roster_entry_for_player(gid, fx["p1"]["id"])
                self.assertEqual(entry.status.value, "accepted",
                                 (label, shape.name))
                self.assertTrue(entry.status.occupies_slot,
                                (label, shape.name))
                self.assertEqual(entry.team_side, home, (label, shape.name))
                self.assertEqual(entry.seated_slot_type, SlotType.SKATER,
                                 (label, shape.name))
            ran.append((label, shape.name))
        self._assert_matrix_ran(ran)

    def test_the_opposing_side_never_inherits_the_seated_row(self):
        """The durable value names ONE side. A row seated on HOME must not
        start counting against AWAY when HOME stops resolving — the
        fail-closed NULL rule charges every side, and this pins that a row
        WITH attribution is emphatically not treated that way."""
        ran = []
        for label, shape, fx in self._cases():
            with self.subTest(backend=label, shape=shape.name):
                api, gid = fx["api"], fx["gid"]
                self._end_participation(fx, shape)
                away = api.roster.compute_roster_status(
                    gid, fx["away"]).to_dict()
                self.assertEqual(away["open_skater_slots"], 1,
                                 (label, shape.name, away))
                self.assertEqual(away["confirmed_skaters"], 0,
                                 (label, shape.name, away))
            ran.append((label, shape.name))
        self._assert_matrix_ran(ran)


# ======================================================================
# 2. no second seat can overfill
# ======================================================================
class NoSecondSeatCanOverfillAfterParticipationEnds(
        _DurabilityHarness, unittest.TestCase):
    """Offer, accept AND Coach-add are each refused against the still-held
    slot, and each leaves the store byte-identical across all four write
    classes the owner names.

    RED at head 580a09f: P2's offer and accept BOTH succeeded, leaving two
    occupying rows against target_skaters=1."""

    def test_offer_accept_and_coach_add_are_refused_with_zero_writes(self):
        ran = []
        for label, shape, fx in self._cases():
            with self.subTest(backend=label, shape=shape.name):
                api, gid = fx["api"], fx["gid"]
                self._assert_seated_and_full(fx, label)
                self._end_participation(fx, shape)

                w0 = self._writes(api, gid)
                where = (label, shape.name)

                # (a) OFFER a still-enrolled candidate.
                o = api.offer_substitute(gid, fx["p3"]["id"])
                self.assertEqual(o["error"]["code"], shape.refusal, (where, o))
                self.assertEqual(self._writes(api, gid), w0, where)

                # (b) ACCEPT an offer made while the slot was still open —
                # the accept gate in its own right, not a bounce off "no
                # active offer to accept".
                a = api.accept_substitute(gid, fx["p2"]["id"])
                self.assertEqual(a["error"]["code"], shape.refusal, (where, a))
                self.assertEqual(self._writes(api, gid), w0, where)

                # (c) COACH-ADD (offer + accept in one step, the override
                # surface). Coach override wins over policy, never over
                # arithmetic.
                c = api.add_substitute_to_roster(gid, fx["p3"]["id"])
                self.assertEqual(c["error"]["code"], shape.refusal, (where, c))
                self.assertEqual(self._writes(api, gid), w0, where)

                # And the arithmetic itself: ONE body against a target of 1.
                occupying = sorted(
                    e.player_id for e in api.store.roster_for_game(gid)
                    if e.status.occupies_slot)
                self.assertEqual(occupying, [fx["p1"]["id"]], where)
            ran.append((label, shape.name))
        self._assert_matrix_ran(ran)


# ======================================================================
# 3. both commit orders
# ======================================================================
class MembershipChangeAndAcceptInBothCommitOrders(
        _DurabilityHarness, unittest.TestCase):
    """The owner asks for membership-change versus accept in BOTH orders.
    They are governed by DIFFERENT rules and both must hold:

    * accept THEN change  -> the seating is DURABLE; the slot stays held
      (this is the blocker).
    * change THEN accept  -> ELIGIBILITY is LIVE; the accept is refused and
      seats nobody, so the slot stays open and honestly reports as open.

    Neither order may end with two bodies in one slot."""

    def test_accept_then_membership_change_keeps_the_slot_held(self):
        ran = []
        for label, shape, fx in self._cases():
            with self.subTest(backend=label, shape=shape.name):
                api, gid, home = fx["api"], fx["gid"], fx["home"]
                # accept already happened in _fixture
                self._assert_seated_and_full(fx, label)
                self._end_participation(fx, shape)
                st = api.roster.compute_roster_status(gid, home).to_dict()
                self.assertEqual(st["open_skater_slots"], 0,
                                 (label, shape.name, st))
                occupying = [e.player_id
                             for e in api.store.roster_for_game(gid)
                             if e.status.occupies_slot]
                self.assertEqual(occupying, [fx["p1"]["id"]],
                                 (label, shape.name))
            ran.append((label, shape.name))
        self._assert_matrix_ran(ran)

    def test_membership_change_then_accept_is_refused_and_seats_nobody(self):
        ran = []
        # seat=False builds the SAME fixture stopped one step earlier: P1
        # holds an OFFER and has NOT accepted. Built that way rather than by
        # deleting the accepted row afterwards, so the arrangement under
        # test is one the production code actually produced.
        for label, shape, fx in self._cases(seat=False):
            with self.subTest(backend=label, shape=shape.name):
                api, gid, home = fx["api"], fx["gid"], fx["home"]
                self.assertIsNone(
                    api.store.roster_entry_for_player(gid, fx["p1"]["id"]),
                    (label, shape.name))
                st = api.roster.compute_roster_status(gid, home).to_dict()
                self.assertEqual(st["open_skater_slots"], 1,
                                 (label, shape.name, st))

                self._end_participation(fx, shape)

                res = api.accept_substitute(gid, fx["p1"]["id"])
                self.assertEqual(res["error"]["code"], "not_eligible",
                                 (label, shape.name, res))
                self.assertIsNone(
                    api.store.roster_entry_for_player(gid, fx["p1"]["id"]),
                    (label, shape.name))
                after = api.roster.compute_roster_status(gid, home).to_dict()
                self.assertEqual(after["open_skater_slots"], 1,
                                 (label, shape.name, after))
            ran.append((label, shape.name))
        self._assert_matrix_ran(ran)


# ======================================================================
# 4. NULL attribution (pre-061 rows) fails closed
# ======================================================================
class LegacyRowsWithNoAttributionFailClosed(
        _DurabilityHarness, unittest.TestCase):
    """Rows written BEFORE migration 061 carry no durable attribution, and
    the owner ruled that they must FAIL CLOSED: never guessed at, and never
    resolved by a live lookup that could name the opposite team.

    THE IMPLEMENTED SEMANTICS. Such a row is charged as occupying on EVERY
    side of its game and in BOTH slot buckets, for as long as its status
    occupies a slot. It names no side, consults no live state at all, and
    can only ever REDUCE an open count — so it cannot reopen a slot and
    therefore cannot admit overfill. The deliberate, accepted cost is
    OVER-refusal, asserted below so nobody mistakes it for an accident.

    Constructed by NULLing the columns on a genuinely-seated row at the
    store, which is exactly the on-disk shape migration 061 leaves behind
    (it deliberately performs no backfill — see its header for why no
    honest backfill value exists)."""

    def _legacy(self, api, gid, player_id):
        entry = api.store.roster_entry_for_player(gid, player_id)
        entry.team_side = None
        entry.seated_position = None
        api.store.save_roster_entry(entry)
        reread = api.store.roster_entry_for_player(gid, player_id)
        # The NULL must survive the round trip on a real database, or this
        # whole class would be testing an in-process object.
        self.assertIsNone(reread.team_side)
        self.assertIsNone(reread.seated_position)
        self.assertIsNone(reread.attribution)
        return reread

    def test_a_null_row_holds_its_slot_on_every_side(self):
        ran = []
        for label, shape, fx in self._cases(shapes=SHAPES[:1],
                                            target_skaters=1):
            with self.subTest(backend=label):
                api, gid = fx["api"], fx["gid"]
                self._legacy(api, gid, fx["p1"]["id"])
                home = api.roster.compute_roster_status(gid,
                                                        fx["home"]).to_dict()
                away = api.roster.compute_roster_status(gid,
                                                        fx["away"]).to_dict()
                # Still held on the side it really was on...
                self.assertEqual(home["open_skater_slots"], 0, (label, home))
                # ...and ALSO held on the opposing side. That is the
                # over-refusal, and it is the price of never guessing.
                self.assertEqual(away["open_skater_slots"], 0, (label, away))
            ran.append((label, shape.name))
        self._assert_matrix_ran(ran, shapes=SHAPES[:1])

    def test_a_null_row_charges_both_slot_buckets(self):
        """A SKATER's legacy row holds the GOALIE slot too.

        This is the over-refusal, stated as an assertion so it can never be
        mistaken for an accident: the row's bucket is as unknown as its
        side, and the implementation resolves that by consulting NOTHING —
        not the membership, not the permanent pointer, not even
        ``Player.position``, any of which would be a guess about what the
        row occupies. Needs a game with BOTH targets non-zero, which the
        shared fixture (target_goalies=0) cannot show."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, season, league, teams, game, ls_id = self._build(
                    store, target_skaters=1, target_goalies=1)
                gid, home = game["id"], teams["home"]["id"]
                skater = self._pointer_only_player(api, teams["third"]["id"],
                                                   "Legacy Skater")
                self._membership(api, skater["id"], ls_id, home)
                self.assertNotIn("error", api.enroll_substitute(
                    gid, skater["id"]))
                self.assertNotIn("error", api.offer_substitute(
                    gid, skater["id"]))
                self.assertNotIn("error", api.accept_substitute(
                    gid, skater["id"]))

                before = api.roster.compute_roster_status(gid, home).to_dict()
                self.assertEqual(before["open_skater_slots"], 0,
                                 (label, before))
                # A skater does NOT hold the goalie slot while its
                # attribution is intact — the durable bucket names one.
                self.assertEqual(before["open_goalie_slots"], 1,
                                 (label, before))

                self._legacy(api, gid, skater["id"])

                after = api.roster.compute_roster_status(gid, home).to_dict()
                self.assertEqual(after["open_skater_slots"], 0, (label, after))
                # ...and once it is NULL it holds BOTH.
                self.assertEqual(after["open_goalie_slots"], 0, (label, after))
                ran.append((label, SHAPES[0].name))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, shapes=SHAPES[:1])

    def test_no_second_seat_can_overfill_against_a_null_row(self):
        ran = []
        for label, shape, fx in self._cases(shapes=SHAPES[:1]):
            with self.subTest(backend=label):
                api, gid = fx["api"], fx["gid"]
                self._legacy(api, gid, fx["p1"]["id"])
                w0 = self._writes(api, gid)
                for call, who in ((api.offer_substitute, fx["p3"]),
                                  (api.accept_substitute, fx["p2"]),
                                  (api.add_substitute_to_roster, fx["p3"])):
                    res = call(gid, who["id"])
                    self.assertEqual(res["error"]["code"],
                                     "slot_already_filled",
                                     (label, call.__name__, res))
                    self.assertEqual(self._writes(api, gid), w0,
                                     (label, call.__name__))
                occupying = sorted(
                    e.player_id for e in api.store.roster_for_game(gid)
                    if e.status.occupies_slot)
                self.assertEqual(occupying, [fx["p1"]["id"]], label)
            ran.append((label, shape.name))
        self._assert_matrix_ran(ran, shapes=SHAPES[:1])

    def test_reconfirm_after_back_out_refuses_on_a_null_row(self):
        """Re-confirming is "take a slot back", and a slot whose side is
        unknown must not be taken. The gate refuses rather than re-deriving
        a side from live membership — the live lookup the owner's ruling
        forbids, which could name the opposing team."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, season, league, teams, game, ls_id = self._build(
                    store, target_skaters=2, target_goalies=0)
                gid, home = game["id"], teams["home"]["id"]
                p = self._player(api, home, "Legacy Backer")
                self.assertNotIn(
                    "error", api.select_roster(gid, [p["id"]],
                                               actor_id=ADMIN))
                self.assertNotIn("error", api.set_availability(
                    gid, p["id"], "available", actor_id=p["id"]))
                self.assertNotIn("error", api.set_availability(
                    gid, p["id"], "unavailable", actor_id=p["id"]))
                entry = api.store.roster_entry_for_player(gid, p["id"])
                self.assertEqual(entry.status.value, "unavailable", label)
                entry.team_side = None
                entry.seated_position = None
                api.store.save_roster_entry(entry)

                res = api.set_availability(gid, p["id"], "available",
                                           actor_id=p["id"])
                self.assertEqual(res["error"]["code"], "not_eligible",
                                 (label, res))
                self.assertIn("durable game-side attribution",
                              res["error"]["message"], (label, res))
                # Refused with nothing half-written: the row is still
                # backed out, not silently re-seated.
                again = api.store.roster_entry_for_player(gid, p["id"])
                self.assertEqual(again.status.value, "unavailable", label)
                ran.append((label, SHAPES[0].name))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, shapes=SHAPES[:1])


# ======================================================================
# 5. _back_out_entry addresses the side that lost the player
# ======================================================================
class BackOutNotifiesTheSeatedSideAfterParticipationEnds(
        _DurabilityHarness, unittest.TestCase):
    """``_back_out_entry`` must alert the side that actually LOST the
    player. It used to re-resolve that side with a live
    ``team_for_game(game, player)`` lookup, so once the membership ended
    the answer was None and the targeted push was suppressed — the HOME
    coach, now genuinely a skater short, heard nothing. It now reads
    ``entry.team_side``.

    (The PostgreSQL PROOF of the sibling #60 crash belongs to its own
    blocker; this pins the BEHAVIOUR on all three stores at the service
    level. See ``BackOutSurvivesLapsedMembership`` in
    test_slot_overfill_regression.py for the adapted round-1 case.)"""

    def test_open_slot_alert_names_the_side_the_row_was_seated_on(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, season, league, teams, game, ls_id = self._build(
                    store, target_skaters=2, target_goalies=0)
                gid, home = game["id"], teams["home"]["id"]
                anchor = self._player(api, home, "Anchor")
                # The Mover shape: permanent pointer THIRD, seasonal HOME.
                mover = self._pointer_only_player(api, teams["third"]["id"],
                                                  "Mover")
                self._membership(api, mover["id"], ls_id, home)
                self.assertNotIn("error", api.select_roster(
                    gid, [anchor["id"], mover["id"]], actor_id=ADMIN))
                for p in (anchor, mover):
                    self.assertNotIn("error", api.set_availability(
                        gid, p["id"], "available", actor_id=p["id"]))
                self.assertEqual(api.roster.compute_roster_status(
                    gid, home).to_dict()["confirmed_skaters"], 2, label)

                # Participation ends AFTER the row was seated.
                mid = self._stint_id(api, mover["id"], ls_id)
                res = api.set_season_roster_membership_status(
                    mid, "inactive", actor_id=ADMIN)
                self.assertNotIn("error", res, (label, res))

                before = [(n.kind.value, n.audience_ref)
                          for n in api.store.all_notifications_feed()]
                out = api.remove_player(gid, mover["id"], actor_id=ADMIN)
                self.assertNotIn("error", out, (label, out))
                after = [(n.kind.value, n.audience_ref)
                         for n in api.store.all_notifications_feed()]
                new = [e for e in after if e not in before]

                # RED at head 580a09f: team_for_game answered None here, so
                # this list was EMPTY and no coach was told.
                self.assertIn(("roster_open_slot", home), new, (label, new))
                self.assertEqual(
                    [e for e in new if e[0] == "roster_open_slot"],
                    [("roster_open_slot", home)], (label, new))
                ran.append((label, SHAPES[0].name))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, shapes=SHAPES[:1])


if __name__ == "__main__":
    unittest.main()
