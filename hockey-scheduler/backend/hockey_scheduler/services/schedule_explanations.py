"""Bounded value objects for schedule-preview explainability (#379).

This module is deliberately read-only.  It formats observations made after the
greedy scheduler has already decided that a pairing is unplaced; it never ranks
or selects ice, changes pairings, or writes to the store.

The two candidate caps are separate on purpose:

* at most 8 candidate windows are returned for one unplaced pairing; and
* at most 128 candidate windows are evaluated/returned across one preview.

That keeps one explanation useful across several rinks/dates while limiting the
extra worst-case payload to 128 small, identifier-only records.  Every pairing
still receives an explanation object, explicit omitted counts, blocking codes,
and bounded input-correction alternatives when the shared preview budget is
exhausted.
"""

import json


EXPLANATION_VALUE_OBJECT_VERSION = 1
MAX_CANDIDATE_WINDOWS_PER_PAIRING = 8
MAX_CANDIDATE_WINDOWS_PER_PREVIEW = 128
MAX_REJECTIONS_PER_CANDIDATE = 8
MAX_ALTERNATIVES_PER_PAIRING = 3


# Scope/eligibility failures precede inventory and per-window constraints;
# window-level codes then follow the scheduler's established evaluation order.
# Unknown future codes sort lexically after this list, preserving deterministic
# output without requiring a contract-breaking formatter change.
REASON_CODE_ORDER = (
    "venue_access_missing",
    "team_not_registered",
    "team_league_mismatch",
    "no_ice_available",
    "ice_already_selected",
    "season_blackout",
    "holiday",
    "team_blackout",
    "rink_blackout",
    "max_per_day",
    "min_rest",
    "insufficient_playable_time",
    "slot_overlap_conflict",
    "turnover_buffer_conflict",
    "curfew_violation",
    "team_overlap",
)
_REASON_RANK = {code: index for index, code in enumerate(REASON_CODE_ORDER)}


# Candidate evidence is an allowlist, not a pass-through of domain errors.
# In particular, free-text messages/names/notes can never enter the response.
_SAFE_DETAIL_FIELDS = {
    "season_blackout": ("date",),
    "holiday": ("date",),
    "team_blackout": ("date", "team_ids"),
    "rink_blackout": ("date", "rink_id"),
    "max_per_day": ("date", "team_ids", "limit"),
    "min_rest": (
        "team_ids", "min_rest_hours", "conflicts",
        "omitted_conflict_count",
    ),
    "insufficient_playable_time": (
        "rink_id", "slot_minutes", "required_minutes"),
    "slot_overlap_conflict": (
        "rink_id", "conflict_game_id", "conflict_slot_id"),
    "turnover_buffer_conflict": (
        "rink_id", "conflict_game_id", "conflict_slot_id",
        "required_gap_minutes", "gap_minutes",
    ),
    "curfew_violation": ("rink_id", "curfew_local", "slot_end_local"),
    "team_overlap": ("team_ids", "conflicts"),
    "ice_already_selected": (),
    "no_ice_available": (),
    "venue_access_missing": ("season_id",),
    "team_not_registered": ("season_id", "team_ids"),
    "team_league_mismatch": ("season_id", "league_id", "team_ids"),
}


def ordered_reason_codes(codes):
    """Canonical unique reason-code order, including future unknown codes."""
    return sorted(
        {str(code) for code in codes if code},
        key=lambda code: (_REASON_RANK.get(code, len(_REASON_RANK)), code),
    )


def _canonical_key(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _safe_scalar(value):
    return value if value is None or isinstance(value, (str, int, float, bool)) \
        else str(value)


def _safe_value(value):
    """Normalize the small allowlisted detail shapes into canonical JSON data."""
    if isinstance(value, dict):
        return {str(key): _safe_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple, set)):
        items = [_safe_value(item) for item in value]
        return sorted(items, key=_canonical_key)
    return _safe_scalar(value)


def _safe_details(code, details):
    allowed = _SAFE_DETAIL_FIELDS.get(code, ())
    source = details if isinstance(details, dict) else {}
    return {
        field: _safe_value(source[field])
        for field in allowed
        if field in source
    }


