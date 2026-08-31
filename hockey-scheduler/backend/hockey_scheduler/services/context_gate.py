"""Per-user quiescence gate ordering exact-Season scoped READS against the
context SWITCH that would invalidate them (#159 follow-up).

THE DEFECT THIS EXISTS FOR. Several GET routes are ceilinged to the caller's
EXACTLY selected Season. Their handlers resolve the persisted ActiveContext
INSIDE the request and refuse, generically, any target that is not inside the
selected tuple. If a ``POST /api/context`` commits a NEW selection while one of
those reads is still inside the server, the read is judged against a tuple it
never saw and answers a refusal for something the operator was legitimately
looking at. CI reproduced exactly that on
``/api/v2/setup/seasons/<id>/venue-candidates`` (run 31504917446, browser shard
1, phone leg).

WHICH ROUTES, and why that is a table and not a rule of thumb. The authoritative
list is ``CONTEXT_SCOPED_READ_ROUTES`` in ``web/server.py``, which carries the
per-route enumeration and the criterion a route must meet to be in it. Five
routes are listed today: the two venue reads above, ``GET
/api/scheduler/scenarios/<id>`` (refused as ``_scenario_not_found``), ``GET
/api/standings/<division_id>`` (whose mismatch is the generic EMPTY standings
shape — a wrong answer that looks like a real one), and ``GET
/api/standings/league-season/<l>/<s>`` (whose mismatch is the generic
``not_found``). The last three are NOT the CI incident; they were found by
auditing the criterion against the code, and the first cut of this module named
only the two it had a failure for. The fifth is the newest and arrived by the
route acquiring the ceiling rather than by an audit finding one already there:
before #202 it resolved no tuple at all — it answered ANONYMOUS callers — so it
genuinely did not qualify, and listing it then would have been wrong. If a
route grows the exact-selected-Season ceiling later, it belongs in that table,
and this paragraph is here so that is not rediscovered from an outage.

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
writer's wait set immediately, so cross-user coupling is bounded by identity
resolution — ONE session lookup on the two ``/api/standings/...`` reads, and TWO
on the routes that pre-check with ``_operator_only``, which calls
``_resolve_role()`` and is then followed by the branch calling it again
(``SESSIONS.resolve`` is uncached). Pre-existing for the venue reads and
inherited by the routes added later; stated here in the measured form rather
than the flattering one. A request with no ``user_id`` at all (the identity-less demo
fallbacks) drops its ticket and takes nothing — the same rule the #386 mutation
mutex applies.

THAT ONE-SESSION-LOOKUP BOUND IS ENFORCED, NOT MERELY INTENDED, and the way it
is enforced is not obvious: ``_bind`` must ANNOUNCE the narrowing BEFORE it
waits on its own predicate, because the narrowing thread can itself go to sleep
inside ``wait_for`` and would otherwise hold the announcement hostage for the
whole of its own wait. The first cut announced afterwards, which made the real
coupling the other operator's entire switch — and, transitively, whatever THEY
were waiting on. It was also invisible: ``wait_for`` re-evaluates its predicate
at the deadline, finds it true, and reports SUCCESS, so no expiry is recorded
and ``stats()`` reads healthy throughout. Nothing in a single-operator test can
see any of this, which is why
``test_one_operators_switch_is_not_stalled_by_anothers_scoped_read`` drives two
real operators through exactly that interleaving.

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

AND THE HANDLING OF THAT FAILURE IS NOT ITSELF A FAILURE MODE — two separate
rules, both learned the hard way:

1. ``_await`` RETURNS the expiry notice; the callers print it once they are
   outside ``self._cv``. That mutex is process-global and every gate operation
   takes it, so a ``print`` under it lets one stalled stderr — a log collector
   that stopped reading its pipe — freeze ``arrive()``, which is documented
   never to block and is the first statement ``do_GET`` runs for every scoped
   read. The whole server would stop answering scoped reads because one line of
   diagnostics was waiting on a pipe.
2. Every registration is removed by a ``finally`` that spans BOTH the wait and
   the notice. The notice is the module's only I/O and it runs exactly when
   things are already wrong; a registration leaked there is leaked for the life
   of the process, and every later participant for that user then waits out the
   full bound behind a ghost. The wedge handler must not be a wedge.

WHAT AN UNAUTHENTICATED CALLER CAN DO WITH THIS, since the arrival ticket is
taken before identity exists. It can add unbound tickets to the set a switch
waits on. That is bounded twice over and deliberately: a writer waits only for
the FINITE set of tickets that already existed when it registered (never for
later arrivals, so no stream of requests can extend the wait), and the whole
wait is capped by ``wait_timeout`` regardless. It is also NOT a slowloris seam:
``do_GET`` is called only after the request line and headers have been fully
read, so a client that dribbles bytes holds no ticket at all.

Said precisely, because the loose version would be wrong: a ticket that BINDS is
released before its response is written, so a dead client socket cannot pin it.
A scoped-read request that never binds — one refused at the authorization
boundary, or an identity-less demo fallback — keeps its arrival ticket until
``do_GET`` returns, which includes writing that refusal. That write is a small
JSON body into the kernel buffer, and the wait bound covers it regardless; it is
recorded here rather than rounded off to "until ``_resolve_role()`` finishes".
The alternative — registering only after
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

from .epoch_fence import EPOCH_FENCE_GLOBAL_KEY

__all__ = ["ContextSwitchGate", "DEFAULT_WAIT_TIMEOUT",
          "CONTEXT_GATE", "LIFECYCLE_GATE", "LIFECYCLE_GATE_KEY"]

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


def _say(notice):
    """Emit an expiry notice returned by :meth:`ContextSwitchGate._await`.

    MUST be called with the gate mutex RELEASED — that separation is the whole
    point of ``_await`` returning the text instead of printing it. ``None`` (no
    expiry) is the common case and writes nothing.
    """
    if notice:
        print(notice, file=sys.stderr, flush=True)


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
        notice = None
        try:
            with self._cv:
                if ticket.seq in self._readers:      # else: already released
                    ticket.user_id = user_id
                    # ANNOUNCE THE NARROWING BEFORE WAITING ON IT — these two
                    # statements are not reorderable (#159 review). Setting
                    # `user_id` narrows this ticket from "every waiting writer"
                    # to "this user's writers", so a writer for a DIFFERENT
                    # user may now be unblocked; `_await` below SLEEPS inside
                    # `wait_for`. Announcing after that sleep leaves the
                    # foreign writer parked until THIS ticket's own wait ends,
                    # which makes cross-user coupling the other operator's
                    # whole switch — and, transitively, their slow read —
                    # rather than the identity resolution this module's
                    # docstring bounds it to. It is also SILENT: `wait_for`
                    # re-evaluates at the deadline, finds the predicate true
                    # and reports success, so `stats()["timeouts"]` never moves
                    # and the gate looks healthy while an unrelated operator
                    # waits out the bound.
                    self._cv.notify_all()

                    # Blocked only by switches for this user that arrived FIRST.
                    def blocked():
                        return any(w.user_id == user_id and w.seq < ticket.seq
                                   for w in self._writers.values())

                    notice = self._await(ticket, blocked)
                    # `bound` is bookkeeping for `stats()` and for the notice's
                    # wording; NO wait predicate in this module reads it (they
                    # compare `seq` and `user_id` only), so there is
                    # deliberately no second notify here — nothing any waiter
                    # can observe changes below this line.
                    ticket.bound = True
            _say(notice)
        except BaseException:
            # OWN THE INVARIANT LOCALLY (#159 review). ``do_GET``'s outer
            # ``finally`` does release this ticket today, but a registration
            # whose removal depends on a CALLER's discipline is one refactor
            # away from the writer's leak below — and the writer had no such
            # caller to be saved by.
            self._release_reader(ticket)
            raise

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
        # THE REGISTRATION AND THE `try` ARE NOT SEPARABLE (#159 review). From
        # the moment this ticket enters `self._writers` the ONLY thing that
        # removes it is the `finally` below, so every statement that can raise
        # after it — the bounded wait, and the expiry notice that wait may hand
        # back — has to be inside. `_await` performed the notice's I/O itself
        # before this correction, which made the handler for a wedged pipe the
        # thing that leaked a writer PERMANENTLY: every later switch AND every
        # later scoped read for that user would then wait out the full bound
        # behind a participant that had already gone. `_bind` has the same
        # shape and is saved by `do_GET`'s outer `finally`; `exclusive` has no
        # such caller, which is why the asymmetry mattered.
        ticket = None
        try:
            with self._cv:
                self._seq += 1
                ticket = _Ticket(self._seq, user_id)
                self._writers[ticket.seq] = ticket

                def blocked():
                    for r in self._readers.values():
                        # An UNBOUND arrival counts: it may yet turn out to be
                        # this user's, and it is already inside the server.
                        if r.seq < ticket.seq and r.user_id in (None, user_id):
                            return True
                    for w in self._writers.values():
                        if w is not ticket and w.user_id == user_id \
                                and w.seq < ticket.seq:
                            return True
                    return False

                notice = self._await(ticket, blocked)
            _say(notice)
            yield ticket
        finally:
            if ticket is not None:
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

        RETURNS THE EXPIRY NOTICE — it does NOT print it (#159 review). This
        method runs under ``self._cv``, the single process-global gate mutex,
        and a ``print`` is synchronous blocking I/O: doing it here means one
        stalled stderr (a log collector that stopped reading its pipe) freezes
        EVERY gate operation, including ``arrive()``, which is documented never
        to block and is the FIRST statement ``do_GET`` runs for every scoped
        read. The whole server would stop answering scoped reads because one
        line of diagnostics was waiting on a pipe. Both call sites say it out
        loud via ``_say`` once they are outside the lock, so an expiry is still
        announced exactly once.
        """
        if not blocked():
            return None
        ticket.waiting = True
        try:
            # `wait_for` returns the predicate's final value: False means the
            # timeout expired with the condition still unmet.
            satisfied = self._cv.wait_for(lambda: not blocked(),
                                          timeout=self.wait_timeout)
            if satisfied:
                return None
            ticket.timed_out = True
            self._timeouts += 1
            # SAID OUT LOUD, once per expiry (by the caller — see above).
            # PRECISELY: the counter moves here, under the mutex, and the line
            # is printed by the caller just after it releases. An asynchronous
            # exception (a signal, KeyboardInterrupt) landing in that gap would
            # leave the expiry COUNTED but UNSAID — the price of not printing
            # under a process-global lock, and the reason `stats()["timeouts"]`
            # rather than the log is the authority on how often the bound is
            # hit. A bound that is hit silently is the failure mode that would make
            # this gate LOOK like it works while the race it exists for is
            # happening again: the waiter proceeds, and the ordering guarantee
            # is off for that one request. `stats()["timeouts"]` is the
            # machine-readable form; this line is the one a human running the
            # server sees.
            return (
                f"[context-gate] wait bound of {self.wait_timeout}s expired "
                f"for {'switch' if ticket.bound else 'scoped read'} "
                f"seq={ticket.seq}; proceeding UNORDERED. A scoped read or "
                f"a switch is taking longer than the bound.")
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


