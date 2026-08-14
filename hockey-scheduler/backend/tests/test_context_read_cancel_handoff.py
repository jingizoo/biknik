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
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import Role, SeasonStatus
from hockey_scheduler.domain.setup_models import ActiveContext
from hockey_scheduler.services import context_epoch as _context_epoch_module
from hockey_scheduler.services.context_epoch import (
    CONTEXT_EPOCH_HEADER, EPOCH_ABSENT, EPOCH_MATCH, EPOCH_MISMATCH,
    context_epoch, epoch_secret, epoch_verdict, is_epoch_token)

from test_context_switch_server_exit import (
    COMMIT_WINDOW, PATIENCE, ContextGateFixtureBase, _Park, _wait)

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


@contextmanager
def _env(**overrides):
    """Temporarily set/clear environment variables, restoring exactly the
    prior state afterward — including ABSENCE, which a plain assignment (or
    ``patch.dict`` with ``clear=False``) cannot tell apart from "was set to
    the empty string". A value of ``None`` means "ensure this key is unset
    for the duration"."""
    sentinel = object()
    previous = {k: os.environ.get(k, sentinel) for k in overrides}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in previous.items():
            if v is sentinel:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _FakeAxis:
    """A stand-in for a resolved Program or League: just enough shape
    (``.id``) for the pure-function cases to hash against. Used instead of a
    real store/``ContextService`` so those cases test the DERIVATION itself —
    no HTTP, no schema, no backend differences to explain away. Resolving the
    EFFECTIVE tuple under one snapshot is ``ContextService``'s job (#159
    review findings 2+3), exercised separately by the WIRING cases below."""

    def __init__(self, id):
        self.id = id


class _FakeSeason(_FakeAxis):
    """A stand-in for a resolved Season: ``.id`` plus the two fields
    ``_season_lifecycle_fields`` reads. ``status`` defaults to ACTIVE so a
    case that only cares about identity does not have to spell out a
    lifecycle; ``tests/test_context_epoch_lifecycle.py`` varies the lifecycle
    instead of the id for its own cases."""

    def __init__(self, id, status=SeasonStatus.ACTIVE, archived_at=None):
        super().__init__(id)
        self.status = status
        self.archived_at = archived_at


