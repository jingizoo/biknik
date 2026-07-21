"""Translate database driver exceptions into stable domain errors (#201 Slice 2).

A single boundary — ``SqlStore.transaction()`` — calls
:func:`translate_db_exception` *after* the failed transaction has rolled back
(so there is zero partial state). Known integrity and concurrency failures
become stable :class:`DomainError`s carrying a machine-readable ``reason``; the
message is generic and NEVER contains SQL, table/column/constraint names,
driver text, credentials, or connection details. Anything we don't recognize
returns ``None`` so the caller re-raises it unchanged — an unclassified
database error must surface as an internal error, not be misclassified as a
user/validation error.

Detection is driver-agnostic:
- PostgreSQL/psycopg carry an authoritative five-character ``SQLSTATE`` on the
  exception (read via the duck-typed ``.sqlstate`` attribute, so psycopg need
  not be importable here);
- SQLite has no SQLSTATE, so the integrity subtype is taken from the stable
  leading phrase of its message — we read the phrase only, never echo it.
"""

import sqlite3
from typing import Optional

from ..domain.errors import (
    ConcurrencyConflictError,
    DomainError,
    IntegrityConflictError,
    ScheduleConflictError,
)

# PostgreSQL SQLSTATEs: class 23 = integrity constraint violation.
_PG_INTEGRITY = {
    "23505": "unique_violation",
    "23503": "foreign_key_violation",
    "23514": "check_violation",
    "23502": "not_null_violation",
}
# class 40 = transaction rollback (serialization/deadlock); 55P03 = lock timeout.
_PG_CONCURRENCY = {
    "40001": "serialization_failure",
    "40P01": "deadlock_detected",
    "55P03": "lock_not_available",
}

# SQLite integrity subtypes, keyed off the stable leading phrase of the message.
_SQLITE_INTEGRITY = (
    ("UNIQUE constraint failed", "unique_violation"),
    ("PRIMARY KEY constraint failed", "unique_violation"),
    ("FOREIGN KEY constraint failed", "foreign_key_violation"),
    ("CHECK constraint failed", "check_violation"),
    ("NOT NULL constraint failed", "not_null_violation"),
)

_CONFLICT_MESSAGES = {
    "unique_violation": "The change conflicts with an existing record.",
    "foreign_key_violation": "The change references a record that does not exist.",
    "check_violation": "The change violates a data constraint.",
    "not_null_violation": "A required value is missing.",
}
_CONCURRENCY_MESSAGE = (
    "The operation could not complete because of concurrent activity. "
    "Please retry.")

_CONCURRENCY_REASONS = frozenset(_PG_CONCURRENCY.values())
_ACTIVE_TEAM_JERSEY_CONSTRAINT = "ux_players_active_team_jersey"
_ACTIVE_ICE_SLOT_CONSTRAINT = "ux_games_active_ice_slot"


def translate_player_jersey_exception(
        exc: BaseException, team_id: str, jersey_number) -> Optional[DomainError]:
    """Translate migration 038's unique violation with domain-safe context.

    The generic transaction boundary intentionally hides constraint/table names,
    but the Player store methods know the attempted Team and jersey. Detect the
    one specific driver constraint internally and return the same stable conflict
    as the service pre-check, without exposing driver text or SQL metadata.
    """
    if isinstance(exc, DomainError):
        return None
    if not _is_active_team_jersey_violation(exc):
        return None
    return IntegrityConflictError(
        f"Jersey number {jersey_number} is already worn by an active player "
        "on this team.",
        details={"reason": "duplicate_jersey_number",
                 "team_id": team_id, "jersey_number": jersey_number})


def translate_reassignment_fk_exception(
        exc: BaseException, *, constraint: str, reason: str,
        message: str, **context) -> Optional[DomainError]:
    """Translate migration 040's reassignment FK violation with domain context.

    players.team_id → teams(id) and teams.club_id → clubs(id) are the concurrency
    backstops for the reassignment races (#201 Slice 2): the service already
    validates the destination parent, so a violation here means a race-losing
    writer tried to land a row whose parent was concurrently deleted. The generic
    transaction boundary would render this as the stable but shape-less
    ``foreign_key_violation`` conflict; the Player/Team store methods know exactly
    which parent was missing, so they surface a precise, stable reason
    (``team_not_found`` / ``club_not_found``) with the offending parent id — the
    same secret-free shape the service pre-check raises on the non-race path,
    never exposing driver text, SQL, or the constraint name.
    """
    if isinstance(exc, DomainError):
        return None
    if not _is_named_fk_violation(exc, constraint):
        return None
    return IntegrityConflictError(
        message, details={"reason": reason, **context})