class ExplanationBudget:
    """One deterministic candidate-evidence budget for a whole preview."""

    def __init__(self, *, per_pairing=MAX_CANDIDATE_WINDOWS_PER_PAIRING,
                 per_preview=MAX_CANDIDATE_WINDOWS_PER_PREVIEW):
        self.per_pairing = max(0, int(per_pairing))
        self.per_preview = max(0, int(per_preview))
        self.remaining = self.per_preview

    def reserve(self, candidate_total):
        """Return ``(allowance, preview_budget_limited)`` for the next pairing."""
        candidate_total = max(0, int(candidate_total))
        per_pair_allowance = min(candidate_total, self.per_pairing)
        allowance = min(per_pair_allowance, self.remaining)
        self.remaining -= allowance
        return allowance, allowance < per_pair_allowance


def _candidate_record(raw):
    rejections = []
    raw_rejections = list(raw.get("rejections") or ())
    for rejection in raw_rejections[:MAX_REJECTIONS_PER_CANDIDATE]:
        code = str(rejection.get("code") or "")
        if not code:
            continue
        rejections.append({
            "code": code,
            "details": _safe_details(code, rejection.get("details")),
        })
    # A candidate with no hard rejection would be a playable suggestion.  This
    # formatter never emits one: an unplaced pairing's evidence is rejection
    # evidence only, never an invented alternative slot.
    if not rejections:
        return None
    record = {
        "ice_slot_id": raw.get("ice_slot_id"),
        "rink_id": raw.get("rink_id"),
        "venue_id": raw.get("venue_id"),
        "start_time": raw.get("start_time"),
        "end_time": raw.get("end_time"),
        "rejections": rejections,
    }
    omitted = max(0, len(raw_rejections) - len(rejections))
    if omitted:
        record["omitted_rejection_count"] = omitted
    return record


def _first_details_by_code(candidates):
    found = {}
    for candidate in candidates:
        for rejection in candidate["rejections"]:
            found.setdefault(rejection["code"], rejection.get("details") or {})
    return found


def _alternative_for(code, details, pairing, scope):
    """One correction derived from an observed code; never a playable claim."""
    pairing_ids = sorted({
        pairing.get("home_team_id"), pairing.get("away_team_id")
    } - {None})
    season_id = scope.get("season_id")

    if code == "venue_access_missing":
        return {
            "action_code": "review_season_venue_access",
            "reason_code": code,
            "season_id": details.get("season_id") or season_id,
        }
    if code in ("team_not_registered", "team_league_mismatch"):
        return {
            "action_code": "correct_season_team_registration",
            "reason_code": code,
            "season_id": details.get("season_id") or season_id,
            "team_ids": details.get("team_ids") or pairing_ids,
        }
    if code in ("no_ice_available", "ice_already_selected"):
        return {
            "action_code": "increase_available_game_ice",
            "reason_code": code,
            "season_id": season_id,
        }
    if code == "season_blackout":
        return {
            "action_code": "review_season_blackout",
            "reason_code": code,
            "date": details.get("date"),
            "season_id": season_id,
        }
    if code == "holiday":
        return {
            "action_code": "review_holiday_closure",
            "reason_code": code,
            "date": details.get("date"),
            "season_id": season_id,
        }
    if code == "team_blackout":
        return {
            "action_code": "review_team_blackout",
            "reason_code": code,
            "date": details.get("date"),
            "team_ids": details.get("team_ids") or pairing_ids,
        }
    if code == "rink_blackout":
        return {
            "action_code": "review_rink_blackout",
            "reason_code": code,
            "date": details.get("date"),
            "rink_id": details.get("rink_id"),
        }
    if code == "max_per_day":
        return {
            "action_code": "review_max_games_per_team_per_day",
            "reason_code": code,
            "date": details.get("date"),
            "limit": details.get("limit"),
            "team_ids": details.get("team_ids") or pairing_ids,
        }
    if code == "min_rest":
        return {
            "action_code": "review_minimum_rest_policy",
            "reason_code": code,
            "min_rest_hours": details.get("min_rest_hours"),
            "team_ids": details.get("team_ids") or pairing_ids,
        }
    if code in (
            "insufficient_playable_time", "turnover_buffer_conflict",
            "curfew_violation"):
        return {
            "action_code": "review_rink_scheduling_policy",
            "reason_code": code,
            "rink_id": details.get("rink_id"),
        }
    if code in ("slot_overlap_conflict", "team_overlap"):
        conflicts = details.get("conflicts") or ()
        game_ids = sorted({
            item.get("conflict_game_id")
            for item in conflicts
            if isinstance(item, dict) and item.get("conflict_game_id")
        })
        if details.get("conflict_game_id"):
            game_ids.append(details["conflict_game_id"])
            game_ids = sorted(set(game_ids))
        return {
            "action_code": (
                "reschedule_conflicting_game" if game_ids
                else "provide_non_overlapping_game_ice"
            ),
            "reason_code": code,
            "game_ids": game_ids,
            "team_ids": details.get("team_ids") or pairing_ids,
        }
    return {
        "action_code": "review_scheduling_input",
        "reason_code": code,
    }