# -- the two process-wide instances (round-N+1 relocation) ------------------
#
# THESE USED TO BE INSTANTIATED IN ``web/server.py``. Moved here, unchanged in
# every other respect (same class, same construction, same objects once the
# module has loaded — Python caches module imports, so every importer below
# gets the SAME ``CONTEXT_GATE``/``LIFECYCLE_GATE``, never a second instance
# that would silently stop contending with the first), because round-N+1's
# fix for finding 1 (the Memory/SQLite "produce() already ran before the
# discard" defect) needs writers OUTSIDE ``web/server.py`` — ``api/service.py``
# (``_guarded_attempt``, ``assign_official``/``unassign_official``/
# ``assign_player_team``/``create_guardian_link``/``verify_guardian_link``/
# ``rebind_user_account_scope``/``set_user_account_active``) — to take these
# SAME gates the way the context-switch and archive/reopen writers already do.
# ``api/service.py`` cannot import them FROM ``web/server.py``: ``web/server.py``
# already imports ``ApiService`` from ``api/service.py``, so the reverse
# import would be circular. ``services/context_gate.py`` imports nothing from
# either module (pure ``threading``, per this file's own opening docstring),
# so it is the natural shared home — exactly how ``services/epoch_fence.py``
# already serves as the shared home for the per-user/global KEY convention
# both layers use.
#
# ONE gate per process, beside ``STATE`` and with the same lifetime — it
# orders REQUESTS, not data, so it deliberately survives ``STATE.reset()``/
# store swaps. See this module's own docstring above for the ordering
# argument, the lock level, and the honest per-process scope limit.
CONTEXT_GATE = ContextSwitchGate()