def translate_venue_hierarchy_fk_exception(
        exc: BaseException, *, constraint: str, reason: str,
        message: str, **context) -> Optional[DomainError]:
    """Translate migration 041's facility-hierarchy FK violation with domain
    context (#201 Slice 3).

    rinks.venue_id → venues(id), ice_slots.rink_id → rinks(id),
    games.ice_slot_id → ice_slots(id) and season_venue_access.venue_id →
    venues(id) are the concurrency backstops for the "no row lock" races
    (create_rink/create_ice_slot/create_game/grant_season_venue_access racing a
    parent delete): the service already validates the destination parent, so a
    violation here means a race-losing writer tried to land a row whose parent was
    concurrently deleted. The Rink/IceSlot/Game/SeasonVenueAccess write sites each
    carry exactly one foreign key, so the missing parent is unambiguous, and they
    surface a precise, stable reason (venue_not_found / rink_not_found /
    ice_slot_not_found) with the offending parent id — the same secret-free shape
    the service pre-check raises on the non-race path, never exposing driver text,
    SQL, or the constraint name.
    """
    if isinstance(exc, DomainError):
        return None
    if not _is_named_fk_violation(exc, constraint):
        return None
    return IntegrityConflictError(
        message, details={"reason": reason, **context})


def translate_ice_slot_conflict_exception(
        exc: BaseException, ice_slot_id: str) -> Optional[DomainError]:
    """Translate migration 022's one-active-game-per-ice-slot violation (#201
    Slice 3).

    ux_games_active_ice_slot is the DB backstop that stops two active games from
    booking the same ice slot even when they run in different Seasons (whose
    Season row locks don't serialize a shared slot). A race-losing create surfaces
    the same stable ScheduleConflictError the service pre-check raises
    (game_using_ice_slot), with reason ``ice_slot_taken`` and the slot id — never a
    raw driver error, SQL, or constraint name.
    """
    if isinstance(exc, DomainError):
        return None
    if not _is_active_ice_slot_violation(exc):
        return None
    return ScheduleConflictError(
        f"Ice slot {ice_slot_id} is already used by another game.",
        details={"reason": "ice_slot_taken", "ice_slot_id": ice_slot_id})


class DependentDeleteConflict(Exception):
    """Internal signal — a parent delete was rejected by an INCOMING foreign key
    because a concurrent create committed a dependent in the pre-check→delete
    window (#201 Slice 3).

    The facility-hierarchy deletes (delete_venue / delete_rink / delete_ice_slot)
    take no row lock — the reviewer's FK-only direction — so in the
    child-commits-first ordering the delete blocks on the child's foreign-key
    key-share lock and, once the child commits, fails because the row is now
    referenced. This signal is NEVER surfaced to callers: the service catches it,
    re-resolves the now-committed dependents on a fresh read, and raises the SAME
    itemised ``HasDependenciesError`` (dependency groups + counts + ids) its
    pre-check raises — so the operator sees an identical, actionable error whether
    the dependent was present up front or committed during the race.
    """

    def __init__(self, entity_type: str, entity_id: str):
        super().__init__(f"{entity_type} {entity_id} has dependents")
        self.entity_type = entity_type
        self.entity_id = entity_id


def dependent_delete_conflict(
        exc: BaseException, *, entity_type: str,
        entity_id: str) -> Optional["DependentDeleteConflict"]:
    """Return a :class:`DependentDeleteConflict` if ``exc`` is an incoming-FK
    violation on a parent delete, else ``None`` (the caller re-raises unchanged).

    At the delete store method the only possible foreign-key violation is an
    incoming reference (the row being deleted is a parent), so any FK violation
    here means dependents exist — the store signals that, and the service turns it
    into the itemised has-dependencies block. Never a raw driver error or cascade.
    """
    if isinstance(exc, DomainError):
        return None
    if not _is_any_fk_violation(exc):
        return None
    return DependentDeleteConflict(entity_type, entity_id)


