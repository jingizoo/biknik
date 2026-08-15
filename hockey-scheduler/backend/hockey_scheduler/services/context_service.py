"""Per-user active Program/Season/League context — the selection backend (#159/#345).

Which Program + Season a user is currently working in. A VIEW selection only: it
never grants authority. On EVERY resolve and set it is filtered through the
caller's real role + account scope (`context_scope`), so a scoped account can
neither select nor enumerate a Program/Season outside its scope. A saved
selection whose underlying record is deleted or which falls outside the caller's
current scope is IGNORED on resolve (a deterministic authorized fallback is
returned) but is NOT rewritten, so if authorization is later restored the saved
choice resolves again. Program-only selection (no Season) is supported for
new/empty Programs; an archived Season is honored as a read-only HISTORICAL
context (writes against it stay blocked by the Season read-only guard), never
silently replaced by an unrelated active Season.

**Snapshot consistency.** ``resolve`` / ``set`` do every read inside ONE
``store.transaction()`` and return the *exact* Program/Season objects they
validated — not scalar ids the caller must re-fetch. The HTTP layer serializes
those very objects and derives ``read_only`` from the returned Season, so the
rendered payload can never internally contradict itself (``read_only`` always
agrees with the serialized Season status; a non-null id always has its object)
even if a concurrent archive / reopen / delete lands between requests.

**Authorization is linearizable with scope-changing writes.** The whole scope
computation + selection (and, for ``set``, the write) runs under ONE SERIALIZABLE
snapshot (``_snapshot``, with bounded retry on a serialization conflict), so a
concurrent revocation (Official unassign, Player/Guardian reassignment) either
orders entirely before this request (it sees the old scope) or entirely after it
(it sees the new scope) — the result can never be a hybrid of the two (e.g. an
old Program set with a now-empty Season set). Memory/SQLite get the same guarantee
for free: each store's per-INSTANCE transaction lock (not a process-wide one)
fully serializes every transaction taken through that store, and on SQLite the
``BEGIN IMMEDIATE`` of #392 extends it to any other connection on the file.

**League is the optional third axis (#345).** It is added ADDITIVELY: ``resolve``,
``options`` and ``set`` keep their exact pre-#345 signatures and tuple shapes, and
the League-aware behavior is exposed as the separate ``resolve_with_league`` /
``options_with_league`` / ``set_with_league``, so the HTTP and context-bar layers
can adopt it in a later slice without this one touching transport or UI. Every
guarantee above extends to it unchanged: one snapshot, bounded retry, detached
objects, non-oracle rejection, ignore-don't-rewrite on an invalid saved value.
Two rules are specific to it — a League must belong to the selected Program, and
a Season+League pair must name an EXISTING ``LeagueSeason`` binding, which the
context resolves read-only and never creates.
"""

import copy
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

from ..domain import ActiveContext, SeasonStatus
from ..domain.errors import (
    ConcurrencyConflictError, NotFoundError, ValidationError)
from . import context_scope
from .context_epoch import context_epoch as _epoch_hash
from .epoch_fence import user_fence_key
from .league_scope import exact_league_season_or_conflict

# The context authorization + selection runs under one SERIALIZABLE snapshot so a
# request can never observe a hybrid of pre- and post-revocation scope (#159). A
# serialization conflict (e.g. two concurrent writes for one user, or a scope
# change the snapshot anti-depends on) is retried a bounded number of times; each
# attempt re-reads a fresh consistent snapshot. Memory/SQLite serialize via each
# store's own per-INSTANCE transaction lock (a ``threading.RLock``, not — as this
# said before — a process-wide one), so no SERIALIZATION conflict arises there.
# On SQLite the retry is not dead code, though: since #392 ``transaction()``
# opens with ``BEGIN IMMEDIATE``, so a second connection to the same file can
# make BEGIN itself fail ``lock_not_available`` after the busy handler gives up,
# and that lands here as the same ConcurrencyConflictError. This loop already
# handles it.
_SNAPSHOT_ISOLATION = "SERIALIZABLE"
_MAX_SNAPSHOT_RETRIES = 10

# (program, season) — the exact validated objects (either may be None), read
# within one transaction so the caller renders a single authoritative snapshot.
_Resolved = Tuple[Optional[object], Optional[object]]


def _detached(obj):
    """A copy of ``obj`` detached from the store's live row. Response rendering
    happens AFTER the transaction lock is released, and ``InMemoryStore`` hands
    back its shared, mutable rows; returning a private copy means a concurrent
    in-place archive/reopen can never mutate the object mid-render (so the two
    reads ``_context_view`` makes of it — ``read_only`` then serialization —
    always see one frozen value). ``SqlStore`` already materializes a fresh row
    per read; the copy is a harmless no-op there."""
    return copy.copy(obj) if obj is not None else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _season_sort_key(season):
    """Latest-first ordering key that parses the boundary date semantically and
    is robust to a date-only (tz-naive) or absent start_date (#272). A null date
    sorts EARLIEST (a dated Season is always preferred as 'most recent'); ties
    break on id, so ordering never relies on insertion or raw-string order."""
    dt = season.start_date
    if dt is None:
        return (0, "", season.id)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)   # a date-only boundary = UTC midnight
    return (1, dt.astimezone(timezone.utc).isoformat(), season.id)