# A SECOND, INDEPENDENT instance of the SAME primitive, ordering scoped reads
# (and, as of round-N+1, every global-keyed writer — see below) against Season
# and other installation-wide LIFECYCLE mutations instead of per-user context
# switches. See ``web/server.py``'s historical comment (now here) for the full
# defect/fix argument this gate exists to satisfy — repeated in brief:
#
# WHY A GATE, NOT A SHARED DATABASE TRANSACTION. Holding the store's own
# process-wide lock across an unbounded dependent read stalls every OTHER
# request touching the store and can deadlock a caller that parks a read
# INSIDE a wrapped service call while reading the store directly on another
# thread. A GATE holds nothing but its own lightweight condition variable, so
# a held reader never blocks unrelated store access.
#
# WHY GLOBAL, NOT PER-USER LIKE ``CONTEXT_GATE``. A Season's lifecycle (and,
# as of round-N+1, every OTHER writer keyed globally by
# ``services/epoch_fence.py``'s ``EPOCH_FENCE_GLOBAL_KEY`` — Program/League/
# LeagueSeason lifecycle+delete, venue-access revoke/delete, Team transfer,
# Official assign/unassign, Player/Guardian reassignment, Guardian link
# create/verify) is SHARED state: it can affect every user who currently has
# it in view, not only the acting operator, so keying this gate by the
# acting user's own id would not order it against ANOTHER user's in-flight
# scoped read at all. ``LIFECYCLE_GATE_KEY`` is therefore one constant every
# participant uses, making this gate's shared/exclusive split GLOBAL rather
# than per-identity.
LIFECYCLE_GATE = ContextSwitchGate()

# THE SAME OBJECT as ``services/epoch_fence.py``'s ``EPOCH_FENCE_GLOBAL_KEY``
# (imported, not a re-spelled duplicate) — one wire/log value for "the one
# shared, global epoch-affecting concern" across BOTH the in-process gate and
# the database-coordinated fence, so the two can never drift apart the way two
# independently-maintained string literals could. ``tests/test_epoch_fence.py``
# already asserts this constant equals ``EPOCH_FENCE_GLOBAL_KEY``.
LIFECYCLE_GATE_KEY = EPOCH_FENCE_GLOBAL_KEY