def _is_any_fk_violation(exc: BaseException) -> bool:
    """Any foreign-key violation, without needing the constraint name.

    PostgreSQL/psycopg carry sqlstate 23503; SQLite reports the fixed phrase
    ``FOREIGN KEY constraint failed``. Used at a delete site where the only FK a
    violation can name is one that points AT the row being deleted.
    """
    if getattr(exc, "sqlstate", None) == "23503":
        return True
    if isinstance(exc, sqlite3.IntegrityError):
        return "FOREIGN KEY constraint failed" in str(exc)
    return False


def _is_active_ice_slot_violation(exc: BaseException) -> bool:
    """A unique violation of ``ux_games_active_ice_slot`` specifically.

    PostgreSQL/psycopg carry the authoritative constraint name on ``.diag``
    (a 23505 for a different unique index is not matched). SQLite names the
    index's column on its ``UNIQUE constraint failed: games.ice_slot_id``
    message — distinct from the games primary key (``games.id``).
    """
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate == "23505":
        diag = getattr(exc, "diag", None)
        return (getattr(diag, "constraint_name", None)
                == _ACTIVE_ICE_SLOT_CONSTRAINT)
    if isinstance(exc, sqlite3.IntegrityError):
        text = str(exc)
        return ("UNIQUE constraint failed" in text
                and "games.ice_slot_id" in text)
    return False


def _is_named_fk_violation(exc: BaseException, constraint: str) -> bool:
    """A foreign-key violation for ``constraint``.

    PostgreSQL/psycopg carry the authoritative constraint name on ``.diag`` (a
    23503 for a different FK is not matched, so it falls through to the generic
    boundary). SQLite's ``FOREIGN KEY constraint failed`` names no column or
    constraint, so the caller's write site — which has exactly one foreign key —
    disambiguates it.
    """
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate == "23503":
        diag = getattr(exc, "diag", None)
        return getattr(diag, "constraint_name", None) == constraint
    if isinstance(exc, sqlite3.IntegrityError):
        return "FOREIGN KEY constraint failed" in str(exc)
    return False


def _is_active_team_jersey_violation(exc: BaseException) -> bool:
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate == "23505":
        diag = getattr(exc, "diag", None)
        return (getattr(diag, "constraint_name", None)
                == _ACTIVE_TEAM_JERSEY_CONSTRAINT)
    if isinstance(exc, sqlite3.IntegrityError):
        text = str(exc)
        return ("UNIQUE constraint failed" in text
                and "players.team_id" in text
                and "players.jersey_number" in text)
    return False


def translate_db_exception(exc: BaseException) -> Optional[DomainError]:
    """Return a stable DomainError for a recognized DB failure, else ``None``."""
    reason = _classify(exc)
    if reason is None:
        return None
    if reason in _CONCURRENCY_REASONS:
        return ConcurrencyConflictError(
            _CONCURRENCY_MESSAGE,
            details={"reason": reason, "retryable": True})
    return IntegrityConflictError(
        _CONFLICT_MESSAGES.get(reason, "The change conflicts with existing data."),
        details={"reason": reason})


def _classify(exc: BaseException) -> Optional[str]:
    # A domain error raised inside the transaction is not a driver failure —
    # let it pass through untouched.
    if isinstance(exc, DomainError):
        return None
    # PostgreSQL / psycopg: authoritative SQLSTATE, never message parsing.
    sqlstate = getattr(exc, "sqlstate", None)
    if isinstance(sqlstate, str):
        if sqlstate in _PG_INTEGRITY:
            return _PG_INTEGRITY[sqlstate]
        if sqlstate in _PG_CONCURRENCY:
            return _PG_CONCURRENCY[sqlstate]
        return None  # a database error we deliberately don't classify
    # SQLite: subtype from the stable leading phrase of the message.
    if isinstance(exc, sqlite3.IntegrityError):
        text = str(exc)
        for phrase, reason in _SQLITE_INTEGRITY:
            if phrase in text:
                return reason
        return None
    if isinstance(exc, sqlite3.OperationalError):
        # "database is locked" / "database table is locked" — SQLite's contended
        # write failure, the closest analogue to a Postgres lock/serialization.
        if "locked" in str(exc).lower():
            return "lock_not_available"
        return None
    return None
