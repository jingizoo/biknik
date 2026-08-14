"""A scoped read DISPATCHED BEFORE A CONTEXT SWITCH but ARRIVING AFTER IT is
DISCARDED, not refused — and no elapsed time and no amount of churn can turn
that discard back into a refusal (#159 follow-up to #415).

WHAT #415 CLOSED AND WHAT IT COULD NOT. ``test_context_switch_server_exit.py``
proves a switch cannot commit while a scoped read is ALREADY INSIDE the server.
Nothing here trades any of that away — one case below re-asserts it directly.
But ``services/context_gate.py`` orders participants BY ARRIVAL AT ``do_GET``
and ``app.js`` orders them BY FETCH DISPATCH, and between those two instants
lies an interval neither process owns: the browser's own request queue and the
wire.

    render() dispatches  GET .../seasons/<old>/venue-candidates
    the operator switches; the client aborts it, the JS promise SETTLES, and
      awaitContextScopedReadSettlement() therefore RETURNS
    POST /api/context reaches the server FIRST, takes the writer slot, commits
    only THEN does the GET reach do_GET, takes a HIGHER arrival sequence,
      correctly waits behind that writer, correctly runs against the NEW tuple,
      and the unchanged exact-Season ceiling correctly answers the generic 404
      — for a question the operator had already withdrawn

CI RECORD: main@1de50d7 and main@e385bfb, browser shard 1, ``setup-state-matrix``
DESKTOP leg, ``GET /api/v2/setup/seasons/season_3/venue-candidates -> 404``,
with the app's own ledger recording that read as ``generation=5
dispatched=true``. No ``[context-gate]`` line is logged, because nothing timed
out: every component behaved correctly.

THE FILE NAME IS OLDER THAN THE DESIGN IT TESTS, and is kept only so the diff
against the rejected build stays legible. The mechanism is NOT a cancellation
handoff. An earlier revision gave each read a client-minted id, had the switch's
POST declare the ids it cancelled, and kept those ids in a bounded per-user
registry with a TTL and two eviction caps. The repository owner rejected it:

    "Eviction/TTL must never turn a declared cancellation back into a 404. 'TTL
     exceeds the gate wait' is insufficient because network arrival can exceed
     both. The design needs proof that retention lasts through claim, or fail
     the switch before committing, or use non-evictable epoch/tombstone
     semantics."

The rejection is exact. A read's arrival time is bounded by the NETWORK, so no
retention window can be proven long enough, and every way an id could leave that
registry — expired, evicted by the per-user FIFO, evicted by the per-process LRU
— collapsed to the same answer and fell through to the ceiling's 404.

WHAT REPLACED IT, AND WHAT THIS FILE HAS TO PROVE. The server derives an opaque
CONTEXT EPOCH from the ``ActiveContext`` row it already persists, hands it out
wherever the client learns its context, and every scoped read echoes the epoch
it was RENDERED UNDER in ``X-Context-Epoch``. On arrival the server compares that
echo with the epoch of the row as it stands now: equal proceeds exactly as today
(ceiling included), unequal answers ``204 No Content`` before the ceiling is
evaluated, absent behaves exactly as it did before any of this existed.

    NOTHING IS RETAINED, so the rejected failure mode is UNCONSTRUCTIBLE rather
    than unlikely. ``test_no_elapsed_time_and_no_churn_can_turn_the_discard_
    back_into_a_404`` is the case that says so, and it says it three ways: with
    real churn far beyond every cap the rejected registry had, with a real
    elapsed delay, and — the part that actually settles it — by showing the
    COMPARED VALUE is byte-identical before and after both, so there is no
    quantity of either that the outcome could depend on.

HOW THE INTERLEAVING IS FORCED RATHER THAN RACED. ``_arrival_park`` shadows
``Handler.do_GET`` on the class and blocks the target GET at the TOP of it,
strictly ABOVE ``CONTEXT_GATE.arrive()``. While it is held the gate holds NO
ticket for that request — asserted, via ``stats()["readers"] == 0`` — so the
gate's view of the world is byte-identical to the request not existing, the
switch waits for nothing and commits, and the read then arrives into a server
whose selection has already moved. That is a browser queue, reproduced exactly.
No production module is patched: the shadow is installed on the test's own
class attribute and removed in a ``finally``.

NO ASSERTION HERE IS SATISFIED BY A SLEEP. Every wait is on an
``threading.Event`` or a polled predicate whose value is asserted. The one real
delay in the file is in the eviction-impossibility case, where the DELAY IS THE
VARIABLE UNDER TEST rather than a synchronization device; it is env-tunable to
any value (``HS_LATE_ARRIVAL_DELAY_SECONDS``) precisely so a reviewer can
confirm the assertions do not move.

EVERY CLASS RUNS ON MEMORY, SQLITE AND POSTGRESQL. The comparison is enforced
in-process above the store, so it must behave identically on all three; the
epoch's material is normalized (``context_epoch._field``) so the token for a
given row is the same string on all three, which is why the pure-function cases
can assert one expected value for every backend.
"""

import importlib
import os
import re
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import Role
from hockey_scheduler.domain.setup_models import ActiveContext
from hockey_scheduler.services.context_epoch import (
    CONTEXT_EPOCH_HEADER, EPOCH_ABSENT, EPOCH_MATCH, EPOCH_MISMATCH,
    context_epoch, epoch_verdict, is_epoch_token)

from test_context_switch_server_exit import (
    PATIENCE, ContextGateFixtureBase, _Park, _wait)

# The "arbitrarily large" delay between dispatch and arrival, in seconds. Small
# by default only so the suite stays fast: the assertions this case makes do not
# reference it, and a reviewer who sets it to 3600 gets the identical result —
# which is the claim. Under the REJECTED design, anything past its 30s TTL (or
# past a single capacity eviction, which needed no time at all) turned the
# discard back into the ceiling's 404.
ARBITRARY_DELAY_SECONDS = float(
    os.environ.get("HS_LATE_ARRIVAL_DELAY_SECONDS", "0.25"))

# Churn sized past the magnitudes the rejected registry's caps were expressed
# in: MAX_USERS was 256 (an LRU over per-user buckets) and MAX_IDS_PER_USER was
# 64 (a FIFO within one bucket). Stated honestly — this cannot RE-RUN that
# design's eviction, because the module is deleted; what it does is drive the
# interleaving under load well beyond the scale at which that design started
# discarding records, so a build that quietly reintroduced retention at similar
# magnitudes fails here.
CHURN_USERS = 300
CHURN_SWITCHES_EACH = 2
CHURN_READS = 80
CHURN_HTTP_SWITCHES = 12

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


