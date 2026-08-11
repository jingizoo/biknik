"""Per-user quiescence gate ordering exact-Season scoped READS against the
context SWITCH that would invalidate them (#159 follow-up).

THE DEFECT THIS EXISTS FOR. Two GET routes are ceilinged to the caller's
EXACTLY selected Season — ``/api/v2/setup/seasons/<id>/venue-candidates`` and
``/api/v2/setup/seasons/<id>/venue-access``. Their handlers resolve the
persisted ActiveContext and refuse, with a deliberately generic 404, any Season
that is not the selected one. If a ``POST /api/context`` commits a NEW
selection while one of those reads is still inside the server, the read is
judged against a tuple it never saw and answers 404 for a Season the operator
was legitimately looking at. CI reproduced exactly that (run 31504917446,
browser shard 1, phone leg).

WHY THE CLIENT CANNOT FIX IT. ``app.js`` enrols those reads in an
AbortController barrier and awaits their settlement before POSTing. But
aborting settles the CLIENT's fetch promise IMMEDIATELY; the server goes on
running the request it already accepted. **An AbortController cannot un-send a
request the server already holds.** Client settlement is not handler exit, so
the ordering has to be enforced here.

WHY NOT THE EXISTING #386 ``active_context_mutex`` — three independent reasons:

1. IT IS A NO-OP IN THE PROCESS THAT HAS THE BUG. The web process holds exactly
   ONE ``SqlStore``, which is one connection behind one per-instance ``RLock``.
   Both ``active_context_mutex`` and ``_lock_active_context_mutex`` are
   PostgreSQL advisory locks on ``self.conn``, and PostgreSQL grants an
   advisory lock re-entrantly to the SAME session — so a read holding it on
   that connection would not block a switch asking for it on that same
   connection. That mutex orders CONNECTIONS, which is why every existing test
   of it builds a SECOND ``SqlStore``. It is additionally a documented no-op on
   SQLite and on the in-memory store.
2. WRONG GRAIN. It is a MUTEX. Making ordinary scoped reads take it would
   serialize every scoped read against every other scoped read and against
   every mutation, for nothing — the rule ``ContextService.resolve_with_league``
   already writes down. What is needed is shared-among-readers,
   exclusive-for-the-switch.
3. WRONG PROPERTY. Mutual exclusion says the read and the switch do not
   interleave; it does not say WHICH GOES FIRST. If the switch won the row lock
   the read would then block, wake, resolve the NEW tuple and 404 the old
   Season — the same CI failure, reproduced *through* the fix. This is an
   ORDERING defect and the order must be BY ARRIVAL AT THE SERVER, a
   request-lifecycle fact no store-layer lock can express.

THE ORDER THIS ENFORCES, and why it cannot deadlock. Every participant takes a
monotonically increasing arrival ``seq`` under one mutex, and **every wait
predicate in this module refers only to participants with a STRICTLY SMALLER
seq**:

  * a WRITER for user *u* with sequence *w* waits for every reader whose seq is
    below *w* and whose identity is either *u* or NOT YET KNOWN, and for every
    writer for *u* below *w*;
  * a READER binding to *u* at seq *r* waits for every writer for *u* below *r*
    — and therefore never for one that registered after it arrived.

The wait-for graph's edges therefore always point from a higher seq to a lower
one, so it is a DAG and a cycle is UNCONSTRUCTIBLE rather than merely avoided
by care. Starvation is bounded for the same reason: each waiter's blocking set
is fixed at the moment it registers and can only shrink.

TWO-PHASE READER REGISTRATION, and why one phase is not enough. A request that
is inside the server but has not yet resolved its identity is still capable of
straddling a commit — ``_resolve_role()`` is itself a store read and is a very
plausible place for a request to be parked. So a reader registers an UNBOUND
ARRIVAL ticket at the top of ``do_GET``, before any identity exists, and every
waiting writer counts unbound tickets that predate it. The ticket then BINDS to
the resolved ``user_id``; binding to a different user drops it from that
writer's wait set immediately, so cross-user coupling is bounded by one session
lookup. A request with no ``user_id`` at all (the identity-less demo
fallbacks) drops its ticket and takes nothing — the same rule the #386 mutation
mutex applies.

LOCK ORDER: this gate is level 0, strictly OUTERMOST, above
``active_context_mutex`` (1), ``store.transaction()``/the store ``_lock`` (2),
the ActiveContext row (3), parents/bridges (4), targets (5). The invariant is
one sentence: **a gate holder never holds a store lock while waiting for the
gate, and a store-lock holder never waits for the gate.** Acquiring this gate
INSIDE ``store.transaction()`` would let a reader already inside the database
wait for a writer waiting to get in, and would close a cycle against every
scheduler mutation that descends ActiveContext -> parent -> target under that
same ``_lock``. Take it first and alone.

NOTHING BLOCKS FOREVER. Both waits are bounded by ``wait_timeout``
(``HS_CONTEXT_GATE_TIMEOUT``). On expiry the waiter PROCEEDS, the expiry is
counted in ``stats()["timeouts"]`` and one line is written to stderr: a scoped
read that never returns must not be able to lock an operator out of switching
context. The bound is the handled failure mode, not a promise that it never
happens — and hitting it means the ordering guarantee was off for that one
request, which is why it is said out loud rather than only counted.

WHAT AN UNAUTHENTICATED CALLER CAN DO WITH THIS, since the arrival ticket is
taken before identity exists. It can add unbound tickets to the set a switch
waits on. That is bounded twice over and deliberately: a writer waits only for
the FINITE set of tickets that already existed when it registered (never for
later arrivals, so no stream of requests can extend the wait), each such ticket
lives only until the request finishes ``_resolve_role()``, and the whole wait is
capped by ``wait_timeout`` regardless. The alternative — registering only after
identity — is what falsifier 2 in ``tests/test_context_switch_server_exit.py``
shows to be broken: it lets a switch commit straight past a read parked inside
``_resolve_role()``, which is the most plausible reading of the CI failure.

HONEST SCOPE LIMIT: this gate is PER PROCESS. The deployment is a single
``ThreadingHTTPServer``; there is no replica/worker configuration in ``k8s/`` or
``render.yaml``. If the app is ever run multi-replica, a read on replica A and a
POST on replica B are NOT ordered by this gate and the fix would have to be
re-expressed at the database — which, for reason (1) above, cannot be done with
the existing advisory lock. This paragraph is the record of that, so it is not
discovered later.
"""

