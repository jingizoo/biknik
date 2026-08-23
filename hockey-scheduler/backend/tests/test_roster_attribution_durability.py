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

WHAT A RE-SEAT DOES, section 6. Sections 1-5 pin that an attribution, once
written, SURVIVES. They say nothing about what happens when the same row is
seated AGAIN, nor about which side a re-confirm asks about — three behaviours
this commit introduces that were measurably pinned by nothing (the product
code was mutated at head 84ecd90 and the full 227-module suite stayed green
each time). Section 6 closes that, and states the rule the three share: a
SEATING decides an attribution and a READ never does.

TRI-STORE, PROVEN. ``_stores`` yields Memory, SQLite and — when
TEST_DATABASE_URL is set — real PostgreSQL; ``_assert_backend`` proves each
one rather than trusting the env var, and ``_assert_matrix_ran`` fails if any
configured backend or any shape did not actually execute. A SKIP IS NOT A
PASS.
"""

import contextlib
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from typing import NamedTuple, Optional

from helpers import BACKEND, FakeClock  # noqa: F401  (sets up sys.path)
from helpers import end_membership_directly, fresh_sql_store

from test_slot_overfill_regression import _OverfillFixture
from test_substitute_membership_cutover import ADMIN

from hockey_scheduler.domain import Position, SlotType
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv
from hockey_scheduler.web.auth import DEMO_PASSWORD, DEMO_USERS


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
        row SWAP, precisely the write a refused path must not perform).

        ``availability`` joined them for the #427 SELECTED->CONFIRMED ruling:
        ``set_availability``'s FIRST write is the GameAvailability upsert, so
        a check that ran after it would leave the roster row untouched and
        still have recorded the player as available. Only a snapshot that
        includes this row can tell "refused before any mutation" apart from
        "refused after the first one"."""
        store = api.store
        return {
            "availability": sorted(
                (a.id, a.player_id, a.availability_status.value,
                 a.response_source, str(a.responded_at))
                for a in store.availability_for_game(game_id)),
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

    # -- "before ANY mutation", as opposed to "nothing survived" ---------
    _WRITE_PREFIXES = ("save_", "add_", "upsert_", "insert_", "update_",
                       "delete_", "remove_", "clear_", "next_id")

    @contextlib.contextmanager
    def _write_attempts(self, store):
        """Record every STORE WRITE METHOD CALLED, whether or not it
        survived.

        WHY A SNAPSHOT DIFF IS NOT ENOUGH HERE, measured rather than
        assumed. ``set_availability`` is ``@_transactional``, so a check
        placed AFTER the ``GameAvailability`` upsert still leaves an
        empty diff -- the raise rolls the transaction back on Memory,
        SQLite and PostgreSQL alike. Moving the identical gate down to
        just before ``_set_entry_status`` was tried in place and the
        ``_writes`` identity assertions all still passed. So the diff
        proves the user-visible property ("nothing survived the refusal")
        and CANNOT prove the ruling's ordering requirement ("perform that
        check before any mutation"). This spy proves the ordering: with
        the gate late, ``next_id``/``upsert_availability``/``add_audit``
        appear here; with the gate first, nothing does.

        Patched on the INSTANCE, restored in ``finally``. The number of
        methods actually wrapped is asserted, so a rename that empties
        the prefix list fails loudly instead of turning this into a spy
        that watches nothing."""
        calls = []
        patched = {}
        for name in dir(type(store)):
            if not name.startswith(self._WRITE_PREFIXES):
                continue
            attr = getattr(store, name, None)
            if not callable(attr):
                continue
            patched[name] = attr

            def _spy(*a, _n=name, _f=attr, **kw):
                calls.append(_n)
                return _f(*a, **kw)

            setattr(store, name, _spy)
        self.assertGreater(len(patched), 20, sorted(patched))
        try:
            yield calls
        finally:
            for name in patched:
                delattr(store, name)

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
                # Its own reason, distinct from the round-3 live-side
                # authorization refusal (``seated_side_not_live_eligible``)
                # and from ``slot_already_filled``: three different
                # conditions, three different answers, never one merged
                # "no" a client cannot act on.
                self.assertEqual(res["error"]["details"]["reason"],
                                 "attribution_missing", (label, res))
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


# ======================================================================
# 6. a RE-SEAT re-attributes the row; a RE-CONFIRM does not
# ======================================================================
# Three behaviours this commit introduces that NOTHING pinned. Each was
# measured unpinned before these tests were written: the product code was
# mutated in place at head 84ecd90 and the FULL suite (227 modules, -j 3,
# Memory/SQLite) stayed green in all three cases.
#
#   * ``select_roster``'s revive branch re-writes ``team_side``/
#     ``seated_position`` from the freshly-validated context.
#   * ``_add_to_roster_entry``'s revive branch re-writes them from the
#     (side, bucket) pair its caller just fed to ``_require_open_slot``.
#   * ``set_availability``'s re-confirm gate reads ``entry.attribution``
#     and NOT a fresh resolution. (The NULL half of that branch is already
#     pinned by ``test_reconfirm_after_back_out_refuses_on_a_null_row``;
#     the durable-versus-live half was not.)
#
# THE RULE THE THREE SHARE, and the reason they split the way they do:
# a SEATING decides an attribution, and a READ never does. Re-seating a
# backed-out row IS a seating -- it is authorized by a context resolved
# right then, and the row must record THAT context, or the slot the gate
# checked and the slot the row is counted in are two different slots.
# Re-confirming is not a seating: it puts THIS ROW back into the slot it
# already held, so it must ask about that slot -- the row's own durable
# value -- even when a live lookup would now answer differently.
#
# ROUND 3 CORRECTION (owner ruling on PR #427, comment 5379885403).
# "Which slot" is only HALF the re-confirm question, and round 2 shipped
# the other half broken: it proved the player still had SOME valid
# context and then gated the ROW's side, so a player whose membership had
# moved HOME->AWAY self-confirmed straight back onto the HOME row. The
# durable value IDENTIFIES the slot; it does not AUTHORIZE anyone to
# re-occupy it. Those are two questions with two different answerers --
# the row answers the first, the LIVE context answers the second -- and
# ``ReconfirmIdentifiesFromTheRowAndAuthorizesFromTheLiveSide`` below
# pins both halves, including the one whose old test asserted the
# forbidden behaviour outright.
#
# WHY THESE ARE NOT VACUOUS. A test that re-seats a player whose live
# resolution still answers the same side cannot tell "re-written from the
# context" apart from "left alone": both leave HOME on the row. So every
# case below moves the player to the OPPOSING side of the SAME game
# between the two seatings (``_move_to_the_other_side``), which makes the
# durable value and the live answer disagree by construction -- and
# asserts that disagreement before exercising anything.


class _Case(NamedTuple):
    """A single-case matrix label for ``_assert_matrix_ran``, which needs
    only ``.name``. These cases do not vary a membership-lifecycle shape
    the way :data:`SHAPES` does -- they vary WHICH SIDE resolves -- so
    borrowing a ``Shape`` name for them would misreport what ran."""
    name: str


RESEAT = (_Case("mover_reseated_on_the_opposing_side"),)
# The re-confirm cases vary WHICH SIDE the live context answers and
# whether the durable slot is free; the bucket cases vary the
# SEASON-SCOPED POSITION after seating. Distinct labels so
# ``_assert_matrix_ran`` reports what actually ran rather than borrowing
# the re-seat case's name.
RECONFIRM = (_Case("reconfirm_after_a_back_out"),)
BUCKET = (_Case("season_position_changed_after_seating"),)


class _ReseatHarness(_DurabilityHarness):
    """The move that makes every case below falsifiable, plus the two-side
    fixture they share. Built on ``_DurabilityHarness`` so the tri-store
    loop, the PROVEN ``store.backend`` check and the loud
    ``_assert_matrix_ran`` are the same ones the rest of this file uses --
    there is exactly one harness here."""

    def _two_sided(self, store, target_skaters=1, target_goalies=0):
        """HOME and AWAY each with ``target_skaters`` skater slots (and
        ``target_goalies`` goalie slots — targets are per-side), and a
        Mover-shaped candidate: permanent pointer THIRD, ACTIVE membership
        on HOME. The pointer names a team that is not a side of this game
        at all, so nothing below can accidentally be reading it."""
        api, season, league, teams, game, ls_id = self._build(
            store, target_skaters=target_skaters,
            target_goalies=target_goalies)
        return {"api": api, "gid": game["id"], "ls_id": ls_id,
                "home": teams["home"]["id"], "away": teams["away"]["id"],
                "third": teams["third"]["id"], "teams": teams}

    def _mover(self, fx, name, side=None, position=None):
        """``_membership``'s create, plus an explicit SEASON-SCOPED
        ``position`` when a case needs a candidate in the GOALIE bucket.
        ``position=None`` is byte-for-byte what ``_membership`` sends, so
        every pre-existing caller is unaffected."""
        pos = Position(position) if position else None
        p = self._pointer_only_player(fx["api"], fx["third"], name,
                                      position=pos)
        m = fx["api"].create_season_roster_membership(
            p["id"], fx["ls_id"], side or fx["home"], status="active",
            position=position, jersey_number=None, actor_id=ADMIN)
        assert "error" not in m, m
        return p

    def _live_side(self, fx, player_id):
        """What a FRESH resolution answers right now -- the value the
        durable one must be provably different from."""
        api = fx["api"]
        ctx = api.roster.resolve_membership_context(
            api.store.get_game(fx["gid"]), api.store.get_player(player_id))
        return None if ctx is None else ctx.team_id

    def _open(self, fx, team_id, bucket="skater"):
        return fx["api"].roster.compute_roster_status(
            fx["gid"], team_id).to_dict()[f"open_{bucket}_slots"]

    def _move_to_the_other_side(self, fx, player_id, to_team, label):
        """End the player's stint terminally and open an ACTIVE one on the
        OPPOSING side of the same game, then PROVE the two answers now
        disagree.

        This is the anti-vacuity device for this whole section, so its
        premise is asserted rather than assumed: after the move the LIVE
        resolution must answer ``to_team`` while the already-seated row
        must still name the side it was seated on. If a future change ever
        made the move a no-op (one-membership-per-season tightened, the
        terminal row still resolving, precedence reordered), these
        assertions fail LOUDLY here instead of quietly turning the three
        regressions below into tautologies.

        The terminal transition is constructed at the STORE
        (``end_membership_directly``) because the facade refuses every
        terminal transition unconditionally until the governed transfer
        slice ships -- the same pattern ``_end_participation`` and the
        Slice A tests already use."""
        api = fx["api"]
        seated_before = api.store.roster_entry_for_player(
            fx["gid"], player_id)
        self.assertIsNotNone(seated_before, label)
        from_team = seated_before.team_side
        self.assertIsNotNone(from_team, label)
        self.assertNotEqual(from_team, to_team, label)

        end_membership_directly(
            api.store, self._stint_id(api, player_id, fx["ls_id"]),
            "transferred")
        m = api.create_season_roster_membership(
            player_id, fx["ls_id"], to_team, status="active", actor_id=ADMIN)
        self.assertNotIn("error", m, (label, m))

        # THE DISAGREEMENT, asserted: live says the new side...
        self.assertEqual(self._live_side(fx, player_id), to_team,
                         (label, "live resolution did not move"))
        # ...while the row still names the old one.
        seated_after = api.store.roster_entry_for_player(fx["gid"], player_id)
        self.assertEqual(seated_after.team_side, from_team,
                         (label, "durable attribution moved on its own"))
        return from_team

    def _each(self, target_skaters=1, target_goalies=0):
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                yield label, self._two_sided(
                    store, target_skaters=target_skaters,
                    target_goalies=target_goalies)
            finally:
                self._close(label, store)


class SelectRosterReviveReAttributesTheRow(_ReseatHarness, unittest.TestCase):
    """``select_roster``'s revive branch (roster_service.py ~L315-329):
    re-selecting a backed-out or coach-removed row is a RE-SEAT, so it
    must record the context that authorized THAT seating -- not the one
    the row happened to be seated with the first time.

    MUTATION-PROVEN: deleting the two attribution writes from that branch
    (so the revived row keeps its original ``team_side``/
    ``seated_position``) leaves the whole 227-module suite green at head
    84ecd90, and fails both tests here."""

    def test_a_backed_out_row_is_re_attributed_when_it_is_re_selected(self):
        ran = []
        for label, fx in self._each():
            with self.subTest(backend=label):
                api, gid = fx["api"], fx["gid"]
                home, away = fx["home"], fx["away"]
                p = self._mover(fx, "Mover")

                self.assertNotIn("error", api.select_roster(
                    gid, [p["id"]], actor_id=ADMIN), label)
                seated = api.store.roster_entry_for_player(gid, p["id"])
                self.assertEqual(seated.team_side, home, label)
                self.assertEqual(seated.seated_position, Position.FORWARD,
                                 label)
                self.assertEqual(self._open(fx, home), 0, label)
                self.assertEqual(self._open(fx, away), 1, label)

                self.assertNotIn("error", api.set_availability(
                    gid, p["id"], "unavailable", actor_id=p["id"]), label)
                out = api.store.roster_entry_for_player(gid, p["id"])
                self.assertEqual(out.status.value, "unavailable", label)
                self.assertFalse(out.status.occupies_slot, label)
                self.assertEqual(self._open(fx, home), 1, label)

                self._move_to_the_other_side(fx, p["id"], away, label)

                self.assertNotIn("error", api.select_roster(
                    gid, [p["id"]], actor_id=ADMIN), label)

                # THE ASSERTION. The re-seat was authorized by a context
                # naming AWAY, so the row names AWAY. Keeping HOME here is
                # the mutation, and it is exactly the shape the blocker is
                # about: gated against one side, counted on another.
                back = api.store.roster_entry_for_player(gid, p["id"])
                self.assertEqual(back.team_side, away, (label, back.team_side))
                self.assertEqual(back.seated_position, Position.FORWARD, label)
                self.assertEqual(back.status.value, "selected", label)

                # ...and the slot arithmetic AGREES with the row, on both
                # sides, so enforcement and reporting are one answer.
                self.assertEqual(self._open(fx, away), 0, label)
                self.assertEqual(self._open(fx, home), 1, label)
            ran.append((label, RESEAT[0].name))
        self._assert_matrix_ran(ran, shapes=RESEAT)

    def test_a_coach_removed_row_is_re_attributed_when_it_is_re_selected(self):
        """The other way into the same branch: ``remove_player`` leaves the
        row REMOVED rather than UNAVAILABLE, and re-selecting it is the
        coach's own re-seat."""
        ran = []
        for label, fx in self._each():
            with self.subTest(backend=label):
                api, gid = fx["api"], fx["gid"]
                home, away = fx["home"], fx["away"]
                p = self._mover(fx, "Mover")

                self.assertNotIn("error", api.select_roster(
                    gid, [p["id"]], actor_id=ADMIN), label)
                self.assertNotIn("error", api.remove_player(
                    gid, p["id"], actor_id=ADMIN), label)
                removed = api.store.roster_entry_for_player(gid, p["id"])
                self.assertEqual(removed.status.value, "removed", label)
                self.assertEqual(removed.team_side, home, label)

                self._move_to_the_other_side(fx, p["id"], away, label)

                self.assertNotIn("error", api.select_roster(
                    gid, [p["id"]], actor_id=ADMIN), label)
                back = api.store.roster_entry_for_player(gid, p["id"])
                self.assertEqual(back.team_side, away, (label, back.team_side))
                self.assertEqual(back.status.value, "selected", label)
                self.assertEqual(self._open(fx, away), 0, label)
                self.assertEqual(self._open(fx, home), 1, label)
            ran.append((label, RESEAT[0].name))
        self._assert_matrix_ran(ran, shapes=RESEAT)


class SubstituteReseatIsAttributedToTheSideItWasGatedAgainst(
        _ReseatHarness, unittest.TestCase):
    """``_add_to_roster_entry``'s revive branch (roster_service.py
    ~L940-947) -- the one the exact-head review called the most
    consequential of the three.

    Its caller gates on ``_require_open_slot(game_id, slot_type,
    ctx.team_id)`` and then hands THAT SAME ``ctx.team_id`` down to be
    written. If the revive kept the row's old side instead, the re-seat
    would be gated against the side it was validated for and counted
    against a different one -- two bodies reachable in the counted side's
    slot, and a phantom body in the gated one. So these tests assert the
    written value AND the enforcement/reporting coherence around it: after
    the re-seat, the side that was gated is the side that is full, and the
    side the row LEFT is genuinely takeable again.

    The enrollment is opened BEFORE any roster row exists, because
    ``enroll_substitute`` refuses a player who already has one (backed out
    or not) -- so this is the real production shape: a substitute-pool
    candidate the coach seated directly, who then backed out, and whose
    still-live candidacy re-seats them.

    MUTATION-PROVEN: deleting the two attribution writes from that branch
    leaves the whole suite green at head 84ecd90, and fails both tests
    here."""

    def _seated_then_backed_out(self, fx, label, offer=False):
        api, gid = fx["api"], fx["gid"]
        p = self._mover(fx, "Mover")
        self.assertNotIn("error", api.enroll_substitute(gid, p["id"]), label)
        if offer:
            # Offered while HOME's slot is genuinely open, so the offer is
            # not standing on a gate it never passed.
            self.assertNotIn("error", api.offer_substitute(gid, p["id"]),
                             label)
        self.assertNotIn("error", api.select_roster(
            gid, [p["id"]], actor_id=ADMIN), label)
        seated = api.store.roster_entry_for_player(gid, p["id"])
        self.assertEqual(seated.team_side, fx["home"], label)
        self.assertNotIn("error", api.set_availability(
            gid, p["id"], "unavailable", actor_id=p["id"]), label)
        return p

    def _assert_coherent(self, fx, p, label):
        """The written side, the reported counts and the ENFORCED gate all
        name AWAY; HOME is genuinely free again. Under the mutation every
        one of these is exactly reversed."""
        api, gid = fx["api"], fx["gid"]
        home, away = fx["home"], fx["away"]

        row = api.store.roster_entry_for_player(gid, p["id"])
        self.assertEqual(row.team_side, away, (label, row.team_side))
        self.assertEqual(row.status.value, "accepted", label)
        self.assertTrue(row.status.occupies_slot, label)

        # Reporting.
        self.assertEqual(self._open(fx, away), 0, label)
        self.assertEqual(self._open(fx, home), 1, label)

        # Enforcement, through the slot-gated substitute surface (coach
        # SELECTION is not slot-gated, so it could not show this): a HOME
        # candidate may be offered the reopened slot...
        h = self._mover(fx, "Home Candidate", side=home)
        self.assertNotIn("error", api.enroll_substitute(gid, h["id"]), label)
        self.assertNotIn("error", api.offer_substitute(gid, h["id"]), label)
        # ...and an AWAY candidate may NOT, because the re-seated row is
        # holding AWAY's only slot -- the same side its gate checked.
        a = self._mover(fx, "Away Candidate", side=away)
        self.assertNotIn("error", api.enroll_substitute(gid, a["id"]), label)
        refused = api.offer_substitute(gid, a["id"])
        self.assertEqual(refused.get("error", {}).get("code"),
                         "slot_already_filled", (label, refused))

        # And no overfill anywhere: one occupying body, on AWAY.
        occupying = sorted(e.player_id
                           for e in api.store.roster_for_game(gid)
                           if e.status.occupies_slot)
        self.assertEqual(occupying, [p["id"]], label)

    def test_coach_add_re_seats_on_the_side_it_resolved(self):
        ran = []
        for label, fx in self._each():
            with self.subTest(backend=label):
                api, gid = fx["api"], fx["gid"]
                p = self._seated_then_backed_out(fx, label)
                self._move_to_the_other_side(fx, p["id"], fx["away"], label)
                self.assertNotIn("error", api.add_substitute_to_roster(
                    gid, p["id"], actor_id=ADMIN), label)
                self._assert_coherent(fx, p, label)
            ran.append((label, RESEAT[0].name))
        self._assert_matrix_ran(ran, shapes=RESEAT)

    def test_accept_re_seats_on_the_side_it_resolved(self):
        """The player-driven entry point into the same branch: an offer
        made while the player was still HOME, accepted after the move.
        ``_accept_offered_substitute`` re-resolves and gates on AWAY, so
        the row it revives must say AWAY."""
        ran = []
        for label, fx in self._each():
            with self.subTest(backend=label):
                api, gid = fx["api"], fx["gid"]
                p = self._seated_then_backed_out(fx, label, offer=True)
                self._move_to_the_other_side(fx, p["id"], fx["away"], label)
                self.assertNotIn("error", api.accept_substitute(
                    gid, p["id"], actor_id=p["id"]), label)
                self._assert_coherent(fx, p, label)
            ran.append((label, RESEAT[0].name))
        self._assert_matrix_ran(ran, shapes=RESEAT)


class ReconfirmIdentifiesFromTheRowAndAuthorizesFromTheLiveSide(
        _ReseatHarness, unittest.TestCase):
    """``set_availability``'s re-confirm branch (roster_service.py
    ~L389-458) asks TWO questions, and their answers come from TWO
    different places. Conflating them is what round 2 shipped.

    IDENTIFY -- "WHICH slot is being taken back?" -- is the ROW's own
    ``entry.attribution``, never a fresh resolution. Re-confirming is not
    a seating: the row never left its slot, so the only slot in question
    is the one it holds. Re-resolving could name a different bucket (a
    season-scoped position that changed) or a different side than the row
    is actually counted in -- the enforcement/reporting incoherence this
    whole blocker is about.

    AUTHORIZE -- "MAY THIS PLAYER re-occupy that slot's SIDE?" -- is the
    LIVE context, never the stored value. A durable attribution records
    what the row holds; it says nothing about who is still entitled to
    hold it. If the stored value could authorize its own re-use, the row
    would be a standing permission that outlives the participation that
    granted it.

    THE ROUND-2 DEFECT THIS CLASS NOW PINS (owner ruling on PR #427,
    comment 5379885403 -- introduced by this branch's own
    durable-attribution round, not inherited). Round 2 called
    ``_require_membership_context`` to prove the player had SOME valid
    context and then gated the row's durable side. A player whose
    membership moved HOME->AWAY therefore passed the eligibility call on
    their AWAY context, the durable attribution yielded HOME, and the
    gate merely asked whether HOME's slot was open -- letting them
    self-confirm back onto a HOME row they were no longer eligible for.
    Reproduced RED at head e60d239 on Memory, SQLite AND PostgreSQL: the
    re-confirm returned no error and the row went to CONFIRMED with
    ``team_side`` still HOME while the live resolution answered AWAY.

    FOUR CASES, because a defect in either half is invisible to the other
    half's test:

    * live side != durable side, BOTH sides open  -> refused, AUTHORIZATION
      (``not_eligible`` / ``seated_side_not_live_eligible``);
    * live side != durable side, durable side FULL -> refused for the
      AUTHORIZATION reason, proving that gate runs FIRST;
    * live side == durable side, slot open        -> SUCCEEDS;
    * live side == durable side, slot FULL        -> refused, OCCUPANCY
      (``slot_already_filled``).

    The two refusal codes are asserted as DIFFERENT from each other, so a
    future change that collapses them into one answer fails here.

    The pre-061 NULL half of this branch is pinned separately by
    ``test_reconfirm_after_back_out_refuses_on_a_null_row``; the durable
    BUCKET half by
    ``ReconfirmTakesBackTheDurableBucketNotTheLiveOne``."""

    def _backed_out_on_home(self, fx, label):
        api, gid = fx["api"], fx["gid"]
        p = self._mover(fx, "Mover")
        self.assertNotIn("error", api.select_roster(
            gid, [p["id"]], actor_id=ADMIN), label)
        self.assertNotIn("error", api.set_availability(
            gid, p["id"], "available", actor_id=p["id"]), label)
        self.assertNotIn("error", api.set_availability(
            gid, p["id"], "unavailable", actor_id=p["id"]), label)
        entry = api.store.roster_entry_for_player(gid, p["id"])
        self.assertEqual(entry.status.value, "unavailable", label)
        self.assertEqual(entry.team_side, fx["home"], label)
        return p

    def _fill(self, fx, side, label, name, position=None):
        """Seat a body in ``side``'s only slot of ``position``'s bucket."""
        f = self._mover(fx, name, side=side, position=position)
        self.assertNotIn("error", fx["api"].select_roster(
            fx["gid"], [f["id"]], actor_id=ADMIN), label)
        bucket = "goalie" if position == "goalie" else "skater"
        self.assertEqual(self._open(fx, side, bucket), 0, label)
        return f

    def _assert_unchanged(self, fx, p, label, occupying):
        """A refused re-confirm writes NOTHING: the row is still backed
        out, still attributed to the side it was seated on, and the game's
        occupying bodies are exactly ``occupying``."""
        api, gid = fx["api"], fx["gid"]
        row = api.store.roster_entry_for_player(gid, p["id"])
        self.assertEqual(row.status.value, "unavailable", label)
        self.assertEqual(row.team_side, fx["home"], label)
        self.assertEqual(
            sorted(e.player_id for e in api.store.roster_for_game(gid)
                   if e.status.occupies_slot), sorted(occupying), label)

    def test_reconfirm_is_refused_when_the_live_side_left_the_durable_side(
            self):
        """Durable HOME, live AWAY, and NEITHER side is full -- so the only
        possible ground for refusal is AUTHORIZATION.

        THIS TEST PREVIOUSLY ASSERTED THE FORBIDDEN BEHAVIOUR. It was
        named ``test_reconfirm_succeeds_when_the_durable_side_is_open``
        and required this exact scenario to SUCCEED, on the reasoning that
        "AWAY's occupancy is none of its business". That reasoning is
        still right about OCCUPANCY and was wrong about ELIGIBILITY: the
        player is not merely seated elsewhere, they are no longer eligible
        for the side this row occupies at all. The owner's ruling (PR
        #427, comment 5379885403) settles it -- "a player whose live
        context moved HOME->AWAY must not self-confirm back onto the
        durable HOME row" -- so the assertion is INVERTED rather than
        deleted, and this docstring is the guard against the old
        expectation ever being restored as a "fix"."""
        ran = []
        for label, fx in self._each():
            with self.subTest(backend=label):
                api, gid = fx["api"], fx["gid"]
                home, away = fx["home"], fx["away"]
                p = self._backed_out_on_home(fx, label)
                self._move_to_the_other_side(fx, p["id"], away, label)

                # BOTH sides open: no slot-gate refusal is available here,
                # so a passing test can only be pinning the live-side
                # authorization check.
                self.assertEqual(self._open(fx, home), 1, label)
                self.assertEqual(self._open(fx, away), 1, label)
                self.assertEqual(self._live_side(fx, p["id"]), away, label)

                res = api.set_availability(gid, p["id"], "available",
                                           actor_id=p["id"])
                err = res.get("error", {})
                self.assertEqual(err.get("code"), "not_eligible",
                                 (label, res))
                self.assertEqual(err.get("details", {}).get("reason"),
                                 "seated_side_not_live_eligible",
                                 (label, res))
                # Distinguishable from the occupancy refusal, always.
                self.assertNotEqual(err.get("code"), "slot_already_filled",
                                    (label, res))
                self.assertEqual(err.get("details", {}).get("seated_team_id"),
                                 home, (label, res))
                self.assertEqual(
                    err.get("details", {}).get("eligible_team_id"), away,
                    (label, res))

                self._assert_unchanged(fx, p, label, occupying=[])
                # The HOME slot they were refused is still open -- refused,
                # not quietly consumed.
                self.assertEqual(self._open(fx, home), 1, label)
            ran.append((label, RECONFIRM[0].name))
        self._assert_matrix_ran(ran, shapes=RECONFIRM)

    def test_the_authorization_refusal_precedes_the_slot_gate(self):
        """Durable HOME is FULL and live is AWAY: BOTH gates would refuse,
        and the AUTHORIZATION one must be the one that speaks.

        This is exactly the scenario
        ``test_reconfirm_is_refused_when_the_durable_side_is_full`` used to
        set up. Once the live-side check exists, that case stops proving
        anything about the SLOT gate -- it never reaches it. Pinning the
        ORDER here is what lets the sibling be restructured to a
        live-agrees-with-durable fixture without losing the coverage this
        arrangement used to (accidentally) provide."""
        ran = []
        for label, fx in self._each():
            with self.subTest(backend=label):
                api, gid = fx["api"], fx["gid"]
                home, away = fx["home"], fx["away"]
                p = self._backed_out_on_home(fx, label)
                filler = self._fill(fx, home, label, "Home Filler")
                self._move_to_the_other_side(fx, p["id"], away, label)

                self.assertEqual(self._open(fx, home), 0, label)
                self.assertEqual(self._open(fx, away), 1, label)
                self.assertEqual(self._live_side(fx, p["id"]), away, label)

                res = api.set_availability(gid, p["id"], "available",
                                           actor_id=p["id"])
                err = res.get("error", {})
                self.assertEqual(err.get("code"), "not_eligible",
                                 (label, res))
                self.assertEqual(err.get("details", {}).get("reason"),
                                 "seated_side_not_live_eligible",
                                 (label, res))
                self._assert_unchanged(fx, p, label,
                                       occupying=[filler["id"]])
            ran.append((label, RECONFIRM[0].name))
        self._assert_matrix_ran(ran, shapes=RECONFIRM)

    def test_reconfirm_succeeds_when_the_live_side_matches_and_slot_is_open(
            self):
        """The ordinary case the whole gate exists to permit: the player
        never moved, so live and durable AGREE on HOME, and HOME's slot is
        still free. It must SUCCEED and re-take THAT slot.

        Asserted so the authorization check cannot be "fixed" by refusing
        everything: an over-broad gate fails here."""
        ran = []
        for label, fx in self._each():
            with self.subTest(backend=label):
                api, gid = fx["api"], fx["gid"]
                home, away = fx["home"], fx["away"]
                p = self._backed_out_on_home(fx, label)

                # The premise, asserted rather than assumed.
                self.assertEqual(self._live_side(fx, p["id"]), home, label)
                self.assertEqual(
                    api.store.roster_entry_for_player(
                        gid, p["id"]).team_side, home, label)
                self.assertEqual(self._open(fx, home), 1, label)

                res = api.set_availability(gid, p["id"], "available",
                                           actor_id=p["id"])
                self.assertNotIn("error", res, (label, res))
                row = api.store.roster_entry_for_player(gid, p["id"])
                self.assertEqual(row.status.value, "confirmed", label)
                # Re-confirming re-takes the slot it held; it does not
                # re-seat, so the attribution is untouched.
                self.assertEqual(row.team_side, home, (label, row.team_side))
                self.assertEqual(row.seated_position, Position.FORWARD, label)
                self.assertEqual(self._open(fx, home), 0, label)
                self.assertEqual(self._open(fx, away), 1, label)
                self.assertEqual(
                    api.roster.compute_roster_status(
                        gid, home).to_dict()["confirmed_skaters"], 1, label)
            ran.append((label, RECONFIRM[0].name))
        self._assert_matrix_ran(ran, shapes=RECONFIRM)

    def test_reconfirm_is_refused_when_the_durable_side_is_full(self):
        """The OCCUPANCY refusal, in the only arrangement that can still
        reach it: live and durable AGREE on HOME (the player never moved),
        and someone took HOME's single slot while they were out.

        RESTRUCTURED (owner requirement, PR #427). This test used to move
        the player HOME->AWAY as well, which -- once the live-side check
        exists -- makes it refuse for the AUTHORIZATION reason FIRST and
        the ``slot_already_filled`` assertion unreachable: the test would
        have gone on passing while silently no longer testing the slot
        gate at all. The agreement between live and durable is therefore
        asserted explicitly below; if a future change ever makes them
        disagree here, that assertion fails loudly instead of letting this
        case decay into a duplicate of the authorization test."""
        ran = []
        for label, fx in self._each():
            with self.subTest(backend=label):
                api, gid = fx["api"], fx["gid"]
                home, away = fx["home"], fx["away"]
                p = self._backed_out_on_home(fx, label)
                filler = self._fill(fx, home, label, "Home Filler")

                # THE PREMISE that keeps the slot gate reachable: the
                # authorization gate must have nothing to say here.
                self.assertEqual(self._live_side(fx, p["id"]), home, label)
                self.assertEqual(
                    api.store.roster_entry_for_player(
                        gid, p["id"]).team_side, home, label)
                self.assertEqual(self._open(fx, home), 0, label)
                self.assertEqual(self._open(fx, away), 1, label)

                res = api.set_availability(gid, p["id"], "available",
                                           actor_id=p["id"])
                self.assertEqual(res.get("error", {}).get("code"),
                                 "slot_already_filled", (label, res))
                self._assert_unchanged(fx, p, label,
                                       occupying=[filler["id"]])
            ran.append((label, RECONFIRM[0].name))
        self._assert_matrix_ran(ran, shapes=RECONFIRM)


class ReconfirmTakesBackTheDurableBucketNotTheLiveOne(_ReseatHarness,
                                                      unittest.TestCase):
    """The BUCKET half of "identify from the row": which of GOALIE/SKATER
    the re-confirm is taking back comes from ``entry.seated_position``,
    not from the player's CURRENT season-scoped position.

    The side is held CONSTANT in both cases (the membership is corrected
    in place with ``update_season_roster_membership``, so the team never
    changes and the round-3 live-side authorization gate is satisfied).
    That isolates the bucket: the only thing that moved is which slot a
    LIVE read would name.

    Why it matters: re-confirming puts THIS ROW back in the slot it held.
    A player recorded as a skater who is later corrected to goalie for the
    season did not retroactively occupy the goalie slot for a game already
    played out of, and must not be able to take one by backing out and
    re-confirming."""

    def _seated_skater_backed_out(self, fx, label):
        api, gid = fx["api"], fx["gid"]
        p = self._mover(fx, "Skater Mover")
        self.assertNotIn("error", api.select_roster(
            gid, [p["id"]], actor_id=ADMIN), label)
        self.assertNotIn("error", api.set_availability(
            gid, p["id"], "available", actor_id=p["id"]), label)
        self.assertNotIn("error", api.set_availability(
            gid, p["id"], "unavailable", actor_id=p["id"]), label)
        row = api.store.roster_entry_for_player(gid, p["id"])
        self.assertEqual(row.status.value, "unavailable", label)
        self.assertEqual(row.seated_position, Position.FORWARD, label)
        self.assertEqual(row.seated_slot_type, SlotType.SKATER, label)
        return p

    def _become_a_goalie(self, fx, p, label):
        """Correct the SEASON-SCOPED position in place, same team, and
        PROVE the live answer moved while the row's did not -- the
        anti-vacuity assertion for this whole class."""
        api = fx["api"]
        mid = self._stint_id(api, p["id"], fx["ls_id"])
        res = api.update_season_roster_membership(
            mid, position="goalie", actor_id=ADMIN)
        self.assertNotIn("error", res, (label, res))
        ctx = api.roster.resolve_membership_context(
            api.store.get_game(fx["gid"]), api.store.get_player(p["id"]))
        self.assertEqual(ctx.team_id, fx["home"],
                         (label, "the side must NOT have moved"))
        self.assertEqual(ctx.slot_type, SlotType.GOALIE,
                         (label, "live bucket did not move"))
        row = api.store.roster_entry_for_player(fx["gid"], p["id"])
        self.assertEqual(row.seated_slot_type, SlotType.SKATER,
                         (label, "durable bucket moved on its own"))

    def _fill_goalie(self, fx, label):
        g = self._mover(fx, "Home Goalie", side=fx["home"],
                        position="goalie")
        self.assertNotIn("error", fx["api"].select_roster(
            fx["gid"], [g["id"]], actor_id=ADMIN), label)
        self.assertEqual(self._open(fx, fx["home"], "goalie"), 0, label)
        return g

    def test_a_full_durable_bucket_refuses_though_the_live_bucket_is_open(
            self):
        """Durable SKATER slot FULL, live GOALIE slot OPEN -> refused.
        Gating on the live bucket would let this through and put a second
        body in HOME's single skater slot."""
        ran = []
        for label, fx in self._each(target_skaters=1, target_goalies=1):
            with self.subTest(backend=label):
                api, gid, home = fx["api"], fx["gid"], fx["home"]
                p = self._seated_skater_backed_out(fx, label)
                filler = self._mover(fx, "Home Filler", side=home)
                self.assertNotIn("error", api.select_roster(
                    gid, [filler["id"]], actor_id=ADMIN), label)
                self._become_a_goalie(fx, p, label)

                self.assertEqual(self._open(fx, home, "skater"), 0, label)
                self.assertEqual(self._open(fx, home, "goalie"), 1, label)

                res = api.set_availability(gid, p["id"], "available",
                                           actor_id=p["id"])
                self.assertEqual(res.get("error", {}).get("code"),
                                 "slot_already_filled", (label, res))
                row = api.store.roster_entry_for_player(gid, p["id"])
                self.assertEqual(row.status.value, "unavailable", label)
                self.assertEqual(row.seated_position, Position.FORWARD,
                                 label)
                self.assertEqual(
                    sorted(e.player_id
                           for e in api.store.roster_for_game(gid)
                           if e.status.occupies_slot),
                    [filler["id"]], label)
            ran.append((label, BUCKET[0].name))
        self._assert_matrix_ran(ran, shapes=BUCKET)

    def test_an_open_durable_bucket_succeeds_though_the_live_bucket_is_full(
            self):
        """The mirror: durable SKATER slot OPEN, live GOALIE slot FULL ->
        SUCCEEDS, and the row goes back into the SKATER bucket with its
        ``seated_position`` untouched (a re-confirm is not a re-seat)."""
        ran = []
        for label, fx in self._each(target_skaters=1, target_goalies=1):
            with self.subTest(backend=label):
                api, gid, home = fx["api"], fx["gid"], fx["home"]
                p = self._seated_skater_backed_out(fx, label)
                self._fill_goalie(fx, label)
                self._become_a_goalie(fx, p, label)

                self.assertEqual(self._open(fx, home, "skater"), 1, label)
                self.assertEqual(self._open(fx, home, "goalie"), 0, label)

                res = api.set_availability(gid, p["id"], "available",
                                           actor_id=p["id"])
                self.assertNotIn("error", res, (label, res))
                row = api.store.roster_entry_for_player(gid, p["id"])
                self.assertEqual(row.status.value, "confirmed", label)
                self.assertEqual(row.seated_position, Position.FORWARD,
                                 label)
                self.assertEqual(row.seated_slot_type, SlotType.SKATER,
                                 label)
                st = api.roster.compute_roster_status(gid, home).to_dict()
                self.assertEqual(st["open_skater_slots"], 0, (label, st))
                self.assertEqual(st["confirmed_skaters"], 1, (label, st))
                # The re-confirm landed in the SKATER bucket and nowhere
                # else: the goalie slot is still held by its own (merely
                # SELECTED, not yet confirmed) filler.
                self.assertEqual(st["open_goalie_slots"], 0, (label, st))
                self.assertEqual(st["confirmed_goalies"], 0, (label, st))
            ran.append((label, BUCKET[0].name))
        self._assert_matrix_ran(ran, shapes=BUCKET)


# ======================================================================
# 7. the SAME split, for the row that never backed out
# ======================================================================
# OWNER RULING, PR #427, 2026-08-22: "For player self-service, the durable
# roster row identifies the side and bucket; the player's current live
# context authorizes the transition. Therefore, SELECTED -> CONFIRMED must
# reject when the player's live team no longer matches the durable row's
# side. Perform that check before any mutation. Do not add an open-slot
# check: SELECTED already occupies the slot."
#
# WHY SECTION 6 DID NOT ALREADY COVER THIS. ``set_availability``'s
# re-confirm gate is guarded by ``reconfirming``, which requires
# ``entry.status == UNAVAILABLE``. A row still in SELECTED never enters
# that branch, so every assertion in section 6 was silent about it. A
# player coach-selected on HOME who never backed out, and whose live
# context then moved HOME->AWAY, fell straight through to the
# ``entry.status.occupies_slot`` sync at the bottom of the method and was
# written to CONFIRMED.
#
# REPRODUCED RED at head a721a77, identically on Memory, SQLite AND
# PostgreSQL, with BOTH sides carrying an open skater slot:
#
#   [memory]   open slots: home=1 away=2  <-- BOTH OPEN
#   [memory]   set_availability -> {'id': 'avail_1', ..., 'availability_
#              status': 'available', ...}          (NO error)
#   [memory]   RESULT row.status=confirmed row.team_side=team_1
#              HOME confirmed_skaters=1
#   [sqlite]   ...identical...
#   [postgres] ...identical...
#
# i.e. a now-AWAY-eligible player was a CONFIRMED HOME body.
#
# THE FIX IS THE SAME GATE, NOT A SECOND ONE.
# ``RosterService._authorize_seated_side`` is called by both of
# ``set_availability``'s routes to CONFIRMED, so the refusal shape
# (``not_eligible`` / ``seated_side_not_live_eligible`` /
# ``seated_team_id`` / ``eligible_team_id``) is identical by construction
# rather than by two copies agreeing. ``_mutating_the_shared_gate_breaks_
# both_paths`` is not a test but a MEASUREMENT recorded in this section's
# docstrings: the helper was mutated in place and the section-6 re-confirm
# tests AND the section-7 tests below failed together.
#
# WHAT IS DELIBERATELY *NOT* HERE: an open-slot check. A SELECTED row
# already holds its slot and CONFIRMED holds the same one, so requiring an
# open slot would refuse the last seat's own confirmation.
# ``test_confirmation_succeeds_when_the_live_side_still_matches`` runs
# with the durable side's only slot held BY THIS ROW, so an added
# occupancy gate fails it.

SELECTED_CONFIRM = (_Case("selected_row_confirmed_by_the_player"),)


class SelectedConfirmAuthorizesFromTheLiveSide(_ReseatHarness,
                                               unittest.TestCase):
    """The ruling's three required cases, tri-store."""

    def _selected_on_home(self, fx, label, name="Mover"):
        """Coach-selects ``name`` onto HOME and PROVES the row is in the
        state this section is about: SELECTED, never backed out, carrying
        a durable HOME attribution."""
        api, gid = fx["api"], fx["gid"]
        p = self._mover(fx, name)
        self.assertNotIn("error", api.select_roster(
            gid, [p["id"]], actor_id=ADMIN), label)
        entry = api.store.roster_entry_for_player(gid, p["id"])
        self.assertEqual(entry.status.value, "selected", label)
        self.assertEqual(entry.team_side, fx["home"], label)
        self.assertEqual(entry.seated_position, Position.FORWARD, label)
        # Never backed out: no availability row exists for them at all, so
        # the ``reconfirming`` branch provably cannot be what runs below.
        self.assertIsNone(api.store.availability_for_player(gid, p["id"]),
                          label)
        return p

    def test_confirmation_is_refused_when_the_live_side_left_the_row(self):
        """(a) + (b) of the ruling's required coverage.

        Selected on HOME, live context moved to AWAY, and BOTH sides still
        carry an open skater slot -- so no occupancy gate could produce
        this refusal even if one existed, and a passing assertion can only
        be pinning the live-side AUTHORIZATION.

        ``target_skaters=2`` is what makes "both sides open" literally
        true rather than nearly true: the SELECTED row itself holds one of
        HOME's seats, so at ``target_skaters=1`` HOME would read
        ``open=0`` and the test could no longer distinguish authorization
        from occupancy. The open counts are asserted on both sides before
        the call.

        TWO DIFFERENT "NO WRITES" CLAIMS, asserted by two different
        devices because ONE OF THEM CANNOT PROVE THE OTHER -- measured,
        not assumed.

        * NOTHING SURVIVED: the ``_writes`` identity snapshot (roster,
          availability, substitutes, AuditLog, SetupAuditLog,
          notification events, the notification feed and deliveries --
          every one as sorted identity tuples, since a bare count is
          satisfied by a same-cardinality row SWAP), plus the roster
          status of BOTH sides.
        * BEFORE ANY MUTATION, which is what the ruling actually
          requires: ``_write_attempts``. ``set_availability`` is
          ``@_transactional``, so the identical gate MOVED DOWN to just
          before ``_set_entry_status`` -- i.e. after the availability
          upsert and its audit row -- still leaves every snapshot above
          identical, on Memory, SQLite AND PostgreSQL, because the raise
          rolls the transaction back. That mutation was run in place and
          all of these assertions passed. The spy is what fails it,
          reporting ``['next_id', 'upsert_availability', 'next_id',
          'add_audit']`` on all three backends."""
        ran = []
        for label, fx in self._each(target_skaters=2):
            with self.subTest(backend=label):
                api, gid = fx["api"], fx["gid"]
                home, away = fx["home"], fx["away"]
                p = self._selected_on_home(fx, label)
                self._move_to_the_other_side(fx, p["id"], away, label)

                # BOTH SIDES OPEN -- the premise, asserted.
                self.assertEqual(self._open(fx, home), 1, label)
                self.assertEqual(self._open(fx, away), 2, label)
                self.assertEqual(self._live_side(fx, p["id"]), away, label)

                before = self._writes(api, gid)
                home_before = api.roster.compute_roster_status(
                    gid, home).to_dict()
                away_before = api.roster.compute_roster_status(
                    gid, away).to_dict()

                with self._write_attempts(api.store) as attempts:
                    res = api.set_availability(gid, p["id"], "available",
                                               actor_id=p["id"])
                # BEFORE ANY MUTATION, in the strong sense: not one store
                # write was even ATTEMPTED. See _write_attempts for why
                # the identity diff below cannot establish this on its own.
                self.assertEqual(attempts, [], (label, attempts))
                err = res.get("error", {})
                self.assertEqual(err.get("code"), "not_eligible",
                                 (label, res))
                self.assertEqual(err.get("details", {}).get("reason"),
                                 "seated_side_not_live_eligible",
                                 (label, res))
                self.assertEqual(err.get("details", {}).get("seated_team_id"),
                                 home, (label, res))
                self.assertEqual(
                    err.get("details", {}).get("eligible_team_id"), away,
                    (label, res))
                # The SAME shape the backed-out path raises -- asserted,
                # so the two cannot drift into two dialects of one refusal.
                self.assertNotEqual(err.get("code"), "slot_already_filled",
                                    (label, res))

                # (b) THE DURABLE ENTRY, unchanged in every field the
                # seating decided.
                row = api.store.roster_entry_for_player(gid, p["id"])
                self.assertEqual(row.status.value, "selected", label)
                self.assertEqual(row.team_side, home, label)
                self.assertEqual(row.seated_position, Position.FORWARD,
                                 label)
                self.assertEqual(row.seated_slot_type, SlotType.SKATER,
                                 label)

                # (b) OCCUPANCY and the roster-status counts, BOTH sides.
                self.assertEqual(
                    api.roster.compute_roster_status(gid, home).to_dict(),
                    home_before, label)
                self.assertEqual(
                    api.roster.compute_roster_status(gid, away).to_dict(),
                    away_before, label)

                # (b) ZERO WRITES, by identity, across every class.
                self.assertEqual(self._writes(api, gid), before, label)
                # ...and the availability row specifically was never even
                # created, which is the write that would otherwise have
                # landed first.
                self.assertIsNone(
                    api.store.availability_for_player(gid, p["id"]), label)
            ran.append((label, SELECTED_CONFIRM[0].name))
        self._assert_matrix_ran(ran, shapes=SELECTED_CONFIRM)

    def test_confirmation_succeeds_when_the_live_side_still_matches(self):
        """(c) of the ruling's required coverage, arranged so that an
        over-broad fix fails here.

        The player never moved, so live and durable AGREE on HOME. HOME's
        ONLY skater slot is the one THIS ROW holds, so
        ``open_skater_slots`` reads 0 before the call -- the exact
        arrangement in which an added open-slot check would refuse a
        legitimate confirmation. The ruling forbids that check, and this
        test is what enforces the prohibition."""
        ran = []
        for label, fx in self._each(target_skaters=1):
            with self.subTest(backend=label):
                api, gid = fx["api"], fx["gid"]
                home, away = fx["home"], fx["away"]
                p = self._selected_on_home(fx, label)

                # The premise: agreement, and the durable side FULL --
                # full because this row is what fills it.
                self.assertEqual(self._live_side(fx, p["id"]), home, label)
                self.assertEqual(self._open(fx, home), 0, label)

                res = api.set_availability(gid, p["id"], "available",
                                           actor_id=p["id"])
                self.assertNotIn("error", res, (label, res))
                self.assertEqual(res["availability_status"], "available",
                                 (label, res))
                row = api.store.roster_entry_for_player(gid, p["id"])
                self.assertEqual(row.status.value, "confirmed", label)
                # Confirming is not a re-seat: the attribution is untouched.
                self.assertEqual(row.team_side, home, label)
                self.assertEqual(row.seated_position, Position.FORWARD,
                                 label)
                st = api.roster.compute_roster_status(gid, home).to_dict()
                self.assertEqual(st["confirmed_skaters"], 1, (label, st))
                self.assertEqual(st["open_skater_slots"], 0, (label, st))
                self.assertEqual(self._open(fx, away), 1, label)
            ran.append((label, SELECTED_CONFIRM[0].name))
        self._assert_matrix_ran(ran, shapes=SELECTED_CONFIRM)

    def test_a_player_with_no_live_context_at_all_is_refused(self):
        """The fail-CLOSED half of "the live context authorizes": a
        SELECTED player whose participation ended entirely resolves to NO
        context, so there is no team to compare and the gate refuses
        before it ever reaches the comparison.

        Distinct from the mismatch refusal above and asserted as such --
        ``not_eligible`` with NO ``seated_side_not_live_eligible`` reason,
        because nothing about the row's side is what is wrong here."""
        ran = []
        for label, fx in self._each(target_skaters=2):
            with self.subTest(backend=label):
                api, gid, home = fx["api"], fx["gid"], fx["home"]
                p = self._selected_on_home(fx, label)
                end_membership_directly(
                    api.store, self._stint_id(api, p["id"], fx["ls_id"]),
                    "transferred")
                self.assertIsNone(self._live_side(fx, p["id"]), label)

                before = self._writes(api, gid)
                with self._write_attempts(api.store) as attempts:
                    res = api.set_availability(gid, p["id"], "available",
                                               actor_id=p["id"])
                err = res.get("error", {})
                self.assertEqual(err.get("code"), "not_eligible",
                                 (label, res))
                self.assertNotEqual(
                    err.get("details", {}).get("reason"),
                    "seated_side_not_live_eligible", (label, res))
                self.assertEqual(attempts, [], (label, attempts))
                self.assertEqual(self._writes(api, gid), before, label)
                self.assertEqual(
                    api.store.roster_entry_for_player(
                        gid, p["id"]).status.value, "selected", label)
                # The slot the row holds did not reopen either.
                self.assertEqual(self._open(fx, home), 1, label)
            ran.append((label, SELECTED_CONFIRM[0].name))
        self._assert_matrix_ran(ran, shapes=SELECTED_CONFIRM)


class _HttpAvailabilityHarness(_ReseatHarness):
    """The real listening socket + real authenticated session shared by
    the two route classes below.

    Extracted when the SELECTED ruling arrived rather than copied: a
    second setUpClass/opener/_req would be a second chance to point
    ``srv.STATE.api`` somewhere subtly different from the store the
    assertions read, which is exactly the vacuous-coverage failure this
    file already exists to prevent.

    THE ROUTE IS THE ONE THE UI POSTS TO, confirmed rather than assumed:
    ``static/app.js`` line 10392 sends the player's "confirm" action to
    ``POST /api/games/{currentGame}/availability`` with
    ``{player_id, availability_status: "available"}`` -- the same route
    line 10393's "backout" uses. There is no separate confirm endpoint;
    ``PATCH /roster/{playerId}/status`` has no frontend caller at all
    (owner ruling, PR #427), which is why the player-facing gate lives in
    ``set_availability`` and is proven here.

    The facade tests above call ``ApiService.set_availability`` directly.
    That proves the rule; it does not prove the transport relays the new
    refusal as a structured JSON error rather than a 500 or a silent 200.
    Both outcomes are asserted: the authorization refusal arrives as
    403 ``not_eligible`` carrying its machine-readable ``reason``, and the
    legitimate confirm arrives as a 200 that really did move the row.

    TRI-STORE, through the same ``_stores``/``_assert_backend``/
    ``_assert_matrix_ran`` loop as the rest of this file -- the server's
    ``STATE.api`` is pointed at THIS fixture's ApiService for the duration
    of each backend's case, so the HTTP request runs against Memory,
    SQLite and real PostgreSQL in turn rather than against the demo
    singleton's own store."""

    @classmethod
    def setUpClass(cls):
        cls._saved_api = srv.STATE.api
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
        # The demo singleton is process-global and shared with every other
        # module this worker runs; hand it back exactly as found.
        srv.STATE.api = cls._saved_api

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            # Closed explicitly: an HTTPError holds an open response body,
            # and letting the GC reclaim it emits a ResourceWarning at
            # interpreter shutdown that run_parallel.py deliberately does
            # NOT filter (only unclosed-socket warnings are noise there),
            # so a refusal-path test would add permanent noise to every
            # failing shard's report.
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def _serve(self, fx, label):
        """Point the running server at this backend's fixture and sign in.

        The account is created in THIS fixture's store, so the login the
        server performs resolves against the same backend the assertions
        read -- there is no second, hidden store behind the request."""
        fx["api"].accounts.create_account(
            "admin", DEMO_PASSWORD, DEMO_USERS["admin"], scope={},
            actor_id="test_seed", account_id="user_admin")
        srv.STATE.api = fx["api"]
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        status, body = self._req(opener, "POST", "/api/auth/login",
                                 {"username": "admin", "password": "demo"})
        self.assertEqual(status, 200, (label, body))
        return opener


class ReconfirmOverTheRealHttpAvailabilityRoute(_HttpAvailabilityHarness,
                                                unittest.TestCase):
    """Section 6's ruling (the BACKED-OUT row), over the real route."""

    def _backed_out_on_home(self, fx, label):
        api, gid = fx["api"], fx["gid"]
        p = self._mover(fx, "Mover")
        self.assertNotIn("error", api.select_roster(
            gid, [p["id"]], actor_id=ADMIN), label)
        self.assertNotIn("error", api.set_availability(
            gid, p["id"], "available", actor_id=p["id"]), label)
        self.assertNotIn("error", api.set_availability(
            gid, p["id"], "unavailable", actor_id=p["id"]), label)
        return p

    def test_http_refuses_the_reconfirm_when_the_live_side_moved_away(self):
        ran = []
        for label, fx in self._each():
            with self.subTest(backend=label):
                api, gid = fx["api"], fx["gid"]
                p = self._backed_out_on_home(fx, label)
                self._move_to_the_other_side(fx, p["id"], fx["away"], label)
                self.assertEqual(self._open(fx, fx["home"]), 1, label)

                opener = self._serve(fx, label)
                status, body = self._req(
                    opener, "POST", f"/api/games/{gid}/availability",
                    {"player_id": p["id"],
                     "availability_status": "available"})
                self.assertEqual(status, 403, (label, body))
                self.assertEqual(body["error"]["code"], "not_eligible",
                                 (label, body))
                self.assertEqual(body["error"]["details"]["reason"],
                                 "seated_side_not_live_eligible",
                                 (label, body))
                # Nothing written: still backed out, still HOME, and the
                # HOME slot still open.
                row = api.store.roster_entry_for_player(gid, p["id"])
                self.assertEqual(row.status.value, "unavailable", label)
                self.assertEqual(row.team_side, fx["home"], label)
                self.assertEqual(self._open(fx, fx["home"]), 1, label)
            ran.append((label, RECONFIRM[0].name))
        self._assert_matrix_ran(ran, shapes=RECONFIRM)

    def test_http_allows_the_reconfirm_when_the_live_side_still_matches(self):
        ran = []
        for label, fx in self._each():
            with self.subTest(backend=label):
                api, gid, home = fx["api"], fx["gid"], fx["home"]
                p = self._backed_out_on_home(fx, label)
                self.assertEqual(self._live_side(fx, p["id"]), home, label)

                opener = self._serve(fx, label)
                status, body = self._req(
                    opener, "POST", f"/api/games/{gid}/availability",
                    {"player_id": p["id"],
                     "availability_status": "available"})
                self.assertEqual(status, 200, (label, body))
                self.assertEqual(body["availability_status"], "available",
                                 (label, body))
                row = api.store.roster_entry_for_player(gid, p["id"])
                self.assertEqual(row.status.value, "confirmed", label)
                self.assertEqual(row.team_side, home, label)
                self.assertEqual(self._open(fx, home), 0, label)
            ran.append((label, RECONFIRM[0].name))
        self._assert_matrix_ran(ran, shapes=RECONFIRM)


class SelectedConfirmOverTheRealHttpAvailabilityRoute(
        _HttpAvailabilityHarness, unittest.TestCase):
    """Section 7's ruling (the SELECTED row that never backed out), over
    the same real route the UI posts to.

    The facade tests prove the rule; these prove the PLAYER-FACING
    SURFACE carries it. Without them the gate could be correct in the
    service and still reach the UI as a 500, or -- worse -- never be
    reached at all because the route took some other path into CONFIRMED.
    """

    def _selected_on_home(self, fx, label):
        api, gid = fx["api"], fx["gid"]
        p = self._mover(fx, "Mover")
        self.assertNotIn("error", api.select_roster(
            gid, [p["id"]], actor_id=ADMIN), label)
        entry = api.store.roster_entry_for_player(gid, p["id"])
        self.assertEqual(entry.status.value, "selected", label)
        self.assertEqual(entry.team_side, fx["home"], label)
        self.assertIsNone(api.store.availability_for_player(gid, p["id"]),
                          label)
        return p

    def test_http_refuses_the_confirm_when_the_live_side_moved_away(self):
        ran = []
        for label, fx in self._each(target_skaters=2):
            with self.subTest(backend=label):
                api, gid, home = fx["api"], fx["gid"], fx["home"]
                p = self._selected_on_home(fx, label)
                self._move_to_the_other_side(fx, p["id"], fx["away"], label)
                # BOTH sides open, over HTTP too.
                self.assertEqual(self._open(fx, home), 1, label)
                self.assertEqual(self._open(fx, fx["away"]), 2, label)

                opener = self._serve(fx, label)
                before = self._writes(api, gid)
                status, body = self._req(
                    opener, "POST", f"/api/games/{gid}/availability",
                    {"player_id": p["id"],
                     "availability_status": "available"})
                self.assertEqual(status, 403, (label, body))
                self.assertEqual(body["error"]["code"], "not_eligible",
                                 (label, body))
                self.assertEqual(body["error"]["details"]["reason"],
                                 "seated_side_not_live_eligible",
                                 (label, body))
                # Nothing written, by identity -- including the
                # GameAvailability row the route would otherwise upsert
                # first.
                self.assertEqual(self._writes(api, gid), before, label)
                self.assertIsNone(
                    api.store.availability_for_player(gid, p["id"]), label)
                row = api.store.roster_entry_for_player(gid, p["id"])
                self.assertEqual(row.status.value, "selected", label)
                self.assertEqual(row.team_side, home, label)
                self.assertEqual(self._open(fx, home), 1, label)
            ran.append((label, SELECTED_CONFIRM[0].name))
        self._assert_matrix_ran(ran, shapes=SELECTED_CONFIRM)

    def test_http_allows_the_confirm_when_the_live_side_still_matches(self):
        ran = []
        for label, fx in self._each(target_skaters=1):
            with self.subTest(backend=label):
                api, gid, home = fx["api"], fx["gid"], fx["home"]
                p = self._selected_on_home(fx, label)
                self.assertEqual(self._live_side(fx, p["id"]), home, label)
                # The durable side's only slot is the one THIS ROW holds:
                # an added occupancy gate would 403 here.
                self.assertEqual(self._open(fx, home), 0, label)

                opener = self._serve(fx, label)
                status, body = self._req(
                    opener, "POST", f"/api/games/{gid}/availability",
                    {"player_id": p["id"],
                     "availability_status": "available"})
                self.assertEqual(status, 200, (label, body))
                self.assertEqual(body["availability_status"], "available",
                                 (label, body))
                row = api.store.roster_entry_for_player(gid, p["id"])
                self.assertEqual(row.status.value, "confirmed", label)
                self.assertEqual(row.team_side, home, label)
                self.assertEqual(
                    api.roster.compute_roster_status(
                        gid, home).to_dict()["confirmed_skaters"], 1, label)
            ran.append((label, SELECTED_CONFIRM[0].name))
        self._assert_matrix_ran(ran, shapes=SELECTED_CONFIRM)


if __name__ == "__main__":
    unittest.main()