# ==========================================================================
# THE DERIVATION ITSELF — no HTTP, no store, no threads.
# ==========================================================================
class ContextEpochDerivationTest(unittest.TestCase):
    """The four properties the epoch has to have before any of the wiring
    below can mean anything. Store-independent on purpose: these are claims
    about a pure function of ``(user_id, generation, program, season,
    league)`` — resolving those five values under one consistent snapshot is
    ``ContextService``'s job (#159 review findings 2+3), exercised by the
    WIRING cases below; proving the DERIVATION through a server would prove
    it only for whichever backend happened to run."""

    USER_ID = "user_a"
    GENERATION = 3
    PROGRAM = _FakeAxis("program_1")
    SEASON = _FakeSeason("season_1")
    LEAGUE = _FakeAxis("league_1")

    def test_the_same_material_always_produces_the_same_token(self):
        """PROPERTY 1: stable for the same material, in this process and any
        other.

        A per-process counter or a random nonce would satisfy every other case
        in this file and fail here — and in production would invalidate every
        outstanding read on every restart.
        """
        first = context_epoch(self.USER_ID, self.GENERATION, self.PROGRAM,
                              self.SEASON, self.LEAGUE)
        again = [context_epoch(self.USER_ID, self.GENERATION, self.PROGRAM,
                               self.SEASON, self.LEAGUE) for _ in range(50)]
        self.assertTrue(all(t == first for t in again),
                        f"the token moved without the material moving: "
                        f"{set(again)}")
        # Recomputed from EQUAL-BUT-DISTINCT objects: the material is their
        # VALUES, never their identity, so a fresh resolution has to hash the
        # same as the objects that were written.
        rehydrated = context_epoch(
            "user_a", 3, _FakeAxis("program_1"), _FakeSeason("season_1"),
            _FakeAxis("league_1"))
        self.assertEqual(rehydrated, first,
                         "equal-but-distinct objects produced a different "
                         "token, so the token depends on object identity")

    def test_the_generation_moving_alone_moves_the_token(self):
        """PROPERTY 2 (#159 review finding 5), and the one a tuple-derived
        token would fail. A -> B -> A leaves the operator on the tuple they
        started from; ``ContextService`` moves the persisted GENERATION on
        every commit regardless — a read rendered before the round trip must
        not be silently READMITTED against a selection that moved twice
        underneath it, and this is what makes that true even when the tuple
        and the clock cannot be trusted to."""
        self.assertNotEqual(
            context_epoch(self.USER_ID, self.GENERATION, self.PROGRAM,
                          self.SEASON, self.LEAGUE),
            context_epoch(self.USER_ID, self.GENERATION + 1, self.PROGRAM,
                          self.SEASON, self.LEAGUE),
            "the token did not move when only the generation moved, so a "
            "switch back to the same tuple would be invisible to it")

    def test_every_axis_the_generation_and_the_owner_are_part_of_the_material(self):
        """Any one field changing must move the token. Otherwise some switch —
        Program-only, a League change, a different operator on identical data —
        would leave a stale read admissible."""
        seen = {context_epoch(self.USER_ID, self.GENERATION, self.PROGRAM,
                              self.SEASON, self.LEAGUE)}
        variants = {
            "generation": (self.GENERATION + 1, self.PROGRAM, self.SEASON,
                          self.LEAGUE),
            "program": (self.GENERATION, _FakeAxis("program_2"), self.SEASON,
                       self.LEAGUE),
            "season": (self.GENERATION, self.PROGRAM, _FakeSeason("season_2"),
                      self.LEAGUE),
            "league": (self.GENERATION, self.PROGRAM, self.SEASON,
                      _FakeAxis("league_2")),
            "season_cleared": (self.GENERATION, self.PROGRAM, None,
                              self.LEAGUE),
            "league_cleared": (self.GENERATION, self.PROGRAM, self.SEASON,
                              None),
        }
        for label, (gen, p, s, lg) in variants.items():
            token = context_epoch(self.USER_ID, gen, p, s, lg)
            self.assertNotIn(token, seen,
                             f"changing the {label} did not move the token")
            seen.add(token)
        # A DIFFERENT OWNER on byte-identical selection data. The comparison is
        # always against the session's own epoch, so a collision here could not
        # widen anything — but it would mean the token described a selection
        # rather than a selection HELD BY SOMEONE, which is not what the reads
        # are being judged against.
        other = context_epoch("user_b", self.GENERATION, self.PROGRAM,
                              self.SEASON, self.LEAGUE)
        self.assertNotIn(other, seen,
                         "two operators' identical selections collided")

    def test_no_field_separator_confusion_can_forge_a_collision(self):
        """The material is separated by a control character no id can
        contain, so no two different inputs can be rearranged into the same
        string. A naive ``"|".join`` would let ("a", "b|c") and ("a|b", "c")
        collide, and the collision would readmit a read across a switch."""
        left = context_epoch("u", 1, _FakeAxis("a"), _FakeSeason("b\x1fc"),
                             None)
        right = context_epoch("u", 1, _FakeAxis("a\x1fb"), _FakeSeason("c"),
                              None)
        self.assertNotEqual(left, right)

    def test_the_token_is_opaque_and_carries_no_identifier(self):
        """PROPERTY 3. Nothing is concatenated or encoded, so a holder of a
        token cannot read a user, Program, Season, League or generation out of
        it. (The honest limit — a party who ALREADY holds the whole material
        AND the deployment secret can recompute and confirm it — is stated in
        the module docstring of services/context_epoch.py; it discloses
        nothing that party did not supply, and confirming it confers nothing.
        ``ContextEpochSecretTest`` covers the OTHER half: without the secret,
        even the whole material is not enough — #159 review finding 4.)"""
        token = context_epoch(self.USER_ID, self.GENERATION, self.PROGRAM,
                              self.SEASON, self.LEAGUE)
        self.assertRegex(token, _HEX32)
        for secret in ("user_a", "program_1", "season_1", "league_1", "3"):
            self.assertNotIn(secret, token,
                             f"{secret!r} is readable out of the token")

    def test_a_user_with_no_resolution_still_has_an_epoch(self):
        """A brand-new operator with no authorized Program resolves to
        ``(program=None, season=None, league=None)`` at generation 0. That is
        still a state a read can be rendered under, and the FIRST switch must
        move away from it — otherwise the very first switch of a session is
        the one interleaving the mechanism does not cover."""
        absent = context_epoch("user_a", 0, None, None, None)
        self.assertRegex(absent, _HEX32)
        self.assertEqual(absent, context_epoch("user_a", 0, None, None, None))
        self.assertNotEqual(
            absent,
            context_epoch("user_a", 1, self.PROGRAM, self.SEASON, self.LEAGUE),
            "making the first selection did not move the epoch away from the "
            "no-resolution state")
        # ...and it is still per-user, so one empty resolution is not every
        # empty resolution.
        self.assertNotEqual(absent,
                            context_epoch("user_b", 0, None, None, None))

    def test_the_verdict_is_absent_match_or_mismatch_and_fails_closed(self):
        """PROPERTY 4. Absent is today's behaviour; anything present but not
        exactly current DISCARDS, including values that cannot be tokens at
        all. There is deliberately no fourth outcome: a 'malformed' branch that
        refused would give the header power over the response, which is the one
        thing it must never have."""
        current = context_epoch(self.USER_ID, self.GENERATION, self.PROGRAM,
                                self.SEASON, self.LEAGUE)
        self.assertEqual(epoch_verdict(None, current), EPOCH_ABSENT)
        self.assertEqual(epoch_verdict("", current), EPOCH_ABSENT)
        self.assertEqual(epoch_verdict("   ", current), EPOCH_ABSENT)
        self.assertEqual(epoch_verdict(current, current), EPOCH_MATCH)
        self.assertEqual(epoch_verdict(f"  {current}  ", current),
                         EPOCH_MATCH)
        for junk in ("not-a-token", current.upper(), current[:-1], current + "0",
                     "g" * 32, "../../etc/passwd", "0", "\x00" * 32):
            self.assertEqual(epoch_verdict(junk, current),
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
# THE KEYED MAC (#159 review finding 4) — no HTTP, no store, no threads.
# ==========================================================================
class ContextEpochSecretTest(unittest.TestCase):
    """``epoch_secret`` and the now-KEYED digest. Store-free for the same
    reason as ``ContextEpochDerivationTest``: these are claims about a pure
    function (given a configured secret) and about ``epoch_secret``'s own
    sourcing/fail-closed logic, not about a server.

    THE FINDING, restated precisely. The pre-fix digest was UNKEYED: a public
    personalization constant plus material that, for a fresh account with no
    saved selection, is just a sentinel and the account's own id. A deployment
    that mints ids sequentially (or any low-entropy id space) turns "I hold one
    leaked token" into "I know which account issued it" — hash every candidate
    id and compare, no request sent, no authority gained or needed. The reviewer
    demonstrated it concretely: token ``20e11328d4f20b6b28b1e150a2cdd961``
    recovered as ``user_37`` by hashing ``user_1``..``user_100``.

    THE FIX does not touch the material (that is findings 2/5's territory) —
    it keys the SAME digest with a deployment secret, so recomputing it, for
    one candidate or for a hundred, requires the secret and not merely the
    material.
    """

    def test_outside_production_the_demo_secret_applies_and_never_raises(self):
        """Zero configuration required outside production — the stdlib-only,
        no-setup test/demo/dev experience is unaffected by this finding's
        fix. Checked with APP_MODE both unset and explicitly 'demo'."""
        with _env(APP_MODE=None, HS_CONTEXT_EPOCH_SECRET=None):
            self.assertEqual(epoch_secret(), _context_epoch_module._DEMO_SECRET)
        with _env(APP_MODE="demo", HS_CONTEXT_EPOCH_SECRET=None):
            self.assertEqual(epoch_secret(), _context_epoch_module._DEMO_SECRET)

    def test_production_without_the_secret_fails_closed(self):
        """THE REQUIRED FAIL-CLOSED BEHAVIOUR. Production with the variable
        unset must refuse outright rather than silently degrade to the demo
        key (which is committed in the open) or to an unkeyed hash."""
        with _env(APP_MODE="production", HS_CONTEXT_EPOCH_SECRET=None):
            with self.assertRaises(RuntimeError):
                epoch_secret()

    def test_production_with_a_too_short_secret_fails_closed(self):
        """A configured-but-weak secret is refused just as loudly as a
        missing one — silently accepting it would be the same failure
        wearing a different cause. Checked exactly one byte under the floor
        too, so an off-by-one in the comparison cannot pass unnoticed."""
        with _env(APP_MODE="production", HS_CONTEXT_EPOCH_SECRET="short"):
            with self.assertRaises(RuntimeError):
                epoch_secret()
        just_under = "x" * (_context_epoch_module._MIN_SECRET_BYTES - 1)
        with _env(APP_MODE="production", HS_CONTEXT_EPOCH_SECRET=just_under):
            with self.assertRaises(RuntimeError):
                epoch_secret()

    def test_production_with_a_sufficient_secret_boots_and_uses_it_verbatim(self):
        at_floor = "x" * _context_epoch_module._MIN_SECRET_BYTES
        with _env(APP_MODE="production", HS_CONTEXT_EPOCH_SECRET=at_floor):
            self.assertEqual(epoch_secret(), at_floor.encode("utf-8"))

    def test_serve_sources_the_secret_before_binding_the_socket(self):
        """THE 'AT STARTUP' HALF, proven rather than asserted from reading the
        source: a fresh subprocess started with ``serve()`` in PRODUCTION and
        no secret configured must exit quickly and non-zero — never hang
        (which is what happens if the check is missing and ``serve_forever``
        is reached) and never print a stack trace from inside a request
        handler (which is what happens if the check is only reached lazily,
        on the first context-scoped request)."""
        env = dict(os.environ)
        env["APP_MODE"] = "production"
        env.pop("HS_CONTEXT_EPOCH_SECRET", None)
        # DATABASE_URL unset -> InMemoryStore, so this cannot fail for any
        # reason OTHER than the secret check: no real database, no migration,
        # nothing else for a production boot to trip over first.
        env.pop("DATABASE_URL", None)
        code = (
            "from hockey_scheduler.web.server import serve\n"
            "serve('127.0.0.1', 0)\n"
            "print('SERVED_FOREVER_UNREACHABLE')\n")
        try:
            result = subprocess.run(
                [sys.executable, "-c", code], env=env, cwd=str(BACKEND),
                capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            self.fail(
                "serve() did not exit within 15s under APP_MODE=production "
                "with no HS_CONTEXT_EPOCH_SECRET -- it reached "
                "serve_forever() instead of failing closed at startup")
        self.assertNotEqual(
            result.returncode, 0,
            f"serve() exited 0 without ever binding — expected a fail-closed "
            f"non-zero exit. stdout={result.stdout!r}")
        self.assertNotIn("SERVED_FOREVER_UNREACHABLE", result.stdout)
        self.assertIn(_context_epoch_module._SECRET_ENV, result.stderr,
                     f"the failure did not name the missing variable: "
                     f"{result.stderr}")

    def test_a_configured_secret_changes_the_token_and_stays_stable(self):
        """The digest IS a function of the secret (two different configured
        secrets over the SAME material produce different tokens), and PROPERTY
        1 survives keying (the same configured secret, called repeatedly,
        produces the same token)."""
        material = ("user_a", 3, ContextEpochDerivationTest.PROGRAM,
                    ContextEpochDerivationTest.SEASON,
                    ContextEpochDerivationTest.LEAGUE)
        with _env(HS_CONTEXT_EPOCH_SECRET="a" * 32):
            token_a1 = context_epoch(*material)
            token_a2 = context_epoch(*material)
        with _env(HS_CONTEXT_EPOCH_SECRET="b" * 32):
            token_b = context_epoch(*material)
        self.assertEqual(
            token_a1, token_a2,
            "the same configured secret produced two different tokens for "
            "the identical material — keying broke property 1")
        self.assertNotEqual(
            token_a1, token_b,
            "two different deployment secrets produced the SAME token for "
            "identical material — the digest is not actually keyed")

    def test_cross_process_stability_with_the_same_configured_secret(self):
        """REPLICA STABILITY, proven across a REAL second interpreter rather
        than asserted from reading the code: a token computed here and one
        computed in a fresh ``python3`` process, given the identical
        configured secret and the identical material, must agree — the
        module docstring's 'PER PROCESS? NO' claim, now that the digest also
        depends on a secret rather than only on the material."""
        secret = "cross-process-" + secrets.token_hex(16)
        with _env(HS_CONTEXT_EPOCH_SECRET=secret):
            here = context_epoch(
                "user_a", 3, ContextEpochDerivationTest.PROGRAM,
                ContextEpochDerivationTest.SEASON,
                ContextEpochDerivationTest.LEAGUE)

        script = (
            "from hockey_scheduler.domain import SeasonStatus\n"
            "from hockey_scheduler.services.context_epoch import context_epoch\n"
            "class A:\n"
            "    def __init__(self, id):\n"
            "        self.id = id\n"
            "class S(A):\n"
            "    status = SeasonStatus.ACTIVE\n"
            "    archived_at = None\n"
            "print(context_epoch('user_a', 3, A('program_1'), "
            "S('season_1'), A('league_1')))\n")
        env = dict(os.environ)
        env["HS_CONTEXT_EPOCH_SECRET"] = secret
        result = subprocess.run([sys.executable, "-c", script],
                                env=env, cwd=str(BACKEND),
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        there = result.stdout.strip()
        self.assertRegex(there, _HEX32, result.stdout)
        self.assertEqual(
            here, there,
            "the same configured secret and the same material hashed "
            "differently in a fresh process — token issuance would not "
            "survive a restart or agree across replicas")

    def test_a_leaked_token_cannot_be_correlated_to_an_identity_without_the_secret(self):
        """THE FINDING 4 POC, reproduced and closed.

        THE POSITIVE CONTROL comes first and must succeed, or the negative
        result below would be vacuous: a party who DOES hold the deployment
        secret brute-forces the exact same 100-candidate dictionary the
        reviewer used (a fresh account, no saved selection — the lowest-
        entropy material this module ever hashes) and DOES recover the
        target token. That proves the attack methodology still works and the
        digest is still a deterministic function of (material, secret).

        THE FIX: the same dictionary, run by a party who does NOT hold the
        deployment's configured secret — here, simply no override at all, so
        the demo/dev fallback key applies instead of whatever the (simulated)
        deployment configured — recovers NOTHING. Not "recovers it more
        slowly": recovers zero of the hundred candidates, because the digest
        is now a MAC and an unkeyed (or wrong-keyed) brute force is not a
        weaker attack on a MAC, it is a non-attack.
        """
        real_secret = "prod-" + secrets.token_hex(32)
        target_user = "user_37"
        # generation=0, program=season=league=None is exactly the material a
        # fresh account with no saved selection and no authorized Program
        # hashes — the lowest-entropy state, and the reviewer's exact PoC.
        with _env(HS_CONTEXT_EPOCH_SECRET=real_secret, APP_MODE="production"):
            leaked = context_epoch(target_user, 0, None, None, None)
        self.assertRegex(leaked, _HEX32)

        with _env(HS_CONTEXT_EPOCH_SECRET=real_secret, APP_MODE="production"):
            found_with_secret = [
                n for n in range(1, 101)
                if context_epoch(f"user_{n}", 0, None, None, None) == leaked]
        self.assertEqual(
            found_with_secret, [37],
            "the brute force did not recover the token even WITH the "
            "correct secret — the positive control is broken, so the "
            "negative result below would prove nothing")

        with _env(HS_CONTEXT_EPOCH_SECRET=None, APP_MODE=None):
            found_without_secret = [
                n for n in range(1, 101)
                if context_epoch(f"user_{n}", 0, None, None, None) == leaked]
        self.assertEqual(
            found_without_secret, [],
            "the leaked token was recovered by a party WITHOUT the "
            "deployment secret — finding 4 is not closed")


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

    def _all_scoped_route_cases(self, fx):
        """One case per entry in ``CONTEXT_SCOPED_READ_ROUTES`` (#159 review
        finding 1) -- label, the exact path to hit, the ``ApiService`` method
        the ceiling reaches, and the id ``_watch_service`` should watch for on
        that method. Built fresh under ``fx['s1']`` so every target genuinely
        exists and answers non-204 there, the same discipline
        ``_program_with_two_seasons`` fixtures already use.

        A TABLE, not a hand-picked subset: the whole point is that a route
        added to ``CONTEXT_SCOPED_READ_ROUTES`` later shows up here too,
        rather than this file silently testing yesterday's membership.
        """
        division_id = self._division_with_teams(fx, fx["s1"], tag="Tbl")
        scenario_id = self._scenario_in(fx, fx["s1"], name="Table run")
        ls_ids = self._league_with_teams(fx, fx["s1"], tag="TblLs")
        return [
            {"label": "venue-candidates",
             "path": f"/api/v2/setup/seasons/{fx['s1']}/venue-candidates",
             "service": "get_venue_grant_candidates", "watch": fx["s1"]},
            {"label": "venue-access",
             "path": f"/api/v2/setup/seasons/{fx['s1']}/venue-access",
             "service": "list_season_venue_access", "watch": fx["s1"]},
            {"label": "scenario",
             "path": f"/api/scheduler/scenarios/{scenario_id}",
             "service": "get_schedule_scenario", "watch": scenario_id},
            {"label": "standings-division",
             "path": f"/api/standings/{division_id}",
             "service": "get_standings", "watch": division_id},
            {"label": "standings-league-season",
             "path": (f"/api/standings/league-season/"
                      f"{ls_ids['league_id']}/{fx['s1']}"),
             "service": "get_league_season_standings",
             "watch": ls_ids["league_id"]},
        ]

    # -- helpers ------------------------------------------------------------
    def _epoch(self, user_id, role=Role.LEAGUE_ADMIN, scope=None):
        """The CURRENT epoch for ``user_id`` as it stands NOW, derived the
        same way the server derives it (``ApiService.context.current_epoch``,
        #159 review finding 2's effective-tuple resolution). ``role``/
        ``scope`` default to this file's standard operator shape — every
        account ``_operator`` mints is a global ``Role.LEAGUE_ADMIN`` — so
        every existing call site (``self._epoch(user_id)``) keeps working
        unchanged; a case that builds a differently-scoped account passes its
        own."""
        return self.api.context.current_epoch(user_id, role, scope or {})

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

    def _selected_context(self, client):
        """The signed-in client's effective context, off ``GET /api/context``
        — including ``context_epoch``, so callers get the tuple and the epoch
        it was rendered under from the SAME payload rather than pairing two
        separate reads that could straddle a concurrent change."""
        status, raw, body = self._req(client, "GET", "/api/context")
        self.assertEqual(status, 200, raw)
        return body

    def _archive(self, client, season_id):
        status, raw, _ = self._req(
            client, "POST", f"/api/v2/setup/seasons/{season_id}/archive", {})
        self.assertEqual(status, 200, raw)

    def _empty_program_with_three_seasons(self, tag):
        """A Program with THREE Seasons, none carrying a Division, Team, or
        Venue grant — deletable and archivable with zero dependency block, so
        the fixture's OWN plumbing can never be the explanation for a result
        in the #159 review finding 2 case below."""
        svc = self.api.setup
        program = svc.create_program(f"{tag} Program")
        s1 = svc.create_season(program.id, f"{tag} S1")
        s2 = svc.create_season(program.id, f"{tag} S2")
        s3 = svc.create_season(program.id, f"{tag} S3")
        return {"tag": tag, "program_id": program.id,
               "s1": s1.id, "s2": s2.id, "s3": s3.id}

    def _delete_season(self, client, season_id):
        status, raw, _ = self._req(
            client, "POST", f"/api/setup/season/{season_id}/delete", {})
        self.assertEqual(status, 200, raw)

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
                epoch_verdict(echoed, self._epoch(user_id)), EPOCH_MISMATCH,
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
    # 6. EVERY REGISTERED ROUTE, TABLE-DRIVEN — not a hand-picked subset
    # ======================================================================
    def test_every_registered_scoped_read_route_discards_on_a_stale_epoch(self):
        """``CONTEXT_SCOPED_READ_ROUTES`` is the authoritative definition of a
        context-scoped read, and every entry gets the SAME treatment — driven
        off the table itself (``_all_scoped_route_cases``), not a hand-picked
        pair (#159 review finding 1).

        THE GAP THIS REPLACES A NARROWER TEST FOR: the previous version of
        this case exercised only the Division standings route and the
        scenario route — "the other two" the #415 CI incident had not hit.
        The FIFTH registered route, ``GET /api/standings/league-season/<l>/
        <s>``, used ``Handler._context_read_hold`` directly instead of
        ``_read_under_context_gate`` and so skipped the epoch comparison
        entirely: a stale echo reached ``ApiService.get_league_season_
        standings`` and answered whatever the ceiling says (its ordinary
        ``not_found``) rather than the contract's empty ``204``/no-service-
        call discard. A test built from the table would have caught it; one
        that named two routes by hand could not.

        Required coverage, per route, per the review: a CURRENT epoch reaches
        the service and reproduces the no-header answer (``current epoch =
        existing response``); a STALE epoch is ``204`` with an EMPTY body and
        a service-call SPY proving the service was never reached (a discard
        must be measured, not inferred from the status code); an ABSENT
        header is never a discard (legacy behavior, unimproved but not worse).
        """
        fx = self._program_with_two_seasons("AllRoutes")
        username, user_id = self._operator("allroutes")
        client = self._login(username)
        self._select(client, fx["program_id"], fx["s1"])
        cases = self._all_scoped_route_cases(fx)
        self.assertEqual(
            len(cases), len(self.srv.CONTEXT_SCOPED_READ_ROUTES),
            "this table has drifted out of sync with "
            "CONTEXT_SCOPED_READ_ROUTES — every registered route needs a "
            "case here, or this test is silently back to hand-picking")

        # The no-header baseline, captured BEFORE any epoch enters play, so
        # "current epoch = existing response" below has something honest to
        # match against rather than a status-code guess.
        baselines = {}
        for case in cases:
            status, raw, _ = self._req(client, "GET", case["path"])
            self.assertNotEqual(
                status, 204, f"{case['label']}: the fixture route itself "
                f"discards with no epoch involved at all: {raw}")
            baselines[case["label"]] = (status, raw)

        stale = self._epoch_from_api(client)

        for case in cases:
            with self.subTest(route=case["label"], phase="current-epoch"):
                service = self._watch_service(case["service"])
                status, raw, _ = self._req(
                    client, "GET", case["path"],
                    headers={CONTEXT_EPOCH_HEADER: stale})
                self.assertEqual(
                    (status, raw), baselines[case["label"]],
                    f"{case['label']}: echoing the CURRENT epoch changed the "
                    f"answer — a match must reproduce the existing response "
                    f"byte for byte")
                self.assertIn(
                    case["watch"], service,
                    f"{case['label']}: a current-epoch read never reached "
                    f"the service at all, so the match above proves nothing")

        self._select(client, fx["program_id"], fx["s2"])

        for case in cases:
            with self.subTest(route=case["label"], phase="stale-epoch"):
                service = self._watch_service(case["service"])
                status, raw, _ = self._req(
                    client, "GET", case["path"],
                    headers={CONTEXT_EPOCH_HEADER: stale})
                self.assertEqual(
                    status, 204,
                    f"{case['label']}: a STALE epoch reached the ceiling "
                    f"({status}) instead of the contract's empty "
                    f"204/no-service-call discard: {raw}")
                self.assertEqual(
                    raw, "", f"{case['label']}: a discard must carry no "
                    f"body: {raw}")
                self.assertEqual(
                    service, [],
                    f"{case['label']}: the stale-epoch read reached the "
                    f"service ({service}) — the ceiling WAS evaluated for "
                    f"it, so the discard did not short-circuit in front of "
                    f"it")

            with self.subTest(route=case["label"], phase="absent-header"):
                bare_status, bare_raw, _ = self._req(
                    client, "GET", case["path"])
                self.assertNotEqual(
                    bare_status, 204,
                    f"{case['label']}: an ABSENT header must never produce "
                    f"a discard — no client is required to participate: "
                    f"{bare_raw}")
        self._assert_gate_is_clean("after the full-route-table stale sweep")

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
        function of PERSISTED state — the effective tuple ``ContextService``
        resolves plus the persisted generation — so re-importing the deriving
        module — the closest a test gets to 'a different process reading the
        same material' — produces the identical value for the identical
        material. A per-process registry could not."""
        fx = self._program_with_two_seasons("Restart")
        username, user_id = self._operator("restart")
        client = self._login(username)
        self._select(client, fx["program_id"], fx["s1"])
        before = self._epoch_from_api(client)

        generation, program, season, league = (
            self.api.context.resolve_epoch_state(
                user_id, Role.LEAGUE_ADMIN, {}))
        module = importlib.import_module(
            "hockey_scheduler.services.context_epoch")
        reloaded = importlib.reload(module)
        self.assertEqual(
            reloaded.context_epoch(user_id, generation, program, season,
                                   league),
            before,
            "a fresh import of the deriving module produced a different "
            "token for the identical material")
        # The row, read straight out of the store, is what the generation
        # came from; the resolution above is what the effective tuple did.
        row = self.api.store.get_active_context(user_id)
        self.assertIsInstance(row, ActiveContext)
        self.assertEqual(row.season_id, fx["s1"])
        self.assertEqual(program.id, fx["program_id"])
        self.assertEqual(season.id, fx["s1"])

    # ======================================================================
    # 8. THE KEYED MAC (#159 review finding 4), end to end over real HTTP
    # ======================================================================
    def test_rotating_the_deployment_secret_discards_outstanding_tokens_like_any_mismatch(self):
        """Rotation is not a new code path — it is the SAME comparison
        against a token that no longer matches, so it must produce EXACTLY
        the ordinary mismatch answer: 204, empty body, service never
        reached. Also the auth-independence half of finding 4's required
        coverage: rotating never locks the account out (a FRESH token
        learned after rotation is honored normally) and never turns into a
        distinguishable error (a bare request with no header at all gets the
        same unimproved answer it always has)."""
        fx = self._program_with_two_seasons("Rotate")
        username, user_id = self._operator("rotate")
        client = self._login(username)
        self._select(client, fx["program_id"], fx["s1"])
        division = self._division_with_teams(fx, fx["s1"])
        epoch = self._epoch_from_api(client)

        # The positive control: BEFORE rotation, this exact token is honored.
        status, raw, _ = self._req(
            client, "GET", f"/api/standings/{division}",
            headers={CONTEXT_EPOCH_HEADER: epoch})
        self.assertEqual(status, 200, raw)

        with _env(HS_CONTEXT_EPOCH_SECRET="rotated-" + secrets.token_hex(16)):
            service = self._watch_service("get_standings")
            status, raw, _ = self._req(
                client, "GET", f"/api/standings/{division}",
                headers={CONTEXT_EPOCH_HEADER: epoch})
            self.assertEqual(
                status, 204,
                f"a token issued under the pre-rotation secret was still "
                f"honored after rotation: {raw!r}")
            self.assertEqual(raw, "", "a discard must carry no body")
            self.assertEqual(
                [s for s in service if s == division], [],
                "the post-rotation mismatch reached ApiService — it did not "
                "short-circuit in front of the ceiling")

            # AUTH-INDEPENDENCE, control 1: no header at all is unimproved.
            bare_status, bare_raw, _ = self._req(
                client, "GET", f"/api/standings/{division}")
            self.assertNotEqual(bare_status, 204, bare_raw)

            # AUTH-INDEPENDENCE, control 2: a FRESH token learned AFTER the
            # rotation is honored normally — rotation discards what was
            # already in flight, it does not lock the account out.
            fresh = self._epoch_from_api(client)
            self.assertNotEqual(
                fresh, epoch,
                "the fixture is vacuous: rotation must change the current "
                "epoch or this control proves nothing")
            status, raw, _ = self._req(
                client, "GET", f"/api/standings/{division}",
                headers={CONTEXT_EPOCH_HEADER: fresh})
            self.assertEqual(status, 200, raw)
        self._assert_gate_is_clean("after the rotation discard")

    # ======================================================================
    # 9. THE EFFECTIVE TUPLE, NOT THE RAW ROW (#159 review finding 2)
    # ======================================================================
    def test_a_deleted_saved_row_then_an_archived_fallback_moves_the_epoch(self):
        """FINDING 2'S EXACT POC, reproduced and closed.

            Persisted S1 explicitly. Deleted S1 -> /api/context now resolves
              the deterministic fallback (S2 or S3 — whichever `_fallback`
              picks; this case observes it rather than predicting it).
            Archived THAT fallback -> the fallback's OWN candidate set
              shrank, so resolve() moves again, to the remaining Season.

        Through both moves the RAW ActiveContext ROW never changes — it still
        names the deleted S1, because #159/#409 explicitly PRESERVE an
        invalid saved row rather than rewriting it, so a later restore of
        authorization/existence resolves it again. Against a token bound to
        that raw row alone, both EFFECTIVE moves are invisible: a read
        rendered under the first fallback's epoch, echoed after ITS archive,
        would still "match" and reach the ceiling naming an Season that is
        now archived — a 404, not the 204 discard the effective move earned
        it. Against the fix (the epoch bound to the EFFECTIVE tuple), each
        move is visible and the stale read is discarded before the ceiling
        runs; a token for the CURRENT effective resolution is still admitted
        normally.

        THE ARCHIVE ARRIVES ON A SEPARATE OPERATOR, deliberately: #409's
        explicit-selection-only mutation gate judges a write against the
        ACTOR's own SAVED context (never a fallback — ``ContextService.
        resolve_saved_with_league``), so the PRIMARY operator's own raw row
        (still naming the deleted S1) could not itself archive anything
        without first explicitly re-selecting — which would rewrite the very
        row this case is proving stays untouched. A second operator, tab, or
        device is exactly the shape #159's own docstring already describes
        for this interleaving, and the epoch is compared against the
        PRIMARY operator's own persisted row and resolution — never the
        archiver's — so which account performs the archive changes nothing
        about what is being measured.
        """
        fx = self._empty_program_with_three_seasons("Delete2")
        username, user_id = self._operator("delete2")
        client = self._login(username)
        archiver_name, _archiver_id = self._operator("delete2arch")
        archiver = self._login(archiver_name)

        self._select(client, fx["program_id"], fx["s1"])
        ctx0 = self._selected_context(client)
        self.assertEqual(ctx0["season_id"], fx["s1"])
        epoch_s1 = ctx0["context_epoch"]
        raw_before = self.api.store.get_active_context(user_id)
        self.assertEqual(raw_before.season_id, fx["s1"])

        # -- move 1: delete the saved Season -> resolve() falls back -------
        self._delete_season(client, fx["s1"])
        ctx1 = self._selected_context(client)
        fallback_1 = ctx1["season_id"]
        fallback_1_program = ctx1["program_id"]
        self.assertIsNotNone(
            fallback_1, "the fixture left no authorized active Season for "
            "the fallback to land on, so this case measures nothing")
        self.assertNotEqual(fallback_1, fx["s1"])
        epoch_1 = ctx1["context_epoch"]
        self.assertNotEqual(
            epoch_1, epoch_s1,
            "the epoch did not move when the EFFECTIVE resolution moved "
            "from the deleted S1 to its fallback — finding 2's exact gap")
        raw_1 = self.api.store.get_active_context(user_id)
        self.assertEqual(
            raw_1.season_id, fx["s1"],
            "the saved row was REWRITTEN by a deleted-Season resolve — "
            "#159/#409 require it be preserved, not corrected, so a later "
            "restore of authorization/existence resolves it again")

        # -- move 2: archive the fallback -> resolve() falls back again ----
        # NOTE: the fallback need not belong to `fx["program_id"]` — a
        # global LEAGUE_ADMIN's fallback search ranges over every Program
        # this store has ever authorized it for (this class runs many cases
        # against one shared store), and always lands on the lexically
        # FIRST Program with an authorized active Season. The archiver
        # selects whatever `ctx1` actually named, not this fixture's own
        # Program id.
        self._select(archiver, fallback_1_program, fallback_1)
        self._archive(archiver, fallback_1)
        ctx2 = self._selected_context(client)
        fallback_2 = ctx2["season_id"]
        self.assertIsNotNone(
            fallback_2, "the fixture left no SECOND authorized active "
            "Season, so this case measures nothing")
        self.assertNotEqual(fallback_2, fallback_1)
        epoch_2 = ctx2["context_epoch"]
        self.assertNotEqual(
            epoch_2, epoch_1,
            "the epoch did not move when the fallback's OWN candidate set "
            "changed and the effective resolution moved again — the second "
            "half of finding 2's exact gap")
        raw_2 = self.api.store.get_active_context(user_id)
        self.assertEqual(
            raw_2.season_id, fx["s1"],
            "the saved row moved on an archive of an UNRELATED Season — it "
            "must stay exactly what the operator themselves chose")

        # -- the stale read: epoch_1 (the deleted-then-superseded fallback's
        #    epoch), echoed now that the effective resolution has moved on.
        service = self._watch_service("get_venue_grant_candidates")
        status, raw, _ = self._req(
            client, "GET",
            f"/api/v2/setup/seasons/{fallback_1}/venue-candidates",
            headers={CONTEXT_EPOCH_HEADER: epoch_1})
        self.assertEqual(
            status, 204,
            f"a read rendered under the deleted-then-superseded fallback's "
            f"epoch reached the ceiling ({status}) instead of being "
            f"discarded — the epoch is still bound to the raw row rather "
            f"than the effective resolution: {raw!r}")
        self.assertEqual(raw, "", "a discard must carry no body")
        self.assertEqual(
            service, [],
            "the stale read reached ApiService — the ceiling WAS "
            "evaluated for it, so the discard did not short-circuit in "
            "front of it")

        # -- the fresh token: admitted normally, to the CURRENT ceiling ----
        status, raw, body = self._req(
            client, "GET",
            f"/api/v2/setup/seasons/{fallback_2}/venue-candidates",
            headers={CONTEXT_EPOCH_HEADER: epoch_2})
        self.assertEqual(
            status, 200,
            f"a fresh token for the CURRENT effective resolution must be "
            f"admitted: {raw}")
        self.assertIn("candidates", body, raw)
        self._assert_gate_is_clean("after the finding-2 PoC sweep")

    # ======================================================================
    # 10. THE PERSISTED GENERATION, NOT THE WALL CLOCK (#159 review
    #     finding 5)
    # ======================================================================
    def test_two_writes_under_an_identical_frozen_clock_still_move_the_epoch(self):
        """FINDING 5'S EXACT SCENARIO: ``updated_at`` is not a reliable
        "moves on every switch" signal, because two commits CAN land inside
        the same wall-clock tick — a coarse system clock, load, or (driven
        here directly rather than waited for) a clock frozen for both
        writes. The persisted GENERATION never consults a clock at all: it
        is read then written one higher inside each write's own transaction,
        so it moves on the second write regardless of what the clock says.
        """
        fx = self._program_with_two_seasons("FrozenClock")
        username, user_id = self._operator("frozenclock")
        client = self._login(username)

        frozen = datetime(2027, 1, 1, tzinfo=timezone.utc)
        original_clock = self.api.context.clock
        self.api.context.clock = lambda: frozen
        try:
            self._select(client, fx["program_id"], fx["s1"])
            row_1 = self.api.store.get_active_context(user_id)
            epoch_1 = self._epoch(user_id)

            self._select(client, fx["program_id"], fx["s2"])
            row_2 = self.api.store.get_active_context(user_id)
            epoch_2 = self._epoch(user_id)
        finally:
            self.api.context.clock = original_clock

        self.assertEqual(
            row_1.updated_at, row_2.updated_at,
            "the fixture did not actually freeze the clock — this case "
            "proves nothing without two IDENTICAL timestamps")
        self.assertNotEqual(row_1.season_id, row_2.season_id,
                            "the two writes selected the same tuple — this "
                            "case needs a genuine switch")
        self.assertEqual(
            row_2.generation, row_1.generation + 1,
            "the persisted generation did not move by exactly one on the "
            "second write")
        self.assertNotEqual(
            epoch_1, epoch_2,
            "two switches under an IDENTICAL frozen clock produced the "
            "SAME epoch — updated_at alone cannot be trusted to move on "
            "every switch, and the generation counter must be what "
            "actually does")

    def test_concurrent_a_to_b_to_a_never_loses_a_generation_step(self):
        """FINDING 5'S CONCURRENCY HALF: A->B->A driven through REAL
        concurrent requests (separate sessions for the same account,
        switching at once) must not let the generation's read-then-write
        step LOSE an update. A lost update would leave the counter short and
        could let two of the writes collide on the SAME generation — the
        A -> B -> A epoch reuse finding 5 exists to close, reached by a race
        rather than by a coarse clock this time. Six alternating switches,
        not two, so a single lucky ordering cannot hide an occasional loss —
        run on Memory/SQLite/PostgreSQL, so the store's own retry-on-conflict
        (the ``ConcurrencyConflictError`` handling ``ContextService.
        _snapshot`` already relies on for every other read-then-write) is
        what is actually being exercised here, on every backend, not merely
        assumed to cover this one too."""
        fx = self._program_with_two_seasons("ConcGen")
        username, user_id = self._operator("concgen")
        setup_client = self._login(username)
        self._select(setup_client, fx["program_id"], fx["s1"])
        start_generation = self.api.store.get_active_context(
            user_id).generation

        rounds = 6
        errors = []

        def switch_round(target):
            try:
                client = self._login(username)
                status, raw, _ = self._req(
                    client, "POST", "/api/context",
                    {"program_id": fx["program_id"], "season_id": target})
                if status != 200:
                    errors.append((target, status, raw))
            except Exception as exc:                    # pragma: no cover
                errors.append((target, "exception", repr(exc)))

        threads = [
            threading.Thread(target=switch_round,
                             args=(fx["s2"] if i % 2 == 0 else fx["s1"],))
            for i in range(rounds)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(PATIENCE)
        self.assertEqual(errors, [], f"a concurrent switch failed: {errors}")

        final = self.api.store.get_active_context(user_id)
        self.assertIn(final.season_id, (fx["s1"], fx["s2"]),
                      "the final persisted tuple is neither requested "
                      "Season — a concurrent write corrupted it")
        self.assertEqual(
            final.generation, start_generation + rounds,
            f"the final generation ({final.generation}) is not exactly "
            f"{rounds} past the pre-churn value ({start_generation}) — a "
            f"concurrent write lost its increment, which can reuse an "
            f"epoch across a switch exactly as finding 5 describes")

    # ======================================================================
    # 11. THE CHECK->SERVICE TOCTOU (#159 review finding 3)
    # ======================================================================
    def test_an_archive_dispatched_while_a_read_is_parked_mid_service_waits_for_it(self):
        """FINDING 3'S EXACT POC, reproduced and closed: park the read
        INSIDE its own service call (the review's own methodology) — i.e.
        strictly AFTER ``_read_under_context_gate``'s epoch check has
        already matched and BEFORE the ceiling evaluates anything — then
        archive the selected Season from a SECOND request while the read is
        held there. Exercised over EVERY route in
        ``CONTEXT_SCOPED_READ_ROUTES`` (a table, not a hand-picked one, for
        the same reason ``test_every_registered_scoped_read_route_discards_
        on_a_stale_epoch`` is).

        BEFORE THE FIX: nothing ordered the archive against a read already
        past its epoch check, so it committed immediately — the parked read
        then resumed against a Season archived out from under an epoch match
        it had already been granted, and the ceiling answered 404 (or the
        generic empty-standings shape) for a request the discard mechanism
        should have caught. That is precisely what the review measured.

        AFTER THE FIX: the read's SHARED hold on ``LIFECYCLE_GATE``
        registers when it enters ``_read_under_context_gate`` — before its
        epoch check, so certainly before it can be parked inside the
        service call that follows a MATCHING one. The archive's EXCLUSIVE
        hold therefore waits behind it. This is measured directly, not
        inferred from the eventual answer: while the read remains parked,
        the Season's live status is polled and asserted still ACTIVE — the
        archive has not committed. Only once the park is released does the
        read complete (entirely against pre-archive state, so it is
        answered exactly as the pre-archive baseline was) and THEN does the
        archive's own request return.
        """
        probe = self._program_with_two_seasons("ToctouLabels")
        labels = [c["label"] for c in self._all_scoped_route_cases(probe)]
        for i, label in enumerate(labels):
            with self.subTest(route=label):
                fx = self._program_with_two_seasons(f"Toctou{i}")
                case = next(c for c in self._all_scoped_route_cases(fx)
                           if c["label"] == label)
                username, user_id = self._operator(f"toctou{i}")
                client = self._login(username)
                self._select(client, fx["program_id"], fx["s1"])
                epoch = self._epoch_from_api(client)
                baseline, base_raw, _ = self._req(
                    client, "GET", case["path"],
                    headers={CONTEXT_EPOCH_HEADER: epoch})
                self.assertNotEqual(
                    baseline, 204, f"{label}: the fixture route itself "
                    f"discards with no race involved at all: {base_raw}")

                out = {}

                def do_read():
                    out["result"] = self._req(
                        client, "GET", case["path"],
                        headers={CONTEXT_EPOCH_HEADER: epoch})

                with self._read_parked_in(case["service"], case["watch"]) as (
                        park, _exited):
                    rt = threading.Thread(target=do_read, daemon=True)
                    rt.start()
                    self.assertTrue(
                        park.arrived.wait(PATIENCE),
                        f"{label}: the read never reached its service call")

                    archive_out = {}

                    def do_archive():
                        archive_out["result"] = self._req(
                            client, "POST",
                            f"/api/v2/setup/seasons/{fx['s1']}/archive", {})

                    at = threading.Thread(target=do_archive, daemon=True)
                    at.start()

                    # THE MEASUREMENT: while the read remains parked between
                    # its epoch match and its service call, the archive must
                    # NOT have committed — it is BLOCKED behind the read's
                    # LIFECYCLE_GATE hold, not racing ahead of it.
                    time.sleep(COMMIT_WINDOW)
                    mid_season = self.api.store.get_season(fx["s1"])
                    self.assertEqual(
                        mid_season.status, SeasonStatus.ACTIVE,
                        f"{label}: the archive committed WHILE the read was "
                        f"still parked between its epoch match and its "
                        f"service call — LIFECYCLE_GATE did not order it, "
                        f"and finding 3's TOCTOU is still open")

                    park.let_go()
                    rt.join(PATIENCE)
                    at.join(PATIENCE)

                self.assertIn("result", out,
                             f"{label}: the parked read never returned")
                status, raw, _ = out["result"]
                self.assertEqual(
                    status, baseline,
                    f"{label}: a read parked ENTIRELY pre-archive must be "
                    f"answered exactly as the pre-archive baseline was: "
                    f"{raw!r}")

                self.assertIn("result", archive_out,
                             f"{label}: the archive request never returned "
                             f"— it may still be blocked")
                arc_status, arc_raw, _ = archive_out["result"]
                self.assertEqual(
                    arc_status, 200,
                    f"{label}: the archive, unblocked after the read "
                    f"finished, must still succeed: {arc_raw}")
                final_season = self.api.store.get_season(fx["s1"])
                self.assertEqual(
                    final_season.status, SeasonStatus.ARCHIVED,
                    f"{label}: the archive never actually took effect once "
                    f"unblocked")
                self._assert_gate_is_clean(
                    f"after the {label} check->service TOCTOU sweep")

    @contextmanager
    def _lifecycle_exclusive_parked(self):
        """Park INSIDE ``LIFECYCLE_GATE.exclusive``'s body — i.e. AFTER the
        wait for prior participants has already been satisfied and the
        EXCLUSIVE hold is genuinely GRANTED, but BEFORE the caller's own
        code (the archive/reopen route, which opens the real store
        transaction) runs.

        Deliberately NOT a park inside ``archive_season`` itself
        (``_read_parked_in`` would reach it too, since its first positional
        argument is a Season id like every other wrapped method here): that
        method runs INSIDE the guarded mutation's own store transaction, so
        parking there holds the STORE's lock (``SqlStore``/``InMemoryStore``
        ``self._lock``) for the pause — which starves an unrelated read's
        own session/role resolution before it can even reach
        ``_read_under_context_gate`` to register with ``LIFECYCLE_GATE`` at
        all, on the SQL-backed stores. That is the identical shape of
        problem the module comment on ``LIFECYCLE_GATE`` documents this
        finding's fix AVOIDING; a test seam must not reintroduce it.
        Parking here instead holds only the gate's own lightweight condition
        variable — never the store — so an unrelated read can complete its
        own store access freely and reach the point where it genuinely
        contends on ``LIFECYCLE_GATE``, which is the fact this case needs to
        observe.
        """
        gate = self.srv.LIFECYCLE_GATE
        original = gate.exclusive
        park = _Park()

        @contextmanager
        def wrapped(key):
            with original(key) as ticket:
                park.hold()
                yield ticket

        gate.exclusive = wrapped
        try:
            yield park
        finally:
            gate.exclusive = original
            park.let_go()

    def test_an_archive_parked_mid_commit_makes_a_later_read_see_only_its_result(self):
        """THE OTHER COMMIT ORDER (#159 review finding 3's "both commit
        orders" requirement): the archive's EXCLUSIVE hold on
        ``LIFECYCLE_GATE`` registers BEFORE a read that is dispatched while
        the archive is still in flight. The read's SHARED hold must wait
        behind it, so the read's OWN EPOCH CHECK — not merely its service
        call — is what observes the post-archive state: the read is
        echoing the PRE-archive epoch it was rendered under, and by the
        time its (delayed) check runs that epoch has moved, so the correct
        answer is the SAME 204 discard any other stale echo produces —
        never a 200 admitted into a ceiling that has moved out from under
        it, and never a straddled mix of pre/post state.

        Driven on ``venue-candidates``, the review's own route; every route
        already shares the one ordering primitive under test
        (``LIFECYCLE_GATE``), so this is not a per-route property the way
        the epoch MATERIAL itself is — that half is covered by
        ``test_an_archive_dispatched_while_a_read_is_parked_mid_service_
        waits_for_it`` across every registered route.
        """
        fx = self._program_with_two_seasons("ToctouOtherOrder")
        username, user_id = self._operator("toctouorder")
        client = self._login(username)
        self._select(client, fx["program_id"], fx["s1"])
        epoch = self._epoch_from_api(client)

        archive_out = {}

        def do_archive():
            archive_out["result"] = self._req(
                client, "POST",
                f"/api/v2/setup/seasons/{fx['s1']}/archive", {})

        read_out = {}
        with self._lifecycle_exclusive_parked() as park:
            at = threading.Thread(target=do_archive, daemon=True)
            at.start()
            self.assertTrue(park.arrived.wait(PATIENCE),
                            "the archive's exclusive hold was never granted")

            service = self._watch_service("get_venue_grant_candidates")

            def do_read():
                read_out["result"] = self._scoped_read(
                    client, fx["s1"], "venue-candidates", epoch)

            rt = threading.Thread(target=do_read, daemon=True)
            rt.start()
            # The read's SHARED hold must be seen to be WAITING (never
            # admitted) while the archiver's EXCLUSIVE hold — registered
            # first — is still held. Measured via the gate's own stats
            # rather than inferred from timing.
            self.assertTrue(
                _wait(lambda:
                     self.srv.LIFECYCLE_GATE.stats()["waiting_readers"] >= 1,
                     PATIENCE),
                "the read never showed up as waiting behind the parked "
                "archive's exclusive hold")
            self.assertNotIn("result", read_out,
                            "the read was admitted WHILE the archive still "
                            "held the exclusive lock — the two orders "
                            "interleaved")

            park.let_go()
            at.join(PATIENCE)
            rt.join(PATIENCE)

        self.assertIn("result", archive_out, "the archive never returned")
        self.assertEqual(archive_out["result"][0], 200, archive_out["result"][1])
        self.assertIn("result", read_out, "the read never returned")
        status, raw, _ = read_out["result"]
        self.assertEqual(
            status, 204,
            f"a read whose SHARED hold waited behind the archiver's "
            f"EXCLUSIVE one must have its (now-delayed) epoch check observe "
            f"the moved epoch and discard — never be admitted to a ceiling "
            f"straddling two states: {raw!r}")
        self.assertEqual(raw, "", "a discard must carry no body")
        self.assertEqual(
            service, [],
            "the discarded read reached ApiService — the ceiling WAS "
            "evaluated for it")
        self._assert_gate_is_clean("after the archive-parked-first sweep")

    # ======================================================================
    # 11b. THE OTHER LIFECYCLE DIRECTION (#159 review finding 3's explicit
    #      "archive AND reopen in the other request" coverage requirement).
    #      `LIFECYCLE_GATE.exclusive` wraps `archive_season` and
    #      `reopen_season` through the identical `with` block in
    #      `web/server.py` (only the `call` function pointer differs), so
    #      these two cases are the SAME races as the pair above with the
    #      lifecycle direction reversed — proven rather than assumed from
    #      that symmetry, the same discipline finding 1 needed: "this route
    #      is basically its sibling" was exactly the belief that let the
    #      LeagueSeason standings branch skip the epoch gate undetected.
    # ======================================================================
    def test_a_reopen_dispatched_while_a_read_is_parked_mid_service_waits_for_it(self):
        """THE MID-SERVICE PARK, reopen direction: the Season starts
        ARCHIVED (selected as read-only history, same as any deliberately
        chosen archived Season), the read's epoch matches that archived
        state, and it parks INSIDE ``get_venue_grant_candidates`` — after
        the epoch check already matched, before the service body (including
        its own archived-destination refusal, #369 owner ruling) runs. A
        second request reopens the same Season while the read is held
        there.

        MEASURED, not inferred: while the read remains parked, the Season's
        live status is polled and asserted still ARCHIVED — the reopen has
        not committed. Only once the park releases does the read complete,
        entirely against PRE-reopen state, so it must reproduce the
        pre-reopen (archived) baseline exactly — here, the archived-season
        refusal (#369) — never a torn answer computed against a Season that
        became active partway through. The reopen's own request then
        returns once unblocked.
        """
        fx = self._program_with_two_seasons("ReopenToctou")
        username, user_id = self._operator("reopentoctou")
        client = self._login(username)
        self._select(client, fx["program_id"], fx["s1"])
        self._archive(client, fx["s1"])
        epoch = self._epoch_from_api(client)
        baseline, base_raw, _ = self._req(
            client, "GET",
            f"/api/v2/setup/seasons/{fx['s1']}/venue-candidates",
            headers={CONTEXT_EPOCH_HEADER: epoch})
        self.assertNotEqual(
            baseline, 204, f"the fixture route itself discards with no "
            f"race involved at all: {base_raw}")

        out = {}

        def do_read():
            out["result"] = self._req(
                client, "GET",
                f"/api/v2/setup/seasons/{fx['s1']}/venue-candidates",
                headers={CONTEXT_EPOCH_HEADER: epoch})

        with self._read_parked_in(
                "get_venue_grant_candidates", fx["s1"]) as (park, _exited):
            rt = threading.Thread(target=do_read, daemon=True)
            rt.start()
            self.assertTrue(
                park.arrived.wait(PATIENCE),
                "the read never reached its service call")

            reopen_out = {}

            def do_reopen():
                reopen_out["result"] = self._req(
                    client, "POST", f"/api/v2/setup/seasons/{fx['s1']}/reopen",
                    {"reason": "Late roster correction approved by the "
                               "League."})

            ot = threading.Thread(target=do_reopen, daemon=True)
            ot.start()

            # THE MEASUREMENT: while the read remains parked between its
            # epoch match and its service call, the reopen must NOT have
            # committed — it is BLOCKED behind the read's LIFECYCLE_GATE
            # hold, not racing ahead of it.
            time.sleep(COMMIT_WINDOW)
            mid_season = self.api.store.get_season(fx["s1"])
            self.assertEqual(
                mid_season.status, SeasonStatus.ARCHIVED,
                "the reopen committed WHILE the read was still parked "
                "between its epoch match and its service call — "
                "LIFECYCLE_GATE did not order it, and finding 3's TOCTOU "
                "is still open for the reopen direction")

            park.let_go()
            rt.join(PATIENCE)
            ot.join(PATIENCE)

        self.assertIn("result", out, "the parked read never returned")
        status, raw, _ = out["result"]
        self.assertEqual(
            status, baseline,
            f"a read parked ENTIRELY pre-reopen must be answered exactly "
            f"as the pre-reopen (archived) baseline was: {raw!r}")

        self.assertIn("result", reopen_out,
                      "the reopen request never returned — it may still "
                      "be blocked")
        reopen_status, reopen_raw, _ = reopen_out["result"]
        self.assertEqual(
            reopen_status, 200,
            f"the reopen, unblocked after the read finished, must still "
            f"succeed: {reopen_raw}")
        final_season = self.api.store.get_season(fx["s1"])
        self.assertEqual(
            final_season.status, SeasonStatus.ACTIVE,
            "the reopen never actually took effect once unblocked")
        self._assert_gate_is_clean(
            "after the reopen check->service TOCTOU sweep")

    def test_a_reopen_parked_mid_commit_makes_a_later_read_see_only_its_result(self):
        """THE OTHER COMMIT ORDER, reopen direction: the reopen's EXCLUSIVE
        hold on ``LIFECYCLE_GATE`` registers BEFORE a read that is
        dispatched while the reopen is still in flight. The read's SHARED
        hold must wait behind it, so the read's OWN EPOCH CHECK observes the
        POST-reopen state: the read is echoing the PRE-reopen (archived)
        epoch it was rendered under, and by the time its (delayed) check
        runs that epoch has moved, so the correct answer is the SAME 204
        discard any other stale echo produces — never a 200 admitted
        against a ceiling that has moved out from under it.
        """
        fx = self._program_with_two_seasons("ReopenToctouOrder")
        username, user_id = self._operator("reopentoctouorder")
        client = self._login(username)
        self._select(client, fx["program_id"], fx["s1"])
        self._archive(client, fx["s1"])
        epoch = self._epoch_from_api(client)

        reopen_out = {}

        def do_reopen():
            reopen_out["result"] = self._req(
                client, "POST", f"/api/v2/setup/seasons/{fx['s1']}/reopen",
                {"reason": "Late roster correction approved by the League."})

        read_out = {}
        with self._lifecycle_exclusive_parked() as park:
            ot = threading.Thread(target=do_reopen, daemon=True)
            ot.start()
            self.assertTrue(park.arrived.wait(PATIENCE),
                            "the reopen's exclusive hold was never granted")

            service = self._watch_service("get_venue_grant_candidates")

            def do_read():
                read_out["result"] = self._scoped_read(
                    client, fx["s1"], "venue-candidates", epoch)

            rt = threading.Thread(target=do_read, daemon=True)
            rt.start()
            # The read's SHARED hold must be seen to be WAITING (never
            # admitted) while the reopener's EXCLUSIVE hold — registered
            # first — is still held. Measured via the gate's own stats
            # rather than inferred from timing.
            self.assertTrue(
                _wait(lambda:
                     self.srv.LIFECYCLE_GATE.stats()["waiting_readers"] >= 1,
                     PATIENCE),
                "the read never showed up as waiting behind the parked "
                "reopen's exclusive hold")
            self.assertNotIn("result", read_out,
                            "the read was admitted WHILE the reopen still "
                            "held the exclusive lock — the two orders "
                            "interleaved")

            park.let_go()
            ot.join(PATIENCE)
            rt.join(PATIENCE)

        self.assertIn("result", reopen_out, "the reopen never returned")
        self.assertEqual(reopen_out["result"][0], 200, reopen_out["result"][1])
        self.assertIn("result", read_out, "the read never returned")
        status, raw, _ = read_out["result"]
        self.assertEqual(
            status, 204,
            f"a read whose SHARED hold waited behind the reopener's "
            f"EXCLUSIVE one must have its (now-delayed) epoch check "
            f"observe the moved epoch and discard — never be admitted to "
            f"a ceiling straddling two states: {raw!r}")
        self.assertEqual(raw, "", "a discard must carry no body")
        self.assertEqual(
            service, [],
            "the discarded read reached ApiService — the ceiling WAS "
            "evaluated for it")
        self._assert_gate_is_clean("after the reopen-parked-first sweep")


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
