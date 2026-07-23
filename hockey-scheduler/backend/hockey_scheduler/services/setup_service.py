"""League + Arena setup service.

Builds the scheduling universe before games exist: league → season →
division, club → team, venue → rink → ice slot, and manual game creation.
Pure logic over the store with an injected clock; every create is audited.
"""

import functools
import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import Callable, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain import (
    Club,
    ContactDestination,
    Division,
    Game,
    GameType,
    IceSlot,
    IceSlotStatus,
    IceSlotType,
    League,
    LeagueSeason,
    Program,
    GameResult,
    NotificationAudience,
    NotificationChannel,
    NotificationKind,
    Official,
    OfficialAssignment,
    OfficialAssignmentStatus,
    OfficialAvailability,
    OfficialAvailabilityStatus,
    OfficialRole,
    Player,
    ResultStatus,
    Position,
    RescheduleRequest,
    RescheduleStatus,
    Organization,
    Rink,
    Season,
    SeasonStatus,
    SeasonTeamRegistration,
    SeasonVenueAccess,
    SetupAuditLog,
    Team,
    Venue,
    intervals_overlap,
    jersey_number_error,
)
from ..domain.jersey import MAX_JERSEY_NUMBER, MIN_JERSEY_NUMBER
from ..domain.shooting import VALID_SHOOTS, normalize_shoots
from ..domain.errors import (
    ConcurrencyConflictError,
    DivisionMismatchError,
    HasDependenciesError,
    IntegrityConflictError,
    InvalidTransitionError,
    NotEligibleError,
    NotFoundError,
    ScheduleConflictError,
    ValidationError,
)
from ..store import InMemoryStore
from .import_validator import validate_import, validate_official_availability
from .ice_availability import plan_ice_windows, parse_hhmm, WEEKDAY_NAMES
from .season_guard import require_active_season
from .league_scope import (
    registered_team_ids_in_division as _registered_team_ids,
    team_registration_valid,
)
from .notifier import push as _push_notification


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Sentinel distinguishing "argument omitted, leave as-is" from an explicit
# value (including None, which clears a nullable field). Used by update_player
# (#268) for partial edits and by the swap-safe import staging (#292) to tell a
# per-row upsert whether a pre-release original jersey was supplied — a plain
# default of None can't express "don't touch" for a nullable column.
_UNSET = object()


def _blank(value) -> bool:
    return value is None or not str(value).strip()


def _clean(value) -> str:
    return str(value).strip()


def _no_club(value) -> bool:
    """Blank, null, and the literal spreadsheet placeholder "NA" (any case)
    all mean "no Club" on import (#233 Slice D) — never a Club literally
    named "NA"."""
    return _blank(value) or _clean(value).upper() == "NA"


