"""Structured age tiers + versioned season-cutoff eligibility (#273).

Replaces the free-text ``Division.age_group`` *convention* (``"U10"``,
``"U13"``, …) with a structured, versioned rule an operator sets per
League-in-Season, and a pure evaluator that answers the acceptance question:
*is athlete X age-eligible for this Season/League/Division under cutoff-rule
version Z?*

Model
-----
* A rule belongs to one **LeagueSeason** (the Season overlay on a permanent
  League — the same scope Divisions and team registrations hang off, and the
  scope the owner's versioned-policy ruling on epic #205 uses for deadlines).
* A rule is a **cutoff** (month + day, applied in the Season's start year)
  plus an ordered list of **tiers** ``{"code": "U10", "max_age": 10}``. An
  athlete is eligible for a tier iff their age on the cutoff date is
  strictly under ``max_age``; ``max_age: None`` is an open tier (Senior).
* Rules are **versioned and immutable**: changing the rule appends version
  N+1; evaluation always names the version it used, so a past answer can be
  reproduced even after the rule changes.
* A Division declares its tier by its existing ``age_group`` text, matched
  case-insensitively against the rule's tier codes — no Division schema
  change in this slice, and an unmatched/blank declaration is an honest
  *indeterminate*, never a guess.
* Enforcement is **warn-first** (#273): a rule carries an ``enforcement``
  mode — ``"warn"`` (default; surface the answer, block nothing) or
  ``"block"`` (a consumer MAY hard-refuse). This module only reports the
  mode; wiring hard blocks into registration/roster flows is explicitly
  later work behind that policy switch.

Everything here is pure data-in/data-out: no store, no clock. The evaluator
returns a plain dict (JSON-safe) with ``status`` ∈ ``eligible`` /
``ineligible`` / ``indeterminate`` and a machine-readable ``reason`` token
whenever the status is not a definite yes.
"""

from datetime import date
from typing import List, Optional, Tuple

from .identity import normalize_birthdate

ENFORCEMENT_MODES = ("warn", "block")

MAX_TIER_CODE_LENGTH = 16
MIN_TIER_AGE = 1
MAX_TIER_AGE = 99

# A cutoff must exist in EVERY year, so February is capped at 28 (a Feb-29
# cutoff would silently drift to March or explode in non-leap years).
_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def normalize_cutoff(month, day) -> Tuple[Optional[Tuple[int, int]],
                                          Optional[str]]:
    """Canonicalize a season cutoff (month, day) → ``((m, d), None)`` or
    ``(None, reason)``. Bool inputs are rejected like every other non-int."""
    for value in (month, day):
        if isinstance(value, bool) or not isinstance(value, int):
            return None, "not_an_integer"
    if not 1 <= month <= 12:
        return None, "month_out_of_range"
    if not 1 <= day <= _DAYS_IN_MONTH[month - 1]:
        return None, "day_out_of_range"
    return (month, day), None


def normalize_age_tiers(tiers) -> Tuple[Optional[List[dict]], Optional[str]]:
    """Canonicalize a tier list → ``([{"code", "max_age"}, ...], None)``.

    Codes are trimmed + upper-cased and must be unique, non-blank, and at most
    :data:`MAX_TIER_CODE_LENGTH` chars. ``max_age`` is an int in
    [:data:`MIN_TIER_AGE`, :data:`MAX_TIER_AGE`] or an EXPLICIT ``None`` (an
    intentional open tier — Senior, no upper bound). Order is preserved — it
    is the operator's display order, not a semantic ranking.

    #273 review round 3 finding 2: the ``max_age`` KEY must be present on
    every tier. ``{"code": "U10"}`` (the key entirely omitted) is
    ``tier_max_age_missing``, a DIFFERENT, harder error than
    ``{"code": "U10", "max_age": None}`` (the key present, explicitly null) —
    the latter is accepted exactly as before and is the only way to declare
    an open tier. Missing data must never silently weaken an age policy: a
    caller who reads ``tier.get("max_age")`` cannot tell "the operator typed
    null on purpose" apart from "the max_age column/key was never supplied
    at all" — those two inputs must not collapse into the same accepted
    open-tier outcome, since an omitted bound was never operator intent.
    """
    if not isinstance(tiers, (list, tuple)) or not tiers:
        return None, "tiers_empty"
    canonical = []
    seen = set()
    for tier in tiers:
        if not isinstance(tier, dict):
            return None, "tier_not_a_mapping"
        unknown = set(tier) - {"code", "max_age"}
        if unknown:
            return None, "tier_unknown_key"
        code = tier.get("code")
        if not isinstance(code, str) or not code.strip():
            return None, "tier_code_invalid"
        code = code.strip().upper()
        if len(code) > MAX_TIER_CODE_LENGTH:
            return None, "tier_code_invalid"
        if code in seen:
            return None, "tier_code_duplicate"
        seen.add(code)
        if "max_age" not in tier:
            return None, "tier_max_age_missing"
        max_age = tier["max_age"]
        if max_age is not None:
            if isinstance(max_age, bool) or not isinstance(max_age, int):
                return None, "tier_max_age_invalid"
            if not MIN_TIER_AGE <= max_age <= MAX_TIER_AGE:
                return None, "tier_max_age_invalid"
        canonical.append({"code": code, "max_age": max_age})
    return canonical, None