class _FakeRow:
    """A stand-in for one persisted ``ActiveContext``, for the pure-function
    cases. Used instead of a real store so those cases test the DERIVATION and
    nothing else — no HTTP, no schema, no backend differences to explain away.
    """

    def __init__(self, id, program_id, season_id, league_id, updated_at):
        self.id = id
        self.program_id = program_id
        self.season_id = season_id
        self.league_id = league_id
        self.updated_at = updated_at


class _FakeStore:
    """Rows plus (optionally) the Seasons they select: the SELECTED Season's
    lifecycle is part of the epoch material (see ``_selected_season_lifecycle``
    in services/context_epoch.py), so the derivation reads ``get_season`` for
    any row with a ``season_id``. The cases in THIS file leave ``seasons``
    empty on purpose — a selected id with no Season behind it is the
    well-defined ``season-gone`` state, constant across every case here, so
    these cases keep measuring the ROW axis alone. The lifecycle axis has its
    own file, ``tests/test_context_epoch_lifecycle.py``."""

    def __init__(self, rows=None, seasons=None):
        self.rows = dict(rows or {})
        self.seasons = dict(seasons or {})

    def get_active_context(self, user_id):
        return self.rows.get(user_id)

    def get_season(self, season_id):
        return self.seasons.get(season_id)


# ==========================================================================
# THE DERIVATION ITSELF — no HTTP, no store, no threads.
# ==========================================================================
class ContextEpochDerivationTest(unittest.TestCase):
    """The four properties the epoch has to have before any of the wiring
    below can mean anything. Store-independent on purpose: these are claims
    about a pure function, and proving them through a server would prove them
    only for whichever backend happened to run."""

    ROW = _FakeRow("user_a", "program_1", "season_1", "league_1",
                   datetime(2026, 8, 12, 9, 30, 15, 123456, tzinfo=timezone.utc))

    def test_the_same_persisted_row_always_produces_the_same_token(self):
        """PROPERTY 1: stable for a given row, in this process and any other.

        A per-process counter or a random nonce would satisfy every other case
        in this file and fail here — and in production would invalidate every
        outstanding read on every restart.
        """
        store = _FakeStore({"user_a": self.ROW})
        first = context_epoch(store, "user_a")
        again = [context_epoch(store, "user_a") for _ in range(50)]
        self.assertTrue(all(t == first for t in again),
                        f"the token moved without the row moving: {set(again)}")
        # Recomputed from an EQUAL-BUT-DISTINCT row object: the material is the
        # row's values, never its identity, so a fresh hydration from the
        # database has to hash the same as the object that was written.
        rehydrated = _FakeStore({"user_a": _FakeRow(
            "user_a", "program_1", "season_1", "league_1",
            datetime(2026, 8, 12, 9, 30, 15, 123456, tzinfo=timezone.utc))})
        self.assertEqual(context_epoch(rehydrated, "user_a"), first,
                         "an equal row hydrated fresh produced a different "
                         "token, so the token depends on object identity")

    def test_a_switch_back_to_the_same_tuple_still_moves_the_token(self):
        """PROPERTY 2, and the one a tuple-derived token would fail.

        A -> B -> A leaves the operator on the tuple they started from. A token
        derived from the tuple alone would be identical at both ends, and a read
        rendered before the round trip would be silently READMITTED against a
        selection that had moved twice underneath it.
        """
        base = self.ROW
        later = _FakeRow(base.id, base.program_id, base.season_id,
                         base.league_id,
                         base.updated_at.replace(microsecond=123457))
        self.assertNotEqual(
            context_epoch(_FakeStore({"user_a": base}), "user_a"),
            context_epoch(_FakeStore({"user_a": later}), "user_a"),
            "the token did not move when only updated_at moved, so a switch "
            "back to the same tuple would be invisible to it")

    def test_every_axis_and_the_owner_are_part_of_the_material(self):
        """Any one field changing must move the token. Otherwise some switch —
        Program-only, a League change, a different operator on identical data —
        would leave a stale read admissible."""
        seen = {context_epoch(_FakeStore({"user_a": self.ROW}), "user_a")}
        variants = {
            "program": ("program_2", self.ROW.season_id, self.ROW.league_id),
            "season": (self.ROW.program_id, "season_2", self.ROW.league_id),
            "league": (self.ROW.program_id, self.ROW.season_id, "league_2"),
            "season_cleared": (self.ROW.program_id, None, self.ROW.league_id),
            "league_cleared": (self.ROW.program_id, self.ROW.season_id, None),
        }
        for label, (p, s, lg) in variants.items():
            row = _FakeRow(self.ROW.id, p, s, lg, self.ROW.updated_at)
            token = context_epoch(_FakeStore({"user_a": row}), "user_a")
            self.assertNotIn(token, seen,
                             f"changing the {label} did not move the token")
            seen.add(token)
        # A DIFFERENT OWNER on byte-identical selection data. The comparison is
        # always against the session's own epoch, so a collision here could not
        # widen anything — but it would mean the token described a selection
        # rather than a selection HELD BY SOMEONE, which is not what the reads
        # are being judged against.
        other = _FakeRow("user_b", self.ROW.program_id, self.ROW.season_id,
                         self.ROW.league_id, self.ROW.updated_at)
        self.assertNotIn(context_epoch(_FakeStore({"user_b": other}), "user_b"),
                         seen, "two operators' identical selections collided")

    def test_no_field_separator_confusion_can_forge_a_collision(self):
        """The material is separated by a control character no id or ISO
        timestamp can contain, so no two different rows can be rearranged into
        the same string. A naive ``"|".join`` would let ("a", "b|c") and
        ("a|b", "c") collide, and the collision would readmit a read across a
        switch."""
        left = _FakeRow("u", "a", "b\x1fc", None, self.ROW.updated_at)
        right = _FakeRow("u", "a\x1fb", "c", None, self.ROW.updated_at)
        self.assertNotEqual(context_epoch(_FakeStore({"u": left}), "u"),
                            context_epoch(_FakeStore({"u": right}), "u"))

    def test_the_token_is_opaque_and_carries_no_identifier(self):
        """PROPERTY 3. Nothing is concatenated or encoded, so a holder of a
        token cannot read a user, Program, Season, League or timestamp out of
        it. (The honest limit — a party who ALREADY holds the whole row can
        recompute and confirm it — is stated in the module docstring of
        services/context_epoch.py; it discloses nothing that party did not
        supply, and confirming it confers nothing.)"""
        token = context_epoch(_FakeStore({"user_a": self.ROW}), "user_a")
        self.assertRegex(token, _HEX32)
        for secret in ("user_a", "program_1", "season_1", "league_1",
                       "2026", "123456"):
            self.assertNotIn(secret, token,
                             f"{secret!r} is readable out of the token")

    def test_a_user_with_no_persisted_row_still_has_an_epoch(self):
        """A brand-new operator has no ``ActiveContext`` row. That is still a
        state a read can be rendered under, and the FIRST switch must move away
        from it — otherwise the very first switch of a session is the one
        interleaving the mechanism does not cover."""
        empty = _FakeStore()
        absent = context_epoch(empty, "user_a")
        self.assertRegex(absent, _HEX32)
        self.assertEqual(absent, context_epoch(empty, "user_a"))
        self.assertNotEqual(
            absent, context_epoch(_FakeStore({"user_a": self.ROW}), "user_a"),
            "making the first selection did not move the epoch away from the "
            "no-row state")
        # ...and it is still per-user, so one absent row is not every absent row.
        self.assertNotEqual(absent, context_epoch(empty, "user_b"))

    def test_the_verdict_is_absent_match_or_mismatch_and_fails_closed(self):
        """PROPERTY 4. Absent is today's behaviour; anything present but not
        exactly current DISCARDS, including values that cannot be tokens at
        all. There is deliberately no fourth outcome: a 'malformed' branch that
        refused would give the header power over the response, which is the one
        thing it must never have."""
        store = _FakeStore({"user_a": self.ROW})
        current = context_epoch(store, "user_a")
        self.assertEqual(epoch_verdict(store, "user_a", None), EPOCH_ABSENT)
        self.assertEqual(epoch_verdict(store, "user_a", ""), EPOCH_ABSENT)
        self.assertEqual(epoch_verdict(store, "user_a", "   "), EPOCH_ABSENT)
        self.assertEqual(epoch_verdict(store, "user_a", current), EPOCH_MATCH)
        self.assertEqual(epoch_verdict(store, "user_a", f"  {current}  "),
                         EPOCH_MATCH)
        for junk in ("not-a-token", current.upper(), current[:-1], current + "0",
                     "g" * 32, "../../etc/passwd", "0", "\x00" * 32):
            self.assertEqual(epoch_verdict(store, "user_a", junk),
                             EPOCH_MISMATCH, f"{junk!r} did not discard")
        self.assertFalse(is_epoch_token(None))
        self.assertFalse(is_epoch_token(current.upper()))
        self.assertTrue(is_epoch_token(current))

    def test_nothing_is_retained_so_nothing_can_be_evicted(self):
        """THE STRUCTURAL HALF of the eviction-impossibility argument, and the
        one that does not depend on any interleaving being reproduced.

        The rejected design's failure was reachable because it KEPT something:
        a TTL could expire it and two capacity caps could evict it, and each of
        those turned a declared cancellation back into the ceiling's 404. This
        module keeps nothing at all — so there is no state to expire, no cap to
        evict from, and no configuration to get wrong. That is asserted here
        rather than described, because 'we removed the cache' is exactly the
        kind of claim that quietly stops being true.
        """
        module = importlib.import_module(
            "hockey_scheduler.services.context_epoch")
        forbidden = re.compile(r"TTL|MAX_|EXPIRE|EVICT|CACHE|REGISTRY|_STATE",
                               re.IGNORECASE)
        offenders = [n for n in vars(module)
                     if not n.startswith("__") and forbidden.search(n)]
        self.assertEqual(offenders, [],
                         f"the epoch module grew retention machinery: "
                         f"{offenders}")
        # NO CLOCK IS IMPORTED AT ALL, so no elapsed time can be consulted even
        # by accident. This is the single most load-bearing line in the file:
        # the rejected design's whole failure was a comparison against a
        # deadline, and a module that cannot read a clock cannot have one.
        for clocky in ("time", "monotonic", "perf_counter"):
            self.assertNotIn(clocky, vars(module),
                             f"the epoch module can now read a clock "
                             f"({clocky}), so a deadline is expressible again")
        # No module-level mutable state of any kind: a dict/list/set at module
        # scope is the shape every cache in this repository has taken.
        mutable = [n for n, v in vars(module).items()
                   if not n.startswith("__")
                   and isinstance(v, (dict, list, set))]
        self.assertEqual(mutable, [],
                         f"the epoch module holds mutable module state: "
                         f"{mutable}")
        # ...and the rejected module is GONE, not merely unused.
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module(
                "hockey_scheduler.services.context_read_cancellations")


