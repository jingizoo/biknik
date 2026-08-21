"""League + Arena setup service.

Builds the scheduling universe before games exist: league → season →
division, club → team, venue → rink → ice slot, and manual game creation.
Pure logic over the store with an injected clock; every create is audited.
"""

import functools
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Callable, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain import (
    AgeEligibilityRule,
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
    PolicyScopeType,
    MembershipStatus,
    Rink,
    SchedulingPolicy,
    Season,
    SeasonCopyForwardCommit,
    SeasonRosterMembership,
    SeasonRosterMembershipEvent,
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
from ..domain.identity import (
    derive_display_name,
    normalize_birthdate,
    normalize_name_part,
    normalize_preferred_name,
    normalize_registration_number,
    normalize_skill_rating,
    normalized_name_key,
    MAX_NAME_PART_LENGTH,
    MAX_SKILL_RATING,
    MIN_SKILL_RATING,
)
from ..domain.eligibility import (
    evaluate_age_eligibility,
    normalize_age_tiers,
    normalize_cutoff,
    normalize_enforcement,
)
from ..domain.errors import (
    ConcurrencyConflictError,
    DivisionMismatchError,
    HasDependenciesError,
    IntegrityConflictError,
    InvalidTransitionError,
    NotAuthorizedError,
    NotEligibleError,
    NotFoundError,
    ScheduleConflictError,
    ValidationError,
)
from ..store import InMemoryStore
from .epoch_fence import EPOCH_FENCE_GLOBAL_KEY
from .import_validator import validate_import, validate_official_availability
from .ice_availability import (plan_ice_windows, parse_hhmm,
                               curfew_instant, WEEKDAY_NAMES)
from . import season_guard
from .season_guard import require_active_season
from .league_scope import (
    exact_registration_or_conflict,
    registered_team_ids_in_division as _registered_team_ids,
    team_registration_valid,
    team_season_participation,
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


def _missing_or_unequal(a, b) -> bool:
    """A scope-spine key is BROKEN when EITHER side is MISSING or the two
    DISAGREE (#205 review round 3 blocker 3) — the Python twin of
    ``integrity_checks._MISSING_OR_UNEQUAL``, the SQL predicate migration
    059's preflight applies to this very invariant.

    The membership spine guards used to be spelled ``if team.league_id and
    ls.league_id != team.league_id``. The leading conjunct is a FALSY-SKIP:
    a Team with NO permanent League skipped the coherence check entirely
    rather than failing it — the exact service-layer analogue of the NULL
    evasion blocker 1 fixed in the preflight, where ``a != b`` evaluated
    UNKNOWN (not TRUE) against a NULL and the row was filtered out. The two
    layers then disagreed: 059 REFUSED to backfill a league-less Team while
    the live service happily minted and revived memberships on one.

    ``not a`` rather than ``a is None`` deliberately: the guards this
    replaces were truthiness gates, so an empty-string id was skipped too.
    Treating both shapes as MISSING is strictly stronger than what shipped
    and keeps one rule for "this key is not there".

    Both-missing is a violation, not agreement — the same conclusion
    ``_MISSING_OR_UNEQUAL``'s own docstring reaches about why
    ``IS DISTINCT FROM`` is the wrong operator for a scope spine."""
    return not a or not b or a != b


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

# #205 Slice A membership lifecycle sets (review round 3 blocker 2).
#
# PARKED — the stint is open but holds no roster place: it was set aside and
# can be brought back. A parked row's spine was validated when the row was
# BORN and never since, so any move OUT of one is a revival that must
# re-prove the spine still holds.
_PARKED_MEMBERSHIP_STATUSES = frozenset({
    MembershipStatus.INACTIVE, MembershipStatus.INJURED})
# REVIVING — the targets ``create_season_roster_membership`` requires a full
# valid spine for, so a parked row may only re-enter one on a spine that is
# still valid. Exactly the three statuses ``_assert_membership_spine_valid``
# has always named in its own contract.
_REVIVING_MEMBERSHIP_STATUSES = frozenset({
    MembershipStatus.ACTIVE, MembershipStatus.APPLICANT,
    MembershipStatus.AFFILIATE})


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


def resolve_team_registration_for_import(store, season_id, team_id,
                                         target_league_id):
    """The registration row an import row should reuse in place, if any,
    and whether the Team's registrations for this Season conflict too
    much to proceed safely (#331 review round 17, generalized round 18).

    Never the first registration ``registrations_for_season`` happens
    to return for this team -- the schema allows one row per
    LeagueSeason within a Season (migration 035), and
    ``transfer_team_to_league`` deliberately preserves an inactive
    prior-League row as history, never touching it again. Both are
    looked up by their exact (team, LeagueSeason) identity instead: the
    row already sitting in the row's own resolved target LeagueSeason
    is reused/reactivated in place, exactly as before; failing that,
    the Team's SOLE other active registration elsewhere in this Season
    (a Rule 7 violation a stale pre-round-16 write path, or legacy
    data, could have left behind) is reused via the same in-place
    League "move" ``transfer_team_to_league`` itself performs when a
    Team's active registration no longer matches its permanent League.
    Inactive rows elsewhere are never candidates here, so they can
    never be touched: history stays byte-for-byte as it was left.

    A module-level, ``store``-only function (rather than a
    ``SetupService`` method) so it is the SAME callable every writer of
    a registration's League/Division shares -- ``commit_teams_players_
    import``, ``upsert_imported_registration``, ``roll_forward_
    registrations``/``_v2``, and ``hierarchy_import.py``'s own pre-write
    ``_preflight_reassignment_safety`` gate, which has no ``SetupService``
    instance of its own to call a method on -- so gate and apply, or two
    independent import entry points, can never derive different answers
    for the identical row (#331 review round 18's own required
    correction: "one exact-identity policy everywhere").

    Returns ``(reg_or_None, is_move, conflicting_ids)``. A non-``None``
    ``reg`` with ``is_move=False`` is the row already at the target --
    upsert it in place, division included, exactly as any repeat
    import row is. ``is_move=True`` means ``reg`` currently belongs to
    a DIFFERENT LeagueSeason and is about to move there -- its old
    division is never comparable to the new target's (they belong to
    different LeagueSeasons' own division pools), so callers must
    apply the League-change game guard, never the division-move one,
    before writing it. ``reg is None`` with no conflicts means no row
    exists anywhere in this Season yet -- create a fresh one. A
    non-empty ``conflicting_ids`` means the Team already holds more
    active registrations in this Season than this row's own target can
    unambiguously absorb (its own target row already exists AND a
    separate row is also active elsewhere, or more than one other
    candidate is active) -- the caller must reject before any write
    rather than silently guess which participation is authoritative or
    rebind one active row onto another's unique key."""
    target_ls = (store.league_season_for(target_league_id, season_id)
                if target_league_id else None)
    # #331 review round 19: the exact-target lookup itself can no longer
    # assume at most one row -- see exact_registration_or_conflict's own
    # docstring for why. A conflict here (Memory-only corrupted duplicate
    # data at the identical key) is folded into this function's own
    # conflicting_ids output exactly like every other conflict shape below.
    target_reg, _target_key_conflicts = (
        exact_registration_or_conflict(store, target_ls.id, team_id)
        if target_ls is not None else (None, []))
    other_active = [
        r for r in store.registrations_for_season(season_id)
        if r.team_id == team_id and r.active
        and (target_ls is None or r.league_season_id != target_ls.id)]
    if _target_key_conflicts:
        return None, False, _target_key_conflicts + [r.id for r in other_active]
    if target_reg is not None:
        if other_active:
            return None, False, (
                [target_reg.id] + [r.id for r in other_active])
        return target_reg, False, []
    if len(other_active) == 1:
        return other_active[0], True, []
    if other_active:
        return None, False, [r.id for r in other_active]
    return None, False, []


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


class _MoveGameRaced(Exception):
    """Internal retry signal (#314 review): move_game's defensive post-lock
    verify found the Rinks it holds don't match the Game's current source Rink
    plus the target slot's Rink. Should not happen — move_game is the only
    writer of an existing Game's ice_slot_id, and it always takes the Team lock
    before reading the current slot — but the check is cheap insurance against
    a future writer that skips that convention. Caught by
    SetupService._retry_on_move_race, which rolls back and retries cleanly in
    a fresh transaction rather than proceeding against an untrustworthy lock
    set."""


class _RinkLockPlanDrifted(Exception):
    """Internal retry signal (#331 review round 13 finding 1):
    commit_rinks_ice_slots_import's lock plan is built from a snapshot taken
    BEFORE the lock loop runs, so a rink_code absent from that snapshot is
    skipped by both the lock loop and the slot_type/overlap gates — even
    though a concurrent transaction can create that exact rink_code (and a
    booked Game on it) in the gap between the snapshot and this attempt's
    lock acquisition. Raised when a re-check after lock acquisition finds a
    rink_code has newly resolved to a Rink outside the locked plan. Caught by
    the commit's bounded retry loop, which rolls back and retries with a
    fresh snapshot — one that will correctly lock and gate the now-visible
    Rink — rather than proceeding against an untrustworthy lock set."""


class _TeamLockPlanDrifted(Exception):
    """Internal retry signal (#331 review round 14 finding 2):
    commit_teams_players_import's gate_errors pass (Program/permanent-League/
    registration-division move guards) runs from a snapshot of
    store.all_teams() taken BEFORE any of that row's writes — a team_code
    absent from that snapshot is a team the gate never evaluated at all, even
    though a concurrent import for a DIFFERENT Season can create that exact
    team_code (with its OWN Program/League already set) in the gap between
    the gate snapshot and this attempt's later double-checked-locking
    recheck. Without this signal, that later recheck would silently adopt
    the concurrently-created Team via the plain "team is not None" update
    branch and write a registration into THIS attempt's League/Program
    without ever re-running the guards that branch exists to enforce.
    Raised when the recheck finds a team_code that was not present at the
    gate_errors snapshot. Caught by the commit's bounded retry loop, which
    rolls back and retries with a fresh snapshot — one whose gate_errors
    pass will correctly evaluate the now-visible Team — rather than
    proceeding against an ungated write."""


class _HierarchyTeamOrPlayerDrifted(Exception):
    """Internal retry signal (#331 review round 14 finding 3):
    commit_hierarchy_import resolves every Team/Player external_ref ONCE,
    into a {code: obj} snapshot taken before its own _preflight_reassignment_
    safety pass and before any write, then passes each row's resolved
    ``existing`` (or None) into upsert_imported_team/upsert_imported_player.
    Neither helper previously rechecked a None ``existing`` under its
    next_id() reservation lock before inserting, so a Team/Player created by
    a DIFFERENT concurrent writer — the pilot commit_teams_players_import,
    or another hierarchy import — in the gap between that snapshot and this
    row's upsert could either (a) be silently duplicated (no lock at all
    protected the insert), or, once (a) is closed by the SAME reserve-then-
    recheck pattern commit_teams_players_import already uses, (b) be
    silently ADOPTED via a plain "existing is not None" update branch that
    _preflight_reassignment_safety's own snapshot never evaluated for
    program/league-move stranding — exactly the "late materialization
    bypasses the earlier gate" class _TeamLockPlanDrifted closes for the
    pilot import, here for the hierarchy path's own preflight instead.
    Raised when either helper's reserve-then-recheck finds a row for a code
    the caller believed absent. Caught by commit_hierarchy_import's bounded
    retry loop, which rolls back and retries the WHOLE nine-sheet batch with
    a fresh snapshot — one whose _preflight_reassignment_safety pass (and
    every other check downstream of it) will correctly evaluate the
    now-visible Team/Player — rather than proceeding against an unguarded
    or ungated write."""


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

    def _resolve_season_creation(self, program_id: str, name: str,
                                 start_date, end_date):
        """Read-only resolution shared by ``create_season`` and the new-Season
        copy-forward preview/commit (#159): Program exists, timezone-anchored
        date parsing (#272), end >= start. No write of its own — extracted
        verbatim from ``create_season`` so the copy-forward preview can run
        the IDENTICAL checks before the target Season exists (to build its
        fingerprint) and commit can re-run them again under its own locks,
        without a caller ever risking drift from ``create_season``'s own
        rules. Returns ``(program, cleaned_name, start, end)``."""
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
        return program, self._require_name(name), start, end

    @_transactional
    def create_season(self, program_id: str, name: str,
                      start_date=None, end_date=None,
                      actor_id: Optional[str] = None) -> Season:
        _program, clean_name, start, end = self._resolve_season_creation(
            program_id, name, start_date, end_date)
        season = Season(id=self.store.next_id("season"), program_id=program_id,
                        name=clean_name, start_date=start, end_date=end)
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

    def _policy_scope_lock_plan(self, rink_ids, season_ids) -> dict:
        """#318 review — pre-lock LOCATOR for every policy scope the placement
        gate will read: the candidate Season(s), the season of every active
        slotted game on the target rink(s) (the directional turnover buffer
        resolves each neighbor game's OWN effective policy), and each such
        season's Program. Plain reads only — the caller locks the returned
        rows in the canonical global order (:meth:`_lock_programs` FIRST,
        then Teams, Rinks, :meth:`_lock_seasons`) and then re-verifies the
        snapshot under those locks with :meth:`_verify_policy_scope_plan`;
        any drift refuses with the retryable ``placement_raced``, exactly
        like create_game's slot-locator defense."""
        rinks = {r for r in rink_ids if r}
        seasons = {s for s in season_ids if s}
        for ex in self.store.all_games():
            if ex.cancelled or not ex.ice_slot_id or not ex.season_id:
                continue
            ex_slot = self.store.get_ice_slot(ex.ice_slot_id)
            if ex_slot is not None and ex_slot.rink_id in rinks:
                seasons.add(ex.season_id)
        programs = set()
        for sid in seasons:
            season = self.store.get_season(sid)
            if season is not None and season.program_id:
                programs.add(season.program_id)
        return {"seasons": seasons, "programs": programs}

    def _lock_programs(self, program_ids) -> None:
        """Row-lock every distinct Program in canonical (sorted) order —
        FIRST in the global Program -> Team -> Rink -> Season chain, matching
        the ice-availability builder and hierarchy import (both lock
        Program -> Rink -> Season). Taking the Program AFTER the Rink/Season
        (as #318 round 1 did) is an ABBA inversion against the builder that
        Postgres aborts with deadlock_detected — reproduced by
        test_placement_concurrency's builder-vs-placement races. A no-op on
        the in-memory store; a real ``SELECT ... FOR UPDATE`` on SQL. MUST
        run inside a ``store.transaction()``, before the Team locks."""
        for pid in sorted({p for p in program_ids if p}):
            self.store.get_program_for_update(pid)

    def _lock_seasons(self, season_ids) -> None:
        """Row-lock every distinct Season in canonical (sorted) order, after
        the Rink locks (the global Program -> Team -> Rink -> Season chain).
        A placement locks EVERY season whose policy rows its gate may read —
        the candidate's plus each same-rink neighbor game's — in ONE sorted
        batch, so two placements with overlapping season sets cannot ABBA
        each other and a season-scope ``set_scheduling_policy`` (which locks
        only that Season row) strictly serializes with any placement that
        would read it. Idempotent re-locks (e.g. the #159 active-season
        guard re-taking the candidate row) are harmless."""
        for sid in sorted({s for s in season_ids if s}):
            self.store.get_season_for_update(sid)

    def _verify_policy_scope_plan(self, plan, rink_ids, season_ids=(),
                                  exclude_slot_ids=(),
                                  exclude_game_id=None) -> None:
        """Re-verify the pre-lock scope locator UNDER the placement's locks:
        the candidate season(s) and every same-rink neighbor's season must be
        in the locked set, and every locked season must still belong to a
        locked Program (a Season reparented between the locator read and its
        row lock would otherwise smuggle an unlocked Program scope past the
        gate). Any drift — e.g. a game landed on the rink between the
        locator and our Rink lock — refuses with the retryable
        ``placement_raced`` rather than running the gate against scope rows
        this transaction does not hold.

        ``exclude_slot_ids``/``exclude_game_id`` mirror the gate's OWN
        read-set exclusions: the occupant of the candidate slot itself (the
        gate refuses ``slot_unavailable`` without ever resolving that game's
        policy) and the moving game (excluded from its own scan) need no
        scope lock — exempting them keeps a plain same-slot race on its
        precise pinned refusal instead of a spurious ``placement_raced``."""
        def _raced(detail):
            raise ConcurrencyConflictError(
                "A scheduling-policy scope changed while processing the "
                "request; please retry.",
                {"reason": "placement_raced", **detail})
        for sid in season_ids:
            if sid and sid not in plan["seasons"]:
                _raced({"season_id": sid})
        rinks = {r for r in rink_ids if r}
        for ex in self.store.all_games():
            if (ex.cancelled or not ex.ice_slot_id or not ex.season_id
                    or ex.id == exclude_game_id
                    or ex.ice_slot_id in exclude_slot_ids):
                continue
            ex_slot = self.store.get_ice_slot(ex.ice_slot_id)
            if (ex_slot is not None and ex_slot.rink_id in rinks
                    and ex.season_id not in plan["seasons"]):
                _raced({"season_id": ex.season_id})
        for sid in plan["seasons"]:
            season = self.store.get_season(sid)
            if (season is not None and season.program_id
                    and season.program_id not in plan["programs"]):
                _raced({"program_id": season.program_id})

    def _lock_teams(self, team_ids) -> None:
        """Row-lock every distinct Team in canonical (sorted) order (#277).

        The final placement check (:meth:`_assert_slot_free_for_game`) refuses a
        game that would double-book a team, but that read-then-write is only
        atomic if concurrent placements sharing a team serialize. Locking exactly
        the game's two teams is sufficient: a ``team_overlap`` can exist only
        between games that SHARE a team, so any racing placement able to conflict
        must also touch — and block on — one of these very rows. Whoever wins the
        lock writes its game; the loser, re-scanning ``all_games`` under the lock,
        now sees that game and is refused with a stable ``team_overlap`` instead
        of silently double-booking. The other half of the check — one active game
        per slot — is guarded by :meth:`_lock_rinks` (which also serializes with
        the ice-availability builder) plus the ``ux_games_active_ice_slot`` DB
        index as a backstop.

        Sorted order gives a total lock order, so two placements can never
        deadlock each other; Teams are always locked AFTER the Program rows
        (:meth:`_lock_programs` — the builder's Program-first order) and BEFORE
        the Rink and Season (matching ``transfer_team_to_league`` /
        ``copy_``/``move_season_teams``, which lock Team→Season), so the global
        Program→Team→Rink→Season order holds. A no-op on the in-memory store
        (whose ``transaction()`` fully serializes for free); a real
        ``SELECT ... FOR UPDATE`` on SQL. MUST run inside a
        ``store.transaction()``, and before the caller's Rink/Season guards so
        the ordering invariant holds."""
        for tid in sorted({t for t in team_ids if t}):
            self.store.get_team_for_update(tid)

    def _lock_rinks(self, rink_ids) -> None:
        """Row-lock every distinct Rink in canonical (sorted) order (#277 / #313).

        The placement check reads a slot's status/occupancy and then allocates it,
        and the ice-availability BUILDER (``commit_ice_availability``) revalidates
        its preview token and creates/reconciles slots UNDER a per-rink
        ``get_rink_for_update`` lock. Game placement must take that SAME rink lock
        so the two serialize: otherwise a cross-Season create/move/draft can
        allocate an exact AVAILABLE slot in the window between the builder's
        under-lock token check and its writes, turning a reviewed new/duplicate row
        into an allocated-Game conflict while the builder commit still succeeds.
        With the lock the loser blocks and then either sees the change (the builder
        recomputes ``preview_mismatch``, or the placement re-reads the slot as
        ``slot_unavailable``) or runs cleanly after the winner commits. (The DB
        index ``ux_games_active_ice_slot`` still backstops the pure game-vs-game
        slot race; this lock additionally covers game-vs-builder, which the index
        cannot.)

        Locked AFTER the Programs and Teams but BEFORE the Seasons — the global
        Program→Team→Rink→Season order — because the builder locks
        Program→Rink→Season (Program first, Rink before Season); inverting
        either pair here would ABBA-deadlock the builder (#318 round 1 proved
        it for Program-last). Sorted order gives a total order between two
        multi-rink placements. A no-op on the in-memory store; a real
        ``SELECT ... FOR UPDATE`` on SQL. MUST run inside a
        ``store.transaction()``, after the Team locks and before the Season
        locks/guard."""
        for rid in sorted({r for r in rink_ids if r}):
            self.store.get_rink_for_update(rid)

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

    def season_is_historical(self, season, now=None) -> bool:
        """Is this Season history? — the service-layer entry point to the one
        shared predicate, :func:`season_guard.season_is_historical` (ARCHIVED
        *or* definitely end-dated). Defaults ``now`` to this service's clock so
        readers need no clock of their own; callers holding a snapshot (the
        transfer path) pass theirs so a multi-Season decision cannot straddle a
        tick. See the shared function for why there is exactly one copy."""
        return season_guard.season_is_historical(
            season, self.clock() if now is None else now)

    def season_is_read_only(self, season) -> bool:
        """Does this Season refuse writes? — the service-layer entry point to
        :func:`season_guard.season_is_read_only`, the SAME predicate
        ``_require_active_season`` refuses on.

        Sits beside ``season_is_historical`` and is deliberately not it: this
        one takes no clock, because an elapsed ``end_date`` makes a Season
        history to READ but does not make it read-only to WRITE. Exposed so a
        read that ADVERTISES the refusal (``get_setup_hierarchy_v2``'s per-Season
        ``read_only``) is answered by the refusal's own authority instead of by
        a second copy of the expression."""
        return season_guard.season_is_read_only(season)

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
        a binding that still owns Divisions, registrations, Games, schedule
        scenarios, or age-eligibility rule history (#273 review round 2 finding 3)
        is refused (resolve those first), and it fails closed with
        ``season_archived`` on an archived Season so read-only history is never
        rewritten. All checks run before the single delete, so a refused unbind
        changes nothing."""
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
        scenarios = [s for s in self.store.all_schedule_scenarios()
                     if s.league_season_id == league_season_id]
        # #273 review round 2 finding 3: age-eligibility rule history is now
        # an itemized dependent too. Previously this delete overlooked rule
        # rows entirely, so removing a LeagueSeason left its
        # age_eligibility_rules orphaned — pointing at a binding that no
        # longer existed, with no operator-facing signal that history would
        # be stranded. Also the SAME invariant migration 058's new FK on
        # age_eligibility_rules.league_season_id now enforces at the
        # database level (a DB-level backstop against a create-rule-vs-
        # delete-binding race this service-level gate alone cannot close —
        # this itemized check is still what gives an operator a friendly,
        # actionable refusal instead of a raw constraint error on the
        # non-race path).
        rules = self.store.age_eligibility_rules_for_league_season(
            league_season_id)
        # #205 review round 1 finding 2 — a membership's league_season_id is
        # a REQUIRED (non-nullable) foreign key onto this exact row, the same
        # shape team registrations/games above already block on regardless of
        # status; ANY membership (even released/transferred history) blocks.
        memberships = [m for m in self.store.all_season_roster_memberships()
                      if m.league_season_id == league_season_id]
        self._block_if_dependents(
            "league_season", league_season_id, "season binding", [
                self._dep_group("division", divisions, lambda d: d.name),
                self._dep_group("team registration", regs,
                                lambda r: self._team_name(r.team_id)),
                self._dep_group("game", games, self._matchup),
                self._dep_group("schedule scenario", scenarios,
                                lambda s: s.name),
                self._dep_group("age eligibility rule", rules,
                                lambda r: f"v{r.version}"),
                self._dep_group("roster membership", memberships,
                                self._membership_label)])
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
                                     actor_id: Optional[str] = None, *,
                                     season_id: Optional[str] = None) -> Division:
        """Create a Division under a permanent League (#283 back-compat, v2).

        Without ``season_id`` (legacy/back-compat, unchanged): the League
        participates in one Season here (the common case); the LeagueSeason is
        resolved from the league's sole binding, and is still ambiguous when the
        league has more than one.

        With ``season_id`` (#345 additive correction): resolves the EXISTING
        ``LeagueSeason(league_id, season_id)`` binding exactly. Never infers the
        first/sole binding and never creates a new one — a League bound to
        several Seasons can commit a Division against a specific one instead of
        always failing ``ambiguous_season_for_league``. Requires the League and
        Season share a Program (rule 5) and the target binding to already exist."""
        # #159 — canonical League→Season lock order: lock the League row first,
        # then resolve the target binding, then lock that binding's Season.
        league = self._lock_league_for_binding(league_id)
        if season_id is not None:
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
            binding = self.store.league_season_for(league_id, season_id)
            if binding is None:
                raise ValidationError(
                    "That league is not part of the given season.",
                    {"reason": "league_not_in_season",
                     "league_id": league_id, "season_id": season_id})
        else:
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
            binding = lss[0]
        self._require_active_season(binding.season_id)  # #159 read-only guard
        # #159 — RE-FETCH the binding UNDER the Season lock before inserting: the
        # resolution above is unlocked, so a concurrent delete_league_season
        # (which locks the same Season row) could have unbound it. Inserting a
        # Division against a deleted LeagueSeason would orphan it (migration 035
        # has no FK). A binding unbound out from under us fails closed with zero
        # write/audit — this applies identically whether the binding came from
        # the sole-binding legacy path or the exact season_id path above.
        binding = self.store.get_league_season(binding.id)
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
            if not g.cancelled
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
        # #331 review round 18/19: never blindly rebind reg.league_season_id
        # -- the Team may already retain ANOTHER row at the target
        # LeagueSeason (history from a prior assignment/transfer cycle);
        # rebinding reg onto it would either silently duplicate a row
        # (Memory) or raise a raw unique_violation (SQLite/PostgreSQL)
        # instead of a structured rejection. A direct (team, target
        # LeagueSeason) lookup -- NOT the shared import resolver -- because
        # `reg` is already pinned by the caller here (unlike
        # commit_teams_players_import/roll_forward_registrations, which
        # must first DISCOVER which row a team_id resolves to): the
        # resolver's own `other_active` scan has no way to exclude `reg`
        # from its "other active rows" candidates, so reusing it here would
        # misreport `reg` itself as a second conflicting registration.
        #
        # Round 18 guarded this whole check on `reg.active`, reasoning "an
        # inactive reg has no Rule 7 participation to protect" -- true, but
        # irrelevant: the `(team_id, league_season_id)` uniqueness a
        # blind rebind can violate is unconditional, not "only among active
        # rows". An INACTIVE reg rebound onto an occupied target key is the
        # identical duplicate-key violation, just without a Rule 7 angle --
        # round 19 reproduced exactly this via a public lifecycle ending in
        # this call on an inactive historical row. The lookup and its
        # conflict handling now run regardless of reg's own active status.
        # `reg` itself can never be among a target-key conflict: the no-op
        # early-return above already excluded old_league == league_id, so
        # reg.league_season_id != new_ls.id is guaranteed here.
        _target_reg, _target_conflicts = exact_registration_or_conflict(
            self.store, new_ls.id, reg.team_id)
        if _target_conflicts:
            raise ValidationError(
                "The target league already has more than one registration "
                "for this team in this season; resolve that conflict "
                "before reassigning this one.",
                {"reason": "team_registration_conflict",
                 "affected_registration_ids": [reg.id] + _target_conflicts})
        if _target_reg is not None:
            if reg.active and not _target_reg.active:
                # The ONE combination with a safe, well-defined resolution:
                # reg (active) moves logically onto the retained INACTIVE
                # target -- reactivate IT in place (mirroring
                # register_team_for_season's own reactivate-in-place
                # semantics) and retire `reg` instead of moving it there,
                # preserving both rows' own identities.
                reg.active = False
                self.store.save_season_team_registration(reg)
                self._audit(
                    "season_team_registration_deactivated",
                    "season_team_registration", reg.id, actor_id,
                    {"reason": "superseded_by_retained_target",
                     "league_id": league_id, "superseded_by": _target_reg.id})
                _target_reg.active = True
                _target_reg.division_id = None
                self.store.save_season_team_registration(_target_reg)
                self._audit(
                    "season_team_league_assigned", "season_team_registration",
                    _target_reg.id, actor_id,
                    {"from": old_league, "to": league_id, "reactivated": True})
                return _target_reg
            # Every other combination -- reg active + target active (Rule 7
            # conflict), or reg INACTIVE with any row already at the target
            # (active or inactive) -- is an unresolvable key collision:
            # rebinding reg onto an already-occupied
            # (team_id, league_season_id) key can never proceed safely.
            # Reject before any write and let the operator resolve it
            # explicitly, rather than guessing which participation/history
            # is authoritative.
            raise ValidationError(
                "This team already has a registration in the target "
                "league for this season; resolve the conflict before "
                "reassigning this one.",
                {"reason": "team_registration_conflict",
                 "affected_registration_ids": [reg.id, _target_reg.id]})
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
        # #331 review round 20: a Team may have at most one ACTIVE
        # registration in this Season, full stop -- not just at this exact
        # LeagueSeason key. Rule 7 above already keeps `ls` at the Team's own
        # permanent League when one is set, so the only way another active
        # row can exist elsewhere in this Season is legacy data or a write
        # path predating full enforcement (e.g. an old League's row a
        # since-superseded transfer didn't retire) -- reject it the same as
        # every other shape of conflict this method can hit, rather than
        # silently leaving two active rows behind. Checked BEFORE the
        # exact-key reactivate-or-create decision below, using the same
        # zero-mutation-before-reject discipline.
        _other_active, _other_conflicts = team_season_participation(
            self.store, season_id, team_id)
        if _other_conflicts:
            raise ValidationError(
                f"Team {team_id} already has more than one active "
                "registration this season; resolve the conflict before "
                "registering.",
                {"reason": "team_registration_conflict",
                 "affected_registration_ids": _other_conflicts})
        if _other_active is not None and _other_active.league_season_id != ls.id:
            raise ValidationError(
                f"Team {team_id} already has an active registration "
                "elsewhere this season; remove it before registering "
                "into a different League.",
                {"reason": "team_registration_conflict",
                 "affected_registration_ids": [_other_active.id]})
        # One registration per (team, LeagueSeason). A prior *inactive*
        # registration (a team removed then re-added) is reactivated in place.
        # #331 review round 19: exact-key multiplicity (Memory-only corrupted
        # duplicate rows at this identical key) must reject before any write,
        # the same as every other shape of conflict this method can hit.
        existing, _existing_conflicts = exact_registration_or_conflict(
            self.store, ls.id, team_id)
        if _existing_conflicts:
            raise ValidationError(
                f"Team {team_id} already has more than one registration "
                "at this exact league/season; resolve the conflict before "
                "registering.",
                {"reason": "team_registration_conflict",
                 "affected_registration_ids": _existing_conflicts})
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
        """Ids of committed, non-cancelled games in ``season_id`` that
        ``team_id`` plays in.

        A generated proposal is still read-only and never appears here. Once an
        operator commits that proposal, however, its ``is_draft`` Game rows own
        allocated ice and are part of the review/publish workflow. Removing or
        re-binding a participant underneath those rows would strand invalid
        drafts, so committed drafts block the same participation changes as
        published Games (#314 review).
        """
        if not season_id:
            return []
        return [g.id for g in self.store.all_games()
                if g.season_id == season_id and not g.cancelled
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

    def _pick_division_candidate(self, candidates, preferred_league_id):
        """Disambiguate same-named Division candidates for one
        ``commit_teams_players_import`` row (#331 review round 16
        reproduction 3): Division names are NOT unique within a Season
        (creation enforces no such constraint), so two Divisions can
        legitimately share a name under two different permanent Leagues.
        The prior code let gate and apply pick DIFFERENT rows for the
        identical ambiguous name -- gate's ``{name: division}`` snapshot
        dict was last-wins by iteration order, apply's own ``next(...)``
        lookup was first-wins -- silently committing a registration under
        whichever League apply happened to prefer, regardless of what gate
        had actually validated. A single candidate is always unambiguous
        regardless of League. Two or more requires ``preferred_league_id``
        (the row's Team's existing permanent League, when it has one) to
        resolve to EXACTLY one candidate's own LeagueSeason -- no
        preference to disambiguate with, or none/more than one candidate
        actually matching it, is genuinely ambiguous and the caller must
        reject rather than silently guess.

        Returns ``(division_or_None, ambiguous_bool)``. ``None`` with
        ``ambiguous=False`` means the name doesn't exist yet (the caller
        creates it); ``None`` with ``ambiguous=True`` means this row cannot
        safely proceed at all."""
        if not candidates:
            return None, False
        if len(candidates) == 1:
            return candidates[0], False
        if preferred_league_id:
            matches = []
            for d in candidates:
                ls = self.store.get_league_season(d.league_season_id)
                if ls and ls.league_id == preferred_league_id:
                    matches.append(d)
            if len(matches) == 1:
                return matches[0], False
        return None, True

    def _resolve_row_division_and_league(self, division_name_raw,
                                         existing_team_league_id,
                                         divisions_by_name,
                                         new_division_target_league,
                                         season_id):
        """One shared, frozen resolution of a ``commit_teams_players_import``
        row's Division and target permanent League -- called identically by
        gate validation AND apply (#331 review round 16), so the two can
        never again independently derive different answers for the same
        row the way reproductions 1 and 3 exploited. Supersedes round 15's
        ``_resolve_import_team_target_league``, which only ever considered
        THIS row's own ``existing_team_league_id`` for a not-yet-existing
        Division -- correct for an existing Team's row, but blind to a
        brand-new Team's row, which the per-existing-Team gate loop never
        even reached, letting apply's own live ``divisions_for_season()``
        re-query silently adopt whatever League an EARLIER row's create
        happened to pick moments before, unconstrained by any gate at all
        (round 16 reproduction 1). ``divisions_by_name`` (grouped by name,
        never a last-wins ``{name: division}`` dict) and
        ``new_division_target_league`` (the whole batch's cross-row-
        resolved consensus for a name that doesn't exist yet -- built
        BEFORE any row is decided, so the result never depends on which
        row a loop visits first) are the two frozen, whole-batch inputs
        that make this deterministic regardless of upload order.

        Returns ``(division_or_None, target_league_id, ambiguous_bool)``.
        A non-``None`` division is an EXISTING row to reuse as-is. Ambiguous
        rows are already rejected by a dedicated pre-pass before gate_errors
        is checked, so apply -- which only ever runs once gate_errors is
        empty -- never actually observes ``ambiguous=True`` in practice."""
        if _blank(division_name_raw):
            if existing_team_league_id:
                return None, existing_team_league_id, False
            candidates = self.store.league_seasons_for_season(season_id)
            return None, (candidates[0].league_id if candidates else None), False
        name = _clean(division_name_raw)
        candidates = divisions_by_name.get(name, [])
        if candidates:
            division, ambiguous = self._pick_division_candidate(
                candidates, existing_team_league_id)
            if ambiguous:
                return None, None, True
            ls = self.store.get_league_season(division.league_season_id)
            return division, (ls.league_id if ls else None), False
        if name in new_division_target_league:
            return None, new_division_target_league[name], False
        candidates = self.store.league_seasons_for_season(season_id)
        return None, (candidates[0].league_id if candidates else None), False

    def _resolve_import_row_registration(self, season_id, team_id,
                                         target_league_id):
        """Thin delegate to the module-level ``resolve_team_registration_
        for_import`` (#331 review round 18) -- kept as a method purely for
        this class's own many existing ``self._resolve_import_row_
        registration(...)`` call sites; see that function's docstring for
        the full contract and why it takes ``store`` directly rather than
        living only here."""
        return resolve_team_registration_for_import(
            self.store, season_id, team_id, target_league_id)

    def _bind_import_league_season(self, season_id, league_id):
        """The LeagueSeason a NEW Division (or a blank-division row) should
        bind to during ``commit_teams_players_import`` apply (#331 review
        round 15 finding 1, generalized round 16): finds/creates the
        binding of ``league_id`` -- the row's already fully RESOLVED target
        League, from ``_resolve_row_division_and_league`` above, whether
        that came from the Team's own permanent League, an existing
        Division's own League, or the batch's cross-row-resolved consensus
        for a not-yet-existing name -- to this Season. Falls back to the
        ambient ``_import_default_league_season`` only when ``league_id``
        is falsy, i.e. a genuinely league-less Team with no other row in
        the batch expressing a preference either. Safe to call here
        specifically because every League this could possibly bind to was
        already row-locked, before the Season guard, by this attempt's own
        pre-lock pass -- calling :meth:`_link_league_season` on an unlocked
        existing League this deep into an already-Season-locked transaction
        would otherwise invert the canonical League->Season lock order."""
        if league_id:
            return self._link_league_season(league_id, season_id)
        return self._import_default_league_season(season_id)

    def _revalidate_game_participation(self, game):
        """Both teams must still be valid participants of ``game``'s competition
        scope (#283 Slice E) — checked before any write (publish/move), so a
        rejection mutates nothing.

        A REGULAR game requires both teams to have an ACTIVE, unambiguous
        registration in the game's exact LeagueSeason (its single competition
        identity) -- unconditionally on any exact-key or season-wide conflict
        (#331 review round 21 finding 1), exactly as ``create_game`` itself
        enforces; when the game also carries a Division, the stricter
        season+division match is kept too. The game's own ``league_season_id``
        is itself resolved and validated first -- it must exist and belong to
        the game's Season AND League (#331 review round 22/23) -- before
        either teams' registrations are checked against it; a Team's
        registration must sit at this EXACT LeagueSeason, not merely one
        under the same permanent League. A legacy regular game with no
        ``league_season_id`` falls back to the season(+division) check. An
        EXHIBITION only requires both teams to remain active participants of
        the game's Season (it may cross Leagues).

        move_game/publish_game both call this before mutating, and
        decide_reschedule's approve path calls move_game/publish_game in
        turn, so the reschedule-approval workflow inherits every guard here
        with no separate implementation to keep in sync."""
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
        # #331 review round 22: `ls_id` being non-None doesn't mean it
        # resolves to a real, correctly-scoped row -- round 21's fix only
        # ever compared League ids, so a dangling reference (the row was
        # since deleted, or never existed) or one silently reassigned to a
        # DIFFERENT Season's LeagueSeason for the SAME League both slipped
        # through undetected. Resolved and validated the same way
        # _require_batch_team_participation resolves its own target
        # LeagueSeason -- existence, then Season match -- before either
        # Team is checked against it.
        ls = self.store.get_league_season(ls_id)
        if ls is None:
            raise ValidationError(
                "This game's league-season no longer exists; it cannot be "
                "published or moved until it is repaired.",
                {"reason": "regular_game_missing_league_season",
                 "game_id": game.id, "league_season_id": ls_id})
        if game.season_id and ls.season_id != game.season_id:
            raise ValidationError(
                "This game's league-season belongs to a different season; "
                "it cannot be published or moved until it is repaired.",
                {"reason": "game_league_season_mismatch", "game_id": game.id,
                 "season_id": game.season_id, "league_season_id": ls.id,
                 "league_season_season_id": ls.season_id})
        # #331 review round 23: a real LeagueSeason at the right Season still
        # isn't proof the game's own legacy `league_id` agrees with it --
        # standings (``_standings_for_division``/``_standings_for_league_
        # season``) already fail closed on exactly this drift when READING,
        # so the write boundary must refuse to CREATE it. Nothing in normal
        # operation can produce this split (`create_game` always derives
        # `league_season_id` from the same `league_id` it stores), so this
        # only ever fires on a corrupted/hand-edited row -- same threat model
        # as the dangling/cross-Season checks above.
        #
        # #331 review round 24: this comparison is UNCONDITIONAL. Round 23
        # guarded it behind `game.league_id and ...`, which skipped the check
        # entirely for a falsy stored League -- and `Game.league_id` is
        # explicitly Optional with a nullable `games.league_id` column, so
        # `None` (or `""`) is exactly the corrupted shape the guard let
        # through. By this point the game is REGULAR (exhibitions returned
        # far above) and `ls` is a real, Season-matched LeagueSeason, so a
        # missing League is drift, not a legacy-tolerant case: the correct
        # invariant is plain equality. The stored value is reported as-is
        # (including `None`) so remediation sees what is actually on the row.
        if game.league_id != ls.league_id:
            raise ValidationError(
                "This game's league-season belongs to a different league; "
                "it cannot be published or moved until it is repaired.",
                {"reason": "game_league_season_mismatch", "game_id": game.id,
                 "league_id": game.league_id, "league_season_id": ls.id,
                 "league_season_league_id": ls.league_id})
        # The season+division check runs first: it raises the precise
        # DivisionMismatchError and is the stricter guard for a divisioned game.
        if game.season_id and game.division_id:
            self._require_team_registered(
                game.season_id, game.home_team_id, game.division_id)
            self._require_team_registered(
                game.season_id, game.away_team_id, game.division_id)
        # Both teams must be active in the game's exact LeagueSeason (covers
        # the division-less regular game the check above skips). #331 review
        # round 22: routed through the same shared per-team resolver
        # _require_batch_team_participation uses -- resolved directly
        # against `ls` (this game's own, now fully-validated LeagueSeason) --
        # rather than round 21's fix, which resolved each Team's registration
        # via its own permanent League (_require_team_registered) and only
        # compared League ids afterward: that comparison could never detect
        # `ls` being a different Season's row for the identical League, since
        # the registration it found (via the Team's CURRENT permanent
        # League + `game.season_id`) would share that same League id too.
        for tid in (game.home_team_id, game.away_team_id):
            if tid is not None:
                self._require_team_in_league_season(game.season_id, ls, tid)

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
                                actor_id: Optional[str] = None,
                                season_axis_guard=None) -> Team:
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
        committed, non-cancelled games (including committed draft rows), moving
        it would strand them, so the WHOLE transfer is rejected before any
        write — the operator must resolve those games first. All checks run
        before any mutation, so a rejected transfer changes nothing (zero
        Team/registration/audit mutation).

        ``season_axis_guard`` (#409) is an optional
        ``callable(season_ids) -> None`` handed in by the caller. It is invoked
        UNDER the locks, with the Seasons whose registration rows this transfer
        is about to rewrite, and raises to refuse. See
        ``ApiService._season_axis_guard``: this operation's context-axis class
        is Program-only by its named targets and only BECOMES Season-owned once
        those registrations are discovered, which cannot happen before the
        locks. ``None`` — the internal and import callers — is ungated, exactly
        as every other #409 gate treats an identity-less caller.
        """
        team = self.store.get_team_for_update(team_id)
        if team is None:
            raise NotFoundError(f"Team {team_id} not found.")
        return self._transfer_team_to_league_inner(
            team, new_league_id, actor_id,
            season_axis_guard=season_axis_guard)

    def _transfer_team_to_league_inner(self, team, new_league_id: str,
                                       actor_id: Optional[str] = None,
                                       season_axis_guard=None) -> Team:
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
        # #409 — the AXIS CHANGE, enforced at the instant it happens. Up to
        # here this transfer has consumed only the PROGRAM axis: a Team and a
        # League are both records that outlive any one Season, which is why the
        # pre-disclosure phase classifies it Program-only and why that phase
        # can honestly read no store row at all. THIS line is where it learns
        # otherwise — every surviving candidate is an active
        # SeasonTeamRegistration, a Season-OWNED bridge row, and the writes
        # below rewrite it onto the target League's LeagueSeason for that same
        # Season. So the two-axis rule applies from here, and it is applied
        # under the locks already held (the Team row, the target League row,
        # and every affected Season row) so the tuple cannot change between
        # this decision and those writes.
        #
        # Placed BEFORE the classification loop rather than after it, so a
        # refusal never discloses the `team_transfer_strands_games`
        # `affected_game_ids` of a Season the caller never chose: "you have not
        # chosen a context" is answered ahead of anything about the records,
        # which is the same ordering `_refuse_unchosen_context` keeps at the
        # transport. A Team with NO such registrations consumes no Season and
        # is deliberately left on the Program-axis rule — demanding a Season
        # there would refuse a legitimate move rather than merely tighten it.
        if season_axis_guard is not None:
            season_axis_guard({
                self._season_of_league_season(reg.league_season_id)
                for reg in fresh_candidates
                if self._season_of_league_season(reg.league_season_id)})
        to_move = []          # (reg, season_id) pairs eligible to move
        blocked = []          # {registration_id, season_id, affected_game_ids}
        # #205 review round 1 finding 2 — {registration_id, season_id,
        # affected_membership_ids}: a LIVE membership still names the OLD
        # (LeagueSeason, Team) pair this transfer is about to move the
        # registration OFF of. Moving/superseding underneath it would leave
        # the membership's league_season_id naming a League the Team no
        # longer plays in (Team<->LeagueSeason disagreement) — exactly the
        # stranding the review demonstrated. Terminal rows are closed
        # history and never block, mirroring the games check (cancelled
        # games don't block either).
        blocked_memberships = []
        for reg in fresh_candidates:
            season_id = self._season_of_league_season(reg.league_season_id)
            season = locked_seasons.get(season_id) if season_id else None
            # #159 — an ended OR archived Season is frozen history: never move
            # its registration (archived may be undated/future). ``now`` is the
            # single clock snapshot taken for this whole transfer, so a
            # multi-Season decision cannot straddle a tick. The standings
            # readers ask ``season_is_historical`` the SAME question, which is
            # what keeps a frozen registration visible in its own history.
            if self.season_is_historical(season, now=now):
                continue
            stranded = self._games_scheduled_for_team_in_season(
                season_id, team_id)
            if stranded:
                blocked.append({"registration_id": reg.id,
                                "season_id": season_id,
                                "affected_game_ids": stranded})
                continue
            live_memberships = self._open_memberships_for_league_season_team(
                reg.league_season_id, team_id)
            if live_memberships:
                blocked_memberships.append({
                    "registration_id": reg.id, "season_id": season_id,
                    "affected_membership_ids":
                        [m.id for m in live_memberships]})
                continue
            to_move.append((reg, season_id))
        if blocked:
            raise ValidationError(
                "Cannot transfer this team while it has active registrations "
                "with scheduled games; resolve those games first.",
                {"reason": "team_transfer_strands_games", "team_id": team_id,
                 "blocked": blocked})
        if blocked_memberships:
            raise ValidationError(
                "Cannot transfer this team while it has live roster "
                "memberships on its current League; release/transfer them "
                "first.",
                {"reason": "team_transfer_strands_memberships",
                 "team_id": team_id, "blocked": blocked_memberships})

        # #331 review round 18: resolve each candidate's target LeagueSeason
        # and check for a row ALREADY sitting there before any write --
        # blindly rebinding `reg` onto it would either silently create a
        # second live row at that exact (team, LeagueSeason) identity
        # (Memory) or raise a raw unique_violation (SQLite/PostgreSQL)
        # instead of a structured rejection, and would abandon (rather than
        # reactivate) any INACTIVE history already sitting there. A
        # pre-existing ACTIVE row at the target, or two `to_move` candidates
        # from the SAME season both landing on the same target LeagueSeason
        # (the team already held concurrent active rows in two OTHER
        # leagues for that season -- exactly the kind of legacy Rule 7
        # violation this pre-scan exists to catch), are both unresolvable
        # conflicts: reject the WHOLE transfer with zero mutation, matching
        # every other check above, rather than guess which participation is
        # authoritative.
        target_conflicts = []
        resolved_targets = {}  # reg.id -> (target_ls, existing_at_target)
        claimed_by_season = {}  # season_id -> reg.id of the first claimant
        for reg, season_id in to_move:
            target_ls = self._link_league_season(new_league_id, season_id)
            # #331 review round 19: exact-key multiplicity (Memory-only
            # corrupted duplicate rows at this identical target key) is its
            # own unconditional conflict -- folded into the same rejection
            # shape as every other conflict this pre-scan already catches,
            # rather than trusting a bare first-match lookup to hide it.
            existing_at_target, _target_key_conflicts = (
                exact_registration_or_conflict(self.store, target_ls.id, team_id))
            conflict_with = list(_target_key_conflicts)
            if not conflict_with:
                if existing_at_target is not None and existing_at_target.active:
                    conflict_with = [existing_at_target.id]
                elif season_id in claimed_by_season:
                    conflict_with = [claimed_by_season[season_id]]
                else:
                    claimed_by_season[season_id] = reg.id
            if conflict_with:
                target_conflicts.append(
                    {"registration_id": reg.id, "season_id": season_id,
                     "conflicting_registration_ids": conflict_with})
            resolved_targets[reg.id] = (
                target_ls, None if _target_key_conflicts else existing_at_target)
        if target_conflicts:
            raise ValidationError(
                "Cannot transfer this team while it already has an active "
                "registration in the target league for the same season; "
                "resolve the conflict first.",
                {"reason": "team_registration_conflict", "team_id": team_id,
                 "conflicts": target_conflicts})

        # Apply — all writes happen only after every check passed.
        registrations_moved = []
        registrations_superseded = []
        for reg, season_id in to_move:
            target_ls, existing_at_target = resolved_targets[reg.id]
            if existing_at_target is not None:
                # A retained INACTIVE row already sits at the target --
                # reactivate IT in place and retire `reg` instead of moving
                # it there, preserving both rows' own identities (mirrors
                # assign_season_team_league's own supersede branch for the
                # identical situation).
                reg.active = False
                self.store.save_season_team_registration(reg)
                existing_at_target.active = True
                existing_at_target.division_id = None
                self.store.save_season_team_registration(existing_at_target)
                registrations_superseded.append({
                    "from_registration_id": reg.id,
                    "reactivated_registration_id": existing_at_target.id,
                    "season_id": season_id})
            else:
                reg.league_season_id = target_ls.id
                reg.division_id = None  # the old Division belonged to the old League
                self.store.save_season_team_registration(reg)
                registrations_moved.append(reg.id)
        team.league_id = new_league_id
        # Keep Program consistent with the new League when the Team had none.
        team.program_id = team.program_id or league.program_id
        self.store.save_team(team)
        self._audit("team_league_transferred", "team", team.id, actor_id,
                    {"from": old, "to": new_league_id,
                     "registrations_moved": registrations_moved,
                     "registrations_superseded": registrations_superseded})
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
        # #205 review round 1 finding 2 — a LIVE (non-terminal) membership on
        # this exact (LeagueSeason, Team) says a player currently participates
        # here; unregistering would strand it against a Team the operator just
        # said is no longer participating (create_season_roster_membership
        # itself refuses a NEW membership once the registration is inactive —
        # this closes the same gap for an EXISTING one). Terminal (released/
        # transferred) rows are closed history and never block, mirroring
        # cancelled games above.
        live_memberships = self._open_memberships_for_league_season_team(
            reg.league_season_id, reg.team_id)
        if live_memberships:
            raise ValidationError(
                "Cannot remove this team from the season while it has live "
                "roster memberships; release/transfer them first.",
                {"reason": "team_has_live_memberships",
                 "affected_membership_ids": [m.id for m in live_memberships],
                 "count": len(live_memberships)})
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
        # #205 review round 1 finding 2 — a membership names this exact
        # (LeagueSeason, Team) pair by REQUIRED (non-nullable) foreign key,
        # so a permanent delete here is exactly the "required FK" shape
        # games/regs already block on above: ANY membership blocks,
        # regardless of status (even released/transferred history still
        # names this row), mirroring delete_team/delete_league/delete_season
        # blocking on ANY registration regardless of active status.
        memberships = self.store.memberships_for_league_season_team(
            reg.league_season_id, reg.team_id)
        self._block_if_dependents(
            "season_team_registration", registration_id, "registration", [
                self._dep_group("game", games, self._matchup),
                self._dep_group("roster membership", memberships,
                                self._membership_label)])
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

    # -- season roster memberships (#205 Slice A) ---------------------------
    #
    # An athlete's participation stint for one Team in one Season, on the
    # permanent Team + LeagueSeason spine. This slice ships the bounded
    # lifecycle only — create, status change, season-scoped attribute edit —
    # each appending a SeasonRosterMembershipEvent (the per-membership
    # immutable history) plus the global SetupAuditLog entry. The
    # transfer/release/deadline-policy workflows and the consumer cutover
    # (roster/substitute eligibility resolving through memberships) are
    # follow-up #205 slices and deliberately absent here.

    def _membership_event(self, membership_id: str, action: str,
                          actor_id: Optional[str], reason: Optional[str],
                          detail: Optional[dict] = None
                          ) -> SeasonRosterMembershipEvent:
        """Append one immutable history event for a membership. Called by
        every membership mutation in this service — never skipped, so the
        per-membership history and the audit trail can only move together.

        ``seq`` (#205 review round 1 finding 4) is minted from the SAME
        counter step as ``id`` (one ``next_seq("srme")`` call, formatted into
        both) rather than a second ``next_id`` draw, so the numeric id suffix
        and the ordering key never drift apart. It is what
        ``events_for_membership`` orders by — never ``id`` (TEXT, sorts
        lexically) or ``at`` alone (an injected/shared clock can tie)."""
        n = self.store.next_seq("srme")
        return self.store.add_season_roster_membership_event(
            SeasonRosterMembershipEvent(
                id=f"srme_{n}",
                membership_id=membership_id,
                action=action,
                at=self.clock(),
                actor_id=actor_id,
                reason=reason,
                detail=detail or {},
                seq=n,
            ))

    def _validate_membership_status(self, status) -> MembershipStatus:
        """Type-safe status gate: a real MembershipStatus or its exact string
        value; anything else is a field-level validation error, never a
        TypeError/500."""
        if isinstance(status, MembershipStatus):
            return status
        try:
            return MembershipStatus(status)
        except (ValueError, TypeError):
            raise ValidationError(
                "status must be one of "
                + ", ".join(s.value for s in MembershipStatus) + ".",
                {"reason": "invalid_membership_status", "field": "status"})

    def _open_memberships_for_league_season_team(
            self, league_season_id: str, team_id: str
            ) -> list:
        """Every LIVE (non-terminal) membership at this exact (LeagueSeason,
        Team) pair (#205 review round 1 finding 2) — the set a parent
        mutation that stops this Team's participation there (unregister,
        League transfer) must not silently strand. Terminal (released/
        transferred) rows are closed history and are never returned."""
        return [m for m in self.store.memberships_for_league_season_team(
                    league_season_id, team_id)
                if not m.status.is_terminal]

    def _assert_membership_program_spine(self, team, ls, *,
                                         membership_id=None) -> None:
        """The membership spine's PROGRAM leg: the Team's own ``program_id``,
        the LeagueSeason's League's ``program_id`` and its Season's
        ``program_id`` must all be present and identical (#205 review round 4,
        owner ruling).

        This is the exact Python twin of the two PROGRAM checks migration
        059's preflight already applies to every backfill candidate —
        ``find_active_players_with_team_program_mismatch`` (``t.program_id``
        vs ``lg.program_id``) and ``find_active_players_with_program_
        mismatch`` (``lg.program_id`` vs ``s.program_id``) — compared with
        the same ``_missing_or_unequal`` rule, so MISSING and UNEQUAL are
        one violation and two missing keys are never agreement.

        WHY IT HAD TO BE ADDED. ``_assert_membership_spine_valid`` and
        ``create_season_roster_membership`` had NO Program clause at all —
        not a falsy-skip, an absence. All six shapes ({Team, League,
        Season}.program_id x {missing, unequal}) were reproduced on this
        branch's head 488d1c8 across Memory, SQLite and PostgreSQL: 059's
        preflight REFUSED every one while the service ACCEPTED create AND
        all twelve parked revivals, writing a row, an event and an audit
        entry each time. The migration therefore refused to materialize
        exactly the rows the live system was still minting.

        WHY IT IS UNCONDITIONAL, despite ``register_team_for_season``'s
        DELIBERATELY legacy-permissive rule 4 (``if team.program_id and
        team.program_id != season.program_id``, :1475). That guard tolerates
        a PROGRAM-LESS Team so pre-#283 legacy data can still be registered.
        The question this guard had to answer first was whether a SUPPORTED
        flow can PRODUCE such a Team and then need a membership on it. It
        cannot — established by execution against every public entry point:

          * ``create_team`` derives the Program from the resolved League and
            refuses a supplied Program that disagrees with it
            (``team_program_mismatch``), and refuses a league-less Team
            outright (``team_league_required``), so it never mints one;
          * every public League-creating path (``create_league``, the
            auto-provisioned default League) copies ``season.program_id``,
            and ``create_season`` requires a real Program — so League and
            Season always carry one;
          * ``register_team_for_season``'s legacy branch only PASSES THROUGH
            a program-less Team, and its non-null disagreement check is
            exact; the canonical ``league_id`` path refuses one outright;
          * ``transfer_team_to_league`` heals a program-less Team ONLY when
            the target League actually differs -- ``team.program_id =
            team.program_id or league.program_id`` sits below an
            ``if old == new_league_id: return team`` no-op, so calling it
            with the Team's current League repairs nothing. It refuses a
            non-null disagreement either way;
          * ``roll_forward_registrations`` and its v2 both refuse a
            program-less Team and refuse a cross-Program rollover;
          * ``commit_hierarchy_import`` refuses a cross-Program team/league
            pair (``team_league_program_mismatch``), and BOTH imports heal a
            pre-existing program-less Team on re-import.

        So the legacy-permissive registration guard is a door for legacy
        DATA, not a supported producer, and enforcing here refuses no spine
        any supported flow can build. Registration semantics are left
        exactly as they were — a legacy program-less Team still registers;
        it just cannot hold a MEMBERSHIP until its Program is repaired,
        which is precisely the state 059's preflight already demands before
        it will backfill one, and which ``transfer_team_to_league`` or
        either import already repairs."""
        league = (self.store.get_league(ls.league_id)
                  if ls.league_id else None)
        season = (self.store.get_season(ls.season_id)
                  if ls.season_id else None)
        league_program = league.program_id if league is not None else None
        season_program = season.program_id if season is not None else None
        if (_missing_or_unequal(team.program_id, league_program)
                or _missing_or_unequal(league_program, season_program)):
            details = {"reason": "membership_program_mismatch",
                       "team_id": team.id,
                       "league_season_id": ls.id,
                       "team_program_id": team.program_id,
                       "league_id": ls.league_id,
                       "league_program_id": league_program,
                       "season_id": ls.season_id,
                       "season_program_id": season_program}
            if membership_id is not None:
                details["membership_id"] = membership_id
            raise ValidationError(
                "A membership must sit on one Program: this Team, its "
                "League and the Season disagree about which Program they "
                "belong to (or one of them names none).",
                details)

    def _assert_membership_spine_valid(
            self, membership: SeasonRosterMembership) -> None:
        """Reactivation must recheck the SAME spine ``create_season_roster_
        membership`` required at birth (#205 review round 1 finding 2):
        Player, Team and LeagueSeason still exist, the Team still belongs to
        the LeagueSeason's League, the Team/League/Season still agree about
        their Program (#205 review round 4 owner ruling — see
        ``_assert_membership_program_spine``), and the Team still holds an
        ACTIVE registration there.

        Without this, a membership PARKED (inactive/injured) before its
        Team's registration was unregistered, or before the Team was
        transferred to a different League, could be waved back to
        ``active``/``applicant``/``affiliate`` even though the spine that
        justified it no longer holds — reactivating a stint the parent side
        has already ended. finding 2's OTHER fixes (unregister/transfer now
        block while a LIVE membership exists) prevent this for anything
        mutated going forward; this is the matching read-time guard for
        rows that predate those fixes, or whose parent state changed by any
        other route this store allows (e.g. a restored backup)."""
        team = self.store.get_team(membership.team_id)
        if team is None:
            raise ValidationError(
                "This membership's Team no longer exists.",
                {"reason": "membership_team_missing",
                 "membership_id": membership.id,
                 "team_id": membership.team_id})
        ls = self.store.get_league_season(membership.league_season_id)
        if ls is None:
            raise ValidationError(
                "This membership's League season no longer exists.",
                {"reason": "membership_league_season_missing",
                 "membership_id": membership.id,
                 "league_season_id": membership.league_season_id})
        # #205 review round 3 blocker 3 — a MISSING ``team.league_id`` is a
        # spine violation, not an exemption. See ``_missing_or_unequal``.
        if _missing_or_unequal(team.league_id, ls.league_id):
            raise ValidationError(
                "This membership's Team no longer sits in this League "
                "season's League (its League is missing, or has changed); "
                "it can no longer be reactivated as-is.",
                {"reason": "membership_league_mismatch",
                 "membership_id": membership.id, "team_id": team.id,
                 "team_league_id": team.league_id,
                 "league_season_league_id": ls.league_id})
        # #205 review round 4 (owner ruling) — the PROGRAM leg of the same
        # spine, the clause this helper never had. See
        # ``_assert_membership_program_spine``.
        self._assert_membership_program_spine(
            team, ls, membership_id=membership.id)
        registration = self.store.registration_for_team_in_league_season(
            membership.league_season_id, membership.team_id)
        if registration is None or not registration.active:
            raise ValidationError(
                "This membership's Team is no longer actively registered "
                "in this season; it can no longer be reactivated as-is.",
                {"reason": "team_not_registered",
                 "membership_id": membership.id, "team_id": team.id,
                 "league_season_id": membership.league_season_id})
        if self.store.get_player(membership.player_id) is None:
            raise ValidationError(
                "This membership's Player no longer exists.",
                {"reason": "membership_player_missing",
                 "membership_id": membership.id,
                 "player_id": membership.player_id})

    def _assert_membership_jersey_available(
            self, league_season_id: str, team_id: str, jersey_number,
            *, exclude_membership_id: Optional[str] = None) -> None:
        """Reject a jersey held by another ACTIVE membership of the same
        (LeagueSeason, Team) — #269's integrity at its season-Team scope.

        Mirrors :meth:`_assert_jersey_available` exactly: only an active
        membership reserves a number, only concrete numbers are constrained,
        and the raise is the SAME stable ``IntegrityConflictError`` migration
        059's ``ux_srm_active_team_jersey`` partial unique index produces on
        a lost cross-process race. Callers run this BEFORE mutating."""
        if jersey_number is None:
            return
        for other in self.store.memberships_for_league_season_team(
                league_season_id, team_id):
            if (other.status is MembershipStatus.ACTIVE
                    and other.jersey_number == jersey_number
                    and other.id != exclude_membership_id):
                raise IntegrityConflictError(
                    f"Jersey number {jersey_number} is already worn by an "
                    f"active membership on this team this season.",
                    {"reason": "duplicate_membership_jersey_number",
                     "league_season_id": league_season_id,
                     "team_id": team_id, "jersey_number": jersey_number,
                     "conflicting_membership_id": other.id})

    def _assert_no_active_membership_conflict(
            self, player_id: str, season_id: str,
            *, exclude_membership_id: Optional[str] = None) -> None:
        """One AUTHORITATIVE active membership per (player, Season) — the
        epic's core uniqueness rule. Affiliate/call-up rows are outside it by
        status. The database index (059) decides cross-process races; this
        pre-check turns the ordinary case into a named, actionable error."""
        conflicts = [m.id for m in
                     self.store.active_memberships_for_player_in_season(
                         player_id, season_id)
                     if m.id != exclude_membership_id]
        if conflicts:
            raise ValidationError(
                "Player already has an active membership this season; "
                "release/transfer it (or use affiliate status) first.",
                {"reason": "membership_active_conflict",
                 "player_id": player_id, "season_id": season_id,
                 "affected_membership_ids": conflicts})

    @_transactional
    def create_season_roster_membership(
            self, player_id: str, league_season_id: str, team_id: str,
            status=MembershipStatus.ACTIVE, position=None,
            jersey_number=_UNSET, shoots=_UNSET,
            reason: Optional[str] = None,
            actor_id: Optional[str] = None) -> SeasonRosterMembership:
        """Open a membership stint for a player on a Team's LeagueSeason
        (#205 Slice A).

        Spine rules, mirroring register_team_for_season: the Team is
        row-locked FIRST (canonical Team → Season → Player lock order, so a
        concurrent League transfer can't strand the membership on a foreign
        LeagueSeason), the Team must belong to the LeagueSeason's League
        (rule 7 analog), the Team/League/Season must agree about their
        Program (#205 review round 4 owner ruling — see
        ``_assert_membership_program_spine``), the Team must hold an ACTIVE
        SeasonTeamRegistration there, and the Season must not be archived. The player is NOT required to have
        ``player.team_id == team_id`` — that permanent coupling is exactly
        what memberships replace; legacy consumers keep reading the untouched
        Player fields until the cutover slice.

        ``position``/``jersey_number``/``shoots`` default to the player's
        current permanent values (the same copy the 059 backfill performs);
        pass explicit values — including ``None`` for jersey/shoots — to
        override. A terminal status can't be born (``released``/
        ``transferred`` rows exist only as ended stints), and an ``active``
        create enforces the one-active-per-(player, Season) rule.

        The Player row is locked (#205 review round 1 finding 1) BEFORE the
        open-stint/active-conflict re-reads below, not just the Team: two
        concurrent creates for the SAME player on DIFFERENT Teams of the SAME
        LeagueSeason take DIFFERENT Team locks, so without a lock keyed on
        the one thing they share (the player) both could observe "no open
        row" and both insert. The Player lock is that common lock — an
        under-lock re-read, mirroring the Team lock's own #201 discipline —
        and migration 059's ``ux_srm_open_player_league_season`` partial
        unique index is the engine-level backstop for any write that still
        reaches the table without it (translated to this SAME
        ``membership_open_conflict`` reason by ``db_errors.py``).
        """
        for field_name, value in (("player_id", player_id),
                                  ("league_season_id", league_season_id),
                                  ("team_id", team_id)):
            if not value or not isinstance(value, str):
                raise ValidationError(
                    f"{field_name} is required.",
                    {"reason": "field_required", "field": field_name})
        team = self.store.get_team_for_update(team_id)
        if team is None:
            raise NotFoundError(f"Team {team_id} not found.")
        ls = self.store.get_league_season(league_season_id)
        if ls is None:
            raise NotFoundError(f"League season {league_season_id} not found.")
        season = self._require_active_season(ls.season_id)  # #159 guard
        # #205 review round 1 finding 1 — row-lock the Player, the ONE thing
        # two concurrent creates on DIFFERENT Teams of the same LeagueSeason
        # share (their Team locks differ). Held to commit, so the open-stint
        # and active-conflict re-reads below are a genuine under-lock check,
        # not a stale snapshot a concurrent create raced past.
        player = self.store.get_player_for_update(player_id)
        if player is None:
            raise NotFoundError(f"Player {player_id} not found.")
        # #205 review round 3 blocker 3 — a MISSING ``team.league_id`` is a
        # spine violation, not an exemption. See ``_missing_or_unequal``.
        if _missing_or_unequal(team.league_id, ls.league_id):
            raise ValidationError(
                "A membership must sit on the Team's own League's season; "
                "the Team's League is missing, or is a different one.",
                {"reason": "membership_league_mismatch", "team_id": team_id,
                 "team_league_id": team.league_id,
                 "league_season_league_id": ls.league_id})
        # #205 review round 4 (owner ruling) — the PROGRAM leg of the spine,
        # which this method never checked at all while migration 059's
        # preflight refused every incoherent shape. See
        # ``_assert_membership_program_spine`` for the six reproduced shapes
        # and why enforcing here does not collide with
        # ``register_team_for_season``'s legacy-permissive rule 4.
        self._assert_membership_program_spine(team, ls)
        registration = self.store.registration_for_team_in_league_season(
            league_season_id, team_id)
        if registration is None or not registration.active:
            raise ValidationError(
                "Team is not actively registered in this season.",
                {"reason": "team_not_registered", "team_id": team_id,
                 "league_season_id": league_season_id})
        status = self._validate_membership_status(status)
        if status.is_terminal:
            raise ValidationError(
                "A membership cannot be created in a terminal status.",
                {"reason": "membership_status_terminal_create",
                 "status": status.value})
        canonical_position = self._validate_position(
            position if position is not None else player.position)
        jersey = (player.jersey_number if jersey_number is _UNSET
                  else jersey_number)
        self._validate_jersey_number(jersey)
        canonical_shoots = self._validate_shoots(
            player.shoots if shoots is _UNSET else shoots)
        # Uniqueness pre-checks (the 059 partial unique indexes decide any
        # cross-process race the same way, via IntegrityConflictError).
        open_rows = self.store.open_memberships_for_player_in_league_season(
            player_id, league_season_id)
        if open_rows:
            raise ValidationError(
                "Player already has an open membership on this league "
                "season; update or end it instead of creating another.",
                {"reason": "membership_open_conflict",
                 "affected_membership_ids": [m.id for m in open_rows]})
        if status is MembershipStatus.ACTIVE:
            self._assert_no_active_membership_conflict(player_id, season.id)
            self._assert_membership_jersey_available(
                league_season_id, team_id, jersey)
        membership = SeasonRosterMembership(
            id=self.store.next_id("srm"),
            player_id=player_id,
            league_season_id=league_season_id,
            season_id=season.id,
            team_id=team_id,
            status=status,
            position=canonical_position,
            jersey_number=jersey,
            shoots=canonical_shoots,
            effective_from=self.clock(),
            effective_to=None,
        )
        self.store.add_season_roster_membership(membership)
        self._membership_event(
            membership.id, "created", actor_id, reason,
            {"player_id": player_id, "league_season_id": league_season_id,
             "season_id": season.id, "team_id": team_id,
             "status": status.value, "position": canonical_position.value,
             "jersey_number": jersey, "shoots": canonical_shoots})
        self._audit("season_roster_membership_created",
                    "season_roster_membership", membership.id, actor_id,
                    {"player_id": player_id, "team_id": team_id,
                     "league_season_id": league_season_id,
                     "season_id": season.id, "status": status.value})
        return membership

    @_transactional
    def set_season_roster_membership_status(
            self, membership_id: str, status,
            reason: Optional[str] = None,
            actor_id: Optional[str] = None) -> SeasonRosterMembership:
        """Move a membership stint through its lifecycle (#205 Slice A).

        A terminal row (released/transferred) is immutable history: it can
        never transition again — the future #205 correction workflow, with
        privileged authorization + reason + audit, is the only thing that
        will ever revisit one, and a NEW stint is a new row. Entering
        ``active`` re-asserts both uniqueness rules (authoritative-per-
        (player, Season) and the season-Team jersey). Entering a terminal
        status stamps ``effective_to``. A no-op transition is rejected, not
        silently absorbed, so the event history never lies about a change
        that didn't happen.

        REVIVING a PARKED row (``inactive``/``injured``) into ``applicant``,
        ``affiliate`` or ``active`` revalidates the FULL Player/Team/
        LeagueSeason/Program/active-registration spine first (#205 review
        round 3 blocker 2, extended to the Program leg by round 4's owner
        ruling) — the same spine ``create_season_roster_membership``
        demands for those three statuses, and the same set
        ``_assert_membership_spine_valid`` names in its own contract. The
        UNIQUENESS rules stay ``active``-only: reviving to applicant or
        affiliate re-proves the spine without acquiring the one-open-stint
        or season-Team jersey constraints, exactly as before.

        Entering a TERMINAL status is UNCONDITIONALLY REFUSED — a hard
        ``NotAuthorizedError``, never reachable by any caller, actor_id or
        reason string (#205 review round 2, owner product ruling overriding
        round 1 finding 5's shipped "actor_id + reason" floor). That floor
        was reachable by any caller supplying an arbitrary non-blank string
        for each — an unvalidated string is not authorization, so it never
        actually stopped anyone from releasing or transferring a
        membership; it only stopped the SILENT/anonymous/unreasoned case.
        This PR (#212 Slice A) is scoped to schema/migration/compatibility
        proof only. The authorized transfer/release workflow — session-
        resolved principals, scope checks, a deadline/override policy,
        Game-state safeguards, atomic audit, tri-store/HTTP coverage — ships
        in a LATER #205 slice with its own review; until then, NOTHING on
        this surface may reach a terminal status. ``released``/
        ``transferred`` remain valid ENUM values (the schema and event model
        stay fully capable of representing them, including for a future
        backfill/migration path) — only SETTING one through this method is
        refused, unconditionally. A membership already born released/
        transferred (e.g. planted directly at the store layer, never through
        this method) is still immutable history exactly as before — that
        rule, above, is untouched."""
        membership = self.store.get_season_roster_membership_for_update(
            membership_id)
        if membership is None:
            raise NotFoundError(f"Membership {membership_id} not found.")
        self._require_active_season(membership.season_id)  # #159 guard
        status = self._validate_membership_status(status)
        if membership.status.is_terminal:
            raise ValidationError(
                "This membership has ended; its row is immutable history. "
                "Create a new membership for a new stint.",
                {"reason": "membership_terminal",
                 "membership_id": membership.id,
                 "status": membership.status.value})
        if status is membership.status:
            raise ValidationError(
                f"Membership is already {status.value}.",
                {"reason": "membership_status_unchanged",
                 "status": status.value})
        # #205 review round 3 blocker 2 — REVIVING a parked row revalidates
        # the spine for EVERY target that requires one, not only ``active``.
        # This guard used to be spelled ``if status is ACTIVE``, even though
        # ``_assert_membership_spine_valid``'s own contract has always named
        # ``active``/``applicant``/``affiliate``: the helper was right and
        # its single call site was wrong. A membership parked to inactive/
        # injured, whose SeasonTeamRegistration was then deactivated (or
        # whose Team/LeagueSeason/Player vanished, or whose Team moved
        # League), could be moved inactive->applicant or inactive->affiliate
        # and the new status was written on that dead spine — reproduced on
        # Memory, SQLite and PostgreSQL. It matters because ``create`` for
        # those same statuses REQUIRES an active registration, and the
        # parent-mutation guards treat every non-terminal membership as
        # live, so a restored backup or direct write could be re-exposed
        # through a status change.
        #
        # Scoped deliberately: the SOURCE must be parked and the TARGET must
        # be one create validates a spine for. The uniqueness rules below
        # stay ACTIVE-only — reviving to applicant/affiliate re-proves the
        # spine, and does NOT start applying the one-open-stint or jersey
        # rules to non-active statuses. Terminal targets never reach here:
        # ``is_terminal`` is disjoint from _REVIVING_MEMBERSHIP_STATUSES, so
        # the unconditional refusal below still fires first and unchanged.
        if status is MembershipStatus.ACTIVE or (
                membership.status in _PARKED_MEMBERSHIP_STATUSES
                and status in _REVIVING_MEMBERSHIP_STATUSES):
            # #205 review round 1 finding 2 — the spine check runs BEFORE
            # the uniqueness re-checks: a reactivation onto a spine that no
            # longer holds is invalid regardless of whether another active
            # membership or jersey happens to conflict.
            self._assert_membership_spine_valid(membership)
        if status is MembershipStatus.ACTIVE:
            self._assert_no_active_membership_conflict(
                membership.player_id, membership.season_id,
                exclude_membership_id=membership.id)
            self._assert_membership_jersey_available(
                membership.league_season_id, membership.team_id,
                membership.jersey_number,
                exclude_membership_id=membership.id)
        elif status.is_terminal:
            # #205 review round 2 — owner product ruling, overriding round
            # 1 finding 5's "actor_id + reason" floor: that floor was a
            # VALIDATED INPUT check, not authorization — any caller could
            # satisfy it by supplying an arbitrary non-blank string for
            # each, so it never actually stopped an unauthorized release or
            # transfer, only a silent/anonymous/unreasoned one. This slice
            # (#212 Slice A) is scoped to schema/migration/compatibility
            # proof only; the authorized workflow (session-resolved
            # principals, scope checks, deadline/override policy, Game-
            # state safeguards, atomic audit, tri-store/HTTP coverage)
            # ships in a LATER #205 slice. Until then this refuses
            # UNCONDITIONALLY — checked before any write/event/audit, and
            # never satisfiable by any actor_id or reason value, unlike the
            # floor it replaces.
            raise NotAuthorizedError(
                "Releasing or transferring a membership is not available "
                "through this method yet; the authorized transition "
                "workflow ships in a later slice.",
                {"reason": "terminal_transition_not_authorized",
                 "membership_id": membership.id, "status": status.value})
        previous = membership.status
        membership.status = status
        if status.is_terminal:
            # Currently unreachable through THIS method — the unconditional
            # refusal above always raises first for any terminal ``status``
            # — deliberately left in place rather than deleted: the later
            # #205 slice that ships the authorized transition workflow
            # replaces that refusal with real authorization checks and
            # reuses this exact stamping, rather than having to re-add it.
            membership.effective_to = self.clock()
        self.store.save_season_roster_membership(membership)
        self._membership_event(
            membership.id, "status_changed", actor_id, reason,
            {"from": previous.value, "to": status.value})
        self._audit("season_roster_membership_status_changed",
                    "season_roster_membership", membership.id, actor_id,
                    {"from": previous.value, "to": status.value})
        return membership

    @_transactional
    def update_season_roster_membership(
            self, membership_id: str, *, position=_UNSET,
            jersey_number=_UNSET, shoots=_UNSET,
            reason: Optional[str] = None,
            actor_id: Optional[str] = None) -> SeasonRosterMembership:
        """Correct a membership's SEASON-SCOPED attributes in place (#205
        Slice A) — id, spine, status and history unchanged.

        Partial and audited exactly like ``update_player``: a field left
        ``_UNSET`` is untouched; explicit ``None`` clears a nullable one
        (jersey/shoots; position is always concrete). Values reuse the shared
        validation (jersey range, membership-scoped active uniqueness,
        position/shoots gates). A genuine no-op writes nothing and appends
        no event, so the history never lies. Terminal rows are immutable."""
        membership = self.store.get_season_roster_membership_for_update(
            membership_id)
        if membership is None:
            raise NotFoundError(f"Membership {membership_id} not found.")
        self._require_active_season(membership.season_id)  # #159 guard
        if membership.status.is_terminal:
            raise ValidationError(
                "This membership has ended; its row is immutable history.",
                {"reason": "membership_terminal",
                 "membership_id": membership.id,
                 "status": membership.status.value})
        changed = {}
        if position is not _UNSET:
            new_position = self._validate_position(position)
            if new_position is not membership.position:
                changed["position"] = {
                    "from": membership.position.value
                    if membership.position else None,
                    "to": new_position.value}
                membership.position = new_position
        if jersey_number is not _UNSET:
            self._validate_jersey_number(jersey_number)
            if jersey_number != membership.jersey_number:
                if membership.status is MembershipStatus.ACTIVE:
                    self._assert_membership_jersey_available(
                        membership.league_season_id, membership.team_id,
                        jersey_number, exclude_membership_id=membership.id)
                changed["jersey_number"] = {
                    "from": membership.jersey_number, "to": jersey_number}
                membership.jersey_number = jersey_number
        if shoots is not _UNSET:
            new_shoots = self._validate_shoots(shoots)
            if new_shoots != membership.shoots:
                changed["shoots"] = {
                    "from": membership.shoots, "to": new_shoots}
                membership.shoots = new_shoots
        if not changed:
            return membership
        self.store.save_season_roster_membership(membership)
        self._membership_event(
            membership.id, "attributes_changed", actor_id, reason, changed)
        self._audit("season_roster_membership_updated",
                    "season_roster_membership", membership.id, actor_id,
                    {"changed_fields": sorted(changed)})
        return membership

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
            # #331 review round 18: resolved by exact (team, target
            # LeagueSeason) identity -- and, only when the Team has no row
            # there yet, its SOLE other active registration in this Season,
            # reused via the same in-place "move" transfer_team_to_league
            # itself performs -- never the first registration
            # registrations_for_season happens to return, which could
            # cannibalize an inactive HISTORICAL row (destroying what
            # transfer_team_to_league deliberately preserved) or collide
            # with an already-correct different active one. v1 has no
            # explicit-selection "reject on league mismatch" of its own (the
            # target League is always the Team's OWN permanent one), so
            # reusing the SOLE other active row here — moving it onto the
            # Team's actual permanent League — is a correction, not a
            # surprise, mirroring what transfer_team_to_league would do for
            # the identical drift. More than one other active row is a
            # genuine conflict rollover can't safely auto-resolve: reject
            # before any write, exactly like every other pre-write check
            # above.
            existing, _is_move, _conflict_ids = (
                self._resolve_import_row_registration(
                    to_season_id, tid, team.league_id))
            if _conflict_ids:
                raise ValidationError(
                    f"Team {tid} already has more than one active "
                    "registration in the target season; resolve the "
                    "conflict before rolling it forward.",
                    {"reason": "team_registration_conflict", "team_id": tid,
                     "affected_registration_ids": _conflict_ids})
            # #331 review round 18: the resolver's `other_active[0]` move
            # candidate is active BY CONSTRUCTION (it filters on `r.active`),
            # so checking `existing.active` alone here -- without also
            # excluding `_is_move` -- would treat every move candidate as
            # "already correctly registered" and skip it, silently leaving
            # the Team registered under the WRONG league forever (the move
            # branch below would then be unreachable dead code). Only a
            # non-move `existing` (the row already sitting at the exact
            # target identity) is a true skip.
            if existing is not None and existing.active and not _is_move:
                skipped += 1
                continue
            if existing is not None and _is_move:
                # `existing` is about to move OUT of its current league --
                # the same guard `assign_season_team_league` and
                # `commit_teams_players_import` apply before an identical
                # move: refuse when a committed, non-cancelled game in the
                # target season still references the row's CURRENT league,
                # rather than silently stranding that game's league binding.
                _old_league = self._registration_league_id(existing)
                _stranded = [
                    g.id for g in self.store.all_games()
                    if not g.cancelled and g.season_id == to_season_id
                    and g.league_id == _old_league
                    and tid in (g.home_team_id, g.away_team_id)]
                if _stranded:
                    raise ValidationError(
                        f"Team {tid} has a committed game referencing its "
                        "current league in the target season; resolve it "
                        "before rolling this team into a different league.",
                        {"reason": "registration_league_change_strands_games",
                         "team_id": tid, "affected_game_ids": _stranded})
            if existing is not None:  # reactivate inactive, or apply the move
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
        # #331 review round 19: a Team can hold more than one active source
        # registration in this Season (a Rule 7 violation legacy data / a
        # write path predating Rule 7 can leave behind) -- two selections
        # for the SAME team_id in one batch is unresolvable ambiguity,
        # counted up front so the gate below can reject it before any write
        # rather than letting `wanted[tid] = ...` silently keep only the
        # last one.
        _team_id_counts = {}
        for sel in selections:
            if isinstance(sel, dict) and isinstance(sel.get("team_id"), str):
                _team_id_counts[sel["team_id"]] = (
                    _team_id_counts.get(sel["team_id"], 0) + 1)
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
            if _team_id_counts.get(tid, 0) > 1:
                raise ValidationError(
                    f"Team {tid} has more than one selection in this "
                    "rollover batch; submit only one selection per team.",
                    {"reason": "rollover_duplicate_team_selection",
                     "team_id": tid})
            # #331 review round 19: when a selection names its SOURCE
            # registration explicitly (the frontend's Season rollover panel
            # always does, #331 review round 19 finding 4), it must resolve
            # to an ACTIVE row for EXACTLY this team in the source season --
            # carrying the operator's actual per-row identity all the way to
            # the write boundary, never trusting team_id alone to mean
            # "whichever of this team's rows happens to match."
            # registration_id is optional (back-compat with any caller that
            # predates this field) -- omitting it keeps the prior team_id-
            # only contract.
            reg_id = sel.get("registration_id")
            if reg_id is not None:
                if not isinstance(reg_id, str) or _blank(reg_id):
                    raise ValidationError(
                        "A selection's registration_id must be a "
                        "non-empty string when present.")
                src_reg = self.store.get_season_team_registration(reg_id)
                if (src_reg is None or src_reg.team_id != tid
                        or not src_reg.active
                        or self._season_of_league_season(
                            src_reg.league_season_id) != from_season_id):
                    raise ValidationError(
                        f"Registration {reg_id} is not an active "
                        f"source-season registration for team {tid}.",
                        {"reason": "rollover_registration_mismatch",
                         "team_id": tid, "registration_id": reg_id})
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
            target_ls = self._link_league_season(lid, to_season_id)
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
            #
            # #331 review round 18: resolved by this Team's exact (team,
            # target LeagueSeason) identity, plus an explicit scan for ANY
            # OTHER active registration elsewhere in the target Season --
            # never the first Season-wide row regardless of LeagueSeason,
            # which could silently miss or misidentify the row a mismatch
            # check above must catch. Unlike commit_teams_players_import's
            # own resolver (round 17), v2 rollover never silently MOVES an
            # existing active registration to match a selection -- that IS
            # the "selection's league silently ignored" contract violation
            # the comment above already guards against, just now reachable
            # even when the wrong-league row wasn't the one the old lookup
            # happened to find. The rule-7 check just above already proved
            # lid == team.league_id, so ANY other active row here is
            # necessarily a Rule 7 violation (legacy data, or a write path
            # predating Rule 7) -- always a conflict the operator must
            # resolve explicitly (assign_season_team_league /
            # transfer_team_to_league), never an implicit rollover side
            # effect.
            # #331 review round 19: exact-key multiplicity (Memory-only
            # corrupted duplicate rows at this identical target key) is its
            # own unconditional conflict, checked before the "any OTHER
            # active row" scan below so a corrupted exact-key pair is never
            # silently absorbed into `other_active` under only one of its
            # ids while the other goes unmentioned.
            existing, _target_key_conflicts = exact_registration_or_conflict(
                self.store, target_ls.id, tid)
            if _target_key_conflicts:
                raise ValidationError(
                    f"Team {tid} already has more than one registration at "
                    "the target league/season; resolve the conflict before "
                    "rolling this team forward.",
                    {"reason": "team_registration_conflict", "team_id": tid,
                     "affected_registration_ids": _target_key_conflicts})
            other_active = [
                r for r in self.store.registrations_for_season(to_season_id)
                if r.team_id == tid and r.active
                and r.league_season_id != target_ls.id]
            if other_active:
                raise ValidationError(
                    f"Team {tid} already has an active registration in "
                    "the target season under a different league; resolve "
                    "it before rolling this team forward.",
                    {"reason": "rollover_conflicts_active_registration",
                     "team_id": tid,
                     "affected_registration_ids": [r.id for r in other_active]})
            if existing is not None and existing.active and (
                    (existing.division_id or None) != div_id):
                raise ValidationError(
                    f"Team {tid} is already registered in the target season "
                    "under a different division than this selection; "
                    "resolve the existing registration first.",
                    {"reason": "rollover_conflicts_active_registration",
                     "team_id": tid, "registration_id": existing.id,
                     "expected_league_id": lid, "expected_division_id": div_id,
                     "actual_league_id": lid,
                     "actual_division_id": existing.division_id})
            wanted[tid] = (lid, div_id)

        return self._apply_registration_selections(
            to_season_id=to_season_id, from_season_id=from_season_id,
            wanted=wanted, actor_id=actor_id)

    def _apply_registration_selections(self, *, to_season_id: str,
                                       from_season_id: str, wanted: dict,
                                       actor_id: Optional[str]) -> dict:
        """Shared write-application core for "apply these Team/League/Division
        selections to this target Season" (#159 copy-forward), extracted
        verbatim from ``roll_forward_registrations_v2``'s own apply phase so
        there is exactly ONE implementation both it and the new-Season
        copy-forward commit call — never two that could silently drift.

        Takes an ALREADY-VALIDATED ``wanted: {team_id: (league_id,
        division_id)}`` mapping (each caller runs its own pre-write gate first
        — see ``roll_forward_registrations_v2`` and
        ``_resolve_copy_forward_plan`` — because what counts as a valid
        selection differs between "the target Season already exists" and "the
        target Season does not exist until this very commit creates it"); this
        helper only performs the upsert/reactivate + per-row and summary audit
        that is identical either way.

        MUST run inside the caller's transaction, with ``to_season_id`` (if it
        already existed before this call) and every League named in
        ``wanted`` already row-locked by the caller in the canonical
        Team -> League -> Season order (#159) — this helper acquires no lock
        of its own beyond ``_link_league_season``'s own re-entrant League
        lock when it creates a new binding. A freshly-minted ``to_season_id``
        (the copy-forward commit's own case) needs no lock: nothing else can
        reference a Season id before this transaction that created it
        commits.
        """
        rolled, skipped, created = 0, 0, []
        for tid, (lid, div_id) in wanted.items():
            # #283: the registration is stored against the League's LeagueSeason
            # in the target Season (find-or-create; idempotent).
            target_ls = self._link_league_season(lid, to_season_id)
            # #331 review round 18: resolved by the SAME exact (team, target
            # LeagueSeason) identity the gate above already validated --
            # never the first Season-wide row regardless of LeagueSeason.
            # The gate's own scan already proved no OTHER active row exists
            # for this team in the target Season, so this exact lookup is
            # unambiguous by construction.
            #
            # #331 review round 19: uses the SAME exact-key wrapper the gate
            # above uses, not the raw store primitive -- nothing in this
            # single transactional call can change state between the gate
            # and here, so a conflict at this point should never happen, but
            # this must still never fall back to guessing a row if it
            # somehow did (a future refactor bug, not a possibility today).
            existing, _target_key_conflicts = exact_registration_or_conflict(
                self.store, target_ls.id, tid)
            if _target_key_conflicts:
                raise ValidationError(
                    f"Team {tid} already has more than one registration at "
                    "the target league/season; resolve the conflict before "
                    "rolling this team forward.",
                    {"reason": "team_registration_conflict", "team_id": tid,
                     "affected_registration_ids": _target_key_conflicts})
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

    # -- new-Season copy-forward preview/commit (#159) ----------------------
    # "A new Season can be previewed/copied forward without mutating the
    # prior Season." Composes create_season (via _resolve_season_creation)
    # with roll_forward_registrations_v2's own selection machinery (via
    # _apply_registration_selections), modeled directly on
    # preview_ice_availability/commit_ice_availability's fingerprint pattern
    # (#158): preview is side-effect-free (plus an optional audit row) and
    # returns a ``copy_forward_fingerprint`` binding the ENTIRE resolved plan;
    # commit refuses before any write unless that fingerprint (a) is
    # supplied, (b) matches a FRESH re-resolution of the plan against current
    # state, and (c) matches a successful preview audit by the SAME actor —
    # then creates the Season and applies the selections in ONE transaction.
    #
    # DIVISION CONTRACT (the trickiest part of this slice, decided and
    # documented here): a selection's optional ``division_id`` cannot name a
    # row in the TARGET Season the way roll_forward_registrations_v2's own
    # ``division_id`` does, because the target Season (and therefore any
    # target LeagueSeason/Division) does not exist until commit creates it,
    # and this slice deliberately builds no machinery to fabricate a matching
    # target Division. So ``division_id`` here instead names a Division the
    # team occupies TODAY, under the SOURCE Season's LeagueSeason for the
    # same League — proving the selection is a coherent "carry this team's
    # current division assignment forward" request rather than a fabricated
    # id — and a division_id that does not resolve there refuses the ENTIRE
    # batch (``division_needs_target_creation``) rather than silently
    # guessing. Even a VALID source-side division_id is never written onto
    # the new registration (no target Division can exist yet): the created
    # registration always carries ``division_id=None``, and the preview
    # response marks each such selection ``division_pending: true`` so the
    # operator sees, before committing, exactly which teams will land without
    # a Division and need one assigned afterward (create the Division under
    # the new Season, then reassign — both already-existing tools, out of
    # scope to extend further here).
    # -- copy-forward commit ledger: immutable response snapshot (#159
    # review round 3, owner P1, structural change 1) ------------------------
    # Explicit, non-generic (de)serializers for exactly the two dataclasses
    # a commit's response carries — NOT a reflective/generic dataclass walk
    # — so a datetime/enum field is never silently handed to ``json.dumps``
    # unconverted, and reconstruction always produces a real ``Season``/
    # ``SeasonTeamRegistration`` instance (not a plain dict) so the facade's
    # existing ``_serialize``/``_registration_dict`` keep working completely
    # unchanged on a replay — they cannot tell a reconstructed row from a
    # freshly queried one. Deliberately local to this module rather than
    # importing api/service.py's own ``_jsonify``: services/ must not depend
    # on api/, the reverse of this codebase's layering (see CLAUDE.md).
    @staticmethod
    def _copy_forward_season_snapshot(season: Season) -> dict:
        return {
            "id": season.id, "program_id": season.program_id,
            "name": season.name,
            "start_date": (season.start_date.isoformat()
                           if season.start_date else None),
            "end_date": (season.end_date.isoformat()
                        if season.end_date else None),
            "external_ref": season.external_ref,
            "status": season.status.value if season.status else None,
            "archived_at": (season.archived_at.isoformat()
                            if season.archived_at else None),
        }

    @staticmethod
    def _copy_forward_season_from_snapshot(data: dict) -> Season:
        return Season(
            id=data["id"], program_id=data.get("program_id"),
            name=data.get("name"),
            start_date=(datetime.fromisoformat(data["start_date"])
                       if data.get("start_date") else None),
            end_date=(datetime.fromisoformat(data["end_date"])
                     if data.get("end_date") else None),
            external_ref=data.get("external_ref"),
            status=(SeasonStatus(data["status"]) if data.get("status")
                   else SeasonStatus.ACTIVE),
            archived_at=(datetime.fromisoformat(data["archived_at"])
                        if data.get("archived_at") else None))

    def _copy_forward_registration_snapshot(
            self, reg: SeasonTeamRegistration) -> dict:
        """... plus the registration's PUBLIC identity -- ``season_id``/
        ``league_id`` -- resolved via its LeagueSeason ONCE, right now,
        the SAME way ``ApiService._registration_dict`` resolves them (#159
        review round 4, owner P1 finding 2). Before this round only the
        domain fields were frozen; ``_registration_dict`` re-resolved
        ``season_id``/``league_id`` from the LIVE LeagueSeason on EVERY
        call, replay included, so deleting the target registration's
        LeagueSeason binding after commit made a replay of an
        already-successful fingerprint come back with
        ``season_id: null, league_id: null`` instead of the values the
        original commit actually returned -- response_snapshot was not
        the immutable API response it claimed to be. Freezing them here,
        alongside every other field this snapshot already freezes, closes
        that: a replay never needs to touch ``league_seasons`` again. See
        ``_copy_forward_result_from_ledger_row``, which builds
        ``registration_identities`` from these two fields instead of
        deferring to a live lookup, and
        ``ApiService.commit_new_season_copy_forward``, which prefers that
        map over ``_registration_dict``'s own resolution."""
        ls = (self.store.get_league_season(reg.league_season_id)
              if reg.league_season_id else None)
        return {"id": reg.id, "league_season_id": reg.league_season_id,
                "team_id": reg.team_id, "division_id": reg.division_id,
                "active": reg.active,
                "season_id": ls.season_id if ls else None,
                "league_id": ls.league_id if ls else None}

    @staticmethod
    def _copy_forward_registration_from_snapshot(
            data: dict) -> SeasonTeamRegistration:
        return SeasonTeamRegistration(
            id=data["id"], league_season_id=data.get("league_season_id"),
            team_id=data.get("team_id"), division_id=data.get("division_id"),
            active=bool(data.get("active")))

    def _copy_forward_response_snapshot(self, *, season, registrations,
                                        totals) -> dict:
        """The full immutable snapshot stored on the ledger row at commit
        time — season + registrations + totals, every value already
        JSON-safe. See migration 054 and ``SeasonCopyForwardCommit.
        response_snapshot``."""
        return {
            "season": self._copy_forward_season_snapshot(season),
            "registrations": [self._copy_forward_registration_snapshot(r)
                              for r in registrations],
            "totals": dict(totals),
        }

    @staticmethod
    def _copy_forward_registration_identities(snapshot: dict) -> dict:
        """``{registration_id: {"season_id":, "league_id":}}`` for every
        registration a ``_copy_forward_response_snapshot`` snapshot
        carries (#159 review round 4, owner P1 finding 2) -- the ONE place
        that reads the ``season_id``/``league_id`` a registration snapshot
        froze, so a fresh commit's own response and every future replay of
        it are built from the exact same values, by construction. See
        ``ApiService.commit_new_season_copy_forward``, the sole reader."""
        return {r["id"]: {"season_id": r.get("season_id"),
                          "league_id": r.get("league_id")}
                for r in snapshot.get("registrations", []) if r.get("id")}

    @staticmethod
    def _copy_forward_request_identity(*, actor_id, program_id, name,
                                       start_date, end_date,
                                       source_season_id, selections) -> dict:
        """The RAW caller-supplied identity of a copy-forward commit
        request (#159 review round 4, owner P1 finding 1) -- the acting
        actor plus every field ``_resolve_copy_forward_plan`` takes as
        input, captured EXACTLY as submitted: no store lookup, no
        normalization.

        Deliberately NOT ``_resolve_copy_forward_plan``'s own ``reviewed``
        dict (the structure ``fingerprint`` hashes), which is enriched
        with Program/Team/League/Division NAMES looked up from the store.
        Re-deriving that enrichment on every early-replay check would mean
        re-running store-dependent resolution before the ledger is even
        consulted -- reintroducing, for the REQUEST side of a replay
        decision, the exact staleness hazard #159 review round 3 closed
        for the RESPONSE side: a later, unrelated rename elsewhere must
        never change whether an already-successful replay is honored, any
        more than it may change what that replay returns. Comparing this
        raw, input-only identity is what lets a replay decision stay a
        pure function of "what did THIS caller just submit", with zero
        store access -- see ``_copy_forward_request_identity_matches``.

        Returns a plain dict wrapping whatever the caller passed for
        ``selections`` -- possibly a mutable list of mutable dicts the
        caller still owns and can go on mutating after this call returns.
        This function's OWN result is therefore still mutable and must
        NEVER be persisted as-is; see ``_copy_forward_canonical_json``, and
        the SOLE two call sites of this method
        (``_copy_forward_request_identity_matches``, and the ledger INSERT
        in ``commit_new_season_copy_forward``), both of which canonicalize
        this dict into an immutable string before it is compared or stored
        (#159 review round 5, owner P1 finding 1).
        """
        return {
            "actor_id": actor_id, "program_id": program_id, "name": name,
            "start_date": start_date, "end_date": end_date,
            "source_season_id": source_season_id, "selections": selections,
        }

    @staticmethod
    def _copy_forward_canonical_json(value) -> str:
        """The SAME canonicalization ``_resolve_copy_forward_plan`` already
        uses to turn its resolved ``reviewed`` plan into ``fingerprint`` —
        reused here (#159 review round 4) so two request-identity payloads
        compare equal whenever they are STRUCTURALLY equal, regardless of
        dict key order or a list-vs-tuple difference introduced by a JSON
        round trip through SQL storage."""
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          default=str)

    def _copy_forward_request_identity_matches(self, row, *, actor_id,
                                                program_id, name, start_date,
                                                end_date, source_season_id,
                                                selections) -> bool:
        """True iff the CURRENT actor and submitted request, canonicalized
        the same way, are byte-identical to the immutable
        ``request_identity`` the ledger row ``row`` stored at the commit
        that actually produced it (#159 review round 4, owner P1 finding
        1). A pure comparison of caller-supplied values against an
        immutable stored record -- no store access, so it can never be
        affected by (and can never itself trigger) any later, unrelated
        mutation. See ``_copy_forward_request_identity`` for exactly what
        is compared and why it is the RAW request rather than the
        resolved plan.

        ``row.request_identity`` is ITSELF already the canonical JSON
        string ``_copy_forward_canonical_json`` produces (#159 review
        round 5, owner P1 finding 1 -- see that method's own docstring and
        the ledger INSERT in ``commit_new_season_copy_forward``, the only
        writer of this column): a plain Python string, immutable by
        construction, frozen at commit time from whatever the ORIGINAL
        caller's ``selections`` looked like at that exact moment. Building
        ``current`` fresh here and comparing it directly against that
        already-canonical string -- rather than re-canonicalizing
        ``row.request_identity`` too, the way this comparison worked before
        this round -- means there is no live dict/list graph on EITHER side
        of this comparison for a later mutation of the caller's own
        ``selections`` object to reach: the stored side was already
        collapsed to an immutable string the instant it was written, no
        matter what InMemoryStore does or does not copy on write/read.

        Called before EVERY return of an already-committed ledger row's
        response -- both the early pre-lock check and the post-lock
        race-check backstop route through this via the shared
        ``_copy_forward_owned_ledger_result`` helper (#159 review round 5,
        owner P1 finding 2) -- so a mismatch here is never itself fatal;
        each CALLER decides what a mismatch means for its own control flow
        (fall through vs. raise a stable refusal). See
        ``_copy_forward_owned_ledger_result``.
        """
        current = self._copy_forward_canonical_json(
            self._copy_forward_request_identity(
                actor_id=actor_id, program_id=program_id, name=name,
                start_date=start_date, end_date=end_date,
                source_season_id=source_season_id, selections=selections))
        return current == (row.request_identity or "")

    def _copy_forward_owned_ledger_result(self, row, *, actor_id,
                                          program_id, name, start_date,
                                          end_date, source_season_id,
                                          selections):
        """The ONE gate every site in ``commit_new_season_copy_forward``
        that can return an already-committed ledger row's response must
        call (#159 review round 5, owner P1 finding 2) -- so the SAME
        actor/request-identity comparison governs every such site, and a
        THIRD site added later cannot reintroduce this bug by omission
        simply by forgetting to call ``_copy_forward_request_identity_
        matches`` itself.

        Returns the row's response (via ``_copy_forward_result_from_
        ledger_row``) if ``row`` is not None AND its stored identity
        matches the CURRENT actor/request; returns ``None`` otherwise --
        for BOTH ``row is None`` (nothing to return) and an identity
        mismatch (something exists, but it is not THIS caller's to
        collect). Callers cannot tell those two ``None`` cases apart from
        the return value alone, which is intentional: whichever refusal a
        caller raises when this returns ``None`` must not depend on WHY it
        was ``None``, or a mismatch would be distinguishable from an
        ordinary "nothing here yet" — the same non-disclosure property
        ``_copy_forward_request_identity_matches`` already documents.

        Before this round, the two return sites in
        ``commit_new_season_copy_forward`` (the early pre-lock check, and
        the post-lock race-check backstop) each decided independently
        whether to return a ledger row's response. The early check called
        ``_copy_forward_request_identity_matches`` directly; the post-lock
        backstop called NOTHING -- ``if already_committed is not None:
        return ...`` was unconditional, so it returned ANY row with a
        matching fingerprint regardless of who committed it. Concretely:
        actor A previews+commits a plan; actor B independently previews
        the IDENTICAL plan (the fingerprint hashes the resolved plan only,
        never the actor, so two different actors submitting the same plan
        get the same fingerprint) and holds their OWN valid preview audit
        for it. Actor B's commit correctly fails the EARLY check (identity
        mismatch), falls through, and independently re-passes both the
        fingerprint-match and preview-audit gates below (their own
        preview really was valid) -- landing on the post-lock backstop,
        which handed back actor A's Season with no further check at all.
        See ``test_second_actor_who_also_previewed_the_identical_plan_is_
        refused_not_given_the_winners_season`` (this file) for the
        regression coverage, sequential AND concurrent, both arrival
        orders.
        """
        if row is None:
            return None
        if not self._copy_forward_request_identity_matches(
                row, actor_id=actor_id, program_id=program_id, name=name,
                start_date=start_date, end_date=end_date,
                source_season_id=source_season_id, selections=selections):
            return None
        return self._copy_forward_result_from_ledger_row(row)

    def _has_matching_copy_forward_preview_audit(self, actor_id, fingerprint):
        """True if ``actor_id`` recorded a successful
        ``new_season_copy_forward_previewed`` audit for exactly
        ``fingerprint`` — the commit preview gate, mirroring
        ``_has_matching_preview_audit`` (#158) exactly."""
        if actor_id is None or fingerprint is None:
            return False
        for entry in self.store.all_setup_audit():
            if (entry.action == "new_season_copy_forward_previewed"
                    and entry.actor_id == actor_id
                    and entry.detail.get("copy_forward_fingerprint")
                    == fingerprint):
                return True
        return False

    def _resolve_copy_forward_plan(self, *, program_id, name, start_date,
                                   end_date, source_season_id, selections):
        """Deterministic, side-effect-free planning shared by preview and
        commit (#159 — mirrors ``_plan_ice_availability``'s #158 role
        exactly): resolve the would-be Season's fields via
        ``_resolve_season_creation`` (Program exists, timezone-anchored
        dates, end >= start — the SAME checks ``create_season`` itself runs),
        validate the source Season and every selection the same way
        ``roll_forward_registrations_v2`` validates its own (team exists,
        belongs to the program, has a permanent league_id matching the
        selection, a named division belongs to the SOURCE LeagueSeason — see
        the division contract above), and hash the resolved, operator-visible
        plan into a fingerprint. Commit calls this AGAIN under its own Team/
        League locks, so the bound snapshot is the one its write acts on —
        any drift (a source registration deactivated, a team moved to
        another league, a division renamed) changes the fingerprint and
        forces a fresh preview, exactly like the ice-availability pattern.
        """
        program, clean_name, start, end = self._resolve_season_creation(
            program_id, name, start_date, end_date)
        if not isinstance(source_season_id, str) or _blank(source_season_id):
            raise ValidationError(
                "source_season_id must be a non-empty string.")
        src = self.store.get_season(source_season_id)
        if src is None:
            raise NotFoundError(f"Season {source_season_id} not found.")
        # Cross-Program refusal (#159): mirrors roll_forward_registrations_v2's
        # own src.program_id != dst.program_id check, except the "destination"
        # here is the REQUESTED program_id for the new Season (it has no
        # Season of its own yet to compare against).
        if (src.program_id or None) != (program_id or None):
            raise ValidationError(
                "The source season belongs to a different program than the "
                "new season.",
                {"reason": "cross_program_source_season",
                 "program_id": program_id,
                 "source_season_id": source_season_id})
        if not isinstance(selections, list) or not selections:
            raise ValidationError(
                "Copy-forward requires a non-empty selections list; each "
                "selection needs a target league_id.")
        source_active = {
            r.team_id for r in self.store.registrations_for_season(
                source_season_id) if r.active}
        # Same round-19 defense roll_forward_registrations_v2 uses: two
        # selections for the same team_id in one batch is unresolvable
        # ambiguity, caught before any selection is otherwise processed.
        team_id_counts = {}
        for sel in selections:
            if isinstance(sel, dict) and isinstance(sel.get("team_id"), str):
                team_id_counts[sel["team_id"]] = (
                    team_id_counts.get(sel["team_id"], 0) + 1)
        rows = []
        for sel in selections:
            if not isinstance(sel, dict):
                raise ValidationError(
                    "Each selection must be an object with a team_id and "
                    "league_id.")
            tid = sel.get("team_id")
            if not isinstance(tid, str) or _blank(tid):
                raise ValidationError(
                    "Each selection needs a non-empty team_id.")
            if tid not in source_active:
                raise ValidationError(
                    f"Team {tid} is not registered in the source season.")
            if team_id_counts.get(tid, 0) > 1:
                raise ValidationError(
                    f"Team {tid} has more than one selection in this "
                    "copy-forward batch; submit only one selection per team.",
                    {"reason": "rollover_duplicate_team_selection",
                     "team_id": tid})
            # Optional explicit source registration identity (#331 round 19
            # parity): when supplied, must resolve to an ACTIVE row for
            # EXACTLY this team in the source season.
            reg_id = sel.get("registration_id")
            if reg_id is not None:
                if not isinstance(reg_id, str) or _blank(reg_id):
                    raise ValidationError(
                        "A selection's registration_id must be a "
                        "non-empty string when present.")
                src_reg = self.store.get_season_team_registration(reg_id)
                if (src_reg is None or src_reg.team_id != tid
                        or not src_reg.active
                        or self._season_of_league_season(
                            src_reg.league_season_id) != source_season_id):
                    raise ValidationError(
                        f"Registration {reg_id} is not an active "
                        f"source-season registration for team {tid}.",
                        {"reason": "rollover_registration_mismatch",
                         "team_id": tid, "registration_id": reg_id})
            lid = sel.get("league_id")
            if not isinstance(lid, str) or _blank(lid):
                raise ValidationError(
                    f"Selection for team {tid} needs a target league_id.")
            league = self.store.get_league(lid)
            if league is None:
                raise NotFoundError(f"League {lid} not found.")
            if (league.program_id or None) != (program_id or None):
                raise ValidationError(
                    f"League {lid} belongs to a different program than the "
                    "new season.",
                    {"reason": "league_program_mismatch", "league_id": lid})
            team = self.store.get_team(tid)
            if team is None:
                raise ValidationError(
                    f"Team {tid} in the source season no longer exists; it "
                    "cannot be copied forward.")
            if (team.program_id or None) != (program_id or None):
                raise ValidationError(
                    f"Team {tid} belongs to a different program than this "
                    "copy-forward; it cannot be carried into the new "
                    "season.")
            # #283 Slice E parity: a Team may only be carried into its OWN
            # permanent League (rule 7) — never a sole/latest/default guess.
            if (team.league_id or None) != lid:
                raise ValidationError(
                    f"Team {tid} can only roll into its permanent league "
                    f"{team.league_id or 'none'}, not {lid}.",
                    {"reason": "rollover_league_not_team_league",
                     "team_id": tid, "team_league_id": team.league_id,
                     "selected_league_id": lid})
            div_id = sel.get("division_id")
            source_division_id = None
            source_division_name = None
            if div_id is not None:
                if not isinstance(div_id, str):
                    raise ValidationError(
                        "A selection's division_id must be a string or "
                        "null.")
                division = self.store.get_division(div_id)
                div_ls = (self.store.get_league_season(
                    division.league_season_id)
                    if division is not None else None)
                # The DIVISION CONTRACT above: resolved against the SOURCE
                # season's LeagueSeason for this selection's league, never a
                # target one (it does not exist yet).
                if (division is None or div_ls is None
                        or div_ls.season_id != source_season_id
                        or div_ls.league_id != lid):
                    raise ValidationError(
                        f"Division {div_id} does not exist under the source "
                        f"season's league {lid}; the new season will need "
                        "this Division created before this team's division "
                        "can be carried forward. Remove this selection's "
                        "division_id, or create the matching Division in "
                        "the source season first.",
                        {"reason": "division_needs_target_creation",
                         "team_id": tid, "division_id": div_id,
                         "league_id": lid})
                source_division_id = division.id
                source_division_name = division.name
            rows.append({
                "team_id": tid, "team_name": team.name,
                "league_id": lid, "league_name": league.name,
                "registration_id": reg_id,
                "source_division_id": source_division_id,
                "source_division_name": source_division_name,
            })
        reviewed = {
            "program_id": program_id, "program_name": program.name,
            "name": clean_name,
            "start_date": start.isoformat() if start else None,
            "end_date": end.isoformat() if end else None,
            "source_season_id": source_season_id,
            "source_season_name": src.name,
            "selections": [
                {**row, "division_pending": row["source_division_id"]
                 is not None}
                for row in rows],
            "totals": {
                "teams": len(rows),
                "leagues": len({row["league_id"] for row in rows}),
                "divisions_pending": sum(
                    1 for row in rows
                    if row["source_division_id"] is not None),
            },
        }
        fingerprint = hashlib.sha256(json.dumps(
            reviewed, sort_keys=True, separators=(",", ":"), default=str
        ).encode()).hexdigest()[:16]
        return {"program": program, "name": clean_name, "start": start,
                "end": end, "src": src, "rows": rows, "reviewed": reviewed,
                "fingerprint": fingerprint}

    def preview_new_season_copy_forward(self, *, program_id=None, name=None,
                                        start_date=None, end_date=None,
                                        source_season_id=None,
                                        selections=None, actor_id=None
                                        ) -> dict:
        """Preview a new-Season copy-forward (#159): the fully-resolved plan
        for creating ``name`` under ``program_id`` and carrying
        ``selections`` forward from ``source_season_id`` — side-effect-free
        except for a server-attributed ``new_season_copy_forward_previewed``
        audit row on a SUCCESSFUL preview by an authenticated caller (an
        invalid plan raises before the audit; ``actor_id`` None records
        nothing), mirroring ``preview_ice_availability`` (#158) exactly."""
        plan = self._resolve_copy_forward_plan(
            program_id=program_id, name=name, start_date=start_date,
            end_date=end_date, source_season_id=source_season_id,
            selections=selections)
        resp = {**plan["reviewed"],
                "copy_forward_fingerprint": plan["fingerprint"]}
        if actor_id is not None:
            with self.store.transaction():
                self._audit(
                    "new_season_copy_forward_previewed", "program",
                    program_id, actor_id, {
                        "copy_forward_fingerprint": plan["fingerprint"],
                        "program_id": program_id,
                        "source_season_id": source_season_id,
                        "team_ids": [row["team_id"] for row in plan["rows"]],
                        "totals": resp["totals"],
                    })
        return resp

    @_transactional
    def commit_new_season_copy_forward(self, *, program_id=None, name=None,
                                       start_date=None, end_date=None,
                                       source_season_id=None,
                                       selections=None,
                                       copy_forward_fingerprint=None,
                                       actor_id=None) -> dict:
        """Commit a previewed new-Season copy-forward (#159): atomically
        create the Season and carry the previewed Team/League selections
        forward (never Division — see the contract above), requiring
        ``copy_forward_fingerprint`` to (a) be supplied, (b) match a FRESH
        re-resolution of the plan against current state, and (c) match a
        successful preview audit for this SAME actor — all three checked
        BEFORE any write, mirroring ``commit_ice_availability`` (#158)
        exactly. A refused commit mutates nothing: no Season row, no
        registrations, no audit beyond the refusal itself.

        IDEMPOTENT on the fingerprint (#159 review round 2 — supersedes the
        original "not single-use" design, which the owner ruled a real
        double-submit/retry blocker, not an acceptable tradeoff): a second
        commit that reuses the SAME still-valid fingerprint — a client
        retry, a double-click, or two genuinely concurrent requests racing
        — does NOT mint a second, distinct Season. A durable ledger
        (``season_copy_forward_commits``, migration 053's UNIQUE index on
        ``copy_forward_fingerprint``) records which Season each fingerprint
        actually produced and — since #159 review round 3 — the FULLY-
        RESOLVED response itself (``response_snapshot``, migration 054); a
        replay returns that immutable snapshot instead of creating a
        second Season — the standard REST idempotency-key replay, exactly
        as if this caller's own request had simply been re-delivered.

        REPLAY ORDER, AND WHY IT CHANGED (#159 review round 3, owner P1):
        the ledger is now consulted FIRST — keyed by the caller-supplied
        ``copy_forward_fingerprint`` directly, before any Team/League lock
        and before ``_resolve_copy_forward_plan`` re-validates anything —
        and, on a hit, ``_copy_forward_result_from_ledger_row`` deserializes
        ``response_snapshot`` and returns it WITHOUT touching the
        ``seasons`` or ``season_team_registrations`` tables at all. Before
        this round, the plan was re-resolved (and the ledger only checked
        AFTER) — which re-validated the SOURCE Season's current
        registrations, so once the selected Team was unregistered from the
        source Season, a byte-identical replay of an already-successful
        fingerprint raised ``Team ... is not registered in the source
        season`` instead of returning the original result. Worse, the OLD
        replay path rebuilt its response by re-fetching
        ``registration_ids`` from the CURRENT store — so once the target
        registration (or the target Season itself) was deleted through an
        otherwise fully supported operation, a replay silently returned
        ``registrations: []`` beside a ledger that still said
        ``rolled_forward: 1``, or (worse) ``season: null``. All three are
        closed the same way: the ledger hit is authoritative and
        self-contained, so nothing that happens to the source Season, the
        target registrations, or (per ``delete_season``'s own itemized
        ``copy_forward_commit`` dependency, added the same round) the
        target Season can ever change what a replay of a given fingerprint
        returns. The full plan re-resolution below still runs — but ONLY
        when there is no ledger hit, i.e. either a genuinely new
        fingerprint or the race-loser path the next paragraph describes.

        THE LEDGER HIT IS AUTHORITATIVE FOR ITS OWN REQUEST ONLY (#159
        review round 4, owner P1 finding 1) — round 3's "authoritative and
        self-contained" above described the RESPONSE side; it said nothing
        about who may collect it. Before this round the early check
        trusted ANY caller-supplied ``copy_forward_fingerprint`` that
        matched a row, full stop — a fingerprint is only a hash of the
        ORIGINAL committer's resolved plan, and this check ran before
        ``_resolve_copy_forward_plan``/``_has_matching_copy_forward_
        preview_audit`` ever looked at the CURRENT actor or CURRENT body
        at all. A second actor who learned (or guessed) an already-
        committed fingerprint — their own earlier preview response, a
        browser history entry, a server log — could submit an entirely
        different Program/source Season/selections/name alongside it and
        receive the ORIGINAL committer's full response verbatim; their own
        submitted request was never validated. Every ledger row now ALSO
        carries ``request_identity`` (migration 055): the RAW actor +
        body this commit was actually validated against, frozen in the
        SAME transaction as ``response_snapshot``. Before any early-replay
        return, the CURRENT actor and submitted request — canonicalized
        the same way, no store lookup — must match that stored identity
        exactly (``_copy_forward_request_identity_matches``); on a
        mismatch this method does NOT raise a bespoke error — it falls
        through to the UNCHANGED Team/League-lock + plan-resolution path
        below, which naturally refuses a foreign/stale fingerprint with
        the ordinary ``preview_mismatch``/``preview_required`` shape any
        other stale fingerprint gets, so a mismatch is never
        distinguishable from an everyday refusal and discloses nothing
        about the original row. See ``test_replay_by_another_actor_who_
        never_previewed_is_refused``, ``test_replay_with_changed_name_
        after_commit_is_refused``, and ``test_replay_with_different_
        program_and_source_season_is_refused`` (tests/test_new_season_
        copy_forward.py, all three classes) for the required regression
        matrix, and that file's ``NewSeasonCopyForwardHttpTest`` for the
        HTTP-only case (d): the original actor's account deactivated
        (loses ALL scope — the closest realizable form of "loses
        original-Program scope" for a role that is already global; see
        ``context_scope._GLOBAL_ROLES``) between the original commit and
        the replay attempt, which the pre-existing session layer already
        refuses independent of anything here — regression-tested so this
        round's change can never accidentally weaken it.

        RESPONSE_SNAPSHOT IS NOW THE COMPLETE PUBLIC DTO (#159 review
        round 4, owner P1 finding 2): each registration entry in
        ``response_snapshot`` also freezes its public ``season_id``/
        ``league_id`` — resolved via its LeagueSeason ONCE, right here,
        the same way ``ApiService._registration_dict`` resolves them —
        instead of leaving the facade to re-resolve them from the LIVE
        LeagueSeason on every read. Before this round that live
        re-resolution ran on a replay exactly as it does on an ordinary
        read, so once the target registration's LeagueSeason binding was
        deleted (a fully supported operation this same round 3 already
        made survivable for every OTHER field), a replay's season_id/
        league_id silently came back null instead of the values the
        original commit actually returned — ``response_snapshot`` claimed
        to be the immutable API response but was not. See
        ``_copy_forward_registration_identities`` and ``ApiService.
        commit_new_season_copy_forward``, and this file's
        ``test_replay_full_dto_equality_after_registration_and_league_
        season_deleted`` (all three service/SQL test classes, plus the
        HTTP twin) for the full-equality regression coverage.

        WHY A PRE-CHECK, NOT A CATCH-ON-CONFLICT (the design this replaced
        in self-review): this method runs under ``setup_guarded_create``
        (server.py's ``/api/v2/setup/seasons/copy-forward/commit`` route)
        INSIDE that caller's OWN already-open transaction — ``@_transactional``
        above then only JOINS it (``SqlStore.transaction()`` is reentrant),
        it does not become a second, independently-rollback-able unit. A
        losing attempt that instead caught the ledger INSERT's unique-
        violation and swallowed it into a normal return would let THAT
        outer transaction commit normally afterward — persisting the
        loser's own un-rolled-back Season, exactly the duplicate this fix
        exists to prevent (caught live: two sequential HTTP commits each
        returned the SAME season id in their JSON, yet a THIRD Season row
        with no client-visible id was left behind in the store). The
        pre-check sidesteps the whole hazard: it is a plain read with no
        side effect, so it is correct whether this transaction is the
        outermost one (a direct/test caller) or nested inside an ancestor's
        (the HTTP route) — nothing here ever needs to unwind a transaction
        it does not own. The residual INSERT-vs-INSERT race below (the
        narrow window between this pre-check and the write) still exists
        for defense in depth, but is vanishingly rare in practice: the SAME
        fingerprint implies the SAME selections (it hashes them), so two
        commits racing on one fingerprint already fully serialize on the
        Team/League locks taken below, before either ever reaches the
        pre-check — by the time a loser gets past those locks, the winner
        has already committed, and the pre-check finds it. When that
        residual race IS lost, this raises a retryable
        ConcurrencyConflictError rather than catching it: propagating
        unwinds to whichever transaction is genuinely outermost for this
        call, and for the HTTP route, ``setup_guarded_create``'s own
        ``except ConcurrencyConflictError`` retry (already used for the
        SAME class of row-moved-under-us race elsewhere in that method)
        re-runs this method from scratch, landing cleanly on the pre-check.
        See ``test_second_commit_reusing_the_same_fingerprint_is_idempotent``
        (tests/test_new_season_copy_forward.py) for the sequential guarantee,
        ``test_concurrent_commits_with_the_same_fingerprint_create_exactly_
        one_season`` for the genuine two-connection race, and
        ``test_sequential_commit_replay_over_http_is_idempotent`` /
        ``test_concurrent_commit_replay_over_http_is_idempotent`` for both
        proved again through the REAL nested ``setup_guarded_create``
        transaction this docstring describes. See (same file)
        ``test_replay_survives_source_team_unregistered_after_commit``,
        ``test_replay_survives_target_registration_deleted_after_commit``,
        and ``test_target_season_delete_is_blocked_then_replay_stays_
        stable`` / ``test_target_season_delete_blocked_by_copy_forward_
        commit_never_raw_fk`` for the three #159 review round 3 scenarios
        this reordering and the itemized ``delete_season`` dependency
        close, and ``test_delete_vs_replay_race_delete_stays_blocked_
        replay_stays_stable`` for the required real two-connection
        delete-vs-replay race.
        """
        # #159 review round 3 (owner P1, structural change 1): authenticate
        # + authorize already happened at the HTTP boundary (server.py) —
        # this is the service entrypoint — so the FIRST thing this method
        # does is validate the fingerprint is well-formed and, if so, check
        # the idempotency ledger BEFORE anything else: before either
        # Team/League lock below and before _resolve_copy_forward_plan
        # re-validates ANY mutable state (the source Season's current
        # registrations, the selections, the Program). ``None`` is refused
        # immediately, exactly as before this round. Anything else that is
        # not a non-blank string can never correctly be a store lookup key
        # (a non-string bound straight to a SQL parameter would crash the
        # driver instead of raising a structured error — see
        # SqlStore._query's ``?`` binding), so it is deliberately left
        # untouched for the UNCHANGED plan/fingerprint-mismatch path below
        # to refuse in the usual, structured ``preview_mismatch`` shape —
        # exactly what a malformed fingerprint already got before this
        # round, since the OLD code never used the raw caller-supplied
        # value as a lookup key either (only ``plan["fingerprint"]``, a
        # value this method always computes itself).
        if copy_forward_fingerprint is None:
            raise ValidationError(
                "Preview this copy-forward before committing it.",
                details={"reason": "preview_required"})
        if (isinstance(copy_forward_fingerprint, str)
                and not _blank(copy_forward_fingerprint)):
            early_replay = (
                self.store.get_season_copy_forward_commit_by_fingerprint(
                    copy_forward_fingerprint))
            # #159 review round 4 (owner P1 finding 1): a fingerprint match
            # ALONE is not enough to trust this row's response to THIS
            # caller — it is only a hash of the ORIGINAL committer's
            # resolved plan, and this early check runs BEFORE
            # _resolve_copy_forward_plan / _has_matching_copy_forward_
            # preview_audit ever look at the CURRENT actor or CURRENT
            # body at all. Without this gate, any caller who learned
            # (or guessed) an already-committed fingerprint — e.g. their
            # own earlier preview response, a browser history entry, a
            # server log — could submit an entirely different Program,
            # source Season, selections, or name alongside it and receive
            # the ORIGINAL committer's full response verbatim, never
            # having validated their own submitted request at all.
            # _copy_forward_owned_ledger_result (#159 review round 5,
            # owner P1 finding 2) is the ONE gate both this site and the
            # post-lock race-check backstop below now share — see its own
            # docstring. A mismatch here does NOT raise — it falls through
            # to the unchanged Team/League-lock + _resolve_copy_forward_
            # plan path below, which naturally refuses a foreign/stale
            # fingerprint with the SAME preview_mismatch/preview_required
            # shape any other stale fingerprint gets (this caller's OWN
            # freshly resolved plan will not hash to a fingerprint that
            # was computed over someone else's request) — so a mismatch
            # is never distinguishable from an ordinary stale-fingerprint
            # refusal, and discloses nothing about the original row.
            early_result = self._copy_forward_owned_ledger_result(
                early_replay, actor_id=actor_id, program_id=program_id,
                name=name, start_date=start_date, end_date=end_date,
                source_season_id=source_season_id, selections=selections)
            if early_result is not None:
                return early_result
        # Lock every distinct Team/League named in selections FIRST, sorted —
        # roll_forward_registrations_v2's own canonical lock order (#159), so
        # a batch can never deadlock Team-vs-Team or League-vs-League, and
        # the plan rebuilt below can't be shifted by a concurrent transfer or
        # delete landing between this lock and the write. No lock is taken
        # for the Season being minted — nothing can reference its id before
        # this transaction, which creates it, commits. Note: since the SAME
        # fingerprint implies the SAME selections (it hashes them), two
        # commits racing on one fingerprint already serialize here — the
        # ledger pre-check/insert below is the backstop that still holds
        # even if that ever stops being true (e.g. a future caller re-using
        # a fingerprint against a hand-built, differently-ordered
        # selections list).
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
        # Build the ENTIRE plan UNDER the locks (#158-review pattern): Program
        # existence, date parsing, the source Season and every selection are
        # (re)resolved here, so nothing the fingerprint check compares against
        # can be stale relative to committed state.
        plan = self._resolve_copy_forward_plan(
            program_id=program_id, name=name, start_date=start_date,
            end_date=end_date, source_season_id=source_season_id,
            selections=selections)
        # Preview binding (#158-review pattern): a commit MUST be preceded by
        # a preview of the SAME resolved plan BY THE SAME actor — the
        # fingerprint alone is not a capability, so both checks below must
        # hold, and they run BEFORE any write. (The "is None" refusal now
        # lives at the very top of this method, before the early ledger
        # check — copy_forward_fingerprint can never be None by this point.)
        if copy_forward_fingerprint != plan["fingerprint"]:
            raise ScheduleConflictError(
                "This copy-forward plan changed since it was previewed. "
                "Preview again before committing.",
                details={"reason": "preview_mismatch",
                         "expected": plan["fingerprint"]})
        if not self._has_matching_copy_forward_preview_audit(
                actor_id, plan["fingerprint"]):
            raise ValidationError(
                "No matching preview by this operator for this "
                "copy-forward plan. Preview it before committing.",
                details={"reason": "preview_required"})
        # SECOND idempotent-replay check (#159 review round 2 originally;
        # round 3 narrows what it is FOR). The EARLY check at the top of
        # this method already handles every ordinary replay of an
        # already-committed fingerprint without reaching here at all. This
        # one exists purely for the residual genuine-race window: a
        # fingerprint that was BRAND NEW when this call started (the early
        # check found nothing) but has since been committed by a concurrent
        # racer that reached the ledger INSERT below first, while THIS call
        # was still blocked acquiring the Team/League locks above. A plain
        # read, no exception, no write attempted — so it is correct and
        # side-effect-free whether this transaction is this call's own
        # outermost one or nested inside an ancestor's.
        already_committed = (
            self.store.get_season_copy_forward_commit_by_fingerprint(
                plan["fingerprint"]))
        # #159 review round 5 (owner P1 finding 2): this return used to be
        # unconditional -- ANY row with a matching fingerprint was handed
        # back, regardless of who committed it. The fingerprint hashes the
        # RESOLVED PLAN only, never the actor, so two different actors who
        # each independently, legitimately preview the IDENTICAL plan get
        # the SAME fingerprint and can each individually pass the
        # fingerprint-match + preview-audit gates just above using their
        # OWN valid preview. Before this round, whichever of them lost the
        # race to commit first landed here and silently received the
        # WINNER's Season -- see _copy_forward_owned_ledger_result's own
        # docstring for the full walkthrough and
        # test_second_actor_who_also_previewed_the_identical_plan_is_
        # refused_not_given_the_winners_season for the regression coverage.
        # Routing through the SAME shared helper the early check above uses
        # means this can never again silently skip the identity check the
        # way the old unconditional return did.
        race_result = self._copy_forward_owned_ledger_result(
            already_committed, actor_id=actor_id, program_id=program_id,
            name=name, start_date=start_date, end_date=end_date,
            source_season_id=source_season_id, selections=selections)
        if race_result is not None:
            return race_result
        if already_committed is not None:
            # A ledger row for this EXACT fingerprint already exists, but
            # its frozen request_identity does not belong to this actor/
            # request -- this is NOT the residual same-request race the
            # INSERT-conflict backstop a few lines below exists for (that
            # backstop's own reasoning -- "the SAME fingerprint implies the
            # SAME selections" -- only holds for the SAME actor/request;
            # the fingerprint never encodes WHO is asking). Falling through
            # to attempt the insert would just lose migration 053's UNIQUE
            # index and surface a misleading "retry" — retrying changes
            # nothing, since this fingerprint is now PERMANENTLY spoken for
            # by someone else's commit. Refused instead with the EXACT SAME
            # message/reason an ordinary missing-preview-audit refusal
            # produces a few lines above, so this is never distinguishable
            # from a caller who simply never previewed, and discloses
            # nothing about a foreign, already-committed fingerprint
            # existing at all.
            raise ValidationError(
                "No matching preview by this operator for this "
                "copy-forward plan. Preview it before committing.",
                details={"reason": "preview_required"})
        # create_season's own validation already ran above (inside
        # _resolve_copy_forward_plan's _resolve_season_creation call), so
        # build and insert the Season directly rather than re-validating.
        season = Season(id=self.store.next_id("season"),
                        program_id=program_id, name=plan["name"],
                        start_date=plan["start"], end_date=plan["end"])
        self.store.add_season(season)
        self._audit("season_created", "season", season.id, actor_id,
                    {"league_id": program_id})
        wanted = {row["team_id"]: (row["league_id"], None)
                 for row in plan["rows"]}
        applied = self._apply_registration_selections(
            to_season_id=season.id, from_season_id=source_season_id,
            wanted=wanted, actor_id=actor_id)
        self._audit(
            "new_season_copy_forward_committed", "season", season.id,
            actor_id, {
                "program_id": program_id,
                "source_season_id": source_season_id,
                "copy_forward_fingerprint": plan["fingerprint"],
                "rolled_forward": applied["rolled_forward"],
                "skipped": applied["skipped"]})
        result = {"season": season, "registrations": applied["registrations"],
                  "totals": {"rolled_forward": applied["rolled_forward"],
                            "skipped": applied["skipped"]}}
        # Built ONCE, here, and reused for BOTH the ledger row's
        # ``response_snapshot`` below AND this fresh commit's own
        # ``registration_identities`` (#159 review round 4, owner P1
        # finding 2) — so a fresh commit's response and every future
        # replay of it read season_id/league_id from the exact same
        # computation, by construction, rather than two separate
        # resolutions that could in principle drift.
        snapshot = self._copy_forward_response_snapshot(
            season=result["season"], registrations=result["registrations"],
            totals=result["totals"])
        result["registration_identities"] = (
            self._copy_forward_registration_identities(snapshot))
        # Atomically consume the fingerprint (#159 review round 2): the
        # LAST statement of this transaction, so migration 053's UNIQUE
        # index is the final word on whether THIS attempt or a racing one
        # gets to keep the Season just created above. This is the RESIDUAL
        # race the pre-check above does not close (the narrow window
        # between that read and this write) — vanishingly rare per this
        # method's own docstring, but still enforced rather than assumed.
        # A race-losing INSERT is translated to IntegrityConflictError and
        # re-raised here as a RETRYABLE ConcurrencyConflictError — NEVER
        # caught in this method — so it always unwinds to whichever
        # transaction is genuinely outermost for this call: this method's
        # own, for a direct caller (which surfaces the retryable conflict,
        # matching this codebase's house style for an exhausted race, e.g.
        # commit_ice_availability's own final `raise` after its retry
        # budget), or setup_guarded_create's wrapping transaction for the
        # HTTP route, whose existing `except ConcurrencyConflictError`
        # retry (already used for the same class of row-moved-under-us
        # race elsewhere in that method) re-runs this method from scratch
        # and lands cleanly on the pre-check above.
        #
        # ``response_snapshot`` (#159 review round 3, structural change 1)
        # is built from ``result`` — the season/registrations THIS
        # transaction just created — right here, still inside the same
        # transaction, so it is a byte-accurate record of what this commit
        # actually produced. Every future replay of this fingerprint
        # deserializes this blob and returns it verbatim; it is never
        # rebuilt by re-querying ``seasons``/``season_team_registrations``
        # again, so nothing that later mutates either table (unregistering
        # the source Team, deleting the target registration — deleting the
        # target Season itself is refused outright, see delete_season's own
        # itemized ``copy_forward_commit`` dependency) can change it.
        try:
            self.store.add_season_copy_forward_commit(
                SeasonCopyForwardCommit(
                    id=self.store.next_id("cfcommit"),
                    copy_forward_fingerprint=plan["fingerprint"],
                    season_id=season.id, actor_id=actor_id,
                    registration_ids=[r.id
                                     for r in applied["registrations"]],
                    rolled_forward=applied["rolled_forward"],
                    skipped=applied["skipped"], committed_at=self.clock(),
                    response_snapshot=snapshot,
                    # #159 review round 4 (owner P1 finding 1): the RAW
                    # request THIS transaction actually validated (via the
                    # plan re-resolution and preview-audit checks above),
                    # frozen alongside the response it produced — the
                    # identity a FUTURE replay of this exact fingerprint
                    # must match before ever reusing this row. See
                    # _copy_forward_request_identity_matches.
                    #
                    # Canonicalized to an immutable STRING here, at the
                    # ledger-write boundary itself (#159 review round 5,
                    # owner P1 finding 1) -- NOT the raw dict
                    # _copy_forward_request_identity returns, which still
                    # wraps whatever mutable ``selections`` object the
                    # CALLER passed in and may keep mutating after this
                    # call returns. This is the ONE place that matters:
                    # once this string is built, nothing that happens to
                    # the caller's own selections list afterward can ever
                    # reach it again, regardless of how any store chooses
                    # to hold the row (by reference or by copy).
                    request_identity=self._copy_forward_canonical_json(
                        self._copy_forward_request_identity(
                            actor_id=actor_id, program_id=program_id,
                            name=name, start_date=start_date,
                            end_date=end_date,
                            source_season_id=source_season_id,
                            selections=selections))))
        except IntegrityConflictError as exc:
            if exc.details.get("reason") != "copy_forward_already_committed":
                raise
            raise ConcurrencyConflictError(
                "This copy-forward was just committed by another request; "
                "please retry.",
                details={"reason": "copy_forward_fingerprint_conflict",
                         "retryable": True}) from exc
        return result

    def _copy_forward_result_from_ledger_row(self, row) -> dict:
        """Rebuild the exact success shape ``commit_new_season_copy_
        forward`` returns from an ALREADY-committed ledger row, by
        deserializing its immutable ``response_snapshot`` (#159 review
        round 3, structural change 1) — NEVER by touching the ``seasons``
        or ``season_team_registrations`` tables. The OLD implementation
        (#159 review round 2) re-fetched ``row.season_id`` and each id in
        ``row.registration_ids`` from those live tables, which is exactly
        what let a later, unrelated mutation of either — unregistering the
        source Team (which the plan re-resolution this replay path used to
        run BEFORE reaching here would then reject), deleting the target
        registration, or deleting the target Season — silently change or
        break what a replay of an already-successful commit returned. This
        version returns precisely, byte-for-byte, what THAT original
        commit produced, regardless of anything that has happened to
        either table since — reconstructing real ``Season``/
        ``SeasonTeamRegistration`` instances (not plain dicts) so the
        facade's existing ``_serialize``/``_registration_dict`` calls on
        the result work completely unchanged, exactly as they do for a
        freshly-created commit.

        ``registration_identities`` (#159 review round 4, owner P1
        finding 2) carries each registration's FROZEN ``season_id``/
        ``league_id`` — see ``_copy_forward_registration_identities`` —
        so ``ApiService.commit_new_season_copy_forward`` never needs to
        re-resolve them from the live LeagueSeason for a replay either."""
        snap = row.response_snapshot or {}
        return {
            "season": self._copy_forward_season_from_snapshot(snap["season"]),
            "registrations": [
                self._copy_forward_registration_from_snapshot(r)
                for r in snap.get("registrations", [])],
            "totals": snap.get("totals") or {
                "rolled_forward": row.rolled_forward, "skipped": row.skipped},
            "registration_identities": (
                self._copy_forward_registration_identities(snap)),
        }

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
        # PR #423 (design §8.6): the epoch fence's GLOBAL exclusive hold,
        # first (row 15 of the design's writer table) — a Player/Team
        # reassignment can change what a Player's or Guardian's own scoped
        # reads resolve to (context_scope walks Player.team_id), and the
        # affected user is found only by a lookup this method doesn't even
        # need to perform itself, so it takes the GLOBAL key rather than a
        # per-user one (§4.2's classification rule). `@_transactional`
        # already opened this method's transaction; when called from
        # `_guarded_attempt` (the v2 reassignment dispatch) this simply joins
        # that already-open, already-SERIALIZABLE transaction (reentrant, see
        # `SqlStore.transaction`), so "same transaction as the write" holds
        # either way.
        self.store.epoch_fence_acquire_exclusive(EPOCH_FENCE_GLOBAL_KEY)
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
        # #273 review round 2 finding 2: the same same-team duplicate check
        # create/update already run, now BEFORE the destination-team move —
        # this method used to check ONLY jersey availability, so a player
        # carrying a registration number could be moved onto a team that
        # already had another player with that same number, landing two
        # rows with one governing-body id on one team. Unlike the jersey
        # check this is NOT conditioned on ``player.is_active``: the
        # existing same-team check includes inactive players (deactivating a
        # row must not free the identity for an accidental duplicate), so a
        # move must honor the same rule regardless of the mover's active
        # state.
        self._assert_registration_number_available(
            team_id, player.registration_number, exclude_player_id=player.id)
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

    # -- scheduling policy (#277 Slice B) --------------------------------
    #
    # Operational turnover/curfew knobs for game placement, configurable per
    # Program / Season / Rink and resolved field by field with Rink overriding
    # Season overriding Program. Enforcement lives in _assert_slot_meets_policy
    # (layered onto _assert_slot_free, THE shared placement gate), so
    # create_game, move_game, and both draft-commit implementations inherit it
    # uniformly — the same no-draft-exception contract as the rest of the gate.

    _POLICY_MINUTE_FIELDS = ("warmup_minutes", "resurfacing_minutes",
                             "min_playable_minutes")
    _POLICY_FIELDS = _POLICY_MINUTE_FIELDS + ("curfew_local",)

    def _require_policy_scope(self, scope_type, scope_id, *, lock=False):
        """Validate ``scope_type`` and that its target entity exists; return
        the parsed :class:`PolicyScopeType`. With ``lock=True`` the scope row
        is read FOR UPDATE — the write path's serialization point, so two
        concurrent ``set_scheduling_policy`` calls for one scope can't race
        past each other into the unique index (single-row lock: cannot
        deadlock the multi-lock placement paths)."""
        try:
            st = PolicyScopeType(scope_type)
        except ValueError:
            raise ValidationError(
                "Unknown scheduling-policy scope.",
                {"reason": "unknown_policy_scope", "scope_type": scope_type})
        getter = {
            PolicyScopeType.PROGRAM: (self.store.get_program_for_update
                                      if lock else self.store.get_program),
            PolicyScopeType.SEASON: (self.store.get_season_for_update
                                     if lock else self.store.get_season),
            PolicyScopeType.RINK: (self.store.get_rink_for_update
                                   if lock else self.store.get_rink),
        }[st]
        if getter(scope_id) is None:
            raise NotFoundError(
                f"{st.value.capitalize()} {scope_id} not found.",
                details={"reason": "policy_scope_missing",
                         "scope_type": st.value, "scope_id": scope_id})
        return st

    @_transactional
    def set_scheduling_policy(self, scope_type, scope_id,
                              warmup_minutes=None, resurfacing_minutes=None,
                              min_playable_minutes=None, curfew_local=None,
                              actor_id: Optional[str] = None):
        """Upsert the scheduling policy for one scope (#277 Slice B).

        The passed values REPLACE the row wholesale (this is a settings form,
        not a patch): a ``None`` field means "inherit from the next scope up",
        and all-``None`` deletes the row entirely (audited as a clear). Values
        never rewrite any stored IceSlot/Game — enforcement is read-time only,
        so setting a policy cannot time-shift existing data (#277).

        Returns the stored row, or ``None`` after a clear.
        """
        st = self._require_policy_scope(scope_type, scope_id, lock=True)
        for field, value in (("warmup_minutes", warmup_minutes),
                             ("resurfacing_minutes", resurfacing_minutes),
                             ("min_playable_minutes", min_playable_minutes)):
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool) \
                    or value < 0:
                raise ValidationError(
                    f"{field} must be an integer >= 0.",
                    {"reason": f"invalid_{field}", "field": field})
        if curfew_local is not None:
            hour, minute = parse_hhmm(curfew_local, "curfew_local")
            curfew_local = f"{hour:02d}:{minute:02d}"  # store normalized

        existing = self.store.find_scheduling_policy(st, scope_id)
        values = {"warmup_minutes": warmup_minutes,
                  "resurfacing_minutes": resurfacing_minutes,
                  "min_playable_minutes": min_playable_minutes,
                  "curfew_local": curfew_local}
        if all(v is None for v in values.values()):
            if existing is not None:
                self.store.delete_scheduling_policy(existing.id)
                self._audit("scheduling_policy_cleared", st.value, scope_id,
                            actor_id, {"policy_id": existing.id})
            return None
        if existing is None:
            policy = self.store.add_scheduling_policy(SchedulingPolicy(
                id=self.store.next_id("schedpolicy"),
                scope_type=st, scope_id=scope_id, **values))
        else:
            for field, value in values.items():
                setattr(existing, field, value)
            policy = self.store.save_scheduling_policy(existing)
        self._audit("scheduling_policy_set", st.value, scope_id, actor_id,
                    {"policy_id": policy.id, **values})
        return policy

    def get_scheduling_policy(self, scope_type, scope_id):
        """The raw stored policy row for one scope, or ``None`` (read-only;
        validates the scope exists so a typo'd id is a 404, not a null)."""
        st = self._require_policy_scope(scope_type, scope_id)
        return self.store.find_scheduling_policy(st, scope_id)

    def _cascade_scheduling_policy(self, scope_type, scope_id, actor_id):
        """Delete (and audit) a scope's policy row when the scope entity
        itself is deleted (#277 Slice B review): migration 046 carries no FK
        to its polymorphic scope, and every API read/write validates the
        scope exists first — so a row surviving its scope would be permanent
        dead data no operator could ever view or clear."""
        row = self.store.find_scheduling_policy(scope_type, scope_id)
        if row is not None:
            self.store.delete_scheduling_policy(row.id)
            self._audit("scheduling_policy_cleared", scope_type.value,
                        scope_id, actor_id,
                        {"policy_id": row.id,
                         "cascade": f"{scope_type.value}_deleted"})

    def _effective_policy(self, rink_id, season_id):
        """Resolve the EFFECTIVE scheduling policy for placing a game on
        ``rink_id`` in ``season_id``: field by field, first non-``None`` of
        Rink -> Season -> Program wins. Returns ``(values, sources)`` where
        ``values`` maps every policy field to its resolved value (still
        ``None`` when unset at every scope — the no-op default) and
        ``sources`` maps each SET field to the scope_type string it came
        from. Plain reads only (no locks): runs inside the caller's
        transaction after its own Team/Rink/Season locks, so a concurrent
        policy edit either committed before those locks or waits — the
        resolved values are stable for the caller's write."""
        season = self.store.get_season(season_id) if season_id else None
        rows = []
        if rink_id:
            rows.append(self.store.find_scheduling_policy(
                PolicyScopeType.RINK, rink_id))
        if season_id:
            rows.append(self.store.find_scheduling_policy(
                PolicyScopeType.SEASON, season_id))
        if season is not None and season.program_id:
            rows.append(self.store.find_scheduling_policy(
                PolicyScopeType.PROGRAM, season.program_id))
        values, sources = {}, {}
        for field in self._POLICY_FIELDS:
            values[field] = None
            for row in rows:
                v = getattr(row, field, None) if row is not None else None
                if v is not None:
                    values[field] = v
                    sources[field] = getattr(row.scope_type, "value",
                                             row.scope_type)
                    break
        return values, sources

    def _curfew_timezone(self, rink_id, season_id):
        """The wall-clock anchor for curfew_local: the slot's venue timezone
        (a curfew is a building rule), falling back to the Season's Program
        timezone, then UTC. Total — unknown/legacy tz names fall through."""
        rink = self.store.get_rink(rink_id) if rink_id else None
        venue = (self.store.get_venue(rink.venue_id)
                 if rink is not None and rink.venue_id else None)
        tz = resolve_timezone(getattr(venue, "timezone", None))
        if tz is None:
            season = self.store.get_season(season_id) if season_id else None
            program = (self.store.get_program(season.program_id)
                       if season is not None and season.program_id else None)
            tz = resolve_timezone(getattr(program, "timezone", None))
        return tz or timezone.utc

    def _assert_slot_meets_policy(self, slot, season_id, *,
                                  exclude_game_id=None):
        """#277 Slice B enforcement, layered onto :meth:`_assert_slot_free`
        exactly as its docstring reserved — raises the violation
        :meth:`_slot_policy_violation` reports as a stable structured
        ``ScheduleConflictError``."""
        violation = self._slot_policy_violation(
            slot, season_id, exclude_game_id=exclude_game_id)
        if violation is not None:
            message, details = violation
            raise ScheduleConflictError(message, details=details)

    def _slot_policy_violation(self, slot, season_id, *,
                               exclude_game_id=None, extra_rink_spans=(),
                               rink_games=None):
        """THE #277 Slice B policy evaluation — one implementation shared by
        the placement gate (:meth:`_assert_slot_meets_policy`, which raises)
        and the draft scheduler's advisory pass (which reports the same codes
        in proposal ``reason_codes`` so a draft never offers a row the commit
        gate would reject). Returns ``(message, details)`` for the first
        violation, or ``None``. ``extra_rink_spans`` is the scheduler's
        tentative same-run picks — ``(slot_id, start, end)`` tuples on this
        slot's rink not yet persisted as Games. ``rink_games`` optionally
        supplies a pre-resolved ``(game, slot)`` inventory (the scheduler
        advisory snapshots it ONCE per run instead of re-scanning the game
        table per candidate); the commit paths omit it and scan fresh under
        their locks. Four checks — ``slot_overlap_conflict`` is
        UNCONDITIONAL (it binds even when no season, hence no policy scope,
        resolves — season-less legacy games included), the other three come
        from the effective Rink>Season>Program policy:

        * ``insufficient_playable_time`` — the slot's playable span
          ``[start_time, end_time]`` is shorter than ``min_playable_minutes``
          (imported contracted slivers are preserved as-is at ingest; this is
          where they are refused a GAME, per #277).
        * ``slot_overlap_conflict`` — the candidate slot physically overlaps
          another active game's slot on the SAME rink. Refused REGARDLESS of
          any configured turnover (#318 review — a zero/absent policy
          changes nothing): the import path deliberately persists
          overlapping contracted rows as warnings, so this gate is where
          physical exclusivity is enforced. Exact adjacency (end == start)
          is NOT overlap and stays compliant even at a zero requirement.
        * ``turnover_buffer_conflict`` — another active game's slot on the
          SAME rink sits closer than the DIRECTIONAL requirement: the
          EARLIER game's resurfacing plus the LATER game's warm-up, each
          resolved from that game's OWN effective policy (#318 review — two
          seasons sharing a rink can carry different policies; the
          candidate's irrelevant-side buffer never blocks). Boundary rule
          (product decision, half-open like ``intervals_overlap``): a gap
          EXACTLY equal to the requirement is compliant. Game-vs-game only —
          buffers against non-game slots (maintenance/public skate) are
          #189's event model, not this gate.
        * ``curfew_violation`` — the slot's playable end lands after the
          curfew instant in the venue's timezone (Program fallback). The
          full operating-day anchor rule — defined relative to the curfew
          itself, never a hard-coded clock boundary — lives in
          :func:`ice_availability.curfew_instant` (#318 review): an
          afternoon/evening curfew is the start-date deadline; a
          small-hours curfew binds a slot starting AT/BEFORE its wall clock
          to THAT date (a 00:30-02:00 slot violates tonight's 01:00 close)
          and a slot starting AFTER it (an 08:00 practice, a 22:00 evening
          fixture) to the FOLLOWING date. Ending exactly AT curfew is
          compliant; spring-forward-skipped curfew wall times resolve
          deterministically; fall-back ambiguity resolves fold=0 and the
          comparison is instant-based so acceptance is monotonic in real
          time.

        A ``season_id`` of ``None`` (no competition scope to resolve a policy
        from — a season-less legacy game) skips the three POLICY checks; the
        unconditional overlap rule still binds.
        All-``None`` policies short-circuit to today's behavior. Read-only;
        the COMMIT paths run it inside the caller's transaction under its
        Rink lock (making the same-rink scan atomic against concurrent
        placements) AND under the FOR UPDATE locks of every policy scope row
        it reads — candidate and neighbor Seasons/Programs alike, planned by
        ``_policy_scope_lock_plan`` — so a racing ``set_scheduling_policy``
        on ANY scope this gate resolves strictly serializes with the
        placement. The scheduler ADVISORY calls it lock-free; the gate stays
        authoritative.
        """
        # The overlap rule is UNCONDITIONAL (#318 review round 2): it binds
        # even for season-less legacy games, where no policy scope resolves
        # at all — so it must not hide behind the season gate that the three
        # policy checks legitimately live behind.
        seasoned = season_id is not None
        policy = (self._effective_policy(slot.rink_id, season_id)[0]
                  if seasoned else None)
        if seasoned:
            min_playable = policy["min_playable_minutes"] or 0
            slot_minutes = int(
                (slot.end_time - slot.start_time).total_seconds() // 60)
            if slot_minutes < min_playable:
                return (
                    f"Ice slot {slot.id} is only {slot_minutes} playable "
                    f"minutes; this competition requires at least "
                    f"{min_playable}.",
                    {"reason": "insufficient_playable_time",
                     "slot_minutes": slot_minutes,
                     "required_minutes": min_playable})
        # -- physical overlap (unconditional) + DIRECTIONAL turnover -------
        # Overlap first, policy-free: two games can never share rink time no
        # matter what turnover is configured (the import path deliberately
        # persists overlapping contracted rows, making this gate the
        # physical-exclusivity enforcement point). The buffer's required gap
        # between two adjacent games is the EARLIER game's resurfacing plus
        # the LATER game's warm-up, each resolved from that game's OWN
        # effective policy (two seasons sharing a rink can differ); the
        # candidate's irrelevant-side buffer never blocks.
        _policy_cache = {(slot.rink_id, season_id): policy} if seasoned else {}

        def _policy_for(rink_id, sid):
            key = (rink_id, sid)
            if key not in _policy_cache:
                _policy_cache[key] = self._effective_policy(rink_id, sid)[0]
            return _policy_cache[key]

        def _buffer_conflict(other_start, other_end, other_policy):
            """(required, gap) when the DIRECTIONAL requirement is violated,
            else None. Half-open: a gap EXACTLY equal to the requirement is
            compliant — exact adjacency stays legal even at a zero
            requirement. Callers test physical overlap FIRST, so the spans
            here never overlap and exactly one ordering holds."""
            if other_start >= slot.end_time:
                # Candidate is earlier: its resurfacing + the other's warmup.
                required = ((policy["resurfacing_minutes"] or 0)
                            + (other_policy["warmup_minutes"] or 0))
                gap = other_start - slot.end_time
            else:
                # Candidate is later: the other's resurfacing + its warmup.
                required = ((other_policy["resurfacing_minutes"] or 0)
                            + (policy["warmup_minutes"] or 0))
                gap = slot.start_time - other_end
            if required > 0 and gap < timedelta(minutes=required):
                return required, gap
            return None

        if rink_games is None:
            rink_games = ((g, self.store.get_ice_slot(g.ice_slot_id))
                          for g in self.store.all_games()
                          if g.ice_slot_id is not None)
        for ex, ex_slot in rink_games:
            if (ex.id == exclude_game_id or ex.cancelled
                    or ex.ice_slot_id is None
                    or ex.ice_slot_id == slot.id):
                continue
            if ex_slot is None or ex_slot.rink_id != slot.rink_id:
                continue
            if intervals_overlap(slot.start_time, slot.end_time,
                                 ex_slot.start_time, ex_slot.end_time):
                return (
                    f"Ice slot {slot.id} overlaps ice slot {ex_slot.id} "
                    f"hosting game {ex.id} on the same rink.",
                    {"reason": "slot_overlap_conflict",
                     "conflict_game_id": ex.id,
                     "conflict_slot_id": ex_slot.id})
            if not seasoned:
                continue
            hit = _buffer_conflict(
                ex_slot.start_time, ex_slot.end_time,
                _policy_for(ex_slot.rink_id, ex.season_id))
            if hit is None:
                continue
            required, gap = hit
            return (
                f"Ice slot {slot.id} is too close to game {ex.id} on "
                f"the same rink for the required "
                f"{required}-minute turnover.",
                {"reason": "turnover_buffer_conflict",
                 "conflict_game_id": ex.id,
                 "conflict_slot_id": ex_slot.id,
                 "required_gap_minutes": required,
                 "gap_minutes": int(gap.total_seconds() // 60)})
        for span_id, span_start, span_end in extra_rink_spans:
            if span_id == slot.id:
                continue
            if intervals_overlap(slot.start_time, slot.end_time,
                                 span_start, span_end):
                return (
                    f"Ice slot {slot.id} overlaps another proposed game's "
                    f"slot on the same rink.",
                    {"reason": "slot_overlap_conflict",
                     "conflict_game_id": None,
                     "conflict_slot_id": span_id})
            if not seasoned:
                continue
            # A tentative same-run pick shares the candidate's own season,
            # so both directions resolve from the same policy.
            hit = _buffer_conflict(span_start, span_end, policy)
            if hit is None:
                continue
            required, gap = hit
            return (
                f"Ice slot {slot.id} is too close to another proposed "
                f"game on the same rink for the required "
                f"{required}-minute turnover.",
                {"reason": "turnover_buffer_conflict",
                 "conflict_game_id": None,
                 "conflict_slot_id": span_id,
                 "required_gap_minutes": required,
                 "gap_minutes": int(gap.total_seconds() // 60)})
        if not seasoned:
            return None
        curfew = policy["curfew_local"]
        if curfew:
            hour, minute = parse_hhmm(curfew, "curfew_local")
            tz = self._curfew_timezone(slot.rink_id, season_id)
            start_local = slot.start_time.astimezone(tz)
            # The anchor rule lives in ice_availability.curfew_instant — ONE
            # implementation shared with the scheduler advisory AND the
            # import advisories (ingest warnings and placement enforcement
            # can never disagree), defining the operating day relative to
            # the curfew itself (#318 review) and resolving spring-forward-
            # skipped wall times deterministically.
            curfew_at = curfew_instant(start_local, hour, minute)
            # Compare INSTANTS, not wall clocks: Python compares two aware
            # datetimes sharing one tzinfo by their naive values (fold
            # ignored), which on a fall-back night would accept a later real
            # end with a smaller repeated wall clock and reject an earlier
            # one — non-monotonic in real time. Converting the fold=0 curfew
            # to UTC makes the documented earlier-occurrence choice operative
            # and the check monotonic: everything after that instant
            # violates, everything at/before it passes.
            end_local = slot.end_time.astimezone(tz)
            if slot.end_time > curfew_at.astimezone(timezone.utc):
                return (
                    f"Ice slot {slot.id} ends at "
                    f"{end_local.strftime('%H:%M')} local, past the "
                    f"{curfew} curfew.",
                    {"reason": "curfew_violation",
                     "curfew_local": curfew,
                     "slot_end_local": end_local.strftime("%H:%M")})
        return None

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
        # #277 Slice B — advisory: warn per rink only when this template
        # ACTUALLY generates a consecutive pair whose real gap is below the
        # rink's effective directional requirement (the earlier row's
        # resurfacing + the later row's warm-up — one policy, the builder's
        # own Season, governs both generated rows), naming the worst
        # offending pair and its gap so the operator can fix the template
        # before committing ice the placement gate will half-refuse.
        # Computed from the REAL sorted generated intervals per rink/date —
        # two far-apart windows on one day stay silent, and a gap exactly
        # equal to the requirement is compliant (half-open, like the gate).
        # Lives inside the reviewed payload, so it is fingerprint-bound like
        # every other reviewed field: setting or clearing such a policy
        # after preview moves the token and forces a re-preview.
        policy_notes = []
        _by_rink_day = {}
        for c in classified:
            _by_rink_day.setdefault((c["rink_id"], c["date"]), []).append(c)
        for rink, _venue in accessible:
            values, _src = self._effective_policy(rink.id, season.id)
            required = ((values["resurfacing_minutes"] or 0)
                        + (values["warmup_minutes"] or 0))
            if required <= 0:
                continue
            worst = None
            for (rid, _d), rows in _by_rink_day.items():
                if rid != rink.id or len(rows) < 2:
                    continue
                rows = sorted(rows, key=lambda r: r["start"])
                for prev, nxt in zip(rows, rows[1:]):
                    gap = int((nxt["start"] - prev["end"]).total_seconds()
                              // 60)
                    if 0 <= gap < required and (worst is None
                                                or gap < worst["gap"]):
                        worst = {"gap": gap, "prev": prev, "nxt": nxt}
            if worst is not None:
                policy_notes.append({
                    "rink_id": rink.id, "rink_name": rink.name,
                    "date": worst["prev"]["date"],
                    "pair_end_local": worst["prev"]["end_local"],
                    "pair_next_start_local": worst["nxt"]["start_local"],
                    "gap_minutes": worst["gap"],
                    "required_gap_minutes": required,
                    "template_turnover_minutes": turnover_minutes,
                    "policy_buffer_minutes": required})
        base = {
            "season": season, "tz": tz, "d_start": d_start, "d_end": d_end,
            "weekday_set": weekday_set,
            "windows_meta": windows_meta,
            "playable_minutes": playable_minutes,
            "turnover_minutes": turnover_minutes,
            "plan": plan, "accessible": accessible,
            "access_missing": access_missing,
            "classified": classified,
            "policy_notes": policy_notes,
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
            "policy_notes": r["policy_notes"],
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
        # No valid registration — surface the precise reason. #331 review
        # round 20: check for a structured conflict FIRST — both the
        # exact-key shape round 19 established and the season-wide shape
        # round 20 adds — so a genuinely ambiguous state gets a
        # deterministic conflict reason, instead of the raw first-match
        # pick below silently depending on which of several colliding rows
        # happens to sort first (insertion order must never change the
        # answer).
        if season is not None and resolved is not None and resolved.league_id:
            ls = self.store.league_season_for(resolved.league_id, season.id)
            if ls is not None:
                _exact_reg, exact_conflicts = exact_registration_or_conflict(
                    self.store, ls.id, team_id)
                if exact_conflicts:
                    raise DivisionMismatchError(
                        f"{label} has more than one registration at this "
                        "exact league/season; resolve the conflict before "
                        "scheduling.",
                        {"reason": "team_registration_conflict",
                         "team_id": team_id, "league_season_id": ls.id,
                         "affected_registration_ids": exact_conflicts})
        if season is not None:
            _season_reg, season_conflicts = team_season_participation(
                self.store, season.id, team_id)
            if season_conflicts:
                raise DivisionMismatchError(
                    f"{label} has more than one active registration this "
                    "season; resolve the conflict before scheduling.",
                    {"reason": "team_registration_conflict",
                     "team_id": team_id, "season_id": season_id,
                     "affected_registration_ids": season_conflicts})
        # #283: a team's registration in a Season is found across the
        # Season's LeagueSeasons.
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

    def _require_team_in_league_season(self, season_id, league_season, team_id):
        """Require ``team_id`` to have exactly one active, unambiguous
        registration in the EXACT ``league_season`` given -- never the
        Team's own permanent-League LeagueSeason ``team_registration_valid``/
        ``_require_team_registered`` resolve instead (#331 review round 22).
        A Team's permanent League can diverge from a Game's or draft's own
        recorded LeagueSeason -- a stale reference a transfer left behind,
        or (this round's reproduction) a corrupted/reassigned
        ``league_season_id`` on the Game itself -- and comparing only
        League ids afterward cannot detect that the registration found
        isn't actually AT this exact LeagueSeason, merely that it happens
        to share the same League.

        Shares ``_require_batch_team_participation``'s own two-layer
        resolution: ``exact_registration_or_conflict`` (unconditional on
        active state) and ``team_season_participation`` (season-wide,
        active-only) must both hold. The caller resolves/validates
        ``league_season`` itself first (existence, and that it belongs to
        ``season_id``) -- this method trusts it as given.

        Raises ``DivisionMismatchError`` with a structured reason; returns
        the registration on success."""
        team = self.store.get_team(team_id)
        label = team.name if team is not None else team_id
        exact_reg, exact_conflicts = exact_registration_or_conflict(
            self.store, league_season.id, team_id)
        if exact_conflicts:
            raise DivisionMismatchError(
                f"{label} has more than one registration at this exact "
                "league/season; resolve the conflict before scheduling.",
                {"reason": "team_registration_conflict", "team_id": team_id,
                 "league_season_id": league_season.id,
                 "affected_registration_ids": exact_conflicts})
        season_reg, season_conflicts = team_season_participation(
            self.store, season_id, team_id)
        if season_conflicts:
            raise DivisionMismatchError(
                f"{label} has more than one active registration this "
                "season; resolve the conflict before scheduling.",
                {"reason": "team_registration_conflict", "team_id": team_id,
                 "season_id": season_id,
                 "affected_registration_ids": season_conflicts})
        registration = (
            exact_reg if exact_reg is not None and exact_reg.active
            and season_reg is not None and season_reg.id == exact_reg.id
            else None)
        if registration is None:
            if season_reg is None:
                raise DivisionMismatchError(
                    f"{label} is not registered in this season.",
                    {"reason": "team_not_registered", "team_id": team_id,
                     "season_id": season_id})
            raise DivisionMismatchError(
                f"{label} is not registered in this league-season.",
                {"reason": "team_not_in_league_season", "team_id": team_id,
                 "season_id": season_id, "league_season_id": league_season.id,
                 "registered_league_season_id": season_reg.league_season_id})
        if (team is None or not team.league_id
                or team.league_id != league_season.league_id):
            raise DivisionMismatchError(
                f"{label}'s registration does not match its permanent "
                "League.",
                {"reason": "registration_cross_league", "team_id": team_id,
                 "league_season_id": league_season.id,
                 "league_id": league_season.league_id,
                 "team_league_id": (
                     team.league_id if team is not None else None)})
        return registration

    def _require_batch_team_participation(self, season_id, league_season_id,
                                          rows):
        """Require every proposed row to belong to one exact LeagueSeason.

        ``draft_season_schedule`` only pairs currently-registered teams, but
        that proposal is generated BEFORE the write transaction opens. Calling
        this check right after acquiring the batch's Team/Rink/Season locks
        (never before) closes the gap: a concurrent ``unregister_team_from_
        season`` takes this same Season lock, and a team-to-league transfer
        can move a registration to another LeagueSeason in the same Season.
        Season-only validation would accept that moved row for a division-less
        draft and persist a Game in the old League. Resolve the expected
        LeagueSeason under the held locks, require both teams' active rows to
        reference it exactly, require any Division to belong to it, and require
        each Team's permanent League to match it. This is the same fail-closed
        competition identity later enforced by publish/move.

        #331 review round 20: "reference it exactly" is enforced two ways —
        ``exact_registration_or_conflict`` rejects a Team with 2+ rows
        colliding at this exact key (regardless of active state), and
        ``team_season_participation`` additionally rejects a Team whose
        one unambiguous row here ISN'T also its only active registration
        this Season (a stray active row under a different League). Both
        must hold for a row to be trusted.
        """
        league_season = (
            self.store.get_league_season(league_season_id)
            if league_season_id else None)
        if league_season is None:
            raise ValidationError(
                "The draft's league-season no longer exists.",
                {"reason": "draft_league_season_missing",
                 "season_id": season_id,
                 "league_season_id": league_season_id})
        if league_season.season_id != season_id:
            raise ValidationError(
                "The draft's league-season belongs to a different season.",
                {"reason": "draft_league_season_mismatch",
                 "season_id": season_id,
                 "league_season_id": league_season.id,
                 "league_season_season_id": league_season.season_id})

        for row in rows:
            division_id = row.get("division_id")
            if division_id is not None:
                division = self.store.get_division(division_id)
                if (division is None
                        or division.league_season_id != league_season.id):
                    raise DivisionMismatchError(
                        "The draft Division does not belong to its "
                        "league-season.",
                        {"reason": "division_league_season_mismatch",
                         "division_id": division_id,
                         "league_season_id": league_season.id,
                         "division_league_season_id": (
                             division.league_season_id
                             if division is not None else None)})

            for team_id in (row["home_team_id"], row["away_team_id"]):
                # #331 review round 22: shared with _revalidate_game_
                # participation's own divisionless branch via
                # _require_team_in_league_season -- see that method's
                # docstring for the two-layer (exact-key + season-wide)
                # contract this delegates to.
                registration = self._require_team_in_league_season(
                    season_id, league_season, team_id)
                if (division_id is not None
                        and registration.division_id != division_id):
                    team = self.store.get_team(team_id)
                    label = team.name if team is not None else team_id
                    raise DivisionMismatchError(
                        f"{label} is not registered in this division.",
                        {"reason": "team_wrong_division",
                         "team_id": team_id, "season_id": season_id,
                         "division_id": division_id,
                         "registered_division_id":
                             registration.division_id})

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
        ``False`` for a HISTORICAL Season's standings so a validly transferred
        Team is still counted (#283 rule 10 + #159). Callers decide historicity
        with :meth:`season_is_historical` — never by re-testing ``end_date``,
        which silently misses an ARCHIVED-but-undated Season."""
        return _registered_team_ids(self.store, division_id,
                                    enforce_team_league=enforce_team_league)

    # -- manual game creation ---------------------------------------------
    def _assert_slot_free(self, ice_slot_id, *, season_id=None,
                          exclude_game_id=None):
        """Physical-placement half of the game-placement check (#277).

        Checks that the slot exists, is a GAME slot, is AVAILABLE, and is not
        already used by another active game. Returns the resolved slot on success;
        raises a structured error carrying ``details["reason"]`` (``slot_missing``
        / ``not_game_slot`` / ``slot_unavailable`` / ``slot_already_filled``, the
        machine-readable codes the move panel and draft review consume) otherwise.
        ``exclude_game_id`` is the game being moved — excluded from the slot-in-use
        check so a move never conflicts with itself.

        This is a decomposition of :meth:`_assert_slot_free_for_game`, not a
        separate entry point: every placement path (create_game, move_game and the
        draft-commit path) goes through the full checker, which calls this first.
        The #277 turnover-buffer and curfew POLICIES layer onto THIS method in the
        policy slice, so they apply uniformly to manual placement and committed
        drafts alike.

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
        # #277 Slice B: the turnover/curfew policy layer this docstring
        # reserved — min-playable, same-rink turnover buffer, and curfew,
        # resolved Rink>Season>Program. No-op until a policy is configured.
        self._assert_slot_meets_policy(slot, season_id,
                                       exclude_game_id=exclude_game_id)
        return slot

    def _assert_slot_free_for_game(self, ice_slot_id, home_team_id, away_team_id,
                                   *, season_id=None, exclude_game_id=None):
        """Shared final conflict check for placing a game on an ice slot (#277).

        THE single choke point that create_game, move_game, AND the draft-commit
        path all route through, so a committed draft is held to exactly the same
        rules as a manual placement (#277 acceptance: schedule commits run the
        same final conflict check as manual moves — there is no draft-only
        exception). Runs the physical-placement checker (:meth:`_assert_slot_free`,
        where the #277 turnover/curfew policies layer) AND rejects placing either
        team on an overlapping fixture. Returns the resolved slot on success;
        raises a structured error carrying ``details["reason"]`` (adds
        ``team_overlap`` to the physical codes) otherwise. ``exclude_game_id`` is
        the game being moved — excluded from the slot-in-use and team-overlap
        checks so a move never conflicts with itself.

        A draft that would double-book a team is rejected atomically at commit,
        exactly like a manual create/move; the draft-review issue flags
        (``list_draft_games``) remain a pre-commit heads-up, not a substitute for
        this final gate.

        Read-only (no transaction of its own) — callers run inside theirs.
        """
        slot = self._assert_slot_free(ice_slot_id, season_id=season_id,
                                      exclude_game_id=exclude_game_id)
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
                    game_type: str = GameType.REGULAR.value,
                    _scope_plan: Optional[dict] = None) -> Game:
        # #277/#313/#318 — the global Program→Team→Rink→Season lock order:
        # Programs are policy scopes the gate reads, and the ice-availability
        # builder already locks Program→Rink→Season, so the Program rows MUST
        # come first here (Program-last was an ABBA deadlock against the
        # builder). The scope locator below is a plain pre-lock read,
        # re-verified under the locks; see _policy_scope_lock_plan /
        # _lock_teams / _lock_rinks for the ordering contract. A caller that
        # already pinned the order at ITS entry (the league-scoped override)
        # passes its plan via ``_scope_plan``; re-locking the SAME held rows
        # below is a no-op, and re-planning here instead could discover — and
        # lock — a fresh Program/Season row AFTER the Rink, re-creating the
        # very inversion this order exists to prevent (drift is refused by
        # the verify instead).
        _plan = _scope_plan
        if _plan is None:
            _pre_slot = self.store.get_ice_slot(ice_slot_id)  # locator only
            _plan = self._policy_scope_lock_plan(
                (_pre_slot.rink_id,) if _pre_slot is not None else (),
                (season_id,))
        self._lock_programs(_plan["programs"])
        self._lock_teams((home_team_id, away_team_id))
        _target = self.store.get_ice_slot(ice_slot_id)
        if _target is not None:
            if (_scope_plan is not None and _target.rink_id
                    not in _scope_plan.get("locked_rinks", ())):
                # The caller (league-scoped override) already locked its
                # Seasons; taking a FRESH Rink lock here would invert
                # Rink-before-Season against the builder. Refuse-and-retry
                # instead — the fresh attempt re-plans and locks in order.
                raise ConcurrencyConflictError(
                    "This ice slot changed while processing the request; "
                    "please retry.",
                    {"reason": "placement_raced", "ice_slot_id": ice_slot_id})
            self._lock_rinks((_target.rink_id,))
        self._lock_seasons(_plan["seasons"])
        season = self._require_active_season(season_id)  # #159 read-only guard
        self._verify_policy_scope_plan(
            _plan, (_target.rink_id,) if _target is not None else (),
            season_ids=(season_id,), exclude_slot_ids=(ice_slot_id,))

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
        # rules (and, in the policy slice, turnover + curfew). The team locks
        # taken above make the team-overlap half atomic; the one-game-per-slot
        # half is backstopped by the ux_games_active_ice_slot partial unique index
        # (migration 022), so a race-losing insert fails with ice_slot_taken.
        slot = self._assert_slot_free_for_game(
            ice_slot_id, home_team_id, away_team_id, season_id=season_id)
        # #277 Slice B review — the rink lock above was taken from a PRE-lock
        # locator read; if that read missed (the slot materialized between the
        # locator and the gate's own re-read — e.g. a concurrent builder
        # commit landing a predictable next id) this whole placement,
        # including the turnover-buffer scan (which unlike slot occupancy has
        # NO DB backstop), would run with no rink lock at all. Mirror
        # move_game's defensive re-verify: refuse with a stable retryable
        # conflict instead of writing unserialized. A rink mismatch (slot
        # replaced under the same id) is the same unlocked condition.
        if _target is None or slot.rink_id != _target.rink_id:
            raise ConcurrencyConflictError(
                "This ice slot changed while processing the request; "
                "please retry.",
                {"reason": "placement_raced", "ice_slot_id": ice_slot_id})

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

    def move_game(self, game_id: str, new_ice_slot_id: str, reason: str = "",
                  actor_id: Optional[str] = None) -> Game:
        """Move a game to another available game ice slot (drag/drop)."""
        return self._retry_on_move_race(
            lambda: self._move_game_locked(
                game_id, new_ice_slot_id, reason=reason, actor_id=actor_id),
            game_id=game_id)

    def _retry_on_move_race(self, attempt_fn, *, game_id=None):
        """Run ``attempt_fn`` inside a fresh transaction, up to 3 times,
        converting the internal ``_MoveGameRaced`` retry signal into a stable
        ``ConcurrencyConflictError`` once retries are exhausted (#314 review).

        ``move_game``'s own transaction boundary lives here (not ``@_transactional``
        on the public method) so a raced attempt can roll back — releasing every
        lock it took — and retry clean in a NEW transaction, rather than trying to
        widen an already-held lock set mid-transaction. The league-scoped override
        shares this same helper so its own pre-check + the base's locked body run
        as one attempt together. ``game_id`` is only for the exhausted-retry error
        detail; callers that don't have one handy may omit it."""
        for attempt in range(3):
            try:
                with self.store.transaction():
                    return attempt_fn()
            except _MoveGameRaced:
                if attempt == 2:
                    raise ConcurrencyConflictError(
                        "This game's ice changed while processing the move; "
                        "please retry.",
                        {"reason": "move_raced", "game_id": game_id})

    def _move_game_locked(self, game_id: str, new_ice_slot_id: str,
                          reason: str = "",
                          actor_id: Optional[str] = None,
                          scope_check=None) -> Game:
        """``move_game``'s locked body — runs inside the caller's transaction
        (``move_game`` itself, or the league-scoped override's own attempt via
        ``_retry_on_move_race``). Raises ``_MoveGameRaced`` to signal a clean
        retry; the caller's transaction rolls back, so no partial write.

        ``scope_check`` (#314 review) is an optional ``(game, new_slot) -> None``
        hook the league-scoped override supplies to re-validate league-ice
        eligibility (``require_game_league_id`` / ``require_slot_belongs_to_season``)
        under the Team/Rink/Season locks just acquired above — called with the
        FRESH, post-lock ``game`` and the resolved target slot, after the
        same-slot no-op check (a no-op move never needs re-scoping) and before
        the final conflict check or any write. The base class has no notion of
        league scope itself, so this is a no-op unless a caller supplies one."""
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.",
                                details={"reason": "game_missing"})
        # #277/#313/#318 — the global Program→Team→Rink→Season lock order:
        # the Program rows (policy scopes the gate reads — the game's own and
        # every neighbor's on either candidate rink) come FIRST, matching the
        # ice-availability builder's Program→Rink→Season; then both teams,
        # then the source AND target slots' rinks, then every involved
        # Season, so the release-old + allocate-new is atomic against a
        # concurrent placement sharing a team, against the builder on either
        # rink, and against any scheduling-policy edit the gate would read.
        # The scope locator is a plain pre-lock read, re-verified under the
        # locks below.
        _pre_rinks = set()
        for _sid in (new_ice_slot_id, game.ice_slot_id):
            _s = self.store.get_ice_slot(_sid) if _sid else None
            if _s is not None:
                _pre_rinks.add(_s.rink_id)
        _plan = self._policy_scope_lock_plan(_pre_rinks, (game.season_id,))
        self._lock_programs(_plan["programs"])
        self._lock_teams((game.home_team_id, game.away_team_id))
        # #314 review — re-read NOW that the Team lock is held, not the
        # pre-lock locator above. move_game is the ONLY writer of an existing
        # Game's ice_slot_id, and it always takes this same Team lock first, so
        # THIS read is the definitive, stable current slot for the rest of this
        # transaction. Locking rinks from the pre-lock snapshot instead was the
        # bug: a move queued behind this same Team lock could still be holding
        # a now-stale source Rink after the lock-holder it waited on already
        # relocated the game — leaving the ACTUAL current Rink unlocked while
        # this move frees a slot on it, reopening the exact builder-vs-move
        # race the Rink lock (#313) was meant to close.
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.",
                                details={"reason": "game_missing"})
        _mv_rinks = set()
        for _sid in (new_ice_slot_id, game.ice_slot_id):
            _s = self.store.get_ice_slot(_sid) if _sid else None
            if _s is not None:
                _mv_rinks.add(_s.rink_id)
        self._lock_rinks(_mv_rinks)
        # Seasons in ONE sorted batch BEFORE the candidate-season guard (the
        # guard's own FOR UPDATE is then an idempotent re-lock) — guarding
        # first would take the candidate row out of sorted order and let two
        # moves with overlapping season sets ABBA each other.
        self._lock_seasons(_plan["seasons"])
        self._guard_game_season(game)  # #159 read-only guard
        # #159 r15 — re-fetch under the Season lock (the pre-lock read was a
        # locator). A concurrent publish_game (which does not take the Team
        # lock) commits under the same Season lock; acting on the stale object
        # would clobber the game's current published/locked state.
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.",
                                details={"reason": "game_missing"})
        # #314 review — defensive verify: the Rinks we hold MUST be exactly the
        # game's CURRENT source Rink plus the target slot's Rink. Provably true
        # given the Team lock makes ice_slot_id stable for this whole
        # transaction (see above) — but cheap, explicit insurance against a
        # future writer that skips the Team-lock convention beats silently
        # trusting a lock set we can no longer prove is right; retry clean
        # rather than proceed against it.
        _now_rinks = set()
        for _sid in (new_ice_slot_id, game.ice_slot_id):
            _s = self.store.get_ice_slot(_sid) if _sid else None
            if _s is not None:
                _now_rinks.add(_s.rink_id)
        if _now_rinks != _mv_rinks:
            raise _MoveGameRaced()
        # #318 — the policy-scope snapshot must hold under the locks too: the
        # definitive game/rinks may have drifted from the pre-lock locator
        # the Program locks were planned from. Signal the caller's retry
        # harness rather than surfacing: a fresh attempt re-plans and locks
        # the drifted scopes, then reports the PRECISE terminal error.
        try:
            self._verify_policy_scope_plan(
                _plan, _mv_rinks, season_ids=(game.season_id,),
                exclude_slot_ids=(new_ice_slot_id,),
                exclude_game_id=game.id)
        except ConcurrencyConflictError:
            raise _MoveGameRaced()
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
        if scope_check is not None:
            scope_check(game, new_slot)
        # Shared final conflict check (#277) — identical rules to create +
        # draft-commit; excludes THIS game so a move never conflicts with itself.
        # The team locks taken above make the team-overlap half atomic; the
        # one-game-per-slot half is backstopped by ux_games_active_ice_slot.
        new_slot = self._assert_slot_free_for_game(
            new_ice_slot_id, game.home_team_id, game.away_team_id,
            season_id=game.season_id, exclude_game_id=game_id)

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
            # #331 review round 14 finding 3: reserve the id via next_id
            # ("team") FIRST (blocks a concurrent creator until it commits/
            # rolls back), then re-check under that lock before inserting —
            # commit_hierarchy_import's own {code: obj} snapshot (and its
            # _preflight_reassignment_safety pass) both ran before this call,
            # so a row this recheck now finds is one a DIFFERENT concurrent
            # writer created in that gap: the whole batch's snapshot is
            # stale, not just this one row. Raise rather than silently
            # adopting it in place (see _HierarchyTeamOrPlayerDrifted's own
            # docstring for why that would bypass the preflight check).
            _reserved_team_id = self.store.next_id("team")
            if any(t.external_ref == code for t in self.store.all_teams()):
                raise _HierarchyTeamOrPlayerDrifted()
            obj = Team(id=_reserved_team_id, external_ref=code,
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

    def upsert_imported_player(self, code: str, name: Optional[str],
                               team_id: str,
                               position: Position, jersey_number: Optional[int],
                               email: Optional[str], existing=None,
                               actor_id: Optional[str] = None,
                               import_batch_id: Optional[str] = None,
                               staged_original_jersey=_UNSET,
                               staged_original_registration=_UNSET,
                               first_name: Optional[str] = None,
                               last_name: Optional[str] = None,
                               preferred_name: Optional[str] = None,
                               birthdate: Optional[str] = None,
                               registration_number: Optional[str] = None,
                               shoots: Optional[str] = None,
                               skill_rating=None):
        """Upsert a Player by its stable player_code (#260), syncing an
        optional email the same way ``add_player`` does: an existing
        ``player:<id>`` ContactDestination's value is updated in place,
        never duplicated. Omitting the email on a repeat row leaves a
        previously-set contact untouched — clearing/retiring a contact is
        #232's own explicit, audited action, never an import side effect.

        #273 identity: the caller may supply structured
        ``first_name``+``last_name`` (the flattened ``name`` is then derived
        through the same shared contract as create/edit) plus the optional
        ``preferred_name`` / ``birthdate`` / ``registration_number`` /
        ``shoots`` / ``skill_rating`` cells. Every optional cell follows the
        email rule above: ``None`` (absent/blank) means "leave as-is on
        update, unset on create" — an import never clears identity data. A
        same-team duplicate registration number is refused before any write;
        a same-name-on-one-Team create appends the ``player_duplicate_warning``
        audit (warn only, never a block or merge). ``staged_original_
        registration`` (#273 review round 3 finding 1) is the pre-staging
        registration_number a caller's own
        :meth:`release_batch_player_registrations` pre-pass already reported
        for this player, if it released one — see that method and
        ``staged_original_jersey`` immediately below for why a caller running
        that pre-pass must pass its result back here rather than let this
        method read ``existing.registration_number`` directly.

        ``jersey_number`` (#269, predates the ``identity_values`` pattern
        above) now follows the SAME "leave as-is" contract, computed the
        SAME way as ``registration_number``'s ``effective_registration``
        just below (#424 round-4 review): a blank cell (``jersey_number is
        None``) RETAINS the pre-staging original — ``staged_original_jersey``
        when a :meth:`release_batch_player_jerseys` pre-pass supplied one,
        else ``existing.jersey_number`` — rather than landing ``None`` and
        silently clearing a real number. Before this fix, ``jersey_number``
        was placed into ``values`` unconditionally as the raw (possibly
        ``None``) argument and checked for availability the same raw way, so
        a blank cell on a Team move both evaded the destination team's
        uniqueness check (a ``None`` availability check is a deliberate
        no-op — an absent jersey never collides) AND then overwrote the
        retained value with ``None`` at write time, once
        ``staged_original_jersey`` had already restored ``obj.jersey_number``
        for diffing. A brand-new player (``existing is None``) has nothing to
        retain, so its effective value is simply the supplied one, exactly as
        before.
        """
        self._validate_jersey_number(jersey_number)
        # Validate/canonicalize the email BEFORE any player write (#268 review):
        # a non-string/non-None value (False, 0, a list) or a malformed string
        # raises a field-level invalid_email here, so the method never applies a
        # partial player change even when a direct caller supplies no outer
        # transaction. None/blank canonicalizes to None -> a no-op below (the
        # import rule: an absent cell is "leave as-is", never a retirement).
        canonical_email = self._validate_email(email)
        canonical_name, canonical_first, canonical_last = (
            self._resolve_new_player_names(name, first_name, last_name))
        identity_values = {}
        if canonical_first is not None:
            identity_values["first_name"] = canonical_first
            identity_values["last_name"] = canonical_last
        if preferred_name is not None:
            identity_values["preferred_name"] = (
                self._validate_preferred_name(preferred_name))
        if birthdate is not None:
            identity_values["birthdate"] = self._validate_birthdate(birthdate)
        if registration_number is not None:
            identity_values["registration_number"] = (
                self._validate_registration_number(registration_number))
        if shoots is not None:
            identity_values["shoots"] = self._validate_shoots(shoots)
        if skill_rating is not None:
            identity_values["skill_rating"] = (
                self._validate_skill_rating(skill_rating))
        if existing is not None:
            # #331 review round 15 finding 2 — re-fetch the Player under its
            # ROW LOCK before reading any of its fields, mirroring
            # upsert_imported_team's identical #201 fix (which locks the
            # Team the same way before its own field/league write). The
            # caller (commit_hierarchy_import) already guarantees `existing`
            # is a fresh, locked object via its own pre-lock pass before
            # calling this method, so this re-fetch is a no-op there (the
            # same row locked again) — but it keeps this method safe to call
            # on its own, not just as part of that caller's larger batch
            # discipline. A Player deleted out from under the import is
            # dropped to the create path below (its own reserve-then-recheck
            # already discovers and safely adopts a concurrently recreated
            # row instead of duplicating it), never treated as a still-live
            # target for a save that would resurrect it (Memory), silently
            # match zero rows while still reporting success (SQLite), or
            # lose a concurrent update (PostgreSQL).
            existing = self.store.get_player_for_update(existing.id)
        # #424 round-4 review: the RETAINED fallback must be the TRUE
        # pre-staging value — ``staged_original_jersey`` when the caller ran
        # a swap-safe :meth:`release_batch_player_jerseys` pre-pass (mirrors
        # #292), never ``existing.jersey_number`` read directly, which may
        # already carry the transient released NULL. The EFFECTIVE value —
        # the row's own supplied number, or that true original when the cell
        # is blank — is what both the availability check and the actual
        # write use from here on, exactly like ``effective_registration``
        # below; ``jersey_number`` itself (the raw, possibly-``None``
        # argument) is never used again past this point.
        original_jersey = (
            staged_original_jersey if staged_original_jersey is not _UNSET
            else (existing.jersey_number if existing is not None else None))
        effective_jersey_number = (
            jersey_number if jersey_number is not None else original_jersey)
        # Enforce active-team jersey uniqueness on the IMPORTED target state
        # before any write (#269), so a conflicting row aborts the whole
        # one-transaction batch with zero committed players. An import never
        # toggles is_active, so an updated player keeps its current active
        # state; only an active target reserves a number, and it excludes
        # itself so re-importing the same row is a no-op, not a self-collision.
        target_active = True if existing is None else existing.is_active
        if target_active:
            self._assert_jersey_available(
                team_id, effective_jersey_number,
                exclude_player_id=None if existing is None else existing.id)
        values = {"name": canonical_name, "team_id": team_id,
                  "position": position,
                  "jersey_number": effective_jersey_number,
                  **identity_values}
        # Same-team duplicate governing-body id (#273): refuse BEFORE any
        # write, excluding the row's own player when this is an update.
        #
        # #273 review round 2 finding 2: check the EFFECTIVE registration
        # number the row will carry after this write, not just a
        # newly-supplied one. The previous version only checked when
        # ``new_registration is not None`` and it differed from the
        # existing row's stored value, so a blank cell (``registration_number``
        # stays unset in ``identity_values`` here -- "leave as-is" on update,
        # per this method's own contract) or an explicitly re-supplied
        # UNCHANGED value skipped the check entirely, even though ``team_id``
        # may be moving the player onto a team that already holds that same
        # number. Now it always runs against the value the row will actually
        # carry -- ``new_registration`` when the sheet supplied one, else the
        # existing row's own retained value -- exactly like the unconditional
        # jersey check just above. ``exclude_player_id`` keeps a same-team
        # no-op (or a same-team re-import) from colliding with itself.
        #
        # #273 review round 3 finding 1: the RETAINED fallback must be the
        # TRUE pre-staging value, not ``existing.registration_number`` read
        # directly -- when the caller ran a swap/cycle-safe
        # ``release_batch_player_registrations`` pre-pass (mirroring #292's
        # jersey release) before calling this method, ``existing`` may
        # already carry the transient released NULL, which would make a
        # blank-cell row that is actually retaining a real number look like
        # it holds nothing at all and skip this check entirely.
        # ``staged_original_registration`` is that pre-pass's own reported
        # original, exactly like ``staged_original_jersey`` above.
        original_registration = (
            staged_original_registration
            if staged_original_registration is not _UNSET
            else (existing.registration_number if existing is not None
                 else None))
        new_registration = identity_values.get("registration_number")
        effective_registration = (
            new_registration if new_registration is not None
            else original_registration)
        if effective_registration is not None:
            self._assert_registration_number_available(
                team_id, effective_registration,
                exclude_player_id=None if existing is None else existing.id)
        if existing is None:
            # #331 review round 14 finding 3: same mechanism as
            # upsert_imported_team above -- reserve first, recheck under
            # that lock, raise (don't silently adopt) if a DIFFERENT
            # concurrent writer already created this player_code.
            _reserved_player_id = self.store.next_id("player")
            if any(p.external_ref == code for p in self.store.all_players()):
                raise _HierarchyTeamOrPlayerDrifted()
            obj = Player(id=_reserved_player_id, external_ref=code,
                        **values)
            self.store.add_player(obj)
            self._audit("player_created", "player", obj.id, actor_id,
                       {"import_batch_id": import_batch_id, "external_ref": code,
                        "team_id": team_id})
            # Same-name-on-one-Team WARNING (#273 AC[4]) — audit only.
            self._warn_same_name_duplicates(
                obj, actor_id, import_batch_id=import_batch_id)
            created, changed = True, []
        else:
            obj = existing
            # If a swap-safe pre-pass released this row's jersey to NULL (#292),
            # restore its real pre-staging value BEFORE diffing so the change
            # set reflects the operator's true before→after — a Team-only move
            # that keeps the same number must NOT report a jersey change, and a
            # blank/keep-current cell must land the original, not the NULL.
            # ``values["jersey_number"]`` is always the EFFECTIVE value
            # computed above (#424 round-4 review) — the row's own supplied
            # number, or this SAME restored original when the cell was blank
            # — so a genuine blank-cell retain now diffs original-vs-original
            # (no reported change) and a genuine supplied change still diffs
            # original-vs-new (reported and applied) exactly as before.
            if staged_original_jersey is not _UNSET:
                obj.jersey_number = staged_original_jersey
            # Same restore-before-diff for registration_number (#273 review
            # round 3 finding 1) — but registration_number reaches ``values``
            # by a DIFFERENT route than jersey_number's effective-value
            # computation above: it is placed into ``values`` ONLY when the
            # sheet supplies a new one (the same "absent key = leave as-is"
            # contract every other optional identity cell in
            # ``identity_values`` follows), so the restore alone is enough
            # here: a blank cell's ``values`` carries no "registration_number"
            # key at all, ``_apply_changes`` never touches the just-restored
            # original, and no false "changed" report follows; an explicitly
            # re-supplied value is still in ``values`` and still overwrites +
            # reports exactly as before. Both routes land on the same
            # "blank retains, ``_apply_changes`` never sees a spurious diff"
            # outcome; only the mechanism (unconditional effective value vs.
            # conditional key placement) differs, because jersey_number is a
            # required top-level field on every ``Player`` and
            # registration_number is one of the purely-optional identity
            # cells.
            if staged_original_registration is not _UNSET:
                obj.registration_number = staged_original_registration
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
        """Upsert a SeasonTeamRegistration by its exact (team, target
        LeagueSeason) identity (#260, #331 review round 18) — it has no
        external_ref of its own. Mirrors ``register_team_for_season``'s
        Rule 5 reactivation (an inactive prior row AT THE EXACT TARGET is
        reactivated in place, never duplicated), but unlike that
        interactive method — which rejects an already-active row as a
        duplicate — an ACTIVE existing row at the exact target is simply
        updated in place: re-importing the same or corrected registration
        data must never error.

        Resolved via the SAME exact-identity helper commit_teams_players_
        import's gate/apply share (round 17) — never the first
        registration ``registrations_for_season`` happens to return, which
        could cannibalize an inactive HISTORICAL row (destroying what
        ``transfer_team_to_league`` deliberately preserved) or collide
        with an already-correct different active one. Raises on an
        unresolvable conflict (more than one active row this call's own
        target can't unambiguously absorb) as a backstop — the caller's
        own pre-write gate (``_preflight_reassignment_safety``) is
        expected to have already predicted and rejected this via the same
        resolver before any row in the batch is written.
        """
        self._require_active_season(season_id)  # #159 read-only guard
        # #283: a registration is stored against a LeagueSeason. Resolve (create
        # if needed) the imported League's participation in the Season; a change
        # of League is now a change of the registration's LeagueSeason.
        ls = self._link_league_season(league_id, season_id)
        reg, _is_move, _conflict_ids = self._resolve_import_row_registration(
            season_id, team_id, league_id)
        if _conflict_ids:
            raise ValidationError(
                "This team already has more than one active registration "
                "in this season; resolve the conflict before importing.",
                {"reason": "team_registration_conflict", "team_id": team_id,
                 "affected_registration_ids": _conflict_ids})
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

    def release_batch_player_registrations(self, assignments) -> dict:
        """Stage a batch import's registration_number moves so a valid
        same-team swap or longer cycle can commit (#273 review round 3
        finding 1) — the SAME mechanism as :meth:`release_batch_player_jerseys`
        (#292) above, applied to ``registration_number`` instead of
        ``jersey_number``, with the one difference the invariant itself
        already draws: a registration number is reserved by an INACTIVE
        player too (migration 058, ``_assert_registration_number_available``),
        so this release is NOT conditioned on ``is_active`` the way the
        jersey release is.

        A sequential per-row apply cannot commit an otherwise-valid same-team
        registration swap (A ``REG-A``→``REG-B``, B ``REG-B``→``REG-A``) or a
        longer cycle: the first write collides with the number a later row in
        the SAME batch still holds. Run FIRST, inside the batch's single
        transaction: for every EXISTING player (active or inactive) whose
        final ``(team, registration_number)`` differs from its current one,
        release the number it holds now (set ``registration_number = NULL`` —
        always unconstrained; migration 058's partial unique index excludes
        NULL), so the subsequent per-row assignment lands the validated final
        state with no transient uniqueness failure. Only
        ``registration_number`` is touched — the id, the team, the jersey,
        and every other field are preserved — and no audit is written here:
        the per-row upsert emits the real ``player_created`` /
        ``player_updated`` entry with the final value. A genuine final-state
        collision is still caught by the per-row
        ``_assert_registration_number_available`` (and the DB index), so the
        whole batch rolls back with zero writes — the SAME final-state
        question :func:`hockey_scheduler.domain.identity.
        plan_effective_registration_state` already answered for the preview
        that gated this commit.

        ``assignments`` is an iterable of ``(existing_player, final_team_id,
        final_registration_number)``; new players (``existing_player is
        None``), numberless players, and players staying in the same slot are
        skipped. Returns ``{player_id: pre-release registration_number}`` for
        exactly the players it released, so the apply step can restore each
        real original — a blank/keep-current cell must land the ORIGINAL
        value, not the transient NULL, and the single final audit must
        describe the operator's real before→after, not the staging.
        """
        released = {}
        for existing, final_team_id, final_registration in assignments:
            if existing is None:
                continue
            if existing.registration_number is None:
                continue  # holds no number → nothing to release
            if (existing.team_id, existing.registration_number) == (
                    final_team_id, final_registration):
                continue  # staying put → keep it so real collisions still catch
            released[existing.id] = existing.registration_number
            existing.registration_number = None
            self.store.save_player(existing)
        return released

    # -- convenience: add a player to a team ------------------------------
    @_transactional
    def add_player(self, team_id: str, name: Optional[str], position: Position,
                   jersey_number: Optional[int] = None,
                   email: Optional[str] = None,
                   shoots: Optional[str] = None,
                   is_active: bool = True,
                   actor_id: Optional[str] = None,
                   first_name: Optional[str] = None,
                   last_name: Optional[str] = None,
                   preferred_name: Optional[str] = None,
                   birthdate: Optional[str] = None,
                   registration_number: Optional[str] = None,
                   skill_rating: Optional[int] = None) -> Player:
        """Manually create one Player (#114) — the same model/store the CSV
        import path writes, so a league admin isn't forced through Import for
        a single new arrival. Validation mirrors import_validator's row
        checks (jersey_number > 0, an ``@`` with a ``.`` after it in email)
        so a manual create can't slip in data the bulk path would reject.

        #273 identity: a caller supplies EITHER the legacy flattened ``name``
        OR structured ``first_name``+``last_name`` (display name derived,
        never free-typed) — one shared contract with edit and both imports
        (:meth:`_resolve_new_player_names`). ``birthdate`` (private),
        ``registration_number`` (private, same-team duplicates hard-refused),
        ``preferred_name`` and the 1-7 ``skill_rating`` are optional. An
        exact same-name teammate lacking disambiguating data appends a
        ``player_duplicate_warning`` audit — a visible warning, never a
        block and never a merge."""
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
        display_name, canonical_first, canonical_last = (
            self._resolve_new_player_names(name, first_name, last_name))
        canonical_preferred = self._validate_preferred_name(preferred_name)
        canonical_birthdate = self._validate_birthdate(birthdate)
        canonical_registration = self._validate_registration_number(
            registration_number)
        canonical_skill = self._validate_skill_rating(skill_rating)
        self._assert_registration_number_available(
            team_id, canonical_registration)
        player = Player(id=self.store.next_id("player"), team_id=team_id,
                        name=display_name,
                        position=canonical_position,
                        jersey_number=jersey_number,
                        shoots=canonical_shoots,
                        is_active=is_active,
                        first_name=canonical_first,
                        last_name=canonical_last,
                        preferred_name=canonical_preferred,
                        birthdate=canonical_birthdate,
                        registration_number=canonical_registration,
                        skill_rating=canonical_skill)
        self.store.add_player(player)
        self._audit("player_added", "player", player.id, actor_id,
                    {"team_id": team_id})
        self._warn_same_name_duplicates(player, actor_id)
        if canonical_email is not None:
            # Nonblank only: create/reactivate via the shared set/retire path.
            self._set_email_contact(f"player:{player.id}", canonical_email)
        return player

    def _resolve_new_player_names(self, name, first_name, last_name):
        """The ONE name-form contract for Player create and both imports
        (#273 AC[3]) → ``(display_name, first, last)``.

        Structured form: ``first_name``+``last_name`` (both required together
        — ``structured_name_incomplete`` names the missing one), display name
        DERIVED, and a nonblank flattened ``name`` alongside them is refused
        (``conflicting_name_forms``) rather than silently ignored — two
        disagreeing name forms in one request is operator error, not data.
        Legacy form: flattened ``name`` alone, validated exactly as before
        (#268). Structured parts are None in that case — never guessed by
        splitting the display name.
        """
        if first_name is not None or last_name is not None:
            if name is not None and (not isinstance(name, str) or name.strip()):
                raise ValidationError(
                    "Supply either name or first_name+last_name, not both.",
                    {"reason": "conflicting_name_forms", "field": "name"})
            canonical_first = self._validate_name_part(first_name, "first_name")
            canonical_last = self._validate_name_part(last_name, "last_name")
            return (derive_display_name(canonical_first, canonical_last),
                    canonical_first, canonical_last)
        return self._validate_player_name(name), None, None

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

    @staticmethod
    def _validate_name_part(value, field: str) -> str:
        """Canonicalize a REQUIRED structured name part (#273).

        Shared by create, edit, and both import paths — one contract
        (AC[3]). Trims and collapses internal whitespace; rejects
        non-strings, blanks, and over-length values with a field-level
        error naming the exact part.
        """
        canonical, reason = normalize_name_part(value)
        if reason is not None:
            raise ValidationError(
                f"{field} must be a non-empty string of at most "
                f"{MAX_NAME_PART_LENGTH} characters.",
                {"reason": f"invalid_{field}", "field": field})
        return canonical

    @staticmethod
    def _validate_preferred_name(value) -> Optional[str]:
        """Optional preferred name (#273): blank/None → unset, else the same
        shape rules as the required parts."""
        canonical, reason = normalize_preferred_name(value)
        if reason is not None:
            raise ValidationError(
                f"preferred_name must be a string of at most "
                f"{MAX_NAME_PART_LENGTH} characters, or left blank.",
                {"reason": "invalid_preferred_name", "field": "preferred_name"})
        return canonical

    def _validate_birthdate(self, value) -> Optional[str]:
        """Optional PRIVATE birthdate (#273) → canonical ``YYYY-MM-DD``.

        Must be a real calendar date, not in the future — "today" comes from
        the service's injected clock, never a domain-side ``now()``. The
        single gate for create, edit, and both import commits.
        """
        canonical, reason = normalize_birthdate(
            value, today=self.clock().date())
        if reason is not None:
            raise ValidationError(
                f"birthdate must be a real past calendar date in YYYY-MM-DD "
                f"form ({reason}).",
                {"reason": "invalid_birthdate", "field": "birthdate"})
        return canonical

    @staticmethod
    def _validate_registration_number(value) -> Optional[str]:
        """Optional governing-body registration number (#273): trimmed,
        case preserved, no internal whitespace, bounded length."""
        canonical, reason = normalize_registration_number(value)
        if reason is not None:
            raise ValidationError(
                f"registration_number must be a short identifier without "
                f"whitespace ({reason}).",
                {"reason": "invalid_registration_number",
                 "field": "registration_number"})
        return canonical

    @staticmethod
    def _validate_skill_rating(value) -> Optional[int]:
        """Optional 1-7 skill rating (#287 owner ruling → #273); None/blank =
        unrated, a fully supported state."""
        canonical, reason = normalize_skill_rating(value)
        if reason is not None:
            raise ValidationError(
                f"skill_rating must be an integer between {MIN_SKILL_RATING} "
                f"and {MAX_SKILL_RATING}, or left blank.",
                {"reason": "invalid_skill_rating", "field": "skill_rating"})
        return canonical

    def _assert_registration_number_available(
            self, team_id: str, registration_number: Optional[str],
            exclude_player_id: Optional[str] = None) -> None:
        """Refuse a SAME-TEAM duplicate governing-body id (#273).

        Two rows on one Team carrying one registration number is
        definitionally the same athlete twice — a hard field-level error, not
        a warning (and not a merge: the caller's row is simply refused).
        CROSS-team duplicates stay allowed: under the legacy permanent
        Player→Team model one human on two teams is two legitimate rows
        (#205 restructures that); ``player_duplicate_report`` surfaces them
        as warnings instead. Includes inactive players — deactivating a row
        must not free the identity for an accidental duplicate.
        """
        if registration_number is None:
            return
        for other in self.store.players_for_team(team_id):
            if other.id == exclude_player_id:
                continue
            if other.registration_number == registration_number:
                raise ValidationError(
                    "registration_number is already used by another player "
                    "on this team.",
                    {"reason": "duplicate_registration_number",
                     "field": "registration_number"})

    def _warn_same_name_duplicates(self, player, actor_id,
                                   import_batch_id=None) -> None:
        """Append a ``player_duplicate_warning`` audit when a just-written
        player is an exact same-name record on one Team lacking
        disambiguating data (#273 AC[4]).

        A pair counts as DISAMBIGUATED only when both rows carry a birthdate
        or registration number that proves them different people; anything
        less is warned. A WARNING only — never a block, and never any merge
        (records are never matched by name alone, per #273 and the epic #205
        ruling). The audit detail carries ids only: no name text, no
        birthdate, no registration number.
        """
        key = normalized_name_key(player.name)
        if key is None:
            return
        matches = []
        for other in self.store.players_for_team(player.team_id):
            if other.id == player.id:
                continue
            if normalized_name_key(other.name) != key:
                continue
            proven_different = (
                (player.birthdate is not None and other.birthdate is not None
                 and player.birthdate != other.birthdate)
                or (player.registration_number is not None
                    and other.registration_number is not None
                    and player.registration_number
                    != other.registration_number))
            if not proven_different:
                matches.append(other.id)
        if matches:
            detail = {"team_id": player.team_id,
                      "matching_player_ids": sorted(matches)}
            if import_batch_id is not None:
                detail["import_batch_id"] = import_batch_id
            self._audit("player_duplicate_warning", "player", player.id,
                        actor_id, detail)

    @_transactional
    def update_player(self, player_id: str, *, name=_UNSET, position=_UNSET,
                      jersey_number=_UNSET, shoots=_UNSET, email=_UNSET,
                      first_name=_UNSET, last_name=_UNSET,
                      preferred_name=_UNSET, birthdate=_UNSET,
                      registration_number=_UNSET, skill_rating=_UNSET,
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
        address or any other value (nor, #273, the birthdate or registration
        number — private values never enter the audit trail).

        #273 identity edits share the create/import contract: while a player
        carries structured names the flattened ``name`` is DERIVED — editing
        it directly is refused (``name_is_derived``); edit the parts instead
        and the display name follows. The parts live and die together:
        ending up with exactly one of first/last set is
        ``structured_name_incomplete``; clearing BOTH (explicit None) returns
        the player to legacy flattened-name state (the display name stays —
        it is required — and may be edited in that same call). A changed
        registration number re-checks same-team uniqueness excluding self.
        """
        player = self.store.get_player_for_update(player_id)
        if player is None:
            raise NotFoundError(f"Player {player_id} not found.")

        changed = []
        # -- structured names first (#273): the flattened name's editability
        # depends on the structured state AFTER these edits are applied.
        if first_name is not _UNSET or last_name is not _UNSET:
            new_first = (player.first_name if first_name is _UNSET
                         else None if first_name is None
                         else self._validate_name_part(first_name,
                                                       "first_name"))
            new_last = (player.last_name if last_name is _UNSET
                        else None if last_name is None
                        else self._validate_name_part(last_name, "last_name"))
            # Validate the whole name-form outcome BEFORE any assignment.
            if (new_first is None) != (new_last is None):
                raise ValidationError(
                    "first_name and last_name are set and cleared together.",
                    {"reason": "structured_name_incomplete",
                     "field": ("first_name" if new_first is None
                               else "last_name")})
            if new_first is not None and name is not _UNSET:
                raise ValidationError(
                    "Supply either name or first_name+last_name, not both.",
                    {"reason": "conflicting_name_forms", "field": "name"})
            if new_first != player.first_name:
                player.first_name = new_first
                changed.append("first_name")
            if new_last != player.last_name:
                player.last_name = new_last
                changed.append("last_name")
            if new_first is not None:
                derived = derive_display_name(new_first, new_last)
                if derived != player.name:
                    player.name = derived
                    changed.append("name")
        if name is not _UNSET:
            if player.first_name is not None or player.last_name is not None:
                raise ValidationError(
                    "name is derived from first_name+last_name on this "
                    "player; edit those instead.",
                    {"reason": "name_is_derived", "field": "name"})
            new_name = self._validate_player_name(name)
            if new_name != player.name:
                player.name = new_name
                changed.append("name")
        if preferred_name is not _UNSET:
            new_preferred = self._validate_preferred_name(preferred_name)
            if new_preferred != player.preferred_name:
                player.preferred_name = new_preferred
                changed.append("preferred_name")
        if birthdate is not _UNSET:
            new_birthdate = self._validate_birthdate(birthdate)
            if new_birthdate != player.birthdate:
                player.birthdate = new_birthdate
                changed.append("birthdate")
        if registration_number is not _UNSET:
            new_registration = self._validate_registration_number(
                registration_number)
            if new_registration != player.registration_number:
                self._assert_registration_number_available(
                    player.team_id, new_registration,
                    exclude_player_id=player.id)
                player.registration_number = new_registration
                changed.append("registration_number")
        if skill_rating is not _UNSET:
            new_skill = self._validate_skill_rating(skill_rating)
            if new_skill != player.skill_rating:
                player.skill_rating = new_skill
                changed.append("skill_rating")
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

    # -- age eligibility rules + duplicate detection (#273) ---------------

    @_transactional
    def set_age_eligibility_rule(self, league_season_id: str,
                                 cutoff_month, cutoff_day, tiers,
                                 enforcement=None,
                                 actor_id: Optional[str] = None
                                 ) -> AgeEligibilityRule:
        """Append the next VERSION of a LeagueSeason's age-eligibility rule.

        Rules are immutable rows; "changing the rule" writes version N+1 and
        leaves history intact, so any past eligibility answer can name and
        reproduce the exact version it used (the owner's versioned-policy
        pattern from epic #205). Cutoff month/day (Feb 29 refused — the
        cutoff must exist every year), tiers, and the warn-first
        ``enforcement`` mode are canonicalized by the pure domain module
        before any write. Audited without PII: the detail carries scope,
        version, cutoff, tier codes, and mode.

        #273 review round 2 finding 3: the OWNING Season is locked (row-locked
        via ``_require_active_season``, mirroring ``create_league_season`` /
        ``delete_league_season``'s identical Season-first lock order) BEFORE
        any further work, and the LeagueSeason binding is RE-READ under that
        lock. A prior revision read the LeagueSeason with a plain unlocked
        get, never called ``_require_active_season`` at all, and computed
        ``max(version) + 1`` with no lock held — so a rule could be appended
        to an ARCHIVED Season's history (frozen history silently mutated),
        and two concurrent callers appending a rule for the SAME
        league_season_id could both read the same ``existing`` list and both
        compute the same next version (the unique ``(league_season_id,
        version)`` index would then reject the loser outright, rather than
        the two committing consecutive versions the way two concurrent
        ``create_league_season``/``archive_season`` callers already do
        elsewhere in this file). Locking the Season row first closes both
        holes at once: it fails closed on an archived Season exactly like
        every other Season-owned write, AND it serializes every writer
        appending a rule for a LeagueSeason under that same Season — a second
        writer blocks on the row lock until the first commits, then re-reads
        ``existing`` (below) and sees the just-committed row, so both writers
        succeed with genuinely consecutive versions instead of racing for the
        same one. The RE-READ of the LeagueSeason itself (like
        ``delete_league_season``'s own re-fetch) catches a concurrent
        ``delete_league_season`` of this SAME binding, which takes the
        identical Season-row lock first: the loser of that race fails closed
        with ``not_found`` and zero mutation rather than resurrecting a
        rule against a binding that no longer exists.
        """
        if not league_season_id:
            raise ValidationError(
                "league_season_id is required.",
                {"reason": "field_required", "field": "league_season_id"})
        ls = self.store.get_league_season(league_season_id)
        if ls is None:
            raise NotFoundError(
                f"LeagueSeason {league_season_id} not found.")
        if ls.season_id:
            self._require_active_season(ls.season_id)  # #159 read-only guard
        ls = self.store.get_league_season(league_season_id)
        if ls is None:
            raise NotFoundError(
                f"LeagueSeason {league_season_id} not found.")
        canonical_cutoff, reason = normalize_cutoff(cutoff_month, cutoff_day)
        if reason is not None:
            raise ValidationError(
                f"cutoff must be a month/day that exists every year "
                f"({reason}).",
                {"reason": "invalid_cutoff",
                 "field": ("cutoff_month" if reason
                           in ("month_out_of_range", "not_an_integer")
                           else "cutoff_day")})
        canonical_tiers, reason = normalize_age_tiers(tiers)
        if reason is not None:
            raise ValidationError(
                f"tiers must be a non-empty list of unique "
                f"{{code, max_age}} entries ({reason}).",
                {"reason": "invalid_tiers", "field": "tiers"})
        canonical_enforcement, reason = normalize_enforcement(enforcement)
        if reason is not None:
            raise ValidationError(
                "enforcement must be 'warn' or 'block'.",
                {"reason": "invalid_enforcement", "field": "enforcement"})
        # Still holding the Season row lock acquired above: a second
        # concurrent writer blocks on that SAME lock until this transaction
        # commits or rolls back, so the version it computes here can never
        # race another writer's — see the docstring.
        existing = self.store.age_eligibility_rules_for_league_season(
            league_season_id)
        version = existing[-1].version + 1 if existing else 1
        rule = AgeEligibilityRule(
            id=self.store.next_id("agerule"),
            league_season_id=league_season_id,
            version=version,
            cutoff_month=canonical_cutoff[0],
            cutoff_day=canonical_cutoff[1],
            tiers=canonical_tiers,
            enforcement=canonical_enforcement,
            created_at=self.clock(),
            actor_id=actor_id)
        self.store.add_age_eligibility_rule(rule)
        self._audit(
            "age_eligibility_rule_set", "age_eligibility_rule", rule.id,
            actor_id,
            {"league_season_id": league_season_id, "version": version,
             "cutoff": f"{rule.cutoff_month:02d}-{rule.cutoff_day:02d}",
             "tier_codes": [t["code"] for t in canonical_tiers],
             "enforcement": canonical_enforcement})
        return rule

    def current_age_eligibility_rule(
            self, league_season_id: str) -> Optional[AgeEligibilityRule]:
        """The highest-version rule for a LeagueSeason, or None."""
        rules = self.store.age_eligibility_rules_for_league_season(
            league_season_id)
        return rules[-1] if rules else None

    def evaluate_player_age_eligibility(self, player_id: str,
                                        division_id: str) -> dict:
        """Answer #273's acceptance question for one athlete and Division.

        Resolves the Division → LeagueSeason → current rule + Season start,
        reads the athlete's birthdate, and delegates the actual decision to
        the pure domain evaluator. The result names the exact rule id +
        version it used and NEVER contains the birthdate itself — only
        derived values (status/reason/age-at-cutoff), so it is safe as the
        Coach-facing eligibility summary the bounded-#124 ruling requires
        (raw protected values stay operator-only). Honest indeterminates
        (``no_rule`` / ``no_birthdate`` / ``unknown_tier`` /
        ``no_season_start``) are answers, never guesses. Nothing here blocks
        anything: wiring ``enforcement == "block"`` into registration/roster
        mutations is explicitly later policy work.
        """
        player = self.store.get_player(player_id)
        if player is None:
            raise NotFoundError(f"Player {player_id} not found.")
        division = self.store.get_division(division_id)
        if division is None:
            raise NotFoundError(f"Division {division_id} not found.")
        league_season = self.store.get_league_season(
            division.league_season_id)
        rule = self.current_age_eligibility_rule(division.league_season_id)
        out = {"player_id": player_id, "division_id": division_id,
               "league_season_id": division.league_season_id,
               "rule_id": None, "rule_version": None, "enforcement": None}
        if rule is None:
            out.update({"status": "indeterminate", "reason": "no_rule",
                        "tier_code": None, "max_age": None,
                        "age_at_cutoff": None, "cutoff_date": None})
            return out
        season = (self.store.get_season(league_season.season_id)
                  if league_season is not None else None)
        result = evaluate_age_eligibility(
            birthdate=player.birthdate,
            tier_declared=division.age_group,
            cutoff_month=rule.cutoff_month,
            cutoff_day=rule.cutoff_day,
            season_start=season.start_date if season is not None else None,
            tiers=rule.tiers)
        out.update({"rule_id": rule.id, "rule_version": rule.version,
                    "enforcement": rule.enforcement, **result})
        return out

    def player_duplicate_report(self,
                                team_id: Optional[str] = None) -> List[dict]:
        """Duplicate-candidate WARNINGS across players (#273 AC[4]).

        Detection keys on the STABLE identifiers plus birthdate context —
        never on name alone, and this report never merges, writes, or even
        suggests a merge; it only points an operator at rows to inspect.

        Three warning shapes, each with sorted ``player_ids``:

        * ``same_name_same_team_undisambiguated`` — exact same-name records
          on one Team where no pair is PROVEN different people by differing
          birthdates or registration numbers (the issue's minimum warning).
        * ``shared_registration_number`` — one governing-body id on multiple
          rows (cross-team; same-team is hard-refused at write time). The
          identifier value itself is included: this report is operator
          tooling and is listed for the MANAGE_SETUP-gated surface in the
          web follow-up, mirroring ``include_email``.
        * ``same_name_same_birthdate`` — same name AND same birthdate on
          different Teams: very likely one athlete row-duplicated under the
          legacy permanent Player→Team model. The shared birthdate is NOT
          echoed into the report.
        """
        players = (self.store.players_for_team(team_id) if team_id
                   else self.store.all_players())
        warnings = []
        by_team_name = {}
        for p in players:
            key = normalized_name_key(p.name)
            if key is not None:
                by_team_name.setdefault((p.team_id, key), []).append(p)
        for (group_team_id, _key), group in sorted(by_team_name.items()):
            if len(group) < 2:
                continue
            proven = all(
                (a.birthdate is not None and b.birthdate is not None
                 and a.birthdate != b.birthdate)
                or (a.registration_number is not None
                    and b.registration_number is not None
                    and a.registration_number != b.registration_number)
                for i, a in enumerate(group) for b in group[i + 1:])
            if not proven:
                warnings.append({
                    "type": "same_name_same_team_undisambiguated",
                    "team_id": group_team_id,
                    "name": group[0].name,
                    "player_ids": sorted(p.id for p in group)})
        by_registration = {}
        for p in players:
            if p.registration_number is not None:
                by_registration.setdefault(
                    p.registration_number, []).append(p)
        for registration, group in sorted(by_registration.items()):
            if len(group) >= 2:
                warnings.append({
                    "type": "shared_registration_number",
                    "registration_number": registration,
                    "team_ids": sorted({p.team_id for p in group}),
                    "player_ids": sorted(p.id for p in group)})
        by_name_birthdate = {}
        for p in players:
            key = normalized_name_key(p.name)
            if key is not None and p.birthdate is not None:
                by_name_birthdate.setdefault(
                    (key, p.birthdate), []).append(p)
        for (_key, _bd), group in sorted(by_name_birthdate.items()):
            teams = {p.team_id for p in group}
            if len(group) >= 2 and len(teams) >= 2:
                warnings.append({
                    "type": "same_name_same_birthdate",
                    "name": group[0].name,
                    "team_ids": sorted(teams),
                    "player_ids": sorted(p.id for p in group)})
        return warnings

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
    def _find_or_create_import_club(self, club_name: str, actor_id, batch_id):
        """Find-or-create a Club by exact name match for the pilot-onboarding
        CSV imports (#93/#94), shared by EVERY call site so a race between
        different import types -- not just two commits of the same type --
        still converges on one Club (#331 review round 13 finding 2: round
        12 fixed this race but only inside
        commit_officials_availability_import; a second, unprotected copy of
        the same find-or-create in commit_teams_players_import could still
        create a duplicate, and a cross-import race -- one of each type
        landing on the identical new club_name -- was never provably safe
        with two independent implementations of "the same" fix).

        Race-safe via double-checked locking over next_id("club")'s existing
        cross-connection counter-row lock (#331 review round 12 finding 1):
        Club has no unique-by-name index -- asserting one would settle a
        global-uniqueness product question no import commit gets to decide
        on its own, see migration 048's own comment -- so a bare
        check-then-create has no row to lock for a brand-new name.
        Reserving the id FIRST blocks a concurrent creator until THIS
        transaction commits or rolls back; the re-check that follows is
        then guaranteed fresh. A reservation that goes unused (the re-check
        finds the row after all) is a harmless gap in the "club" id
        sequence -- ids are opaque strings.

        Returns ``(club, created)``; callers own their own
        ``counts["clubs_created"]`` bookkeeping from ``created``.
        """
        club = next((c for c in self.store.all_clubs() if c.name == club_name), None)
        if club is not None:
            return club, False
        reserved_id = self.store.next_id("club")
        club = next((c for c in self.store.all_clubs() if c.name == club_name), None)
        if club is not None:
            return club, False
        club = Club(id=reserved_id, name=club_name)
        self.store.add_club(club)
        self._audit("club_created", "club", club.id, actor_id,
                    {"import_batch_id": batch_id})
        return club, True

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

        # today (#273): lets the dry-run report a future birthdate BEFORE
        # any write, from this service's injected clock (never a validator-
        # side now()).
        result = validate_import(sheets, store=self.store,
                                 today=self.clock().date())
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

        # Generated up front (not after the row loops) so every row-level
        # audit entry below can be tagged with it, letting the Activity feed
        # group a batch's individual creates/updates under its summary (#102).
        batch_id = self.store.next_id("importbatch")

        # #331 review round 14 finding 2: counts must reset PER ATTEMPT, not
        # once before the retry loop -- a _TeamLockPlanDrifted retry rolls
        # back every write from the failed attempt, but a Python-level
        # counter increment survives that rollback unless reset here too,
        # which would double-count rows the fresh attempt reprocesses.
        for attempt in range(3):
            counts = {"teams_created": 0, "teams_updated": 0,
                      "players_created": 0, "players_updated": 0,
                      "clubs_created": 0, "divisions_created": 0}
            try:
                # #331 review round 15 finding 1: an existing Team's permanent
                # League must be row-locked BEFORE the Season guard -- the
                # SAME canonical Team -> League -> Season order
                # register_team_for_season already follows (#159). Locking a
                # League only once inside the transaction below (e.g. when
                # binding a new Division to it) would lock League AFTER
                # Season, inverting the order delete_league/transfer_team_
                # to_league rely on and risking a PostgreSQL deadlock.
                # Unlocked pre-read (necessarily stale-tolerant, verified
                # under lock and retried via _TeamLockPlanDrifted on
                # mismatch -- the same idiom this file's other lock-plan
                # fixes already use): every Team this batch's team_code rows
                # currently resolve to.
                _codes_in_batch = {_clean(row.get("team_code")) for row in team_rows}
                _candidate_team_ids = sorted(
                    t.id for t in self.store.all_teams()
                    if t.external_ref in _codes_in_batch)
                with self.store.transaction():
                    # Lock every candidate Team row, in a deterministic
                    # (id-sorted) order, before anything else. A row we hold
                    # locked can't be moved by a concurrent transfer_team_
                    # to_league/delete_team, so its league_id is trustworthy
                    # for the rest of this attempt -- and IS the set of
                    # League ids that need pre-locking next.
                    _locked_teams_by_id = {}
                    for _tid in _candidate_team_ids:
                        _t = self.store.get_team_for_update(_tid)
                        if _t is not None:
                            _locked_teams_by_id[_tid] = _t
                    _candidate_league_ids = sorted({
                        t.league_id for t in _locked_teams_by_id.values()
                        if t.league_id})
                    for _lid in _candidate_league_ids:
                        self._lock_league_for_binding(_lid)
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
                    # Snapshot of every Team known to exist BEFORE the gate_errors
                    # pass below runs (#331 review round 14 finding 2), reused for
                    # both that pass's own existing-lookup AND, later, to detect a
                    # team_code that only became visible AFTER this snapshot was
                    # taken -- a concurrent import for a DIFFERENT Season creating
                    # it in the gap between this snapshot and this attempt's later
                    # double-checked-locking recheck. See _TeamLockPlanDrifted's
                    # docstring for the full mechanism this closes.
                    _teams_at_gate_check = {t.external_ref: t for t in self.store.all_teams()
                                            if t.external_ref}
                    # #331 review round 15 finding 1: verify the League
                    # pre-lock above actually covers every Team this batch
                    # will read as "existing" -- if a code we expected to be
                    # absent/at a locked id now resolves to a DIFFERENT,
                    # unlocked Team (deleted-and-recreated between the
                    # unlocked pre-read and the lock acquisition, or simply
                    # created there for a genuinely new code), retry with a
                    # fresh pre-read/lock plan rather than trust a League we
                    # never actually locked.
                    for _code in _codes_in_batch:
                        _gate_team = _teams_at_gate_check.get(_code)
                        if _gate_team is not None and _gate_team.id not in _locked_teams_by_id:
                            raise _TeamLockPlanDrifted()
                    # Snapshot of every Division known to exist for this
                    # Season BEFORE any row's apply can create one (#331
                    # review round 15 finding 1), so both the gate-check
                    # prediction below and same-upload cross-row conflict
                    # tracking see ONE consistent "what already exists" view
                    # -- not a view that changes mid-pass as earlier rows'
                    # own (not-yet-run) apply creates divisions later rows
                    # would otherwise see. Grouped by name, not a last-wins
                    # {name: division} dict (#331 review round 16
                    # reproduction 3): Division names are not unique within
                    # a Season, so two Divisions can share a name under
                    # different permanent Leagues -- a dict collapse let
                    # gate's snapshot and apply's own live next() lookup
                    # pick DIFFERENT rows for the identical ambiguous name.
                    _divisions_by_name = {}
                    for _d in self.store.divisions_for_season(season_id):
                        _divisions_by_name.setdefault(_d.name, []).append(_d)
                    gate_errors = []
                    # #331 review round 16 reproduction 1: the per-existing-
                    # Team loop below (a) never even runs for a brand-new
                    # Team's row, and (b) previously recorded a not-yet-
                    # existing division_name's predicted League only from
                    # the FIRST row visited, in team_rows upload order --
                    # so a new Team's row, entirely unseen by any gate,
                    # could get applied FIRST (falling back to the Season's
                    # ambient default League with nothing to compare
                    # against), and a LATER existing Team's row would then
                    # silently reuse that already-created Division via
                    # apply's own live divisions_for_season() re-query,
                    # regardless of its own real permanent League --
                    # reversing the two rows' upload order made the defect
                    # disappear, proving it was order-dependent rather than
                    # a real conflict. Collected here across the WHOLE
                    # batch -- every row, new Team or existing -- before
                    # any row is decided, so the result never depends on
                    # which row a loop visits first.
                    _new_division_prefs = {}
                    for row in team_rows:
                        _name_raw = row.get("division_name")
                        if _blank(_name_raw):
                            continue
                        _name = _clean(_name_raw)
                        if _name in _divisions_by_name:
                            continue
                        _code = _clean(row.get("team_code"))
                        _existing = _teams_at_gate_check.get(_code)
                        _league_pref = _existing.league_id if _existing is not None else None
                        if _league_pref:
                            _new_division_prefs.setdefault(_name, set()).add(_league_pref)
                    for _name, _prefs in _new_division_prefs.items():
                        if len(_prefs) > 1:
                            gate_errors.append({
                                "sheet": "teams", "team_code": None,
                                "reason": "import_new_division_league_conflict",
                                "message": (f"Division '{_name}' would be "
                                            "created for more than one "
                                            "permanent league in this same "
                                            "import; give each team's row a "
                                            "consistent division or resolve "
                                            "their leagues first."),
                                "division_name": _name})
                    _new_division_target_league = {
                        _name: next(iter(_prefs))
                        for _name, _prefs in _new_division_prefs.items()
                        if len(_prefs) == 1}
                    # #331 review round 16 reproduction 3: checked for EVERY
                    # row (new Team or existing alike, mirroring the pass
                    # above) since apply's own division resolution must
                    # pick the identical row this validated, never a
                    # first/last-wins guess that can silently diverge.
                    for row in team_rows:
                        _name_raw = row.get("division_name")
                        if _blank(_name_raw):
                            continue
                        _name = _clean(_name_raw)
                        _candidates = _divisions_by_name.get(_name)
                        if not _candidates or len(_candidates) < 2:
                            continue
                        _code = _clean(row.get("team_code"))
                        _existing = _teams_at_gate_check.get(_code)
                        _league_pref = _existing.league_id if _existing is not None else None
                        _, _ambiguous = self._pick_division_candidate(
                            _candidates, _league_pref)
                        if _ambiguous:
                            gate_errors.append({
                                "sheet": "teams", "team_code": _code,
                                "reason": "import_division_name_ambiguous",
                                "message": (f"Division '{_name}' exists "
                                            f"under more than one league in "
                                            f"this season and team {_code}'s "
                                            "own league doesn't resolve it "
                                            "to exactly one; name a "
                                            "different division or resolve "
                                            "the ambiguity first."),
                                "division_name": _name})
                    for row in team_rows:
                        code = _clean(row.get("team_code"))
                        existing = _teams_at_gate_check.get(code)
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
                        # (1b) #331 review round 14 finding 2, widened round 15
                        # finding 1, round 16 reproduction 1: the SAME
                        # invariant as (1), but at the narrower permanent-
                        # League grain -- two Seasons can share one Program
                        # yet carry distinct permanent Leagues, a case (1)
                        # alone cannot see since it only compares Program
                        # ids. Resolved via the ONE shared, whole-batch-
                        # frozen resolver apply below also calls, rather
                        # than this loop's own separate (and, round 16
                        # found, still-incomplete) prediction -- same-upload
                        # new-Division-League conflicts and same-name
                        # Division ambiguity were already fully resolved,
                        # for every row including brand-new Teams', in the
                        # two whole-batch pre-passes above.
                        division, target_league_id, _ambiguous = (
                            self._resolve_row_division_and_league(
                                row.get("division_name"), existing.league_id,
                                _divisions_by_name, _new_division_target_league,
                                season_id))
                        if _ambiguous:
                            continue  # already reported by the pre-pass above
                        if (target_league_id is not None
                                and (existing.league_id or None) != target_league_id):
                            if existing.league_id is None:
                                stray = [r.id for r in team_regs
                                         if self._registration_league_id(r) != target_league_id]
                                if stray:
                                    gate_errors.append({
                                        "sheet": "teams", "team_code": code,
                                        "reason": "team_permanent_league_ambiguous",
                                        "message": (f"Team {code} has registrations in "
                                                    "another permanent league; assign its "
                                                    "permanent league before importing."),
                                        "affected_registration_ids": stray})
                            else:
                                gate_errors.append({
                                    "sheet": "teams", "team_code": code,
                                    "reason": "team_permanent_league_move_blocked",
                                    "message": (f"Team {code} already belongs to a "
                                                "different permanent league; the import "
                                                "can't re-home it."),
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
                        # too (a re-import reactivates and may re-place them). Uses the
                        # SAME `division` the shared resolver above already picked
                        # (#331 review round 16 reproduction 3) rather than a second,
                        # separately-queried next() lookup that could silently pick a
                        # DIFFERENT same-named Division than (1b) just validated.
                        div_name = row.get("division_name")
                        # #331 review round 17: resolved by exact (team, target
                        # LeagueSeason) identity -- and, only when the Team has no
                        # row there yet, its SOLE other active registration in this
                        # Season, reused via the same in-place "move"
                        # transfer_team_to_league itself performs -- never the
                        # first registration `registrations_for_season` happens to
                        # return, which could cannibalize an inactive HISTORICAL
                        # row (destroying what transfer_team_to_league deliberately
                        # preserved) or collide with an already-correct different
                        # active one. See _resolve_import_row_registration.
                        reg, _is_move, _conflict_ids = (
                            self._resolve_import_row_registration(
                                season_id, existing.id, target_league_id))
                        if _conflict_ids:
                            # The Team already holds more active registrations in
                            # this Season than this row's own target can
                            # unambiguously absorb -- never guess which
                            # participation is authoritative or rebind one active
                            # row onto another's unique key; reject before any
                            # write and let the operator resolve it directly.
                            gate_errors.append({
                                "sheet": "teams", "team_code": code,
                                "reason": "team_registration_conflict",
                                "message": (f"Team {code} has more than one "
                                            "active registration in this season; "
                                            "resolve the conflict before "
                                            "importing."),
                                "affected_registration_ids": _conflict_ids,
                                "affected_game_ids":
                                    self._games_scheduled_for_team_in_season(
                                        season_id, existing.id)})
                        elif reg is not None:
                            # #331 review round 17: skipped when `reg` is about to
                            # MOVE into the target LeagueSeason (_is_move) -- its
                            # old division belongs to a DIFFERENT LeagueSeason's
                            # own division pool, so comparing it to the new
                            # target's is meaningless (transfer_team_to_league
                            # itself unconditionally clears the division on a
                            # cross-league move, gated only by the League-change
                            # guard below, never a division comparison).
                            if not _is_move:
                                if _blank(div_name):
                                    target_div_id = None
                                else:
                                    # A not-yet-created named division is necessarily a
                                    # different placement than the current one.
                                    target_div_id = division.id if division else object()
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
                            # (3) #331 review round 16 reproduction 2, round 17: the
                            # STORED registration itself can already be wrong --
                            # pointing at a League other than the one this row (and
                            # (1b) above) resolves to -- entirely independent of
                            # whether Team.league_id itself is already correct. A
                            # pre-round-16 import (or any other write path predating
                            # this invariant) can leave exactly this behind: an
                            # active registration with a matching division_id (so
                            # the check above never fires) but a stale
                            # league_season_id. Repaired with the SAME lifecycle
                            # semantics assign_season_team_league already uses
                            # (never merely folded into apply's raw-save condition,
                            # which would silently move a registration out of a
                            # League committed games still reference): zero
                            # mutation and a structured rejection when a
                            # non-cancelled Game in the CURRENT (soon-to-be-stale)
                            # league would be stranded, otherwise the repair
                            # proceeds in apply below (which rewrites
                            # league_season_id once this gate has cleared it). A
                            # no-op for a non-move `reg` (already in the target
                            # LeagueSeason by construction), so this only ever
                            # fires for the `_is_move` case above.
                            _current_reg_league = self._registration_league_id(reg)
                            if (target_league_id is not None
                                    and _current_reg_league != target_league_id):
                                _league_stranded = [
                                    g.id for g in self.store.all_games()
                                    if not g.cancelled and g.season_id == season_id
                                    and g.league_id == _current_reg_league
                                    and existing.id in (g.home_team_id, g.away_team_id)]
                                if _league_stranded:
                                    gate_errors.append({
                                        "sheet": "teams", "team_code": code,
                                        "reason": "registration_league_change_strands_games",
                                        "message": (f"Re-importing team {code} would move "
                                                    "its registration to a different "
                                                    "league while committed games "
                                                    "reference the current one; resolve "
                                                    "them first."),
                                        "affected_game_ids": _league_stranded})
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
                            # #331 review round 13 finding 2: shared with
                            # commit_officials_availability_import -- see
                            # _find_or_create_import_club's own docstring for why a
                            # bare check-then-create here (this method's own copy,
                            # unprotected until now) could still duplicate a Club
                            # racing a DIFFERENT Season's import, or an officials
                            # import, on the identical new name.
                            club, created = self._find_or_create_import_club(
                                club_name, actor_id, batch_id)
                            if created:
                                counts["clubs_created"] += 1
                            club_id = club.id

                        # #331 review round 15 finding 1: Team identity is
                        # resolved BEFORE Division/LeagueSeason (moved up
                        # from below), so a not-yet-created Division/
                        # LeagueSeason can be bound to the Team's OWN
                        # permanent League when it has one -- an ambient
                        # Season-wide default has no relationship to this
                        # specific Team's identity and is exactly what let
                        # the registration disagree with Team.league_id.
                        team = next((t for t in self.store.all_teams()
                                    if t.external_ref == team_code), None)
                        if team is None:
                            # #331 review round 13 finding 2: this method's own copy
                            # of the same unlocked check-then-create shape Club had
                            # (see _find_or_create_import_club's docstring) --
                            # team_code is documented above as a globally stable
                            # external ref, but this method only locks its OWN
                            # target Season, so two imports for DIFFERENT Seasons
                            # never serialize against each other here and could
                            # each create their own duplicate Team for an identical
                            # new team_code. Same fix, same mechanism: reserve the
                            # id via next_id("team") first (blocks a concurrent
                            # creator until it commits/rolls back), then re-check
                            # under that lock before deciding create-vs-update.
                            _reserved_team_id = self.store.next_id("team")
                            team = next((t for t in self.store.all_teams()
                                        if t.external_ref == team_code), None)
                        # #331 review round 14 finding 2, strengthened round 15
                        # finding 1 with id-verification (not just code
                        # presence, mirroring the Rink lock-plan fix): a Team
                        # this row resolves to must be the IDENTICAL row
                        # gate_errors validated above -- a matching code but a
                        # DIFFERENT id means the original was deleted and a
                        # new one recreated under the same external_ref in the
                        # gap between the gate snapshot and this row's
                        # resolution (a code entirely absent from the snapshot
                        # is the same signal: a concurrent import for a
                        # DIFFERENT Season created it there). Either way none
                        # of the (1)/(1b) guards above ever evaluated the Team
                        # apply is about to use -- retry the whole attempt
                        # with a fresh snapshot/lock plan, which will
                        # correctly gate and lock this Team's real current
                        # state before any write.
                        _gate_team = _teams_at_gate_check.get(team_code)
                        if team is not None and (_gate_team is None
                                                  or _gate_team.id != team.id):
                            raise _TeamLockPlanDrifted()
                        _existing_team_league_id = team.league_id if team is not None else None

                        # #331 review round 16 reproduction 1/3: resolved via
                        # the ONE shared resolver gate validation above also
                        # called, against the SAME frozen _divisions_by_name
                        # and _new_division_target_league -- never apply's
                        # own independent live divisions_for_season()
                        # re-query, which could see (and silently adopt) a
                        # Division an EARLIER row in THIS SAME apply loop
                        # just created, unconstrained by any gate that ever
                        # evaluated that earlier row's own permanent League.
                        division, _target_league_id, _ambiguous = (
                            self._resolve_row_division_and_league(
                                row.get("division_name"), _existing_team_league_id,
                                _divisions_by_name, _new_division_target_league,
                                season_id))
                        assert not _ambiguous, (
                            "gate_errors above must already have rejected "
                            "any ambiguous division before apply runs")
                        if division is None and not _blank(row.get("division_name")):
                            # #283: a Division belongs to a LeagueSeason. This simple
                            # onboarding import carries no per-division League of its
                            # own, so bind the resolved target League into this
                            # Season (#331 review round 15 finding 1, round 16
                            # generalized to the whole batch's cross-row consensus)
                            # -- auto-provisioning a default League only when nothing
                            # in the batch expressed a preference, mirroring
                            # create_division so imported rows are never orphaned
                            # with a null league_season_id.
                            _ls = self._bind_import_league_season(
                                season_id, _target_league_id)
                            division = Division(id=self.store.next_id("division"),
                                                league_season_id=_ls.id,
                                                name=_clean(row.get("division_name")))
                            self.store.add_division(division)
                            self._audit("division_created", "division", division.id,
                                        actor_id, {"season_id": season_id,
                                                  "import_batch_id": batch_id})
                            counts["divisions_created"] += 1
                            # A LATER row in this same apply loop naming the
                            # identical name must reuse THIS row, never re-derive
                            # (and never re-create a duplicate) -- safe to mutate
                            # this frozen-for-reads snapshot because gate_errors
                            # above already proved every row naming this name
                            # agrees on _target_league_id, so the single division
                            # just created is unambiguously the right one for all
                            # of them regardless of upload order.
                            _divisions_by_name.setdefault(division.name, []).append(division)

                        division_id = division.id if division else None

                        # #180/#283: a team's participation is converged onto its
                        # permanent League + a SeasonTeamRegistration, never the legacy
                        # Team.division_id. The team is a permanent member of THIS
                        # import season's League -- its OWN permanent League when it
                        # has one, else auto-provisioned (#331 review round 15 finding
                        # 1); the imported division lives on the registration.
                        _import_ls = (self.store.get_league_season(division.league_season_id)
                                      if division is not None
                                      else self._bind_import_league_season(
                                          season_id, _target_league_id))
                        _import_league_id = _import_ls.league_id if _import_ls else None
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
                            team = Team(id=_reserved_team_id, name=team_name,
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
                        # #331 review round 17: resolved by the SAME exact
                        # (team, target LeagueSeason) identity gate validated
                        # above (never the first registration
                        # `registrations_for_season` happens to return, which
                        # could cannibalize an inactive HISTORICAL row or
                        # collide with an already-correct different active
                        # one) -- see _resolve_import_row_registration. The
                        # conflict case can never actually occur here: gate
                        # already rejected the whole batch before any write
                        # whenever this row's team_registration_conflict fires.
                        reg, _is_move, _conflict_ids = (
                            self._resolve_import_row_registration(
                                season_id, team.id, _import_league_id))
                        assert not _conflict_ids, (
                            "gate_errors above must already have rejected any "
                            "team_registration_conflict before apply runs")
                        if reg is not None:
                            # #331 review round 16 reproduction 2, round 17:
                            # league_season_id is now part of the trigger, not
                            # just the write -- a repeat row with an already-
                            # active, already-division-matching registration
                            # used to skip this branch entirely even when
                            # league_season_id had drifted, silently reporting
                            # success while leaving the stale League in place.
                            # Safe to include unconditionally here (never
                            # "merely" added to a raw save with no guard)
                            # because gate check (3) above already rejected
                            # this exact row, before any write, whenever the
                            # drift/move it is about to perform would strand a
                            # committed Game. When `_is_move` is True, this is
                            # also where the row's league_season_id actually
                            # gets rewritten -- the SAME in-place "move" gate's
                            # (3) already validated as safe.
                            if (not reg.active or reg.division_id != division_id
                                    or reg.league_season_id != reg_ls_id):
                                reg.active = True
                                reg.division_id = division_id
                                reg.league_season_id = reg_ls_id
                                self.store.save_season_team_registration(reg)
                                self._audit("season_team_registration_updated",
                                            "season_team_registration", reg.id, actor_id,
                                            {"season_id": season_id, "team_id": team.id,
                                             "division_id": division_id,
                                             "league_season_id": reg_ls_id,
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

                    # Swap-safe apply, registration_number (#273 review round
                    # 3 finding 1, mirrors the jersey release just above):
                    # release every existing player's registration_number
                    # whose final (team, registration) differs from its
                    # current one BEFORE any per-row write, so a valid
                    # same-team (or cross-team) swap or longer cycle commits
                    # without a transient uniqueness failure. A blank
                    # registration_number cell RETAINS the current value
                    # (final == current → not released).
                    def _final_registration(row):
                        existing = by_code.get(_clean(row.get("player_code")))
                        team_id = team_code_to_id.get(_clean(row.get("team_code")))
                        raw = row.get("registration_number")
                        registration = (_clean(raw) if not _blank(raw)
                                        else (existing.registration_number
                                              if existing else None))
                        return existing, team_id, registration
                    released_registrations = self.release_batch_player_registrations(
                        _final_registration(row) for row in player_rows)

                    for row in player_rows:
                        player_code = _clean(row.get("player_code"))
                        # #273: the sheet's structured names are PERSISTED now
                        # (they were previously flattened away at write time),
                        # through the same shared name contract create/edit
                        # use; the display name is derived, never free-typed.
                        canonical_first = self._validate_name_part(
                            _clean(row.get("first_name")), "first_name")
                        canonical_last = self._validate_name_part(
                            _clean(row.get("last_name")), "last_name")
                        full_name = derive_display_name(canonical_first,
                                                        canonical_last)
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
                        # Optional identity cells (#273) + the shoots import
                        # wiring gap (#268 covered create/edit only). A blank
                        # or absent cell means "leave as-is" on update and
                        # unset on create — never a clearing write (the email
                        # rule).
                        preferred_cell = (
                            self._validate_preferred_name(
                                _clean(row.get("preferred_name")))
                            if not _blank(row.get("preferred_name")) else None)
                        shoots_cell = (
                            self._validate_shoots(_clean(row.get("shoots")))
                            if not _blank(row.get("shoots")) else None)
                        birthdate_cell = (
                            self._validate_birthdate(
                                _clean(row.get("birthdate")))
                            if not _blank(row.get("birthdate")) else None)
                        registration_cell = (
                            self._validate_registration_number(
                                _clean(row.get("registration_number")))
                            if not _blank(row.get("registration_number"))
                            else None)
                        skill_cell = (
                            self._validate_skill_rating(
                                _clean(row.get("skill_rating")))
                            if not _blank(row.get("skill_rating")) else None)

                        player = next((p for p in self.store.all_players()
                                      if p.external_ref == player_code), None)
                        if player is None:
                            # #331 review round 13 finding 2: same gap, same fix as
                            # Team above -- player_code is documented as a globally
                            # stable external ref but only THIS Season's row is
                            # locked, so two different-Season imports could each
                            # create their own duplicate Player for an identical
                            # new player_code.
                            _reserved_player_id = self.store.next_id("player")
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
                            player.first_name = canonical_first
                            player.last_name = canonical_last
                            player.team_id = team_id
                            player.jersey_number = target_jersey
                            if position is not None:
                                player.position = position
                            if preferred_cell is not None:
                                player.preferred_name = preferred_cell
                            if shoots_cell is not None:
                                player.shoots = shoots_cell
                            if birthdate_cell is not None:
                                player.birthdate = birthdate_cell
                            # #273 review round 2 finding 2: check the
                            # EFFECTIVE registration number the row will
                            # carry after this write, not just a
                            # newly-supplied cell. The previous version
                            # only checked ``registration_cell is not
                            # None and registration_cell !=
                            # player.registration_number``, so a BLANK
                            # cell (registration_cell is None -- "leave
                            # as-is", the same rule every other optional
                            # cell in this loop follows) or an explicitly
                            # re-supplied UNCHANGED value skipped the
                            # check entirely, even though ``team_id`` a
                            # few lines up may already have moved this
                            # player onto a team that holds that same
                            # number on another row. Always check the
                            # value the row will actually end up
                            # carrying, exactly like the unconditional
                            # jersey check above; exclude_player_id keeps
                            # a same-team re-import from colliding with
                            # itself.
                            #
                            # #273 review round 3 finding 1: the RETAINED
                            # fallback must be the TRUE pre-staging value —
                            # resolved from ``released_registrations``
                            # (captured before the swap-safe release above),
                            # never ``player.registration_number`` read
                            # directly, which may already hold the transient
                            # released NULL — exactly the same original vs.
                            # transient distinction ``original_jersey`` draws
                            # for jersey_number just above. The final value is
                            # then assigned UNCONDITIONALLY (mirroring
                            # ``player.jersey_number = target_jersey`` above),
                            # not only ``if registration_cell is not None``:
                            # a blank cell's effective value already equals
                            # the just-restored original when nothing else in
                            # this batch moved it, and equals the row's own
                            # supplied value when it did — either way this is
                            # always the row's true final state, so it is
                            # always safe (and, once release is in play,
                            # required) to land it.
                            original_registration = released_registrations.get(
                                player.id, player.registration_number)
                            effective_registration = (
                                registration_cell if registration_cell is not None
                                else original_registration)
                            if effective_registration is not None:
                                self._assert_registration_number_available(
                                    team_id, effective_registration,
                                    exclude_player_id=player.id)
                            player.registration_number = effective_registration
                            if skill_cell is not None:
                                player.skill_rating = skill_cell
                            self.store.save_player(player)
                            self._audit("player_updated", "player", player.id, actor_id,
                                        {"team_id": team_id, "import_batch_id": batch_id})
                            counts["players_updated"] += 1
                        else:
                            # A brand-new imported player is active, so its number must
                            # be free among the team's active players (#269).
                            self._assert_jersey_available(team_id, jersey_number)
                            # Same-team duplicate governing-body id (#273): hard
                            # error BEFORE the write, aborting the whole batch.
                            self._assert_registration_number_available(
                                team_id, registration_cell)
                            # The domain model requires a Position with no default,
                            # but #92 doesn't require the CSV to supply one — default
                            # a brand-new player to FORWARD as an explicit judgment
                            # call; may want revisiting.
                            player = Player(id=_reserved_player_id, team_id=team_id,
                                            name=full_name,
                                            position=position or Position.FORWARD,
                                            jersey_number=jersey_number,
                                            external_ref=player_code,
                                            first_name=canonical_first,
                                            last_name=canonical_last,
                                            preferred_name=preferred_cell,
                                            shoots=shoots_cell,
                                            birthdate=birthdate_cell,
                                            registration_number=registration_cell,
                                            skill_rating=skill_cell)
                            self.store.add_player(player)
                            self._audit("player_added", "player", player.id, actor_id,
                                        {"team_id": team_id, "import_batch_id": batch_id})
                            # Same-name-on-one-Team WARNING (#273 AC[4]) —
                            # an audit entry, never a block, never a merge.
                            self._warn_same_name_duplicates(
                                player, actor_id, import_batch_id=batch_id)
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
                break  # committed cleanly
            except _TeamLockPlanDrifted:
                # A team_code resolved to a real Team outside this
                # attempt's gate-checked snapshot (#331 review round 14
                # finding 2) -- retry with a fresh snapshot, which will
                # correctly gate it.
                if attempt == 2:
                    raise ConcurrencyConflictError(
                        "A team referenced by this import was created "
                        "concurrently; please retry.",
                        {"reason": "team_import_raced"})

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

        # See commit_teams_players_import's identical note: generated up
        # front so every row-level audit entry can be tagged with it (#102).
        batch_id = self.store.next_id("importbatch")

        # Retry the whole batch if the unique-index backstop rejects an
        # INSERT (#331 review round 11 finding 2): unlike
        # commit_teams_players_import, there is no Season row to lock, so a
        # brand-new official_code or availability (official, start, end)
        # tuple has nothing to serialize check-then-create against two
        # concurrent commits landing the same key. Migration 047's unique
        # indexes are the authoritative backstop; a race-losing INSERT rolls
        # the whole attempt back (self.store.transaction() translates it to
        # IntegrityConflictError) and the retry's fresh absence checks see
        # the winner's row and correctly take the update path instead of
        # creating a second one -- mirrors commit_ice_availability's
        # identical retry loop.
        for attempt in range(3):
            counts = {"officials_created": 0, "officials_updated": 0,
                      "availability_created": 0, "availability_updated": 0,
                      "clubs_created": 0}
            try:
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
                            # #331 review round 13 finding 2: shared with
                            # commit_teams_players_import -- see
                            # _find_or_create_import_club's own docstring.
                            club, created = self._find_or_create_import_club(
                                club_name, actor_id, batch_id)
                            if created:
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
                break  # committed cleanly
            except IntegrityConflictError:
                if attempt == 2:
                    raise

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
        ``status`` correctly preserved. This is checked FRESH under the rink
        lock, folded into the overlap gate below rather than run as a
        separate pre-transaction preflight (#331 review round 12 finding 2):
        ``create_game`` takes this SAME rink lock before allocating a slot,
        so a lock-free preflight read can already be stale — observing no
        game yet — by the time this transaction acquires the lock and a
        concurrent ``create_game`` has since committed. The same TOCTOU class
        the overlap gate below was already written to avoid (#158 review).
        Rejects the ENTIRE commit, before any write, if an incoming row would
        change the ``slot_type`` of a slot a game already uses.

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
        # #277 Slice B: store-aware, so the ingest policy advisories
        # (sliver / sub-buffer gap / past-curfew warnings) surface on COMMIT
        # too, not just the dry-run — warnings never block the commit.
        result = validate_import(sheets, store=self.store)
        if not result["ok"]:
            return {"committed": False, "summary": result["summary"],
                    "errors": result["errors"], "warnings": result["warnings"]}

        rink_rows = list(sheets.get("rinks") or [])
        slot_rows = list(sheets.get("ice_slots") or [])

        # See commit_teams_players_import's identical note: generated up
        # front so every row-level audit entry can be tagged with it (#102).
        batch_id = self.store.next_id("importbatch")

        # Retry the whole batch if the unique-index backstop rejects an
        # INSERT (#331 review round 11 finding 2): the row-lock loop just
        # below only covers rinks this import ALREADY has -- a brand-new
        # rink_code has nothing to lock, so two concurrent commits can each
        # see it absent and both attempt to create their own Rink. Migration
        # 048's unique index is the authoritative backstop; a race-losing
        # Rink INSERT rolls the whole attempt back and the retry's fresh
        # by-external_ref lookup sees the winner's row and correctly takes
        # the update path instead of creating a second one -- mirrors
        # commit_ice_availability's and
        # commit_officials_availability_import's identical retry loops. The
        # separate Venue-by-name match is a structurally identical unlocked
        # check-then-create to commit_officials_availability_import's Club-
        # by-name match (also out of this scope) and is not independently
        # closed by this index -- see migration 048's own comment.
        for attempt in range(3):
            counts = {"rinks_created": 0, "rinks_updated": 0,
                      "ice_slots_created": 0, "ice_slots_updated": 0,
                      "venues_created": 0}
            try:
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
                    _planned_ids = sorted(_existing_rink_by_code[c].id for c in _codes
                                         if c in _existing_rink_by_code)
                    _locked_by_id = {_rid: self.store.get_rink_for_update(_rid)
                                     for _rid in _planned_ids}

                    # Freeze the rink_code -> Rink mapping this attempt will use
                    # for BOTH the gates below AND the apply/write phase,
                    # verified against the IDs this attempt actually locked --
                    # not just code presence (#331 review round 14 finding 1).
                    # Round 13's own recheck asked only "did a code that was
                    # absent become present", which misses two narrower windows:
                    # (a) a code can stay PRESENT throughout but silently REMAP
                    # to a different, never-locked Rink id -- e.g. the planned
                    # Rink is deleted (delete_rink permits this once it has no
                    # dependent slots/games) and a new Rink is created under the
                    # same external_ref, racing this attempt's lock acquisition;
                    # get_rink_for_update on the now-deleted id simply returns
                    # None, which round 13's loop silently discarded instead of
                    # treating as drift. (b) a genuinely-new code can materialize,
                    # unlocked, in the gap between this recheck and a LATER fresh
                    # lookup -- which is exactly what the apply phase used to do
                    # below (its own independent all_rinks() scan by code, re-run
                    # per row instead of reusing this snapshot). Comparing every
                    # code's fresh id against what this attempt actually locked,
                    # ONCE here, and then having every downstream read go through
                    # this frozen mapping rather than re-querying, closes both
                    # windows at once: nothing that changes Rink state after this
                    # point can be silently adopted by either the gates or the
                    # write. A code with no fresh Rink at all (never existed, or
                    # deleted with nothing recreated in its place) is frozen to
                    # None -- the apply phase below then reserves+inserts it,
                    # under migration 048's unique external_ref index, exactly
                    # as it already does for a code new from the start.
                    _fresh_rink_by_code = {r.external_ref: r
                                           for r in self.store.all_rinks()
                                           if r.external_ref}
                    _rink_plan = {}
                    for c in _codes:
                        _fresh = _fresh_rink_by_code.get(c)
                        if _fresh is None:
                            _rink_plan[c] = None
                        elif _locked_by_id.get(_fresh.id) is not None:
                            _rink_plan[c] = _fresh
                        else:
                            raise _RinkLockPlanDrifted()

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
                        _rink = _rink_plan.get(_clean(row.get("rink_code")))
                        if _rink is None:
                            continue  # a brand-new rink can't yet have persisted ice
                        _start = _parse_iso_utc(row.get("start_time"))
                        _end = _parse_iso_utc(row.get("end_time"))
                        _persisted = _persisted_by_rink.get(_rink.id, [])
                        _exact = next((s for s in _persisted
                                      if s.start_time == _start and s.end_time == _end),
                                     None)
                        if _exact is not None:
                            # Exact tuple -> update path, not a new overlap. The
                            # booked-slot slot_type gate lives HERE, under the
                            # rink lock (see this function's docstring, "#331
                            # review round 12 finding 2") rather than at an
                            # earlier, lock-free preflight -- create_game takes
                            # this identical rink lock before allocating a
                            # slot, so only a check made fresh at this exact
                            # point can't be stale relative to a concurrent
                            # commit.
                            _new_slot_type = IceSlotType(_clean(row.get("slot_type")))
                            if (_exact.slot_type != _new_slot_type
                                    and self.store.game_using_ice_slot(_exact.id)
                                        is not None):
                                raise ValidationError(
                                    f"Ice slot {_exact.id} on rink_code "
                                    f"{_clean(row.get('rink_code'))} has a game "
                                    f"scheduled on it; slot_type cannot change "
                                    f"from {_exact.slot_type.value} to "
                                    f"{_new_slot_type.value}.")
                            continue
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
                            # #331 review round 12 finding 1: same gap, same
                            # fix as the Club match in
                            # commit_officials_availability_import above (see
                            # that comment for the full mechanism). Venue has
                            # no unique-by-name index -- migration 048's own
                            # comment named this exact interleaving as
                            # deliberately open, product-decision-gated scope;
                            # closed here structurally instead, via
                            # next_id("venue")'s existing cross-connection
                            # lock rather than a new constraint.
                            _reserved_venue_id = self.store.next_id("venue")
                            venue = next((v for v in self.store.all_venues()
                                         if v.name == venue_name), None)
                            if venue is None:
                                venue = Venue(id=_reserved_venue_id, name=venue_name,
                                              address=address)
                                self.store.add_venue(venue)
                                self._audit("venue_created", "venue", venue.id,
                                            actor_id,
                                            {"import_batch_id": batch_id})
                                counts["venues_created"] += 1

                        # Resolve against the frozen, lock-verified _rink_plan
                        # (#331 review round 14 finding 1), NOT a fresh
                        # all_rinks() scan here: a fresh scan at this later
                        # point could silently adopt a Rink that never went
                        # through either gate above -- the same unlocked-
                        # adoption bug the plan freeze exists to close.
                        rink = _rink_plan.get(rink_code)
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
                break  # committed cleanly
            except _RinkLockPlanDrifted:
                # A requested rink_code resolved to a real Rink outside this
                # attempt's lock plan (#331 review round 13 finding 1) — retry
                # with a fresh snapshot, which will lock and gate it correctly.
                if attempt == 2:
                    raise ConcurrencyConflictError(
                        "A rink referenced by this import was created "
                        "concurrently; please retry.",
                        {"reason": "rink_import_raced"})
            except IntegrityConflictError:
                if attempt == 2:
                    raise

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

        PR #423 (design §8.5): acquires the epoch fence's GLOBAL exclusive
        hold first (row 13 of the design's writer table) — an assignment can
        change what the assigned Official's own scoped reads resolve to, and
        the affected user is found only by a lookup, so this uses the GLOBAL
        key (§4.2's classification rule)."""
        self.store.epoch_fence_acquire_exclusive(EPOCH_FENCE_GLOBAL_KEY)
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
        """Remove an official assignment from a game entirely.

        PR #423 (design §8.5): acquires the epoch fence's GLOBAL exclusive
        hold first (row 14 of the design's writer table) — an unassignment is
        an authorization WITHDRAWAL for the affected Official's own scoped
        reads, found only by a lookup, so this uses the GLOBAL key."""
        self.store.epoch_fence_acquire_exclusive(EPOCH_FENCE_GLOBAL_KEY)
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
        scenarios = [s for s in self.store.all_schedule_scenarios()
                     if s.program_id == program_id]
        return [self._dep_group("season", seasons, lambda s: s.name),
                self._dep_group("level", leagues, lambda lg: lg.name,
                                display="league"),
                self._dep_group("team", teams, lambda t: t.name),
                self._dep_group("venue", venues, lambda v: v.name),
                self._dep_group("schedule scenario", scenarios,
                                lambda s: s.name)]

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

    def _membership_label(self, m) -> str:
        """Display label for a SeasonRosterMembership dependency-block row
        (#205 review round 1 finding 2): the player's name plus its current
        status, so a blocked parent mutation names WHO is stranding it, not
        just an opaque membership id."""
        player = self.store.get_player(m.player_id) if m.player_id else None
        name = player.name if player else (m.player_id or "—")
        return f"{name} ({m.status.value})"

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
        scenarios = [s for s in self.store.all_schedule_scenarios()
                     if s.league_id == league_id]
        # #159 — a permanent Team references exactly one League (Team.league_id,
        # #283 rule 3). Deleting the League would orphan those Teams, so they are
        # explicit dependents (there is no FK to catch this at the DB layer).
        teams = [t for t in self.store.all_teams()
                 if t.league_id == league_id]
        # #205 review round 1 finding 2 — same "required FK" shape as
        # registrations/games above: ANY membership on any of this League's
        # LeagueSeasons blocks, regardless of status.
        memberships = [m for m in self.store.all_season_roster_memberships()
                      if m.league_season_id in ls_ids]
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
            self._dep_group("schedule scenario", scenarios,
                            lambda s: s.name),
            self._dep_group("team", teams, lambda t: t.name),
            self._dep_group("season binding", ls_rows,
                            lambda ls: self._season_name(ls.season_id)),
            self._dep_group("roster membership", memberships,
                            self._membership_label)])
        self.store.delete_league(league_id)
        self._audit("level_deleted", "level", league_id, actor_id,
                    {"name": league.name, "program_id": league.program_id})
        return league

    @_transactional
    def delete_program(self, program_id: str, actor_id: Optional[str] = None) -> Program:
        # #318 review — row-lock the Program FIRST and hold it through the
        # policy cascade + delete: set_scheduling_policy locks this same row,
        # so a concurrent Program-scope policy set either committed before
        # this lock (the cascade below removes it) or blocks until the delete
        # commits (then fails policy_scope_missing) — never an unreachable
        # orphan policy row surviving its scope.
        program = self.store.get_program_for_update(program_id)
        if program is None:
            raise NotFoundError(f"Program {program_id} not found.")
        # #201 Slice 4: seasons.program_id / leagues.program_id / venues.league_id
        # now have foreign keys onto programs, so a permanent League is a real
        # dependent (added to the itemised block below, no longer silently
        # orphaned). The incoming FKs backstop the create-child-vs-delete race
        # (the row lock above serializes only same-row writers, i.e. policy
        # sets) and the block is re-resolved on the post-rollback path.
        self._block_if_dependents("league", program_id, "program",
                                  self._program_dependent_groups(program_id))
        self._cascade_scheduling_policy(
            PolicyScopeType.PROGRAM, program_id, actor_id)
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
        scenarios = [s for s in self.store.all_schedule_scenarios()
                     if s.season_id == season_id]
        # Copy-forward commit ledger (#159 review round 3, owner P1,
        # structural change 2): a Season this route MINTED is named by
        # exactly the row(s) SeasonCopyForwardCommit.season_id points at
        # it — the same itemized-dependency pattern as every group above,
        # not a silent orphan and not a raw/generic FK failure. Checked
        # (and blocked) here, BEFORE ``self.store.delete_season`` below
        # ever runs, so migration 053's ``season_copy_forward_commits.
        # season_id`` foreign key is never actually violated: on SQLite
        # and PostgreSQL alike that would otherwise surface as the
        # unhelpful generic ``foreign_key_violation`` conflict this same
        # review reproduced, rather than a named, itemized reason. There
        # is deliberately no separate "clear this dependency" tool (unlike
        # team registrations/venue access/games, an operator never creates
        # or removes a ledger row directly — it exists purely to make a
        # committed copy-forward's replay response stable) — a Season a
        # copy-forward commit produced stays permanently undeletable
        # through this route, which is the tradeoff that guarantees the
        # replay contract below can never observe a torn or missing
        # Season. See _copy_forward_result_from_ledger_row.
        copy_forward_commits = (
            self.store.season_copy_forward_commits_for_season(season_id))
        # #205 review round 1 finding 2 — same "required FK" shape as the
        # registrations/games above: ANY membership in this Season blocks,
        # regardless of status (``season_id`` is denormalized but NOT NULL
        # on every membership row).
        memberships = self.store.memberships_for_season(season_id)
        self._block_if_dependents("season", season_id, "season", [
            self._dep_group("level", levels, lambda lv: lv.name,
                            display="league"),
            self._dep_group("division", divisions, lambda d: d.name),
            self._dep_group("team registration", regs,
                            lambda r: self._team_name(r.team_id)),
            self._dep_group("game", games, self._matchup),
            self._dep_group("schedule scenario", scenarios,
                            lambda s: s.name),
            self._dep_group("venue access", venue_access,
                            lambda a: self._venue_name(a.venue_id)),
            self._dep_group("copy_forward_commit", copy_forward_commits,
                            lambda c: c.copy_forward_fingerprint,
                            display="copy-forward commit"),
            self._dep_group("roster membership", memberships,
                            self._membership_label)])
        self._cascade_scheduling_policy(
            PolicyScopeType.SEASON, season_id, actor_id)
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
        scenarios = [s for s in self.store.all_schedule_scenarios()
                     if s.division_id == division_id]
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
            self._dep_group("game", games, self._matchup),
            self._dep_group("schedule scenario", scenarios,
                            lambda s: s.name)])
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
        # #205 review round 1 finding 2 — a membership's team_id is a
        # REQUIRED (non-nullable) foreign key onto this exact Team, the same
        # shape season registrations/games above already block on regardless
        # of status; ANY membership (even released/transferred history)
        # blocks.
        memberships = [m for m in self.store.all_season_roster_memberships()
                      if m.team_id == team_id]
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
                            # Masked, never the raw destination (#426 review
                            # finding 2): this itemisation is an
                            # unaudited read path outside the
                            # policy+audit boundary, and the raw value is
                            # unnecessary here — the operator only needs
                            # to know THAT a contact destination blocks
                            # the delete, with an optional human label if
                            # one was set.
                            lambda c: c.label or "(unlabeled contact)"),
            self._dep_group("notification preference", prefs,
                            lambda p: p.channel.value),
            self._dep_group("device token", devices,
                            lambda d: d.label or d.provider),
            self._dep_group("roster membership", memberships,
                            self._membership_label)])
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
                            # Masked, never the raw destination (#426 review
                            # finding 2): this itemisation is an
                            # unaudited read path outside the
                            # policy+audit boundary, and the raw value is
                            # unnecessary here — the operator only needs
                            # to know THAT a contact destination blocks
                            # the delete, with an optional human label if
                            # one was set.
                            lambda c: c.label or "(unlabeled contact)"),
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
        # #205 review round 1 finding 2 — a membership's player_id is a
        # REQUIRED (non-nullable) foreign key onto this exact Player; before
        # this check, Memory left a dangling player_id on any surviving
        # membership and SQLite/PostgreSQL surfaced only the DB's generic,
        # untranslated foreign-key-violation error. ANY membership (even
        # released/transferred history) blocks now, mirroring the roster
        # entry/availability/substitute checks above, which also block on
        # historical rows, not just live ones.
        memberships = self.store.memberships_for_player(player_id)
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
                            # Masked, never the raw destination (#426 review
                            # finding 2): this itemisation is an
                            # unaudited read path outside the
                            # policy+audit boundary, and the raw value is
                            # unnecessary here — the operator only needs
                            # to know THAT a contact destination blocks
                            # the delete, with an optional human label if
                            # one was set.
                            lambda c: c.label or "(unlabeled contact)"),
            self._dep_group("notification preference", prefs,
                            lambda p: p.channel.value),
            self._dep_group("device token", devices,
                            lambda d: d.label or d.provider),
            self._dep_group("roster membership", memberships,
                            self._membership_label)])
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
        # #318 review — row-lock the Rink first, exactly like delete_program
        # above (delete_season already holds its row lock), closing the
        # set-policy-vs-delete orphan window.
        rink = self.store.get_rink_for_update(rink_id)
        if rink is None:
            raise NotFoundError(f"Rink {rink_id} not found.")
        self._block_if_dependents("rink", rink_id, "rink",
                                  self._rink_dependent_groups(rink_id))
        self._cascade_scheduling_policy(PolicyScopeType.RINK, rink_id, actor_id)
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