def normalize_enforcement(value) -> Tuple[Optional[str], Optional[str]]:
    """``"warn"`` / ``"block"`` (default warn for None/blank)."""
    if value is None:
        return "warn", None
    if not isinstance(value, str):
        return None, "invalid_enforcement"
    trimmed = value.strip().lower()
    if not trimmed:
        return "warn", None
    if trimmed not in ENFORCEMENT_MODES:
        return None, "invalid_enforcement"
    return trimmed, None


def match_tier(tiers: List[dict], declared) -> Optional[dict]:
    """The rule tier a Division's ``age_group`` text declares, or None.

    Case-insensitive, whitespace-trimmed exact match on the tier code — a
    fuzzy match here would quietly decide a child's eligibility from a typo.
    """
    if not isinstance(declared, str):
        return None
    wanted = declared.strip().upper()
    if not wanted:
        return None
    for tier in tiers:
        if tier.get("code") == wanted:
            return tier
    return None


def age_on(birthdate: date, on: date) -> int:
    """Completed years of age on ``on`` (birthday-not-yet-reached aware).

    A Feb-29 birthdate ticks over on Mar 1 in non-leap years — the
    ``(month, day)`` tuple comparison gives exactly that without special
    cases.
    """
    years = on.year - birthdate.year
    if (on.month, on.day) < (birthdate.month, birthdate.day):
        years -= 1
    return years


def evaluate_age_eligibility(*, birthdate, tier_declared,
                             cutoff_month: int, cutoff_day: int,
                             season_start, tiers: List[dict]) -> dict:
    """Answer *"is this athlete age-eligible for this tier under this rule?"*

    Pure: every input is passed in. ``birthdate`` is the athlete's canonical
    ISO string (or None — legacy athletes may not have one yet, and the
    answer is then an honest ``indeterminate``/``no_birthdate``, never a
    guess). ``tier_declared`` is the Division's ``age_group`` text.
    ``season_start`` is the Season's start date/datetime (its YEAR anchors
    the cutoff) or None. ``tiers`` is the rule's canonical tier list.

    Returns a JSON-safe dict::

        {"status": "eligible" | "ineligible" | "indeterminate",
         "reason": None | "no_birthdate" | "no_season_start" | "no_tier_declared"
                  | "unknown_tier" | "invalid_birthdate" | "invalid_cutoff"
                  | "not_born_at_cutoff" | "over_age",
         "tier_code": str | None,  "max_age": int | None,
         "age_at_cutoff": int | None,  "cutoff_date": "YYYY-MM-DD" | None}

    The dict never contains the birthdate itself — callers can hand the
    result to a Coach-facing summary without re-filtering (#124 boundary).
    """
    result = {"status": "indeterminate", "reason": None, "tier_code": None,
              "max_age": None, "age_at_cutoff": None, "cutoff_date": None}

    tier = match_tier(tiers, tier_declared)
    if tier is None:
        blank = not isinstance(tier_declared, str) or not tier_declared.strip()
        result["reason"] = "no_tier_declared" if blank else "unknown_tier"
        return result
    result["tier_code"] = tier["code"]
    result["max_age"] = tier["max_age"]

    if season_start is None:
        result["reason"] = "no_season_start"
        return result
    try:
        cutoff = date(season_start.year, cutoff_month, cutoff_day)
    except (ValueError, TypeError, AttributeError):
        result["reason"] = "invalid_cutoff"
        return result
    result["cutoff_date"] = cutoff.isoformat()

    # #273 review round 2 finding 4: a birthdate strictly AFTER the cutoff
    # means the athlete is not yet born as of that date. This is checked
    # BEFORE the open-tier shortcut below (an open tier used to report
    # "eligible" without ever looking at birthdate at all — even earlier
    # than the bounded-tier bug) and independent of ``tier["max_age"]``, so
    # both bounded and open tiers are covered by one check. Previously a
    # bounded tier fell through to ``age_on()``, which naively subtracts
    # calendar years with no floor — cutoff 2010-12-31 / birthdate
    # 2015-01-01 produced ``age_at_cutoff=-5`` and, since -5 is less than
    # any positive ``max_age``, ``status="eligible"``. A birthdate must
    # never produce a negative displayed age or a positive eligibility
    # answer for a cutoff date before the athlete existed; this is a
    # distinct, stable outcome, not folded into ``over_age`` (a birth-order
    # data/config problem reads very differently from an athlete who is
    # simply too old for the tier). ``age_at_cutoff`` stays None here
    # (never negative) — there is no meaningful age to report yet.
    canonical_birthdate = None
    if birthdate is not None:
        canonical_birthdate, reason = normalize_birthdate(birthdate)
        if reason is not None or canonical_birthdate is None:
            result["reason"] = "invalid_birthdate"
            return result
        if date.fromisoformat(canonical_birthdate) > cutoff:
            result["status"] = "ineligible"
            result["reason"] = "not_born_at_cutoff"
            return result

    if tier["max_age"] is None:
        # Open tier: eligible whatever the age (birth-before-cutoff already
        # confirmed above when a birthdate was supplied) — even with no
        # birthdate on file, since no age bound exists to check against.
        result["status"] = "eligible"
        return result

    if canonical_birthdate is None:
        result["reason"] = "no_birthdate"
        return result

    age = age_on(date.fromisoformat(canonical_birthdate), cutoff)
    result["age_at_cutoff"] = age
    if age < tier["max_age"]:
        result["status"] = "eligible"
    else:
        result["status"] = "ineligible"
        result["reason"] = "over_age"
    return result