# ==========================================================================
# THE WIRING — real ThreadingHTTPServer, real sessions, real store.
# ==========================================================================
class ContextReadEpochBase(ContextGateFixtureBase):
    """FIXTURE AND SEAMS ONLY, no test methods — the same split, for the same
    reason, as ``ContextGateFixtureBase`` above it: a sibling suite must be
    able to reuse the machinery (the arrival park, the service watch, the
    epoch helpers) without silently re-running and re-timing every case in
    this file on its own classes. ``tests/test_context_epoch_lifecycle.py``
    does exactly that for the SEASON-LIFECYCLE dimension of the epoch; the
    switch-dimension cases live on ``ContextReadCancelHandoffCases`` below.

    Reuses the #415 fixture (a real server, real session cookies, the
    Program/Season builders and the gate assertions) so the suites cannot
    drift apart about what a scoped read is."""

    VENUE_ROUTES = ("venue-candidates", "venue-access")

    # -- seams --------------------------------------------------------------
    @contextmanager
    def _arrival_park(self, path):
        """Hold the NEXT GET of exactly ``path`` at the TOP of ``do_GET``,
        strictly ABOVE ``CONTEXT_GATE.arrive()``.

        THE PLACEMENT IS THE INSTRUMENT. While the request is held the gate
        holds no ticket for it, so a switch arriving meanwhile waits for
        nothing and commits — which is precisely what happens when a browser
        has dispatched a read that is still sitting in its own queue. Parking
        any lower would re-create #415's 'already inside the server' case,
        which is a different interleaving and is covered by that file.

        ONE-SHOT and path-exact, so the switch's own requests and the churn
        below run unimpeded. Installed on the Handler CLASS (there is no
        instance to shadow — the server builds one per connection) and removed
        in a ``finally``.
        """
        original = self.srv.Handler.do_GET
        park = _Park()
        state = {"armed": path, "reached_gate": None}
        lock = threading.Lock()

        def wrapper(handler):
            target = handler.path.split("?", 1)[0]
            with lock:
                mine = state["armed"] is not None and target == state["armed"]
                if mine:
                    state["armed"] = None
                    state["reached_gate"] = False
            if mine:
                park.hold()
                with lock:
                    state["reached_gate"] = True
            return original(handler)

        wrapper.__name__ = "do_GET"
        self.srv.Handler.do_GET = wrapper
        try:
            yield park, state
        finally:
            self.srv.Handler.do_GET = original
            park.let_go()

    def _watch_service(self, method_name):
        """Record every season_id the named ``ApiService`` read is called with.

        The NEGATIVE observation the discard needs: 'the ceiling was not
        evaluated' has to be a measurement, not an inference from a status
        code. A 204 with the service untouched is a different fact from a 204
        produced after the service ran.
        """
        seen = []

        def factory(original):
            def wrapper(sid, *a, **kw):
                seen.append(sid)
                return original(sid, *a, **kw)
            return wrapper

        self._wrap(self.api, method_name, factory)
        return seen

    _SERVICE_FOR = {"venue-candidates": "get_venue_grant_candidates",
                    "venue-access": "list_season_venue_access"}

    # -- helpers ------------------------------------------------------------
    def _epoch(self, user_id):
        """The epoch of the row as it stands NOW, derived the same way the
        server derives it."""
        return context_epoch(self.api.store, user_id)

    def _epoch_from_api(self, client):
        """The epoch as the CLIENT learns it — off ``GET /api/context``.

        Used wherever a case is standing in for a browser, so the value under
        test is the one the wire actually carries rather than one the test
        computed for itself. ``_epoch`` above is used only where the case is
        making a statement about the derivation.
        """
        status, raw, body = self._req(client, "GET", "/api/context")
        self.assertEqual(status, 200, raw)
        self.assertIn("context_epoch", body,
                      f"/api/context did not carry an epoch, so no client "
                      f"could echo one: {raw}")
        self.assertRegex(body["context_epoch"], _HEX32, raw)
        return body["context_epoch"]

    def _scoped_read(self, client, season_id, kind="venue-candidates",
                     epoch=None):
        headers = {} if epoch is None else {CONTEXT_EPOCH_HEADER: epoch}
        return self._req(client, "GET",
                         f"/api/v2/setup/seasons/{season_id}/{kind}",
                         headers=headers)

    def _read_thread(self, client, season_id, kind, epoch, out):
        def run():
            out["result"] = self._scoped_read(client, season_id, kind, epoch)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def _assert_no_ticket_while_parked(self, why):
        """The read is inside the process but INVISIBLE TO THE GATE — which is
        what makes this the late-arrival interleaving rather than #415's. If it
        held a ticket the switch below would wait for it, and the case would be
        measuring the wrong thing entirely."""
        stats = self._gate().stats()
        self.assertEqual(stats["readers"], 0, f"{why}: {stats}")
        self.assertEqual(stats["writers"], 0, f"{why}: {stats}")

    def _late_arrival(self, kind, epoch_choice, expect_status,
                      expect_service_called, tag="Late"):
        """The whole interleaving, once, and the only place it is written.

        ``epoch_choice`` is called with (client, user_id) AFTER the tuple is
        selected and BEFORE the read is dispatched, and returns the epoch the
        read will echo — or None for 'send no header at all', which is the
        built-in control that makes every other outcome falsifiable.
        """
        fx = self._program_with_two_seasons(tag)
        username, user_id = self._operator(tag.lower())
        reader, switcher = self._login(username), self._login(username)
        self._select(reader, fx["program_id"], fx["s1"])
        self.assertEqual(self._persisted(user_id),
                         (fx["program_id"], fx["s1"]))
        # The read answers 200 RIGHT NOW, so a 204 later cannot be explained by
        # the fixture, the route or the role.
        baseline, raw, _ = self._scoped_read(reader, fx["s1"], kind)
        self.assertEqual(baseline, 200,
                         f"the fixture cannot even answer this read: {raw}")

        echoed = epoch_choice(reader, user_id)
        service = self._watch_service(self._SERVICE_FOR[kind])
        path = f"/api/v2/setup/seasons/{fx['s1']}/{kind}"
        out = {}
        with self._arrival_park(path) as (park, state):
            thread = self._read_thread(reader, fx["s1"], kind, echoed, out)
            self.assertTrue(park.arrived.wait(PATIENCE),
                            "the read never reached the server")
            self._assert_no_ticket_while_parked("while the read was parked")
            before = len(service)

            # THE SWITCH GOES FIRST AND COMPLETES, with the read still held.
            # This is the OPPOSITE of #415's case, and deliberately: there the
            # switch must be unable to commit because the read was inside the
            # server; here it must commit, because the read is not.
            self._select(switcher, fx["program_id"], fx["s2"])
            self.assertEqual(self._persisted(user_id),
                             (fx["program_id"], fx["s2"]),
                             "the switch did not commit while the read was "
                             "held — this case measured nothing")
            self.assertFalse(park.release.is_set())

            park.let_go()
            thread.join(PATIENCE)

        self.assertIn("result", out, "the late-arriving read never returned")
        status, raw, body = out["result"]
        self.assertEqual(status, expect_status,
                         f"the late-arriving {kind} read answered {status}: "
                         f"{raw!r}")
        if expect_status == 204:
            self.assertEqual(raw, "",
                             f"a discard must carry no body, got {raw!r}")
            self.assertEqual(body, {})
        reached = [s for s in service[before:] if s == fx["s1"]]
        if expect_service_called:
            self.assertTrue(reached,
                            "the read never reached ApiService, so its status "
                            "did not come from the ceiling")
        else:
            self.assertEqual(
                reached, [],
                "the discarded read reached ApiService, so the exact-Season "
                "ceiling WAS evaluated for it; a discard must short-circuit in "
                "FRONT of the ceiling, not after it")
        self.assertTrue(state["reached_gate"],
                        "the parked read never continued into do_GET")
        self._assert_gate_is_clean("after the late arrival")