def _parse_iso_utc(value) -> Optional[datetime]:
    """Parse a timezone-aware ISO-8601 timestamp, else None.

    Duplicated locally rather than imported from ``import_validator`` (same
    precedent as ``_blank``/``_clean`` above) — by the time this is called
    from ``commit_officials_availability_import`` the value has already
    passed ``validate_official_availability``, so ``None`` here would
    indicate a bug elsewhere, not a real user-facing validation failure.
    """
    if _blank(value):
        return None
    try:
        parsed = datetime.fromisoformat(_clean(value))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def resolve_timezone(name):
    """Return a ``ZoneInfo`` for an IANA name, or ``None`` if it is unknown or
    not a non-empty string. Total (never raises), so callers on the hot path —
    e.g. the season-boundary parser applied to stored/legacy data — can fall
    back to UTC without a try/except of their own.
    """
    if not isinstance(name, str) or not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def parse_season_boundary(value, field_name: str, tz) -> Optional[datetime]:
    """Normalize a Season start/end boundary to a timezone-aware UTC datetime
    (#272). See ``docs/architecture/season-dates.md`` for the contract.

    Accepts, in the Program's timezone ``tz`` (a ``tzinfo``):

    * ``None`` / ``""`` → ``None`` (an unset boundary).
    * a **date-only** ``YYYY-MM-DD`` string (or a bare ``date``) → interpreted as
      LOCAL MIDNIGHT (00:00) on that calendar day in ``tz``, stored as the
      equivalent UTC instant. This lets a league office enter ``2026-09-15``
      without manufacturing a UTC time, and the value round-trips to the same
      calendar day when displayed in the Program timezone.
    * a **timezone-aware** ISO-8601 string or ``datetime`` → that exact instant,
      converted to UTC (existing behavior; no drift for stored values).

    A naive value that carries a TIME component (e.g. ``2026-09-15T18:30:00``)
    stays rejected as ambiguous — only a bare calendar date is tz-anchored.
    Invalid input raises a field-level ``ValidationError``
    (``invalid_<field_name>``).
    """
    if value is None or value == "":
        return None
    field = {"reason": f"invalid_{field_name}", "field": field_name}
    # datetime FIRST — datetime is a subclass of date, so the date branch below
    # must not swallow it.
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValidationError(
                f"{field_name} must be timezone-aware, or a YYYY-MM-DD date.",
                field)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day,
                        tzinfo=tz).astimezone(timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if _DATE_ONLY.match(s):
            try:
                d = date.fromisoformat(s)
            except ValueError:
                raise ValidationError(
                    f"Invalid {field_name}: {value!r} is not a real date.", field)
            return datetime(d.year, d.month, d.day,
                            tzinfo=tz).astimezone(timezone.utc)
        try:
            dt = datetime.fromisoformat(s)
        except (ValueError, TypeError):
            raise ValidationError(
                f"Invalid {field_name}: {value!r}. Expected YYYY-MM-DD or a "
                "timezone-aware ISO-8601 timestamp.", field)
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise ValidationError(
                f"Invalid {field_name}: {value!r}. A timestamp with a time must "
                "be timezone-aware; use YYYY-MM-DD for a calendar date.", field)
        return dt.astimezone(timezone.utc)
    raise ValidationError(
        f"{field_name} must be a YYYY-MM-DD date or an ISO-8601 timestamp.",
        field)


def _transactional(fn):
    """Wrap a mutating service method in a single store transaction."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.store.transaction():
            return fn(self, *args, **kwargs)
    return wrapper


class _SeasonReparented(Exception):
    """Internal retry signal (#158 review): a Season was moved to a different
    Program between commit_ice_availability's pre-lock program_id read and its
    Season lock, so the wrong Program was locked. Caught by the commit's bounded
    retry loop, which re-reads and locks the correct Program first."""


class SetupService:
    def __init__(self, store: InMemoryStore, clock: Callable[[], datetime] = _utcnow):
        self.store = store
        self.clock = clock
        # Re-resolve a no-row-lock facility delete that lost the FK race into the
        # itemised has-dependencies error (#201 Slice 3). Registered on the store
        # so it fires from the OUTERMOST transaction()'s post-rollback handler —
        # on a clean connection — even when the delete was nested inside a
        # caller's transaction(). Stores without the hook (in-memory) never raise
        # the conflict (their pre-check catches dependents), so the getattr guard
        # simply skips them.
        register = getattr(store, "set_dependent_conflict_resolver", None)
        if register is not None:
            register(self._resolve_dependent_delete_conflict)

    # -- helpers -----------------------------------------------------------
    def _require_name(self, name: str, field_name: str = "name") -> str:
        if name is None or not str(name).strip():
            raise ValidationError(f"{field_name} is required.")
        return str(name).strip()

    def _require_utc(self, dt, field_name: str) -> datetime:
        if not isinstance(dt, datetime):
            raise ValidationError(f"{field_name} must be a datetime.")
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise ValidationError(
                f"{field_name} must be a timezone-aware UTC datetime."
            )
        return dt.astimezone(timezone.utc)

    def _audit(self, action: str, entity_type: str, entity_id: str,
               actor_id: Optional[str] = None, detail: Optional[dict] = None
               ) -> SetupAuditLog:
        return self.store.add_setup_audit(SetupAuditLog(
            id=self.store.next_id("setupaudit"),
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            at=self.clock(),
            actor_id=actor_id,
            detail=detail or {},
        ))

    def _matchup(self, game) -> str:
        def team_name(tid):
            t = self.store.get_team(tid) if tid else None
            return t.name if t else "TBD"
        return f"{team_name(game.home_team_id)} vs {team_name(game.away_team_id)}"

    def _notify(self, kind, audience, title, message, **links):
        return _push_notification(self.store, self.clock, kind, audience,
                                  title, message, **links)

    def _game_label(self, game) -> str:
        def name(tid):
            t = self.store.get_team(tid) if tid else None
            return t.name if t else "TBD"
        return f"{name(game.home_team_id)} vs {name(game.away_team_id)}"

    def _notify_game_change(self, game, kind, title, message,
                            include_public=False):
        """Fan a schedule-change notification out to the affected parties (#87):
        both teams (coach audience → team recipient), each actively-assigned
        official, and optionally the public feed. Delivery honors each
        recipient's channel preferences (#81)."""
        for tid in (game.home_team_id, game.away_team_id):
            if tid:
                self._notify(kind, NotificationAudience.COACH, title, message,
                             audience_ref=tid, game_id=game.id)
        for a in self.store.assignments_for_game(game.id):
            if a.status.is_active:
                self._notify(kind, NotificationAudience.OFFICIAL, title, message,
                             audience_ref=a.official_id, game_id=game.id)
        if include_public:
            self._notify(kind, NotificationAudience.PUBLIC, title, message,
                         game_id=game.id)

    # -- program / season / league / division -----------------------------
    @_transactional
    def create_program(self, name: str, country: str = "", timezone_name: str = "UTC",
                       operator_organization_id: Optional[str] = None,
                       actor_id: Optional[str] = None) -> Program:
        # Optional operating organization (#173) — validated when given so a
        # program never dangles off a non-existent operator. Nullable for
        # migration/legacy; null-operator programs surface in the queue.
        if operator_organization_id and \
                self.store.get_organization(operator_organization_id) is None:
            raise NotFoundError(
                f"Organization {operator_organization_id} not found.")
        # The Program timezone is the anchor for date-only Season boundaries
        # (#272), so it must be a real IANA zone — validate the TYPE and value
        # explicitly, before any write (#272 review). `timezone_name or "UTC"`
        # would silently map a falsy non-string (False/0/[]/{}) to UTC, and a
        # truthy non-string would reach ZoneInfo(...) and raise an uncaught
        # TypeError/500. None/blank is the intentional UTC default.
        if timezone_name is None:
            tz_name = "UTC"
        elif isinstance(timezone_name, str):
            tz_name = timezone_name.strip() or "UTC"
        else:
            raise ValidationError(
                "timezone must be a string IANA name (or null for UTC).",
                {"reason": "invalid_timezone", "field": "timezone"})
        if resolve_timezone(tz_name) is None:
            raise ValidationError(
                f"Invalid timezone: {timezone_name!r}. Expected an IANA name "
                "like 'America/Chicago' or 'UTC'.",
                {"reason": "invalid_timezone", "field": "timezone"})
        program = Program(id=self.store.next_id("league"),
                          name=self._require_name(name), country=country,
                          timezone=tz_name,
                          operator_organization_id=operator_organization_id or None)
        self.store.add_program(program)
        self._audit("league_created", "league", program.id, actor_id,
                    {"organization_id": operator_organization_id}
                    if operator_organization_id else None)
        return program

    @_transactional
    def create_season(self, program_id: str, name: str,
                      start_date=None, end_date=None,
                      actor_id: Optional[str] = None) -> Season:
        program = self.store.get_program(program_id)
        if program is None:
            raise NotFoundError(f"Program {program_id} not found.")
        # Date-only boundaries (e.g. '2026-09-15') are anchored to local midnight
        # in the PROGRAM's timezone (#272); timezone-aware values pass through to
        # UTC unchanged. A legacy/unknown zone falls back to UTC so creation
        # never 500s (new programs validate the zone at create_program).
        tz = resolve_timezone(program.timezone) or timezone.utc
        start = parse_season_boundary(start_date, "start_date", tz)
        end = parse_season_boundary(end_date, "end_date", tz)
        if start and end and end < start:
            raise ValidationError(
                "end_date cannot be before start_date.",
                {"reason": "end_before_start", "field": "end_date"})
        season = Season(id=self.store.next_id("season"), program_id=program_id,
                        name=self._require_name(name), start_date=start, end_date=end)
        self.store.add_season(season)
        self._audit("season_created", "season", season.id, actor_id,
                    {"league_id": program_id})
        return season

    def _require_active_season(self, season_id: str) -> Season:
        """Fail closed if a Season is archived/read-only (#159). Thin wrapper over
        the shared :func:`season_guard.require_active_season` (row-locked, must run
        inside the caller's ``transaction()``); see that module for the full
        linearizability contract. Every Season-owned write in SetupService routes
        through here."""
        return require_active_season(self.store, season_id)

    def _guard_game_season(self, game) -> None:
        """Guard a Game-owned mutation against its archived Season (#159).
        Row-locks + checks the Game's Season so no write lands on a Game whose
        Season is archived. Takes the already-fetched Game (preserving each
        caller's own not-found semantics); a Season-less legacy Game is a no-op."""
        if game is not None and game.season_id:
            require_active_season(self.store, game.season_id)

    @staticmethod
    def _normalize_lifecycle_reason(reason, *, required: bool) -> Optional[str]:
        """Validate + normalize a lifecycle ``reason`` BEFORE any mutation (#159).

        The reason may only be JSON ``null`` or a string; every other JSON type
        (boolean, number, array, object) is a stable ``invalid_reason`` /
        ``field="reason"`` error — never a silent coercion or a 500 from calling
        ``.strip()`` on a non-string. ``bool`` is rejected explicitly because it
        is an ``int`` subclass. A string is trimmed; a blank string collapses to
        ``None``. When ``required`` (reopen), a ``None``/blank result is the
        stable ``reason_required`` error."""
        if reason is not None and (isinstance(reason, bool)
                                   or not isinstance(reason, str)):
            raise ValidationError(
                "The reason must be a string.",
                {"reason": "invalid_reason", "field": "reason"})
        normalized = (reason or "").strip() or None
        if required and normalized is None:
            raise ValidationError(
                "A reason is required to reopen an archived Season.",
                {"reason": "reason_required", "field": "reason"})
        return normalized

    @_transactional
    def archive_season(self, season_id: str, *, actor_id: Optional[str] = None,
                       reason: Optional[str] = None) -> Season:
        """Archive a Season into read-only historical mode (#159). Idempotent
        transitions are rejected (re-archiving an archived Season is an explicit
        error) so the audit trail records exactly one transition. Authorization
        (MANAGE_SETUP) is enforced at the HTTP boundary. The Season row is locked
        (#159) so concurrent archive/reopen serialize — exactly one wins, the
        loser gets the stable lifecycle error with no duplicate audit."""
        # Validate/normalize the reason before touching any row so a bad-typed
        # reason is a stable invalid_reason error with zero mutation.
        normalized_reason = self._normalize_lifecycle_reason(
            reason, required=False)
        season = self.store.get_season_for_update(season_id)
        if season is None:
            raise NotFoundError(f"Season {season_id} not found.")
        if season.status == SeasonStatus.ARCHIVED:
            raise ValidationError(
                f"Season '{season.name}' is already archived.",
                {"reason": "season_already_archived", "season_id": season_id})
        season.status = SeasonStatus.ARCHIVED
        season.archived_at = self.clock()
        self.store.save_season(season)
        self._audit("season_archived", "season", season_id, actor_id,
                    {"reason": normalized_reason})
        return season

    @_transactional
    def reopen_season(self, season_id: str, *, actor_id: Optional[str] = None,
                      reason: Optional[str] = None) -> Season:
        """Reopen an archived Season back to active/writable (#159). This is a
        privileged, *reasoned* operation — a non-empty ``reason`` is required and
        recorded in the audit trail (authorization is enforced at the HTTP
        boundary). Reopening a Season that is not archived is an explicit error."""
        # A wrong-typed reason is a stable invalid_reason error, and a
        # missing/blank one is reason_required — both before any mutation.
        normalized_reason = self._normalize_lifecycle_reason(
            reason, required=True)
        # Lock the row (#159) so concurrent archive/reopen serialize.
        season = self.store.get_season_for_update(season_id)
        if season is None:
            raise NotFoundError(f"Season {season_id} not found.")
        if season.status != SeasonStatus.ARCHIVED:
            raise ValidationError(
                f"Season '{season.name}' is not archived.",
                {"reason": "season_not_archived", "season_id": season_id})
        season.status = SeasonStatus.ACTIVE
        season.archived_at = None
        self.store.save_season(season)
        self._audit("season_reopened", "season", season_id, actor_id,
                    {"reason": normalized_reason})
        return season

    def _lock_league_for_binding(self, league_id: str) -> League:
        """Row-lock an existing permanent League before binding it to a Season
        (#159 concurrency). This establishes the FIRST half of the canonical
        **League → Season** lock order that ``delete_league`` and
        ``transfer_team_to_league`` already follow: a binding path locks the
        League row HERE, before it takes the Season read-only guard, so a
        concurrent ``delete_league`` (which locks the same row) serializes —
        either this binding commits first and the delete then sees it and blocks
        (bindings are itemized dependents), or the delete commits first and this
        lock finds the League gone. Never an orphaned ``LeagueSeason`` (migration
        035 defines no FK to catch one at the DB layer). Raises ``NotFoundError``
        when the League does not exist. Callers MUST invoke this before any
        Season lock; acquiring the League lock AFTER the Season lock would invert
        the canonical order and can deadlock against ``delete``/``transfer`` on
        PostgreSQL (Memory/SQLite serialize whole transactions, so order is moot
        there, but the guarantee must hold on every backend)."""
        league = self.store.get_league_for_update(league_id)
        if league is None:
            raise NotFoundError(f"League {league_id} not found.")
        return league

    def _link_league_season(self, league_id: str, season_id: str) -> LeagueSeason:
        """Find or create the LeagueSeason binding ``league_id`` to ``season_id``
        (#283). Enforces the invariant ``league.program_id == season.program_id``
        (rule 5) before creating a new binding. Plain helper (no audit, no own
        transaction) so it composes inside a caller's transaction.

        #159 concurrency: when a NEW binding must be created, the League row is
        RE-VALIDATED under a row lock (``get_league_for_update``) so the binding
        can never be inserted for a League a concurrent ``delete_league`` is
        removing (no orphan; migration 035 has no FK). Every binding caller must
        already have taken this same League lock BEFORE its Season guard (see
        :meth:`_lock_league_for_binding`) so the lock here is re-entrant and the
        canonical League→Season order holds; the lock is repeated defensively so
        a caller that forgets still cannot orphan a binding."""
        existing = self.store.league_season_for(league_id, season_id)
        if existing is not None:
            return existing
        league = self.store.get_league_for_update(league_id)
        if league is None:
            raise NotFoundError(f"League {league_id} not found.")
        season = self.store.get_season(season_id)
        if season is None:
            raise NotFoundError(f"Season {season_id} not found.")
        if league.program_id != season.program_id:
            raise ValidationError(
                "That League and Season belong to different Programs.",
                {"reason": "league_season_program_mismatch",
                 "league_id": league_id, "season_id": season_id,
                 "league_program_id": league.program_id,
                 "season_program_id": season.program_id})
        ls = LeagueSeason(id=self.store.next_id("leagueseason"),
                          league_id=league_id, season_id=season_id)
        self.store.add_league_season(ls)
        return ls

    @_transactional
    def create_league(self, season_id: str, name: str, sort_order: int = 0,
                      actor_id: Optional[str] = None) -> League:
        """Create a competition League for a Season (#283 back-compat entry).

        A League is now a PERMANENT child of the Season's Program; this
        season-oriented entry point creates that permanent League and binds it to
        the Season via a :class:`LeagueSeason` in one step, so existing callers
        keep working. (Slice C adds the program-first create + explicit
        LeagueSeason API.)"""
        season = self._require_active_season(season_id)  # #159 read-only guard
        league = League(id=self.store.next_id("league"),
                        program_id=season.program_id,
                        name=self._require_name(name), sort_order=sort_order or 0)
        self.store.add_league(league)
        self._link_league_season(league.id, season_id)
        self._audit("level_created", "level", league.id, actor_id,
                    {"season_id": season_id, "program_id": season.program_id})
        return league

    @_transactional
    def create_league_season(self, league_id: str, season_id: str,
                             actor_id: Optional[str] = None) -> LeagueSeason:
        """Bind an existing permanent League to a Season (#283 rule 5). Returns
        the existing binding when already present; enforces the shared-Program
        invariant via :meth:`_link_league_season`."""
        # #159 — canonical League→Season lock order: lock the League row BEFORE
        # the Season guard so this serializes with delete_league (never an
        # orphaned binding) and never deadlocks against delete/transfer.
        self._lock_league_for_binding(league_id)
        self._require_active_season(season_id)  # #159 read-only guard
        existing = self.store.league_season_for(league_id, season_id)
        ls = self._link_league_season(league_id, season_id)
        if existing is None:
            self._audit("league_season_created", "league_season", ls.id,
                        actor_id, {"league_id": league_id, "season_id": season_id})
        return ls

    @_transactional
    def delete_league_season(self, league_season_id: str,
                             actor_id: Optional[str] = None) -> dict:
        """Explicitly unbind a permanent League from a Season (#159).

        This is the authorized, audited counterpart to ``create_league_season``:
        it removes a single ``LeagueSeason`` binding so an operator can, in turn,
        delete a permanent League (which blocks on its bindings — deletions are
        dependency-gated with no silent cascades). It is itself dependency-gated:
        a binding that still owns Divisions, registrations, or Games is refused
        (resolve those first), and it fails closed with ``season_archived`` on an
        archived Season so read-only history is never rewritten. All checks run
        before the single delete, so a refused unbind changes nothing."""
        ls = self.store.get_league_season(league_season_id)
        if ls is None:
            raise NotFoundError(
                f"LeagueSeason {league_season_id} not found.")
        # #159 — read-only guard: an archived Season's participation history is
        # frozen (locks the Season row, serializing against a concurrent
        # archive AND a concurrent unbind/create on the same binding).
        if ls.season_id:
            self._require_active_season(ls.season_id)
        # #159 — RE-FETCH the binding UNDER the Season lock before scanning
        # dependents or deleting. The first read above is unlocked, so a
        # concurrent delete_league_season of the SAME binding (which locks the
        # same Season row) could have already removed it; without this re-check
        # both callers would "succeed" and write duplicate league_season_deleted
        # audits. A binding that is already gone fails closed with zero
        # delete/audit — exactly one unbind wins.
        ls = self.store.get_league_season(league_season_id)
        if ls is None:
            raise NotFoundError(
                f"LeagueSeason {league_season_id} not found.")
        divisions = [d for d in self.store.all_divisions()
                     if d.league_season_id == league_season_id]
        regs = [r for r in self.store.all_season_team_registrations()
                if r.league_season_id == league_season_id]
        games = [g for g in self.store.all_games()
                 if g.league_season_id == league_season_id]
        self._block_if_dependents(
            "league_season", league_season_id, "season binding", [
                self._dep_group("division", divisions, lambda d: d.name),
                self._dep_group("team registration", regs,
                                lambda r: self._team_name(r.team_id)),
                self._dep_group("game", games, self._matchup)])
        self.store.delete_league_season(league_season_id)
        self._audit("league_season_deleted", "league_season", league_season_id,
                    actor_id, {"league_id": ls.league_id,
                               "season_id": ls.season_id})
        return {"id": league_season_id, "league_id": ls.league_id,
                "season_id": ls.season_id, "deleted": True}

    @_transactional
    def create_division(self, season_id: str, name: str, age_group: str = "",
                        league_id: Optional[str] = None,
                        actor_id: Optional[str] = None) -> Division:
        """Create a Division for a Season (#283 rule 6, back-compat entry).

        A Division is owned by a :class:`LeagueSeason`. This season-oriented
        entry point resolves the LeagueSeason from ``(league_id, season_id)`` —
        or the Season's sole LeagueSeason when no league is given — so existing
        callers keep working while the Division is stored against its
        LeagueSeason."""
        # #159 — canonical League→Season order: an explicit League is row-locked
        # BEFORE the Season guard (a league-less division auto-provisions a NEW
        # League inside the resolver, created in-txn and unreachable by a
        # concurrent delete, so it needs no pre-lock).
        if league_id:
            self._lock_league_for_binding(league_id)
        self._require_active_season(season_id)  # #159 read-only guard
        ls = self._resolve_division_league_season(season_id, league_id)
        division = Division(id=self.store.next_id("division"),
                            league_season_id=ls.id,
                            name=self._require_name(name), age_group=age_group)
        self.store.add_division(division)
        self._audit("division_created", "division", division.id, actor_id,
                    {"season_id": season_id,
                     **({"level_id": league_id} if league_id else {})})
        return division

    def _resolve_division_league_season(self, season_id: str,
                                        league_id: Optional[str]) -> LeagueSeason:
        """The LeagueSeason a Division should belong to (#283). With a league,
        find/create its binding to the Season; without one, the Season's sole
        LeagueSeason. A Division now always belongs to a League (rule 6), so a
        league-less division added to a Season that has NO league yet
        auto-provisions a single default League for that Season — this preserves
        the pre-#283 ergonomics (a Season implicitly had one grouping) so a
        caller that only cares about divisions/teams/games keeps working."""
        if league_id:
            if self.store.get_league(league_id) is None:
                raise NotFoundError(f"League {league_id} not found.")
            return self._link_league_season(league_id, season_id)
        candidates = self.store.league_seasons_for_season(season_id)
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            season = self.store.get_season(season_id)
            league = League(id=self.store.next_id("league"),
                            program_id=season.program_id, name="League",
                            sort_order=0)
            self.store.add_league(league)
            return self._link_league_season(league.id, season_id)
        raise ValidationError(
            "This season has several leagues; specify which league the division "
            "belongs to.",
            {"reason": "ambiguous_league_for_season", "season_id": season_id})

    def _import_default_league_season(self, season_id: str) -> LeagueSeason:
        """The LeagueSeason the simple (two-sheet) team import registers into
        (#283): the Season's existing LeagueSeason (first if several — this
        onboarding import carries no per-division League), auto-provisioning a
        default League when the Season has none, so imported divisions and
        registrations are never orphaned with a null league_season_id."""
        candidates = self.store.league_seasons_for_season(season_id)
        if candidates:
            return candidates[0]
        season = self.store.get_season(season_id)
        league = League(id=self.store.next_id("league"),
                        program_id=season.program_id if season else None,
                        name="League", sort_order=0)
        self.store.add_league(league)
        return self._link_league_season(league.id, season_id)

    @_transactional
    def create_division_under_league(self, league_id: str, name: str,
                                     age_group: str = "",
                                     actor_id: Optional[str] = None) -> Division:
        """Create a Division under a permanent League (#283 back-compat, v2).

        The League participates in one Season here (the common case); the
        LeagueSeason is resolved from the league's sole binding. Delegates to the
        Division create against that LeagueSeason."""
        # #159 — canonical League→Season lock order: lock the League row first,
        # then resolve its sole binding, then lock that binding's Season.
        self._lock_league_for_binding(league_id)
        lss = self.store.league_seasons_for_league(league_id)
        if not lss:
            raise ValidationError(
                "That league is not yet part of any season.",
                {"reason": "league_has_no_season", "league_id": league_id})
        if len(lss) > 1:
            raise ValidationError(
                "That league participates in several seasons; create the "
                "division against a specific season.",
                {"reason": "ambiguous_season_for_league", "league_id": league_id})
        self._require_active_season(lss[0].season_id)  # #159 read-only guard
        # #159 — RE-FETCH the binding UNDER the Season lock before inserting: the
        # sole-binding read above is unlocked, so a concurrent
        # delete_league_season (which locks the same Season row) could have
        # unbound it. Inserting a Division against a deleted LeagueSeason would
        # orphan it (migration 035 has no FK). A binding unbound out from under us
        # fails closed with zero write/audit.
        binding = self.store.get_league_season(lss[0].id)
        if binding is None:
            raise ValidationError(
                "That league is not yet part of any season.",
                {"reason": "league_has_no_season", "league_id": league_id})
        division = Division(id=self.store.next_id("division"),
                            league_season_id=binding.id,
                            name=self._require_name(name), age_group=age_group)
        self.store.add_division(division)
        self._audit("division_created", "division", division.id, actor_id,
                    {"league_season_id": binding.id, "level_id": league_id})
        return division

    @_transactional
    def assign_season_team_league(self, registration_id: str,
                                  league_id: Optional[str] = None,
                                  actor_id: Optional[str] = None
                                  ) -> SeasonTeamRegistration:
        """Move a registration to a different League within the same Season
        (#283 back-compat). The registration's League is fixed by its
        LeagueSeason, so this repoints it to the LeagueSeason of
        ``(league_id, same season)``; when the Team has a permanent League it may
        only be that League (rule 7). Refuses to strand a committed game."""
        reg = self.store.get_season_team_registration(registration_id)
        if reg is None:
            raise NotFoundError(f"Registration {registration_id} not found.")
        if not league_id:
            raise ValidationError("A league_id is required.")
        # #159 — canonical Team → League → Season lock order (shared with
        # transfer_team_to_league): row-lock the Team FIRST so its permanent
        # league_id can't change under a concurrent transfer between the rule-7
        # check below and the registration write, THEN the target League, THEN
        # the Season guard. Reading the Team unlocked would let a transfer commit
        # Team→L2 after this rule-7 check passed for L1, binding the registration
        # to a League that is no longer the Team's — a canonical-invariant
        # violation. The lock is held through the binding/registration/audit.
        team = self.store.get_team_for_update(reg.team_id)
        if team and team.league_id and team.league_id != league_id:
            raise ValidationError(
                "A team may only register in its own League.",
                {"reason": "team_league_mismatch", "team_id": reg.team_id,
                 "team_league_id": team.league_id, "league_id": league_id})
        self._lock_league_for_binding(league_id)
        season_id = self._season_of_league_season(reg.league_season_id)
        if season_id:
            self._require_active_season(season_id)  # #159 read-only guard
        # #159 r14 — the pre-lock read was only a LOCATOR. Re-fetch the
        # registration UNDER the Season row lock (which every unregister /
        # reactivate / permanent-delete also takes) and operate on the fresh
        # row, so a concurrent unregister that committed active=False can't be
        # silently resurrected by saving a stale active=True snapshot. The row's
        # Team is immutable and its Season never changes, so the Team/League/
        # Season locks taken above still apply to the fresh row.
        reg = self.store.get_season_team_registration(registration_id)
        if reg is None:
            raise NotFoundError(f"Registration {registration_id} not found.")
        old_league = self._registration_league_id(reg)
        if (league_id or None) == (old_league or None):
            # No-op League assignment: write and audit NOTHING, so it can never
            # reactivate a row a concurrent unregister just deactivated.
            return reg
        stranded = [
            g.id for g in self.store.all_games()
            if not g.cancelled and not g.is_draft
            and g.season_id == season_id and g.league_id == old_league
            and reg.team_id in (g.home_team_id, g.away_team_id)]
        if stranded:
            raise ValidationError(
                "Cannot change this registration's league while committed "
                "games reference its current league for this team; resolve "
                "those games first.",
                {"reason": "registration_league_change_strands_games",
                 "registration_id": reg.id,
                 "affected_game_ids": stranded, "count": len(stranded)})
        new_ls = self._link_league_season(league_id, season_id)
        # A division set on the registration must belong to the new LeagueSeason;
        # clear it if it doesn't (the league moved out from under it).
        if reg.division_id:
            division = self.store.get_division(reg.division_id)
            if division and division.league_season_id != new_ls.id:
                reg.division_id = None
        reg.league_season_id = new_ls.id
        self.store.save_season_team_registration(reg)
        self._audit("season_team_league_assigned", "season_team_registration",
                    reg.id, actor_id, {"from": old_league, "to": league_id})
        return reg

    # -- club / team -------------------------------------------------------
    @_transactional
    def create_club(self, name: str, country: str = "",
                    actor_id: Optional[str] = None) -> Club:
        club = Club(id=self.store.next_id("club"), name=self._require_name(name),
                    country=country)
        self.store.add_club(club)
        self._audit("club_created", "club", club.id, actor_id)
        return club

    @_transactional
    def create_team(self, club_id: Optional[str] = None,
                    division_id: Optional[str] = None,
                    name: str = "", actor_id: Optional[str] = None,
                    program_id: Optional[str] = None,
                    league_id: Optional[str] = None) -> Team:
        """Create a permanent League team (#283 rules 2 & 3, back-compat entry).

        A Team belongs to a permanent League (``Team.league_id``). Which League:

        - Pass ``league_id`` directly, or
        - Pass a ``division_id`` — the League is derived from the division's
          LeagueSeason (a read; the division is not stored on the Team), or
        - Pass only ``program_id`` (legacy) — the Team is created with its
          Program but no League yet (assigned later via a registration or
          ``transfer_team_to_league``); strict rule-2 enforcement lands with the
          Slice C create API.

        The Team's Program is kept consistent with its League when both resolve
        (rule 3). ``club_id`` is optional (#233 Slice D) — validated only when a
        non-null id is supplied; never invent a placeholder Club.
        """
        if club_id and self.store.get_club(club_id) is None:
            raise NotFoundError(f"Club {club_id} not found.")
        derived_league_id = league_id or None
        if derived_league_id is not None:
            if self.store.get_league(derived_league_id) is None:
                raise NotFoundError(f"League {derived_league_id} not found.")
        if division_id:
            division = self.store.get_division(division_id)
            if division is None:
                raise NotFoundError(f"Division {division_id} not found.")
            ls = self.store.get_league_season(division.league_season_id)
            div_league_id = ls.league_id if ls else None
            if derived_league_id and div_league_id \
                    and derived_league_id != div_league_id:
                raise ValidationError(
                    "The chosen division belongs to a different league.",
                    {"reason": "team_league_mismatch",
                     "league_id": derived_league_id,
                     "division_league_id": div_league_id})
            derived_league_id = derived_league_id or div_league_id
        # Derive/validate the Program from the resolved League (rule 3).
        if derived_league_id:
            league = self.store.get_league(derived_league_id)
            derived_program = league.program_id if league else None
            if program_id and derived_program and program_id != derived_program:
                raise ValidationError(
                    "The chosen league belongs to a different program.",
                    {"reason": "team_program_mismatch",
                     "league_id": derived_league_id, "program_id": program_id,
                     "league_program_id": derived_program})
            program_id = derived_program or program_id
        # #283 Slice E (mandatory rule 2): every Team belongs to a permanent
        # League. When only a Program is given, deterministically resolve its
        # SOLE League — unambiguous, so never a guess. A Program with zero or
        # several Leagues can't be resolved and is rejected rather than guessed.
        if not derived_league_id and program_id:
            prog_leagues = self.store.leagues_for_program(program_id)
            if len(prog_leagues) == 1:
                derived_league_id = prog_leagues[0].id
        # A new Team is never created league-less; teams_without_league is only a
        # legacy/migration remediation state, never produced by a fresh create.
        if not derived_league_id:
            raise ValidationError(
                "A team must belong to a permanent league (choose a league, or "
                "a division to derive it from).",
                {"reason": "team_league_required"})
        if program_id and self.store.get_program(program_id) is None:
            raise NotFoundError(f"Program {program_id} not found.")
        # #159 — lock the permanent League row before binding a Team to it, so a
        # concurrent delete_league of the same League (which locks the same row)
        # serializes: either this create commits first and the delete then sees
        # the Team and blocks, or the delete commits first and this re-check
        # finds the League gone — never an orphaned Team.
        if self.store.get_league_for_update(derived_league_id) is None:
            raise NotFoundError(f"League {derived_league_id} not found.")
        team = Team(id=self.store.next_id("team"), name=self._require_name(name),
                    club_id=club_id or None, program_id=program_id,
                    league_id=derived_league_id)
        self.store.add_team(team)
        self._audit("team_created", "team", team.id, actor_id,
                    {"club_id": team.club_id, "league_id": program_id,
                     "permanent_league_id": derived_league_id})
        return team

    # -- permanent teams + LeagueSeason registrations (#283) ----------------
    # A Team belongs permanently to its League; each Season it plays in is a
    # SeasonTeamRegistration against a LeagueSeason of that same League, carrying
    # the season-specific optional Division.
    @_transactional
    def register_team_for_season(self, season_id: str, team_id: str,
                                 division_id: Optional[str] = None,
                                 actor_id: Optional[str] = None,
                                 league_id: Optional[str] = None
                                 ) -> SeasonTeamRegistration:
        """Register a Team into a Season (#283 rules 2, 6, 7, back-compat entry).

        The registration is stored against a :class:`LeagueSeason`, resolved from
        the explicit ``league_id`` / the Team's permanent League / the Division's
        League / the Season's sole League (in that order). A Team with a permanent
        League may register only in that League (rule 7); an optional Division
        must belong to the resolved LeagueSeason (rule 6). One registration per
        (team, LeagueSeason); a prior inactive one is reactivated in place.
        """
        # #159 — canonical Team → League → Season lock order (shared with
        # transfer_team_to_league, which row-locks the Team first): row-lock the
        # Team BEFORE deriving/locking its candidate League, so its permanent
        # league_id can't be moved by a concurrent transfer between the read and
        # this registration write. Reading the Team unlocked here would let a
        # transfer commit Team→L2 after we resolved Team→L1, leaving a
        # registration in LeagueSeason(L1) while team.league_id == L2 — a
        # canonical-invariant violation with no DB constraint to catch it. The
        # lock is held (to commit on PostgreSQL; process-wide on Memory/SQLite)
        # through the League lock, the binding, and the registration/audit writes.
        team = self.store.get_team_for_update(team_id)
        if team is None:
            raise NotFoundError(f"Team {team_id} not found.")
        # Resolve the candidate permanent League (reads only, off the now-locked
        # Team) and row-lock it BEFORE the Season guard, so a new LeagueSeason
        # binding can't be orphaned by a concurrent delete_league and the lock
        # order never inverts against delete/transfer. The Season's-sole-League
        # fallback (no candidate League) creates no new binding, so it needs no
        # pre-lock.
        candidate_league = self._candidate_registration_league_id(
            team, division_id, league_id)
        if candidate_league:
            self._lock_league_for_binding(candidate_league)
        season = self._require_active_season(season_id)  # #159 read-only guard
        # Rule 4 — program consistency (legacy-permissive: only a non-null
        # mismatch is rejected, so a legacy program-less team still registers).
        if team.program_id and team.program_id != season.program_id:
            raise ValidationError(
                "Team belongs to a different program than this season.")
        # When an explicit league is supplied (the v2 canonical path), the
        # Team→Program match is EXACT (#283/#233 C2): a program-less team can't
        # slip into the canonical tree the way the legacy-permissive check above
        # would allow it to.
        if league_id and (not team.program_id
                          or team.program_id != season.program_id):
            raise ValidationError(
                "Team must belong to this season's program.",
                {"reason": "team_program_mismatch", "team_id": team.id,
                 "team_program_id": team.program_id,
                 "season_program_id": season.program_id})
        ls = self._resolve_registration_league_season(
            season, team, division_id, league_id)
        # Rule 7 — a Team with a permanent League registers only in that League.
        if team.league_id and ls.league_id != team.league_id:
            raise ValidationError(
                "A team may only register in its own League.",
                {"reason": "team_league_mismatch", "team_id": team_id,
                 "team_league_id": team.league_id,
                 "league_season_league_id": ls.league_id})
        # #283 Slice E (rule 2): a Team must always have a permanent League. A
        # legacy league-less Team gains one deterministically here — the League
        # it is registering into — so no registration write ever leaves a Team
        # league-less. (New Teams already have one; create_team requires it.)
        if not team.league_id:
            team.league_id = ls.league_id
            self.store.save_team(team)
            self._audit("team_league_resolved", "team", team.id, actor_id,
                        {"permanent_league_id": ls.league_id,
                         "via": "registration"})
        if division_id:
            division = self.store.get_division(division_id)
            if division is None:
                raise NotFoundError(f"Division {division_id} not found.")
            # Rule 6 — the Division must belong to the resolved LeagueSeason.
            if division.league_season_id != ls.id:
                raise ValidationError(
                    "Division belongs to a different league/season.",
                    {"reason": "division_league_season_mismatch",
                     "division_id": division_id,
                     "division_league_season_id": division.league_season_id,
                     "league_season_id": ls.id})
        # One registration per (team, LeagueSeason). A prior *inactive*
        # registration (a team removed then re-added) is reactivated in place.
        existing = self.store.registration_for_team_in_league_season(
            ls.id, team_id)
        if existing is not None:
            if existing.active:
                raise ValidationError(
                    f"Team {team_id} is already registered for this season.")
            existing.active = True
            existing.division_id = division_id or None
            self.store.save_season_team_registration(existing)
            self._audit("season_team_registered", "season_team_registration",
                        existing.id, actor_id,
                        {"season_id": season_id, "team_id": team_id,
                         "division_id": existing.division_id, "reactivated": True})
            return existing
        reg = SeasonTeamRegistration(
            id=self.store.next_id("streg"), league_season_id=ls.id,
            team_id=team_id, division_id=division_id or None, active=True)
        self.store.add_season_team_registration(reg)
        self._audit("season_team_registered", "season_team_registration",
                    reg.id, actor_id,
                    {"season_id": season_id, "team_id": team_id,
                     "division_id": reg.division_id})
        return reg

    def _candidate_registration_league_id(self, team, division_id,
                                          league_id) -> Optional[str]:
        """The permanent League a registration would bind to, resolved from
        reads only (#159): explicit ``league_id`` → the Team's permanent League →
        the Division's League. Returns ``None`` when none resolves (the Season's-
        sole-League fallback, which binds no new League). Used to row-lock that
        League BEFORE the Season guard, ahead of :meth:`_resolve_registration_
        league_season` which re-resolves the same League and creates the binding.
        Tolerant of a missing Division (returns ``None``) so the resolver keeps
        its own not-found/precedence semantics after the guard."""
        candidate = league_id or team.league_id
        if not candidate and division_id:
            division = self.store.get_division(division_id)
            ls_of_div = (self.store.get_league_season(division.league_season_id)
                         if division is not None else None)
            candidate = ls_of_div.league_id if ls_of_div else None
        return candidate

    def _resolve_registration_league_season(self, season, team, division_id,
                                            league_id) -> LeagueSeason:
        """The LeagueSeason a registration belongs to (#283 back-compat). Prefer
        the explicit ``league_id``, else the Team's permanent League, else the
        Division's League, else the Season's sole LeagueSeason.

        #159: the resolved League is expected to already be row-locked by the
        caller (:meth:`_candidate_registration_league_id` +
        :meth:`_lock_league_for_binding`) before the Season guard, so the binding
        created here can't be orphaned by a concurrent delete_league."""
        candidate_league = league_id or team.league_id
        if not candidate_league and division_id:
            division = self.store.get_division(division_id)
            if division is None:
                raise NotFoundError(f"Division {division_id} not found.")
            ls_of_div = self.store.get_league_season(division.league_season_id)
            candidate_league = ls_of_div.league_id if ls_of_div else None
        if candidate_league:
            if self.store.get_league(candidate_league) is None:
                raise NotFoundError(f"League {candidate_league} not found.")
            return self._link_league_season(candidate_league, season.id)
        candidates = self.store.league_seasons_for_season(season.id)
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise ValidationError(
                "Create a league for this season before registering teams.",
                {"reason": "no_league_for_season", "season_id": season.id})
        raise ValidationError(
            "This season has several leagues; specify which league to register "
            "the team in.",
            {"reason": "ambiguous_league_for_season", "season_id": season.id})

    def _season_of_league_season(self, league_season_id):
        """The Season id a LeagueSeason belongs to, or None."""
        ls = self.store.get_league_season(league_season_id)
        return ls.season_id if ls else None

    def _games_scheduled_for_team_in_season(self, season_id, team_id):
        """Ids of committed (non-cancelled, non-draft) games in ``season_id``
        that ``team_id`` plays in — the games a removal or division change would
        strand. Draft proposals aren't real games yet, so they don't block."""
        if not season_id:
            return []
        return [g.id for g in self.store.all_games()
                if g.season_id == season_id and not g.cancelled
                and not g.is_draft
                and team_id in (g.home_team_id, g.away_team_id)]

    def _registration_program(self, reg):
        """The Program a registration resolves to (via LeagueSeason -> Season),
        or None."""
        season_id = self._season_of_league_season(reg.league_season_id)
        season = self.store.get_season(season_id) if season_id else None
        return season.program_id if season else None

    def _registration_league_id(self, reg):
        """The League a registration belongs to (via its LeagueSeason), or None."""
        ls = self.store.get_league_season(reg.league_season_id)
        return ls.league_id if ls else None

    def _revalidate_game_participation(self, game):
        """Both teams must still be valid participants of ``game``'s competition
        scope (#283 Slice E) — checked before any write (publish/move), so a
        rejection mutates nothing.

        A REGULAR game requires both teams to have an ACTIVE registration in the
        game's exact LeagueSeason (its single competition identity); when the
        game also carries a Division, the stricter season+division match is kept
        too. A legacy regular game with no ``league_season_id`` falls back to the
        season(+division) check. An EXHIBITION only requires both teams to remain
        active participants of the game's Season (it may cross Leagues)."""
        if game.game_type == GameType.EXHIBITION.value:
            if not game.season_id:
                return
            season = self.store.get_season(game.season_id)
            for tid in (game.home_team_id, game.away_team_id):
                if tid is None:
                    continue
                if team_registration_valid(self.store, season, tid,
                                           require_division=False) is None:
                    label = (self.store.get_team(tid) or Team(id=tid, name=tid)).name
                    raise ValidationError(
                        f"{label} is no longer an active participant in this "
                        "game's season.",
                        {"reason": "team_not_season_participant",
                         "team_id": tid, "season_id": game.season_id})
            return
        # Regular game. #283 Slice E: a regular game MUST reference a
        # LeagueSeason — fail closed (never a lenient legacy fallback) so an
        # unscoped regular game can't be published or moved until it is repaired.
        ls_id = getattr(game, "league_season_id", None)
        if ls_id is None:
            raise ValidationError(
                "This regular game has no league-season; it cannot be published "
                "or moved until it is repaired.",
                {"reason": "regular_game_missing_league_season",
                 "game_id": game.id})
        # The season+division check runs first: it raises the precise
        # DivisionMismatchError and is the stricter guard for a divisioned game.
        if game.season_id and game.division_id:
            self._require_team_registered(
                game.season_id, game.home_team_id, game.division_id)
            self._require_team_registered(
                game.season_id, game.away_team_id, game.division_id)
        # Both teams must be active in the game's exact LeagueSeason (covers the
        # division-less regular game the check above skips).
        active_ids = {r.team_id for r
                      in self.store.registrations_for_league_season(ls_id)
                      if r.active}
        for tid in (game.home_team_id, game.away_team_id):
            if tid is not None and tid not in active_ids:
                label = (self.store.get_team(tid) or Team(id=tid, name=tid)).name
                raise ValidationError(
                    f"{label} is no longer registered in this game's "
                    "league-season.",
                    {"reason": "team_not_in_league_season",
                     "team_id": tid, "league_season_id": ls_id})

    @_transactional
    def assign_season_team_division(self, registration_id: str,
                                    division_id: Optional[str] = None,
                                    actor_id: Optional[str] = None,
                                    v2: bool = False
                                    ) -> SeasonTeamRegistration:
        """Set (or clear) a registration's Division within its LeagueSeason
        (#283 rule 6). A registration's League is fixed by its LeagueSeason and
        never changes here — only the optional Division moves."""
        reg = self.store.get_season_team_registration(registration_id)
        if reg is None:
            raise NotFoundError(f"Registration {registration_id} not found.")
        # #159 read-only guard — reassigning a registration's Division is a
        # Season-owned write, so it is blocked on an archived Season regardless
        # of whether the Division value actually changes.
        season_id = self._season_of_league_season(reg.league_season_id)
        if season_id:
            self._require_active_season(season_id)
        # #159 r14 — re-fetch the registration UNDER the Season row lock (the
        # pre-lock read was only a locator) and operate on the fresh row, so a
        # concurrent unregister/permanent-delete isn't silently clobbered by a
        # stale snapshot. A row purged out from under us is now a clean
        # not-found rather than a resurrecting save.
        reg = self.store.get_season_team_registration(registration_id)
        if reg is None:
            raise NotFoundError(f"Registration {registration_id} not found.")
        old = reg.division_id
        if (division_id or None) == (old or None):
            # No-op Division assignment: write and audit NOTHING, so it can
            # never reactivate a row a concurrent unregister just deactivated.
            return reg
        if division_id:
            division = self.store.get_division(division_id)
            if division is None:
                raise NotFoundError(f"Division {division_id} not found.")
            # Rule 6 — a registration's Division must belong to its LeagueSeason.
            if division.league_season_id != reg.league_season_id:
                raise ValidationError(
                    "Division belongs to a different LeagueSeason.",
                    {"reason": "division_league_season_mismatch",
                     "registration_id": reg.id,
                     "registration_league_season_id": reg.league_season_id,
                     "division_league_season_id": division.league_season_id})
        # Safety — a division change would leave already-scheduled games in the
        # old division mismatched against the team's participation. Refuse and
        # report the affected games so the operator can resolve them first,
        # rather than silently invalidating a published schedule.
        if (division_id or None) != (old or None):
            stranded = self._games_scheduled_for_team_in_season(
                season_id, reg.team_id)
            if stranded:
                raise ValidationError(
                    "Cannot change this team's division while it has scheduled "
                    "games this season; resolve those games first.",
                    {"reason": "team_has_scheduled_games",
                     "affected_game_ids": stranded, "count": len(stranded)})
        reg.division_id = division_id or None
        self.store.save_season_team_registration(reg)
        self._audit("season_team_division_assigned", "season_team_registration",
                    reg.id, actor_id, {"from": old, "to": reg.division_id})
        return reg

    @_transactional
    def transfer_team_to_league(self, team_id: str, new_league_id: str,
                                actor_id: Optional[str] = None) -> Team:
        """Move a Team to a different permanent League — promotion/relegation or
        transfer (#283 rule 10).

        History is preserved exactly: INACTIVE (past) registrations, their
        Games, results, and standings are never touched. The target League must
        share the Team's Program (rule 3).

        A Team's ACTIVE registrations must stay consistent with its permanent
        League (rule 7), so the transfer atomically moves each active
        registration that currently sits in a DIFFERENT League to the target
        League's LeagueSeason for that same Season (clearing its Division, which
        belonged to the old LeagueSeason). If any such active registration has
        committed (non-draft, non-cancelled) games, moving it would strand
        them, so the WHOLE transfer is rejected before any write — the operator
        must resolve those games first. All checks run before any mutation, so a
        rejected transfer changes nothing (zero Team/registration/audit
        mutation).
        """
        team = self.store.get_team_for_update(team_id)
        if team is None:
            raise NotFoundError(f"Team {team_id} not found.")
        return self._transfer_team_to_league_inner(team, new_league_id, actor_id)

    def _transfer_team_to_league_inner(self, team, new_league_id: str,
                                       actor_id: Optional[str] = None) -> Team:
        """Body of :meth:`transfer_team_to_league`, without its own transaction
        or Team fetch — so the import path (#283 Slice E) can route a permanent-
        League change through the same lifecycle guards inside its own commit
        transaction. Operates on an already-resolved ``team``."""
        team_id = team.id
        # #159 — lock the target League row so a rebind of a Team's permanent
        # League serializes against a concurrent delete_league of that League
        # (which locks the same row): the Team can't be rebound onto a League
        # that is being deleted, nor deleted out from under this rebind.
        league = self.store.get_league_for_update(new_league_id)
        if league is None:
            raise NotFoundError(f"League {new_league_id} not found.")
        if team.program_id and league.program_id != team.program_id:
            raise ValidationError(
                "The target league belongs to a different program.",
                {"reason": "team_program_mismatch", "team_id": team_id,
                 "team_program_id": team.program_id,
                 "league_program_id": league.program_id})
        old = team.league_id
        if old == new_league_id:
            return team  # no-op: already in this League.

        # Pre-scan: only a CURRENT/FUTURE active registration in a different
        # League is a conflict to resolve. An ENDED Season's active registration
        # is history — it (and its Games/results/standings) is never touched, so
        # a transfer leaves the record of what actually happened intact. A
        # current/future conflict is moved when game-free, or blocks the whole
        # transfer (zero mutation) when it has committed games.
        now = self.clock()
        # The candidate registrations to (maybe) move: this Team's own active
        # rows that currently sit in a DIFFERENT League than the target.
        candidates = [
            reg for reg in self.store.all_season_team_registrations()
            if reg.team_id == team_id and reg.active
            and self._registration_league_id(reg) != new_league_id]
        # #159 — lock every distinct Season these registrations touch, in
        # canonical sorted order, BEFORE classifying or mutating. The locked read
        # is the linearization point: a Season that reads ARCHIVED under its lock
        # is frozen history and never moved, and a concurrent archive cannot slip
        # in between the status check and the registration rewrite (it blocks on
        # the row until this transfer commits, or committed first and is observed
        # here). Sorted order avoids lock-order deadlocks across the batch.
        locked_seasons = {}
        for sid in sorted({
                self._season_of_league_season(r.league_season_id)
                for r in candidates
                if self._season_of_league_season(r.league_season_id)}):
            locked_seasons[sid] = self.store.get_season_for_update(sid)
        # #159 r15 — the candidate scan above was an UNLOCKED locator snapshot.
        # Re-fetch every candidate under its now-locked Season and re-validate
        # its CURRENT state: a row a concurrent unregister deactivated (or a
        # delete removed, or a rebind already moved to the target League) between
        # the snapshot and the lock must stay frozen — drop it here so it is
        # never resurrected by saving a stale active=True object, and so it is
        # excluded from registrations_moved. A registration's Season never
        # changes, so every surviving candidate's Season is already locked.
        fresh_candidates = []
        for reg in candidates:
            current = self.store.get_season_team_registration(reg.id)
            if current is None or not current.active:
                continue
            if self._registration_league_id(current) == new_league_id:
                continue
            fresh_candidates.append(current)
        to_move = []          # (reg, season_id) pairs eligible to move
        blocked = []          # {registration_id, season_id, affected_game_ids}
        for reg in fresh_candidates:
            season_id = self._season_of_league_season(reg.league_season_id)
            season = locked_seasons.get(season_id) if season_id else None
            # A Season is historical only once it has DEFINITELY ended (a real
            # end_date in the past). A missing/undated Season is treated as
            # current/future (the safe default) until an operator resolves it.
            if season is not None and (
                    season.status == SeasonStatus.ARCHIVED
                    or (season.end_date is not None and season.end_date < now)):
                # #159 — an ended OR archived Season is frozen history: never
                # move its registration (archived may be undated/future).
                continue
            stranded = self._games_scheduled_for_team_in_season(
                season_id, team_id)
            if stranded:
                blocked.append({"registration_id": reg.id,
                                "season_id": season_id,
                                "affected_game_ids": stranded})
            else:
                to_move.append((reg, season_id))
        if blocked:
            raise ValidationError(
                "Cannot transfer this team while it has active registrations "
                "with scheduled games; resolve those games first.",
                {"reason": "team_transfer_strands_games", "team_id": team_id,
                 "blocked": blocked})

        # Apply — all writes happen only after every check passed.
        for reg, season_id in to_move:
            target_ls = self._link_league_season(new_league_id, season_id)
            reg.league_season_id = target_ls.id
            reg.division_id = None  # the old Division belonged to the old League
            self.store.save_season_team_registration(reg)
        team.league_id = new_league_id
        # Keep Program consistent with the new League when the Team had none.
        team.program_id = team.program_id or league.program_id
        self.store.save_team(team)
        self._audit("team_league_transferred", "team", team.id, actor_id,
                    {"from": old, "to": new_league_id,
                     "registrations_moved": [r.id for r, _ in to_move]})
        return team

    @_transactional
    def unregister_team_from_season(self, registration_id: str,
                                    actor_id: Optional[str] = None
                                    ) -> SeasonTeamRegistration:
        # Rule 6 — removing a team from a season deactivates only this season's
        # registration; the permanent Team and prior-season registrations are
        # untouched.
        reg = self.store.get_season_team_registration(registration_id)
        if reg is None:
            raise NotFoundError(f"Registration {registration_id} not found.")
        # Safety — refuse to strand a team that still has committed games this
        # season, returning the affected game ids so they can be resolved first.
        season_id = self._season_of_league_season(reg.league_season_id)  # #283
        if season_id:
            self._require_active_season(season_id)  # #159 read-only guard
        # #159 r14 — re-fetch UNDER the Season row lock (the pre-lock read was
        # only a locator). A concurrent permanent-delete may have removed the
        # row (→ clean not-found, never resurrect it by saving a stale
        # snapshot); a concurrent unregister may have already deactivated it
        # (→ idempotent, no duplicate deactivation audit).
        reg = self.store.get_season_team_registration(registration_id)
        if reg is None:
            raise NotFoundError(f"Registration {registration_id} not found.")
        if not reg.active:
            return reg  # already unregistered — idempotent, no second write/audit
        stranded = self._games_scheduled_for_team_in_season(
            season_id, reg.team_id)
        if stranded:
            raise ValidationError(
                "Cannot remove this team from the season while it has scheduled "
                "games; resolve those games first.",
                {"reason": "team_has_scheduled_games",
                 "affected_game_ids": stranded, "count": len(stranded)})
        reg.active = False
        self.store.save_season_team_registration(reg)
        self._audit("season_team_unregistered", "season_team_registration",
                    reg.id, actor_id,
                    {"season_id": season_id, "team_id": reg.team_id})
        return reg

    @_transactional
    def delete_season_team_registration(self, registration_id: str,
                                        actor_id: Optional[str] = None
                                        ) -> dict:
        """Permanently remove an INACTIVE, game-free registration (#251).

        unregister_team_from_season() deliberately only deactivates a row —
        it retains the registration's Season/Team/League/Division identity
        for history, which is correct, but leaves no way to resolve the
        hidden blocker it creates for delete_league/delete_season/
        delete_team (each of which still — correctly — blocks on ANY
        registration row, active or not, since League/Season/Team are
        required identifiers on a registration and can't simply be cleared
        the way delete_division clears an optional division_id). This is
        the explicit, safe cleanup operation for that hidden row: never an
        active registration, never one any Game still references (draft,
        scheduled, cancelled, or historical all count as history).
        """
        reg = self.store.get_season_team_registration(registration_id)
        if reg is None:
            raise NotFoundError(f"Registration {registration_id} not found.")
        # #159 read-only guard — an archived Season fails closed before any
        # other precheck, so purging a registration under it is uniformly
        # season_archived (not registration_active) with zero mutation.
        season_id = self._season_of_league_season(reg.league_season_id)  # #283
        if season_id:
            self._require_active_season(season_id)
        # #159 r14 — re-fetch UNDER the Season row lock (the pre-lock read was
        # only a locator). A concurrent register/reactivate may have flipped
        # this row back to active AFTER the locator read; deleting it on the
        # stale inactive snapshot would destroy a live registration. A row
        # already purged by a concurrent delete is a clean not-found.
        reg = self.store.get_season_team_registration(registration_id)
        if reg is None:
            raise NotFoundError(f"Registration {registration_id} not found.")
        if reg.active:
            raise ValidationError(
                "Cannot permanently delete an active registration; remove "
                "it from the season first.",
                {"reason": "registration_active", "registration_id": reg.id})
        # Resolve every parent this row claims to belong to BEFORE any write —
        # an inactive row whose Season/Team/League has since vanished, or
        # whose Division no longer resolves, is not safe to purge blindly,
        # and the caller needs real labels (not bare ids) to confirm what it
        # just removed. Division alone is genuinely optional on the model.
        season = self.store.get_season(season_id) if season_id else None
        if season is None:
            raise ValidationError(
                "This registration's Season no longer exists.",
                {"reason": "invalid_season", "season_id": season_id})
        team = self.store.get_team(reg.team_id)
        if team is None:
            raise ValidationError(
                "This registration's Team no longer exists.",
                {"reason": "invalid_team", "team_id": reg.team_id})
        league_id = self._registration_league_id(reg)  # #283
        league = self.store.get_league(league_id) if league_id else None
        if league is None:
            raise ValidationError(
                "This registration's League no longer resolves.",
                {"reason": "invalid_league", "league_id": league_id})
        division = None
        if reg.division_id:
            division = self.store.get_division(reg.division_id)
            if division is None:
                raise ValidationError(
                    "This registration's Division no longer resolves.",
                    {"reason": "invalid_division",
                     "division_id": reg.division_id})
        games = [g for g in self.store.all_games()
                if g.season_id == season_id
                and reg.team_id in (g.home_team_id, g.away_team_id)]
        self._block_if_dependents(
            "season_team_registration", registration_id, "registration", [
                self._dep_group("game", games, self._matchup)])
        detail = {"season_id": season_id, "team_id": reg.team_id,
                  "league_id": league_id, "division_id": reg.division_id,
                  "reason": "explicit_inactive_cleanup"}
        self.store.delete_season_team_registration(registration_id)
        self._audit("season_team_registration_deleted",
                    "season_team_registration", registration_id, actor_id,
                    detail)
        return {"id": reg.id, **detail,
                "season_name": season.name, "team_name": team.name,
                "league_name": league.name,
                "division_name": division.name if division else None}

    @_transactional
    def grant_season_venue_access(self, season_id: str, venue_id: str,
                                  actor_id: Optional[str] = None
                                  ) -> SeasonVenueAccess:
        """Give a Season access to a Venue's ice (#233 Slice E).

        Physical structure (Venue) and competition structure (Season) are
        independent — unlike the legacy Venue.league_id bridge, this never
        constrains a Venue to one Program, and a Season may hold access to any
        number of Venues regardless of operator. Rule mirrors
        register_team_for_season's Rule 5: a prior *inactive* access row (once
        revoked, now regranted) is reactivated in place rather than
        duplicated, honoring the (season_id, venue_id) one-active-row
        invariant enforced by the partial unique index (migration 029).
        """
        season = self._require_active_season(season_id)  # #159 read-only guard
        venue = self.store.get_venue(venue_id)
        if venue is None:
            raise NotFoundError(f"Venue {venue_id} not found.")
        existing = self.store.season_venue_access_for_pair(season_id, venue_id)
        if existing is not None:
            if existing.active:
                raise ValidationError(
                    f"Season {season_id} already has access to venue "
                    f"{venue_id}.",
                    {"reason": "already_active", "access_id": existing.id})
            existing.active = True
            self.store.save_season_venue_access(existing)
            self._audit("season_venue_access_granted", "season_venue_access",
                        existing.id, actor_id,
                        {"season_id": season_id, "venue_id": venue_id,
                         "reactivated": True})
            return existing
        access = SeasonVenueAccess(
            id=self.store.next_id("sva"), season_id=season_id,
            venue_id=venue_id, active=True)
        self.store.add_season_venue_access(access)
        self._audit("season_venue_access_granted", "season_venue_access",
                    access.id, actor_id,
                    {"season_id": season_id, "venue_id": venue_id})
        return access

    @_transactional
    def revoke_season_venue_access(self, access_id: str,
                                   actor_id: Optional[str] = None
                                   ) -> SeasonVenueAccess:
        """Deactivate a Season's access to a Venue (#233 Slice E).

        Deactivates only — history is preserved (mirrors
        unregister_team_from_season) so any Game already scheduled against
        this Venue for this Season remains intact and auditable. A revoked
        row still blocks a Season/Venue delete (delete_season/delete_venue
        check every access row regardless of active status, mirroring
        delete_league/delete_season/delete_team's identical registration
        check) — delete_season_venue_access below is the explicit,
        separate cleanup action for an already-revoked row.
        """
        access = self.store.get_season_venue_access(access_id)
        if access is None:
            raise NotFoundError(f"Season-venue access {access_id} not found.")
        if not access.active:
            raise ValidationError(
                "This access is already revoked.",
                {"reason": "already_revoked", "access_id": access.id})
        self._require_active_season(access.season_id)  # #159 read-only guard
        # #159 r15 — re-fetch under the Season lock (the pre-lock read was a
        # locator). A concurrent revoke or delete may have committed first; act
        # on the fresh row so exactly one revoke writes one audit and a deleted
        # row is a clean not-found, never a resurrecting save.
        access = self.store.get_season_venue_access(access_id)
        if access is None:
            raise NotFoundError(f"Season-venue access {access_id} not found.")
        if not access.active:
            raise ValidationError(
                "This access is already revoked.",
                {"reason": "already_revoked", "access_id": access.id})
        access.active = False
        self.store.save_season_venue_access(access)
        self._audit("season_venue_access_revoked", "season_venue_access",
                    access.id, actor_id,
                    {"season_id": access.season_id, "venue_id": access.venue_id})
        return access

    @_transactional
    def delete_season_venue_access(self, access_id: str,
                                   actor_id: Optional[str] = None) -> dict:
        """Permanently remove an already-revoked, game-free Season-Venue
        access row (#233 Slice E, #255 review, #257 review).

        revoke_season_venue_access only deactivates a row, preserving it as a
        blocker for delete_season/delete_venue (both check every row
        regardless of active status) so the grant/revoke history stays
        auditable by default. This is the explicit, separate cleanup action
        that actually removes an inactive row once an operator has confirmed
        it no longer needs to block a parent delete — mirrors
        delete_season_team_registration (#251) exactly, including its
        game-history guard: a revoked row can still be the ONLY explicit
        record of why a draft/committed/cancelled/published Game's ice was
        allowed at this Venue for this Season, so it is never purged out from
        under live Game history. Never an active row.
        """
        access = self.store.get_season_venue_access(access_id)
        if access is None:
            raise NotFoundError(f"Season-venue access {access_id} not found.")
        if access.active:
            raise ValidationError(
                "Cannot permanently delete an active access; revoke it "
                "first.",
                {"reason": "access_active", "access_id": access.id})
        # Resolve both parents before any write — an inactive row whose
        # Season or Venue has since vanished is not safe to purge blindly,
        # and the caller needs real labels (not bare ids) to confirm what it
        # just removed (mirrors delete_season_team_registration exactly).
        self._require_active_season(access.season_id)  # #159 read-only guard
        # #159 r15 — re-fetch under the Season lock and re-check active: a
        # concurrent grant may have reactivated this row after the pre-lock read,
        # and hard-deleting a now-active access would destroy live state (mirrors
        # delete_season_team_registration).
        access = self.store.get_season_venue_access(access_id)
        if access is None:
            raise NotFoundError(f"Season-venue access {access_id} not found.")
        if access.active:
            raise ValidationError(
                "Cannot permanently delete an active access; revoke it "
                "first.",
                {"reason": "access_active", "access_id": access.id})
        season = self.store.get_season(access.season_id)
        if season is None:
            raise ValidationError(
                "This access's Season no longer exists.",
                {"reason": "invalid_season", "season_id": access.season_id})
        venue = self.store.get_venue(access.venue_id)
        if venue is None:
            raise ValidationError(
                "This access's Venue no longer exists.",
                {"reason": "invalid_venue", "venue_id": access.venue_id})
        rink_ids = {r.id for r in self.store.all_rinks()
                    if r.venue_id == access.venue_id}
        games = [
            g for g in self.store.all_games()
            if g.season_id == access.season_id and g.ice_slot_id
            and (slot := self.store.get_ice_slot(g.ice_slot_id)) is not None
            and slot.rink_id in rink_ids]
        self._block_if_dependents(
            "season_venue_access", access_id, "venue access", [
                self._dep_group("game", games, self._matchup)])
        detail = {"season_id": access.season_id, "venue_id": access.venue_id,
                  "reason": "explicit_revoked_cleanup"}
        self.store.delete_season_venue_access(access_id)
        self._audit("season_venue_access_deleted", "season_venue_access",
                    access_id, actor_id, detail)
        return {"id": access_id, **detail,
                "season_name": season.name, "venue_name": venue.name}

    @_transactional
    def roll_forward_registrations(self, from_season_id: str, to_season_id: str,
                                   selections: Optional[list] = None,
                                   actor_id: Optional[str] = None) -> dict:
        """Copy participation from one season into another (#180 rollover).

        Creates a registration in ``to_season_id`` for each carried-forward team
        that plays in ``from_season_id``, reusing the permanent Team — Team
        records are never copied. ``selections`` is an optional list of
        ``{"team_id", "division_id"}`` (division_id in the *target* season); when
        omitted, every team active in the source season is carried with no
        division so the operator assigns them afterward. A team already active in
        the target season is skipped, not duplicated. Not ``@_transactional``-
        nested — it opens its own single transaction (via the decorator) and
        inlines the per-team writes rather than calling the transactional
        register method, since the store's transaction isn't reentrant.

        Every carried team is resolved and league-checked in a pre-write gate
        (#197): a source season's registration rows may be legacy, orphaned, or
        cross-league, and the store transaction is a lock rather than a
        rollback, so a missing or foreign team aborts the entire batch before
        any registration or audit is written.
        """
        # Season ids must be strings before they reach the store: an unhashable
        # JSON value (list/dict) from a malformed body would otherwise raise a
        # TypeError inside ``store.get_season`` (dict.get / SQL bind) — a 500,
        # not the structured validation_error (#197) this route must return.
        if not isinstance(from_season_id, str) or _blank(from_season_id):
            raise ValidationError("from_season_id must be a non-empty string.")
        if not isinstance(to_season_id, str) or _blank(to_season_id):
            raise ValidationError("to_season_id must be a non-empty string.")
        src = self.store.get_season(from_season_id)
        if src is None:
            raise NotFoundError(f"Season {from_season_id} not found.")
        # #159 — freeze the source-active Team set ONCE. The store's default
        # PostgreSQL isolation is READ COMMITTED, so re-reading the source
        # registrations after the lock pre-pass could observe a Team that
        # registered into the source Season in between — a "late entrant" that
        # would then be rolled forward WITHOUT its Team/League locks (and could
        # race a transfer into a mismatched League). Read the set once here and
        # use this exact frozen set for BOTH the lock pre-pass and the wanted/
        # validation below, so every carried Team is one this batch locked.
        source_active = {r.team_id
                         for r in self.store.registrations_for_season(from_season_id)
                         if r.active}
        # #159 — canonical Team → League → Season lock order (shared with
        # transfer_team_to_league): a v1 rollover derives each carried Team's
        # target League from Team.league_id and writes a registration in it.
        # Row-lock every carried Team FIRST (sorted, deduped) so its permanent
        # league_id can't move under a concurrent transfer between the derive and
        # the write, THEN lock the Leagues derived from those now-locked Teams
        # (so no binding is orphaned by a concurrent delete_league and the order
        # never inverts), THEN the target Season below. A None/bad team league is
        # skipped here and still fails the pre-write gate.
        _carry = ([s.get("team_id") for s in selections
                   if isinstance(s, dict) and isinstance(s.get("team_id"), str)]
                  if isinstance(selections, list)
                  else list(source_active) if selections is None else [])
        _roll_lids = set()
        for _tid in sorted({t for t in _carry if isinstance(t, str)}):
            _t = self.store.get_team_for_update(_tid)
            if _t is not None and _t.league_id:
                _roll_lids.add(_t.league_id)
        for _lid in sorted(_roll_lids):
            self._lock_league_for_binding(_lid)
        # #159 — lock the target row so a rollover serializes with archive.
        dst = self.store.get_season_for_update(to_season_id)
        if dst is None:
            raise NotFoundError(f"Season {to_season_id} not found.")
        # #159 — a rollover may READ an archived source season's history, but
        # never write registrations INTO an archived (read-only) target.
        if dst.status == SeasonStatus.ARCHIVED:
            raise ValidationError(
                f"Season '{dst.name}' is archived and read-only. Reopen it "
                "before rolling participation into it.",
                {"reason": "season_archived", "season_id": to_season_id})
        if from_season_id == to_season_id:
            raise ValidationError("Source and target seasons must differ.")
        # Rule 4 — a rollover stays within one program.
        if (src.program_id or None) != (dst.program_id or None):
            raise ValidationError(
                "Cannot roll participation between seasons of different programs.")
        # `source_active` was frozen once above (READ COMMITTED safety) and is
        # reused here for selection validation / the copy-all wanted set, so it
        # can't diverge from the Team set the lock pre-pass locked.
        if selections is not None:
            # Malformed HTTP input must surface as a structured validation
            # error, not an AttributeError 500 (#197): ``selections`` is a list
            # of ``{team_id, division_id}`` objects, each with a non-empty
            # team_id. ``catch`` only translates DomainError, so an attribute
            # access on a non-dict selection would otherwise escape as a 500.
            if not isinstance(selections, list):
                raise ValidationError(
                    "selections must be a list of {team_id, division_id} objects.")
            wanted = {}
            for sel in selections:
                if not isinstance(sel, dict):
                    raise ValidationError(
                        "Each selection must be an object with a team_id.")
                tid = sel.get("team_id")
                # Must be a non-empty *string*: a non-string id (incl. an
                # unhashable list/dict) would slip past ``_blank`` — which
                # stringifies its argument — and then raise a TypeError on the
                # ``in source_active`` set-membership test below.
                if not isinstance(tid, str) or _blank(tid):
                    raise ValidationError(
                        "Each selection needs a non-empty team_id.")
                if tid not in source_active:
                    raise ValidationError(
                        f"Team {tid} is not registered in the source season.")
                div = sel.get("division_id")
                # Likewise reject a non-string division_id before it reaches the
                # ``set(wanted.values())`` de-dup, which would TypeError on an
                # unhashable value.
                if div is not None and not isinstance(div, str):
                    raise ValidationError(
                        "A selection's division_id must be a string or null.")
                wanted[tid] = div or None
        else:
            wanted = {tid: None for tid in source_active}
        # Pre-write gate. The store's transaction is a lock, not a rollback
        # (see ``memory_store.transaction``), so every check that can fail must
        # run *before* the first write; any failure below aborts the whole
        # batch with zero registrations and zero audits.
        #
        # (a) Every carried team must be a real permanent Team whose permanent
        # league matches this rollover (#197). A source season's registration
        # rows can be legacy, orphaned, or cross-league, so ``source_active``
        # alone is not trustworthy — resolve each Team and verify its permanent
        # ``league_id`` before writing. This is what stops a rollover from
        # materializing a target registration for a missing or foreign team.
        # Deliberately stricter than ``register_team_for_season`` (which
        # tolerates a null team league): manual registration is an operator
        # vouching for one team, whereas rollover blindly trusts a batch of
        # source rows, so a null/other league_id here is treated as bad data.
        program_id = src.program_id or None
        for tid in wanted:
            team = self.store.get_team(tid)
            if team is None:
                raise ValidationError(
                    f"Team {tid} in the source season no longer exists; "
                    "it cannot be rolled forward.")
            if (team.program_id or None) != program_id:
                raise ValidationError(
                    f"Team {tid} belongs to a different program than this "
                    "rollover; it cannot be carried into this season.")
            # #283 Slice E: rollover resolves the target LeagueSeason from the
            # Team's PERMANENT League, so a team with none can't be carried.
            if not team.league_id:
                raise ValidationError(
                    f"Team {tid} has no permanent league; it cannot be rolled "
                    "into the target season.",
                    {"reason": "team_without_league", "team_id": tid})
        # (b) Every target division must belong to the target season.
        for div_id in set(wanted.values()):
            if div_id is not None:
                division = self.store.get_division(div_id)
                if division is None:
                    raise NotFoundError(f"Division {div_id} not found.")
                # #283: a Division's Season is resolved via its LeagueSeason.
                div_ls = self.store.get_league_season(division.league_season_id)
                if div_ls is None or div_ls.season_id != to_season_id:
                    raise ValidationError(
                        "A target division belongs to a different season.")

        rolled, skipped, created = 0, 0, []
        for tid, div_id in wanted.items():
            # #283 Slice E: a rollover ALWAYS resolves the target LeagueSeason
            # from the Team's PERMANENT League — never a "sole/latest/default"
            # guess. (Gate (a) already proved team + team.league_id exist.) A
            # chosen target Division must belong to that exact LeagueSeason.
            team = self.store.get_team(tid)
            target_ls = self._link_league_season(team.league_id, to_season_id)
            if div_id is not None:
                division = self.store.get_division(div_id)
                div_ls = (self.store.get_league_season(division.league_season_id)
                          if division else None)
                if div_ls is None or div_ls.id != target_ls.id:
                    raise ValidationError(
                        "The chosen division is not in this team's league for "
                        "the target season.",
                        {"reason": "division_not_in_team_league",
                         "team_id": tid, "division_id": div_id,
                         "league_id": team.league_id})
            existing = next(
                (r for r in self.store.registrations_for_season(to_season_id)
                 if r.team_id == tid), None)
            if existing is not None and existing.active:
                skipped += 1
                continue
            if existing is not None:  # reactivate a prior inactive registration
                existing.active = True
                existing.division_id = div_id
                existing.league_season_id = target_ls.id
                self.store.save_season_team_registration(existing)
                reg = existing
            else:
                reg = SeasonTeamRegistration(
                    id=self.store.next_id("streg"),
                    league_season_id=target_ls.id,
                    team_id=tid, division_id=div_id,
                    active=True)
                self.store.add_season_team_registration(reg)
            self._audit("season_team_registered", "season_team_registration",
                        reg.id, actor_id,
                        {"season_id": to_season_id, "team_id": tid,
                         "division_id": div_id,
                         "rolled_forward_from": from_season_id})
            rolled += 1
            created.append(reg)
        self._audit("season_registrations_rolled_forward", "season",
                    to_season_id, actor_id,
                    {"from_season_id": from_season_id,
                     "rolled_forward": rolled, "skipped": skipped})
        return {"rolled_forward": rolled, "skipped": skipped,
                "registrations": created}

    @_transactional
    def roll_forward_registrations_v2(self, from_season_id: str,
                                      to_season_id: str,
                                      selections: Optional[list] = None,
                                      actor_id: Optional[str] = None) -> dict:
        """Canonical v2 rollover honoring the required grouping League (#233 C2).

        Unlike the FROZEN v1 ``roll_forward_registrations`` (which ignores the
        grouping League and derives it), every carried selection REQUIRES an
        explicit target ``league_id``: the League must belong to the TARGET
        Season, the optional Division must belong to that League and Season, and
        the written registration's ``league_id`` is that League verbatim (never
        null). Selections are therefore mandatory in v2 — a division-less
        copy-all can't know which League each team joins. Same all-or-nothing
        pre-write gate as v1: any failure aborts the batch with zero writes."""
        if not isinstance(from_season_id, str) or _blank(from_season_id):
            raise ValidationError("from_season_id must be a non-empty string.")
        if not isinstance(to_season_id, str) or _blank(to_season_id):
            raise ValidationError("to_season_id must be a non-empty string.")
        src = self.store.get_season(from_season_id)
        if src is None:
            raise NotFoundError(f"Season {from_season_id} not found.")
        # #159 — canonical Team → League → Season lock order (shared with
        # transfer_team_to_league). Row-lock every carried Team FIRST (sorted, so
        # a batch never deadlocks Team-vs-Team), so its permanent league_id can't
        # move under a concurrent transfer between the rule-7 gate and the
        # registration write; THEN row-lock every distinct target League (sorted)
        # so a binding can't be orphaned by a concurrent delete_league and the
        # order never inverts; THEN the target Season below. Malformed selections
        # are re-validated in the pre-write gate; only well-formed ids are locked
        # here (a bad one still fails the gate with zero writes).
        if isinstance(selections, list):
            for _tid in sorted({sel.get("team_id") for sel in selections
                                if isinstance(sel, dict)
                                and isinstance(sel.get("team_id"), str)
                                and not _blank(sel.get("team_id"))}):
                self.store.get_team_for_update(_tid)
            for _lid in sorted({sel.get("league_id") for sel in selections
                                if isinstance(sel, dict)
                                and isinstance(sel.get("league_id"), str)
                                and not _blank(sel.get("league_id"))}):
                self._lock_league_for_binding(_lid)
        # #159 — lock the target row so a rollover serializes with archive.
        dst = self.store.get_season_for_update(to_season_id)
        if dst is None:
            raise NotFoundError(f"Season {to_season_id} not found.")
        # #159 — never roll participation INTO an archived (read-only) season.
        if dst.status == SeasonStatus.ARCHIVED:
            raise ValidationError(
                f"Season '{dst.name}' is archived and read-only. Reopen it "
                "before rolling participation into it.",
                {"reason": "season_archived", "season_id": to_season_id})
        if from_season_id == to_season_id:
            raise ValidationError("Source and target seasons must differ.")
        if (src.program_id or None) != (dst.program_id or None):
            raise ValidationError(
                "Cannot roll participation between seasons of different programs.")
        source_active = {r.team_id
                         for r in self.store.registrations_for_season(from_season_id)
                         if r.active}
        if not isinstance(selections, list) or not selections:
            raise ValidationError(
                "v2 rollover requires a non-empty selections list; each "
                "selection needs a target league_id.")
        program_id = src.program_id or None
        # Pre-write gate — resolve/validate every selection before any write, so
        # a failure leaves the target season and audit log untouched.
        wanted = {}  # team_id -> (league_id, division_id)
        for sel in selections:
            if not isinstance(sel, dict):
                raise ValidationError(
                    "Each selection must be an object with a team_id and "
                    "league_id.")
            tid = sel.get("team_id")
            if not isinstance(tid, str) or _blank(tid):
                raise ValidationError("Each selection needs a non-empty team_id.")
            if tid not in source_active:
                raise ValidationError(
                    f"Team {tid} is not registered in the source season.")
            lid = sel.get("league_id")
            if not isinstance(lid, str) or _blank(lid):
                raise ValidationError(
                    f"Selection for team {tid} needs a target league_id.")
            league = self.store.get_league(lid)
            if league is None:
                raise NotFoundError(f"League {lid} not found.")
            # #283: a League is permanent; its participation in the target
            # Season is a LeagueSeason. Ensure/create that binding (which
            # enforces the shared-Program invariant) in place of the old
            # league.season_id == to_season_id membership check.
            self._link_league_season(lid, to_season_id)
            div = sel.get("division_id")
            if div is not None:
                if not isinstance(div, str):
                    raise ValidationError(
                        "A selection's division_id must be a string or null.")
                division = self.store.get_division(div)
                if division is None:
                    raise NotFoundError(f"Division {div} not found.")
                # #283: a Division's Season and League resolve via its
                # LeagueSeason.
                div_ls = self.store.get_league_season(division.league_season_id)
                if div_ls is None or div_ls.season_id != to_season_id:
                    raise ValidationError(
                        "A target division belongs to a different season.")
                if div_ls.league_id != lid:
                    raise ValidationError(
                        "A selection's division belongs to a different league "
                        "than the selection's league.")
            team = self.store.get_team(tid)
            if team is None:
                raise ValidationError(
                    f"Team {tid} in the source season no longer exists; "
                    "it cannot be rolled forward.")
            if (team.program_id or None) != program_id:
                raise ValidationError(
                    f"Team {tid} belongs to a different program than this "
                    "rollover; it cannot be carried into this season.")
            # #283 Slice E: a Team may only be rolled into its OWN permanent
            # League — the selection's league_id must equal Team.league_id
            # (rule 7). Never a sole/latest/default guess.
            if (team.league_id or None) != lid:
                raise ValidationError(
                    f"Team {tid} can only roll into its permanent league "
                    f"{team.league_id or 'none'}, not {lid}.",
                    {"reason": "rollover_league_not_team_league",
                     "team_id": tid, "team_league_id": team.league_id,
                     "selected_league_id": lid})
            div_id = div or None
            # An already-active target registration is an idempotent skip ONLY
            # when its League AND Division exactly match this selection. A
            # mismatch means the selection's required League/Division would be
            # silently ignored (the team left in its current League) — a
            # contract violation. Catch it in the pre-write gate so the whole
            # batch aborts with zero writes rather than reporting a false skip.
            existing = next(
                (r for r in self.store.registrations_for_season(to_season_id)
                 if r.team_id == tid), None)
            existing_lid = (self._registration_league_id(existing)
                            if existing is not None else None)  # #283
            if existing is not None and existing.active and (
                    (existing_lid or None) != lid
                    or (existing.division_id or None) != div_id):
                raise ValidationError(
                    f"Team {tid} is already registered in the target season "
                    "under a different league/division than this selection; "
                    "resolve the existing registration first.",
                    {"reason": "rollover_conflicts_active_registration",
                     "team_id": tid, "registration_id": existing.id,
                     "expected_league_id": lid, "expected_division_id": div_id,
                     "actual_league_id": existing_lid,
                     "actual_division_id": existing.division_id})
            wanted[tid] = (lid, div_id)

        rolled, skipped, created = 0, 0, []
        for tid, (lid, div_id) in wanted.items():
            # #283: the registration is stored against the League's LeagueSeason
            # in the target Season (find-or-create; idempotent).
            target_ls = self._link_league_season(lid, to_season_id)
            existing = next(
                (r for r in self.store.registrations_for_season(to_season_id)
                 if r.team_id == tid), None)
            if existing is not None and existing.active:
                # Guaranteed an exact League+Division match by the pre-write gate
                # above — a safe idempotent skip.
                skipped += 1
                continue
            if existing is not None:  # reactivate a prior inactive registration
                existing.active = True
                existing.division_id = div_id
                existing.league_season_id = target_ls.id
                self.store.save_season_team_registration(existing)
                reg = existing
            else:
                reg = SeasonTeamRegistration(
                    id=self.store.next_id("streg"),
                    league_season_id=target_ls.id,
                    team_id=tid, division_id=div_id, active=True)
                self.store.add_season_team_registration(reg)
            self._audit("season_team_registered", "season_team_registration",
                        reg.id, actor_id,
                        {"season_id": to_season_id, "team_id": tid,
                         "division_id": div_id, "league_id": lid,
                         "rolled_forward_from": from_season_id})
            rolled += 1
            created.append(reg)
        self._audit("season_registrations_rolled_forward", "season",
                    to_season_id, actor_id,
                    {"from_season_id": from_season_id,
                     "rolled_forward": rolled, "skipped": skipped})
        return {"rolled_forward": rolled, "skipped": skipped,
                "registrations": created}

    # -- reassignment: move a record under a new parent (#166 PR D) --------
    # Each records the old→new parent id in the audit detail so a move is
    # traceable. Nullable links (venue→org, division→level, team→club) accept
    # None to unassign; required links (rink→venue, team→division,
    # player→team) demand a valid target so a move never orphans a record.
    @_transactional
    def assign_venue_organization(self, venue_id: str,
                                  organization_id: Optional[str] = None,
                                  actor_id: Optional[str] = None) -> Venue:
        venue = self.store.get_venue(venue_id)
        if venue is None:
            raise NotFoundError(f"Venue {venue_id} not found.")
        if organization_id and self.store.get_organization(organization_id) is None:
            raise NotFoundError(f"Organization {organization_id} not found.")
        old = venue.organization_id
        venue.organization_id = organization_id or None
        self.store.save_venue(venue)
        self._audit("venue_organization_assigned", "venue", venue.id, actor_id,
                    {"from": old, "to": venue.organization_id})
        return venue

    @_transactional
    def assign_rink_venue(self, rink_id: str, venue_id: str,
                          actor_id: Optional[str] = None) -> Rink:
        rink = self.store.get_rink(rink_id)
        if rink is None:
            raise NotFoundError(f"Rink {rink_id} not found.")
        if not venue_id or self.store.get_venue(venue_id) is None:
            raise NotFoundError(f"Venue {venue_id} not found.")
        old = rink.venue_id
        rink.venue_id = venue_id
        self.store.save_rink(rink)
        self._audit("rink_venue_assigned", "rink", rink.id, actor_id,
                    {"from": old, "to": venue_id})
        return rink

    @_transactional
    def assign_division_league(self, division_id: str,
                               league_id: Optional[str] = None,
                               actor_id: Optional[str] = None,
                               v2: bool = False) -> Division:
        division = self.store.get_division(division_id)
        if division is None:
            raise NotFoundError(f"Division {division_id} not found.")
        # #283: a Division no longer stores its own league_id — its League (and
        # Season) are fixed by its LeagueSeason. Reassigning a Division's League
        # therefore means reparenting it to the LeagueSeason of (new League, the
        # division's own Season).
        div_ls = self.store.get_league_season(division.league_season_id)
        season_id = div_ls.season_id if div_ls else None
        # #159 — canonical League→Season lock order: row-lock the target League
        # BEFORE the Season guard, so the reparent's new LeagueSeason binding
        # can't be orphaned by a concurrent delete_league and the lock order never
        # inverts against delete/transfer.
        if league_id:
            self._lock_league_for_binding(league_id)
        if season_id:
            self._require_active_season(season_id)  # #159 read-only guard
        # #159 r15 — re-fetch the Division under the Season lock; the pre-lock
        # read was a locator. A concurrent delete_division (removes it) or
        # rename (both lock the same Season) can commit in the window, so act on
        # the fresh row — never resurrect a deleted Division or clobber a rename.
        division = self.store.get_division(division_id)
        if division is None:
            raise NotFoundError(f"Division {division_id} not found.")
        div_ls = self.store.get_league_season(division.league_season_id)
        old = div_ls.league_id if div_ls else None
        # v2 (#233 Slice C2): a canonical Division is always parented by a
        # grouping League — the reparent target is REQUIRED. v1 keeps its nullable
        # unassign behavior (league_id=None clears the division's level).
        if v2 and not league_id:
            raise ValidationError("A league_id is required.")
        # v2 dependent-record integrity (#233 Slice C2 review): moving a Division
        # between Leagues must not strand its registrations or committed games
        # under a League that no longer matches. Any active registration or
        # non-cancelled game bound to this division whose own ``league_id`` isn't
        # the new League would become cross-league — reject (safe default) and
        # mutate ZERO records/audit rather than silently splitting the data.
        if v2 and (league_id or None) != (old or None):
            stranded_regs = [
                r.id for r in
                self.store.registrations_for_season(season_id)
                if r.active and r.division_id == division.id
                and self._registration_league_id(r) != league_id]
            stranded_games = [
                g.id for g in self.store.all_games()
                if not g.cancelled and g.division_id == division.id
                and g.league_id != league_id]
            if stranded_regs or stranded_games:
                raise ValidationError(
                    "Cannot move this division to another league while "
                    "registrations or games under it belong to the old league; "
                    "reconcile them first.",
                    {"reason": "division_reparent_strands_dependents",
                     "division_id": division.id,
                     "affected_registration_ids": stranded_regs,
                     "affected_game_ids": stranded_games})
        # #283: with a target League, reparent the Division to that League's
        # LeagueSeason in the same Season. Without one (v1 unassign), a Division
        # can no longer be league-less, so this is a no-op that keeps the row on
        # its current LeagueSeason rather than clearing a field that is gone.
        if league_id and season_id and league_id != (old or None):
            target_ls = self._link_league_season(league_id, season_id)
            division.league_season_id = target_ls.id
            self.store.save_division(division)
        new_ls = self.store.get_league_season(division.league_season_id)
        new_league = new_ls.league_id if new_ls else None
        self._audit("division_level_assigned", "division", division.id, actor_id,
                    {"from": old, "to": new_league})
        return division

    @_transactional
    def assign_team_club(self, team_id: str, club_id: Optional[str] = None,
                         actor_id: Optional[str] = None) -> Team:
        # #159 r15 — row-lock the Team (not an unlocked read): transfer_team_to_
        # league / register / delete_team all lock this row, so without the lock
        # a concurrent transfer could rebind team.league_id and this whole-object
        # save would revert it while only meaning to change club_id.
        team = self.store.get_team_for_update(team_id)
        if team is None:
            raise NotFoundError(f"Team {team_id} not found.")
        # Club is optional on a Team in both v1 and v2 (#233 Slice D): null
        # unassigns it. Only validate the id when one is actually supplied.
        if club_id and self.store.get_club(club_id) is None:
            raise NotFoundError(f"Club {club_id} not found.")
        old = team.club_id
        team.club_id = club_id or None
        self.store.save_team(team)
        self._audit("team_club_assigned", "team", team.id, actor_id,
                    {"from": old, "to": team.club_id})
        return team

    # assign_team_division was removed (#180): a Team's season/division placement
    # lives in SeasonTeamRegistration (assign_season_team_division), never on the
    # legacy Team.division_id. The legacy field is retained only for persistence/
    # migration compatibility and is no longer written by any current workflow.

    @_transactional
    def assign_player_team(self, player_id: str, team_id: str,
                           actor_id: Optional[str] = None) -> Player:
        # #159 r15 — row-lock the Player (not an unlocked read): update_player /
        # set_player_active / delete_player all lock this row, so without the
        # lock a concurrent profile edit or deactivation would be clobbered (or a
        # retired player resurrected active) by this whole-object save.
        player = self.store.get_player_for_update(player_id)
        if player is None:
            raise NotFoundError(f"Player {player_id} not found.")
        if not team_id or self.store.get_team(team_id) is None:
            raise NotFoundError(f"Team {team_id} not found.")
        old = player.team_id
        # Reject a destination-team jersey collision BEFORE moving the player
        # (#269): an active player carrying a number can only move to a team
        # where that number is free among active players. exclude_player_id
        # keeps a same-team no-op from colliding with itself.
        if player.is_active:
            self._assert_jersey_available(team_id, player.jersey_number,
                                          exclude_player_id=player.id)
        player.team_id = team_id
        self.store.save_player(player)
        self._audit("player_team_assigned", "player", player.id, actor_id,
                    {"from": old, "to": team_id})
        return player

    # -- cross-domain reassignment: league owner (#173) --------------------
    @_transactional
    def assign_program_organization(self, program_id: str,
                                    organization_id: Optional[str] = None,
                                    actor_id: Optional[str] = None) -> Program:
        program = self.store.get_program(program_id)
        if program is None:
            raise NotFoundError(f"Program {program_id} not found.")
        if organization_id and self.store.get_organization(organization_id) is None:
            raise NotFoundError(f"Organization {organization_id} not found.")
        old = program.operator_organization_id
        # #233 Slice E: physical (facility owner) and competition (Program
        # operator) structure are independent trees — a Program's operating
        # organization is free to change without regard to any Venue, since
        # Venue<->Program access is now a decoupled SeasonVenueAccess grant,
        # not a shared-owner bridge.
        program.operator_organization_id = organization_id or None
        self.store.save_program(program)
        self._audit("league_organization_assigned", "league", program.id, actor_id,
                    {"from": old, "to": program.operator_organization_id})
        return program

    # -- organization / venue / rink / ice slot ---------------------------
    @_transactional
    def create_organization(self, name: str, short_name: str = "",
                            actor_id: Optional[str] = None) -> Organization:
        org = Organization(id=self.store.next_id("org"),
                           name=self._require_name(name), short_name=short_name or "")
        self.store.add_organization(org)
        self._audit("organization_created", "organization", org.id, actor_id)
        return org

    @_transactional
    def create_venue(self, name: str, address: str = "", timezone_name: str = "UTC",
                     organization_id: Optional[str] = None,
                     league_id: Optional[str] = None,
                     actor_id: Optional[str] = None) -> Venue:
        # An optional owning organization (#166) — validated when given so a
        # venue never dangles off a non-existent org; null is fine (unassigned).
        if organization_id and self.store.get_organization(organization_id) is None:
            raise NotFoundError(f"Organization {organization_id} not found.")
        # An optional owning league (#173). A league carries its own owner, so
        # the venue's owner must agree with it: prefer deriving the owner from
        # the league, and reject an explicitly-supplied conflicting owner rather
        # than silently transferring facility ownership.
        if league_id:
            league = self.store.get_program(league_id)
            if league is None:
                raise NotFoundError(f"Program {league_id} not found.")
            league_owner = league.operator_organization_id
            if organization_id and league_owner and organization_id != league_owner:
                raise ValidationError(
                    "Current v1 compatibility: while the temporary Venue→Program "
                    "link is active (removed in Slice E), a venue's facility owner "
                    "must match the program's operating organization.")
            organization_id = league_owner or organization_id
        venue = Venue(id=self.store.next_id("venue"), name=self._require_name(name),
                      address=address, timezone=timezone_name or "UTC",
                      organization_id=organization_id or None, league_id=league_id or None)
        self.store.add_venue(venue)
        detail = {k: v for k, v in
                  {"organization_id": organization_id, "league_id": league_id}.items() if v}
        self._audit("venue_created", "venue", venue.id, actor_id, detail or None)
        return venue

    @_transactional
    def create_rink(self, venue_id: str, name: str,
                    actor_id: Optional[str] = None) -> Rink:
        if self.store.get_venue(venue_id) is None:
            raise NotFoundError(f"Venue {venue_id} not found.")
        rink = Rink(id=self.store.next_id("rink"), venue_id=venue_id,
                    name=self._require_name(name))
        self.store.add_rink(rink)
        self._audit("rink_created", "rink", rink.id, actor_id, {"venue_id": venue_id})
        return rink

    @_transactional
    def create_ice_slot(self, rink_id: str, start_time: datetime, end_time: datetime,
                        slot_type: IceSlotType = IceSlotType.GAME,
                        actor_id: Optional[str] = None) -> IceSlot:
        # Row-lock the rink (#158 review): serializes this create with a
        # concurrent create or ice-availability commit on the same rink, so two
        # writers can't both pass the overlap check below and then both insert
        # an overlapping/duplicate slot.
        if self.store.get_rink_for_update(rink_id) is None:
            raise NotFoundError(f"Rink {rink_id} not found.")
        start = self._require_utc(start_time, "start_time")
        end = self._require_utc(end_time, "end_time")
        if end <= start:
            raise ValidationError("end_time must be after start_time.")
        # No two slots may overlap on the same rink (adjacent is fine).
        for ex in self.store.all_ice_slots():
            if ex.rink_id != rink_id:
                continue
            if start < ex.end_time and end > ex.start_time:
                raise ScheduleConflictError(
                    f"Ice slot overlaps existing slot {ex.id} on this rink."
                )
        # Only GAME ice is bookable for league games; other types are blocked
        # so they cannot be scheduled into.
        status = (IceSlotStatus.AVAILABLE if slot_type == IceSlotType.GAME
                  else IceSlotStatus.BLOCKED)
        slot = IceSlot(id=self.store.next_id("slot"), rink_id=rink_id,
                       start_time=start, end_time=end, slot_type=slot_type,
                       status=status)
        self.store.add_ice_slot(slot)
        self._audit("ice_slot_created", "ice_slot", slot.id, actor_id,
                    {"rink_id": rink_id, "slot_type": slot_type.value})
        return slot

    # -- recurring ice availability builder (#158) -------------------------
    # An arena operator builds a draft ice INVENTORY from a recurring weekly
    # template, previews the exact slots, then explicitly commits them as
    # AVAILABLE Game ice. No games or published schedule are created here; the
    # planner (#206) later consumes this inventory. The pure date/time math is
    # in services/ice_availability.py; the store-aware resolution, SeasonVenueAccess
    # gating, collision/duplicate classification, idempotent commit, and audit
    # live here next to create_ice_slot and the rinks/ice-slots importer.

    def _ice_avail_range(self, season, tz, start_date, end_date):
        """Resolve the generation date range. Explicit YYYY-MM-DD wins; otherwise
        default to the Season's own start/end rendered in the Program timezone.
        The Season is never mutated."""
        def to_date(value, field, fallback):
            if value in (None, ""):
                if fallback is None:
                    raise ValidationError(
                        f"{field} is required (the Season has no {field} to "
                        "default from).",
                        {"reason": f"missing_{field}", "field": field})
                return fallback.astimezone(tz).date()
            if isinstance(value, date) and not isinstance(value, datetime):
                return value
            try:
                return date.fromisoformat(str(value).strip())
            except ValueError:
                raise ValidationError(
                    f"Invalid {field}: {value!r}. Expected YYYY-MM-DD.",
                    {"reason": f"invalid_{field}", "field": field})
        return (to_date(start_date, "start_date", season.start_date),
                to_date(end_date, "end_date", season.end_date))

    def _ice_avail_weekdays(self, weekdays):
        if not isinstance(weekdays, (list, tuple)) or not weekdays:
            raise ValidationError(
                "Select at least one weekday.",
                {"reason": "no_weekdays", "field": "weekdays"})
        out = set()
        for wd in weekdays:
            if isinstance(wd, bool) or not isinstance(wd, int) or wd not in range(7):
                raise ValidationError(
                    f"Invalid weekday {wd!r}; use 0 (Monday) through 6 (Sunday).",
                    {"reason": "invalid_weekday", "field": "weekdays"})
            out.add(wd)
        return out

    def _ice_avail_windows(self, *, weekdays, start_local, end_local, windows):
        """Normalize the recurring template's per-weekday time windows.

        #158's recorded operator flow gives each selected weekday its OWN local
        start/end time ("local start and end time for each selected day"). The
        canonical input is ``windows`` — a list of
        ``{"weekday", "start_local", "end_local"}``, one entry per selected day.
        The older uniform form (``weekdays`` + a single ``start_local`` /
        ``end_local``) is still accepted and expands to the same window on every
        selected day, so a single contracted block stays a two-field entry.

        Returns ``(weekday_windows, windows_meta)`` where ``weekday_windows`` is
        the ``{wd: ((sh, sm), (eh, em))}`` the pure planner consumes and
        ``windows_meta`` is the ordered ``[{"weekday", "start", "end"}]`` echoed
        back through the preview/commit response and the commit audit, so the
        per-day windows are preserved verbatim across idempotent reruns.
        """
        if windows not in (None, ""):
            if not isinstance(windows, (list, tuple)) or not windows:
                raise ValidationError(
                    "Select at least one weekday.",
                    {"reason": "no_weekdays", "field": "weekdays"})
            weekday_windows, meta, seen = {}, [], set()
            for entry in windows:
                if not isinstance(entry, dict):
                    raise ValidationError(
                        "Each window must be a {weekday, start_local, end_local} "
                        "block.",
                        {"reason": "invalid_window", "field": "windows"})
                wd = entry.get("weekday")
                if isinstance(wd, bool) or not isinstance(wd, int) \
                        or wd not in range(7):
                    raise ValidationError(
                        f"Invalid weekday {wd!r}; use 0 (Monday) through 6 (Sunday).",
                        {"reason": "invalid_weekday", "field": "weekdays"})
                if wd in seen:
                    raise ValidationError(
                        f"{WEEKDAY_NAMES[wd]} is listed more than once; give each "
                        "selected weekday a single time window.",
                        {"reason": "duplicate_weekday", "field": "weekdays"})
                seen.add(wd)
                start = entry.get("start_local")
                end = entry.get("end_local")
                weekday_windows[wd] = (parse_hhmm(start, "start_local"),
                                       parse_hhmm(end, "end_local"))
                meta.append({"weekday": wd, "start": start, "end": end})
            meta.sort(key=lambda m: m["weekday"])
            return weekday_windows, meta
        # Legacy uniform form: one window applied to every selected weekday.
        weekday_set = self._ice_avail_weekdays(weekdays)
        window = (parse_hhmm(start_local, "start_local"),
                  parse_hhmm(end_local, "end_local"))
        weekday_windows = {wd: window for wd in weekday_set}
        meta = [{"weekday": wd, "start": start_local, "end": end_local}
                for wd in sorted(weekday_set)]
        return weekday_windows, meta

    def _ice_avail_exclusions(self, exclusion_dates):
        if exclusion_dates in (None, ""):
            return set()
        if not isinstance(exclusion_dates, (list, tuple)):
            raise ValidationError(
                "exclusion_dates must be a list of YYYY-MM-DD dates.",
                {"reason": "invalid_exclusion_dates", "field": "exclusion_dates"})
        out = set()
        for value in exclusion_dates:
            try:
                out.add(date.fromisoformat(str(value).strip()))
            except ValueError:
                raise ValidationError(
                    f"Invalid exclusion date {value!r}. Expected YYYY-MM-DD.",
                    {"reason": "invalid_exclusion_dates", "field": "exclusion_dates"})
        return out

    def _plan_ice_availability(self, *, season_id, rink_ids, weekdays,
                               start_local, end_local, start_date, end_date,
                               playable_minutes, turnover_minutes,
                               exclusion_dates, windows=None):
        """Deterministic, side-effect-free planning shared by preview and commit:
        resolve the Season timezone + date range, run the pure planner, split the
        (de-duplicated) selected rinks by SeasonVenueAccess, and classify the
        proposed windows against current inventory so the returned ``fingerprint``
        binds the ENTIRE reviewed preview payload — every commit-relevant input
        and operator-visible resolved field, not just the generated tuples or
        their classification (#158 review — see the fingerprint block below).
        Reads inventory only for the classification/totals; commit calls this
        UNDER its write locks, so the bound snapshot matches the one its write
        loop acts on."""
        season = self.store.get_season(season_id) if season_id else None
        if season is None:
            raise NotFoundError(
                "Season not found.",
                details={"reason": "season_missing", "season_id": season_id})
        program = self.store.get_program(season.program_id)
        tz = resolve_timezone(program.timezone if program else None) or timezone.utc

        if not isinstance(rink_ids, (list, tuple)) or not rink_ids:
            raise ValidationError(
                "Select at least one rink.",
                {"reason": "no_rinks", "field": "rink_ids"})
        # De-duplicate up front (#158 review): a repeated rink must never
        # generate the same slot twice within one request.
        rink_ids = list(dict.fromkeys(rink_ids))

        d_start, d_end = self._ice_avail_range(season, tz, start_date, end_date)
        weekday_windows, windows_meta = self._ice_avail_windows(
            weekdays=weekdays, start_local=start_local, end_local=end_local,
            windows=windows)
        weekday_set = set(weekday_windows)
        exclusions = self._ice_avail_exclusions(exclusion_dates)

        plan = plan_ice_windows(
            weekday_windows=weekday_windows, start_date=d_start, end_date=d_end,
            playable_minutes=playable_minutes, turnover_minutes=turnover_minutes,
            exclusion_dates=exclusions, tz=tz)

        # Split rinks by SeasonVenueAccess: the builder never grants access
        # (that is a MANAGE_SETUP action) — a rink whose Venue is not available
        # to this Season is reported with a remediation route and produces no
        # slots, mirroring the scheduler's require_slot_belongs_to_season gate.
        # commit re-runs this SAME split under its Season/rink write locks so a
        # revoke/move/delete that lands after this preview read can't leak ice.
        accessible, access_missing = self._split_rinks_by_access(
            season_id, rink_ids)

        # Classify the proposed windows against the CURRENT ice inventory, then
        # bind the WHOLE reviewed preview INTO the fingerprint by hashing the exact
        # operator-visible payload the preview response renders, minus the token
        # itself (#158 review). Four progressively-rejected narrower bindings show
        # why it must be the whole payload:
        #   * the raw form fields alone miss a concurrent Program-timezone /
        #     Season-boundary edit that moves the resolved windows under a stale
        #     form;
        #   * the generated (rink, start, end) tuples alone miss a slot or Game
        #     added, removed, allocated, or booked after preview that reclassifies
        #     a row (new <-> duplicate <-> conflict) or changes its conflict target
        #     WITHOUT moving the tuple;
        #   * tuples + classification STILL miss an edit that changes what the
        #     operator reviewed without changing any tuple — extend a window past
        #     the last slot, retune the playable / turnover minutes, add an
        #     exclusion on an already-empty day, or flip a day skipped<->too_short;
        #   * even a hand-maintained field list misses the reviewed RINK/VENUE
        #     IDENTITY — rename a rink or reassign it to another Season-authorized
        #     Venue after preview and the tuples/classifications/totals are
        #     unchanged, yet the operator reviewed a different physical context.
        # Rather than chase fields, derive the token STRUCTURALLY from
        # _reviewed_preview_payload — the same builder _ice_availability_response
        # uses — so every field the operator sees (per-rink {id, name, venue} rows,
        # the full venue-access-missing rows, totals, skipped/too-short, the full
        # conflict target, ...) is bound for free and adding a field to the
        # response binds it automatically. Any change flips the token and forces a
        # re-preview of exactly what changed; an unedited template over unchanged
        # inventory is stable, so preview and commit agree. Commit recomputes this
        # UNDER its Season + per-rink write locks, so the bound snapshot is the one
        # its write loop acts on. Deterministic — no clock; the payload's list
        # order is fixed by the (identical) template + inventory, sort_keys
        # canonicalizes the rest.
        classified = self._classify_ice_windows(accessible, plan)
        base = {
            "season": season, "tz": tz, "d_start": d_start, "d_end": d_end,
            "weekday_set": weekday_set,
            "windows_meta": windows_meta,
            "playable_minutes": playable_minutes,
            "turnover_minutes": turnover_minutes,
            "plan": plan, "accessible": accessible,
            "access_missing": access_missing,
            "classified": classified,
        }
        base["fingerprint"] = hashlib.sha256(json.dumps(
            self._reviewed_preview_payload(base),
            sort_keys=True, separators=(",", ":"), default=str
        ).encode()).hexdigest()[:16]
        return base

    def _split_rinks_by_access(self, season_id, rink_ids):
        """Resolve each selected rink's current Venue + active SeasonVenueAccess
        and split them into ``accessible`` [(rink, venue), ...] and
        ``access_missing`` [report, ...]. Pure reads — no locks of its own — so
        preview calls it directly, while commit calls it AGAIN inside its write
        transaction (after the Season + per-rink locks) so a revoke/move/delete
        that raced the preview read is caught before any slot or audit is
        written (#158 review). A rink id that no longer resolves is the same
        stable ``rink_missing`` NotFoundError the preview raises."""
        accessible, access_missing = [], []
        for rid in rink_ids:
            rink = self.store.get_rink(rid)
            if rink is None:
                raise NotFoundError(
                    f"Rink {rid} not found.",
                    details={"reason": "rink_missing", "rink_id": rid})
            venue = self.store.get_venue(rink.venue_id) if rink.venue_id else None
            access = (self.store.season_venue_access_for_pair(season_id, rink.venue_id)
                      if venue else None)
            if venue is None or access is None or not access.active:
                access_missing.append({
                    "rink_id": rid, "rink_name": rink.name,
                    "venue_id": rink.venue_id,
                    "venue_name": venue.name if venue else None,
                    "remediation_route":
                        f"/api/v2/setup/seasons/{season_id}/venue-access",
                })
            else:
                accessible.append((rink, venue))
        return accessible, access_missing

    def _slot_compatible_duplicate(self, slot):
        """Whether an exact ``(rink, start, end)`` match is an idempotent
        DUPLICATE — i.e. it is already compatible AVAILABLE Game ice with no
        active Game on it. An ALLOCATED slot backing a Game, or a BLOCKED /
        maintenance / non-Game slot at that exact tuple, is NOT a harmless
        duplicate: it is a real collision the preview must report and the commit
        must refuse to overwrite, not hidden capacity (#158 review)."""
        return (slot.slot_type == IceSlotType.GAME
                and slot.status == IceSlotStatus.AVAILABLE
                and self.store.game_using_ice_slot(slot.id) is None)

    def _classify_ice_windows(self, accessible, plan):
        """Classify each (accessible rink × planner window) against CURRENT ice
        inventory as new / duplicate / conflict. Reads all_ice_slots(); the
        commit path runs an equivalent pass INSIDE its write transaction (under
        the Season + per-rink locks) so its decision can never go stale."""
        existing_by_rink = {}
        for existing in self.store.all_ice_slots():
            existing_by_rink.setdefault(existing.rink_id, []).append(existing)
        classified = []
        for rink, venue in accessible:
            existing = existing_by_rink.get(rink.id, [])
            for w in plan["windows"]:
                start, end = w["start"], w["end"]
                exact = next((e for e in existing if e.start_time == start
                              and e.end_time == end), None)
                # An exact tuple is an idempotent DUPLICATE only when it is
                # compatible AVAILABLE Game ice; an occupied / blocked / non-Game
                # slot at that tuple — or any non-exact overlap — is a conflict.
                if exact is not None and self._slot_compatible_duplicate(exact):
                    status, ref, clash = "duplicate", exact.id, None
                else:
                    clash = exact or next(
                        (e for e in existing if intervals_overlap(
                            start, end, e.start_time, e.end_time)), None)
                    status = "conflict" if clash is not None else "new"
                    ref = clash.id if clash is not None else None
                game = (self.store.game_using_ice_slot(clash.id)
                        if clash is not None else None)
                classified.append({
                    "rink_id": rink.id, "rink_name": rink.name,
                    "venue_id": venue.id, "venue_name": venue.name,
                    "date": w["date"], "start": start, "end": end,
                    "start_local": w["start_local"], "end_local": w["end_local"],
                    "status": status, "conflict_with": ref,
                    "conflict_has_game": game is not None,
                    "conflict_slot_type": clash.slot_type.value if clash else None,
                    "conflict_slot_status": clash.status.value if clash else None,
                    "conflict_game_id": game.id if game is not None else None,
                })
        return classified

    def _resolve_ice_availability(self, **kwargs):
        """Plan + classify against current inventory — the PREVIEW path (no
        writes). _plan_ice_availability already classified (to bind the
        fingerprint), so reuse that ``classified`` rather than re-running it."""
        base = self._plan_ice_availability(**kwargs)
        return {**base, "weekdays": sorted(base["weekday_set"])}

    def _reviewed_preview_payload(self, r):
        """The operator-visible preview payload — EVERYTHING the preview response
        renders EXCEPT the token itself (#158 review). The fingerprint is the hash
        of this (see ``_plan_ice_availability``) and ``_ice_availability_response``
        returns this PLUS the token, so the token binds precisely what the operator
        reviewed: any field added here is bound for free. Notably it carries each
        rink's full ``{rink_id, rink_name, venue_id, venue_name}`` identity and the
        full venue-access-missing rows, so a post-preview rename or venue
        reassignment moves the token even when the generated tuples do not.
        Deterministic — datetimes to ISO, list order fixed by the template +
        inventory, no clock."""
        classified = r["classified"]
        plan = r["plan"]
        n_rinks = len(r["accessible"])
        per_rink = {}
        for c in classified:
            row = per_rink.setdefault(c["rink_id"], {
                "rink_id": c["rink_id"], "rink_name": c["rink_name"],
                "venue_id": c["venue_id"], "venue_name": c["venue_name"],
                "new": 0, "duplicate": 0, "conflict": 0})
            row[c["status"]] += 1
        new_n = sum(1 for c in classified if c["status"] == "new")
        dup_n = sum(1 for c in classified if c["status"] == "duplicate")
        con_n = sum(1 for c in classified if c["status"] == "conflict")
        return {
            "season_id": r["season"].id, "season_name": r["season"].name,
            "timezone": str(r["tz"]),
            "date_range": {"start": r["d_start"].isoformat(),
                           "end": r["d_end"].isoformat()},
            "weekdays": sorted(r["weekday_set"]), "windows": r["windows_meta"],
            "playable_minutes": r["playable_minutes"],
            "turnover_minutes": r["turnover_minutes"],
            "rinks": list(per_rink.values()),
            "slots": [{
                "rink_id": c["rink_id"], "rink_name": c["rink_name"],
                "date": c["date"],
                "start_time": c["start"].isoformat(),
                "end_time": c["end"].isoformat(),
                "start_local": c["start_local"], "end_local": c["end_local"],
                "status": c["status"], "conflict_with": c["conflict_with"],
                "conflict_has_game": c["conflict_has_game"],
                "conflict_slot_type": c["conflict_slot_type"],
                "conflict_slot_status": c["conflict_slot_status"],
                "conflict_game_id": c["conflict_game_id"],
            } for c in classified],
            "totals": {
                "new": new_n, "duplicate": dup_n, "conflict": con_n,
                "capacity_games": new_n,
                "reserved_minutes": plan["reserved_minutes"] * n_rinks,
                "playable_minutes": plan["playable_minutes_total"] * n_rinks,
            },
            "skipped_dates": plan["skipped_dates"],
            "too_short": plan["too_short"],
            "dst_skipped": plan["dst_skipped"],
            "dst_ambiguous": plan["dst_ambiguous"],
            "venue_access_missing": r["access_missing"],
        }

    def _ice_availability_response(self, r):
        """The preview API response: the reviewed payload (exactly what the token
        binds) PLUS the token itself (#158 review)."""
        return {**self._reviewed_preview_payload(r),
                "template_fingerprint": r["fingerprint"]}

    def _ice_slot_dto(self, slot):
        return {"id": slot.id, "rink_id": slot.rink_id,
                "start_time": slot.start_time.isoformat(),
                "end_time": slot.end_time.isoformat(),
                "slot_type": slot.slot_type.value, "status": slot.status.value}

    def _has_matching_preview_audit(self, actor_id, fingerprint):
        """True if ``actor_id`` recorded a successful ``ice_availability_previewed``
        audit for exactly ``fingerprint`` — the commit preview gate (#158 review).

        The preview fingerprint is a hash of the RESOLVED snapshot (Season
        boundaries, Program timezone, generated UTC windows), and a successful
        preview writes one server-attributed audit carrying that fingerprint and
        the acting operator. Requiring a matching audit makes the fingerprint a
        real, actor-bound preview capability rather than a value a caller can
        fabricate or replay across operators. Setup-audit volume is low
        (arena-setup actions), so the linear scan is not a hot path.
        """
        if actor_id is None or fingerprint is None:
            return False
        for entry in self.store.all_setup_audit():
            if (entry.action == "ice_availability_previewed"
                    and entry.actor_id == actor_id
                    and entry.detail.get("template_fingerprint") == fingerprint):
                return True
        return False

    def preview_ice_availability(self, *, season_id=None, rink_ids=None,
                                 weekdays=None, start_local=None, end_local=None,
                                 start_date=None, end_date=None,
                                 playable_minutes=None, turnover_minutes=None,
                                 exclusion_dates=None, windows=None,
                                 actor_id=None):
        """Preview the slots a recurring template would create (#158).

        The planning itself is side-effect-free; a SUCCESSFUL preview by an
        authenticated caller records one server-attributed
        ``ice_availability_previewed`` audit carrying the ``template_fingerprint``
        (so it correlates to the later commit), the Season, the deduplicated
        Rinks, the resolved range/timezone, and the result totals — #158 audits
        both halves of the flow. An invalid or not-found template raises before
        the audit, and an unauthenticated caller (``actor_id`` None, e.g. a
        programmatic reader) records nothing, so a failed or anonymous preview
        never leaves an audit row.
        """
        resp = self._ice_availability_response(self._resolve_ice_availability(
            season_id=season_id, rink_ids=rink_ids, weekdays=weekdays,
            start_local=start_local, end_local=end_local,
            start_date=start_date, end_date=end_date,
            playable_minutes=playable_minutes, turnover_minutes=turnover_minutes,
            exclusion_dates=exclusion_dates, windows=windows))
        if actor_id is not None:
            with self.store.transaction():
                self._audit(
                    "ice_availability_previewed", "season", resp["season_id"],
                    actor_id, {
                        "template_fingerprint": resp["template_fingerprint"],
                        "season_id": resp["season_id"],
                        "rink_ids": ([r["rink_id"] for r in resp["rinks"]]
                                     + [m["rink_id"]
                                        for m in resp["venue_access_missing"]]),
                        "timezone": resp["timezone"],
                        "date_range": resp["date_range"],
                        "totals": resp["totals"],
                    })
        return resp

    def commit_ice_availability(self, *, season_id=None, rink_ids=None,
                                weekdays=None, start_local=None, end_local=None,
                                start_date=None, end_date=None,
                                playable_minutes=None, turnover_minutes=None,
                                exclusion_dates=None, windows=None,
                                template_fingerprint=None, actor_id=None):
        """Create the AVAILABLE Game ice slots a template implies (#158).

        Idempotent and race-safe: planning is side-effect-free, but the
        classify-then-write happens together INSIDE one transaction holding the
        Season lock (serializes commit-vs-commit) and a per-rink row lock
        (serializes commit-vs-manual-create), with the (rink, start, end) unique
        index (migration 045) as the atomic backstop. So concurrent commits, or a
        commit racing a manual/import write, can never create duplicate or
        overlapping slots. An exact-duplicate window is skipped, an overlap is
        reported, a rerun creates nothing. Audited per slot + a batch summary.
        Requires an active Season (#159).

        Preview is REQUIRED (#158 review): the commit is refused before any
        write unless ``template_fingerprint`` is supplied (``preview_required``),
        equals the freshly-resolved snapshot's fingerprint (``preview_mismatch``
        — an edited or stale template), AND matches a successful
        ``ice_availability_previewed`` audit for ``actor_id`` (``preview_required``
        — a fabricated or cross-operator token). So an authenticated caller can
        never create the recurring inventory without first previewing exactly
        this template; the fingerprint is a real preview capability tied to the
        acting operator and the resolved snapshot, not an optional hint. A
        refused commit mutates nothing (no slots, no batch audit).
        """
        # De-duplicated selected rinks (same as _plan_ice_availability does).
        # NOTHING authoritative is resolved from mutable state out here: the
        # entire plan — Season boundaries, Program timezone, generated windows
        # AND venue access — is (re)built INSIDE the write transaction below,
        # under the Season / Program / rink locks, so nothing it produces can be
        # stale relative to committed state.
        requested_rink_ids = list(dict.fromkeys(rink_ids or []))
        # Generated up front so every per-slot audit row is tagged with it.
        batch_id = self.store.next_id("iceavailbatch")

        created, counts = [], {}
        # Retry the whole batch if the unique-index backstop rejects an INSERT —
        # rare: the Season + per-rink locks already serialize commits and manual
        # creates, so only the lock-free CSV import path can trigger it.
        for attempt in range(3):
            created, dup, conflict = [], 0, 0
            try:
                with self.store.transaction():
                    # Canonical lock order Program -> Rinks -> Season, matching
                    # the hierarchy import (it saves Programs, then Rinks, then
                    # upsert_imported_season takes the Season lock), so a
                    # concurrent import changing a Program timezone / Season dates
                    # and this commit can never deadlock (#158 review). program_id
                    # comes from a pre-lock read, so revalidate the Season->Program
                    # link under the Season lock and retry (bounded) if the Season
                    # was re-parented to another Program in between.
                    pre = self.store.get_season(season_id)
                    if pre is None:
                        raise NotFoundError(
                            "Season not found.",
                            details={"reason": "season_missing",
                                     "season_id": season_id})
                    if pre.program_id:
                        self.store.get_program_for_update(pre.program_id)
                    for rid in sorted(requested_rink_ids):
                        self.store.get_rink_for_update(rid)
                    season = self._require_active_season(season_id)
                    if season.program_id != pre.program_id:
                        raise _SeasonReparented()
                    # Build the ENTIRE plan UNDER the locks (#158 review): the
                    # Season boundaries, Program timezone and generated UTC
                    # windows can't be shifted out of range or to the wrong
                    # instants by a concurrent metadata edit, and venue access is
                    # re-resolved so a revoke/move/delete after the preview read
                    # can't leak ice (grant/revoke take the Season lock we hold; a
                    # deleted rink is the same stable rink_missing, rolling back).
                    base = self._plan_ice_availability(
                        season_id=season_id, rink_ids=rink_ids,
                        weekdays=weekdays, start_local=start_local,
                        end_local=end_local, start_date=start_date,
                        end_date=end_date, playable_minutes=playable_minutes,
                        turnover_minutes=turnover_minutes,
                        exclusion_dates=exclusion_dates, windows=windows)
                    # Preview binding (#158 review): a commit MUST be preceded by
                    # a preview of the SAME resolved template BY THE SAME actor.
                    # The fingerprint alone is not a capability — a client can
                    # omit or fabricate it — so all three of these must hold, and
                    # they run BEFORE any write, so a refused commit mutates
                    # nothing (no slots, no batch audit): the recurring inventory
                    # can never be created without previewing exactly this
                    # template first.
                    #   1. a fingerprint was supplied at all (preview_required);
                    #   2. it equals the freshly-resolved snapshot's fingerprint,
                    #      deterministic over the same inputs so an unedited form
                    #      always matches and an edited/stale one never does
                    #      (preview_mismatch);
                    #   3. it matches a successful ice_availability_previewed
                    #      audit for THIS actor — rejecting a fabricated token or
                    #      one previewed by a different operator (preview_required).
                    if template_fingerprint is None:
                        raise ValidationError(
                            "Preview this template before creating slots.",
                            details={"reason": "preview_required"})
                    if template_fingerprint != base["fingerprint"]:
                        raise ScheduleConflictError(
                            "This template changed since it was previewed. "
                            "Preview again before creating slots.",
                            details={"reason": "preview_mismatch",
                                     "expected": base["fingerprint"]})
                    if not self._has_matching_preview_audit(
                            actor_id, base["fingerprint"]):
                        raise ValidationError(
                            "No matching preview by this operator for this "
                            "template. Preview it before creating slots.",
                            details={"reason": "preview_required"})
                    # NB: keep the `windows` PARAMETER intact for the retry loop —
                    # bind the generated planner windows to a distinct name, or a
                    # second attempt would re-plan with generated rows instead of
                    # the per-weekday input and raise.
                    plan_windows = base["plan"]["windows"]
                    accessible = base["accessible"]
                    access_missing = base["access_missing"]
                    access_skipped = len(access_missing)
                    existing_by_rink = {}
                    for s in self.store.all_ice_slots():
                        existing_by_rink.setdefault(s.rink_id, []).append(s)
                    for rink, _venue in accessible:
                        existing = existing_by_rink.setdefault(rink.id, [])
                        for w in plan_windows:
                            start, end = w["start"], w["end"]
                            exact = next(
                                (e for e in existing if e.start_time == start
                                 and e.end_time == end), None)
                            # Exact tuple is an idempotent skip ONLY when it is
                            # compatible AVAILABLE Game ice; an occupied / blocked
                            # / non-Game slot there is a conflict, never silently
                            # counted as a duplicate (#158 review).
                            if exact is not None \
                                    and self._slot_compatible_duplicate(exact):
                                dup += 1
                                continue
                            if exact is not None or any(
                                    intervals_overlap(start, end, e.start_time,
                                                      e.end_time)
                                    for e in existing):
                                conflict += 1
                                continue
                            slot = IceSlot(
                                id=self.store.next_id("slot"), rink_id=rink.id,
                                start_time=start, end_time=end,
                                slot_type=IceSlotType.GAME,
                                status=IceSlotStatus.AVAILABLE)
                            self.store.add_ice_slot(slot)
                            existing.append(slot)  # a later window/rink sees it too
                            self._audit(
                                "ice_slot_created", "ice_slot", slot.id, actor_id,
                                {"rink_id": rink.id, "slot_type": "game",
                                 "ice_availability_batch_id": batch_id})
                            created.append(slot)
                    counts = {
                        "created": len(created), "duplicate_skipped": dup,
                        "conflict_skipped": conflict,
                        "access_skipped_rinks": access_skipped}
                    self._audit(
                        "ice_availability_committed", "ice_availability_batch",
                        batch_id, actor_id, {
                            "season_id": season_id,
                            # Same fingerprint the preview audited, so the two
                            # halves of the flow correlate (#158 review).
                            "template_fingerprint": base["fingerprint"],
                            "rink_ids": [r.id for r, _v in accessible],
                            "weekdays": sorted(base["weekday_set"]),
                            "windows": base["windows_meta"],
                            "playable_minutes": playable_minutes,
                            "turnover_minutes": turnover_minutes, **counts})
                break  # committed cleanly
            except _SeasonReparented:
                # The Season was moved to a different Program between the pre-lock
                # program read and the Season lock; the batch rolled back. Retry —
                # the next pass reads the new program_id and locks that Program
                # first, in canonical order. Converges immediately (the Season
                # lock now pins the relationship), bounded by the loop.
                if attempt == 2:
                    raise ConcurrencyConflictError(
                        "The Season was moved to a different Program while "
                        "committing ice availability; please retry.",
                        {"reason": "season_reparented", "season_id": season_id})
            except IntegrityConflictError:
                # A lock-free writer landed a slot our snapshot missed; the batch
                # rolled back. Retry — the fresh classification will see it and
                # treat it as a duplicate (a retry can only turn 'new' into
                # 'duplicate', so it converges).
                if attempt == 2:
                    raise
        return {
            "committed": True, "batch_id": batch_id,
            "created": [self._ice_slot_dto(s) for s in created],
            "totals": {**counts, "capacity_games": counts["created"]},
        }

    # -- shared season-registration guard (#180) ---------------------------
    # A team's participation in a (season, division) is resolved through its
    # SeasonTeamRegistration — the single source of truth — not the legacy
    # Team.division_id. Game creation, moves, publishing, and standings all go
    # through these helpers so the same rule is enforced everywhere; a team may
    # only play in a season+division it is actively registered in.
    def _require_team_registered(self, season_id: str, team_id: str,
                                 division_id: Optional[str], team=None,
                                 *, require_division: bool = True):
        """Raise ``DivisionMismatchError`` unless ``team_id`` has an active,
        league-consistent registration in ``season_id`` — and, when
        ``require_division``, in ``division_id``. A registration is trusted only
        if its Team exists and the Team's permanent league matches the season's
        (#199: rows can be orphaned or cross-league). ``require_division=False``
        (the override path) still demands an active season registration; it
        relaxes only the division match. Returns the registration on success."""
        season = self.store.get_season(season_id) if season_id else None
        resolved = team if team is not None else self.store.get_team(team_id)
        label = resolved.name if resolved is not None else team_id
        reg = team_registration_valid(self.store, season, team_id, division_id,
                                      require_division=require_division)
        if reg is not None:
            return reg
        # No valid registration — surface the precise reason. #283: a team's
        # registration in a Season is found across the Season's LeagueSeasons.
        raw = (next((r for r in self.store.registrations_for_season(season_id)
                     if r.team_id == team_id), None)
               if season is not None else None)
        if raw is None or not raw.active:
            raise DivisionMismatchError(
                f"{label} is not registered in this season.",
                {"reason": "team_not_registered", "team_id": team_id,
                 "season_id": season_id})
        rteam = self.store.get_team(team_id)
        season_league = season.program_id if season is not None else None
        if (rteam is None or not rteam.program_id or not season_league
                or rteam.program_id != season_league):
            raise DivisionMismatchError(
                f"{label}'s registration does not belong to this program.",
                {"reason": "registration_cross_league", "team_id": team_id,
                 "season_id": season_id})
        raise DivisionMismatchError(
            f"{label} is not registered in this division.",
            {"reason": "team_wrong_division", "team_id": team_id,
             "season_id": season_id, "division_id": division_id,
             "registered_division_id": raw.division_id})

    def _team_participates(self, season, team_id: str,
                           division_id: Optional[str],
                           require_division: bool = True) -> bool:
        """Non-raising counterpart of ``_require_team_registered`` — used where
        participation gates a *decision* (e.g. whether to run the cross-league
        ice guard) rather than a hard failure."""
        return team_registration_valid(
            self.store, season, team_id, division_id,
            require_division=require_division) is not None

    def registered_team_ids_in_division(self, division_id: str,
                                        enforce_team_league: bool = True) -> set:
        """Team ids validly registered in ``division_id`` — the division's
        membership/standings roster. Delegates to the shared resolver so it
        excludes orphaned/cross-league rows exactly as draft generation does.

        ``enforce_team_league`` (default ``True``) keeps the live-scheduling
        rule that a Team must currently belong to the Division's League; pass
        ``False`` for an ENDED Season's historical standings so a validly
        transferred Team is still counted (#283 rule 10)."""
        return _registered_team_ids(self.store, division_id,
                                    enforce_team_league=enforce_team_league)

    # -- manual game creation ---------------------------------------------
    def _assert_slot_free(self, ice_slot_id, *, exclude_game_id=None):
        """Shared physical-placement checker for putting a game on ice (#277).

        THE single conflict choke point that create_game, move_game, AND the
        draft-commit path all route through, so a committed draft occupies its ice
        under exactly the same physical + policy rules as a manual placement.
        Checks that the slot exists, is a GAME slot, is AVAILABLE, and is not
        already used by another active game. Returns the resolved slot on success;
        raises a structured error carrying ``details["reason"]`` (``slot_missing``
        / ``not_game_slot`` / ``slot_unavailable`` / ``slot_already_filled``, the
        machine-readable codes the move panel and draft review consume) otherwise.
        ``exclude_game_id`` is the game being moved — excluded from the slot-in-use
        check so a move never conflicts with itself.

        The #277 turnover-buffer and curfew POLICIES layer onto THIS method (the
        shared checker) in the policy slice, so they apply to draft commits just
        as they do to manual placement — NOT onto
        :meth:`_assert_slot_free_for_game`, which adds only the team-overlap scan.

        Team double-booking is the ONE deliberate exception (recorded here as the
        product decision for #277 / #314): it is layered on for manual create/move
        via :meth:`_assert_slot_free_for_game`, but a draft's team overlaps are
        surfaced in review (``list_draft_games`` issues), NOT rejected at commit —
        a committed draft is provisional, and re-drafting teams that already hold
        fixtures is a normal review-then-resolve step (the demo seed does exactly
        that). Draft commit therefore stops at this physical + policy check by
        design; every other rule stays shared.

        Read-only (no transaction of its own) — callers run inside theirs.
        """
        slot = self.store.get_ice_slot(ice_slot_id)
        if slot is None:
            raise NotFoundError(f"Ice slot {ice_slot_id} not found.",
                                details={"reason": "slot_missing"})
        if slot.slot_type != IceSlotType.GAME:
            raise ValidationError(
                "Only game ice slots can host a game (not maintenance / "
                "public skate / practice / tournament).",
                details={"reason": "not_game_slot",
                         "slot_type": slot.slot_type.value})
        if slot.status != IceSlotStatus.AVAILABLE:
            raise ScheduleConflictError(
                f"Ice slot {ice_slot_id} is not available.",
                details={"reason": "slot_unavailable",
                         "slot_status": slot.status.value})
        clash = self.store.game_using_ice_slot(ice_slot_id)
        if clash is not None and clash.id != exclude_game_id:
            raise ScheduleConflictError(
                f"Ice slot {ice_slot_id} is already used by game {clash.id}.",
                details={"reason": "slot_already_filled",
                         "conflict_game_id": clash.id})
        return slot

    def _assert_slot_free_for_game(self, ice_slot_id, home_team_id, away_team_id,
                                   *, exclude_game_id=None):
        """Manual-placement check: the shared physical checker PLUS team overlap (#277).

        create_game and move_game route through this — it runs the shared physical
        + policy checker (:meth:`_assert_slot_free`, which is where the #277
        turnover/curfew policies live) AND additionally rejects placing either
        team on an overlapping fixture. Returns the resolved slot on success;
        raises a structured error carrying ``details["reason"]`` (adds
        ``team_overlap`` to the shared checker's codes) otherwise.
        ``exclude_game_id`` is the game being moved — excluded from the slot-in-use
        and team-overlap checks so a move never conflicts with itself.

        The team-overlap scan is the ONE check the draft-commit path deliberately
        does not run (see :meth:`_assert_slot_free` for the recorded product
        decision); it is manual-placement-only, so committed drafts surface team
        conflicts in review instead of failing the commit. Turnover and curfew do
        NOT live here — they layer onto the shared checker so drafts get them too.

        Read-only (no transaction of its own) — callers run inside theirs.
        """
        slot = self._assert_slot_free(ice_slot_id, exclude_game_id=exclude_game_id)
        for ex in self.store.all_games():
            if ex.id == exclude_game_id or ex.cancelled or ex.ice_slot_id is None:
                continue
            ex_slot = self.store.get_ice_slot(ex.ice_slot_id)
            if ex_slot is None:
                continue
            overlaps = intervals_overlap(slot.start_time, slot.end_time,
                                         ex_slot.start_time, ex_slot.end_time)
            same_team = (ex.home_team_id in (home_team_id, away_team_id)
                         or ex.away_team_id in (home_team_id, away_team_id))
            if overlaps and same_team:
                raise ScheduleConflictError(
                    f"A team already has an overlapping game {ex.id}.",
                    details={"reason": "team_overlap", "conflict_game_id": ex.id})
        return slot

    @_transactional
    def create_game(self, season_id: str, division_id: str, home_team_id: str,
                    away_team_id: str, ice_slot_id: str,
                    target_goalies: int = 1, target_skaters: int = 15,
                    max_skaters: int = 18, allow_division_override: bool = False,
                    actor_id: Optional[str] = None,
                    league_id: Optional[str] = None,
                    game_type: str = GameType.REGULAR.value) -> Game:
        season = self._require_active_season(season_id)  # #159 read-only guard

        # #283 Slice D: a Game is REGULAR (counts toward standings, bound to one
        # LeagueSeason) or EXHIBITION (a friendly that may cross League lines and
        # never affects standings). An unknown kind is a stable validation error.
        game_type = (game_type or GameType.REGULAR.value)
        if game_type not in (GameType.REGULAR.value, GameType.EXHIBITION.value):
            raise ValidationError(
                "Unknown game type.",
                {"reason": "unknown_game_type", "game_type": game_type})
        is_exhibition = game_type == GameType.EXHIBITION.value

        if is_exhibition:
            # A friendly: both teams must be real, active participants in THIS
            # Season (so rosters and venue eligibility resolve), but they MAY
            # belong to different Leagues within the Season. It carries no owning
            # League and no Division, and never counts toward standings — so
            # rules 8/9 (same-League match) are deliberately relaxed here.
            scoped_league_id = None
            division_id = None
        else:
            # v2 competition scope (#233 Slice C2): when a ``league_id`` is
            # supplied it is REQUIRED-and-validated against the Season, and
            # ``division_id`` becomes OPTIONAL. v1 (league_id=None) is unchanged
            # — division_id stays mandatory and the game's league is derived from
            # the division below.
            scoped_league_id = league_id or None
            if scoped_league_id:
                league = self.store.get_league(scoped_league_id)
                if league is None:
                    raise NotFoundError(f"League {scoped_league_id} not found.")
                # #283: a League is permanent; it "belongs" to a Season only via
                # a LeagueSeason. Require that participation to exist.
                if self.store.league_season_for(scoped_league_id,
                                                season_id) is None:
                    raise ValidationError(
                        "League belongs to a different season than the game.")

            division = None
            if division_id:
                division = self.store.get_division(division_id)
                if division is None:
                    raise NotFoundError(f"Division {division_id} not found.")
                # #283: a Division's Season and League resolve via LeagueSeason.
                div_ls = self.store.get_league_season(division.league_season_id)
                div_season_id = div_ls.season_id if div_ls else None
                div_league_id = div_ls.league_id if div_ls else None
                if div_season_id != season_id:
                    raise ValidationError(
                        "Division does not belong to the given season."
                    )
                if scoped_league_id and div_league_id != scoped_league_id:
                    raise ValidationError(
                        "Division belongs to a different league than the game.")
                if not scoped_league_id:
                    scoped_league_id = div_league_id
            elif not scoped_league_id:
                # v1 path: a division is mandatory — preserve the legacy error.
                raise NotFoundError(f"Division {division_id} not found.")

        if home_team_id == away_team_id:
            raise ValidationError("A team cannot play itself.")

        home = self.store.get_team(home_team_id)
        away = self.store.get_team(away_team_id)
        if home is None:
            raise NotFoundError(f"Team {home_team_id} not found.")
        if away is None:
            raise NotFoundError(f"Team {away_team_id} not found.")

        # Participation is resolved through SeasonTeamRegistration (#180), not
        # the legacy Team.division_id: both teams must have an active, league-
        # consistent registration in this season AND division. Cross-division
        # games are deprecated until an explicit competition mode exists (#200
        # review): allow_division_override no longer relaxes the division match,
        # so every game's teams are co-registered and it can be moved,
        # rescheduled, and published without a dead-end. The flag is retained
        # (and audited below) for API compatibility. When v2 omits a division,
        # the registration check relaxes to season-only (require_division=False).
        require_division = division_id is not None
        home_reg = self._require_team_registered(
            season_id, home_team_id, division_id, home,
            require_division=require_division)
        away_reg = self._require_team_registered(
            season_id, away_team_id, division_id, away,
            require_division=require_division)

        # Rules 8/9 (#283 Slice D): a REGULAR game is tied to its teams'
        # registration League — both teams' active registrations must be in the
        # game's exact grouping League, so cross-League and cross-Program
        # pairings are rejected (a League belongs to one Program, so same-League
        # implies same-Program). This now fires on BOTH the v2 path (explicit
        # league_id) and the v1 path (league derived from the division): in both
        # cases ``scoped_league_id`` is resolved by here. EXHIBITION games skip
        # it — a friendly may cross League lines. Runs before any slot
        # allocation, so a mismatch mutates nothing. (Division consistency +
        # division→league agreement were already checked above.)
        if not is_exhibition and scoped_league_id is not None:
            for team, reg in ((home, home_reg), (away, away_reg)):
                # #283: a registration's League is resolved via its LeagueSeason.
                reg_league_id = self._registration_league_id(reg)
                if reg_league_id != scoped_league_id:
                    label = team.name if team is not None else "Team"
                    raise ValidationError(
                        f"{label}'s registration belongs to a different league "
                        "than this game.",
                        {"reason": "registration_wrong_league",
                         "team_id": team.id if team is not None else None,
                         "season_id": season_id,
                         "expected_league_id": scoped_league_id,
                         "registered_league_id": reg_league_id})

        # Shared final conflict check (#277): create/move/draft-commit all route
        # through the one checker so they enforce identical slot + team-overlap
        # rules (and, in the policy slice, turnover + curfew).
        slot = self._assert_slot_free_for_game(
            ice_slot_id, home_team_id, away_team_id)

        rink = self.store.get_rink(slot.rink_id)
        # #283 Slice E: a REGULAR game references its exact LeagueSeason (its
        # single competition identity); an EXHIBITION has none. scoped_league_id
        # + season_id already resolved to a real LeagueSeason above for regular
        # games, so this lookup always succeeds there.
        league_season_id = None
        if not is_exhibition and scoped_league_id is not None:
            ls = self.store.league_season_for(scoped_league_id, season_id)
            league_season_id = ls.id if ls else None
        game = Game(
            id=self.store.next_id("game"),
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            start_time=slot.start_time,
            end_time=slot.end_time,
            rink=rink.name if rink else None,
            target_goalies=target_goalies,
            target_skaters=target_skaters,
            max_skaters=max_skaters,
            season_id=season_id,
            division_id=division_id or None,
            ice_slot_id=ice_slot_id,
            league_id=scoped_league_id,
            game_type=game_type,
            league_season_id=league_season_id,
        )
        self.store.add_game(game)
        # Mark the slot allocated so it reads as taken across the arena.
        slot.status = IceSlotStatus.ALLOCATED
        self.store.save_ice_slot(slot)
        self._audit("game_created", "game", game.id, actor_id, {
            "season_id": season_id, "division_id": division_id,
            "home_team_id": home_team_id, "away_team_id": away_team_id,
            "ice_slot_id": ice_slot_id, "override": allow_division_override,
            "game_type": game_type,
        })
        return game

    @_transactional
    def publish_game(self, game_id: str, published: bool = True,
                     actor_id: Optional[str] = None) -> Game:
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.")
        self._guard_game_season(game)  # #159 read-only guard
        # #159 r15 — re-fetch under the Season lock (the pre-lock read was a
        # locator). A concurrent move_game relocates the game (new slot/time,
        # unpublished) under the same Season lock; publishing the stale object
        # would clobber those fields back. Act on the fresh row.
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.")
        # A game may only be made public while both teams are still valid
        # participants of its competition scope (#180 / #283 Slice E: exact
        # LeagueSeason for a regular game, active Season participation for an
        # exhibition). Unpublishing is unguarded so an invalid fixture can
        # always be pulled back from public view.
        if published:
            self._revalidate_game_participation(game)
        was_published = game.published
        game.published = published
        self.store.save_game(game)
        self._audit("game_published" if published else "game_unpublished",
                    "game", game_id, actor_id)
        # Notify affected parties + public only on the false→true transition,
        # so re-publishing an already-public game is a no-op (#87 idempotency).
        if published and not was_published:
            label = self._game_label(game)
            self._notify_game_change(
                game, NotificationKind.GAME_PUBLISHED, "Game published",
                f"{label} has been published.", include_public=True)
        return game

    @_transactional
    def move_game(self, game_id: str, new_ice_slot_id: str, reason: str = "",
                  actor_id: Optional[str] = None) -> Game:
        """Move a game to another available game ice slot (drag/drop)."""
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.",
                                details={"reason": "game_missing"})
        self._guard_game_season(game)  # #159 read-only guard
        # #159 r15 — re-fetch under the Season lock (the pre-lock read was a
        # locator). A concurrent move_game/publish_game commits under the same
        # Season lock; acting on the stale object would release the WRONG old
        # slot and clobber the game's current slot/time/published state.
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.",
                                details={"reason": "game_missing"})
        if game.cancelled:
            raise ValidationError("Cannot move a cancelled game.",
                                  details={"reason": "game_cancelled"})
        # A move can't revive a fixture whose participation has since become
        # invalid: both teams must still be valid participants of the game's
        # competition scope (#180 / #283 Slice E — exact LeagueSeason for a
        # regular game, active Season participation for an exhibition).
        self._revalidate_game_participation(game)

        new_slot = self.store.get_ice_slot(new_ice_slot_id)
        if new_slot is None:
            raise NotFoundError(f"Ice slot {new_ice_slot_id} not found.",
                                details={"reason": "slot_missing"})
        if new_slot.id == game.ice_slot_id:
            raise ValidationError("Game is already in that ice slot.",
                                  details={"reason": "same_slot"})
        # Shared final conflict check (#277) — identical rules to create +
        # draft-commit; excludes THIS game so a move never conflicts with itself.
        new_slot = self._assert_slot_free_for_game(
            new_ice_slot_id, game.home_team_id, game.away_team_id,
            exclude_game_id=game_id)

        old_slot_id = game.ice_slot_id
        if old_slot_id:
            old_slot = self.store.get_ice_slot(old_slot_id)
            if old_slot is not None:
                old_slot.status = IceSlotStatus.AVAILABLE
                self.store.save_ice_slot(old_slot)
        new_slot.status = IceSlotStatus.ALLOCATED
        self.store.save_ice_slot(new_slot)

        rink = self.store.get_rink(new_slot.rink_id)
        was_published = game.published
        was_locked = game.locked
        game.ice_slot_id = new_slot.id
        game.start_time = new_slot.start_time
        game.end_time = new_slot.end_time
        game.rink = rink.name if rink else None
        if was_published:
            # The fixture changed — it must be re-published before going public.
            game.published = False
        if was_locked:
            # Time/rink changed — players must reconfirm, so unlock the roster.
            game.locked = False
        self.store.save_game(game)
        self._audit("game_moved", "game", game_id, actor_id, {
            "old_slot_id": old_slot_id, "new_slot_id": new_slot.id,
            "reason": reason, "unpublished": was_published,
            "roster_unlocked": was_locked,
        })
        when = game.start_time.isoformat() if game.start_time else "a new time"
        self._notify_game_change(
            game, NotificationKind.GAME_MOVED, "Game moved",
            f"{self._game_label(game)} moved to {game.rink or 'a new rink'} "
            f"({when}).{(' ' + reason) if reason else ''}")
        return game

    # -- reschedule request / approval workflow (#29) -----------------------
    # A controlled, coach-initiated way to move a PUBLISHED game — distinct
    # from move_game above (an operator-only drag/drop). The opponent team
    # must accept before it ever reaches league review; an approval reuses
    # move_game/publish_game verbatim for the actual slot swap, so it
    # inherits the same one-game-per-slot / team-overlap guarantees rather
    # than re-implementing them.
    _OPEN_RESCHEDULE_STATUSES = {
        RescheduleStatus.PENDING_OPPONENT, RescheduleStatus.PENDING_LEAGUE_APPROVAL}

    @_transactional
    def request_reschedule(self, game_id: str, team_id: str, reason: str,
                           actor_id: Optional[str] = None) -> RescheduleRequest:
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.")
        self._guard_game_season(game)  # #159 read-only guard
        # #159 r15 — re-fetch under the Season lock; a concurrent move_game may
        # have unpublished/relocated the game after the locator read.
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.")
        if game.cancelled:
            raise ValidationError("Cannot request a reschedule for a cancelled game.")
        if not game.published:
            raise ValidationError(
                "Only a published game can have a reschedule requested.")
        if team_id not in (game.home_team_id, game.away_team_id):
            raise ValidationError(
                "Only the game's own two teams may request a reschedule.")
        reason = self._require_name(reason, "reason")
        if any(r.status in self._OPEN_RESCHEDULE_STATUSES
              for r in self.store.reschedule_requests_for_game(game_id)):
            raise ScheduleConflictError(
                "This game already has an open reschedule request.",
                details={"reason": "reschedule_already_open"})
        req = RescheduleRequest(
            id=self.store.next_id("reschedule"), game_id=game_id,
            requested_by_team_id=team_id, reason=reason,
            status=RescheduleStatus.PENDING_OPPONENT, created_at=self.clock())
        self.store.add_reschedule_request(req)
        self._audit("reschedule_requested", "game", game_id, actor_id, {
            "request_id": req.id, "requested_by_team_id": team_id, "reason": reason})
        opponent_id = (game.away_team_id if team_id == game.home_team_id
                      else game.home_team_id)
        if opponent_id:
            self._notify(
                NotificationKind.RESCHEDULE_REQUESTED, NotificationAudience.COACH,
                "Reschedule requested",
                f"{self._matchup(game)}: a reschedule was requested ({reason}). "
                f"Please respond.", audience_ref=opponent_id, game_id=game_id)
        return req

    @_transactional
    def respond_to_reschedule(self, request_id: str, accept: bool,
                              actor_id: Optional[str] = None) -> RescheduleRequest:
        req = self.store.get_reschedule_request(request_id)
        if req is None:
            raise NotFoundError(f"Reschedule request {request_id} not found.")
        if req.status != RescheduleStatus.PENDING_OPPONENT:
            raise InvalidTransitionError(
                "This reschedule request is not awaiting an opponent response.",
                details={"status": req.status.value})
        game = self.store.get_game(req.game_id)
        self._guard_game_season(game)  # #159 read-only guard
        # #159 r15 — re-fetch the request under the Season lock and re-check its
        # status: a concurrent responder/unassign committed first would make this
        # a stale double-transition otherwise.
        req = self.store.get_reschedule_request(request_id)
        if req is None:
            raise NotFoundError(f"Reschedule request {request_id} not found.")
        if req.status != RescheduleStatus.PENDING_OPPONENT:
            raise InvalidTransitionError(
                "This reschedule request is not awaiting an opponent response.",
                details={"status": req.status.value})
        req.status = (RescheduleStatus.PENDING_LEAGUE_APPROVAL if accept
                     else RescheduleStatus.OPPONENT_REJECTED)
        req.opponent_responded_at = self.clock()
        self.store.save_reschedule_request(req)
        self._audit("reschedule_accepted" if accept else "reschedule_rejected",
                   "game", req.game_id, actor_id, {"request_id": req.id})
        if accept:
            self._notify(
                NotificationKind.RESCHEDULE_ACCEPTED, NotificationAudience.SCHEDULER,
                "Reschedule awaiting approval",
                f"{self._matchup(game)}: the opponent accepted a reschedule "
                f"request ({req.reason}). League approval needed.",
                game_id=req.game_id)
        else:
            self._notify(
                NotificationKind.RESCHEDULE_REJECTED, NotificationAudience.COACH,
                "Reschedule rejected",
                f"{self._matchup(game)}: the opponent rejected the reschedule "
                f"request ({req.reason}).",
                audience_ref=req.requested_by_team_id, game_id=req.game_id)
        return req

    def decide_reschedule(self, request_id: str, approve: bool,
                          new_ice_slot_id: Optional[str] = None,
                          note: Optional[str] = None,
                          actor_id: Optional[str] = None) -> RescheduleRequest:
        """NOT @_transactional: the approve path calls move_game/publish_game,
        which already are — nested transaction() calls are not reentrant on
        SqlStore (matches copy_previous_roster's call to select_roster)."""
        req = self.store.get_reschedule_request(request_id)
        if req is None:
            raise NotFoundError(f"Reschedule request {request_id} not found.")
        if req.status != RescheduleStatus.PENDING_LEAGUE_APPROVAL:
            raise InvalidTransitionError(
                "This reschedule request is not awaiting league approval.",
                details={"status": req.status.value})
        self._guard_game_season(self.store.get_game(req.game_id))  # #159
        if not approve:
            req.status = RescheduleStatus.DENIED
            req.decision_note = note
            req.league_decided_at = self.clock()
            self.store.save_reschedule_request(req)
            self._audit("reschedule_denied", "game", req.game_id, actor_id,
                       {"request_id": req.id, "note": note})
            game = self.store.get_game(req.game_id)
            self._notify_game_change(
                game, NotificationKind.RESCHEDULE_DENIED, "Reschedule denied",
                f"{self._matchup(game)}: the reschedule request was denied."
                f"{(' ' + note) if note else ''}")
            return req
        if not new_ice_slot_id:
            raise ValidationError(
                "A replacement ice slot is required to approve a reschedule.")
        # This workflow's whole point is that by this step both the opponent
        # and league have signed off, so — unlike a raw move_game drag/drop,
        # which conservatively unpublishes pending human review — republish
        # immediately rather than leaving it in limbo.
        #
        # move_game/publish_game are each their own committed transaction (not
        # one atomic unit with the RescheduleRequest save below), so a crash
        # between them could otherwise leave the game moved-but-unpublished
        # with this request stuck PENDING_LEAGUE_APPROVAL forever — retrying
        # with the same slot would hit move_game's own "already in that slot"
        # guard before ever reaching the request update. Skip a step that's
        # already done instead of re-running it, so re-approving (the natural
        # recovery action) always completes the request rather than erroring.
        game = self.store.get_game(req.game_id)
        if game is None or game.ice_slot_id != new_ice_slot_id:
            self.move_game(req.game_id, new_ice_slot_id,
                           reason=f"Reschedule approved: {req.reason}",
                           actor_id=actor_id)
            game = self.store.get_game(req.game_id)
        if not game.published:
            self.publish_game(req.game_id, actor_id=actor_id)
        req.status = RescheduleStatus.REPUBLISHED
        req.new_ice_slot_id = new_ice_slot_id
        req.decision_note = note
        req.league_decided_at = self.clock()
        self.store.save_reschedule_request(req)
        self._audit("reschedule_republished", "game", req.game_id, actor_id,
                   {"request_id": req.id, "new_ice_slot_id": new_ice_slot_id})
        return req

    def list_reschedule_requests(self, game_id: Optional[str] = None
                                 ) -> List[RescheduleRequest]:
        """Pure read helper — must NOT be @_transactional."""
        rows = (self.store.reschedule_requests_for_game(game_id) if game_id
               else self.store.all_reschedule_requests())
        return sorted(rows, key=lambda r: r.created_at, reverse=True)

    # -- hierarchy import upsert helpers (#260 Slice F) ---------------------
    # Narrow, purpose-built upsert methods the hierarchy CSV importer
    # (hierarchy_import.py) calls instead of duplicating create/update
    # invariants inline (#260 review decision 3). The importer resolves each
    # row's external_ref against a `{code: obj}` dict it pre-builds once per
    # sheet (a read-only lookup, not a write) and passes the resolved
    # `existing` object (or None) in; every actual store write and audit
    # entry happens here. None of these carry their own @_transactional —
    # they run inside the importer's single outer `with store.transaction():`
    # block (store.transaction() is reentrant, so nesting is safe), which is
    # what makes the whole nine-sheet batch one atomic commit/rollback unit.
    #
    # Every helper returns (obj, created, changed_fields) so the caller can
    # track created/updated/skipped counts without re-deriving them.

    @staticmethod
    def _apply_changes(obj, values: dict) -> List[str]:
        changed = []
        for field, value in values.items():
            if getattr(obj, field) != value:
                setattr(obj, field, value)
                changed.append(field)
        return changed

    def upsert_imported_organization(self, code: str, name: str, short_name: str,
                                     existing=None, actor_id: Optional[str] = None,
                                     import_batch_id: Optional[str] = None):
        values = {"name": name, "short_name": short_name}
        if existing is None:
            obj = Organization(id=self.store.next_id("org"), external_ref=code,
                               **values)
            self.store.add_organization(obj)
            self._audit("organization_created", "organization", obj.id, actor_id,
                       {"import_batch_id": import_batch_id, "external_ref": code})
            return obj, True, []
        changed = self._apply_changes(existing, values)
        if changed:
            self.store.save_organization(existing)
            self._audit("organization_updated", "organization", existing.id,
                       actor_id, {"import_batch_id": import_batch_id,
                                  "external_ref": code, "changed_fields": changed})
        return existing, False, changed

    def upsert_imported_program(self, code: str, name: str, country: str,
                                timezone_name: str,
                                operator_organization_id: Optional[str],
                                existing=None, actor_id: Optional[str] = None,
                                import_batch_id: Optional[str] = None):
        values = {"name": name, "country": country,
                  "timezone": timezone_name or "UTC",
                  "operator_organization_id": operator_organization_id}
        if existing is None:
            obj = Program(id=self.store.next_id("program"), external_ref=code,
                          **values)
            self.store.add_program(obj)
            self._audit("program_created", "program", obj.id, actor_id,
                       {"import_batch_id": import_batch_id, "external_ref": code,
                        "operator_organization_id": operator_organization_id})
            return obj, True, []
        changed = self._apply_changes(existing, values)
        if changed:
            self.store.save_program(existing)
            self._audit("program_updated", "program", existing.id, actor_id,
                       {"import_batch_id": import_batch_id, "external_ref": code,
                        "operator_organization_id": operator_organization_id,
                        "changed_fields": changed})
        return existing, False, changed

    def upsert_imported_venue(self, code: str, name: str, address: str,
                              timezone_name: str, organization_id: str,
                              existing=None, actor_id: Optional[str] = None,
                              import_batch_id: Optional[str] = None):
        # Never sets league_id (#233 Slice E, #260 review decision) — the
        # legacy one-Venue-one-Program bridge is retired; a Venue's
        # schedulability comes only from SeasonVenueAccess.
        values = {"name": name, "address": address,
                  "timezone": timezone_name or "UTC",
                  "organization_id": organization_id}
        if existing is None:
            obj = Venue(id=self.store.next_id("venue"), external_ref=code,
                       **values)
            self.store.add_venue(obj)
            self._audit("venue_created", "venue", obj.id, actor_id,
                       {"import_batch_id": import_batch_id, "external_ref": code,
                        "organization_id": organization_id})
            return obj, True, []
        changed = self._apply_changes(existing, values)
        if changed:
            self.store.save_venue(existing)
            self._audit("venue_updated", "venue", existing.id, actor_id,
                       {"import_batch_id": import_batch_id, "external_ref": code,
                        "organization_id": organization_id,
                        "changed_fields": changed})
        return existing, False, changed

    def upsert_imported_rink(self, code: str, name: str, venue_id: str,
                             existing=None, actor_id: Optional[str] = None,
                             import_batch_id: Optional[str] = None):
        values = {"name": name, "venue_id": venue_id}
        if existing is None:
            obj = Rink(id=self.store.next_id("rink"), external_ref=code, **values)
            self.store.add_rink(obj)
            self._audit("rink_created", "rink", obj.id, actor_id,
                       {"import_batch_id": import_batch_id, "external_ref": code,
                        "venue_id": venue_id})
            return obj, True, []
        changed = self._apply_changes(existing, values)
        if changed:
            self.store.save_rink(existing)
            self._audit("rink_updated", "rink", existing.id, actor_id,
                       {"import_batch_id": import_batch_id, "external_ref": code,
                        "venue_id": venue_id, "changed_fields": changed})
        return existing, False, changed

    def upsert_imported_season(self, code: str, name: str, program_id: str,
                               start_date=None, end_date=None,
                               existing=None, actor_id: Optional[str] = None,
                               import_batch_id: Optional[str] = None):
        # start_date/end_date are ALREADY-PARSED timezone-aware UTC datetimes (or
        # None) from the caller's shared parse_season_boundary (#272). On CREATE
        # they set the boundaries (None → unset). On UPDATE a None side is
        # OMITTED from the diff so a blank import cell PRESERVES the stored
        # boundary rather than clearing it; a supplied side that already equals
        # the stored instant is a no-op (_apply_changes skips it → no false
        # update/audit on an equivalent repeat import).
        if existing is not None:
            self._require_active_season(existing.id)  # #159 read-only guard
        if existing is None:
            obj = Season(id=self.store.next_id("season"), external_ref=code,
                         name=name, program_id=program_id,
                         start_date=start_date, end_date=end_date)
            self.store.add_season(obj)
            self._audit("season_created", "season", obj.id, actor_id,
                       {"import_batch_id": import_batch_id, "external_ref": code,
                        "program_id": program_id})
            return obj, True, []
        values = {"name": name, "program_id": program_id}
        if start_date is not None:
            values["start_date"] = start_date
        if end_date is not None:
            values["end_date"] = end_date
        changed = self._apply_changes(existing, values)
        if changed:
            self.store.save_season(existing)
            self._audit("season_updated", "season", existing.id, actor_id,
                       {"import_batch_id": import_batch_id, "external_ref": code,
                        "program_id": program_id, "changed_fields": changed})
        return existing, False, changed

    def upsert_imported_league(self, code: str, name: str, sort_order: int,
                               season_id: str, existing=None,
                               actor_id: Optional[str] = None,
                               import_batch_id: Optional[str] = None):
        # League is REQUIRED on every canonical competition row (#260 review
        # decision 2), unlike the old optional level_code/level_name.
        # #283: a League is a permanent child of the Season's Program; its
        # participation in the Season is a LeagueSeason, not a season_id column.
        season = self.store.get_season(season_id)
        program_id = season.program_id if season else None
        values = {"name": name, "sort_order": sort_order}
        if existing is None:
            obj = League(id=self.store.next_id("league"), external_ref=code,
                        program_id=program_id, **values)
            self.store.add_league(obj)
            self._link_league_season(obj.id, season_id)
            self._audit("league_created", "league", obj.id, actor_id,
                       {"import_batch_id": import_batch_id, "external_ref": code,
                        "season_id": season_id})
            return obj, True, []
        changed = self._apply_changes(existing, values)
        # Ensure the permanent League participates in this Season (idempotent).
        self._link_league_season(existing.id, season_id)
        if changed:
            self.store.save_league(existing)
            self._audit("league_updated", "league", existing.id, actor_id,
                       {"import_batch_id": import_batch_id, "external_ref": code,
                        "season_id": season_id, "changed_fields": changed})
        return existing, False, changed

    def upsert_imported_league_season(self, league_id: str, season_id: str,
                                      actor_id: Optional[str] = None,
                                      import_batch_id: Optional[str] = None):
        """Ensure the permanent League participates in the Season (idempotent).

        #283: a League may participate in MULTIPLE Seasons via LeagueSeason. The
        import must bind EVERY (League, Season) pair the sheet declares — even a
        Season row that carries no Division and no registration, which the
        Division/registration upserts would otherwise never reach. Returns
        ``(league_season, created)``; a repeat import finds the existing binding
        and is a no-op (no duplicate, no audit)."""
        existing = self.store.league_season_for(league_id, season_id)
        if existing is not None:
            return existing, False
        self._require_active_season(season_id)  # #159 read-only guard
        ls = self._link_league_season(league_id, season_id)
        self._audit("league_season_created", "league_season", ls.id, actor_id,
                    {"import_batch_id": import_batch_id, "league_id": league_id,
                     "season_id": season_id})
        return ls, True

    def upsert_imported_division(self, code: str, name: str, age_group: str,
                                 season_id: str, league_id: str, existing=None,
                                 actor_id: Optional[str] = None,
                                 import_batch_id: Optional[str] = None):
        # Optional per row (#260) — the importer only calls this when a row
        # actually carries a division_code.
        # #283: a Division belongs to a LeagueSeason (the League's participation
        # in the Season), resolved/created from (league_id, season_id).
        self._require_active_season(season_id)  # #159 read-only guard
        ls = self._link_league_season(league_id, season_id)
        values = {"name": name, "age_group": age_group,
                  "league_season_id": ls.id}
        if existing is None:
            obj = Division(id=self.store.next_id("division"), external_ref=code,
                          **values)
            self.store.add_division(obj)
            self._audit("division_created", "division", obj.id, actor_id,
                       {"import_batch_id": import_batch_id, "external_ref": code,
                        "season_id": season_id, "league_id": league_id})
            return obj, True, []
        changed = self._apply_changes(existing, values)
        if changed:
            self.store.save_division(existing)
            self._audit("division_updated", "division", existing.id, actor_id,
                       {"import_batch_id": import_batch_id, "external_ref": code,
                        "season_id": season_id, "league_id": league_id,
                        "changed_fields": changed})
        return existing, False, changed

    def upsert_imported_club(self, code: str, name: str, country: str,
                             existing=None, actor_id: Optional[str] = None,
                             import_batch_id: Optional[str] = None):
        # Matched by club_code, never by name (#260 review decision 1) — a
        # renamed Club (same code, new club_name) updates the existing
        # record in place rather than creating a second one.
        values = {"name": name, "country": country}
        if existing is None:
            obj = Club(id=self.store.next_id("club"), external_ref=code, **values)
            self.store.add_club(obj)
            self._audit("club_created", "club", obj.id, actor_id,
                       {"import_batch_id": import_batch_id, "external_ref": code})
            return obj, True, []
        changed = self._apply_changes(existing, values)
        if changed:
            self.store.save_club(existing)
            self._audit("club_updated", "club", existing.id, actor_id,
                       {"import_batch_id": import_batch_id, "external_ref": code,
                        "changed_fields": changed})
        return existing, False, changed

    def upsert_imported_team(self, code: str, name: str, program_id: str,
                             club_id: Optional[str], existing=None,
                             league_id: Optional[str] = None,
                             actor_id: Optional[str] = None,
                             import_batch_id: Optional[str] = None):
        # club_id is always set from the row's resolved club (#260 review
        # decision 1): a blank/NA club_code means club_id=None on BOTH a
        # create and a repeat row — never a placeholder Club, but a genuine
        # unassign on re-import is allowed, mirroring the interactive
        # "— none —" option exactly.
        # #283 Slice E: a permanent Team is bound to its permanent League;
        # league_id is written on create AND on re-import (a promotion/
        # relegation in the sheet updates it in place, mirroring
        # transfer_team_to_league's field change).
        if existing is None:
            obj = Team(id=self.store.next_id("team"), external_ref=code,
                       name=name, program_id=program_id, club_id=club_id,
                       league_id=league_id)
            self.store.add_team(obj)
            self._audit("team_created", "team", obj.id, actor_id,
                       {"import_batch_id": import_batch_id, "external_ref": code,
                        "program_id": program_id, "club_id": club_id,
                        "permanent_league_id": league_id})
            return obj, True, []
        # #201 — re-fetch the Team under its ROW LOCK before any field/league
        # write, mirroring the public transfer_team_to_league (which locks the
        # Team first). This serializes the import's writes with a concurrent
        # register_team_for_season / transfer_team_to_league (both lock the same
        # Team row), so an inactive old-League registration can't be reactivated
        # between the transfer's candidate scan and the league change — which
        # would otherwise leave an active registration in the old League while
        # Team.league_id moved. A Team deleted out from under the import fails
        # the whole batch closed rather than writing against a ghost.
        existing = self.store.get_team_for_update(existing.id)
        if existing is None:
            raise NotFoundError("Team no longer exists.")
        old_program_id = existing.program_id
        # #283 Slice E: a permanent-League change on re-import (promotion/
        # relegation in the sheet) must go through the SAME lifecycle guards as
        # transfer_team_to_league — never a raw field write — so it can't strand
        # committed games or rewrite completed-Season history. Apply the other
        # fields first, then route the league change through the shared inner
        # transfer (which raises to abort the whole import if it would strand
        # games, and moves eligible current/future game-free registrations).
        league_changed = (league_id is not None
                          and existing.league_id != league_id)
        changed = self._apply_changes(
            existing, {"name": name, "program_id": program_id, "club_id": club_id})
        if changed:
            self.store.save_team(existing)
            detail = {"import_batch_id": import_batch_id, "external_ref": code,
                      "program_id": program_id, "changed_fields": changed}
            if "program_id" in changed:  # exact from/to on a program move
                detail["from_program_id"] = old_program_id
                detail["to_program_id"] = program_id
            self._audit("team_updated", "team", existing.id, actor_id, detail)
        if league_changed:
            self._transfer_team_to_league_inner(existing, league_id, actor_id)
            changed = list(changed) + ["league_id"]
        return existing, False, changed

    def upsert_imported_player(self, code: str, name: str, team_id: str,
                               position: Position, jersey_number: Optional[int],
                               email: Optional[str], existing=None,
                               actor_id: Optional[str] = None,
                               import_batch_id: Optional[str] = None,
                               staged_original_jersey=_UNSET):
        """Upsert a Player by its stable player_code (#260), syncing an
        optional email the same way ``add_player`` does: an existing
        ``player:<id>`` ContactDestination's value is updated in place,
        never duplicated. Omitting the email on a repeat row leaves a
        previously-set contact untouched — clearing/retiring a contact is
        #232's own explicit, audited action, never an import side effect.
        """
        self._validate_jersey_number(jersey_number)
        # Validate/canonicalize the email BEFORE any player write (#268 review):
        # a non-string/non-None value (False, 0, a list) or a malformed string
        # raises a field-level invalid_email here, so the method never applies a
        # partial player change even when a direct caller supplies no outer
        # transaction. None/blank canonicalizes to None -> a no-op below (the
        # import rule: an absent cell is "leave as-is", never a retirement).
        canonical_email = self._validate_email(email)
        canonical_name = self._validate_player_name(name)
        # Enforce active-team jersey uniqueness on the IMPORTED target state
        # before any write (#269), so a conflicting row aborts the whole
        # one-transaction batch with zero committed players. An import never
        # toggles is_active, so an updated player keeps its current active
        # state; only an active target reserves a number, and it excludes
        # itself so re-importing the same row is a no-op, not a self-collision.
        target_active = True if existing is None else existing.is_active
        if target_active:
            self._assert_jersey_available(
                team_id, jersey_number,
                exclude_player_id=None if existing is None else existing.id)
        values = {"name": canonical_name, "team_id": team_id,
                  "position": position, "jersey_number": jersey_number}
        if existing is None:
            obj = Player(id=self.store.next_id("player"), external_ref=code,
                        **values)
            self.store.add_player(obj)
            self._audit("player_created", "player", obj.id, actor_id,
                       {"import_batch_id": import_batch_id, "external_ref": code,
                        "team_id": team_id})
            created, changed = True, []
        else:
            obj = existing
            # If a swap-safe pre-pass released this row's jersey to NULL (#292),
            # restore its real pre-staging value BEFORE diffing so the change
            # set reflects the operator's true before→after — a Team-only move
            # that keeps the same number must NOT report a jersey change, and a
            # blank/keep-current cell must land the original, not the NULL.
            if staged_original_jersey is not _UNSET:
                obj.jersey_number = staged_original_jersey
            changed = self._apply_changes(obj, values)
            if changed:
                self.store.save_player(obj)
                self._audit("player_updated", "player", obj.id, actor_id,
                           {"import_batch_id": import_batch_id,
                            "external_ref": code, "team_id": team_id,
                            "changed_fields": changed})
            created = False
        # A supplied nonblank email (already validated/canonicalized above)
        # updates AND reactivates the single player:<id> EMAIL contact via the
        # shared set/retire path — so re-importing an address that a prior edit
        # retired makes the contact active again (was: destination updated but
        # left active=False). An absent/blank cell canonicalized to None, so it
        # stays a no-op that leaves any existing contact untouched (#268 review).
        if canonical_email is not None:
            self._set_email_contact(f"player:{obj.id}", canonical_email)
        return obj, created, changed

    def upsert_imported_registration(self, season_id: str, team_id: str,
                                     league_id: str, division_id: Optional[str],
                                     actor_id: Optional[str] = None,
                                     import_batch_id: Optional[str] = None):
        """Upsert a SeasonTeamRegistration by its (season_id, team_id)
        identity (#260) — it has no external_ref of its own. Mirrors
        ``register_team_for_season``'s Rule 5 reactivation (an inactive
        prior row is reactivated in place, never duplicated), but unlike
        that interactive method — which rejects an already-active row as a
        duplicate — an ACTIVE existing row is simply updated in place:
        re-importing the same or corrected registration data must never
        error.
        """
        self._require_active_season(season_id)  # #159 read-only guard
        # #283: a registration is stored against a LeagueSeason. Resolve (create
        # if needed) the imported League's participation in the Season; a change
        # of League is now a change of the registration's LeagueSeason.
        ls = self._link_league_season(league_id, season_id)
        reg = next((r for r in self.store.registrations_for_season(season_id)
                    if r.team_id == team_id), None)
        if reg is None:
            reg = SeasonTeamRegistration(
                id=self.store.next_id("streg"), league_season_id=ls.id,
                team_id=team_id, division_id=division_id,
                active=True)
            self.store.add_season_team_registration(reg)
            self._audit(
                "season_team_registered", "season_team_registration", reg.id,
                actor_id, {"season_id": season_id, "team_id": team_id,
                          "league_id": league_id, "division_id": division_id,
                          "import_batch_id": import_batch_id})
            return reg, True, []
        changed = []
        old_league_id = self._registration_league_id(reg)
        old_division_id = reg.division_id
        if old_league_id != league_id:
            reg.league_season_id = ls.id
            changed.append("league_id")
        if reg.division_id != division_id:
            reg.division_id = division_id
            changed.append("division_id")
        if not reg.active:
            reg.active = True
            changed.append("active")
        if changed:
            self.store.save_season_team_registration(reg)
            detail = {"season_id": season_id, "team_id": team_id,
                      "league_id": league_id, "division_id": division_id,
                      "changed_fields": changed,
                      "import_batch_id": import_batch_id}
            if "league_id" in changed:  # exact from/to on a move
                detail["from_league_id"] = old_league_id
                detail["to_league_id"] = league_id
            if "division_id" in changed:
                detail["from_division_id"] = old_division_id
                detail["to_division_id"] = division_id
            self._audit("season_team_registration_updated",
                       "season_team_registration", reg.id, actor_id, detail)
        return reg, False, changed

    def upsert_imported_venue_access(self, season_id: str, venue_id: str,
                                     active: bool, actor_id: Optional[str] = None,
                                     import_batch_id: Optional[str] = None):
        """Grant, reactivate, or revoke a Season's access to a Venue's ice
        by its (season_id, venue_id) identity (#260) — mirrors
        ``grant_season_venue_access``/``revoke_season_venue_access``'s own
        reactivate-vs-create branch, but never raises on an already-active
        or already-revoked row: re-importing identical data is a no-op.
        Revoking a pair that never existed writes nothing at all — it is a
        no-op flagged as a warning at dry-run time, never a fabricated
        inactive record.
        """
        self._require_active_season(season_id)  # #159 read-only guard
        access = self.store.season_venue_access_for_pair(season_id, venue_id)
        if access is None:
            if not active:
                return None, False, []  # nothing to revoke — a true no-op
            access = SeasonVenueAccess(
                id=self.store.next_id("sva"), season_id=season_id,
                venue_id=venue_id, active=True)
            self.store.add_season_venue_access(access)
            self._audit(
                "season_venue_access_granted", "season_venue_access",
                access.id, actor_id,
                {"season_id": season_id, "venue_id": venue_id,
                 "import_batch_id": import_batch_id})
            return access, True, []
        if access.active != active:
            access.active = active
            self.store.save_season_venue_access(access)
            self._audit(
                "season_venue_access_granted" if active
                else "season_venue_access_revoked", "season_venue_access",
                access.id, actor_id,
                {"season_id": season_id, "venue_id": venue_id,
                 "import_batch_id": import_batch_id})
            return access, False, ["active"]
        return access, False, []

    # -- jersey-number invariants (#269) ----------------------------------
    def _validate_jersey_number(self, jersey_number) -> None:
        """Range/type gate: an integer 1..98, or ``None`` (#269).

        A field-level ``validation_error`` names the offending field, so a bad
        value is a clear 400 rather than a mysterious downstream failure. The
        check is here in the service (not only the HTTP schema) so a direct
        service/import caller is held to the same contract.
        """
        if jersey_number_error(jersey_number) is not None:
            raise ValidationError(
                f"jersey_number must be a whole number from {MIN_JERSEY_NUMBER} "
                f"to {MAX_JERSEY_NUMBER}, or left blank.",
                {"reason": "invalid_jersey_number", "field": "jersey_number"})

    def _assert_jersey_available(self, team_id: str, jersey_number,
                                 *, exclude_player_id: Optional[str] = None) -> None:
        """Reject a jersey already worn by an ACTIVE player on the same team (#269).

        Only ACTIVE players hold a number — an inactive player frees it for
        reuse — and only a concrete (non-null) jersey is constrained, so a
        team may have any number of players with no jersey. ``exclude_player_id``
        skips the player being edited/reassigned so it never collides with
        itself. Raises the SAME stable ``IntegrityConflictError`` the database's
        partial unique index raises on a lost race, carrying the conflicting
        team/jersey/player context (never any other private field). Callers must
        run this BEFORE mutating, so a rejected write leaves zero state.
        """
        if jersey_number is None:
            return
        for other in self.store.players_for_team(team_id):
            if (other.is_active and other.jersey_number == jersey_number
                    and other.id != exclude_player_id):
                raise IntegrityConflictError(
                    f"Jersey number {jersey_number} is already worn by an "
                    f"active player on this team.",
                    {"reason": "duplicate_jersey_number", "team_id": team_id,
                     "jersey_number": jersey_number,
                     "conflicting_player_id": other.id,
                     "conflicting_player_name": other.name})

    def release_batch_player_jerseys(self, assignments) -> dict:
        """Stage a batch import's jersey moves so a valid swap can commit (#292).

        A sequential per-row apply cannot commit an otherwise-valid same-team
        swap (A ``7→8``, B ``8→7``): the first write collides with the number
        the second player still holds. Run FIRST, inside the batch's single
        transaction: for every EXISTING active player whose final
        ``(team, jersey)`` slot differs from its current one, release the number
        it holds now (set ``jersey_number = NULL`` — always unconstrained), so
        the subsequent per-row assignment lands the validated final state with
        no transient uniqueness failure (the DB partial index never sees a
        duplicate mid-batch). Only ``jersey_number`` is touched — the id, the
        roster/availability/guardian history, and every other field are
        preserved — and no audit is written here: the per-row upsert emits the
        real ``player_created`` / ``player_updated`` entry with the final value.
        A genuine final-state collision is still caught by the per-row
        ``_assert_jersey_available`` (and the DB index), so the whole batch
        rolls back with zero writes.

        ``assignments`` is an iterable of ``(existing_player, final_team_id,
        final_jersey)``; new players (``existing_player is None``), inactive
        players, numberless players, and players staying in the same slot are
        skipped. Returns ``{player_id: pre-release jersey_number}`` for exactly
        the players it released, so the apply step can restore each real
        original — a blank/keep-current cell must land the ORIGINAL number, not
        the transient NULL (#292 review), and the single final audit must
        describe the operator's real before→after, not the staging.
        """
        released = {}
        for existing, final_team_id, final_jersey in assignments:
            if existing is None or not existing.is_active:
                continue
            if existing.jersey_number is None:
                continue  # holds no number → nothing to release
            if (existing.team_id, existing.jersey_number) == (
                    final_team_id, final_jersey):
                continue  # staying put → keep it so real collisions still catch
            released[existing.id] = existing.jersey_number
            existing.jersey_number = None
            self.store.save_player(existing)
        return released

    # -- convenience: add a player to a team ------------------------------
    @_transactional
    def add_player(self, team_id: str, name: str, position: Position,
                   jersey_number: Optional[int] = None,
                   email: Optional[str] = None,
                   shoots: Optional[str] = None,
                   is_active: bool = True,
                   actor_id: Optional[str] = None) -> Player:
        """Manually create one Player (#114) — the same model/store the CSV
        import path writes, so a league admin isn't forced through Import for
        a single new arrival. Validation mirrors import_validator's row
        checks (jersey_number > 0, an ``@`` with a ``.`` after it in email)
        so a manual create can't slip in data the bulk path would reject."""
        # Name the missing required field (#271) BEFORE the team lookup, so a
        # None/empty team_id is a clear `field_required` validation error rather
        # than the misleading `NotFoundError("Team None not found.")` — correct
        # even when the service is called directly, not just via the HTTP layer.
        if not team_id:
            raise ValidationError(
                "team_id is required.",
                {"reason": "field_required", "field": "team_id"})
        # NOTE (#159 r15): a concurrent delete_team could in principle orphan a
        # player added in its lock window. We deliberately DON'T row-lock the
        # Team here — that would serialize all concurrent same-team player
        # creates, which is a supported path (the jersey partial-unique index
        # decides same-number races). The correct durable fix is a real
        # player.team_id → teams(id) FK (a migration), tracked separately; a
        # broad create-time lock is the wrong trade-off.
        if self.store.get_team(team_id) is None:
            raise NotFoundError(f"Team {team_id} not found.")
        self._validate_jersey_number(jersey_number)
        # Uniqueness is only meaningful among ACTIVE players; a player created
        # inactive parks its number without reserving it (#269).
        if is_active:
            self._assert_jersey_available(team_id, jersey_number)
        # Validate/normalize the email BEFORE any write, so a bad type/format
        # (False, 0, a list, or a malformed string) is a field-level 400 and no
        # player is created — a blank/None just means "no email" (#268 review).
        canonical_email = self._validate_email(email)
        canonical_shoots = self._validate_shoots(shoots)
        canonical_position = self._validate_position(position)
        canonical_name = self._validate_player_name(name)
        player = Player(id=self.store.next_id("player"), team_id=team_id,
                        name=canonical_name,
                        position=canonical_position,
                        jersey_number=jersey_number,
                        shoots=canonical_shoots,
                        is_active=is_active)
        self.store.add_player(player)
        self._audit("player_added", "player", player.id, actor_id,
                    {"team_id": team_id})
        if canonical_email is not None:
            # Nonblank only: create/reactivate via the shared set/retire path.
            self._set_email_contact(f"player:{player.id}", canonical_email)
        return player

    @staticmethod
    def _validate_email(email) -> Optional[str]:
        """Validate + normalize a Player/Official email (#268 review).

        The single type-safe gate reused by create, edit, and both import
        paths. The ONLY non-address values allowed are ``None`` and a
        blank/whitespace string — both normalize to ``None`` (no email / a
        retire on the set-or-retire path). EVERY other non-string (``False``,
        ``0``, a list/dict — note ``bool`` is an ``int`` subclass, so it is
        rejected here rather than coerced) and any malformed string raises a
        field-level ``validation_error`` (``reason="invalid_email"``,
        ``field="email"``) BEFORE any normalization or mutation, so a direct
        service/import caller gets the same atomic, structured rejection the
        HTTP schema gives — never a silent retire or a bare ``AttributeError``.
        Returns the trimmed canonical address, or ``None``.
        """
        if email is None:
            return None
        if not isinstance(email, str):
            raise ValidationError(
                "email must be a string.",
                {"reason": "invalid_email", "field": "email"})
        trimmed = email.strip()
        if not trimmed:
            return None
        at = trimmed.find("@")
        if at <= 0 or "." not in trimmed[at + 1:]:
            raise ValidationError(
                f"Invalid email {trimmed}.",
                {"reason": "invalid_email", "field": "email"})
        return trimmed

    @staticmethod
    def _validate_shoots(shoots) -> Optional[str]:
        """Normalize a shooting hand to canonical ``L``/``R``/``None`` (#268).

        The single enforcement point for the ``shoots`` contract on create and
        edit — the browser dropdown is not a boundary. Returns the canonical
        stored value (``"L"``, ``"R"`` or ``None``); a non-canonical string or a
        non-string raises a field-level ``validation_error`` so a bad value is a
        clean 400 that never reaches the store, contact, or audit trail.
        """
        canonical, reason = normalize_shoots(shoots)
        if reason is not None:
            raise ValidationError(
                f"shoots must be one of {', '.join(VALID_SHOOTS)}, or left blank.",
                {"reason": "invalid_shoots", "field": "shoots"})
        return canonical

    @staticmethod
    def _validate_position(position) -> Position:
        """Canonicalize a Player position to a ``Position`` (#268 review).

        The single validator shared by Player create and edit (and aligned with
        the import parser, which likewise turns a cell string into ``Position``).
        Accepts a ``Position`` as-is or parses a valid position string; every
        other value — an invalid string, ``None`` (position is required, not
        nullable), or a wrong type — raises a field-level ``validation_error``
        (``reason="invalid_position"``, ``field="position"``) BEFORE any
        mutation, so a direct service caller can't persist a bad position and
        the HTTP error carries the field the same way jersey/shoots/email do.
        """
        if isinstance(position, Position):
            return position
        if isinstance(position, str):
            try:
                return Position(position)
            except ValueError:
                pass
        raise ValidationError(
            f"position must be one of {', '.join(p.value for p in Position)}.",
            {"reason": "invalid_position", "field": "position"})

    @staticmethod
    def _validate_player_name(name) -> str:
        """Canonicalize a Player name (#268 review).

        The one name validator shared by Player create, edit, and import — it
        does NOT go through the generic ``_require_name`` (which ``str()``-
        coerces, so ``False``/``0``/``[]``/``{}`` would persist as
        ``"False"``/``"0"``/…). Accepts only a nonblank string and returns it
        trimmed; every non-string (``None``, a bool, a number, a collection) and
        a blank/whitespace string raises a field-level ``validation_error``
        (``reason="invalid_name"``, ``field="name"``) BEFORE any mutation, so a
        direct service/import caller gets the same atomic, field-named rejection
        the other Player fields now give.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValidationError(
                "name is required and must be a non-empty string.",
                {"reason": "invalid_name", "field": "name"})
        return name.strip()

    @_transactional
    def update_player(self, player_id: str, *, name=_UNSET, position=_UNSET,
                      jersey_number=_UNSET, shoots=_UNSET, email=_UNSET,
                      actor_id: Optional[str] = None) -> Player:
        """Correct a Player's profile in place (#268) — id and history unchanged.

        A partial, audited edit for ``name`` / ``position`` / ``jersey_number`` /
        ``shoots`` and the Player's email ``ContactDestination``. Team
        reassignment (``assign_player_team``) and the active/inactive lifecycle
        (#270) stay separate operations and are intentionally NOT editable here.
        Any field left ``_UNSET`` is untouched; an explicit ``None`` clears a
        nullable one (jersey/shoots, or the email → the contact is retired).

        Every change reuses the shared validation (jersey range + active-team
        uniqueness from #269, name required, email shape). A rejected value
        raises a field-level error and — because the whole method is one
        transaction — leaves ZERO partial state. A genuine no-op writes nothing
        and appends no ``player_updated`` audit (so the trail never lies). The
        audit's ``changed_fields`` names WHICH fields changed, never the email
        address or any other value.
        """
        player = self.store.get_player_for_update(player_id)
        if player is None:
            raise NotFoundError(f"Player {player_id} not found.")

        changed = []
        if name is not _UNSET:
            new_name = self._validate_player_name(name)
            if new_name != player.name:
                player.name = new_name
                changed.append("name")
        if position is not _UNSET:
            new_position = self._validate_position(position)
            if new_position != player.position:
                player.position = new_position
                changed.append("position")
        if shoots is not _UNSET:
            new_shoots = self._validate_shoots(shoots)
            if new_shoots != player.shoots:
                player.shoots = new_shoots
                changed.append("shoots")
        if jersey_number is not _UNSET:
            self._validate_jersey_number(jersey_number)
            if jersey_number != player.jersey_number:
                # Only an active player reserves a number; check the CURRENT team
                # (reassignment is a separate op), excluding self (#269).
                if player.is_active:
                    self._assert_jersey_available(
                        player.team_id, jersey_number,
                        exclude_player_id=player.id)
                player.jersey_number = jersey_number
                changed.append("jersey_number")

        email_changed = False
        if email is not _UNSET:
            email_changed = self._apply_player_email(player, email)

        if changed:
            self.store.save_player(player)
        if changed or email_changed:
            fields = list(changed) + (["email"] if email_changed else [])
            self._audit("player_updated", "player", player.id, actor_id,
                        {"changed_fields": fields})
        return player

    def _set_email_contact(self, recipient_ref: str, email) -> bool:
        """The one set/retire path for a recipient's EMAIL ``ContactDestination``.

        Shared by Player create, edit, and BOTH import paths so no caller can
        drift on the contact lifecycle (#268 review). A non-empty ``email``
        validates then becomes the single active EMAIL destination for
        ``recipient_ref`` — created, or **updated-and-reactivated in place**, so
        a value re-supplied after a retirement makes the contact active again
        rather than silently staying ``active=False``. An empty/``None`` value
        retires an existing active one (``active=False``, never deleted, so its
        history and any notification preferences — keyed on ``recipient_ref``,
        not this row — survive and are never orphaned). Returns ``True`` only
        when something actually changed.

        Callers that must treat a blank value as a *no-op* rather than a
        retirement (the imports, where an absent cell means "leave as-is")
        simply guard the call on a non-empty email; this helper never sees the
        blank in that case.
        """
        # Type-safe gate FIRST: a non-string/non-None value (False, 0, a list)
        # or a malformed string raises a field-level error before any read or
        # mutation — never coerced to "" (which would silently retire) and
        # never a bare AttributeError (#268 review). None/blank → None (retire).
        normalized = self._validate_email(email)
        existing = self.store.get_contact_destination(
            recipient_ref, NotificationChannel.EMAIL)
        if normalized is None:
            if existing is not None and existing.active:
                existing.active = False
                self.store.save_contact_destination(existing)
                return True
            return False
        if existing is not None:
            if existing.destination == normalized and existing.active:
                return False
            existing.destination = normalized
            existing.active = True
            self.store.save_contact_destination(existing)
            return True
        self.store.add_contact_destination(ContactDestination(
            id=self.store.next_id("contact"), recipient_ref=recipient_ref,
            channel=NotificationChannel.EMAIL, destination=normalized))
        return True

    def _apply_player_email(self, player: Player, email) -> bool:
        """Set/retire the Player's email contact from the edit drawer (#268).

        The edit field IS the Player's current email, so an explicit ``None``
        retires it — thin wrapper over the shared ``_set_email_contact`` on the
        single ``player:<id>`` EMAIL destination.
        """
        return self._set_email_contact(f"player:{player.id}", email)

    def active_player_email(self, player_id: str) -> Optional[str]:
        """The Player's current active email, for the operator edit drawer (#268).

        Operator-gated read only (its one caller is the MANAGE_SETUP ``/api/
        players`` route). Returns the address of the active ``player:<id>``
        EMAIL destination, or ``None`` — a retired row reads as no email.
        """
        c = self.store.get_contact_destination(
            f"player:{player_id}", NotificationChannel.EMAIL)
        return c.destination if (c is not None and c.active) else None

    @_transactional
    def set_player_active(self, player_id: str, active: bool, *,
                          actor_id: Optional[str] = None,
                          reason: Optional[str] = None) -> Player:
        """Deactivate or reactivate a Player without deleting history (#270).

        The supported exit for an injured-reserve, a mid-season move, or a
        departure — distinct from delete (which correctly refuses to shed
        historical dependencies) and from the profile edit (#268) and Team
        reassignment, which stay their own operations. The Player id, guardian
        links, contact history, roster/game history, availability, audit, and
        statistics are all preserved; only ``is_active`` flips. Roster
        selection and substitute eligibility already gate on ``is_active``
        (roster_service), so deactivation removes the Player from FUTURE
        selection while every historical row stays readable and unchanged.

        Reactivation is NOT a silent resurrection: it re-runs the same jersey
        integrity the create/edit paths enforce (#269) — while the Player was
        inactive its number was released and an active teammate may now wear it,
        so a collision blocks reactivation with the usual field-level conflict —
        and it re-checks that the Player's Team still exists. A scoped Player
        account can never be bound or reactivated onto an inactive Player
        (account_service), so a login can't outlive the roster exit.

        Idempotent: setting the state the Player is already in is a no-op that
        writes nothing and appends no audit (no duplicate audit noise). A real
        change writes a ``player_activated`` / ``player_deactivated`` audit with
        the actor, prior + new state, and the ``reason`` when supplied — never a
        raw value beyond the caller-provided reason string.
        """
        # Validate the contract fields BEFORE any read/mutation (#270 review):
        # ApiService.set_player_active is also a supported boundary and forwards
        # raw values, so `active` must be an actual bool (never bool()-coerced —
        # "false"/"0"/[] would flip state silently) and `reason` must be a
        # string or None (a non-string must not enter or vanish from the audit).
        if not isinstance(active, bool):
            raise ValidationError(
                "active must be true or false.",
                {"reason": "invalid_active", "field": "active"})
        if reason is not None:
            if not isinstance(reason, str):
                raise ValidationError(
                    "reason must be a string.",
                    {"reason": "invalid_reason", "field": "reason"})
            reason = reason.strip() or None   # blank/whitespace → omitted
        player = self.store.get_player_for_update(player_id)
        if player is None:
            raise NotFoundError(f"Player {player_id} not found.")
        target = active
        if not target or not player.is_active:
            # Reconcile dangling scoped-account authority for this Player, in
            # this same transaction, BEFORE the idempotent short-circuit below
            # (#270 review). A login must never outlive the roster exit, and
            # bringing a Player record back must never silently restore a login:
            #   • Deactivation (target False) — retire any ACTIVE scoped account
            #     and revoke its live sessions. Runs even when the Player row is
            #     ALREADY inactive so legacy data (inactive Players were once
            #     creatable and account binding did not reject them) that still
            #     carries a live account/session is reconciled, not left
            #     dangling.
            #   • Reactivation TRANSITION (target True while the row is still
            #     inactive) — retire any account still bound from before the
            #     Player went inactive, so once ``is_active`` flips true (and
            #     ``_player_team_id`` resolves again) that stale account/session
            #     does NOT immediately regain private-game / self-service
            #     access. Account access is restored only by the separate,
            #     explicit account-lifecycle reactivation, which re-checks the
            #     Player is active (#266/#282).
            # An idempotent reactivation of an ALREADY-active Player skips this
            # (``not target`` false, ``not player.is_active`` false) so a
            # legitimate live login is never nuked. The reconcile is itself
            # idempotent — it touches only still-active accounts and un-revoked
            # sessions and audits only what it actually changes — so a repeat
            # call mutates nothing further.
            #
            # Pass the ACCURATE lifecycle cause into the audit (#270 review): the
            # helper runs in three distinct cases and the account-deactivation
            # audit reason must tell the truth so an investigator can tell a real
            # deactivation apart from a legacy reconcile or the reactivation
            # safety reconcile — never a hard-coded "player_deactivated" beside a
            # player_activated event.
            if not target:
                retire_reason = ("player_deactivated" if player.is_active
                                 else "player_deactivation_reconcile")
            else:
                retire_reason = "player_reactivation_reconcile"
            self._retire_player_account_authority(
                player.id, actor_id, retire_reason)
        if player.is_active == target:
            # No Player-state change → no player_activated/deactivated audit
            # (idempotent lifecycle). Any account/session repair above audited
            # itself; nothing else is written.
            return player
        if target:
            # Reactivation integrity: the Team must still exist and the jersey
            # must be free among the team's ACTIVE players (it was released
            # while inactive) — never resurrect an invalid record silently.
            if self.store.get_team(player.team_id) is None:
                raise NotFoundError(f"Team {player.team_id} not found.")
            self._assert_jersey_available(
                player.team_id, player.jersey_number,
                exclude_player_id=player.id)
        prior = player.is_active
        player.is_active = target
        self.store.save_player(player)
        detail = {"from_active": prior, "to_active": target}
        if reason:
            detail["reason"] = reason
        self._audit("player_activated" if target else "player_deactivated",
                    "player", player.id, actor_id, detail)
        return player

    def _retire_player_account_authority(self, player_id: str,
                                         actor_id: Optional[str],
                                         reason: str) -> None:
        """Deactivate every active account scoped to ``player_id`` and revoke
        its live sessions (#270 review). ``reason`` is the accurate lifecycle
        cause recorded on each ``user_account_deactivated`` audit — the caller
        passes ``player_deactivated`` for a real deactivation,
        ``player_deactivation_reconcile`` for a legacy already-inactive Player,
        or ``player_reactivation_reconcile`` for the inactive→active safety
        reconcile — so the committed audit trail never claims a deactivation
        cause beside a ``player_activated`` event. Runs inside
        ``set_player_active``'s transaction, so it rolls back with the rest on
        any failure. Because ``SessionManager.resolve`` fails closed on an
        inactive account, killing the account row is what terminates the
        session; the explicit revoke is belt-and-suspenders and mirrors the
        account-active route.

        Concurrency (#270 review): the unlocked ``all_user_accounts()`` scan only
        picks CANDIDATE ids; each account is then re-read under its ROW LOCK
        (``get_user_account_for_update``) and re-checked immediately before the
        write. This is what makes the reconcile safe against a concurrent
        ``rebind_account_scope`` / ``set_active`` on the same account: we never
        blind-save a stale whole-row object (which would clobber a completed
        rebind and re-bind the account to this inactive Player while the audit
        said it moved). If, under the lock, the account was rebound away from
        this Player or is already inactive, we skip it — leaving the rebind's new
        scope intact. The lock order is Player→Account everywhere
        (``set_player_active`` already holds the Player lock; account
        create/rebind/reactivate lock the Player subject BEFORE the account), so
        this cannot deadlock with those paths."""
        now = _utcnow()
        candidate_ids = [a.id for a in self.store.all_user_accounts()
                         if a.active
                         and (a.scope or {}).get("player_id") == player_id]
        for account_id in candidate_ids:
            acct = self.store.get_user_account_for_update(account_id)
            if acct is None:
                continue
            # Re-check UNDER THE LOCK: a concurrent rebind may have moved the
            # scope off this Player, or a concurrent deactivate may have already
            # retired it. Only retire an account still active AND still bound to
            # this Player — never overwrite a rebind's committed new scope.
            if not acct.active or (acct.scope or {}).get("player_id") != player_id:
                continue
            acct.active = False
            self.store.save_user_account(acct)
            self._audit("user_account_deactivated", "user_account", acct.id,
                        actor_id, {"reason": reason, "player_id": player_id})
            for sess in self.store.sessions_for_user(acct.id):
                if sess.revoked_at is None:
                    sess.revoked_at = now
                    self.store.save_session(sess)

    # -- CSV import commit (#93) --------------------------------------------
    def commit_teams_players_import(self, season_id: str, sheets: dict,
                                    actor_id: Optional[str] = None) -> dict:
        """Commit step 2 of the pilot onboarding import wizard: teams+players.

        ``sheets`` is ALREADY-PARSED row dicts (``{"teams": [...],
        "players": [...]}``) — CSV-text parsing stays at the API facade layer
        (see ``ApiService.commit_teams_players_import``), matching #92's
        layering. ``validate_import`` (the SAME pure gate #92 added) is reused
        unchanged as the pre-commit check: if it reports any errors, nothing
        is written at all, not even otherwise-valid rows in the other sheet.

        Teams and players are matched across repeat uploads by ``external_ref``
        (the sheet's ``team_code``/``player_code``), so re-importing the same
        payload updates existing rows in place instead of duplicating them.
        This match is deliberately GLOBAL, not scoped to ``season_id``: a
        ``team_code`` reused in a different season's import will move the
        existing team into the new season rather than create a second one.
        Acceptable for v1 if codes are treated as globally stable identifiers;
        revisit if/when multi-season imports become a real workflow (#93
        review).

        Clubs and divisions have no external code in this slice — they are
        found-or-created by an exact (``.strip()``ped) name match, division
        scoped to ``season_id`` since the same division name can recur across
        seasons. This is a deliberately simple v1: no fuzzy matching, no
        dedup across near-identical names beyond whitespace-trimming.

        Not ``@_transactional``: this method must call no other
        ``@_transactional`` service method (``create_club``/``create_team``/
        ``add_player`` all open their own transaction), or a nested
        ``with store.transaction():`` raises ``sqlite3.OperationalError:
        cannot start a transaction within a transaction`` on the SQL backend —
        exactly the decorator/transaction-boundary bug that broke #87 and
        #88. Instead this opens exactly one transaction itself below and
        duplicates the small amount of create-logic it needs via raw store
        calls + its own ``self._audit(...)`` calls.
        """
        if self.store.get_season(season_id) is None:
            raise NotFoundError(f"Season {season_id} not found.")
        # NOTE: the archived-Season read-only guard is NOT taken here — an
        # unlocked pre-transaction check runs in autocommit on PostgreSQL and
        # releases its FOR UPDATE lock before the writes, letting a concurrent
        # archive commit in between. It is acquired as the first statement inside
        # the single write transaction below, so the lock is held through every
        # import mutation (#159).

        result = validate_import(sheets, store=self.store)
        if not result["ok"]:
            return {"committed": False, "summary": result["summary"],
                    "errors": result["errors"], "warnings": result["warnings"]}

        team_rows = list(sheets.get("teams") or [])
        player_rows = list(sheets.get("players") or [])

        # #92's validator never checks that `position` is a recognized value
        # (it doesn't require the column at all). Validate every row's
        # position BEFORE any writes, so a bad value on a later row can't
        # leave earlier rows (or the teams sheet) partially committed —
        # the same all-or-nothing guarantee validate_import already gives us.
        for row in player_rows:
            value = row.get("position")
            if _blank(value):
                continue
            try:
                Position(_clean(value))
            except ValueError:
                raise ValidationError(
                    f"Unknown position '{value}' for player_code "
                    f"{row.get('player_code')}.")

        counts = {"teams_created": 0, "teams_updated": 0,
                  "players_created": 0, "players_updated": 0,
                  "clubs_created": 0, "divisions_created": 0}
        # Generated up front (not after the row loops) so every row-level
        # audit entry below can be tagged with it, letting the Activity feed
        # group a batch's individual creates/updates under its summary (#102).
        batch_id = self.store.next_id("importbatch")

        with self.store.transaction():
            # #159 — acquire the archived-Season row lock as the FIRST statement
            # inside the single transaction that holds every import write, and
            # derive Season-owned values from that same locked read. On
            # PostgreSQL the FOR UPDATE lock is then held through all the
            # Team/Division/registration/Player/contact/audit writes, so a
            # concurrent archive either commits before this import (which then
            # fails season_archived with zero mutation) or blocks until this
            # import commits — never a write into an already-archived Season.
            _season = self._require_active_season(season_id)
            # The permanent program every imported team belongs to (#180): the
            # program of the season being imported into.
            season_league_id = _season.program_id
            # Pre-write integrity gate (#180 review): before ANY write, prove the
            # import won't silently re-home a permanent Team across leagues or
            # strand committed games by moving a registration's division. A
            # violation returns a structured error with zero writes.
            if (not season_league_id
                    or self.store.get_program(season_league_id) is None):
                return {"committed": False, "summary": result["summary"],
                        "errors": [{"sheet": "teams",
                            "reason": "season_league_missing",
                            "message": ("The import target season is not linked "
                                        "to a valid league."),
                            "season_id": season_id}],
                        "warnings": result["warnings"]}
            gate_errors = []
            for row in team_rows:
                code = _clean(row.get("team_code"))
                existing = next((t for t in self.store.all_teams()
                                 if t.external_ref == code), None)
                if existing is None:
                    continue
                team_regs = [r for r in self.store.all_season_team_registrations()
                             if r.team_id == existing.id]
                # (1) The import must never move a permanent Team's program.
                if (existing.program_id or None) != season_league_id:
                    if existing.program_id is None:
                        # A league-less team is repaired to the target league
                        # only if EVERY retained registration already resolves
                        # there; otherwise its history would go cross-league.
                        stray = [r.id for r in team_regs
                                 if self._registration_program(r) != season_league_id]
                        if stray:
                            gate_errors.append({
                                "sheet": "teams", "team_code": code,
                                "reason": "team_league_ambiguous",
                                "message": (f"Team {code} has registrations in "
                                            "another league; assign its permanent "
                                            "league before importing."),
                                "affected_registration_ids": stray})
                    else:
                        gate_errors.append({
                            "sheet": "teams", "team_code": code,
                            "reason": "team_league_move_blocked",
                            "message": (f"Team {code} already belongs to a "
                                        "different league; the import can't "
                                        "re-home it."),
                            "affected_registration_ids": [r.id for r in team_regs],
                            "affected_game_ids": [
                                g.id for g in self.store.all_games()
                                if existing.id in (g.home_team_id, g.away_team_id)]})
                # (2) ANY change to a registration's division must not strand
                # committed games — the same guard assign_season_team_division
                # enforces. The target is resolved for every row, including
                # None for a blank division (which would CLEAR the division), so
                # a blank re-import can't quietly unassign a team that still has
                # scheduled games. Applies to inactive/historical registrations
                # too (a re-import reactivates and may re-place them).
                div_name = row.get("division_name")
                reg = next(  # #283: find the team's registration in this Season
                    (r for r in self.store.registrations_for_season(season_id)
                     if r.team_id == existing.id), None)
                if reg is not None:
                    if _blank(div_name):
                        target_div_id = None
                    else:
                        match = next(
                            (d for d in self.store.divisions_for_season(season_id)
                             if d.name == _clean(div_name)), None)
                        # A not-yet-created named division is necessarily a
                        # different placement than the current one.
                        target_div_id = match.id if match else object()
                    if target_div_id != reg.division_id:
                        stranded = self._games_scheduled_for_team_in_season(
                            season_id, existing.id)
                        if stranded:
                            gate_errors.append({
                                "sheet": "teams", "team_code": code,
                                "reason": "registration_division_move_strands_games",
                                "message": (f"Re-importing team {code} would move "
                                            "or clear its division while it has "
                                            "scheduled games; resolve them first."),
                                "affected_game_ids": stranded})
            if gate_errors:
                return {"committed": False, "summary": result["summary"],
                        "errors": gate_errors, "warnings": result["warnings"]}

            team_code_to_id = {}
            for row in team_rows:
                team_code = _clean(row.get("team_code"))
                team_name = _clean(row.get("team_name"))

                club_id = None
                club_name_raw = row.get("club_name")
                if not _no_club(club_name_raw):
                    club_name = _clean(club_name_raw)
                    club = next((c for c in self.store.all_clubs()
                                if c.name == club_name), None)
                    if club is None:
                        club = Club(id=self.store.next_id("club"), name=club_name)
                        self.store.add_club(club)
                        self._audit("club_created", "club", club.id, actor_id,
                                    {"import_batch_id": batch_id})
                        counts["clubs_created"] += 1
                    club_id = club.id

                division = None
                division_name_raw = row.get("division_name")
                if not _blank(division_name_raw):
                    division_name = _clean(division_name_raw)
                    division = next(
                        (d for d in self.store.divisions_for_season(season_id)
                         if d.name == division_name),
                        None)
                    if division is None:
                        # #283: a Division belongs to a LeagueSeason. This simple
                        # onboarding import carries no per-division League, so use
                        # the Season's LeagueSeason, auto-provisioning a default
                        # League when the Season has none yet (mirrors
                        # create_division so imported rows are never orphaned with
                        # a null league_season_id).
                        _ls = self._import_default_league_season(season_id)
                        division = Division(id=self.store.next_id("division"),
                                            league_season_id=_ls.id,
                                            name=division_name)
                        self.store.add_division(division)
                        self._audit("division_created", "division", division.id,
                                    actor_id, {"season_id": season_id,
                                              "import_batch_id": batch_id})
                        counts["divisions_created"] += 1

                division_id = division.id if division else None

                # #180/#283: a team's participation is converged onto its
                # permanent League + a SeasonTeamRegistration, never the legacy
                # Team.division_id. The team is a permanent member of THIS
                # import season's League (auto-provisioned if the Season had
                # none); the imported division lives on the registration.
                _import_ls = (self.store.get_league_season(division.league_season_id)
                              if division is not None
                              else self._import_default_league_season(season_id))
                _import_league_id = _import_ls.league_id if _import_ls else None
                team = next((t for t in self.store.all_teams()
                            if t.external_ref == team_code), None)
                if team is not None:
                    team.name = team_name
                    team.club_id = club_id
                    if season_league_id:
                        team.program_id = season_league_id
                    if _import_league_id and not team.league_id:
                        team.league_id = _import_league_id
                    self.store.save_team(team)
                    self._audit("team_updated", "team", team.id, actor_id,
                                {"club_id": club_id, "league_id": team.program_id,
                                 "import_batch_id": batch_id})
                    counts["teams_updated"] += 1
                else:
                    team = Team(id=self.store.next_id("team"), name=team_name,
                               club_id=club_id, program_id=season_league_id,
                               league_id=_import_league_id,
                               external_ref=team_code)
                    self.store.add_team(team)
                    self._audit("team_created", "team", team.id, actor_id,
                                {"club_id": club_id, "league_id": season_league_id,
                                 "import_batch_id": batch_id})
                    counts["teams_created"] += 1
                team_code_to_id[team_code] = team.id

                # Idempotently upsert THIS season's registration with the
                # imported division; never touch another season's row.
                # #283: a registration is stored against a LeagueSeason — the
                # chosen Division's LeagueSeason, else the Season's sole one.
                if division is not None:
                    reg_ls_id = division.league_season_id
                else:
                    reg_ls_id = _import_ls.id if _import_ls else None
                reg = next(
                    (r for r in self.store.registrations_for_season(season_id)
                     if r.team_id == team.id), None)
                if reg is not None:
                    if not reg.active or reg.division_id != division_id:
                        reg.active = True
                        reg.division_id = division_id
                        reg.league_season_id = reg_ls_id
                        self.store.save_season_team_registration(reg)
                        self._audit("season_team_registration_updated",
                                    "season_team_registration", reg.id, actor_id,
                                    {"season_id": season_id, "team_id": team.id,
                                     "division_id": division_id,
                                     "import_batch_id": batch_id})
                else:
                    reg = SeasonTeamRegistration(
                        id=self.store.next_id("streg"),
                        league_season_id=reg_ls_id,
                        team_id=team.id, division_id=division_id,
                        active=True)
                    self.store.add_season_team_registration(reg)
                    self._audit("season_team_registered",
                                "season_team_registration", reg.id, actor_id,
                                {"season_id": season_id, "team_id": team.id,
                                 "division_id": division_id,
                                 "import_batch_id": batch_id})

            # Swap-safe apply (#292): release every existing player's jersey
            # whose final slot moves BEFORE any per-row write, so a valid
            # same-team swap (A 7→8, B 8→7) commits without a transient
            # uniqueness failure. A blank jersey cell keeps the current number
            # (final == current → not released).
            by_code = {p.external_ref: p for p in self.store.all_players()
                       if p.external_ref is not None}

            def _final_slot(row):
                existing = by_code.get(_clean(row.get("player_code")))
                team_id = team_code_to_id.get(_clean(row.get("team_code")))
                raw = row.get("jersey_number")
                jersey = (int(_clean(raw)) if not _blank(raw)
                          else (existing.jersey_number if existing else None))
                return existing, team_id, jersey
            released = self.release_batch_player_jerseys(
                _final_slot(row) for row in player_rows)

            for row in player_rows:
                player_code = _clean(row.get("player_code"))
                full_name = self._validate_player_name(
                    (f"{_clean(row.get('first_name'))} "
                     f"{_clean(row.get('last_name'))}").strip())
                # validate_import already guarantees this team_code matches a
                # row in THIS SAME upload's teams sheet; .get() is just a
                # defensive belt-and-suspenders check against a bug elsewhere.
                team_id = team_code_to_id.get(_clean(row.get("team_code")))
                if team_id is None:
                    raise ValidationError(
                        f"Unknown team_code for player_code {player_code}.")

                jersey_raw = row.get("jersey_number")
                jersey_number = (int(_clean(jersey_raw)) if not _blank(jersey_raw)
                                else None)
                self._validate_jersey_number(jersey_number)
                position_raw = row.get("position")
                position = (Position(_clean(position_raw))
                           if not _blank(position_raw) else None)
                email_raw = row.get("email")
                email = _clean(email_raw) if not _blank(email_raw) else None

                player = next((p for p in self.store.all_players()
                              if p.external_ref == player_code), None)
                if player is not None:
                    # Partial-field-overwrite: only fields the sheet actually
                    # supplies this time are updated. A blank jersey cell keeps
                    # the existing number — resolved from the ORIGINAL captured
                    # before the swap-safe release (#292), never the transient
                    # NULL the release wrote, so a Team move with a blank cell
                    # never erases the number. The uniqueness check uses the
                    # resulting number on the resulting team (#269), before any
                    # write — a collision aborts the whole batch, zero rows.
                    original_jersey = released.get(player.id, player.jersey_number)
                    target_jersey = (jersey_number if jersey_number is not None
                                     else original_jersey)
                    if player.is_active:
                        self._assert_jersey_available(
                            team_id, target_jersey, exclude_player_id=player.id)
                    player.name = full_name
                    player.team_id = team_id
                    player.jersey_number = target_jersey
                    if position is not None:
                        player.position = position
                    self.store.save_player(player)
                    self._audit("player_updated", "player", player.id, actor_id,
                                {"team_id": team_id, "import_batch_id": batch_id})
                    counts["players_updated"] += 1
                else:
                    # A brand-new imported player is active, so its number must
                    # be free among the team's active players (#269).
                    self._assert_jersey_available(team_id, jersey_number)
                    # The domain model requires a Position with no default,
                    # but #92 doesn't require the CSV to supply one — default
                    # a brand-new player to FORWARD as an explicit judgment
                    # call; may want revisiting.
                    player = Player(id=self.store.next_id("player"), team_id=team_id,
                                    name=full_name,
                                    position=position or Position.FORWARD,
                                    jersey_number=jersey_number,
                                    external_ref=player_code)
                    self.store.add_player(player)
                    self._audit("player_added", "player", player.id, actor_id,
                                {"team_id": team_id, "import_batch_id": batch_id})
                    counts["players_created"] += 1

                # A blank cell parsed to None above → no-op (leave any existing
                # contact untouched). A supplied address validates, updates AND
                # reactivates the single player:<id> EMAIL contact through the
                # shared set/retire path, so a re-import revives a contact a
                # prior edit had retired instead of leaving it active=False
                # (#268 review).
                if email is not None:
                    self._set_email_contact(f"player:{player.id}", email)

            # skipped/errors are always 0 here by construction: the
            # all-or-nothing gate above means the only way to reach this line
            # is a fully clean validate_import result — any error blocks the
            # transaction before an audit row is ever written. Present anyway
            # for a stable import_committed detail shape across #93-#95.
            self._audit("import_committed", "import_batch", batch_id, actor_id,
                        {"import_type": "teams_players", "season_id": season_id,
                         "skipped": 0, "errors": 0, **counts})

        return {
            "committed": True,
            "summary": {
                "teams": {"created": counts["teams_created"],
                         "updated": counts["teams_updated"]},
                "players": {"created": counts["players_created"],
                           "updated": counts["players_updated"]},
                "clubs_created": counts["clubs_created"],
                "divisions_created": counts["divisions_created"],
            },
            "warnings": result["warnings"],
        }

    # -- CSV import commit: officials + availability (#94) ------------------
    def commit_officials_availability_import(self, sheets: dict,
                                             actor_id: Optional[str] = None
                                             ) -> dict:
        """Commit step 3 of the pilot onboarding import wizard: officials +
        their availability windows.

        ``sheets`` is ALREADY-PARSED row dicts (``{"officials": [...],
        "official_availability": [...]}``) — CSV-text parsing stays at the
        API facade layer, matching #93's layering. No ``season_id`` param:
        officials aren't season-scoped.

        Validation happens FIRST, before any writes, all-or-nothing across
        BOTH sheets: a bad ``official_availability`` row blocks an otherwise-
        clean ``officials`` sheet, exactly like #93's cross-sheet guarantee.
        The officials sheet reuses #92's existing (unmodified) checks via
        ``validate_import({"officials": ...})`` — every other key is treated
        as empty by that function. The availability sheet is checked by the
        NEW sibling function ``validate_official_availability``, which
        (deliberately, see its own docstring) resolves ``official_code``
        against either this upload's officials sheet OR an official already
        persisted from a prior commit.

        Not ``@_transactional``, for the exact reason documented at length on
        ``commit_teams_players_import`` above: this method must call no other
        ``@_transactional`` service method (``create_official`` opens its own
        transaction), or a nested ``with store.transaction():`` raises
        ``sqlite3.OperationalError: cannot start a transaction within a
        transaction`` on the SQL backend — the #87/#88 bug class. It opens
        exactly one transaction itself below, duplicating the small amount of
        create/update logic it needs via raw store calls plus its own
        ``self._audit(...)`` calls. ``self.set_official_availability`` is NOT
        ``@_transactional`` (see its docstring), so it is safe to call
        directly from inside this method's single transaction for the
        create-a-new-availability-window path.
        """
        officials_rows = list(sheets.get("officials") or [])
        availability_rows = list(sheets.get("official_availability") or [])

        officials_result = validate_import({"officials": officials_rows})
        if officials_result["errors"]:
            return {"committed": False, "summary": officials_result["summary"],
                    "errors": officials_result["errors"],
                    "warnings": officials_result["warnings"]}

        official_codes_in_sheet = {
            _clean(row.get("official_code")) for row in officials_rows
            if not _blank(row.get("official_code"))
        }
        existing_external_refs = {
            o.external_ref for o in self.store.all_officials() if o.external_ref
        }
        avail_result = validate_official_availability(
            availability_rows, official_codes_in_sheet, existing_external_refs)
        if avail_result["errors"]:
            return {
                "committed": False,
                "summary": {"officials": len(officials_rows),
                           "official_availability": len(availability_rows)},
                "errors": officials_result["errors"] + avail_result["errors"],
                "warnings": officials_result["warnings"] + avail_result["warnings"],
            }

        counts = {"officials_created": 0, "officials_updated": 0,
                  "availability_created": 0, "availability_updated": 0,
                  "clubs_created": 0}
        # See commit_teams_players_import's identical note: generated up
        # front so every row-level audit entry can be tagged with it (#102).
        batch_id = self.store.next_id("importbatch")

        with self.store.transaction():
            official_code_to_id = {}
            for row in officials_rows:
                official_code = _clean(row.get("official_code"))
                name = _clean(row.get("name"))
                email_raw = row.get("email")
                email = _clean(email_raw) if not _blank(email_raw) else None

                club_id = None
                club_name_raw = row.get("home_club_name")
                if not _no_club(club_name_raw):
                    club_name = _clean(club_name_raw)
                    club = next((c for c in self.store.all_clubs()
                                if c.name == club_name), None)
                    if club is None:
                        club = Club(id=self.store.next_id("club"), name=club_name)
                        self.store.add_club(club)
                        self._audit("club_created", "club", club.id, actor_id,
                                    {"import_batch_id": batch_id})
                        counts["clubs_created"] += 1
                    club_id = club.id

                official = next((o for o in self.store.all_officials()
                                 if o.external_ref == official_code), None)
                if official is not None:
                    official.name = name
                    official.home_club_id = club_id
                    self.store.save_official(official)
                    self._audit("official_updated", "official", official.id,
                                actor_id, {"home_club_id": club_id,
                                          "import_batch_id": batch_id})
                    counts["officials_updated"] += 1
                else:
                    official = Official(id=self.store.next_id("official"),
                                        name=name, home_club_id=club_id,
                                        external_ref=official_code)
                    self.store.add_official(official)
                    self._audit("official_created", "official", official.id,
                                actor_id, {"import_batch_id": batch_id})
                    counts["officials_created"] += 1
                official_code_to_id[official_code] = official.id

                if email is not None:
                    recipient_ref = f"official:{official.id}"
                    existing = self.store.get_contact_destination(
                        recipient_ref, NotificationChannel.EMAIL)
                    if existing is not None:
                        existing.destination = email
                        self.store.save_contact_destination(existing)
                    else:
                        self.store.add_contact_destination(ContactDestination(
                            id=self.store.next_id("contact"),
                            recipient_ref=recipient_ref,
                            channel=NotificationChannel.EMAIL,
                            destination=email))

            for row in availability_rows:
                official_code = _clean(row.get("official_code"))
                official_id = official_code_to_id.get(official_code)
                if official_id is None:
                    # Not in this upload's officials sheet — validation only
                    # let this through because official_code matched an
                    # existing_external_ref, i.e. an official created by a
                    # PRIOR commit (#94's key new capability over #93).
                    existing = next(
                        (o for o in self.store.all_officials()
                         if o.external_ref == official_code), None)
                    official_id = existing.id if existing else None
                if official_id is None:
                    raise ValidationError(
                        f"Unknown official_code {official_code} for "
                        f"official_availability row.")

                start = _parse_iso_utc(row.get("start_time"))
                end = _parse_iso_utc(row.get("end_time"))
                status_raw = _clean(row.get("status"))
                note_raw = row.get("note")
                note = _clean(note_raw) if not _blank(note_raw) else None

                existing_window = next(
                    (a for a in self.store.availability_for_official(official_id)
                     if a.start_time == start and a.end_time == end), None)
                if existing_window is not None:
                    existing_window.status = OfficialAvailabilityStatus(status_raw)
                    existing_window.note = note
                    self.store.save_official_availability(existing_window)
                    self._audit("official_availability_updated",
                                "official_availability", existing_window.id,
                                actor_id, {"official_id": official_id,
                                          "status": status_raw,
                                          "import_batch_id": batch_id})
                    counts["availability_updated"] += 1
                else:
                    self.set_official_availability(
                        official_id, start, end, status_raw, note=note,
                        actor_id=actor_id,
                        extra_detail={"import_batch_id": batch_id})
                    counts["availability_created"] += 1

            # skipped/errors are always 0 here by construction — see the
            # identical note on commit_teams_players_import's import_committed
            # audit row above.
            self._audit("import_committed", "import_batch", batch_id, actor_id,
                        {"import_type": "officials_availability",
                         "officials_created": counts["officials_created"],
                         "officials_updated": counts["officials_updated"],
                         "availability_created": counts["availability_created"],
                         "availability_updated": counts["availability_updated"],
                         "clubs_created": counts["clubs_created"],
                         "skipped": 0, "errors": 0})

        return {
            "committed": True,
            "summary": {
                "officials": {"created": counts["officials_created"],
                             "updated": counts["officials_updated"]},
                "official_availability": {
                    "created": counts["availability_created"],
                    "updated": counts["availability_updated"]},
                "clubs_created": counts["clubs_created"],
            },
            "warnings": officials_result["warnings"] + avail_result["warnings"],
        }

    # -- CSV import commit: rinks + ice slots (#95) --------------------------
    def commit_rinks_ice_slots_import(self, sheets: dict,
                                      actor_id: Optional[str] = None) -> dict:
        """Commit step 4 of the pilot onboarding import wizard: rinks + their
        ice slots.

        ``sheets`` is ALREADY-PARSED row dicts (``{"rinks": [...],
        "ice_slots": [...]}``) — CSV-text parsing stays at the API facade
        layer, matching #93/#94's layering. No ``season_id`` param: rinks
        aren't season-scoped (same as #94's officials).

        Unlike #94, no new sibling validator is needed here: ``rinks`` and
        ``ice_slots`` are already first-class ``IMPORT_SHEET_NAMES`` members
        that #92's ``validate_import`` fully validates (required fields,
        ``rink_code`` uniqueness, the ``ice_slots`` sheet's ``rink_code``
        cross-reference, ISO-8601 UTC parsing, the ``slot_type`` enum, and
        same-rink overlap warnings) — reused completely unchanged via a
        single ``validate_import(sheets)`` call. That cross-reference is
        deliberately SHEET-INTERNAL-ONLY, like #93's ``team_code`` (NOT
        widened to persisted ``external_ref``s the way #94's
        ``official_code`` was): a real pilot workflow sends ``rinks.csv`` and
        ``ice_slots.csv`` together in one upload, not rinks now and
        slots-for-those-rinks in a separate later commit. ``slot_type`` is
        already validated against :class:`IceSlotType` by ``validate_import``
        itself, so no extra gate is needed for its FORMAT — but see the
        allocated-slot pre-write gate below, which is about its INTERACTION
        with an already-booked game, not its format.

        Venues are found-or-created by an exact (``.strip()``ped) name
        match, the same simple v1 as #93/#94's club matching — never
        updated in place even if a later row's ``address`` differs for the
        same name. Rinks are matched across repeat uploads by
        ``external_ref`` (the sheet's ``rink_code``), and ``rink_name`` is a
        PARTIAL-FIELD-OVERWRITE on that update path (mirroring
        ``commit_teams_players_import``'s player name/jersey_number/position
        handling): since ``validate_import`` doesn't require ``rink_name``
        (only ``venue_name``/``rink_code`` are required for the ``rinks``
        sheet), a repeat row that omits it must leave the existing rink's
        name alone rather than clobbering it back to the row's
        ``rink_code`` — only a brand-new rink defaults its name to the code.
        Ice slots have no code of their own and are matched by the natural
        ``(rink_id, start_time, end_time)`` tuple, mirroring #94's
        availability-window matching.

        Not ``@_transactional``, for the same reason documented at length on
        ``commit_teams_players_import``/``commit_officials_availability_import``
        above — but note the DIFFERENT consequence here: ``create_rink`` AND
        ``create_ice_slot`` are BOTH ``@_transactional`` (unlike
        ``set_official_availability``, which #94 could call directly), so
        this method must call neither — it duplicates their create logic via
        raw store calls plus its own ``self._audit(...)`` calls, all inside
        the single transaction it opens itself.

        Correctness note: an ice slot's ``status`` also transitions to
        ``ALLOCATED`` outside this import, when a game is scheduled onto it
        (``create_game``/``move_game``). A repeat import that always
        re-derived ``status`` from ``slot_type`` — the way ``create_ice_slot``
        does on first creation — would silently downgrade an already-booked
        slot back to ``AVAILABLE``/``BLOCKED`` while the game record still
        points at it. The update-in-place path below therefore leaves
        ``status`` untouched whenever ``store.game_using_ice_slot`` reports a
        game already claims the slot.

        That guard alone isn't enough, though: ``create_game`` also requires
        a booked slot's ``slot_type`` to stay ``GAME`` (it refuses to host a
        game on ``practice``/``maintenance``/etc ice), so silently changing
        ``slot_type`` out from under an allocated slot would leave the game
        record pointing at ice that's no longer game-bookable, even with
        ``status`` correctly preserved. A pre-write gate below therefore
        rejects the ENTIRE commit — before any writes, same all-or-nothing
        guarantee as everything else here — if an incoming row would change
        the ``slot_type`` of a slot a game already uses.

        Overlap handling is split by SOURCE. Overlaps WITHIN this upload
        (two rows in the same ``ice_slots`` sheet) stay ``validate_import``'s
        WARNING-only concern (mirroring #94's availability-overlap warning) —
        both slots are created and the warning surfaces in the response,
        never aborting the commit. But a new slot that would overlap ice
        ALREADY PERSISTED on the rink by another writer — a concurrent
        ``commit_ice_availability`` batch or an earlier import — IS a hard
        conflict: the whole commit is rejected with a ``ScheduleConflictError``
        and rolled back. Migration 045's ``(rink, start, end)`` unique index
        only catches an exact-tuple duplicate (which takes the update path
        here), so this write-boundary revalidation, under the per-rink row
        lock taken below, is what stops a NON-exact overlap (e.g. an imported
        22:30-23:30 against a committed 22:00-23:00) from leaving both rows
        alive (#158 review).
        """
        result = validate_import(sheets)
        if not result["ok"]:
            return {"committed": False, "summary": result["summary"],
                    "errors": result["errors"], "warnings": result["warnings"]}

        rink_rows = list(sheets.get("rinks") or [])
        slot_rows = list(sheets.get("ice_slots") or [])

        # Pre-write gate (mirrors #93's Position check): create_game requires
        # a booked slot's slot_type to stay GAME (it refuses to schedule onto
        # practice/maintenance/etc ice). Silently changing slot_type on a
        # slot a game already uses would leave that game pointing at ice
        # that's no longer game-bookable, even though the status-preserving
        # guard below correctly leaves `status` alone (review fix). Block the
        # WHOLE commit before any writes if any row would do this — the same
        # all-or-nothing guarantee as everything else here.
        for row in slot_rows:
            rink_code = _clean(row.get("rink_code"))
            existing_rink = next((r for r in self.store.all_rinks()
                                 if r.external_ref == rink_code), None)
            if existing_rink is None:
                continue  # a brand-new rink can't yet have a booked slot
            start = _parse_iso_utc(row.get("start_time"))
            end = _parse_iso_utc(row.get("end_time"))
            new_slot_type = IceSlotType(_clean(row.get("slot_type")))
            existing_slot = next(
                (s for s in self.store.all_ice_slots()
                 if s.rink_id == existing_rink.id and s.start_time == start
                 and s.end_time == end), None)
            if existing_slot is None:
                continue
            if (existing_slot.slot_type != new_slot_type
                    and self.store.game_using_ice_slot(existing_slot.id) is not None):
                raise ValidationError(
                    f"Ice slot {existing_slot.id} on rink_code {rink_code} "
                    f"has a game scheduled on it; slot_type cannot change "
                    f"from {existing_slot.slot_type.value} to "
                    f"{new_slot_type.value}.")

        counts = {"rinks_created": 0, "rinks_updated": 0,
                  "ice_slots_created": 0, "ice_slots_updated": 0,
                  "venues_created": 0}
        # See commit_teams_players_import's identical note: generated up
        # front so every row-level audit entry can be tagged with it (#102).
        batch_id = self.store.next_id("importbatch")

        with self.store.transaction():
            # Row-lock every rink this import already has (matched by
            # external_ref), in ascending id order, before any slot read or
            # write. commit_ice_availability locks its accessible rinks in the
            # same order, so a concurrent builder commit (or a second import) on
            # a shared rink serializes here instead of racing or deadlocking, and
            # the overlap snapshot below then reads state no locked-out writer can
            # still grow underneath it (#158 review).
            _existing_rink_by_code = {r.external_ref: r
                                      for r in self.store.all_rinks()
                                      if r.external_ref}
            _codes = {_clean(row.get("rink_code")) for row in rink_rows}
            for _rid in sorted(_existing_rink_by_code[c].id for c in _codes
                               if c in _existing_rink_by_code):
                self.store.get_rink_for_update(_rid)

            # Overlap gate, under the locks and BEFORE any write (mirrors the
            # slot_type gate's all-or-nothing placement so it rolls back cleanly
            # on every backend, including InMemoryStore's no-op transaction). A
            # new slot on an EXISTING rink must not physically overlap ice already
            # persisted there by another writer — a concurrent
            # commit_ice_availability batch or an earlier import. Migration 045's
            # (rink, start, end) unique index catches only an exact-tuple
            # duplicate (which takes the update path below), so a NON-exact
            # overlap (an imported 22:30-23:30 against a committed 22:00-23:00)
            # would otherwise leave both rows alive. Slots on rinks this import
            # is creating have no persisted ice to clash with; overlaps *within
            # this upload* aren't persisted yet, so they stay validate_import's
            # warning-only concern (#158 review).
            _persisted_by_rink = {}
            for _s in self.store.all_ice_slots():
                _persisted_by_rink.setdefault(_s.rink_id, []).append(_s)
            for row in slot_rows:
                _rink = _existing_rink_by_code.get(_clean(row.get("rink_code")))
                if _rink is None:
                    continue  # a brand-new rink can't yet have persisted ice
                _start = _parse_iso_utc(row.get("start_time"))
                _end = _parse_iso_utc(row.get("end_time"))
                _persisted = _persisted_by_rink.get(_rink.id, [])
                if any(s.start_time == _start and s.end_time == _end
                       for s in _persisted):
                    continue  # exact tuple -> update path, not a new overlap
                _clash = next((s for s in _persisted if intervals_overlap(
                    _start, _end, s.start_time, s.end_time)), None)
                if _clash is not None:
                    raise ScheduleConflictError(
                        f"Imported ice slot {_start.isoformat()} to "
                        f"{_end.isoformat()} on rink_code "
                        f"{_clean(row.get('rink_code'))} overlaps persisted slot "
                        f"{_clash.id} on the same rink.",
                        {"reason": "ice_slot_overlap", "rink_id": _rink.id,
                         "rink_code": _clean(row.get("rink_code")),
                         "conflict_slot_id": _clash.id})

            rink_code_to_id = {}
            for row in rink_rows:
                rink_code = _clean(row.get("rink_code"))
                venue_name = _clean(row.get("venue_name"))
                rink_name_raw = row.get("rink_name")
                # rink_name isn't in validate_import's required fields for
                # "rinks" (only venue_name/rink_code are). A brand-new rink
                # defaults its name to the code so it's never blank (an
                # explicit judgment call mirroring #93's position-default,
                # may want revisiting); an EXISTING rink's name is a
                # partial-field-overwrite — a repeat row that omits
                # rink_name must leave the current name alone rather than
                # clobbering it back to the code (review fix).
                rink_name_supplied = not _blank(rink_name_raw)
                rink_name = _clean(rink_name_raw) if rink_name_supplied else None
                address_raw = row.get("address")
                address = _clean(address_raw) if not _blank(address_raw) else ""

                venue = next((v for v in self.store.all_venues()
                             if v.name == venue_name), None)
                if venue is None:
                    venue = Venue(id=self.store.next_id("venue"), name=venue_name,
                                  address=address)
                    self.store.add_venue(venue)
                    self._audit("venue_created", "venue", venue.id, actor_id,
                                {"import_batch_id": batch_id})
                    counts["venues_created"] += 1

                rink = next((r for r in self.store.all_rinks()
                            if r.external_ref == rink_code), None)
                if rink is not None:
                    if rink_name_supplied:
                        rink.name = rink_name
                    rink.venue_id = venue.id
                    self.store.save_rink(rink)
                    self._audit("rink_updated", "rink", rink.id, actor_id,
                                {"venue_id": venue.id, "import_batch_id": batch_id})
                    counts["rinks_updated"] += 1
                else:
                    rink = Rink(id=self.store.next_id("rink"), venue_id=venue.id,
                               name=rink_name if rink_name_supplied else rink_code,
                               external_ref=rink_code)
                    self.store.add_rink(rink)
                    self._audit("rink_created", "rink", rink.id, actor_id,
                                {"venue_id": venue.id, "import_batch_id": batch_id})
                    counts["rinks_created"] += 1
                rink_code_to_id[rink_code] = rink.id

            for row in slot_rows:
                rink_code = _clean(row.get("rink_code"))
                # validate_import already guarantees this rink_code matches a
                # row in THIS SAME upload's rinks sheet; .get() is just a
                # defensive belt-and-suspenders check against a bug elsewhere.
                rink_id = rink_code_to_id.get(rink_code)
                if rink_id is None:
                    raise ValidationError(
                        f"Unknown rink_code {rink_code} for ice_slots row.")

                start = _parse_iso_utc(row.get("start_time"))
                end = _parse_iso_utc(row.get("end_time"))
                slot_type = IceSlotType(_clean(row.get("slot_type")))

                existing_slot = next(
                    (s for s in self.store.all_ice_slots()
                     if s.rink_id == rink_id and s.start_time == start
                     and s.end_time == end), None)
                if existing_slot is not None:
                    existing_slot.slot_type = slot_type
                    if self.store.game_using_ice_slot(existing_slot.id) is None:
                        existing_slot.status = (
                            IceSlotStatus.AVAILABLE
                            if slot_type == IceSlotType.GAME
                            else IceSlotStatus.BLOCKED)
                    self.store.save_ice_slot(existing_slot)
                    self._audit("ice_slot_updated", "ice_slot", existing_slot.id,
                                actor_id, {"rink_id": rink_id,
                                          "slot_type": slot_type.value,
                                          "import_batch_id": batch_id})
                    counts["ice_slots_updated"] += 1
                else:
                    status = (IceSlotStatus.AVAILABLE
                             if slot_type == IceSlotType.GAME
                             else IceSlotStatus.BLOCKED)
                    slot = IceSlot(id=self.store.next_id("slot"), rink_id=rink_id,
                                  start_time=start, end_time=end,
                                  slot_type=slot_type, status=status)
                    self.store.add_ice_slot(slot)
                    self._audit("ice_slot_created", "ice_slot", slot.id, actor_id,
                                {"rink_id": rink_id, "slot_type": slot_type.value,
                                 "import_batch_id": batch_id})
                    counts["ice_slots_created"] += 1

            # skipped/errors are always 0 here by construction — see the
            # identical note on commit_teams_players_import's import_committed
            # audit row above.
            self._audit("import_committed", "import_batch", batch_id, actor_id,
                        {"import_type": "rinks_ice_slots", "skipped": 0,
                         "errors": 0, **counts})

        return {
            "committed": True,
            "summary": {
                "rinks": {"created": counts["rinks_created"],
                         "updated": counts["rinks_updated"]},
                "ice_slots": {"created": counts["ice_slots_created"],
                             "updated": counts["ice_slots_updated"]},
                "venues_created": counts["venues_created"],
            },
            "warnings": result["warnings"],
        }

    # -- officials (#30) ---------------------------------------------------
    @_transactional
    def create_official(self, name: str, home_club_id: Optional[str] = None,
                        actor_id: Optional[str] = None) -> Official:
        if home_club_id and self.store.get_club(home_club_id) is None:
            raise NotFoundError(f"Club {home_club_id} not found.")
        official = Official(id=self.store.next_id("official"),
                            name=self._require_name(name),
                            home_club_id=home_club_id or None)
        self.store.add_official(official)
        self._audit("official_created", "official", official.id, actor_id)
        return official

    def _game_time(self, game: Game):
        slot = self.store.get_ice_slot(game.ice_slot_id) if game.ice_slot_id else None
        start = game.start_time or (slot.start_time if slot else None)
        end = game.end_time or (slot.end_time if slot else None)
        return start, end

    # -- official availability (#88) ---------------------------------------
    def set_official_availability(self, official_id, start_time, end_time,
                                  status, note=None, actor_id=None,
                                  extra_detail=None):
        """Declare an available/unavailable window for an official (#88).

        ``extra_detail`` merges additional keys into the audit entry's
        ``detail`` dict — used by ``commit_officials_availability_import``
        (#102) to tag the entry with its ``import_batch_id`` without this
        single-entity method needing to know anything about imports.

        Not ``@_transactional``: the decorator here was an orphan that had
        drifted off ``assign_official`` (its rightful owner) onto this method
        via the section comment. Restored to ``assign_official`` below; this is
        a single add + audit like ``delete_official_availability``."""
        if self.store.get_official(official_id) is None:
            raise NotFoundError(f"Official {official_id} not found.")
        try:
            st = OfficialAvailabilityStatus(status) if not isinstance(
                status, OfficialAvailabilityStatus) else status
        except ValueError:
            raise ValidationError(f"Unknown availability status '{status}'.")
        if end_time <= start_time:
            raise ValidationError("The end time must be after the start time.")
        a = OfficialAvailability(
            id=self.store.next_id("oavail"), official_id=official_id,
            start_time=start_time, end_time=end_time, status=st, note=note)
        self.store.add_official_availability(a)
        detail = {"official_id": official_id, "status": st.value}
        if extra_detail:
            detail.update(extra_detail)
        self._audit("official_availability_set", "official_availability", a.id,
                    actor_id, detail)
        return a

    def official_availabilities(self, official_id):
        return sorted(self.store.availability_for_official(official_id),
                      key=lambda a: a.start_time)

    def delete_official_availability(self, avail_id, actor_id=None):
        a = self.store.get_official_availability(avail_id)
        if a is None:
            raise NotFoundError("Availability window not found.")
        self.store.delete_official_availability(avail_id)
        self._audit("official_availability_deleted", "official_availability",
                    avail_id, actor_id, {"official_id": a.official_id})
        return a

    def unavailable_window(self, official_id, start, end):
        """The official's UNAVAILABLE window overlapping [start, end), else None."""
        if start is None or end is None:
            return None
        for a in self.store.availability_for_official(official_id):
            if a.status != OfficialAvailabilityStatus.UNAVAILABLE:
                continue
            if start < a.end_time and end > a.start_time:
                return a
        return None

    @_transactional
    def assign_official(self, game_id: str, official_id: str, role: OfficialRole,
                        actor_id: Optional[str] = None,
                        override_unavailable: bool = False) -> OfficialAssignment:
        """Propose an official for a role on a game, with conflict checks.

        If the official has declared an overlapping UNAVAILABLE window (#88),
        the assignment is blocked unless ``override_unavailable`` is set — an
        explicit operator override, which is audited.
        """
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.")
        self._guard_game_season(game)  # #159 read-only guard
        # #159 r15 — re-fetch the game under the Season lock so the cancelled /
        # time-overlap checks below run on its current state, not a locator
        # snapshot a concurrent move_game may have changed.
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.")
        if game.cancelled:
            raise ValidationError("Cannot assign officials to a cancelled game.")
        # #159 r15 — row-lock the Official: delete_official locks the Official row
        # and blocks on assignments_for_official, so without this lock a
        # concurrent delete_official could remove it while this inserts an
        # assignment referencing it — an orphan.
        official = self.store.get_official_for_update(official_id)
        if official is None:
            raise NotFoundError(f"Official {official_id} not found.")
        if not official.is_active:
            raise NotEligibleError(f"{official.name} is not an active official.")

        # Conflict of interest: an official from a club playing in this game.
        if official.home_club_id:
            for tid in (game.home_team_id, game.away_team_id):
                team = self.store.get_team(tid) if tid else None
                if team is not None and team.club_id == official.home_club_id:
                    raise NotEligibleError(
                        f"{official.name} has a club conflict with this game.")

        # Already actively assigned to this game (any role)?
        for a in self.store.assignments_for_game(game_id):
            if a.official_id == official_id and a.status.is_active:
                raise ScheduleConflictError(
                    f"{official.name} is already assigned to this game.")

        # Time overlap with another active assignment on a different game.
        start, end = self._game_time(game)
        if start is not None and end is not None:
            for a in self.store.assignments_for_official(official_id):
                if a.game_id == game_id or not a.status.is_active:
                    continue
                other = self.store.get_game(a.game_id)
                if other is None or other.cancelled:
                    continue
                o_start, o_end = self._game_time(other)
                if o_start is not None and o_end is not None \
                        and start < o_end and end > o_start:
                    raise ScheduleConflictError(
                        f"{official.name} is already officiating an overlapping game.")

        # Declared-unavailable window (#88): block unless the operator overrides.
        unavail = self.unavailable_window(official_id, start, end)
        if unavail is not None and not override_unavailable:
            raise ScheduleConflictError(
                f"{official.name} is marked unavailable at this time.",
                details={"reason": "official_unavailable",
                         "availability_id": unavail.id,
                         "note": unavail.note})

        now = self.clock()
        assignment = OfficialAssignment(
            id=self.store.next_id("assign"),
            game_id=game_id, official_id=official_id, role=role,
            status=OfficialAssignmentStatus.PROPOSED,
            assigned_at=now, assigned_by=actor_id,
        )
        self.store.add_official_assignment(assignment)
        self._audit("official_assigned", "official_assignment", assignment.id,
                    actor_id, {"game_id": game_id, "official_id": official_id,
                               "role": role.value})
        self._notify(
            NotificationKind.ASSIGNMENT_OFFERED, NotificationAudience.OFFICIAL,
            "New game assignment",
            f"You've been assigned as {role.value} for {self._matchup(game)}.",
            audience_ref=official_id, game_id=game_id, assignment_id=assignment.id)
        return assignment

    @_transactional
    def respond_assignment(self, assignment_id: str, accept: bool,
                           actor_id: Optional[str] = None) -> OfficialAssignment:
        """An official accepts or declines a proposed assignment."""
        a = self.store.get_official_assignment(assignment_id)
        if a is None:
            raise NotFoundError(f"Assignment {assignment_id} not found.")
        self._guard_game_season(self.store.get_game(a.game_id))  # #159
        # #159 r15 — re-fetch under the Season lock; a concurrent unassign
        # (removes the row) or a second respond commits first, so the stale
        # PROPOSED check would double-transition or resurrect it otherwise.
        a = self.store.get_official_assignment(assignment_id)
        if a is None:
            raise NotFoundError(f"Assignment {assignment_id} not found.")
        if a.status != OfficialAssignmentStatus.PROPOSED:
            raise ValidationError("Only a proposed assignment can be responded to.")
        a.status = (OfficialAssignmentStatus.ACCEPTED if accept
                    else OfficialAssignmentStatus.DECLINED)
        a.responded_at = self.clock()
        self.store.save_official_assignment(a)
        self._audit("assignment_accepted" if accept else "assignment_declined",
                    "official_assignment", a.id, actor_id)
        official = self.store.get_official(a.official_id)
        name = official.name if official else "An official"
        game = self.store.get_game(a.game_id)
        matchup = self._matchup(game) if game else "a game"
        self._notify(
            NotificationKind.ASSIGNMENT_ACCEPTED if accept
            else NotificationKind.ASSIGNMENT_DECLINED,
            NotificationAudience.SCHEDULER,
            f"Assignment {'accepted' if accept else 'declined'}",
            f"{name} {'accepted' if accept else 'declined'} the {a.role.value} "
            f"assignment for {matchup}.",
            game_id=a.game_id, assignment_id=a.id)
        return a

    @_transactional
    def unassign_official(self, assignment_id: str,
                          actor_id: Optional[str] = None) -> OfficialAssignment:
        """Remove an official assignment from a game entirely."""
        a = self.store.get_official_assignment(assignment_id)
        if a is None:
            raise NotFoundError(f"Assignment {assignment_id} not found.")
        self._guard_game_season(self.store.get_game(a.game_id))  # #159
        # #201 — re-fetch under the Season lock; a concurrent unassign that
        # already removed the row makes this loser a clean not-found, with no
        # duplicate official_unassigned audit/notification for a row that's gone.
        a = self.store.get_official_assignment(assignment_id)
        if a is None:
            raise NotFoundError(f"Assignment {assignment_id} not found.")
        self.store.remove_official_assignment(assignment_id)
        self._audit("official_unassigned", "official_assignment", assignment_id,
                    actor_id, {"game_id": a.game_id, "official_id": a.official_id})
        # Tell the official their assignment was removed (#87).
        game = self.store.get_game(a.game_id)
        label = self._game_label(game) if game else "a game"
        self._notify(NotificationKind.ASSIGNMENT_UNASSIGNED,
                     NotificationAudience.OFFICIAL, "Assignment removed",
                     f"You are no longer assigned to {label}.",
                     audience_ref=a.official_id, game_id=a.game_id)
        return a

    # -- results (#31) -----------------------------------------------------
    @_transactional
    def record_result(self, game_id: str, home_score: int, away_score: int,
                      actor_id: Optional[str] = None) -> GameResult:
        """Enter (or re-enter) a score as a DRAFT result. Approval finalizes it."""
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.")
        self._guard_game_season(game)  # #159 read-only guard
        # #201 — re-fetch under the Season lock; a concurrent cancel_game commits
        # under the same lock, so the cancelled gate must see the fresh Game.
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.")
        if game.cancelled:
            raise ValidationError("Cannot record a result for a cancelled game.")
        hs, as_ = self._require_score(home_score), self._require_score(away_score)
        result = self.store.result_for_game(game_id)
        if result is not None and result.status == ResultStatus.FINAL:
            raise ValidationError(
                "Result is already final. It can't be edited without reopening.")
        now = self.clock()
        if result is None:
            result = GameResult(id=self.store.next_id("result"), game_id=game_id,
                                home_score=hs, away_score=as_,
                                status=ResultStatus.DRAFT,
                                recorded_by=actor_id, recorded_at=now)
            self.store.add_game_result(result)
        else:
            result.home_score, result.away_score = hs, as_
            result.recorded_by, result.recorded_at = actor_id, now
            self.store.save_game_result(result)
        self._audit("result_recorded", "game_result", result.id, actor_id,
                    {"game_id": game_id, "home_score": hs, "away_score": as_})
        return result

    @_transactional
    def approve_result(self, game_id: str,
                       actor_id: Optional[str] = None) -> GameResult:
        """Approve a draft result → FINAL. Only a FINAL result affects standings."""
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.")
        self._guard_game_season(game)  # #159 read-only guard
        # #201 — re-fetch under the Season lock; a concurrent cancel_game commits
        # under the same lock, so the cancelled gate must see the fresh Game.
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.")
        if game.cancelled:
            raise ValidationError("Cannot approve a result for a cancelled game.")
        result = self.store.result_for_game(game_id)
        if result is None:
            raise NotFoundError("No result recorded for this game yet.")
        if result.status == ResultStatus.FINAL:
            raise ValidationError("Result is already final.")
        result.status = ResultStatus.FINAL
        result.approved_by = actor_id
        result.approved_at = self.clock()
        self.store.save_game_result(result)
        self._audit("result_approved", "game_result", result.id, actor_id,
                    {"game_id": game_id})
        self._notify(
            NotificationKind.RESULT_APPROVED, NotificationAudience.PUBLIC,
            "Final result",
            f"Final: {self._matchup(game)} — {result.home_score}–{result.away_score}.",
            game_id=game_id)
        return result

    def _require_score(self, value) -> int:
        try:
            score = int(value)
        except (TypeError, ValueError):
            raise ValidationError("Score must be a whole number.")
        if score < 0:
            raise ValidationError("Score can't be negative.")
        return score

    # -- listings ----------------------------------------------------------
    def list_programs(self) -> List[Program]:
        return list(self.store.all_programs())

    def list_seasons(self, program_id: str) -> List[Season]:
        return self.store.seasons_for_program(program_id)

    def list_divisions(self, season_id: str) -> List[Division]:
        return self.store.divisions_for_season(season_id)

    def record_demo_event(self, action: str,
                          actor_id: Optional[str] = None) -> SetupAuditLog:
        """Audit a demo-lifecycle event (#215) — ``demo_reset``/``demo_loaded``/
        ``demo_cleared`` — once the new dataset exists.

        Written by DemoState.reset after the (re)build, inside the same atomic
        unit for a SQL store, so the action and its audit row commit together.
        Not ``@_transactional``: the caller already owns the transaction.
        """
        return self._audit(action, "demo", "demo", actor_id,
                           {"at": self.clock().isoformat()})

    # -- safe destructive deletion (#215) ---------------------------------
    # Every delete runs a *pre-write* dependency gate: if any dependent record
    # or history exists it raises HasDependenciesError with a structured
    # breakdown and writes nothing. The gate must precede the first write
    # because the in-memory store's transaction is a lock, not a rollback, so a
    # mid-delete abort would not undo anything. Deletion is never a silent
    # cascade — the operator must clear the dependents first. All eight routed
    # under /api/setup (MANAGE_SETUP → League Admin only), each audited with the
    # server-resolved actor id.
    _DEP_SAMPLE = 8  # cap sample names per dependency group in the error detail

    def _dep_group(self, label: str, items: list, name_fn, id_fn=None,
                   display: Optional[str] = None) -> dict:
        """One dependency group for a blocked delete.

        Carries the full ``count`` plus a capped list of ``items`` — each an
        ``{id, name}`` pair — so the UI can locate a specific blocker even when
        names collide (#215 review 4). ``id_fn`` defaults to the record's ``id``
        attribute; pass one when the blocker's identifier lives elsewhere.
        ``names`` is retained as a convenience mirror of the item names.

        ``type`` is the frozen structured code (e.g. ``league``/``level``);
        ``display`` is the human-facing noun rendered in the message and UI
        (#233 — e.g. ``program``/``league``). It defaults to ``type`` so groups
        whose display noun is unchanged need no extra argument.
        """
        id_of = id_fn or (lambda x: getattr(x, "id", None))
        sample = items[:self._DEP_SAMPLE]
        pairs = [{"id": id_of(x), "name": name_fn(x)} for x in sample]
        return {"type": label, "display": display or label, "count": len(items),
                "items": pairs, "names": [p["name"] for p in pairs]}

    def _block_if_dependents(self, entity_type: str, entity_id: str,
                             entity_label: str, groups: list) -> None:
        groups = [g for g in groups if g["count"]]
        if not groups:
            return
        total = sum(g["count"] for g in groups)
        parts = ", ".join(f"{g['count']} {g.get('display', g['type'])}"
                          f"{'' if g['count'] == 1 else 's'}" for g in groups)
        raise HasDependenciesError(
            f"Can't delete this {entity_label} — {total} dependent "
            f"record(s) still exist ({parts}). Remove them first.",
            details={"entity_type": entity_type, "entity_id": entity_id,
                     "dependencies": groups})

    # -- facility-hierarchy delete: itemised block on the concurrency race -----
    # The venue/rink/ice-slot deletes take no row lock (#201 Slice 3 is FK-only),
    # so a dependent CREATE that commits in the pre-check→delete window is invisible
    # to _block_if_dependents and only the incoming foreign key stops the orphaning
    # delete. The store signals that as a DependentDeleteConflict and — from the
    # OUTERMOST transaction()'s post-rollback handler, once the connection is clean
    # again — calls _resolve_dependent_delete_conflict below. We re-resolve the
    # now-committed dependents on a fresh read and raise the SAME itemised
    # HasDependenciesError the pre-check raises, so the operator gets identical
    # dependency groups/counts/ids whether the dependent was present up front or
    # landed during the race, never a thin error. Deferring to the outermost
    # rollback is essential: when the delete is nested inside a caller's
    # transaction() the connection stays transaction-aborted until that outer unit
    # rolls back, so re-scanning any earlier would raise InFailedSqlTransaction and
    # poison the caller's atomic unit.
    def _venue_dependent_groups(self, venue_id: str) -> list:
        rinks = [r for r in self.store.all_rinks() if r.venue_id == venue_id]
        venue_access = self.store.season_venue_access_for_venue(venue_id)
        return [self._dep_group("rink", rinks, lambda r: r.name),
                self._dep_group("venue access", venue_access,
                                lambda a: self._season_name(a.season_id))]

    def _rink_dependent_groups(self, rink_id: str) -> list:
        slots = [s for s in self.store.all_ice_slots() if s.rink_id == rink_id]
        return [self._dep_group("ice slot", slots, self._slot_label)]

    def _ice_slot_dependent_groups(self, slot_id: str) -> list:
        games = [g for g in self.store.all_games() if g.ice_slot_id == slot_id]
        return [self._dep_group("game", games, self._matchup)]

    # #201 Slice 4 — Program/Organization + Venue-owner ownership integrity. The
    # organization/program deletes take no row lock (FK-only, mirroring the
    # facility deletes above): a child CREATE that commits in the
    # pre-check→delete window is invisible to _block_if_dependents and only the
    # incoming foreign key stops the orphaning delete. Same shared machinery — the
    # store signals DependentDeleteConflict, the outermost transaction()
    # post-rollback handler re-resolves via these builders and raises the SAME
    # itemised HasDependenciesError the pre-check raises.
    def _organization_dependent_groups(self, org_id: str) -> list:
        programs = [p for p in self.store.all_programs()
                    if p.operator_organization_id == org_id]
        venues = [v for v in self.store.all_venues()
                  if v.organization_id == org_id]
        return [self._dep_group("league", programs, lambda p: p.name,
                                display="program"),
                self._dep_group("venue", venues, lambda v: v.name)]

    def _program_dependent_groups(self, program_id: str) -> list:
        seasons = self.store.seasons_for_program(program_id)
        leagues = [lg for lg in self.store.all_leagues()
                   if lg.program_id == program_id]
        teams = self.store.teams_for_program(program_id)
        venues = [v for v in self.store.all_venues()
                  if v.league_id == program_id]
        return [self._dep_group("season", seasons, lambda s: s.name),
                self._dep_group("level", leagues, lambda lg: lg.name,
                                display="league"),
                self._dep_group("team", teams, lambda t: t.name),
                self._dep_group("venue", venues, lambda v: v.name)]

    # Store entity_type (from DependentDeleteConflict) → (details entity_type,
    # itemised-block label, dependent-groups resolver). The details entity_type
    # matches the pre-check's exactly, so the race loser and the up-front block
    # carry byte-identical HasDependenciesError.details. (delete_program's
    # pre-check entity_type is the legacy "league", from before #233 renamed
    # League → Program.)
    _DEPENDENT_DELETE_SPECS = {
        "venue": ("venue", "venue", "_venue_dependent_groups"),
        "rink": ("rink", "rink", "_rink_dependent_groups"),
        "ice_slot": ("ice slot", "ice slot", "_ice_slot_dependent_groups"),
        "organization": ("organization", "facility owner",
                         "_organization_dependent_groups"),
        "program": ("league", "program", "_program_dependent_groups"),
    }

    def _resolve_dependent_delete_conflict(self, conflict) -> None:
        """Store callback, invoked from the outermost transaction()'s
        post-rollback handler when a no-row-lock facility delete lost the FK race
        (#201 Slice 3). Runs on a clean connection, so the fresh dependent scan
        is safe — including when the delete was nested inside a caller's
        transaction() (the outer unit has fully rolled back, zero partial state).

        Raises the SAME itemised has-dependencies error the pre-check raises. If
        the dependent was created then removed again before this re-scan, there is
        nothing to itemise, so it raises a stable, retryable conflict rather than
        leaking the internal signal."""
        spec = self._DEPENDENT_DELETE_SPECS.get(conflict.entity_type)
        if spec is None:  # not a facility delete we itemise — let it propagate
            return
        entity_type, entity_label, groups_attr = spec
        entity_id = conflict.entity_id
        self._block_if_dependents(entity_type, entity_id, entity_label,
                                  getattr(self, groups_attr)(entity_id))
        raise ConcurrencyConflictError(
            "The delete could not complete because of concurrent activity. "
            "Please retry.",
            details={"reason": "concurrent_dependent_change",
                     "entity_type": entity_type, "entity_id": entity_id})

    def _team_name(self, team_id) -> str:
        team = self.store.get_team(team_id) if team_id else None
        return team.name if team else (team_id or "—")

    def _season_name(self, season_id) -> str:
        season = self.store.get_season(season_id) if season_id else None
        return season.name if season else (season_id or "—")

    def _venue_name(self, venue_id) -> str:
        venue = self.store.get_venue(venue_id) if venue_id else None
        return venue.name if venue else (venue_id or "—")

    def _slot_label(self, slot) -> str:
        rink = self.store.get_rink(slot.rink_id) if slot.rink_id else None
        when = slot.start_time.isoformat() if slot.start_time else ""
        return f"{rink.name if rink else 'ice slot'} {when}".strip()

    @_transactional
    def delete_organization(self, org_id: str,
                            actor_id: Optional[str] = None) -> Organization:
        # No row lock (#201 Slice 4, FK-only like the facility deletes): the
        # incoming programs.operator_organization_id / venues.organization_id
        # foreign keys backstop the create-child-vs-delete race, and the
        # itemised block is re-resolved from the outermost transaction's
        # post-rollback handler when the child commits first.
        org = self.store.get_organization(org_id)
        if org is None:
            raise NotFoundError(f"Organization {org_id} not found.")
        self._block_if_dependents("organization", org_id, "facility owner",
                                  self._organization_dependent_groups(org_id))
        self.store.delete_organization(org_id)
        self._audit("organization_deleted", "organization", org_id, actor_id,
                    {"name": org.name})
        return org

    @_transactional
    def delete_league(self, league_id: str, actor_id: Optional[str] = None) -> League:
        # #159 — lock the League row so a concurrent Team create/rebind
        # (create_team / transfer_team_to_league, which take the same lock)
        # serializes against this dependency scan: a Team can't be bound to the
        # League between the scan and the delete, and vice-versa.
        league = self.store.get_league_for_update(league_id)
        if league is None:
            raise NotFoundError(f"League {league_id} not found.")
        # #283: a Division/registration no longer stores league_id directly —
        # both hang off the League's LeagueSeasons. Resolve this League's
        # LeagueSeason bindings and find dependents through them.
        ls_rows = self.store.league_seasons_for_league(league_id)
        ls_ids = {ls.id for ls in ls_rows}
        # #159 — a permanent League participates in a Season only through its
        # LeagueSeason bindings. Lock every distinct Season it touches, in
        # canonical sorted order, and fail closed if ANY is archived: deleting
        # the League would drop that archived Season's LeagueSeason (and any
        # Game) history, changing the archived hierarchy after the read-only
        # linearization point. The lock serializes this against a concurrent
        # archive on the same Season row.
        for sid in sorted({ls.season_id for ls in ls_rows if ls.season_id}):
            self._require_active_season(sid)
        divisions = [d for d in self.store.all_divisions()
                     if d.league_season_id in ls_ids]
        # #233 B2b review r2: a registration's league is REQUIRED in v2 and can
        # point directly at this League with no Division (division-less
        # participation) — checking only Divisions as dependents let a League
        # delete silently orphan such a registration. Mirrors delete_division's
        # own registration check just below.
        regs = [r for r in self.store.all_season_team_registrations()
                if r.league_season_id in ls_ids]
        # #159 — Games reference this League by its LeagueSeason (or, for legacy
        # rows, its league_id). Historical Game-backed participation must not be
        # silently dropped: a Game blocks the delete so the operator resolves it
        # first (its owning Season, if archived, has already failed above).
        games = [g for g in self.store.all_games()
                 if g.league_season_id in ls_ids or g.league_id == league_id]
        # #159 — a permanent Team references exactly one League (Team.league_id,
        # #283 rule 3). Deleting the League would orphan those Teams, so they are
        # explicit dependents (there is no FK to catch this at the DB layer).
        teams = [t for t in self.store.all_teams()
                 if t.league_id == league_id]
        # #159 — a LeagueSeason binding is itself a dependent, NOT something this
        # delete may silently cascade away: the destructive-delete contract is
        # dependency-gated, itemized blockers with no implicit cascades. An
        # operator must remove each binding through its own authorized path
        # before the League can be deleted. Archived bindings have already
        # failed above with season_archived.
        self._block_if_dependents("level", league_id, "league", [
            self._dep_group("division", divisions, lambda d: d.name),
            self._dep_group("team registration", regs,
                            lambda r: self._team_name(r.team_id)),
            self._dep_group("game", games, self._matchup),
            self._dep_group("team", teams, lambda t: t.name),
            self._dep_group("season binding", ls_rows,
                            lambda ls: self._season_name(ls.season_id))])
        self.store.delete_league(league_id)
        self._audit("level_deleted", "level", league_id, actor_id,
                    {"name": league.name, "program_id": league.program_id})
        return league

    @_transactional
    def delete_program(self, program_id: str, actor_id: Optional[str] = None) -> Program:
        program = self.store.get_program(program_id)
        if program is None:
            raise NotFoundError(f"Program {program_id} not found.")
        # #201 Slice 4: seasons.program_id / leagues.program_id / venues.league_id
        # now have foreign keys onto programs, so a permanent League is a real
        # dependent (added to the itemised block below, no longer silently
        # orphaned). The delete takes no row lock; the incoming FKs backstop the
        # create-child-vs-delete race and the block is re-resolved on the
        # post-rollback path.
        self._block_if_dependents("league", program_id, "program",
                                  self._program_dependent_groups(program_id))
        self.store.delete_program(program_id)
        self._audit("league_deleted", "league", program_id, actor_id,
                    {"name": program.name})
        return program

    @_transactional
    def delete_season(self, season_id: str, actor_id: Optional[str] = None) -> Season:
        # Lock the Season row (#159) so this serializes against a concurrent
        # archive on the same row.
        season = self.store.get_season_for_update(season_id)
        if season is None:
            raise NotFoundError(f"Season {season_id} not found.")
        # #159 — an archived Season is read-only history and must retain it:
        # deleting it (even when empty) would destroy that history, so it fails
        # closed through the same active-season guard before any dependent scan
        # or write. Reopen it first if it genuinely needs removing.
        self._require_active_season(season_id)
        # #283: leagues are permanent; those participating in this Season are its
        # LeagueSeasons' leagues.
        levels = [lg for lg in (self.store.get_league(ls.league_id)
                                for ls in
                                self.store.league_seasons_for_season(season_id))
                  if lg is not None]
        divisions = self.store.divisions_for_season(season_id)
        regs = self.store.registrations_for_season(season_id)
        # Games/results/history reference the season directly (#215): a game
        # whose division is legacy/null/mismatched still carries season_id, so
        # check by season_id rather than trusting the division tree above.
        games = [g for g in self.store.all_games() if g.season_id == season_id]
        # SeasonVenueAccess (#233 Slice E, reviewer blocker on #255): checked
        # regardless of active status, mirroring team registrations above —
        # revoke_season_venue_access deliberately only deactivates a row (the
        # grant/revoke history is preserved), so an inactive row is not proof
        # the Season is free of it. delete_season_venue_access (mirroring
        # #251's delete_season_team_registration) is the explicit cleanup an
        # operator runs on each revoked row before this delete can succeed.
        venue_access = self.store.season_venue_access_for_season(season_id)
        self._block_if_dependents("season", season_id, "season", [
            self._dep_group("level", levels, lambda lv: lv.name,
                            display="league"),
            self._dep_group("division", divisions, lambda d: d.name),
            self._dep_group("team registration", regs,
                            lambda r: self._team_name(r.team_id)),
            self._dep_group("game", games, self._matchup),
            self._dep_group("venue access", venue_access,
                            lambda a: self._venue_name(a.venue_id))])
        self.store.delete_season(season_id)
        self._audit("season_deleted", "season", season_id, actor_id,
                    {"name": season.name, "league_id": season.program_id})
        return season

    @_transactional
    def delete_division(self, division_id: str,
                        actor_id: Optional[str] = None) -> dict:
        division = self.store.get_division(division_id)
        if division is None:
            raise NotFoundError(f"Division {division_id} not found.")
        _dsid = self._season_of_league_season(division.league_season_id)  # #159
        if _dsid:
            self._require_active_season(_dsid)  # read-only guard
        regs = [r for r in self.store.all_season_team_registrations()
                if r.division_id == division_id]
        games = [g for g in self.store.all_games() if g.division_id == division_id]
        # Deletion keys off real operational dependents only (#180, #233 D1
        # bundled fix): an ACTIVE registration or any Game blocks deletion. An
        # INACTIVE registration (the team was removed from the season via
        # unregister_team_from_season, which deliberately retains division_id
        # for history) is not a live dependent — the Setup UI can show 0 teams
        # under a Division while these hidden rows still exist, so they must
        # not silently block deletion forever. A stale legacy
        # Team.division_id pointer is no longer read operationally, so it
        # never blocks a division delete either.
        active_regs = [r for r in regs if r.active]
        inactive_regs = [r for r in regs if not r.active]
        self._block_if_dependents("division", division_id, "division", [
            self._dep_group("team registration", active_regs,
                            lambda r: self._team_name(r.team_id)),
            self._dep_group("game", games, self._matchup)])
        # Clear the inactive registrations' division_id (never a hard delete —
        # the row, its Season/Team/League identity, and its active=False
        # status are all retained) before removing the Division itself. Each
        # cleanup is audited individually against its own registration (the
        # old→new convention used elsewhere, e.g. season_team_league_assigned)
        # so every affected row has its own traceable audit entry.
        for reg in inactive_regs:
            old_division_id = reg.division_id
            reg.division_id = None
            self.store.save_season_team_registration(reg)
            self._audit("season_team_division_cleared",
                        "season_team_registration", reg.id, actor_id,
                        {"from": old_division_id, "to": None,
                         "reason": "division_deleted"})
        # #283: a Division's Season and League resolve via its LeagueSeason.
        _dls = self.store.get_league_season(division.league_season_id)
        _dls_season_id = _dls.season_id if _dls else None
        _dls_league_id = _dls.league_id if _dls else None
        self.store.delete_division(division_id)
        self._audit("division_deleted", "division", division_id, actor_id,
                    {"name": division.name, "season_id": _dls_season_id,
                     "inactive_registrations_cleaned": len(inactive_regs)})
        return {"id": division.id, "season_id": _dls_season_id,
                "name": division.name, "age_group": division.age_group,
                "league_id": _dls_league_id,
                "external_ref": division.external_ref,
                "inactive_registrations_cleaned": len(inactive_regs)}

    @_transactional
    def delete_club(self, club_id: str, actor_id: Optional[str] = None) -> Club:
        # Row-lock the club before scanning its dependent teams (#201 Slice 2,
        # mirroring delete_team's #266 lock): a concurrent create_team /
        # assign_team_club whose write takes the FK key-share lock on this club
        # row serializes against this FOR UPDATE, so either the team is already
        # bound and this delete sees it and blocks, or this delete commits first
        # and the team write then fails the club_id → clubs(id) foreign key
        # (club_not_found). Without the lock the two could interleave between the
        # dependent scan and the delete and surface a raw FK violation on the
        # losing delete instead of the stable has-dependencies block.
        club = self.store.get_club_for_update(club_id)
        if club is None:
            raise NotFoundError(f"Club {club_id} not found.")
        teams = [t for t in self.store.all_teams() if t.club_id == club_id]
        self._block_if_dependents("club", club_id, "club", [
            self._dep_group("team", teams, lambda t: t.name)])
        self.store.delete_club(club_id)
        self._audit("club_deleted", "club", club_id, actor_id, {"name": club.name})
        return club

    @_transactional
    def delete_team(self, team_id: str, actor_id: Optional[str] = None) -> Team:
        # Row-lock the team before scanning its dependents (#266 review): a
        # concurrent coach-account creation locks the same row first, so the two
        # serialize — either the coach is already inserted and blocks this
        # delete, or this delete commits first and the create then sees the team
        # gone. Without the lock, both could pass their checks and orphan a coach
        # against a deleted team under READ COMMITTED.
        team = self.store.get_team_for_update(team_id)
        if team is None:
            raise NotFoundError(f"Team {team_id} not found.")
        regs = [r for r in self.store.all_season_team_registrations()
                if r.team_id == team_id]
        games = [g for g in self.store.all_games()
                 if team_id in (g.home_team_id, g.away_team_id)]
        players = self.store.players_for_team(team_id)
        # Live scoped/integration state also references a Team directly (#215
        # review 4): a coach account bound to it, a live team calendar feed, and
        # any team-targeted contact/preference/device rows. Deleting the Team
        # while these exist would strand an active identity or integration
        # pointing at a missing record, so they block the delete too. (This is a
        # block, not a silent cascade or account erasure — the latter is an
        # explicit non-goal of #215.)
        accounts = [a for a in self.store.all_user_accounts()
                    if (a.scope or {}).get("team_id") == team_id]
        feeds = [t for t in self.store.all_calendar_feed_tokens()
                 if t.actor_type == "team" and t.actor_ref == team_id
                 and t.revoked_at is None]
        contacts = [c for c in self.store.all_contact_destinations()
                    if c.recipient_ref == team_id]
        prefs = [p for p in self.store.all_notification_preferences()
                 if p.recipient_ref == team_id]
        devices = [d for d in self.store.all_device_tokens()
                   if d.recipient_ref == team_id]
        self._block_if_dependents("team", team_id, "team", [
            self._dep_group("season registration", regs,
                            lambda r: self._season_name(  # #283: via LeagueSeason
                                self._season_of_league_season(
                                    r.league_season_id))),
            self._dep_group("game", games, self._matchup),
            self._dep_group("player", players, lambda p: p.name),
            self._dep_group("account", accounts, lambda a: a.username),
            self._dep_group("calendar feed", feeds,
                            lambda t: t.label or t.actor_ref),
            self._dep_group("contact destination", contacts,
                            lambda c: c.label or c.destination),
            self._dep_group("notification preference", prefs,
                            lambda p: p.channel.value),
            self._dep_group("device token", devices,
                            lambda d: d.label or d.provider)])
        self.store.delete_team(team_id)
        self._audit("team_deleted", "team", team_id, actor_id,
                    {"name": team.name, "league_id": team.program_id})
        return team

    @_transactional
    def delete_official(self, official_id: str,
                        actor_id: Optional[str] = None) -> Official:
        """Permanently remove an Official (#232), gated on any live
        assignment, availability window, account, or integration state —
        the same pre-write, no-cascade contract as `delete_team`. Never
        deletes, erases, or silently cascades any dependent row (#232 review
        4): a blocked contact destination or notification preference is
        cleared through its own explicit, audited retire action
        (`set_contact_destination_active`/`set_notification_preference_active`,
        ``active=False``) — mirroring account/device-token deactivation
        below — which preserves the stored row and its opt-out history
        rather than deleting it. Only an ACTIVE row counts as a live
        dependency, exactly like an active account/device token.
        """
        # Row-lock the Official before the account scan (#282 review): account
        # create/rebind/reactivation take the same lock while resolving the
        # official subject, so the two serialize. Without it, on PostgreSQL a
        # concurrent bind could commit an active account between this scan and
        # the delete and strand a live login against a deleted official.
        official = self.store.get_official_for_update(official_id)
        if official is None:
            raise NotFoundError(f"Official {official_id} not found.")
        assignments = self.store.assignments_for_official(official_id)
        availability = self.store.availability_for_official(official_id)
        # Only an ACTIVE account/device token/contact/preference is a live
        # pointer (#232 review): each already has a supported
        # deactivation/retire route that resolves this dependency without
        # needing a new one. An inactive row is inert history, exactly like
        # a revoked calendar feed below.
        accounts = [a for a in self.store.all_user_accounts()
                    if a.active and (a.scope or {}).get("official_id") == official_id]
        feeds = [t for t in self.store.all_calendar_feed_tokens()
                 if t.actor_type == "official" and t.actor_ref == official_id
                 and t.revoked_at is None]
        ref = f"official:{official_id}"
        contacts = [c for c in self.store.all_contact_destinations()
                    if c.active and c.recipient_ref == ref]
        prefs = [p for p in self.store.all_notification_preferences()
                 if p.active and p.recipient_ref == ref]
        devices = [d for d in self.store.all_device_tokens()
                   if d.active and d.recipient_ref == ref]
        self._block_if_dependents("official", official_id, "official", [
            self._dep_group("assignment", assignments, self._matchup_for_game_ref),
            self._dep_group("availability window", availability,
                            lambda a: a.status.value if a.status else a.id),
            self._dep_group("account", accounts, lambda a: a.username),
            self._dep_group("calendar feed", feeds,
                            lambda t: t.label or t.actor_ref),
            self._dep_group("contact destination", contacts,
                            lambda c: c.label or c.destination),
            self._dep_group("notification preference", prefs,
                            lambda p: p.channel.value),
            self._dep_group("device token", devices,
                            lambda d: d.label or d.provider)])
        self.store.delete_official(official_id)
        self._audit("official_deleted", "official", official_id, actor_id,
                    {"name": official.name})
        return official

    @_transactional
    def delete_player(self, player_id: str,
                      actor_id: Optional[str] = None) -> Player:
        """Permanently remove a Player (#232), gated on any live roster
        entry, availability response, substitute enrolment, guardian link,
        account, or integration state — the same pre-write, no-cascade
        contract as `delete_team`. See `delete_official`'s docstring: a
        blocked contact destination or notification preference is cleared
        by retiring it (``active=False``), never by deleting it (#232
        review 4).
        """
        # Row-lock the Player before the account scan (#282 review): account
        # create/rebind/reactivation take the same lock while resolving the
        # player subject, so the two serialize. Without it, on PostgreSQL a
        # concurrent bind could commit an active account between this scan and
        # the delete and strand a live login against a deleted player.
        player = self.store.get_player_for_update(player_id)
        if player is None:
            raise NotFoundError(f"Player {player_id} not found.")
        rosters = self.store.roster_entries_for_player(player_id)
        availability = self.store.availability_entries_for_player(player_id)
        subs = self.store.substitute_enrollments_for_player(player_id)
        guardian_links = self.store.guardian_links_for_player(player_id)
        # Only an ACTIVE account/device token/contact/preference is a live
        # pointer (#232 review) — see delete_official's identical comment.
        accounts = [a for a in self.store.all_user_accounts()
                    if a.active and (a.scope or {}).get("player_id") == player_id]
        feeds = [t for t in self.store.all_calendar_feed_tokens()
                 if t.actor_type == "player" and t.actor_ref == player_id
                 and t.revoked_at is None]
        ref = f"player:{player_id}"
        contacts = [c for c in self.store.all_contact_destinations()
                    if c.active and c.recipient_ref == ref]
        prefs = [p for p in self.store.all_notification_preferences()
                 if p.active and p.recipient_ref == ref]
        devices = [d for d in self.store.all_device_tokens()
                   if d.active and d.recipient_ref == ref]
        self._block_if_dependents("player", player_id, "player", [
            self._dep_group("roster entry", rosters, self._matchup_for_game_ref),
            self._dep_group("availability response", availability,
                            self._matchup_for_game_ref),
            self._dep_group("substitute enrolment", subs,
                            self._matchup_for_game_ref),
            self._dep_group("guardian link", guardian_links,
                            lambda g: g.guardian_user_id),
            self._dep_group("account", accounts, lambda a: a.username),
            self._dep_group("calendar feed", feeds,
                            lambda t: t.label or t.actor_ref),
            self._dep_group("contact destination", contacts,
                            lambda c: c.label or c.destination),
            self._dep_group("notification preference", prefs,
                            lambda p: p.channel.value),
            self._dep_group("device token", devices,
                            lambda d: d.label or d.provider)])
        self.store.delete_player(player_id)
        self._audit("player_deleted", "player", player_id, actor_id,
                    {"name": player.name, "team_id": player.team_id})
        return player

    def _matchup_for_game_ref(self, entry) -> str:
        game = self.store.get_game(entry.game_id)
        return self._matchup(game) if game else entry.game_id

    @_transactional
    def delete_venue(self, venue_id: str, actor_id: Optional[str] = None) -> Venue:
        venue = self.store.get_venue(venue_id)
        if venue is None:
            raise NotFoundError(f"Venue {venue_id} not found.")
        # SeasonVenueAccess (#233 Slice E, reviewer blocker on #255): checked
        # regardless of active status — see delete_season's identical
        # comment; delete_season_venue_access is the matching cleanup op.
        self._block_if_dependents("venue", venue_id, "venue",
                                  self._venue_dependent_groups(venue_id))
        self.store.delete_venue(venue_id)
        self._audit("venue_deleted", "venue", venue_id, actor_id,
                    {"name": venue.name})
        return venue

    @_transactional
    def delete_rink(self, rink_id: str, actor_id: Optional[str] = None) -> Rink:
        rink = self.store.get_rink(rink_id)
        if rink is None:
            raise NotFoundError(f"Rink {rink_id} not found.")
        self._block_if_dependents("rink", rink_id, "rink",
                                  self._rink_dependent_groups(rink_id))
        self.store.delete_rink(rink_id)
        self._audit("rink_deleted", "rink", rink_id, actor_id,
                    {"name": rink.name, "venue_id": rink.venue_id})
        return rink

    @_transactional
    def delete_ice_slot(self, slot_id: str,
                        actor_id: Optional[str] = None) -> IceSlot:
        slot = self.store.get_ice_slot(slot_id)
        if slot is None:
            raise NotFoundError(f"Ice slot {slot_id} not found.")
        # Only an UNUSED, FUTURE, still-AVAILABLE slot may be deleted (#215).
        # A game referencing the slot is a true dependency (report it first, with
        # counts/ids). Otherwise past inventory is history and an
        # allocated/blocked/maintenance slot is in use — neither is a free future
        # opening; those are state rules that raise a plain validation error.
        # Every path is zero-write.
        self._block_if_dependents("ice slot", slot_id, "ice slot",
                                  self._ice_slot_dependent_groups(slot_id))
        if slot.start_time is not None and slot.start_time <= self.clock():
            raise ValidationError(
                "Only a future ice slot can be deleted; past slots are history.")
        if slot.status != IceSlotStatus.AVAILABLE:
            raise ValidationError(
                "Only an available ice slot can be deleted; this slot is "
                f"{slot.status.value} (in use or reserved).")
        self.store.delete_ice_slot(slot_id)
        self._audit("ice_slot_deleted", "ice_slot", slot_id, actor_id,
                    {"rink_id": slot.rink_id})
        return slot

    @_transactional
    def delete_game(self, game_id: str, actor_id: Optional[str] = None) -> Game:
        """Delete a scheduler *draft* game only (#215).

        Only a true draft (``is_draft`` True, not published, not cancelled, no
        result) may be hard-deleted. A manually created or committed game — even
        an unpublished one — is NOT a draft and must be cancelled instead so its
        fixture and notification history survive. A clean draft carries no
        operational work; if any roster entry, official assignment, availability
        response, substitute request, or reschedule request exists, deletion is
        blocked rather than silently orphaning it. On deletion the draft's
        allocated ice slot is released back to AVAILABLE so it can be rebooked.
        """
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.")
        self._guard_game_season(game)  # #159 read-only guard
        # #159 r15 — re-fetch under the Season lock; a concurrent publish_game/
        # move_game commits under the same lock, so decide draft-eligibility and
        # release the slot from the FRESH row (never delete a just-published game
        # or free the wrong old slot).
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.")
        if not getattr(game, "is_draft", False) or game.published \
                or game.cancelled \
                or self.store.result_for_game(game_id) is not None:
            raise ValidationError(
                "Only a draft game can be deleted; a scheduled, published, or "
                "historical game must be cancelled instead.")
        roster = self.store.roster_for_game(game_id)
        assignments = self.store.assignments_for_game(game_id)
        availability = self.store.availability_for_game(game_id)
        substitutes = self.store.substitutes_for_game(game_id)
        reschedules = self.store.reschedule_requests_for_game(game_id)
        self._block_if_dependents("game", game_id, "draft game", [
            self._dep_group("roster entry", roster,
                            lambda e: getattr(e, "player_id", "player")),
            self._dep_group("official assignment", assignments,
                            lambda a: getattr(a, "official_id", "official")),
            self._dep_group("availability response", availability,
                            lambda a: getattr(a, "player_id", "player")),
            self._dep_group("substitute request", substitutes,
                            lambda s: getattr(s, "player_id", "player")),
            self._dep_group("reschedule request", reschedules, lambda r: r.id)])
        # Release the slot the draft held so the ice reads as bookable again.
        if game.ice_slot_id:
            slot = self.store.get_ice_slot(game.ice_slot_id)
            if slot is not None and slot.status == IceSlotStatus.ALLOCATED:
                slot.status = IceSlotStatus.AVAILABLE
                self.store.save_ice_slot(slot)
        self.store.delete_game(game_id)
        self._audit("game_deleted", "game", game_id, actor_id,
                    {"matchup": self._matchup(game),
                     "ice_slot_id": game.ice_slot_id})
        return game