import os
import sys
import threading
from contextlib import contextmanager

__all__ = ["ContextSwitchGate", "DEFAULT_WAIT_TIMEOUT"]

# Long enough that no honest read on a healthy server ever reaches it, short
# enough that a wedged one cannot hold an operator's context switch hostage for
# a session. Overridable for tests and for operators who measure differently.
DEFAULT_WAIT_TIMEOUT = 10.0
_WAIT_TIMEOUT_ENV = "HS_CONTEXT_GATE_TIMEOUT"


def _configured_timeout():
    raw = os.environ.get(_WAIT_TIMEOUT_ENV)
    if not raw:
        return DEFAULT_WAIT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_WAIT_TIMEOUT
    return value if value > 0 else DEFAULT_WAIT_TIMEOUT


class _Ticket:
    """One participant's registration. ``seq`` is its arrival order and is the
    only thing any wait predicate compares."""

    __slots__ = ("seq", "user_id", "bound", "waiting", "timed_out")

    def __init__(self, seq, user_id=None):
        self.seq = seq
        self.user_id = user_id
        self.bound = user_id is not None
        self.waiting = False
        self.timed_out = False


class ReaderTicket:
    """The reader's handle. Created UNBOUND by :meth:`ContextSwitchGate.arrive`
    at the top of the request, before any identity exists; converted to a
    per-user SHARED hold by :meth:`bind` once ``_resolve_role()`` has answered.
    """

    def __init__(self, gate, ticket):
        self._gate = gate
        self._ticket = ticket

    @property
    def seq(self):
        return self._ticket.seq

    @contextmanager
    def bind(self, user_id):
        """Convert the arrival ticket into a SHARED hold for ``user_id``.

        Blocks only for switches that registered BEFORE this request arrived,
        so a read already in flight is never made to wait for a switch that
        started after it. A falsy ``user_id`` — the identity-less demo
        fallbacks, which own no context row — releases the ticket and takes
        nothing.

        The hold is released on exit, which callers place BEFORE the response
        is written: a slow or dead client socket must never be able to pin the
        gate.
        """
        if not user_id:
            self.release()
            yield self
            return
        self._gate._bind(self._ticket, user_id)
        try:
            yield self
        finally:
            self.release()

    def release(self):
        """Idempotent. Safe to call from an outer ``finally`` after ``bind``
        has already released, and safe on a request that never bound."""
        self._gate._release_reader(self._ticket)


