"""Per-user active Program/Season context — the selection backend (#159).

Which Program + Season a user is currently working in. A VIEW selection only: it
never grants authority. On EVERY resolve and set it is filtered through the
caller's real role + account scope (`context_scope`), so a scoped account can
neither select nor enumerate a Program/Season outside its scope, and a saved
selection is dropped the instant the underlying record is deleted or the scope
changes. Program-only selection (no Season) is supported for new/empty Programs;
an archived Season is honored as a read-only HISTORICAL context (writes against
it stay blocked by the Season read-only guard), never silently replaced by an
unrelated active Season.
"""

from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

from ..domain import ActiveContext, SeasonStatus
from ..domain.errors import NotFoundError, ValidationError
from . import context_scope

_Resolved = Tuple[Optional[str], Optional[str], bool]


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

    # -- resolution --------------------------------------------------------
    def resolve(self, user_id: Optional[str], role, scope) -> _Resolved:
        """The caller's effective ``(program_id, season_id, read_only)``:

        * the saved selection when its Program is still authorized+present and
          its Season (if any) is still authorized+present — an ARCHIVED saved
          Season is honored as a read-only historical context;
        * else a deterministic authorized fallback (an active Season when the
          scope has one, otherwise a Program-only context);
        * else an empty context. ``read_only`` is True iff the Season is archived.
        """
        programs = context_scope.authorized_program_ids(
            self.store, role, scope, user_id)
        saved = self.store.get_active_context(user_id) if user_id else None
        if (saved and saved.program_id in programs
                and self.store.get_program(saved.program_id) is not None):
            if saved.season_id is None:
                return saved.program_id, None, False          # program-only
            season = self.store.get_season(saved.season_id)
            seasons = context_scope.authorized_season_ids(
                self.store, role, scope, saved.program_id, user_id)
            if season is not None and saved.season_id in seasons:
                return (saved.program_id, saved.season_id,
                        season.status == SeasonStatus.ARCHIVED)
            # Season deleted or no longer authorized → do not dangle and do not
            # invent an unrelated active Season under the same Program; take the
            # deterministic fallback below.
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
                return program.id, max(active, key=_season_sort_key).id, False
        # No authorized active Season anywhere → a Program-only context on the
        # first authorized Program (supports new/empty Programs, #159 gate).
        if candidates:
            return candidates[0].id, None, False
        return None, None, False

    # -- mutation ----------------------------------------------------------
    def set(self, user_id: Optional[str], role, scope,
            program_id, season_id) -> _Resolved:
        """Record a user's selection, filtered through their authorized scope.
        An unauthorized OR non-existent Program/Season both return the SAME
        generic not-found (no existence oracle). ``season_id`` may be None
        (Program-only). An archived Season is accepted as a read-only historical
        context; writes against it stay blocked by the Season read-only guard."""
        if not user_id:
            raise ValidationError(
                "A signed-in user is required to set a working context.")
        if not program_id:
            raise ValidationError("A program_id is required.",
                                  {"reason": "field_required"})
        programs = context_scope.authorized_program_ids(
            self.store, role, scope, user_id)
        if (program_id not in programs
                or self.store.get_program(program_id) is None):
            # Non-oracle: identical result whether it doesn't exist or isn't ours.
            raise NotFoundError("Program not found or not accessible.",
                                {"reason": "program_not_accessible"})
        read_only = False
        if season_id is not None:
            seasons = context_scope.authorized_season_ids(
                self.store, role, scope, program_id, user_id)
            season = self.store.get_season(season_id)
            if season_id not in seasons or season is None:
                raise NotFoundError("Season not found or not accessible.",
                                    {"reason": "season_not_accessible"})
            read_only = (season.status == SeasonStatus.ARCHIVED)
        self.store.set_active_context(ActiveContext(
            id=user_id, program_id=program_id, season_id=season_id,
            updated_at=self.clock()))
        return program_id, season_id, read_only
