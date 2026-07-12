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
