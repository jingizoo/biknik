"""Per-user active Program/Season context — the selection backend (#159).

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
for free: their process-wide lock fully serializes every transaction.
"""

import copy
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

from ..domain import ActiveContext, SeasonStatus
from ..domain.errors import (
    ConcurrencyConflictError, NotFoundError, ValidationError)
from . import context_scope

# The context authorization + selection runs under one SERIALIZABLE snapshot so a
# request can never observe a hybrid of pre- and post-revocation scope (#159). A
# serialization conflict (e.g. two concurrent writes for one user, or a scope
# change the snapshot anti-depends on) is retried a bounded number of times; each
# attempt re-reads a fresh consistent snapshot. Memory/SQLite serialize via their
# process lock, so the retry never fires there.
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
        rendered) by ``resolve`` and grants no authority."""
        if not user_id:
            raise ValidationError(
                "A signed-in user is required to set a working context.")
        if not program_id:
            raise ValidationError("A program_id is required.",
                                  {"reason": "field_required"})

        def work():
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
            self.store.set_active_context(ActiveContext(
                id=user_id, program_id=program_id, season_id=season_id,
                updated_at=self.clock()))
            return _detached(program), _detached(season)

        # Validation + write share ONE serializable snapshot (with bounded retry),
        # so a concurrent revocation is either seen here (rejected non-oracle) or
        # ordered entirely after this call — never a half-applied hybrid.
        return self._snapshot(work)
