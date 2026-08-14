"""Athlete identity field contracts (#273).

The single source of truth for the durable-identity fields Player gains in
#273 — structured names, the private birthdate, the governing-body
registration number, and the 1-7 skill rating (assigned to #273 by the owner's
ruling on #287) — shared by the service layer's create/edit validators and both
import paths, exactly like :mod:`.shooting` (#268) and :mod:`.jersey` (#269).

Every function is pure: no I/O, no clock reads. Where a rule needs "today"
(a birthdate may not be in the future), the caller passes it in explicitly,
per the domain convention that times are always injected.

Canonical storage decisions:

* Name parts (``first_name`` / ``last_name`` / ``preferred_name``) are trimmed
  and internal whitespace runs are collapsed to single spaces; case is
  preserved (``McDonald``, ``van der Berg`` are somebody's real names, not a
  formatting bug). ``preferred_name`` is optional; blank normalizes to None.
* The flattened legacy ``Player.name`` remains the DISPLAY name. When
  structured names exist it is always ``"<first> <last>"`` — derived by
  :func:`derive_display_name`, never free-typed — so every legacy consumer
  (rosters, notifications, exports) keeps working unchanged.
* ``birthdate`` is stored as a canonical ISO calendar date string
  (``YYYY-MM-DD``). A birthdate is a calendar fact, not an instant, so it
  carries no timezone; storing the validated string keeps every layer
  JSON-safe and matches the store's dates-as-ISO-text convention.
* ``registration_number`` is the governing body's stable athlete id. Trimmed,
  case preserved, no internal whitespace, bounded length. Optional.
* ``skill_rating`` is an integer 1-7 or None (unrated). Unrated is a fully
  supported state — #287's ranking must degrade, never exclude.

All functions return ``(canonical, reason)``; ``reason`` is None on success
and a stable machine-readable token on failure (the first element is then
None, so a caller can never persist a partially-accepted value).
"""

import re
from datetime import date
from typing import Optional, Tuple

# Bounds chosen to be generous for real-world data while still refusing
# obviously-corrupt cells (a pasted paragraph, a whole CSV row in one field).
MAX_NAME_PART_LENGTH = 80
MAX_REGISTRATION_NUMBER_LENGTH = 64
MIN_SKILL_RATING = 1
MAX_SKILL_RATING = 7

# A birthdate before this is corrupt data, not a young-at-heart veteran.
MIN_BIRTHDATE = date(1900, 1, 1)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _collapse_ws(value: str) -> str:
    return " ".join(value.split())


def normalize_name_part(value) -> Tuple[Optional[str], Optional[str]]:
    """Canonicalize a REQUIRED name part (``first_name`` / ``last_name``).

    Non-string → ``not_a_string``; blank/whitespace → ``blank``; longer than
    :data:`MAX_NAME_PART_LENGTH` after collapsing → ``too_long``.
    """
    if not isinstance(value, str):
        return None, "not_a_string"
    canonical = _collapse_ws(value)
    if not canonical:
        return None, "blank"
    if len(canonical) > MAX_NAME_PART_LENGTH:
        return None, "too_long"
    return canonical, None


def normalize_preferred_name(value) -> Tuple[Optional[str], Optional[str]]:
    """Canonicalize the OPTIONAL preferred/display given name.

    None and blank are a valid *unset* → ``(None, None)``; otherwise the same
    contract as :func:`normalize_name_part`.
    """
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, "not_a_string"
    canonical = _collapse_ws(value)
    if not canonical:
        return None, None
    if len(canonical) > MAX_NAME_PART_LENGTH:
        return None, "too_long"
    return canonical, None


def derive_display_name(first_name: str, last_name: str) -> str:
    """The flattened legacy ``Player.name`` for a structured-name athlete."""
    return f"{first_name} {last_name}"


def normalize_birthdate(value, today: Optional[date] = None
                        ) -> Tuple[Optional[str], Optional[str]]:
    """Canonicalize an optional birthdate to a strict ``YYYY-MM-DD`` string.

    Accepts None/blank (unset → ``(None, None)``), a :class:`datetime.date`
    (NOT a datetime — a birthdate is a calendar date, and accepting a datetime
    would silently invent a timezone question), or a strict ISO ``YYYY-MM-DD``
    string. The string must be a REAL calendar date (``2025-02-30`` is
    ``not_a_date``). Bounds: not before :data:`MIN_BIRTHDATE`
    (``too_old``); when the caller supplies ``today``, not after it
    (``in_future``) — ``today`` is always passed in, never read from a clock
    here, so the rule stays deterministic and testable.
    """
    if value is None:
        return None, None
    if isinstance(value, date) and type(value) is date:
        parsed = value
    elif isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return None, None
        if not _ISO_DATE.match(trimmed):
            return None, "not_iso_format"
        try:
            parsed = date.fromisoformat(trimmed)
        except ValueError:
            return None, "not_a_date"
    else:
        return None, "not_a_date_or_string"
    if parsed < MIN_BIRTHDATE:
        return None, "too_old"
    if today is not None and parsed > today:
        return None, "in_future"
    return parsed.isoformat(), None


def normalize_registration_number(value) -> Tuple[Optional[str], Optional[str]]:
    """Canonicalize an optional governing-body registration number.

    None/blank → unset. Trimmed, case preserved (registries differ on case
    significance, so we never fold), no internal whitespace
    (``has_whitespace``), at most :data:`MAX_REGISTRATION_NUMBER_LENGTH`
    characters (``too_long``). Non-string → ``not_a_string``.
    """
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, "not_a_string"
    trimmed = value.strip()
    if not trimmed:
        return None, None
    if any(ch.isspace() for ch in trimmed):
        return None, "has_whitespace"
    if len(trimmed) > MAX_REGISTRATION_NUMBER_LENGTH:
        return None, "too_long"
    return trimmed, None


def normalize_skill_rating(value) -> Tuple[Optional[int], Optional[str]]:
    """Canonicalize an optional 1-7 skill rating (#287 owner ruling → #273).

    None/blank → unset (unrated). An ``int`` (bool is rejected — it is an int
    subclass, and ``True`` silently becoming rating 1 would be a data bug) or
    a string of digits in range → the int. Out-of-range → ``out_of_range``;
    anything else → ``not_an_integer``.
    """
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "not_an_integer"
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return None, None
        if not trimmed.isdigit():
            return None, "not_an_integer"
        value = int(trimmed)
    if not isinstance(value, int):
        return None, "not_an_integer"
    if not MIN_SKILL_RATING <= value <= MAX_SKILL_RATING:
        return None, "out_of_range"
    return value, None


def normalized_name_key(name) -> Optional[str]:
    """The case/whitespace-insensitive key duplicate DETECTION groups by.

    Used only to find *candidate* duplicates to WARN about — per #273 (and the
    epic #205 ruling), records are never matched, merged, or written by name
    alone. None for a value that has no usable name.
    """
    if not isinstance(name, str):
        return None
    collapsed = _collapse_ws(name)
    return collapsed.casefold() if collapsed else None
