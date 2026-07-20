"""Shooting-hand rule (#268).

A single source of truth for the Player ``shoots`` contract, shared by the
service layer on create and edit so no caller can persist a non-canonical value.

* Canonical stored values are ``"L"`` / ``"R"`` (handedness), or ``None`` when
  the hand is unset — the field is always optional.
* Input is normalized case-insensitively with surrounding whitespace trimmed,
  so ``"l"``, ``" R "`` are accepted and stored as ``"L"`` / ``"R"``; a blank
  or absent value normalizes to ``None`` (cleared). Anything else — ``"left"``,
  ``"both"``, ``"banana"``, or a non-string like a number/boolean — is rejected.
"""

from typing import Optional, Tuple

VALID_SHOOTS = ("L", "R")


def normalize_shoots(value) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(canonical, error_reason)`` for a shooting-hand input.

    A blank/``None`` value is a valid *unset* hand → ``(None, None)``. A string
    that trims+uppercases to ``"L"`` or ``"R"`` → ``(canonical, None)``. A
    non-string (numbers, booleans) is ``not_a_string``; any other string is
    ``not_canonical``. On any error the first element is ``None`` so a caller
    never persists a partially-accepted value.
    """
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, "not_a_string"
    trimmed = value.strip()
    if not trimmed:
        return None, None
    canonical = trimmed.upper()
    if canonical not in VALID_SHOOTS:
        return None, "not_canonical"
    return canonical, None