class ContextReadCancelHandoffCases(ContextReadEpochBase):
    """The switch-dimension cases themselves. Split from the fixture above
    only so the fixture can be reused by the lifecycle-dimension suite; every
    case, every assertion and every store class below is unchanged."""

    # ======================================================================
    # 1. THE DEFECT
    # ======================================================================
    def test_a_read_dispatched_before_the_switch_and_arriving_after_is_discarded(self):
        """RED WITHOUT THE COMPARISON: 404. GREEN WITH IT: 204, and the service
        is never called.

        The read echoes the epoch it was rendered under, the switch commits
        while it is still in the queue, and it then arrives into a server whose
        selection has moved. Without the comparison in
        ``Handler._read_under_context_gate`` this is exactly the CI failure —
        a correct 404 for a question the operator had already withdrawn.
        """
        for kind in self.VENUE_ROUTES:
            with self.subTest(route=kind):
                self._late_arrival(
                    kind, lambda client, uid: self._epoch_from_api(client),
                    expect_status=204, expect_service_called=False,
                    tag=f"Late{kind[:5].title()}")

    def test_the_same_interleaving_without_an_epoch_is_the_untouched_404(self):
        """THE CONTROL that makes the case above falsifiable, and requirement 4
        in one: an absent header must behave EXACTLY as it did before any of
        this existed.

        Same fixture, same park, same switch, same instant — only the header is
        withheld. The 404 must come back, and it must come back FROM THE
        CEILING (the service is reached). If this ever goes green the suite is
        measuring something other than the epoch: a build that stopped refusing
        late reads generally would be a widened ceiling wearing the fix's
        clothes.
        """
        for kind in self.VENUE_ROUTES:
            with self.subTest(route=kind):
                self._late_arrival(
                    kind, lambda client, uid: None,
                    expect_status=404, expect_service_called=True,
                    tag=f"Bare{kind[:5].title()}")

    # ======================================================================
    # 2. THE EVICTION-IMPOSSIBILITY CASE — the point of the redesign
    # ======================================================================
    def test_no_elapsed_time_and_no_churn_can_turn_the_discard_back_into_a_404(self):
        """THE CASE THE REJECTED DESIGN FAILED, driven three ways.

        The owner's ruling was that eviction or expiry must never restore the
        404, and that 'the TTL is longer than the gate's wait' is not an
        argument because network arrival is bounded by neither. So this case
        puts the read through everything the old design could not survive:

          * CHURN past the magnitudes its caps were expressed in —
            ``CHURN_USERS`` other operators' selections written and rewritten
            (its per-process LRU held 256 buckets), ``CHURN_HTTP_SWITCHES`` real
            POSTs by another signed-in operator through the gate, and
            ``CHURN_READS`` further scoped reads by THIS operator (its per-user
            FIFO held 64).
          * A REAL ELAPSED DELAY between dispatch and arrival, tunable to any
            value. Under retention, anything past the TTL is a 404.
          * And the part that actually settles it: the COMPARED VALUE is
            asserted byte-identical before and after both. The decision is
            'echoed == current'; if `current` cannot move, there is no quantity
            of time or churn the outcome could depend on. That is what makes
            this an impossibility rather than a large-enough margin.
        """
        fx = self._program_with_two_seasons("Churn")
        username, user_id = self._operator("churn")
        reader, switcher = self._login(username), self._login(username)
        self._select(reader, fx["program_id"], fx["s1"])
        echoed = self._epoch_from_api(reader)

        service = self._watch_service("get_venue_grant_candidates")
        path = f"/api/v2/setup/seasons/{fx['s1']}/venue-candidates"
        out = {}
        with self._arrival_park(path) as (park, state):
            thread = self._read_thread(reader, fx["s1"], "venue-candidates",
                                       echoed, out)
            self.assertTrue(park.arrived.wait(PATIENCE))
            self._assert_no_ticket_while_parked("parked before the churn")
            before = len(service)

            self._select(switcher, fx["program_id"], fx["s2"])
            after_switch = self._epoch(user_id)
            self.assertNotEqual(after_switch, echoed,
                                "the switch did not move the epoch, so this "
                                "case is not testing a superseded read")

            # ---- CHURN, past every cap the rejected registry had ----------
            self._churn_other_users()
            for i in range(CHURN_READS):
                season = fx["s2"] if i % 2 else fx["s3"]
                self._scoped_read(reader, season, "venue-candidates")

            # ---- ELAPSED TIME, the variable under test ---------------------
            time.sleep(ARBITRARY_DELAY_SECONDS)

            # ---- THE IMPOSSIBILITY ARGUMENT, as an assertion ---------------
            # Nothing about this user's persisted row changed, so the value the
            # comparison reads is the value it would have read a millisecond
            # after the switch. There is no cache to have been evicted from and
            # no deadline to have passed, so no larger delay and no heavier
            # churn can reach a different verdict.
            self.assertEqual(
                self._epoch(user_id), after_switch,
                "the compared epoch MOVED under churn or elapsed time — "
                "something is retaining state, and the rejected design's "
                "failure mode is reachable again")
            self.assertEqual(
                epoch_verdict(self.api.store, user_id, echoed), EPOCH_MISMATCH,
                "the superseded read stopped being recognised as superseded")

            park.let_go()
            thread.join(PATIENCE)

        status, raw, _ = out["result"]
        self.assertEqual(
            status, 204,
            f"after {CHURN_USERS} other operators switched "
            f"{CHURN_SWITCHES_EACH}x, {CHURN_READS} further reads, and "
            f"{ARBITRARY_DELAY_SECONDS}s of delay, the late arrival answered "
            f"{status} instead of 204. Retention has come back: this is the "
            f"exact failure the epoch design exists to make unconstructible. "
            f"{raw!r}")
        self.assertEqual(
            [s for s in service[before:] if s == fx["s1"]], [],
            "the discarded read reached ApiService after the churn")
        self.assertTrue(state["reached_gate"])
        self._assert_gate_is_clean("after churn")

    def _churn_other_users(self):
        """Unrelated operators switching, in both shapes that matter.

        BULK, at the service layer: ``CHURN_USERS`` distinct user ids each
        writing ``CHURN_SWITCHES_EACH`` real ``ActiveContext`` rows. Deliberately
        not ``CHURN_USERS`` HTTP logins — that is ``CHURN_USERS`` PBKDF2 hashes
        per store class and measures the test harness, not the mechanism. The
        persisted churn is identical, and persisted churn is what a
        retention-based design would have been evicting on.

        AND A REAL ONE, through HTTP: a second signed-in operator POSTing
        ``CHURN_HTTP_SWITCHES`` times, so the writer side of the gate is
        genuinely transited by somebody else while the read is held. The bulk
        half alone would never take the gate at all.
        """
        svc = self.api.setup
        program = svc.create_program(f"Churn {uuid.uuid4().hex[:6]}",
                                     timezone_name="America/Toronto")
        seasons = [svc.create_season(program.id, f"Churn S{i}").id
                   for i in range(2)]
        for i in range(CHURN_USERS):
            uid = f"churn_user_{i}_{uuid.uuid4().hex[:8]}"
            for k in range(CHURN_SWITCHES_EACH):
                result = self.api.set_active_context(
                    uid, Role.LEAGUE_ADMIN, {}, program.id,
                    seasons[(i + k) % len(seasons)])
                self.assertNotIn("error", result, result)
            self.assertIsNotNone(self.api.store.get_active_context(uid),
                                 "the churn wrote nothing, so this case is "
                                 "not churning anything")
        noisy_name, _noisy_id = self._operator("noisy")
        noisy = self._login(noisy_name)
        for k in range(CHURN_HTTP_SWITCHES):
            self._select(noisy, program.id, seasons[k % len(seasons)])

    # ======================================================================
    # 3. CROSS-USER
    # ======================================================================
    def test_one_operators_switch_does_not_discard_anothers_read(self):
        """The obvious attack and the obvious bug, in one case.

        The epoch is derived per-user from that user's own row, and compared
        against the ``user_id`` resolved from the SESSION. So operator A's
        switching — repeatedly, while B's read is held — cannot move B's epoch
        and cannot discard B's read. If it could, one signed-in account could
        blank another's Setup surface at will.
        """
        fx_a = self._program_with_two_seasons("XUserA")
        fx_b = self._program_with_two_seasons("XUserB")
        user_a, id_a = self._operator("xa")
        user_b, id_b = self._operator("xb")
        a_client, b_client = self._login(user_a), self._login(user_b)
        self._select(a_client, fx_a["program_id"], fx_a["s1"])
        self._select(b_client, fx_b["program_id"], fx_b["s1"])
        b_epoch = self._epoch_from_api(b_client)

        path = f"/api/v2/setup/seasons/{fx_b['s1']}/venue-candidates"
        out = {}
        with self._arrival_park(path) as (park, _state):
            thread = self._read_thread(b_client, fx_b["s1"],
                                       "venue-candidates", b_epoch, out)
            self.assertTrue(park.arrived.wait(PATIENCE))
            for _ in range(6):
                self._select(a_client, fx_a["program_id"], fx_a["s2"])
                self._select(a_client, fx_a["program_id"], fx_a["s1"])
            self.assertEqual(
                self._epoch(id_b), b_epoch,
                "operator A's switching moved operator B's epoch")
            park.let_go()
            thread.join(PATIENCE)

        status, raw, body = out["result"]
        self.assertEqual(status, 200,
                         f"B's read was discarded by A's switch: {raw}")
        self.assertEqual(body.get("season_id", fx_b["s1"]), fx_b["s1"], raw)
        self.assertEqual(self._persisted(id_a),
                         (fx_a["program_id"], fx_a["s1"]))
        self._assert_gate_is_clean("after the cross-user case")

    def test_another_users_current_epoch_discards_your_own_read_and_nothing_else(self):
        """A token is not a capability. Presenting SOMEONE ELSE's current epoch
        is a mismatch against your own row, so it throws away YOUR request — it
        cannot reach their data, cannot reach their reads, and cannot widen
        anything. Tested because 'confers no authority' is the property the
        whole design has to be read against."""
        fx_a = self._program_with_two_seasons("StealA")
        fx_b = self._program_with_two_seasons("StealB")
        user_a, id_a = self._operator("stealera")
        user_b, id_b = self._operator("stealerb")
        a_client, b_client = self._login(user_a), self._login(user_b)
        self._select(a_client, fx_a["program_id"], fx_a["s1"])
        self._select(b_client, fx_b["program_id"], fx_b["s1"])
        stolen = self._epoch_from_api(b_client)

        status, raw, _ = self._scoped_read(a_client, fx_a["s1"],
                                           epoch=stolen)
        self.assertEqual(status, 204,
                         f"another user's epoch was accepted as A's own: {raw}")
        # ...and B is entirely unaffected: same epoch, same answers.
        self.assertEqual(self._epoch(id_b), stolen)
        b_status, b_raw, _ = self._scoped_read(b_client, fx_b["s1"],
                                               epoch=stolen)
        self.assertEqual(b_status, 200, b_raw)
        # A's own reads are untouched the moment it stops presenting the stolen
        # value — the discard is per-request, never a poisoned route.
        again, again_raw, _ = self._scoped_read(a_client, fx_a["s1"])
        self.assertEqual(again, 200, again_raw)

    # ======================================================================
    # 4./5. THE HEADER CANNOT SERVE WHAT THE CEILING WOULD REFUSE
    # ======================================================================
    def test_an_absent_epoch_leaves_every_answer_exactly_as_it_was(self):
        """No client is required to participate, and none is penalised for not
        doing so: with no header the selected Season answers 200 and every
        sibling answers the generic 404, which is main's behaviour verbatim."""
        fx = self._program_with_two_seasons("Absent")
        username, user_id = self._operator("absent")
        client = self._login(username)
        self._select(client, fx["program_id"], fx["s1"])
        for kind in self.VENUE_ROUTES:
            self._assert_reads_agree_with(client, fx, fx["s1"])
            for season, expected in ((fx["s1"], 200), (fx["s2"], 404),
                                     (fx["s3"], 404)):
                status, raw, _ = self._scoped_read(client, season, kind)
                self.assertEqual(status, expected,
                                 f"{kind} for {season} with no epoch: {raw}")
        # An EMPTY or whitespace header is the same statement as no header —
        # a client that has not learned an epoch yet must not be worse off than
        # one that never will.
        for blank in ("", "   "):
            status, raw, _ = self._scoped_read(client, fx["s1"], epoch=blank)
            self.assertEqual(status, 200, f"blank epoch {blank!r}: {raw}")

    def test_a_current_epoch_does_not_buy_a_single_byte_the_ceiling_refuses(self):
        """THE ANTI-AUTHORITY CASE. A valid, current epoch is not permission:
        the exact-Season ceiling still refuses a sibling Season of the caller's
        OWN Program with the same generic 404 as a Season that never existed,
        and the two refusals stay indistinguishable so the header cannot be
        turned into an existence oracle either."""
        fx = self._program_with_two_seasons("Ceiling")
        username, user_id = self._operator("ceiling")
        client = self._login(username)
        self._select(client, fx["program_id"], fx["s1"])
        current = self._epoch_from_api(client)
        ghost = "season_does_not_exist_999999"

        for kind in self.VENUE_ROUTES:
            ok, ok_raw, _ = self._scoped_read(client, fx["s1"], kind, current)
            self.assertEqual(ok, 200, ok_raw)
            sib, sib_raw, sib_body = self._scoped_read(client, fx["s2"], kind,
                                                       current)
            miss, miss_raw, miss_body = self._scoped_read(client, ghost, kind,
                                                          current)
            self.assertEqual(sib, 404,
                             f"a current epoch widened the {kind} ceiling to a "
                             f"non-selected sibling Season: {sib_raw}")
            self.assertEqual(miss, 404, miss_raw)
            self.assertEqual(
                sib_raw.replace(fx["s2"], "<S>"),
                miss_raw.replace(ghost, "<S>"),
                f"the {kind} refusals differ, so the epoch turned the ceiling "
                f"into an existence oracle: {sib_raw} vs {miss_raw}")

    def test_a_forged_or_garbage_epoch_discards_and_never_serves(self):
        """FAILS CLOSED. Anything present that is not exactly the current token
        is a discard — including values shaped like a token and values that
        could not be one. None of them serves data, for the selected Season or
        any other, so no input to this header is worth forging."""
        fx = self._program_with_two_seasons("Forge")
        username, user_id = self._operator("forge")
        client = self._login(username)
        self._select(client, fx["program_id"], fx["s1"])
        current = self._epoch_from_api(client)

        forgeries = ["0" * 32, "f" * 32, current[:-1] + ("0" if current[-1]
                                                         != "0" else "1"),
                     current.upper(), current[:16], current + "ff",
                     "not-a-token", "../../secret", "%s" % user_id]
        for kind in self.VENUE_ROUTES:
            for forged in forgeries:
                for season in (fx["s1"], fx["s2"], "season_ghost_1"):
                    status, raw, _ = self._scoped_read(client, season, kind,
                                                       forged)
                    self.assertEqual(
                        status, 204,
                        f"forged epoch {forged!r} on {kind}/{season} answered "
                        f"{status} — a discard is the ONLY thing this header "
                        f"may cause: {raw}")
                    self.assertEqual(raw, "", raw)
        # And the real one still works, so the case is not passing because the
        # route broke.
        status, raw, _ = self._scoped_read(client, fx["s1"], epoch=current)
        self.assertEqual(status, 200, raw)

    def test_a_stale_epoch_discards_even_the_currently_selected_season(self):
        """The discard is decided by WHEN the read was rendered, never by what
        it names. A read rendered under the old selection is thrown away even
        when it happens to name the Season that is now selected — because its
        answer would be painted into a page built from a tuple that has moved,
        which is the same defect in a friendlier disguise."""
        fx = self._program_with_two_seasons("Stale")
        username, user_id = self._operator("stale")
        client = self._login(username)
        self._select(client, fx["program_id"], fx["s2"])
        stale = self._epoch_from_api(client)
        self._select(client, fx["program_id"], fx["s1"])
        self._select(client, fx["program_id"], fx["s2"])   # back to the same
        status, raw, _ = self._scoped_read(client, fx["s2"], epoch=stale)
        self.assertEqual(
            status, 204,
            f"a read rendered before an A->B->A round trip was served: {raw}")
        current, raw2, _ = self._scoped_read(
            client, fx["s2"], epoch=self._epoch_from_api(client))
        self.assertEqual(current, 200, raw2)

    # ======================================================================
    # 6. EVERY LISTED ROUTE, NOT ONLY THE TWO CI HIT
    # ======================================================================
    def test_the_other_two_listed_routes_discard_on_a_stale_epoch_too(self):
        """``CONTEXT_SCOPED_READ_ROUTES`` is the authoritative definition of a
        context-scoped read, and every entry gets the same treatment.
        ``app.js`` does not enrol these two today, so no shipped request
        carries the header on them — this case is what stops the four routes
        drifting apart while that is true."""
        fx = self._program_with_two_seasons("Routes")
        username, user_id = self._operator("routes")
        client = self._login(username)
        self._select(client, fx["program_id"], fx["s1"])
        division = self._division_with_teams(fx, fx["s1"])
        scenario = self._scenario_in(fx, fx["s1"])
        stale = self._epoch_from_api(client)

        for path in (f"/api/standings/{division}",
                     f"/api/scheduler/scenarios/{scenario}"):
            ok, raw, _ = self._req(client, "GET", path,
                                   headers={CONTEXT_EPOCH_HEADER: stale})
            self.assertEqual(ok, 200, f"{path} under a current epoch: {raw}")

        self._select(client, fx["program_id"], fx["s2"])
        for path in (f"/api/standings/{division}",
                     f"/api/scheduler/scenarios/{scenario}"):
            status, raw, _ = self._req(client, "GET", path,
                                       headers={CONTEXT_EPOCH_HEADER: stale})
            self.assertEqual(
                status, 204,
                f"{path} was judged against the new tuple instead of being "
                f"discarded: {raw}")
            self.assertEqual(raw, "", raw)
            # ...and with no epoch it is the untouched pre-#159 answer.
            bare, bare_raw, _ = self._req(client, "GET", path)
            self.assertNotEqual(bare, 204, bare_raw)

    # ======================================================================
    # 7. NOTHING LEAKS, NOTHING BLOCKS
    # ======================================================================
    def test_discards_leak_no_gate_participants_and_time_nothing_out(self):
        """A discard returns from inside the gate's shared hold, on a path that
        did not exist before. If it ever returned without releasing, or waited
        on something, this is where it shows: the counters must be back to zero
        and the gate's timeout tally must not have moved."""
        fx = self._program_with_two_seasons("Leak")
        username, user_id = self._operator("leak")
        client = self._login(username)
        self._select(client, fx["program_id"], fx["s1"])
        stale = self._epoch_from_api(client)
        self._select(client, fx["program_id"], fx["s2"])
        timeouts_before = self._gate().stats()["timeouts"]

        for i in range(40):
            kind = self.VENUE_ROUTES[i % 2]
            status, raw, _ = self._scoped_read(client, fx["s1"], kind, stale)
            self.assertEqual(status, 204, raw)
        stats = self._assert_gate_is_clean("after 40 discards")
        self.assertEqual(stats["timeouts"], timeouts_before,
                         f"a discard waited on something: {stats}")
        # Interleaved with real reads, so a leak that only shows when the two
        # share the gate is reachable too.
        for i in range(20):
            self.assertEqual(
                self._scoped_read(client, fx["s2"], epoch=stale)[0], 204)
            self.assertEqual(self._scoped_read(client, fx["s2"])[0], 200)
        self._assert_gate_is_clean("after interleaved discards and reads")

    def test_a_read_already_inside_the_server_keeps_the_415_ordering(self):
        """#415 IS NOT TRADED AWAY. A read the gate ordered AHEAD of a switch
        holds the gate, so the switch waits for it, the row does not move
        underneath it, and its echoed epoch still matches when it is compared —
        it keeps the 200 that #415 exists to give it.

        This is the interleaving the epoch must NOT touch, and it is asserted
        here as well as in ``test_context_switch_server_exit.py`` because the
        comparison added by this change sits inside that very hold.
        """
        fx = self._program_with_two_seasons("Inside")
        username, user_id = self._operator("inside")
        reader, switcher = self._login(username), self._login(username)
        self._select(reader, fx["program_id"], fx["s1"])
        echoed = self._epoch_from_api(reader)

        commit = {}
        self._instrument_commit(commit)
        out = {}
        with self._read_parked_in("get_venue_grant_candidates", fx["s1"]) \
                as (park, exited):
            thread = self._read_thread(reader, fx["s1"], "venue-candidates",
                                       echoed, out)
            self.assertTrue(park.arrived.wait(PATIENCE))
            switch = {}
            st = self._switch_thread(switcher, fx["program_id"], fx["s2"],
                                     switch)
            # The switch must NOT have committed: this read holds the gate.
            self.assertTrue(
                _wait(lambda: self._gate().stats()["waiting_writers"] == 1,
                      timeout=PATIENCE),
                f"the switch never queued behind the read: "
                f"{self._gate().stats()}")
            self.assertEqual(self._persisted(user_id),
                             (fx["program_id"], fx["s1"]))
            park.let_go()
            thread.join(PATIENCE)
            st.join(PATIENCE)

        status, raw, body = out["result"]
        self.assertEqual(status, 200,
                         f"the epoch comparison broke #415's guarantee for a "
                         f"read that was already inside the server: {raw}")
        self.assertEqual(body.get("season_id", fx["s1"]), fx["s1"], raw)
        self.assertEqual(switch["result"][0], 200, switch["result"][1])
        self.assertEqual(self._persisted(user_id),
                         (fx["program_id"], fx["s2"]))
        self._assert_gate_is_clean("after the #415 ordering case")

    # ======================================================================
    # 8. THE PAYLOADS THE CLIENT LEARNS ITS CONTEXT FROM
    # ======================================================================
    def test_every_context_payload_carries_the_epoch_and_it_agrees(self):
        """A read can only echo what a render pass was told. All three places
        the client learns its context must carry the SAME epoch at the same
        instant — a disagreement between them would let the page pair a tuple
        from one payload with an epoch from another, which is the pairing the
        client-side rule exists to make impossible."""
        fx = self._program_with_two_seasons("Payload")
        username, user_id = self._operator("payload")
        client = self._login(username)
        posted = self._select(client, fx["program_id"], fx["s1"])
        self.assertIn("context_epoch", posted,
                      f"POST /api/context did not echo an epoch: {posted}")

        _, _, got = self._req(client, "GET", "/api/context")
        _, _, options = self._req(client, "GET", "/api/context/options")
        for label, payload in (("POST /api/context", posted),
                               ("GET /api/context", got),
                               ("GET /api/context/options", options)):
            self.assertIn("context_epoch", payload, f"{label}: {payload}")
            self.assertRegex(payload["context_epoch"], _HEX32, label)
            self.assertEqual(payload["context_epoch"], self._epoch(user_id),
                             f"{label} disagreed with the persisted row")
        self.assertEqual(options.get("selected", {}).get("season_id"),
                         fx["s1"], options)

        # ...and the POST's echo is the epoch of the row THAT POST WROTE, so a
        # client adopting it beside `selected` is adopting a matched pair.
        moved = self._select(client, fx["program_id"], fx["s2"])
        self.assertNotEqual(moved["context_epoch"], posted["context_epoch"])
        self.assertEqual(moved["context_epoch"], self._epoch(user_id))
        # A read echoing the epoch the POST just handed back is served.
        status, raw, _ = self._scoped_read(client, fx["s2"],
                                           epoch=moved["context_epoch"])
        self.assertEqual(status, 200, raw)

    def test_a_switch_landing_mid_payload_leaves_a_STALE_epoch_not_a_stale_tuple(self):
        """THE ORDER THE PAYLOAD IS ASSEMBLED IN, driven rather than reasoned
        about.

        Nothing gates ``GET /api/context/options``, so a switch for the SAME
        account — a second tab, another device — can commit between the two
        reads that response is built from. Only one order of those reads fails
        safe, and this case forces the interleaving to prove it is the one in
        the code:

          * derive the epoch FIRST  -> the payload carries the NEW tuple with
            the OLD epoch. The client renders the new tuple and echoes a stale
            epoch, so its reads are DISCARDED and it re-reads. Safe.
          * derive the epoch LAST   -> the payload would carry the OLD tuple
            with the NEW epoch. The client renders a selection it has left
            while echoing the epoch of the one it is on, so its reads are
            ADMITTED and judged against a tuple they were not rendered under —
            the exact 404 this mechanism exists to prevent, reintroduced by the
            mechanism itself.

        The final assertion is the one that matters: whatever the payload hands
        back, echoing it must not get data served.
        """
        fx = self._program_with_two_seasons("MidPayload")
        username, user_id = self._operator("midpayload")
        client = self._login(username)
        self._select(client, fx["program_id"], fx["s1"])
        epoch_s1 = self._epoch(user_id)
        fired = []

        def factory(original):
            def wrapper(*a, **kw):
                # Runs INSIDE the handler, after it has derived the epoch and
                # before it reads the tuple — which is the whole window.
                if not fired:
                    fired.append(1)
                    moved = self.api.set_active_context(
                        user_id, Role.LEAGUE_ADMIN, {}, fx["program_id"],
                        fx["s2"])
                    self.assertNotIn("error", moved, moved)
                return original(*a, **kw)
            return wrapper

        self._wrap(self.api, "get_context_options", factory)
        status, raw, body = self._req(client, "GET", "/api/context/options")
        self.assertEqual(status, 200, raw)
        self.assertTrue(fired, "the seam never ran, so nothing was interleaved")
        self.assertEqual(body.get("selected", {}).get("season_id"), fx["s2"],
                         f"the switch did not land inside the payload: {raw}")
        self.assertEqual(
            body.get("context_epoch"), epoch_s1,
            "the payload paired the NEW tuple with the NEW epoch, so the epoch "
            "was derived AFTER the tuple — a client rendering this would have "
            "its stale reads ADMITTED instead of discarded")
        st, st_raw, _ = self._scoped_read(client, fx["s2"],
                                          epoch=body["context_epoch"])
        self.assertEqual(
            st, 204,
            f"a read echoing exactly what this payload handed back was SERVED; "
            f"the assembly order is not fail-safe: {st_raw}")

    def test_the_epoch_survives_a_process_restart_of_the_derivation(self):
        """PROPERTY 1 through the real store, on every backend: the token is a
        function of PERSISTED state, so re-importing the deriving module — the
        closest a test gets to 'a different process reading the same row' —
        produces the identical value. A per-process registry could not."""
        fx = self._program_with_two_seasons("Restart")
        username, user_id = self._operator("restart")
        client = self._login(username)
        self._select(client, fx["program_id"], fx["s1"])
        before = self._epoch_from_api(client)

        module = importlib.import_module(
            "hockey_scheduler.services.context_epoch")
        reloaded = importlib.reload(module)
        self.assertEqual(reloaded.context_epoch(self.api.store, user_id),
                         before,
                         "a fresh import of the deriving module produced a "
                         "different token for the same persisted row")
        # The row, read straight out of the store, is what it hashed.
        row = self.api.store.get_active_context(user_id)
        self.assertIsInstance(row, ActiveContext)
        self.assertEqual(row.season_id, fx["s1"])


class MemoryContextReadEpochTest(ContextReadCancelHandoffCases, unittest.TestCase):
    STORE_URL = None


class SqliteContextReadEpochTest(ContextReadCancelHandoffCases, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, path = tempfile.mkstemp(suffix=".db", prefix="hs_ctxepoch_")
        os.close(fd)
        cls._db_path = path
        cls.STORE_URL = path
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        try:
            os.unlink(cls._db_path)
        except OSError:
            pass


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL not configured (TEST_DATABASE_URL)")
class PostgresContextReadEpochTest(ContextReadCancelHandoffCases, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.STORE_URL = os.environ["TEST_DATABASE_URL"]
        super().setUpClass()