def _clean_alternative(value):
    # Null/empty optional locators are omitted; no prose or name fields exist.
    return {
        key: _safe_value(item)
        for key, item in value.items()
        if item not in (None, [], ())
    }


def _alternatives(codes, candidates, pairing, scope):
    details_by_code = _first_details_by_code(candidates)
    all_alternatives = []
    seen = set()
    for code in ordered_reason_codes(codes):
        value = _clean_alternative(_alternative_for(
            code, details_by_code.get(code, {}), pairing, scope))
        key = _canonical_key(value)
        if key in seen:
            continue
        seen.add(key)
        all_alternatives.append(value)
    return (all_alternatives[:MAX_ALTERNATIVES_PER_PAIRING],
            max(0, len(all_alternatives) - MAX_ALTERNATIVES_PER_PAIRING))


def build_unplaced_explanation(*, pairing, scope, legacy_reason_codes,
                               raw_candidates, candidate_total,
                               preview_budget_limited=False):
    """Build the additive, versioned explanation object for one pairing."""
    candidates = []
    for raw in raw_candidates:
        record = _candidate_record(raw)
        if record is not None:
            candidates.append(record)

    observed_codes = list(legacy_reason_codes or ())
    for candidate in candidates:
        observed_codes.extend(r["code"] for r in candidate["rejections"])
    codes = ordered_reason_codes(observed_codes)
    alternatives, alternatives_omitted = _alternatives(
        codes, candidates, pairing, scope)
    omitted_candidates = max(0, int(candidate_total) - len(candidates))
    return {
        "value_object_version": EXPLANATION_VALUE_OBJECT_VERSION,
        "blocking_constraint_codes": codes,
        "candidate_windows": candidates,
        "alternatives": alternatives,
        "bounds": {
            "candidate_window_limit_per_pairing":
                MAX_CANDIDATE_WINDOWS_PER_PAIRING,
            "candidate_window_limit_per_preview":
                MAX_CANDIDATE_WINDOWS_PER_PREVIEW,
            "rejection_limit_per_candidate": MAX_REJECTIONS_PER_CANDIDATE,
            "alternative_limit_per_pairing": MAX_ALTERNATIVES_PER_PAIRING,
            "candidate_window_total": int(candidate_total),
            "candidate_window_count": len(candidates),
            "candidate_window_omitted_count": omitted_candidates,
            "candidate_windows_truncated": bool(omitted_candidates),
            "preview_candidate_budget_limited": bool(preview_budget_limited),
            "alternative_omitted_count": alternatives_omitted,
        },
    }


def add_scope_blocker(explanation, *, code, details, pairing, scope):
    """Add a fail-closed scope blocker without adding any candidate evidence."""
    explanation["blocking_constraint_codes"] = ordered_reason_codes(
        list(explanation.get("blocking_constraint_codes") or ()) + [code])
    alternatives, omitted = _alternatives(
        explanation["blocking_constraint_codes"],
        explanation.get("candidate_windows") or (), pairing, scope)
    # ``details`` is authoritative scope context (e.g. the selected Season),
    # not a hidden candidate.  Replace the generic alternative for this code
    # with the same formatter fed those explicit safe details.
    scoped = _clean_alternative(_alternative_for(code, details, pairing, scope))
    alternatives = [a for a in alternatives if a.get("reason_code") != code]
    alternatives.insert(0, scoped)
    unique = []
    seen = set()
    for alternative in alternatives:
        key = _canonical_key(alternative)
        if key not in seen:
            seen.add(key)
            unique.append(alternative)
    explanation["alternatives"] = unique[:MAX_ALTERNATIVES_PER_PAIRING]
    explanation["bounds"]["alternative_omitted_count"] = max(
        omitted, len(unique) - MAX_ALTERNATIVES_PER_PAIRING)
    return explanation


__all__ = [
    "EXPLANATION_VALUE_OBJECT_VERSION",
    "MAX_CANDIDATE_WINDOWS_PER_PAIRING",
    "MAX_CANDIDATE_WINDOWS_PER_PREVIEW",
    "MAX_REJECTIONS_PER_CANDIDATE",
    "MAX_ALTERNATIVES_PER_PAIRING",
    "ExplanationBudget",
    "add_scope_blocker",
    "build_unplaced_explanation",
    "ordered_reason_codes",
]