class ContextSwitchGate:
    """A per-user SHARED/EXCLUSIVE quiescence gate. Pure ``threading`` — it
    knows nothing about HTTP, the store, or the domain, which is exactly why it
    behaves identically on the in-memory, SQLite and PostgreSQL backends."""

    def __init__(self, wait_timeout=None):
        self._cv = threading.Condition(threading.Lock())
        self._seq = 0
        # Keyed by the ticket's own arrival `seq`, which is unique and
        # monotonic for the life of the process — never by identity, which a
        # freed object can hand back to an unrelated successor.
        self._readers = {}          # seq -> _Ticket
        self._writers = {}          # seq -> _Ticket
        self._timeouts = 0
        self.wait_timeout = (_configured_timeout() if wait_timeout is None
                             else wait_timeout)

    # -- reader side --------------------------------------------------------
    def arrive(self):
        """Register an UNBOUND arrival. Never blocks: arriving is not the same
        as being admitted, and a request that has not yet been identified must
        still be VISIBLE to a switch that is about to wait."""
        with self._cv:
            self._seq += 1
            ticket = _Ticket(self._seq)
            self._readers[ticket.seq] = ticket
            return ReaderTicket(self, ticket)

    def _bind(self, ticket, user_id):
        with self._cv:
            if ticket.seq not in self._readers:      # already released
                return
            ticket.user_id = user_id
            # Blocked only by switches for this user that arrived FIRST.
            def blocked():
                return any(w.user_id == user_id and w.seq < ticket.seq
                           for w in self._writers.values())
            self._await(ticket, blocked)
            ticket.bound = True
            # Binding narrows this ticket from "every waiting writer" to "this
            # user's writers", so a writer for a DIFFERENT user may now be
            # unblocked. Announce it.
            self._cv.notify_all()

    def _release_reader(self, ticket):
        with self._cv:
            if self._readers.pop(ticket.seq, None) is not None:
                self._cv.notify_all()

    # -- writer side --------------------------------------------------------
    @contextmanager
    def exclusive(self, user_id):
        """Hold the EXCLUSIVE gate for ``user_id`` across the context commit.

        On entry it waits for exactly the participants that were already inside
        when it registered: unbound arrivals, this user's shared holds, and this
        user's earlier switches. It never waits for anything that registers
        afterwards — and from the instant it registers, a NEW shared hold for
        this user waits for IT, which is what stops a reader admitted mid-
        quiesce from straddling the commit.

        A falsy ``user_id`` takes nothing; such a caller cannot own a context
        row and is refused at the boundary before reaching here.
        """
        if not user_id:
            yield None
            return
        with self._cv:
            self._seq += 1
            ticket = _Ticket(self._seq, user_id)
            self._writers[ticket.seq] = ticket

            def blocked():
                for r in self._readers.values():
                    # An UNBOUND arrival counts: it may yet turn out to be this
                    # user's, and it is already inside the server.
                    if r.seq < ticket.seq and r.user_id in (None, user_id):
                        return True
                for w in self._writers.values():
                    if w is not ticket and w.user_id == user_id \
                            and w.seq < ticket.seq:
                        return True
                return False

            self._await(ticket, blocked)
        try:
            yield ticket
        finally:
            with self._cv:
                self._writers.pop(ticket.seq, None)
                self._cv.notify_all()

    # -- the bounded wait ---------------------------------------------------
    def _await(self, ticket, blocked):
        """Wait while ``blocked()`` — but never past ``wait_timeout``.

        Called with ``self._cv`` held. On expiry the caller PROCEEDS: a
        participant that never returns must not be able to block another one
        permanently. The expiry is recorded rather than swallowed, so the
        pathological case is observable instead of merely survivable.
        """
        if not blocked():
            return
        ticket.waiting = True
        try:
            # `wait_for` returns the predicate's final value: False means the
            # timeout expired with the condition still unmet.
            satisfied = self._cv.wait_for(lambda: not blocked(),
                                          timeout=self.wait_timeout)
            if not satisfied:
                ticket.timed_out = True
                self._timeouts += 1
                # SAID OUT LOUD, once per expiry. A bound that is hit silently
                # is the failure mode that would make this gate LOOK like it
                # works while the race it exists for is happening again: the
                # waiter proceeds, and the ordering guarantee is off for that
                # one request. `stats()["timeouts"]` is the machine-readable
                # form; this line is the one a human running the server sees.
                print(
                    f"[context-gate] wait bound of {self.wait_timeout}s expired "
                    f"for {'switch' if ticket.bound else 'scoped read'} "
                    f"seq={ticket.seq}; proceeding UNORDERED. A scoped read or "
                    f"a switch is taking longer than the bound.",
                    file=sys.stderr, flush=True)
        finally:
            ticket.waiting = False

    # -- observability ------------------------------------------------------
    def stats(self):
        """A snapshot for tests and diagnostics. ``readers``/``writers`` back to
        zero is what "nothing was leaked" means here; it is measured, never
        asserted in prose."""
        with self._cv:
            return {
                "readers": len(self._readers),
                "unbound_readers": sum(1 for r in self._readers.values()
                                       if not r.bound),
                "writers": len(self._writers),
                "waiting_readers": sum(1 for r in self._readers.values()
                                       if r.waiting),
                "waiting_writers": sum(1 for w in self._writers.values()
                                       if w.waiting),
                "timeouts": self._timeouts,
            }