class ContextService:
    def __init__(self, store, clock: Callable[[], datetime] = _utcnow):
        self.store = store
        self.clock = clock

    def _snapshot(self, work):
        """Run ``work()`` inside ONE serializable transaction, so the whole
        authorization computation + selection (and, for ``set``, the write) reads
        a single consistent snapshot — the result always corresponds wholly to
        the pre- OR post-revocation scope, never a hybrid. Retry a bounded number
        of times on a serialization conflict (each retry re-reads a fresh
        snapshot); a domain error (e.g. a non-oracle not-found) is not a conflict,
        so it propagates immediately and unchanged."""
        for attempt in range(_MAX_SNAPSHOT_RETRIES):
            try:
                with self.store.transaction(isolation=_SNAPSHOT_ISOLATION):
                    return work()
            except ConcurrencyConflictError:
                if attempt == _MAX_SNAPSHOT_RETRIES - 1:
                    raise
        # Unreachable: the loop either returns or re-raises on the last attempt.
        raise AssertionError("snapshot retry loop exited without a result")

    def _next_generation_locked(self, user_id):
        """The generation to write on THIS commit (#159 review findings 2+5):
        the currently persisted row's generation (0 if none has ever been
        written) plus one. MUST be called immediately before the write, and
        both MUST run inside the SAME ``_snapshot`` — the read-then-write is
        made atomic by that transaction's isolation (a bounded retry on
        conflict), exactly the pattern every other read-then-write in this
        service already relies on, not by any locking done here.

        A MONOTONIC counter rather than a timestamp on purpose: two commits
        landing inside the same wall-clock tick would write the same
        ``updated_at``, so a token derived from the tuple + timestamp could
        reuse itself across an A -> B -> A round trip. This can't, because it
        is READ before each write and always written one higher than what was
        just read — never derived from a clock at all."""
        current = self.store.get_active_context(user_id) if user_id else None
        return (getattr(current, "generation", 0) or 0) + 1

    # -- resolution --------------------------------------------------------
    def resolve(self, user_id: Optional[str], role, scope) -> _Resolved:
        """The caller's effective ``(program, season)`` — the exact objects to
        render, read within ONE transaction:

        * the saved selection when its Program is still authorized+present and
          its Season (if any) is still authorized+present — an ARCHIVED saved
          Season is honored as a read-only historical context;
        * else a deterministic authorized fallback (an active Season when the
          scope has one, otherwise a Program-only context);
        * else an empty context ``(None, None)``.

        The caller derives ``read_only`` from the returned Season and serializes
        these very objects, so the payload is snapshot-consistent by construction.
        The returned objects are DETACHED from the store's live rows before the
        lock is released, so rendering cannot observe a concurrent in-place edit.
        All reads run under one serializable snapshot (``_snapshot``), so the
        result never mixes pre- and post-revocation scope.
        """
        def work():
            program, season = self._resolve_locked(user_id, role, scope)
            return _detached(program), _detached(season)
        return self._snapshot(work)

    def _resolve_locked(self, user_id, role, scope) -> _Resolved:
        """The validated ``(program, season)`` live rows — MUST run inside the
        transaction; ``resolve`` detaches the result before the lock releases."""
        programs = context_scope.authorized_program_ids(
            self.store, role, scope, user_id)
        saved = self.store.get_active_context(user_id) if user_id else None
        if saved and saved.program_id in programs:
            program = self.store.get_program(saved.program_id)
            if program is not None:
                if saved.season_id is None:
                    return program, None                      # program-only
                season = self.store.get_season(saved.season_id)
                seasons = context_scope.authorized_season_ids(
                    self.store, role, scope, saved.program_id, user_id)
                if season is not None and saved.season_id in seasons:
                    return program, season
                # Season deleted or no longer authorized → do not dangle and do
                # not invent an unrelated active Season under the same Program;
                # take the deterministic fallback below.
        return self._fallback(role, scope, user_id, programs)

    def _fallback(self, role, scope, user_id, programs) -> _Resolved:
        candidates = sorted(
            (p for pid in programs if (p := self.store.get_program(pid))),
            key=lambda p: p.id)
        # Prefer a Program with an authorized ACTIVE Season (an active work
        # context); pick that Season by semantic date (latest), id as tiebreak.
        for program in candidates:
            season_ids = context_scope.authorized_season_ids(
                self.store, role, scope, program.id, user_id)
            active = [s for s in (self.store.get_season(sid) for sid in season_ids)
                      if s is not None and s.status != SeasonStatus.ARCHIVED]
            if active:
                return program, max(active, key=_season_sort_key)
        # No authorized active Season anywhere → a Program-only context on the
        # first authorized Program (supports new/empty Programs, #159 gate).
        if candidates:
            return candidates[0], None
        return None, None

    # -- enumeration (switcher options) ------------------------------------
    def options(self, user_id: Optional[str], role, scope):
        """The AUTHORIZED (Program, [Season]) options for the context switcher,
        plus the currently-resolved ``(program, season)`` — everything computed
        under ONE serializable snapshot (``_snapshot``), through the SAME
        ``context_scope`` rules ``set`` enforces. So the switcher can only ever
        offer a context the caller could actually select (no enumeration of an
        unrelated Program/Season), and the offered list agrees with the current
        selection. Every Program the caller may see is listed (Program-only is
        always selectable); each carries only its authorized Seasons — active and
        archived alike, the latter flagged read-only historical. Returns
        ``(programs, selected_program, selected_season)`` where ``programs`` is a
        list of ``(program, [season, ...])`` of DETACHED rows (safe to serialize
        after the lock releases)."""
        def work():
            authorized = context_scope.authorized_program_ids(
                self.store, role, scope, user_id)
            programs = []
            for pid in sorted(authorized):
                program = self.store.get_program(pid)
                if program is None:
                    continue
                season_ids = context_scope.authorized_season_ids(
                    self.store, role, scope, pid, user_id)
                seasons = sorted(
                    (s for sid in season_ids
                     if (s := self.store.get_season(sid)) is not None),
                    key=_season_sort_key, reverse=True)   # latest first
                programs.append((_detached(program),
                                 [_detached(s) for s in seasons]))
            sel_program, sel_season = self._resolve_locked(user_id, role, scope)
            return programs, _detached(sel_program), _detached(sel_season)
        return self._snapshot(work)

    # -- League axis (#345) ------------------------------------------------
    # Additive throughout: `resolve`/`options`/`set` above keep their exact
    # signatures and 2-/3-tuple shapes, because `api/service.py` unpacks them
    # positionally. Every League-aware entry point is a NEW method returning a
    # wider tuple, so the HTTP/UI integration slice can adopt them one at a time
    # without a flag day and without this slice touching the transport layer.

    def _resolve_league_locked(self, user_id, role, scope, program, season):
        """The saved League for an already-resolved ``(program, season)`` — live
        row or None. MUST run inside the transaction.

        Returns None (Season-without-League, a first-class state) whenever the
        saved League is absent, deleted, cross-Program, unauthorized, or — with a
        Season selected — not bound to it. The saved row is NEVER rewritten here,
        so restoring the League or the caller's authorization restores the
        choice, exactly as the Program/Season axes already behave.

        The League is deliberately NOT re-derived from anything else when it is
        invalid: dropping to None keeps the operator's validated Program/Season
        intact and never invents a League they did not choose — the same
        "do not invent an unrelated one" rule `_resolve_locked` applies to
        Season. It also means this method can never change the Program/Season
        that `resolve()` already returned, which is what keeps the pre-#345
        two-axis contract byte-identical.
        """
        if program is None:
            return None
        saved = self.store.get_active_context(user_id) if user_id else None
        league_id = getattr(saved, "league_id", None) if saved else None
        if not league_id:
            return None
        # The saved League only applies to the context it was saved against: if
        # the Program/Season resolution fell back to something else, the saved
        # League is stale by construction and is dropped rather than reattached.
        if saved.program_id != program.id:
            return None
        saved_season = saved.season_id
        current_season = season.id if season is not None else None
        if saved_season != current_season:
            return None
        authorized = context_scope.authorized_league_ids(
            self.store, role, scope, program.id, user_id,
            season_id=current_season)
        if league_id not in authorized:
            return None
        return self.store.get_league(league_id)

    def resolve_with_league(self, user_id: Optional[str], role, scope,
                            lock=False):
        """``(program, season, league)`` — the League-aware form of
        :meth:`resolve`, with identical Program/Season semantics.

        The first two elements are always exactly what :meth:`resolve` returns
        for the same arguments; the third is the saved League when it is still
        valid for that resolved context, else None. All three are read under ONE
        serializable snapshot and DETACHED before the lock releases, so a
        concurrent unbind/delete/revocation cannot make the triple internally
        contradict itself.

        ``lock=True`` additionally takes the caller's ActiveContext ROW LOCK
        (#386) and holds it for the rest of the SURROUNDING transaction, which
        the caller must own. A snapshot alone answers "what was the tuple when
        I read it"; the lock is what makes the answer still true when a write
        authorized by it lands. `set_active_context` takes the same lock before
        writing, so a concurrent context switch either orders wholly before
        this read (and is seen) or waits until the authorized write has
        committed. Every MUTATING active-tuple gate uses this form; a read-only
        gate does not need it and must not take it, or ordinary reads would
        serialize against each other for nothing.
        """
        def work():
            if lock:
                self.store.get_active_context_for_update(user_id)
            program, season = self._resolve_locked(user_id, role, scope)
            league = self._resolve_league_locked(
                user_id, role, scope, program, season)
            return _detached(program), _detached(season), _detached(league)
        return self._snapshot(work)

    def resolve_saved_with_league(self, user_id: Optional[str], role, scope,
                                  lock=False):
        """``(saved_row, program, season, league)`` derived ONLY from the
        PERSISTED ``ActiveContext`` row — ``_fallback()`` is NEVER called (#409).

        THE AUTHORIZATION SOURCE for every mutation gate. :meth:`resolve` and
        :meth:`resolve_with_league` answer "what context should this caller be
        shown", and their answer may be one ``_fallback()`` INVENTED; that is
        the owner's ruling for landing and navigation and it is untouched here.
        This answers the different question a WRITE has to ask: "what did the
        operator themselves CHOOSE, and is it still valid?".

        The distinction is not cosmetic and it cannot be recovered by comparing
        values afterwards. ``_fallback()`` may well pick the very Program the
        row names — it walks the caller's authorized Programs in id order, and
        with one Program in the installation it picks that one every time. A
        resolved value that merely EQUALS the saved one is not evidence that it
        CAME from it, so a predicate built on the resolve is only accidentally
        right, and stops being right the moment the inventory changes. Reading
        the row and validating IT is the only form of the check that cannot be
        satisfied by coincidence.

        Each axis is validated exactly as ``_resolve_locked`` validates it, so
        an honoured saved selection is BYTE-FOR-BYTE the tuple that method
        already returns for the same row; the only difference is what happens
        when an axis does not validate. Here the axis is simply dropped:

        * saved row absent, or naming no Program -> ``(row, None, None, None)``;
        * Program deleted or no longer authorized -> ``(row, None, None, None)``
          — the Program axis is the root, so nothing below it can stand;
        * Season deleted or no longer authorized -> the Program is KEPT and the
          Season becomes ``None``, which is exactly the shape a deliberate
          Program-only selection already has. A caller whose Season went stale
          is therefore refused by a two-axis gate and admitted by a
          Program-axis one, which is the #409 rule;
        * the League axis reuses :meth:`_resolve_league_locked` unchanged.

        ``lock=True`` takes the caller's ActiveContext ROW LOCK
        (``get_active_context_for_update``, #386) and holds it for the rest of
        the SURROUNDING transaction, which the caller must own — and, unlike
        :meth:`resolve_with_league`, the locked read's RESULT is what is
        judged, rather than a lock taken beside a separate unlocked read.
        """
        def work():
            saved, program, season, league = self._saved_locked(
                user_id, role, scope, lock=lock)
            return (_detached(saved), _detached(program), _detached(season),
                    _detached(league))
        return self._snapshot(work)

    def _saved_locked(self, user_id, role, scope, lock=False):
        """The persisted row and the axes it still validly names, as LIVE rows —
        MUST run inside the transaction; every caller detaches its result before
        the lock releases.

        Extracted from :meth:`resolve_saved_with_league` so the switcher's
        enumeration (:meth:`options_with_saved`) can report the SAME authority
        the mutation gates are judged against, under the SAME snapshot as the
        options beside it, rather than through a second read that could disagree
        with them. The rules are that method's rules verbatim — read its
        docstring for what each axis drops and why ``_fallback()`` is never
        consulted here."""
        saved = None
        if user_id:
            saved = (self.store.get_active_context_for_update(user_id)
                     if lock else self.store.get_active_context(user_id))
        if saved is None or not saved.program_id:
            return saved, None, None, None
        programs = context_scope.authorized_program_ids(
            self.store, role, scope, user_id)
        if saved.program_id not in programs:
            return saved, None, None, None
        program = self.store.get_program(saved.program_id)
        if program is None:
            return saved, None, None, None
        season = None
        if saved.season_id is not None:
            candidate = self.store.get_season(saved.season_id)
            seasons = context_scope.authorized_season_ids(
                self.store, role, scope, saved.program_id, user_id)
            if candidate is not None and saved.season_id in seasons:
                season = candidate
        league = self._resolve_league_locked(
            user_id, role, scope, program, season)
        return saved, program, season, league

    def options_with_league(self, user_id: Optional[str], role, scope):
        """``(programs, selected_program, selected_season, selected_league)``
        where ``programs`` is ``[(program, [season, ...], [league, ...]), ...]``
        — the League-aware form of :meth:`options`.

        Each Program carries the Leagues the caller may actually select under it,
        through the SAME `context_scope` rules :meth:`set_with_league` enforces,
        so the switcher can never offer a League the caller could not select. The
        list is Program-scoped rather than Season-scoped: a League that exists
        under the Program but is not bound to the currently-selected Season is
        still offered, because selecting it is a legitimate way to move to a
        Season+League pair — the binding requirement is enforced at selection
        time, where it can report a precise reason, not silently by omission
        here.
        """
        return self._snapshot(
            lambda: self._options_locked(user_id, role, scope))

    def _options_locked(self, user_id, role, scope):
        """The switcher's enumeration + RESOLVED selection, already detached —
        MUST run inside the transaction. Shared verbatim by
        :meth:`options_with_league` and :meth:`options_with_saved` so the two can
        never drift into offering different option sets."""
        authorized = context_scope.authorized_program_ids(
            self.store, role, scope, user_id)
        programs = []
        for pid in sorted(authorized):
            program = self.store.get_program(pid)
            if program is None:
                continue
            season_ids = context_scope.authorized_season_ids(
                self.store, role, scope, pid, user_id)
            seasons = sorted(
                (s for sid in season_ids
                 if (s := self.store.get_season(sid)) is not None),
                key=_season_sort_key, reverse=True)   # latest first
            league_ids = context_scope.authorized_league_ids(
                self.store, role, scope, pid, user_id)
            leagues = sorted(
                (lg for lid in league_ids
                 if (lg := self.store.get_league(lid)) is not None),
                key=lambda lg: (lg.sort_order or 0, lg.id))
            programs.append((_detached(program),
                             [_detached(s) for s in seasons],
                             [_detached(lg) for lg in leagues]))
        sel_program, sel_season = self._resolve_locked(user_id, role, scope)
        sel_league = self._resolve_league_locked(
            user_id, role, scope, sel_program, sel_season)
        return (programs, _detached(sel_program), _detached(sel_season),
                _detached(sel_league))

    def options_with_saved(self, user_id: Optional[str], role, scope):
        """``(programs, sel_program, sel_season, sel_league,
        saved_program, saved_season, saved_league)`` — :meth:`options_with_league`
        plus the axes the PERSISTED row actually names (#411).

        A new, wider method rather than a changed return shape, exactly as the
        League axis was added: every positional unpacker of
        :meth:`options_with_league` keeps its meaning.

        WHY THE SWITCHER NEEDS BOTH TUPLES. ``sel_*`` is the resolved,
        renderable context and may have been INVENTED by ``_fallback()``;
        ``saved_*`` is what the operator themselves chose, and is the only thing
        a mutation gate will honour (:meth:`resolve_saved_with_league`). On a
        one-Program installation the two are value-identical while nothing is
        persisted at all, so a UI that has only ``sel_*`` cannot help but claim
        a selection the writes will refuse — the display/authority disagreement
        #409 exists to remove. Reporting them side by side, from ONE snapshot,
        is what lets the switcher paint "showing you this" and "you have chosen
        this" as the different facts they are.

        The saved half is deliberately reported as VALIDATED axes rather than as
        the raw row: an operator whose saved Season was deleted still holds a
        real Program-axis selection, and that is precisely what the create gate
        will grant them."""
        # ONE `work()`, not a call to `options_with_league` followed by a call to
        # `resolve_saved_with_league`: two snapshots could be taken either side
        # of a concurrent switch, and the pair the switcher compares would then
        # describe two different moments — the one comparison that must never be
        # able to go wrong, since a false "already saved" hides the only control
        # that can save anything.
        def work():
            programs, sel_program, sel_season, sel_league = self._options_locked(
                user_id, role, scope)
            _row, saved_program, saved_season, saved_league = self._saved_locked(
                user_id, role, scope)
            return (programs, sel_program, sel_season, sel_league,
                    _detached(saved_program), _detached(saved_season),
                    _detached(saved_league))
        return self._snapshot(work)

    # -- epoch (#159 review findings 2+5) -----------------------------------
    # Everything the context epoch (services/context_epoch.py) needs to hash:
    # the EFFECTIVE resolved (program, season, league) `resolve_with_league`
    # would render -- not the raw saved row, which can differ from it whenever
    # the saved Program/Season is deleted/unauthorized and `_fallback` stands
    # in, or whenever the fallback's OWN candidate set changes (a fallback
    # Season getting archived moves who `_fallback` would pick even though
    # nothing about THIS user's saved row changed) -- plus the persisted
    # switch generation, which needs no row-lifecycle argument at all: it is
    # what makes the epoch move on every switch regardless of what the clock
    # or the resolution do.

    def _epoch_material_locked(self, user_id, role, scope):
        """``(generation, program, season, league)`` — MUST run inside the
        caller's transaction; a caller that returns these across the lock
        boundary detaches them first (see :meth:`resolve_epoch_state`).

        Deliberately NOT built on top of :meth:`resolve_with_league`: that
        method opens its OWN ``_snapshot``, and a caller that needs this
        material read inside a WIDER transaction it already holds open — the
        epoch comparison in front of a dependent scoped read,
        :meth:`run_scoped_read`, #159 review finding 3 — needs the
        un-snapshotted form so the whole thing stays ONE transaction rather
        than two independent ones a lifecycle mutation could land between.
        """
        row = self.store.get_active_context(user_id) if user_id else None
        generation = (getattr(row, "generation", 0) or 0) if row else 0
        program, season = self._resolve_locked(user_id, role, scope)
        league = self._resolve_league_locked(
            user_id, role, scope, program, season)
        return generation, program, season, league

    def resolve_epoch_state(self, user_id, role, scope):
        """``(generation, program, season, league)`` read under ONE snapshot
        and DETACHED — the public, self-contained form of
        :meth:`_epoch_material_locked` for a caller (``current_epoch`` below)
        that only needs the epoch's material and is not also running a
        dependent read under the same lock."""
        def work():
            generation, program, season, league = self._epoch_material_locked(
                user_id, role, scope)
            return (generation, _detached(program), _detached(season),
                    _detached(league))
        return self._snapshot(work)

    def current_epoch(self, user_id, role, scope) -> str:
        """The CURRENT context-epoch token for ``user_id`` (#159 review
        finding 2), derived from the EFFECTIVE resolution rather than the raw
        saved row. Used by the three places the client learns its context
        (``GET /api/context``, ``GET /api/context/options``, and the ``POST
        /api/context`` response, all in ``web/server.py``).

        A falsy ``user_id`` is hashed with NO resolution attempted — there is
        no role/scope-driven material to resolve for a caller with no
        identity, and :func:`context_epoch.context_epoch` already has a
        well-defined, stable answer for that case that cannot collide with a
        real (if empty) resolution, because the id itself is part of the
        material."""
        if not user_id:
            return _epoch_hash(None, 0, None, None, None)
        generation, program, season, league = self.resolve_epoch_state(
            user_id, role, scope)
        return _epoch_hash(user_id, generation, program, season, league)

    # NOTE on #159 review finding 3 (the check->service TOCTOU): an earlier
    # revision of this fix added a `run_scoped_read` here that wrapped the
    # epoch comparison AND the dependent service call in one `_snapshot` —
    # correct in isolation, but it holds the store's own lock
    # (`SqlStore`/`InMemoryStore` `self._lock`, ACQUIRED FOR THE WHOLE
    # TRANSACTION) across whatever `produce()` does, which is unbounded from
    # this method's point of view. That deadlocked the EXISTING
    # `test_context_switch_server_exit.py` harness, which parks a read
    # INSIDE a wrapped ApiService method (i.e. inside that transaction) and
    # then has the MAIN TEST THREAD read the store directly via `SqlStore.
    # _get`'s OWN `with self._lock:` — a different thread blocking on a lock
    # the parked thread holds while waiting on the harness to un-park it,
    # which the blocked main thread can now never reach. It is also a live
    # concern outside tests: a slow or blocked dependent read would hold the
    # store's ONE process-wide lock for its full duration, stalling every
    # OTHER request that touches the store at all, not only the one racing
    # a lifecycle mutation. The fix that landed instead orders scoped reads
    # against archive/reopen with a GATE (the same proven shape
    # `web/server.py`'s `CONTEXT_GATE` already uses for the switch
    # dimension) rather than a shared database transaction — see
    # `web/server.py`'s `LIFECYCLE_GATE` and `Handler._read_under_context_
    # gate`. This method stays store-transaction-free on purpose.

    # Every League-selection refusal uses this ONE message/reason, whatever the
    # underlying cause (#364 owner ruling): nonexistent, cross-Program,
    # unauthorized, deleted, archived-only, unbound, revoked, or ambiguous. The
    # sameness is the security property -- distinguishing them would turn the
    # context bar into an existence oracle for records the caller may not see.
    _LEAGUE_REFUSED = ("League not found or not accessible for this context.",
                       {"reason": "league_not_accessible"})

    def _canonical_league_season(self, role, scope, user_id, program_id,
                                 season_id, league_id):
        """The SERVER-owned canonical ``(season_id, league)`` for a League
        selection — MUST run inside the transaction (#364 owner ruling).

        The browser submits League *intent* only; it never infers a binding and
        never reads one out of global overview data. Resolution here is the sole
        authority, in the ruling's exact order:

        1. the current Season is kept when it is itself a valid binding for the
           selected League;
        2. otherwise, when the League has EXACTLY ONE valid authorized ACTIVE
           Season binding in this Program, move atomically to that Season;
        3. zero valid bindings, or several with the current Season not among
           them, refuse generically with zero mutation.

        Rule 3 deliberately does NOT tie-break. A deterministic pick (first by
        date/id) would silently move an operator into a Season they never chose
        and never saw — the same invisible-default class of defect as the
        wrong-Program writes this module already exists to prevent.

        ARCHIVED Seasons are never auto-selected by rule 2, matching
        ``_fallback``'s own existing rule: an archived Season is honored when it
        is *explicitly* chosen (read-only history) but is never something the
        system moves you into on your behalf. An archived-only League therefore
        refuses via rule 3, indistinguishably from any other refusal.
        """
        # Program-scoped authorization first, WITHOUT Season narrowing: this is
        # what makes cross-Program / unauthorized / deleted collapse into the
        # single non-oracle refusal before any binding is considered.
        allowed = context_scope.authorized_league_ids(
            self.store, role, scope, program_id, user_id)
        league = self.store.get_league(league_id)
        if league_id not in allowed or league is None:
            raise NotFoundError(*self._LEAGUE_REFUSED)

        # -- rule 1: keep the current Season when it genuinely binds ----------
        if season_id is not None:
            binding, conflicts = exact_league_season_or_conflict(
                self.store, league_id, season_id)
            if binding is not None:
                return season_id, league
            if conflicts:
                # Duplicate rows at one exact (league, season) key is corrupted
                # state; never let insertion order decide a committed context.
                raise NotFoundError(*self._LEAGUE_REFUSED)

        # -- rule 2: exactly one valid authorized ACTIVE binding --------------
        authorized_seasons = context_scope.authorized_season_ids(
            self.store, role, scope, program_id, user_id)
        candidates = {}
        for ls in self.store.league_seasons_for_league(league_id):
            if ls.season_id not in authorized_seasons:
                continue          # outside this caller's scope, or another Program
            candidate = self.store.get_season(ls.season_id)
            if candidate is None or candidate.status == SeasonStatus.ARCHIVED:
                continue          # deleted, or read-only history (never auto-entered)
            # Re-resolved through the exact-key primitive so an ambiguous
            # binding cannot contribute a candidate.
            exact, dupes = exact_league_season_or_conflict(
                self.store, league_id, candidate.id)
            if exact is None or dupes:
                continue
            candidates[candidate.id] = candidate
        if len(candidates) == 1:
            return next(iter(candidates)), league

        # -- rule 3: zero, or ambiguous -> refuse, mutate nothing -------------
        raise NotFoundError(*self._LEAGUE_REFUSED)

    def set_with_league(self, user_id: Optional[str], role, scope,
                        program_id, season_id, league_id=None):
        """Record a Program/Season/League selection and return the exact
        ``(program, season, league)`` objects validated+written (detached).

        The League-aware form of :meth:`set`, with identical Program/Season
        validation. ``league_id`` may be None — Program-only and
        Season-without-League both remain first-class, so passing None here is a
        deliberate selection of "no League", not a missing argument.

        A League selection is rejected with ONE generic non-oracle not-found —
        identical whether the League does not exist, belongs to another Program,
        is outside the caller's scope, or is not bound to the selected Season —
        so this never becomes an existence oracle for records the caller may not
        see. That single reason is the point: distinguishing them would leak
        exactly what the scope rules exist to hide.

        When both a Season and a League are given, the SERVER resolves the
        canonical tuple (#364 owner ruling) via :meth:`_canonical_league_season`
        — keeping the current Season when it binds, else moving atomically to
        the League's single valid authorized active Season binding, else
        refusing with zero mutation. **The returned Season may therefore differ
        from the requested one**, and callers must render only what is returned
        rather than what they sent.

        This never CREATES a binding: an unbound pair is refused, and binding a
        League to a Season stays the authorized, audited job of
        ``setup_service.create_league_season``.

        UNCHANGED three-tuple contract (PR #423): every existing caller —
        production and the ~60 call sites across
        ``tests/test_league_context_canonical.py``,
        ``tests/test_active_context_league.py`` and
        ``tests/test_league_context_races.py`` — unpacks exactly
        ``(program, season, league)``. See :meth:`set_with_league_and_epoch`
        for the epoch-returning variant the facade uses; this method is now a
        thin wrapper over the same underlying work so the two can never drift
        apart, and drops the fourth element rather than changing this
        widely-depended-on shape.
        """
        program, season, league, _epoch = self._set_with_league_locked(
            user_id, role, scope, program_id, season_id, league_id)
        return program, season, league

    def set_with_league_and_epoch(self, user_id: Optional[str], role, scope,
                                  program_id, season_id, league_id=None):
        """Same as :meth:`set_with_league`, but ALSO returns the epoch
        derived from the SAME snapshot as the write (PR #423 design §8.1) —
        ``(program, season, league, epoch)``.

        CALLED BY THE FACADE, UNCONDITIONALLY (correction: an earlier
        revision of this docstring claimed the opposite — that claim went
        stale the moment round-N review finding 3 landed, and sat
        uncorrected through a full review round; it is fixed here).
        ``api/service.py``'s ``set_active_context`` calls this method —
        never plain :meth:`set_with_league` — on every invocation. Its own
        ``include_epoch`` keyword controls only whether the epoch is kept
        in *that method's* return value (dropped for the ~200 legacy
        callers expecting a bare dict) or paired with the payload (the ONE
        production caller, ``web/server.py``'s ``POST /api/context``
        handler); the underlying call, and the single-snapshot epoch
        derivation it buys, always happens.

        THE RACE THIS CLOSES. Before finding 3, the HTTP handler called
        :meth:`set_with_league` (the write, one transaction) and THEN a
        SEPARATE ``current_epoch()`` call (a second, independent
        transaction) to build the response epoch. Between those two
        transactions, another replica could commit a second switch for the
        SAME user, and the response would then describe THAT replica's
        selection rather than the one this request's own write just made.
        Folding the write and the epoch derivation into ONE ``_snapshot``
        (what :meth:`_set_with_league_locked` does) makes "the epoch
        matches the row this response carries" true by construction.

        WHY THIS CLOSES THE CROSS-PROCESS CASE TOO, not merely the
        in-process one ``CONTEXT_GATE.exclusive(user_id)`` (still held,
        unchanged, at the HTTP call site) already covered on its own.
        :meth:`_set_with_league_locked`'s ``work()`` acquires
        ``epoch_fence_acquire_exclusive(user_fence_key(user_id))`` — a REAL
        PostgreSQL advisory lock — FIRST, before any row is read, and holds
        it for the WHOLE transaction: the epoch is derived at the END of
        that SAME ``work()``, still inside the same held lock, released
        only at commit. A second replica's own call to this method for the
        SAME user therefore cannot even START its write until this one's
        lock releases: the two calls fully serialize, one committing
        (write and epoch together, atomically) before the other's
        ``epoch_fence_acquire_exclusive`` is even granted. There is no
        window for a second replica's write to land between THIS call's
        write and an epoch read, because there is no separate read left to
        land inside — the gap the earlier (now-corrected) revision of this
        docstring described as a "narrow, stated residual gap" is closed,
        not merely narrowed. See ``tests/test_epoch_fence_cross_replica.py``
        for the real two-process, real-authenticated-HTTP proof of exactly
        this claim (round-N+1 finding 3): two independent server
        processes, a deterministic A->B->A (and B->A->B, and a genuinely
        concurrent) ordering, every response's epoch independently
        recomputed from a fresh read of the persisted row.

        Kept as a SEPARATE method from :meth:`set_with_league`, rather than
        changing that method's own return shape, so the widely-depended-on
        two/three-tuple contract ~60 existing test call sites unpack stays
        untouched — the two public methods differ only in whether the
        epoch is dropped, sharing one validation/write/epoch-derivation
        implementation in :meth:`_set_with_league_locked`."""
        return self._set_with_league_locked(
            user_id, role, scope, program_id, season_id, league_id)

    def _set_with_league_locked(self, user_id: Optional[str], role, scope,
                                program_id, season_id, league_id=None):
        """Shared implementation for :meth:`set_with_league` /
        :meth:`set_with_league_and_epoch` — always returns the full
        ``(program, season, league, epoch)`` four-tuple; the two public
        methods differ only in whether they drop the epoch, so the
        validation/write/epoch-derivation logic exists in exactly one
        place."""
        if not user_id:
            raise ValidationError(
                "A signed-in user is required to set a working context.")
        if not program_id:
            raise ValidationError("A program_id is required.",
                                  {"reason": "field_required"})

        def work():
            # PR #423: the epoch fence's EXCLUSIVE hold, FIRST — before any
            # read or lock below, matching the design's general "truly first"
            # placement (§4.2/§4.4/§8.1). Per-user key: this writer already
            # holds the affected account's own id, so it is row 1 of the
            # design's per-user class, ordered against a scoped read's shared
            # hold on the SAME key (``web/server.py``'s
            # ``_read_under_context_gate``). Auto-released at this
            # ``_snapshot``'s commit/rollback.
            self.store.epoch_fence_acquire_exclusive(user_fence_key(user_id))
            # `sid` is a LOCAL working copy of the requested season_id, never a
            # rebinding of the enclosing parameter. Two reasons, both real:
            # assigning the closure variable would make Python treat it as local
            # throughout `work` (an UnboundLocalError on the reads above), and
            # `_snapshot` RE-RUNS `work` on a serialization conflict — a mutated
            # season_id would make the retry resolve from the already-resolved
            # Season instead of the operator's original request.
            sid = season_id
            programs = context_scope.authorized_program_ids(
                self.store, role, scope, user_id)
            program = (self.store.get_program(program_id)
                       if program_id in programs else None)
            if program is None:
                raise NotFoundError("Program not found or not accessible.",
                                    {"reason": "program_not_accessible"})
            season = None
            if sid is not None:
                seasons = context_scope.authorized_season_ids(
                    self.store, role, scope, program_id, user_id)
                season = self.store.get_season(sid)
                if sid not in seasons or season is None:
                    raise NotFoundError("Season not found or not accessible.",
                                        {"reason": "season_not_accessible"})
            league = None
            if league_id is not None:
                # SERVER-owned canonical resolution (#364 owner ruling), for
                # EVERY non-null League — including `sid is None`.
                #
                # #364 re-review: this deliberately no longer special-cases the
                # Program-only path. An earlier revision skipped canonicalization
                # when no Season was selected, on the reasoning that Program-only
                # is a first-class state and picking a Season for the operator
                # would be "choosing on their behalf". That was wrong, for a
                # reason worth keeping written down: the ruling applies whenever
                # a LEAGUE is selected. With no current Season rule 1 simply
                # cannot match, so rule 2 must take the unique valid binding
                # (a unique binding is not a guess) and rule 3 must reject the
                # zero/ambiguous cases. Skipping it let the context persist and
                # render an ACTIVE LEAGUE WITH NO `LeagueSeason` AT ALL, which
                # is precisely the canonical-tuple invariant #367 and #365 are
                # built on. It was reachable from the shipped UI: pick "Program
                # overview (no season)", then pick a League.
                #
                # Program-only stays first-class only where it is genuinely
                # Program-only -- i.e. when `league_id` is null too, which never
                # enters this branch. An explicitly supplied archived Season
                # still behaves as before (honored read-only history), because
                # that is rule 1's exact-binding path, not rule 2's auto-move.
                resolved_sid, league = self._canonical_league_season(
                    role, scope, user_id, program_id, sid, league_id)
                if resolved_sid != sid:
                    # Re-validate the Season we are MOVING TO through the same
                    # authorization gate the requested one passed, so an atomic
                    # move can never land somewhere the caller may not go.
                    seasons = context_scope.authorized_season_ids(
                        self.store, role, scope, program_id, user_id)
                    moved = self.store.get_season(resolved_sid)
                    if resolved_sid not in seasons or moved is None:
                        raise NotFoundError(*self._LEAGUE_REFUSED)
                    sid, season = resolved_sid, moved
            # One write, of the CANONICAL tuple. Validation and this write share
            # the single serializable snapshot `_snapshot` opened around `work`,
            # so a concurrent unbind / archive / revocation / competing context
            # write either orders wholly before this (and is seen, and refuses)
            # or wholly after it — never a half-applied or stale tuple.
            generation = self._next_generation_locked(user_id)
            self.store.set_active_context(ActiveContext(
                id=user_id, program_id=program_id, season_id=sid,
                updated_at=self.clock(), league_id=league_id,
                generation=generation))
            # PR #423 (design §8.1): the RESPONSE epoch, computed HERE, in the
            # SAME snapshot as the write — not a second, separate
            # `current_epoch()` call after commit. `server.py`'s own comment
            # at the call site explains why a second call was needed before
            # this fold existed: a second concurrent switch for the same user
            # could otherwise land between the write and that second read and
            # hand back an epoch for a selection this caller never asked for.
            # Folding it into one snapshot makes "same transaction as the
            # write" literally true for the epoch this response carries,
            # rather than approximately true via the caller's own gate.
            epoch = _epoch_hash(user_id, generation, program, season, league)
            return (_detached(program), _detached(season), _detached(league),
                    epoch)

        return self._snapshot(work)

    # -- mutation ----------------------------------------------------------
    def set(self, user_id: Optional[str], role, scope,
            program_id, season_id) -> _Resolved:
        """Record a user's selection, filtered through their authorized scope,
        and return the exact ``(program, season)`` objects validated+written
        (DETACHED from the store's live rows) so the caller renders a single
        authoritative snapshot that a concurrent edit cannot mutate. An
        unauthorized OR
        non-existent Program/Season both return the SAME generic not-found (no
        existence oracle). ``season_id`` may be None (Program-only). An archived
        Season is accepted as a read-only historical context; writes against it
        stay blocked by the Season read-only guard. Validation and the write run
        in one transaction, so a concurrent parent delete either is seen (and
        rejected) here or lands after the row is written — where it is harmless,
        since a saved row pointing at a since-deleted parent is ignored (never
        rendered) by ``resolve`` and grants no authority.

        Unchanged by #345, deliberately: this two-axis entry point CLEARS any
        previously-saved League (the written row's ``league_id`` defaults to
        None). Carrying one over would leave a League bound to a Program/Season
        it may not belong to — the one state the League axis must never reach.
        Callers that want to preserve or set a League use
        :meth:`set_with_league`.

        UNCHANGED two-tuple contract (PR #423): every existing caller (all in
        ``tests/`` — the facade's ``set_active_context`` always routes
        through :meth:`set_with_league`, even for a two-axis body, per
        ``api/service.py``'s own docstring) unpacks exactly
        ``(program, season)``. See :meth:`set_and_epoch` for the
        epoch-returning variant."""
        program, season, _epoch = self._set_locked(
            user_id, role, scope, program_id, season_id)
        return program, season

    def set_and_epoch(self, user_id: Optional[str], role, scope,
                      program_id, season_id):
        """Same as :meth:`set`, but ALSO returns the epoch derived from the
        SAME snapshot as the write (PR #423 design §8.1) —
        ``(program, season, epoch)``. Not currently called in production —
        the facade's ``set_active_context`` always routes through plain
        :meth:`set_with_league` (see that method's own corrected docstring:
        its epoch-returning twin, :meth:`set_with_league_and_epoch`, is ALSO
        not wired to the facade this round). Added for symmetry and so a
        future two-axis-only HTTP path gets the same same-snapshot epoch
        guarantee without re-deriving this logic."""
        return self._set_locked(user_id, role, scope, program_id, season_id)

    def _set_locked(self, user_id: Optional[str], role, scope,
                    program_id, season_id):
        """Shared implementation for :meth:`set` / :meth:`set_and_epoch` —
        always returns ``(program, season, epoch)``."""
        if not user_id:
            raise ValidationError(
                "A signed-in user is required to set a working context.")
        if not program_id:
            raise ValidationError("A program_id is required.",
                                  {"reason": "field_required"})

        def work():
            # PR #423: same placement/reasoning as set_with_league's own
            # fence acquisition above — truly first, per-user key.
            self.store.epoch_fence_acquire_exclusive(user_fence_key(user_id))
            programs = context_scope.authorized_program_ids(
                self.store, role, scope, user_id)
            program = (self.store.get_program(program_id)
                       if program_id in programs else None)
            if program is None:
                # Non-oracle: identical whether it doesn't exist or isn't ours.
                raise NotFoundError("Program not found or not accessible.",
                                    {"reason": "program_not_accessible"})
            season = None
            if season_id is not None:
                seasons = context_scope.authorized_season_ids(
                    self.store, role, scope, program_id, user_id)
                season = self.store.get_season(season_id)
                if season_id not in seasons or season is None:
                    raise NotFoundError("Season not found or not accessible.",
                                        {"reason": "season_not_accessible"})
            generation = self._next_generation_locked(user_id)
            self.store.set_active_context(ActiveContext(
                id=user_id, program_id=program_id, season_id=season_id,
                updated_at=self.clock(),
                generation=generation))
            # PR #423 (design §8.1): the two-axis form has no League, so the
            # epoch hash's league argument is simply None -- context_epoch
            # already treats a None league as "no League selected"
            # (context_epoch.py), the same value resolve()/set() without a
            # League always produced.
            epoch = _epoch_hash(user_id, generation, program, season, None)
            return _detached(program), _detached(season), epoch

        # Validation + write share ONE serializable snapshot (with bounded retry),
        # so a concurrent revocation is either seen here (rejected non-oracle) or
        # ordered entirely after this call — never a half-applied hybrid.
        return self._snapshot(work)
