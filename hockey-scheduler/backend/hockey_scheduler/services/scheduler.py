"""Season scheduler engine v1 — draft fixture generation (#84).

A deliberately simple, deterministic generator: single round-robin pairings for
a division's teams, each pairing assigned to the earliest available game ice
slot. It produces a *draft* proposal only — nothing is persisted or published
here; review and publish are a later slice (#86). Pure over the store so the
output is reproducible and unit-testable.
"""

from datetime import date, timedelta

from ..domain import IceSlotStatus, IceSlotType
from ..domain.errors import ValidationError


def round_robin_pairings(team_ids):
    """Single round-robin pairings via the circle method (deterministic).

    Returns a flat list of ``(home_team_id, away_team_id)`` in round order.
    Every team plays every other exactly once. An odd number of teams yields a
    bye each round (the byed team simply has no game that round). Home/away
    alternates by round for basic balance.
    """
    teams = sorted(team_ids)
    if len(teams) < 2:
        return []
    if len(teams) % 2 == 1:
        teams = teams + [None]  # None marks the bye
    n = len(teams)
    fixed, rot = teams[0], teams[1:]
    pairings = []
    for r in range(n - 1):
        arrangement = [fixed] + rot
        for i in range(n // 2):
            a, b = arrangement[i], arrangement[n - 1 - i]
            if a is None or b is None:
                continue  # bye
            pairings.append((a, b) if r % 2 == 0 else (b, a))
        rot = [rot[-1]] + rot[:-1]  # rotate all but the fixed team
    return pairings


def _available_game_slots(store, slot_ids=None):
    """Game-type ice slots that are AVAILABLE and not already tied to a game,
    earliest first (deterministic tie-break on id)."""
    wanted = set(slot_ids) if slot_ids else None
    slots = []
    for s in store.all_ice_slots():
        if wanted is not None and s.id not in wanted:
            continue
        if s.slot_type != IceSlotType.GAME or s.status != IceSlotStatus.AVAILABLE:
            continue
        if store.game_using_ice_slot(s.id) is not None:
            continue
        slots.append(s)
    slots.sort(key=lambda s: (s.start_time, s.id))
    return slots


def _validate_day(value, field):
    """A blackout entry must be a strict ``YYYY-MM-DD`` string (#85).

    The scheduler compares against ``slot.start_time.date().isoformat()``, so a
    loosely-formatted date (``2026/01/05``, ``Jan 5``, ``2026-1-5``) would never
    match and the blackout would be silently ignored — worse than rejecting it.
    The round-trip check pins the exact canonical format."""
    if not isinstance(value, str):
        raise ValidationError(
            f"'{field}' entries must be YYYY-MM-DD date strings.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValidationError(
            f"'{field}' entries must be YYYY-MM-DD date strings.")
    if parsed.isoformat() != value:
        raise ValidationError(
            f"'{field}' entries must be YYYY-MM-DD date strings.")
    return value


def _blackout_map(value, field):
    """Validate a ``{id: [YYYY-MM-DD, …]}`` blackout map into ``{id: set}``.

    Client input, so every bad shape raises a structured ``ValidationError``
    (never a raw AttributeError/TypeError across the facade boundary, #85)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError(
            f"'{field}' must be an object of id to a list of "
            f"YYYY-MM-DD date strings.")
    out = {}
    for key, days in value.items():
        if days is None:
            out[str(key)] = set()
            continue
        if not isinstance(days, (list, tuple)):
            raise ValidationError(
                f"'{field}[{key}]' must be a list of YYYY-MM-DD date strings.")
        out[str(key)] = {_validate_day(d, f"{field}[{key}]") for d in days}
    return out


def _normalize_constraints(constraints):
    """Validate and coerce the request constraints into a predictable shape (#85).

    Recognized keys (all optional): ``team_blackouts`` {team_id: [YYYY-MM-DD]},
    ``rink_blackouts`` {rink_id: [YYYY-MM-DD]}, ``min_rest_hours`` (number ≥ 0),
    ``max_games_per_team_per_day`` (int ≥ 0). Malformed input raises a
    ``ValidationError`` so the facade returns a structured error rather than
    letting a raw exception cross the boundary.
    """
    if constraints is None:
        constraints = {}
    if not isinstance(constraints, dict):
        raise ValidationError("'constraints' must be an object or null.")
    c = constraints

    min_rest = c.get("min_rest_hours")
    min_rest = 0 if min_rest is None else min_rest
    # bool is an int subclass — reject it explicitly so True/False can't sneak
    # through as 1/0.
    if isinstance(min_rest, bool) or not isinstance(min_rest, (int, float)) \
            or min_rest < 0:
        raise ValidationError("'min_rest_hours' must be a number >= 0.")

    max_per_day = c.get("max_games_per_team_per_day")
    max_per_day = 0 if max_per_day is None else max_per_day
    if isinstance(max_per_day, bool) or not isinstance(max_per_day, int) \
            or max_per_day < 0:
        raise ValidationError(
            "'max_games_per_team_per_day' must be an integer >= 0.")

    return {
        "team_blackouts": _blackout_map(c.get("team_blackouts"), "team_blackouts"),
        "rink_blackouts": _blackout_map(c.get("rink_blackouts"), "rink_blackouts"),
        "min_rest_hours": float(min_rest),
        "max_per_day": int(max_per_day),
    }


def _slot_reason(slot, home, away, con, team_slots):
    """Why ``slot`` can't host ``home`` vs ``away`` under the constraints, or
    None if it can. ``team_slots`` maps team_id → [assigned start_time]."""
    day = slot.start_time.date().isoformat()
    if day in con["team_blackouts"].get(home, ()) \
            or day in con["team_blackouts"].get(away, ()):
        return "team blackout date"
    if day in con["rink_blackouts"].get(slot.rink_id, ()):
        return "rink blackout date"
    if con["max_per_day"] > 0:
        for tid in (home, away):
            same_day = sum(1 for t in team_slots.get(tid, [])
                           if t.date() == slot.start_time.date())
            if same_day >= con["max_per_day"]:
                return "max games per team per day reached"
    if con["min_rest_hours"] > 0:
        rest = timedelta(hours=con["min_rest_hours"])
        for tid in (home, away):
            for t in team_slots.get(tid, []):
                if abs(slot.start_time - t) < rest:
                    return "minimum rest between games not met"
    return None


def draft_schedule(store, division_id, slot_ids=None, constraints=None):
    """Generate a draft round-robin schedule for a division (#84/#85).

    Returns ``{division_id, draft_games, unscheduled}``. Each pairing takes the
    earliest available slot that satisfies the optional constraints (#85: team
    and rink blackout dates, minimum rest between a team's games, and a max
    games-per-team-per-day cap); a pairing with no valid slot is returned in
    ``unscheduled`` with the reason(s) that blocked it. Nothing is persisted.
    """
    teams = sorted(t.id for t in store.all_teams()
                   if t.division_id == division_id)
    pairings = round_robin_pairings(teams)
    slots = _available_game_slots(store, slot_ids)
    con = _normalize_constraints(constraints)

    def team_name(tid):
        t = store.get_team(tid)
        return t.name if t else tid

    draft_games, unscheduled = [], []
    used = set()
    team_slots = {}  # team_id -> [assigned start_time]
    for home, away in pairings:
        chosen, reasons = None, []
        for s in slots:
            if s.id in used:
                continue
            reason = _slot_reason(s, home, away, con, team_slots)
            if reason is None:
                chosen = s
                break
            reasons.append(reason)
        if chosen is not None:
            used.add(chosen.id)
            team_slots.setdefault(home, []).append(chosen.start_time)
            team_slots.setdefault(away, []).append(chosen.start_time)
            rink = store.get_rink(chosen.rink_id) if chosen.rink_id else None
            draft_games.append({
                "home_team_id": home, "away_team_id": away,
                "home_team_name": team_name(home), "away_team_name": team_name(away),
                "division_id": division_id,
                "ice_slot_id": chosen.id,
                "rink_id": chosen.rink_id,
                "rink_name": rink.name if rink else None,
                "start_time": chosen.start_time.isoformat(),
                "end_time": chosen.end_time.isoformat(),
            })
        else:
            # No free slot at all, or every candidate hit a constraint.
            if not reasons:
                reason = "No available ice slot for this pairing."
            else:
                reason = "No slot satisfies constraints: " + \
                    ", ".join(sorted(set(reasons))) + "."
            unscheduled.append({
                "home_team_id": home, "away_team_id": away,
                "home_team_name": team_name(home), "away_team_name": team_name(away),
                "division_id": division_id, "reason": reason,
            })
    return {"division_id": division_id, "team_count": len(teams),
            "draft_games": draft_games, "unscheduled": unscheduled}
